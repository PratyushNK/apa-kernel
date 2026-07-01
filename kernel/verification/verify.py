"""
kernel/verification/verify.py

Two-layer policy verification harness:
  1. Python-native InvariantChecker — fast, no jar required (pre-commit)
  2. TLCRunner — exhaustive breadth-first model checking (pre-promotion)

Usage:
    python verify.py                                 # Python checker only
    python verify.py --jar tla_specs/tla2tools.jar   # + TLC exhaustive check
    python verify.py --suite base                    # single suite
    python verify.py --json                          # machine-readable output
    python verify.py --show-tlc                      # always print TLC output
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import os
import signal
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Policy parameter mirror of PolicyVector in policy_engine.py
# ---------------------------------------------------------------------------

@dataclass
class PolicyParams:
    provider_priority      : list[str]        = field(default_factory=lambda: ["G1", "G2"])
    provider_weights       : dict[str, float] = field(default_factory=lambda: {"G1": 0.5, "G2": 0.5})
    weight_learning_rate   : float            = 0.1
    max_retry              : int              = 3
    retryable_statuses     : list[str]        = field(default_factory=lambda: ["SOFT_DECLINE", "TIMEOUT"])
    base_backoff_ms        : int              = 100
    backoff_multiplier     : float            = 2.0
    retry_budget_window_ms : int              = 60_000
    max_retries_per_window : int              = 200

    @classmethod
    def base(cls) -> "PolicyParams":
        return cls()

    @classmethod
    def adapted_aggressive(cls) -> "PolicyParams":
        return cls(max_retry=5, max_retries_per_window=500)

    @classmethod
    def adapted_conservative(cls) -> "PolicyParams":
        return cls(max_retry=1, retryable_statuses=["SOFT_DECLINE"], max_retries_per_window=50)

    @classmethod
    def adapted_fast_backoff(cls) -> "PolicyParams":
        return cls(backoff_multiplier=1.5)

    @classmethod
    def adapted_g1_preferred(cls) -> "PolicyParams":
        return cls(provider_weights={"G1": 0.8, "G2": 0.2})

    @classmethod
    def adapted_timeout_only(cls) -> "PolicyParams":
        return cls(retryable_statuses=["TIMEOUT"])


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class VerificationStatus(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    ERROR   = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass
class InvariantResult:
    name    : str
    status  : VerificationStatus
    message : str = ""


@dataclass
class VerificationResult:
    suite_name  : str
    params_name : str
    params      : PolicyParams
    status      : VerificationStatus
    invariants  : list[InvariantResult]
    tlc_output  : str = ""
    error       : str = ""

    def passed(self) -> bool:
        return self.status == VerificationStatus.PASS

    def summary(self) -> str:
        icons = {
            VerificationStatus.PASS    : "✓",
            VerificationStatus.FAIL    : "✗",
            VerificationStatus.ERROR   : "⚠",
            VerificationStatus.SKIPPED : "—",
        }
        icon  = icons.get(self.status, "?")
        lines = [f"  {icon} [{self.status.value}] {self.suite_name} / {self.params_name}"]
        for inv in self.invariants:
            i_icon = "✓" if inv.status == VerificationStatus.PASS else "✗"
            lines.append(f"      {i_icon} {inv.name}: {inv.message or inv.status.value}")
        if self.error:
            lines.append(f"      ERROR: {self.error}")
        if self.tlc_output:
            tlc_lines = self.tlc_output.strip().splitlines()
            preview   = tlc_lines[-20:] if len(tlc_lines) > 20 else tlc_lines
            lines.append("      --- TLC/SANY output ---")
            for ln in preview:
                lines.append(f"      {ln}")
            lines.append("      ---")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Python-native invariant checker (no jar required)
# ---------------------------------------------------------------------------

@dataclass
class TxnState:
    txn_status         : str
    attempt_count      : int
    last_status        : str
    current_provider   : str
    provider_up        : dict[str, bool]
    retry_window_count : int
    extra_state        : dict[str, Any]

    @classmethod
    def fresh(cls, providers: list[str]) -> "TxnState":
        return cls(
            txn_status         = "PENDING",
            attempt_count      = 0,
            last_status        = "SUCCESS",
            current_provider   = "",
            provider_up        = {p: True for p in providers},
            retry_window_count = 0,
            extra_state        = {},
        )


class InvariantChecker:
    ATTEMPT_STATUSES = {"SUCCESS", "SOFT_DECLINE", "HARD_DECLINE", "TIMEOUT"}
    TXN_STATUSES     = {"PENDING", "SUCCESS", "FAILED"}

    def __init__(self, params: PolicyParams):
        self.p = params

    def check_type_invariant(self, s: TxnState) -> InvariantResult:
        failures = []
        if s.txn_status not in self.TXN_STATUSES:
            failures.append(f"txn_status={s.txn_status!r} not in {self.TXN_STATUSES}")
        if not isinstance(s.attempt_count, int) or s.attempt_count < 0:
            failures.append(f"attempt_count={s.attempt_count} not a non-negative int")
        if s.last_status not in self.ATTEMPT_STATUSES:
            failures.append(f"last_status={s.last_status!r} not in {self.ATTEMPT_STATUSES}")
        providers = set(self.p.provider_priority)
        if s.current_provider not in (providers | {""}):
            failures.append(f"current_provider={s.current_provider!r} invalid")
        if set(s.provider_up.keys()) != providers:
            failures.append(f"provider_up keys {set(s.provider_up)} != {providers}")
        if not isinstance(s.retry_window_count, int) or s.retry_window_count < 0:
            failures.append(f"retry_window_count={s.retry_window_count} invalid")
        msg = "; ".join(failures) if failures else "ok"
        return InvariantResult(
            "TypeInvariant",
            VerificationStatus.FAIL if failures else VerificationStatus.PASS, msg)

    def check_i2(self, s: TxnState) -> InvariantResult:
        # I2 — Retry Bound: attempt_count never exceeds MAX_RETRY
        ok = s.attempt_count <= self.p.max_retry
        return InvariantResult(
            "I2_RetryBound",
            VerificationStatus.PASS if ok else VerificationStatus.FAIL,
            "ok" if ok else f"attempt_count={s.attempt_count} max={self.p.max_retry}")

    def check_i3(self, s: TxnState) -> InvariantResult:
        # I3 — Terminal Absorption: no attempts beyond max after terminal state
        ok = not (s.txn_status in {"SUCCESS", "FAILED"} and
                  s.attempt_count > self.p.max_retry)
        return InvariantResult(
            "I3_TerminalAbsorption",
            VerificationStatus.PASS if ok else VerificationStatus.FAIL,
            "ok" if ok else f"terminal state {s.txn_status} with attempt={s.attempt_count}")

    def check_i4(self, s: TxnState) -> InvariantResult:
        # I4 — Circuit Respect: never route to a DOWN provider
        if s.current_provider != "":
            if not s.provider_up.get(s.current_provider, False):
                return InvariantResult(
                    "I4_CircuitRespect", VerificationStatus.FAIL,
                    f"routed to {s.current_provider!r} which is DOWN")
        return InvariantResult("I4_CircuitRespect", VerificationStatus.PASS, "ok")

    def check_i5(self, s: TxnState) -> InvariantResult:
        # I5 — Weight Domain: provider weights only for known providers
        providers = set(self.p.provider_priority)
        unknown   = set(self.p.provider_weights.keys()) - providers
        ok        = len(unknown) == 0
        return InvariantResult(
            "I5_WeightDomainValid",
            VerificationStatus.PASS if ok else VerificationStatus.FAIL,
            "ok" if ok else f"unknown providers in weights: {unknown}")

    def _reachable_states(self) -> list[TxnState]:
        providers = self.p.provider_priority
        all_up    = {p: True  for p in providers}
        all_down  = {p: False for p in providers}
        g1_down   = {p: (p != providers[0]) for p in providers} if providers else {}
        windows   = [0, self.p.max_retries_per_window // 2, self.p.max_retries_per_window]
        states: list[TxnState] = []

        def add(txn, attempt, last, provider, pu, window):
            states.append(TxnState(txn, attempt, last, provider, dict(pu), window, {}))

        # Initial state
        add("PENDING", 0, "SUCCESS", "", all_up, 0)

        # First attempt outcomes
        for provider in providers:
            for window in windows:
                add("SUCCESS", 1, "SUCCESS",     provider, all_up, window)
                add("FAILED",  1, "HARD_DECLINE", provider, all_up, window)
                for last in self.p.retryable_statuses:
                    add("PENDING", 1, last, provider, all_up, window)
                    if len(providers) >= 2:
                        add("PENDING", 1, last, providers[1], g1_down, window)

        # Mid-retry states
        for attempt in range(2, self.p.max_retry + 1):
            for last in self.p.retryable_statuses:
                for provider in providers:
                    for window in windows:
                        if window < self.p.max_retries_per_window:
                            add("PENDING", attempt, last, provider, all_up, window)
            for provider in providers:
                add("SUCCESS", attempt, "SUCCESS", provider, all_up, 0)

        # Exhausted retry states
        for last in self.p.retryable_statuses:
            for provider in providers:
                for window in windows:
                    add("FAILED", self.p.max_retry, last, provider, all_up, window)
                add("FAILED", self.p.max_retry, last, provider, all_up,
                    self.p.max_retries_per_window)

        # All providers down — failed at route
        add("FAILED", 0, "SUCCESS", "", all_down, 0)
        for last in self.p.retryable_statuses:
            add("FAILED", 1, last, "", all_down, 1)

        return states

    def run_all(self) -> list[InvariantResult]:
        checks = [
            self.check_type_invariant,
            self.check_i2,
            self.check_i3,
            self.check_i4,
            self.check_i5,
        ]
        aggregated: dict[str, InvariantResult] = {}
        for state in self._reachable_states():
            for check in checks:
                result   = check(state)
                existing = aggregated.get(result.name)
                if existing is None or existing.status == VerificationStatus.PASS:
                    aggregated[result.name] = result
        # ensure all invariant names present even if no state triggered them
        for check in checks:
            probe = check(TxnState.fresh(self.p.provider_priority))
            if probe.name not in aggregated:
                aggregated[probe.name] = InvariantResult(
                    probe.name, VerificationStatus.PASS, "ok")
        return list(aggregated.values())


# ---------------------------------------------------------------------------
# TLC config + spec generator
# ---------------------------------------------------------------------------

class TLCConfig:

    def __init__(self, spec_dir: Path):
        self.spec_dir = spec_dir

    @staticmethod
    def _tla_set(items: list[str]) -> str:
        return "{" + ", ".join(f'"{x}"' for x in items) + "}"

    def generate(self, name: str, params: PolicyParams) -> tuple[Path, Path, Path]:
        spec_name = f"TB_{name}"
        tla_path  = self.spec_dir / f"{spec_name}.tla"
        cfg_path  = self.spec_dir / f"{spec_name}.cfg"
        cfg_fair  = self.spec_dir / f"{spec_name}_fair.cfg"

        # Allow a "fast mode" to clamp large numeric bounds that blow up
        # TLC state space. This does not remove any checks; it merely
        # reduces domain sizes for quicker, conservative verification runs.
        import os as _os
        fast_mode = _os.getenv("VERIFIER_TLC_FAST_MODE", "0") == "1"
        max_window_orig = params.max_retries_per_window
        max_retry_orig = params.max_retry
        if fast_mode:
            max_window = min(max_window_orig, int(_os.getenv("VERIFIER_TLC_MAX_WINDOW_CLAMP", "10")))
            max_retry = min(max_retry_orig, int(_os.getenv("VERIFIER_TLC_MAX_RETRY_CLAMP", "3")))
            print(f"[verify] VERIFIER_TLC_FAST_MODE active: MaxRetriesPerWindow {max_window_orig} -> {max_window}, MaxRetry {max_retry_orig} -> {max_retry}")
        else:
            max_window = max_window_orig
            max_retry = max_retry_orig

        pw_entries = " @@ ".join(
            f'("{k}" :> {int(round(v * 100))})'
            for k, v in params.provider_weights.items()
        )

        # Read template and substitute placeholders.
        # TB_template.tla stores raw TLA+ so Python never interprets backslashes.
        template_path = self.spec_dir / "TB_template.tla"
        tla_content = (
            template_path.read_text()
            .replace("%%SPEC_NAME%%", spec_name)
            .replace("%%PROVIDERS%%",  self._tla_set(params.provider_priority))
            .replace("%%MAX_RETRY%%",  str(max_retry))
            .replace("%%RETRYABLE%%",  self._tla_set(params.retryable_statuses))
            .replace("%%MAX_WINDOW%%", str(max_window))
            .replace("%%WEIGHTS%%",    pw_entries)
        )
        # If fast mode is enabled, also clamp the allowed execution depth
        # by restricting Next to only fire while `step_counter < MaxSteps`.
        if fast_mode:
            # Default fast-mode max steps chosen from experiments to balance
            # depth vs runtime (~5-6s on typical dev machines).
            # Use a stuttering ELSE branch when the depth bound is reached
            # so TLC does not report a deadlock at MaxSteps.
            max_steps = int(_os.getenv("VERIFIER_TLC_MAX_STEPS", "384"))
            tla_content = tla_content.replace(
                "\nNext == StandardNext\n",
                f"\nMaxSteps == {max_steps}\nNext == IF step_counter < MaxSteps THEN StandardNext ELSE UNCHANGED vars\n",
            )

        # Build an explicit ProviderWeights sum operator in the TLA module
        providers = params.provider_priority
        if providers:
            sum_expr = " + ".join(f'ProviderWeights["{p}"]' for p in providers)
            sum_op = f"\nProviderWeightsSumOk == ({sum_expr}) = 100\n"
        else:
            sum_op = "\nProviderWeightsSumOk == TRUE\n"

        # Append the operator to the TLA content so the cfg can reference it.
        # Insert the operator before the terminating `====` marker so it is
        # defined inside the module (not appended after the module end).
        if "====" in tla_content:
            tla_content = tla_content.replace("\n====", f"\n{sum_op}\n====", 1)
        else:
            tla_content = tla_content + "\n" + sum_op

        # base cfg (no environment fairness) — used to check safety invariants unconditionally
        cfg_base_content = "\n".join([
            f"SPECIFICATION {spec_name}Spec",
            "INVARIANT TypeInvariant",
            "INVARIANT I1_SingleSettlement",
            "INVARIANT I2_RetryBound",
            "INVARIANT I3_TerminalAbsorption",
            "INVARIANT I4_CircuitRespect",
            "INVARIANT I5_WeightDomainValid",
            "INVARIANT ProviderWeightsSumOk",
            "",
        ])

        # fairness cfg — runs the spec with the environment fairness assumption
        cfg_fair_content = "\n".join([
            "SPECIFICATION SpecWithFairness",
            "INVARIANT TypeInvariant",
            "INVARIANT I1_SingleSettlement",
            "INVARIANT I2_RetryBound",
            "INVARIANT I3_TerminalAbsorption",
            "INVARIANT I4_CircuitRespect",
            "INVARIANT I5_WeightDomainValid",
            "INVARIANT ProviderWeightsSumOk",
            "PROPERTY I1_SingleSettlementProp",
            "PROPERTY I3_TerminalAbsorptionProp",
            "PROPERTY I4_CircuitRespectProp",
            "PROPERTY L1_EventualTerminalProp",
            "PROPERTY L2_BoundedTerminalProp",
            "PROPERTY L1_BoundedAttemptsProp",
            "",
        ])

        tla_path.write_text(tla_content)
        cfg_path.write_text(cfg_base_content)
        cfg_fair.write_text(cfg_fair_content)
        return tla_path, cfg_path, cfg_fair


# ---------------------------------------------------------------------------
# SANY syntax checker
# ---------------------------------------------------------------------------

class SANYChecker:

    def __init__(self, jar_path: Path | None):
        self.jar = jar_path

    def available(self) -> bool:
        return self.jar is not None and self.jar.exists() and self.jar.stat().st_size > 0

    def check(self, tla_path: Path) -> tuple[bool, str]:
        if not self.available():
            return True, "SANY skipped (tla2tools.jar not available)"
        spec_dir = str(tla_path.parent.resolve())
        jar_str = str(self.jar.resolve()) if self.jar is not None else ""
        cmd = [
            "java",
            f"-DTLA-Library={spec_dir}",
            "-cp", jar_str,
            "tla2sany.SANY",
            tla_path.name,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                cwd=str(tla_path.parent.resolve()),
            )
            out = (result.stdout + result.stderr).strip()
            return result.returncode == 0, out
        except Exception as e:
            return False, str(e)


# ---------------------------------------------------------------------------
# TLC exhaustive model checker
# ---------------------------------------------------------------------------

class TLCRunner:

    def __init__(self, jar_path: Path | None, workers: int = 2, timeout: int | None = None):
        self.jar     = jar_path
        self.workers = workers
        # Timeout in seconds for the TLC subprocess; default from env or 300s
        if timeout is None:
            try:
                timeout = int(os.getenv("VERIFIER_TLC_TIMEOUT", "300"))
            except Exception:
                timeout = 300
        self.timeout = timeout

    def available(self) -> bool:
        return self.jar is not None and self.jar.exists() and self.jar.stat().st_size > 0

    def run(self, tla_path: Path, cfg_path: Path) -> tuple[bool, str]:
        if not self.available():
            return False, "TLC not available"

        # unique metadir per suite so parallel runs don't collide
        metadir = str(tla_path.parent / "states" / tla_path.stem)
        jar_str = str(self.jar.resolve()) if self.jar is not None else ""
        cmd = [
            "java", "-Xmx2g", "-XX:+UseParallelGC",
            "-jar", jar_str,
            "-config", cfg_path.name,
            "-workers", str(self.workers),
            "-metadir", metadir,
            # Allow overriding the JVM heap via env var `VERIFIER_TLC_XMX` (e.g. "2g").
            # Default remains 2g for backwards compatibility.
            initial_xmx = os.getenv("VERIFIER_TLC_XMX", "2g")
            cmd = [
                "java", f"-Xmx{initial_xmx}", "-XX:+UseParallelGC",
                "-jar", jar_str,
                "-config", cfg_path.name,
                "-workers", str(self.workers),
                "-metadir", metadir,
                "-nowarning",
                tla_path.name,
            ]
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(tla_path.parent.resolve()),
                start_new_session=True,
            )
            # Allow an environment override to force unbounded TLC runs for
            # experimental or test purposes (VERIFIER_TLC_UNBOUNDED=1).
            unbounded = os.getenv("VERIFIER_TLC_UNBOUNDED", "0") == "1"
            timeout_arg = None if unbounded else self.timeout

            try:
                out, err = proc.communicate(timeout=timeout_arg)
                out_text = (out or "") + (err or "")
                out_text = out_text.strip()
                ok = (
                    "Model checking completed. No error has been found." in out_text
                    or (
                        "Model checking completed" in out_text
                        and "violated"            not in out_text
                        and "Exception"           not in out_text
                        and "ConfigFileException" not in out_text
                    )
                )

                # Detect known TLC runtime failures (e.g. Java ArithmeticException
                # Division by zero) and retry with a safer single-worker run.
                lowered = out_text.lower()
                if (not ok) and ("division by zero" in lowered or "arithmeticexception" in lowered):
                    try:
                        # Retry with a single worker to avoid concurrency-related
                        # issues in the TLC runtime. Use a separate metadir.
                        retry_cmd = [c for c in cmd]
                        # replace workers argument (assumes '-workers', <n> present)
                        if "-workers" in retry_cmd:
                            idx = retry_cmd.index("-workers")
                            retry_cmd[idx + 1] = "1"
                        retry_metadir = metadir + "_w1"
                        retry_cmd[retry_cmd.index("-metadir") + 1] = retry_metadir

                        proc2 = subprocess.Popen(
                            retry_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            cwd=str(tla_path.parent.resolve()),
                            start_new_session=True,
                        )
                        out2, err2 = proc2.communicate(timeout=timeout_arg)
                        out_text2 = ((out2 or "") + (err2 or "")).strip()
                        ok2 = (
                            "Model checking completed. No error has been found." in out_text2
                            or (
                                "Model checking completed" in out_text2
                                and "violated"            not in out_text2
                                and "Exception"           not in out_text2
                                and "ConfigFileException" not in out_text2
                            )
                        )
                        if ok2:
                            return True, out_text2 + "\n[Retried with -workers 1]"
                        return False, out_text2 + "\n[Retried with -workers 1]"
                    except Exception:
                        # fallback to returning the original output
                        return False, out_text

                return ok, out_text
            except subprocess.TimeoutExpired:
                # Best-effort terminate the whole process group.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                # Give it a short grace period to flush output
                try:
                    out, err = proc.communicate(timeout=5)
                except Exception:
                    out, err = ("", "")
                # If still alive, force kill
                try:
                    if proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                except Exception:
                    pass
                out_text = ((out or "") + (err or "")).strip()
                return False, f"TLC timed out after {self.timeout}s\n{out_text}"
        except Exception as e:
            return False, str(e)


# ---------------------------------------------------------------------------
# PolicyVerifier — orchestrates all suites
# ---------------------------------------------------------------------------

class PolicyVerifier:

    SUITES: dict[str, PolicyParams] = {
        "base"                : PolicyParams.base(),
        "adapted_aggressive"  : PolicyParams.adapted_aggressive(),
        "adapted_conservative": PolicyParams.adapted_conservative(),
        "adapted_fast_backoff": PolicyParams.adapted_fast_backoff(),
        "adapted_g1_preferred": PolicyParams.adapted_g1_preferred(),
        "adapted_timeout_only": PolicyParams.adapted_timeout_only(),
    }

    def __init__(self, jar_path: Path | None = None):
        self._jar     = jar_path
        self._sany    = SANYChecker(jar_path)
        self._tlc     = TLCRunner(jar_path)
        self._cfg_gen = TLCConfig(Path(__file__).parent / "tla_specs")

    def _run_suite(self, name: str, params: PolicyParams) -> VerificationResult:
        # Always run Python checker first
        checker     = InvariantChecker(params)
        inv_results = checker.run_all()
        tlc_output  = ""
        error       = ""

        if self._tlc.available():
            tla_path, cfg_path, cfg_fair = self._cfg_gen.generate(name, params)

            sany_ok, sany_out = self._sany.check(tla_path)
            if not sany_ok:
                return VerificationResult(
                    suite_name  = name,
                    params_name = name,
                    params      = params,
                    status      = VerificationStatus.ERROR,
                    invariants  = inv_results,
                    tlc_output  = sany_out,
                    error       = "SANY syntax error",
                )

            tlc_ok, tlc_output = self._tlc.run(tla_path, cfg_path)
            if not tlc_ok:
                error = "TLC found invariant violation or error"

        all_pass = all(r.status == VerificationStatus.PASS for r in inv_results)
        status   = VerificationStatus.PASS if (all_pass and not error) else VerificationStatus.FAIL

        return VerificationResult(
            suite_name  = name,
            params_name = name,
            params      = params,
            status      = status,
            invariants  = inv_results,
            tlc_output  = tlc_output,
            error       = error,
        )

    def verify_base(self) -> VerificationResult:
        return self._run_suite("base", self.SUITES["base"])

    def verify_adapted(self) -> list[VerificationResult]:
        return [self._run_suite(n, p) for n, p in self.SUITES.items() if n != "base"]

    def verify_all(self) -> list[VerificationResult]:
        return [self.verify_base()] + self.verify_adapted()

    def verify_custom(self, name: str, params: PolicyParams) -> VerificationResult:
        """Verify any arbitrary policy — used by adaptation pipeline before promotion."""
        return self._run_suite(name, params)

    def compare(self, base_result: VerificationResult,
                adapted_results: list[VerificationResult]) -> dict:
        base_map = {r.name: r.status for r in base_result.invariants}
        diff = {}
        for result in adapted_results:
            adapted_map = {r.name: r.status for r in result.invariants}
            changed = {
                inv: {"base": base_map.get(inv, "—"), "adapted": adapted_map.get(inv, "—")}
                for inv in (set(base_map) | set(adapted_map))
                if base_map.get(inv) != adapted_map.get(inv)
            }
            diff[result.params_name] = {
                "overall_base"    : base_result.status.value,
                "overall_adapted" : result.status.value,
                "invariant_diffs" : changed,
            }
        return diff


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="APA Policy Engine — TLA+ Verifier")
    parser.add_argument("--suite",    choices=["base", "adapted", "all"], default="all")
    parser.add_argument("--jar",      default=None, help="Path to tla2tools.jar")
    parser.add_argument("--json",     action="store_true")
    parser.add_argument("--show-tlc", action="store_true",
                        help="Always print full TLC output even on PASS")
    args = parser.parse_args()

    jar      = Path(args.jar) if args.jar else Path(__file__).parent / "tla2tools.jar"
    verifier = PolicyVerifier(jar_path=jar if jar.exists() else None)

    if args.suite == "base":
        results = [verifier.verify_base()]
    elif args.suite == "adapted":
        results = verifier.verify_adapted()
    else:
        results = verifier.verify_all()

    if args.json:
        print(json.dumps([
            {
                "suite"      : r.suite_name,
                "status"     : r.status.value,
                "invariants" : [{"name": i.name, "status": i.status.value,
                                 "message": i.message} for i in r.invariants],
                "tlc_output" : r.tlc_output,
                "error"      : r.error,
            }
            for r in results
        ], indent=2))
    else:
        tlc_avail = jar.exists() and jar.stat().st_size > 0 if jar else False
        print("\n" + "=" * 60)
        print("  APA Policy Engine — TLA+ Invariant Verification")
        print("=" * 60)
        print(f"  TLC model checker : "
              f"{'available ✓' if tlc_avail else 'not available — Python checker active'}")
        print("=" * 60 + "\n")

        for r in results:
            # suppress TLC output on clean pass unless --show-tlc
            if not args.show_tlc and r.status == VerificationStatus.PASS:
                r.tlc_output = ""
            print(r.summary())
            print()

        if len(results) > 1:
            base    = results[0]
            adapted = results[1:]
            diff    = verifier.compare(base, adapted)
            print("\n" + "-" * 60)
            print("  Diff: Base vs Adapted Policies")
            print("-" * 60)
            for suite_name, d in diff.items():
                if d["invariant_diffs"]:
                    print(f"\n  {suite_name}:")
                    for inv, change in d["invariant_diffs"].items():
                        print(f"    {inv}: {change['base']} → {change['adapted']}")
                else:
                    print(f"\n  {suite_name}: no invariant changes vs base ✓")

        total  = len(results)
        passed = sum(1 for r in results if r.passed())
        print(f"\n{'=' * 60}")
        print(f"  Result: {passed}/{total} suites passed all invariants")
        print(f"{'=' * 60}\n")

    return 0 if all(r.passed() for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())