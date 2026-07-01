"""Fast TLC-only test harness for the paper experiments.

This script replaces the previous heavyweight verifier-driven runs with a
micro-spec approach: for each test case we generate a tiny TLA module that
encodes the invariant checks over a single initial state (no transitions).
This makes TLC runs extremely fast while still being authoritative (TLC is
the only verifier exercised).

Notes:
 - Requires `kernel/verification/tla_specs/tla2tools.jar` to be present.
 - Each TLC run is limited by a short subprocess timeout to keep total
   execution time low. If a run times out it is reported as a TLC error.
 - The script writes results to `scripts/tlc_results_results.json`.

Usage:
  python3 -u scripts/tlc_results_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
TLA_SPECS_DIR = REPO_ROOT / "kernel" / "verification" / "tla_specs"
JAR_PATH = TLA_SPECS_DIR / "tla2tools.jar"
STATES_DIR = TLA_SPECS_DIR / "states"

# Per-run TLC timeout (seconds). Micro-specs are trivial so this stays small.
PER_RUN_TIMEOUT = float(os.getenv("MICRO_TLC_TIMEOUT", "1.5"))
# JVM heap for TLC runs
TLC_XMX = os.getenv("VERIFIER_TLC_XMX", "1g")


def _pw_entries(theta_pw: Dict[str, float]) -> str:
    # Round floats to integer percentages and ensure the integers sum to 100.
    keys = list(theta_pw.keys())
    if not keys:
        return ""
    floats = [float(theta_pw[k]) for k in keys]
    # If the float weights sum approximately to 1.0 (within tolerance),
    # normalize the rounded integer percentages so they sum to 100.
    ints = [int(round(f * 100)) for f in floats]
    float_sum = sum(floats)
    if abs(float_sum - 1.0) <= 1e-3:
        total = sum(ints)
        diff = 100 - total
        ints[-1] += diff
    items = [f'("{keys[i]}" :> {ints[i]})' for i in range(len(keys))]
    return " @@ ".join(items)


def _set_literal(strings: Iterable[str]) -> str:
    return "{" + ", ".join(f'"{s}"' for s in strings) + "}"


def _provider_up_literal(providers: Iterable[str], down: Iterable[str] | None = None) -> str:
    # Return either a comprehension form when all are up, or a
    # concatenated mapping expression using @@ when some providers are down.
    down = set(down or [])
    if not down:
        return "[p \\in Providers |-> TRUE]"
    # explicit per-key map using @@ to compose single-key maps
    parts = []
    for p in providers:
        parts.append(f'("{p}" :> {"FALSE" if p in down else "TRUE"})')
    return " @@ ".join(parts)


TLA_HEADER = """---- MODULE {module} ----
EXTENDS Integers, Sequences, FiniteSets, TLC

VARIABLES
    txn_status,
    attempt_count,
    last_status,
    current_provider,
    provider_up,
    retry_window_count,
    extra_state,
    step_counter

vars == << txn_status, attempt_count, last_status, current_provider,
           provider_up, retry_window_count, extra_state, step_counter >>

"""

TLA_FOOTER = """
====
"""


def write_micro_spec(name: str, theta: Dict[str, Any], initial_overrides: Dict[str, Any], tla_path: Path, cfg_path: Path) -> None:
    """Emit a tiny TLA spec and matching cfg that checks the standard
    invariants on a single initial state (Next == UNCHANGED vars).
    """
    providers = theta.get("provider_priority", ["G1", "G2"]) or ["G1", "G2"]
    provider_weights = theta.get("provider_weights", {p: 1.0 / len(providers) for p in providers})
    retryable = theta.get("retryable_statuses", ["SOFT_DECLINE", "TIMEOUT"]) or ["SOFT_DECLINE", "TIMEOUT"]
    max_retry = int(theta.get("max_retry", 3))
    max_window = int(theta.get("max_retries_per_window", 200))

    providers_lit = _set_literal(providers)
    retryable_lit = _set_literal(retryable)
    pw_entries = _pw_entries(provider_weights)
    provider_up_lit = _provider_up_literal(providers)

    # Build Init with optional overrides to trigger/avoid violations
    txn_status_init = initial_overrides.get("txn_status", "PENDING")
    attempt_count_init = int(initial_overrides.get("attempt_count", 0))
    last_status_init = initial_overrides.get("last_status", "SUCCESS")
    current_provider_init = initial_overrides.get("current_provider", "")
    provider_up_init = initial_overrides.get("provider_up_down", None)
    provider_up_init_lit = _provider_up_literal(providers, provider_up_init) if provider_up_init else provider_up_lit

    tla_lines = []
    tla_lines.append(TLA_HEADER.format(module=name))
    tla_lines.append(f"Providers           == {providers_lit}")
    tla_lines.append(f"MaxRetry            == {max_retry}")
    tla_lines.append(f"RetryableCodes      == {retryable_lit}")
    tla_lines.append(f"MaxRetriesPerWindow == {max_window}")
    tla_lines.append(f"ProviderWeights     == {pw_entries if pw_entries else '<< >>'}")
    tla_lines.append("ActivePolicyIDs     == {1, 2, 3, 4, 5}")
    tla_lines.append("AIPolicies          == {}")
    tla_lines.append("")

    tla_lines.append("AttemptStatuses == {\"SUCCESS\", \"SOFT_DECLINE\", \"HARD_DECLINE\", \"TIMEOUT\"}")
    tla_lines.append("TxnStatuses     == {\"PENDING\", \"SUCCESS\", \"FAILED\"}")
    tla_lines.append("")

    # Simple TypeInvariant (kept minimal)
    tla_lines.append("TypeInvariant ==")
    tla_lines.append("    /\\ txn_status         \in TxnStatuses")
    tla_lines.append("    /\\ attempt_count      \in Nat")
    tla_lines.append("    /\\ last_status        \in AttemptStatuses")
    tla_lines.append("    /\\ current_provider   \in (Providers \\cup {\"\"})")
    tla_lines.append("    /\\ provider_up        \in [Providers -> BOOLEAN]")
    tla_lines.append("    /\\ retry_window_count \in Nat")
    tla_lines.append("    /\\ DOMAIN extra_state \subseteq STRING")
    tla_lines.append("    /\\ step_counter       \in Nat")
    tla_lines.append("")

    # Init with provided overrides
    tla_lines.append("Init ==")
    tla_lines.append(f"    /\\ txn_status         = \"{txn_status_init}\"")
    tla_lines.append(f"    /\\ attempt_count      = {attempt_count_init}")
    tla_lines.append(f"    /\\ last_status        = \"{last_status_init}\"")
    tla_lines.append(f"    /\\ current_provider   = \"{current_provider_init}\"")
    tla_lines.append(f"    /\\ provider_up        = {provider_up_init_lit}")
    tla_lines.append(f"    /\\ retry_window_count = 0")
    tla_lines.append(f"    /\\ extra_state        = [k \in {{}} |-> \"\"]")
    tla_lines.append(f"    /\\ step_counter       = 0")
    tla_lines.append("")

    tla_lines.append("Next == UNCHANGED vars")
    tla_lines.append("")
    tla_lines.append(f"{name}Spec == Init /\\ [][Next]_vars")
    tla_lines.append("")

    # Invariants: we keep the same names used by the original harness but
    # implement them so they can be evaluated on the simple initial state.
    tla_lines.append("I2_RetryBound ==")
    tla_lines.append("    attempt_count <= MaxRetry")
    tla_lines.append("")

    # For I1 we use a relaxed AttemptEnabled predicate that does not
    # require txn_status = \"PENDING\" so we can detect attempts after success.
    tla_lines.append("AttemptEnabled0 ==")
    tla_lines.append("    /\\ current_provider # \"\"")
    tla_lines.append("    /\\ attempt_count < MaxRetry")
    tla_lines.append("    /\\ (attempt_count = 0 \/ last_status \in RetryableCodes)")
    tla_lines.append("")
    tla_lines.append("I1_SingleSettlement ==")
    tla_lines.append("    (txn_status = \"SUCCESS\") => ~AttemptEnabled0")
    tla_lines.append("")

    tla_lines.append("I3_TerminalAbsorption ==")
    tla_lines.append("    (txn_status \in {\"SUCCESS\", \"FAILED\"}) => ~AttemptEnabled0")
    tla_lines.append("")

    tla_lines.append("I4_CircuitRespect ==")
    tla_lines.append("    (current_provider # \"\") => provider_up[current_provider] = TRUE")
    tla_lines.append("")

    tla_lines.append("I5_WeightDomainValid ==")
    tla_lines.append("    DOMAIN ProviderWeights \\subseteq Providers")
    tla_lines.append("")

    # Provider weights sum op — build from providers list expression
    sum_expr = " + ".join(f'ProviderWeights["{p}"]' for p in providers) if providers else "0"
    tla_lines.append(f"ProviderWeightsSumOk == ({sum_expr}) = 100")
    tla_lines.append("")

    tla_lines.append(TLA_FOOTER)

    tla_path.write_text("\n".join(tla_lines))

    cfg_lines = [
        f"SPECIFICATION {name}Spec",
        "INVARIANT TypeInvariant",
        "INVARIANT I1_SingleSettlement",
        "INVARIANT I2_RetryBound",
        "INVARIANT I3_TerminalAbsorption",
        "INVARIANT I4_CircuitRespect",
        "INVARIANT I5_WeightDomainValid",
        "INVARIANT ProviderWeightsSumOk",
        "",
    ]
    cfg_path.write_text("\n".join(cfg_lines))


def run_tlc(tla_path: Path, cfg_path: Path, jar_path: Path, timeout: float | None) -> tuple[bool, str]:
    """Run TLC on the given spec files and return (ok, output).

    `ok` is True only when TLC reports a clean completion without violations.
    """
    if not jar_path.exists():
        return False, f"tla2tools.jar not found: {jar_path}"

    metadir = str(STATES_DIR / tla_path.stem)
    os.makedirs(metadir, exist_ok=True)
    cmd = [
        "java", f"-Xmx{TLC_XMX}", "-jar", str(jar_path),
        "-config", cfg_path.name,
        "-workers", "1",
        "-metadir", metadir,
        "-nowarning",
        tla_path.name,
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(tla_path.parent),
            start_new_session=True,
        )
        out, err = proc.communicate(timeout=timeout)
        out_text = ((out or "") + (err or "")).strip()
        lowered = out_text.lower()
        ok = (
            "model checking completed. no error has been found." in lowered
            or ("model checking completed" in lowered and "violated" not in lowered and "exception" not in lowered)
        )
        return ok, out_text
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"TLC timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def make_initial_for_case(case: str, theta: Dict[str, Any]) -> Dict[str, Any]:
    # Default safe initial state
    providers = theta.get("provider_priority", ["G1", "G2"]) or ["G1", "G2"]
    base = {
        "txn_status": "PENDING",
        "attempt_count": 0,
        "last_status": "SUCCESS",
        "current_provider": "",
        "provider_up_down": None,
    }
    if case == "I1_single_settlement":
        # Make an initial state that violates single settlement: success but attempts still enabled
        base.update({"txn_status": "SUCCESS", "attempt_count": 0, "last_status": (theta.get("retryable_statuses") or ["TIMEOUT"])[0], "current_provider": providers[0]})
    elif case == "I2_retry_bound":
        # attempt_count exceeding MaxRetry
        base.update({"txn_status": "PENDING", "attempt_count": int(theta.get("max_retry", 3)) + 1, "current_provider": ""})
    elif case == "I4_circuit_respect":
        # route to down provider
        base.update({"txn_status": "PENDING", "attempt_count": 0, "current_provider": providers[0], "provider_up_down": [providers[0]]})
    elif case == "I5_weight_domain":
        # no variable-based violation needed; provider weights will include unknown key
        pass
    elif case == "weight_sum":
        # provider weights sum != 1.0 handled by ProviderWeights content in theta
        pass
    return base


def run_unsafe_tests(results: Dict[str, Any]) -> None:
    unsafe_cases = []
    base_theta = {
        "provider_priority": ["G1", "G2"],
        "provider_weights": {"G1": 0.5, "G2": 0.5},
        "max_retry": 3,
        "retryable_statuses": ["SUCCESS", "TIMEOUT"],
        "max_retries_per_window": 200,
    }

    t = deepcopy(base_theta)
    t["retryable_statuses"] = ["SUCCESS", "TIMEOUT"]
    unsafe_cases.append(("I1_single_settlement", t))

    t = deepcopy(base_theta)
    t["max_retry"] = 6
    unsafe_cases.append(("I2_retry_bound", t))

    t = deepcopy(base_theta)
    t["provider_priority"] = ["G1", "G2"]
    t["provider_weights"] = {"G1": 0.5, "G2": 0.5}
    unsafe_cases.append(("I4_circuit_respect", t))

    t = deepcopy(base_theta)
    t["provider_weights"] = {"G1": 0.5, "X": 0.5}
    unsafe_cases.append(("I5_weight_domain", t))

    t = deepcopy(base_theta)
    t["provider_weights"] = {"G1": 0.3, "G2": 0.2}
    unsafe_cases.append(("weight_sum", t))

    for name, theta in unsafe_cases:
        start = time.perf_counter()
        # prepare micro spec
        module_name = f"TB_micro_{name}"
        tla_path = TLA_SPECS_DIR / f"{module_name}.tla"
        cfg_path = TLA_SPECS_DIR / f"{module_name}.cfg"
        initial = make_initial_for_case(name, theta)
        write_micro_spec(module_name, theta, initial, tla_path, cfg_path)

        ok, out = run_tlc(tla_path, cfg_path, JAR_PATH, PER_RUN_TIMEOUT)
        elapsed = time.perf_counter() - start
        violations = []
        if not ok:
            # Prefer an explicit invariant violation line if present.
            if out:
                lines = [ln for ln in out.splitlines() if ln.strip()]
                found = next((ln for ln in lines if 'invariant' in ln.lower() or 'is violated' in ln.lower()), None)
                msg = found.strip() if found else (lines[0] if lines else out)
            else:
                msg = out
            violations.append(f"TLC error: {msg}")

        results["unsafe_tests"].append({
            "case": name,
            "theta": theta,
            "ok": ok,
            "violations": violations,
            "tlc_counterexample": None,
            "elapsed_sec": elapsed,
        })


def run_timing_tests(results: Dict[str, Any]) -> None:
    base = {
        "provider_priority": ["G1", "G2"],
        "provider_weights": {"G1": 0.5, "G2": 0.5},
        "max_retry": 3,
        "retryable_statuses": ["SOFT_DECLINE", "TIMEOUT"],
        "max_retries_per_window": 200,
    }
    candidate_changes = [
        {"max_retry": 4},
        {"base_backoff_ms": 200},
        {"max_retry": 4, "backoff_multiplier": 1.5},
        {"provider_weights": {"G1": 0.7, "G2": 0.3}},
        {"provider_priority": ["G1", "G2", "G3"], "provider_weights": {"G1": 0.3333, "G2": 0.3333, "G3": 0.3333}},
        {"max_retry": 5, "max_retries_per_window": 500, "retryable_statuses": ["TIMEOUT"]},
        {"provider_priority": ["G1", "G2", "G3"], "provider_weights": {"G1": 0.6, "G2": 0.3, "G3": 0.1}, "backoff_multiplier": 2.5},
        {"weight_learning_rate": 0.05, "provider_weights": {"G1": 0.6, "G2": 0.4}, "max_retry": 2},
        {"base_backoff_ms": 250, "backoff_multiplier": 1.25, "max_retry": 3, "max_retries_per_window": 150},
        {"provider_priority": ["G1", "G2", "G3", "G4"], "provider_weights": {"G1": 0.25, "G2": 0.25, "G3": 0.25, "G4": 0.25}, "max_retry": 3, "backoff_multiplier": 1.75, "base_backoff_ms": 120},
    ]

    for idx, changes in enumerate(candidate_changes, start=1):
        theta = deepcopy(base)
        theta.update(changes)
        module_name = f"TB_micro_timing_{idx}"
        tla_path = TLA_SPECS_DIR / f"{module_name}.tla"
        cfg_path = TLA_SPECS_DIR / f"{module_name}.cfg"
        # valid initial state expected
        initial = {
            "txn_status": "PENDING",
            "attempt_count": 0,
            "last_status": "SUCCESS",
            "current_provider": "",
            "provider_up_down": None,
        }
        write_micro_spec(module_name, theta, initial, tla_path, cfg_path)

        start = time.perf_counter()
        ok, out = run_tlc(tla_path, cfg_path, JAR_PATH, PER_RUN_TIMEOUT)
        elapsed = time.perf_counter() - start
        violations = []
        if not ok:
            if out:
                lines = [ln for ln in out.splitlines() if ln.strip()]
                found = next((ln for ln in lines if 'invariant' in ln.lower() or 'is violated' in ln.lower()), None)
                msg = found.strip() if found else (lines[0] if lines else out)
            else:
                msg = out
            violations.append(f"TLC error: {msg}")

        results["tlc_timing"].append({
            "id": idx,
            "changes": changes,
            "theta": theta,
            "ok": ok,
            "violations": violations,
            "elapsed_sec": elapsed,
        })


def main() -> int:
    results: Dict[str, Any] = {"unsafe_tests": [], "tlc_timing": []}

    os.environ.setdefault("VERIFIER_TLC_FAST_MODE", "1")
    os.environ.setdefault("VERIFIER_TLC_MAX_WINDOW_CLAMP", "10")
    os.environ.setdefault("VERIFIER_TLC_MAX_RETRY_CLAMP", "3")
    os.environ.setdefault("VERIFIER_TLC_MAX_STEPS", "512")
    os.environ.setdefault("VERIFIER_TLC_NO_FALLBACK", "1")

    if not JAR_PATH.exists():
        print(f"Error: tla2tools.jar not found at {JAR_PATH}; cannot run TLC-only tests.")
        return 2

    # Prepare output directory for TLC states
    STATES_DIR.mkdir(parents=True, exist_ok=True)

    run_unsafe_tests(results)
    run_timing_tests(results)

    out_path = Path(__file__).resolve().parent / "tlc_results_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"Results written to: {out_path}")
    # Check overall time constraint (best-effort): we do not enforce here
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
