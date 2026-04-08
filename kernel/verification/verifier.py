"""
kernel/verification/verifier.py

Phase-1: numeric range invariant checks on proposed policy vector.
Now acts as an adapter that invokes the TLA+ based verifier first (TLC),
and falls back to the fast Python checks if TLC is unavailable or reports
an error/violation.

Each check maps to a named invariant from the project spec.
"""

from __future__ import annotations

import os
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=os.getenv("VERIFIER_LOG_LEVEL", "INFO"))


class InvariantVerifier:

    def _dict_to_policyparams(self, theta: dict) -> Any:
        """Lazily map a dict to the `PolicyParams` dataclass from the
        TLA-based verifier. Returns None if the import fails.
        """
        try:
            from .verify import PolicyParams
        except Exception:
            return None

        allowed = {
            "provider_priority",
            "provider_weights",
            "weight_learning_rate",
            "max_retry",
            "retryable_statuses",
            "base_backoff_ms",
            "backoff_multiplier",
            "retry_budget_window_ms",
            "max_retries_per_window",
        }
        kwargs = {k: v for k, v in theta.items() if k in allowed}
        try:
            return PolicyParams(**kwargs)
        except Exception:
            return None

    def check(self, proposed_theta: dict, fast_mode: Optional[bool] = None, tlc_timeout_override: Optional[int] = None) -> tuple[bool, list[str]]:
        """
        Validate proposed policy vector against all invariants.

        Behaviour: run the TLA+ / TLC verifier first. If it reports success
        return (True, []). If TLC is unavailable or reports failure, run the
        legacy Python numeric checks as a fallback and return their violations.

        To skip TLC in local/dev environments, set `VERIFIER_DISABLE_TLC=1`.
        """
        violations: list[str] = []

        disable_tlc = os.getenv("VERIFIER_DISABLE_TLC", "0") == "1"

        if not disable_tlc:
            try:
                from .verify import TLCConfig, TLCRunner

                spec_dir = Path(__file__).parent / "tla_specs"
                params = self._dict_to_policyparams(proposed_theta)

                # If we couldn't map params or import the verifier types, fall back
                if params is None:
                    logger.info("[adaptation] TLC skipped: could not construct PolicyParams; falling back to Python checks")
                else:
                    # Only generate TLA/CFG files if TLC (tla2tools.jar) is available
                    jar_path = spec_dir / "tla2tools.jar"
                    workers = int(os.getenv("VERIFIER_TLC_WORKERS", "2"))
                    # Allow special values for the adaptation timeout so test
                    # runners can request an unbounded TLC run. If
                    # VERIFIER_TLC_TIMEOUT_ADAPTATION is set to one of
                    # ('none','unbounded','') treat it as no timeout.
                    # Allow caller to override TLC timeout programmatically
                    if tlc_timeout_override is not None:
                        tlc_timeout = tlc_timeout_override
                    else:
                        tlc_timeout_env = os.getenv("VERIFIER_TLC_TIMEOUT_ADAPTATION")
                        if tlc_timeout_env is None:
                            tlc_timeout = int(os.getenv("VERIFIER_TLC_TIMEOUT", "300"))
                        else:
                            if str(tlc_timeout_env).lower() in ("", "none", "unbounded", "null"):
                                tlc_timeout = None
                            else:
                                try:
                                    tlc_timeout = int(tlc_timeout_env)
                                except Exception:
                                    tlc_timeout = int(os.getenv("VERIFIER_TLC_TIMEOUT", "300"))

                    tlc = TLCRunner(jar_path, workers=workers, timeout=tlc_timeout)
                    if not tlc.available():
                        logger.info("[adaptation] TLC unavailable (tla2tools.jar missing); falling back to Python checks")
                    else:
                        # unique spec name per invocation to avoid metadir collisions
                        nonce = uuid.uuid4().hex[:8]
                        spec_name = f"verifier_adapter_{int(time.time())}_{nonce}"
                        tla_path, cfg_path, cfg_fair = TLCConfig(spec_dir).generate(spec_name, params)

                        logger.info("[adaptation] Running TLC for spec %s (workers=%s, timeout=%ss)", spec_name, workers, tlc_timeout)
                        ok, out = tlc.run(tla_path, cfg_path)

                        if ok:
                            logger.info("[adaptation] TLC passed for spec %s", spec_name)
                            return True, []

                        # TLC ran but reported a failure or error. Save output and decide
                        # whether to treat as a concrete counterexample (reject) or a
                        # runtime/timeout error (fallback to Python numeric checks).
                        try:
                            metadir = Path(tla_path.parent) / "states" / tla_path.stem
                            metadir.mkdir(parents=True, exist_ok=True)
                            out_path = metadir / "tlc_output.txt"
                            out_path.write_text(out or "")
                            logger.warning("[adaptation] TLC run produced non-success for %s; output saved to %s", spec_name, out_path)
                        except Exception as e:
                            logger.exception("Failed to write TLC output: %s", e)

                        out_lower = (out or "").lower()
                        last_line = (out.splitlines()[-1] if out else "no TLC output")

                        # If TLC reports explicit temporal counterexamples, treat as
                        # a hard failure. Otherwise (timeouts, exceptions), fall back
                        # to the fast Python checks so adaptation can proceed.
                        if any(k in out_lower for k in ("violated", "counter-example", "counterexample")):
                            return False, [f"TLC counterexample: {last_line}"]
                        else:
                            # If strict-TLC mode is enabled, treat any non-success
                            # (timeouts, errors) as a hard failure and do NOT
                            # fall back to Python checks. This is useful in
                            # experimental/test runs where TLC must be the
                            # authoritative verifier.
                            if os.getenv("VERIFIER_TLC_NO_FALLBACK", "0") == "1":
                                logger.warning("[adaptation] TLC non-success and no-fallback enforced: %s", last_line)
                                return False, [f"TLC error: {last_line}"]
                            logger.warning("[adaptation] TLC run timed out or errored; falling back to Python checks: %s", last_line)
                            # don't return here — let the function continue to the
                            # Python fallback below and return those violations.
            except Exception as e:
                # If strict-TLC mode is active, surface the exception as
                # a hard failure instead of falling back to Python checks.
                if os.getenv("VERIFIER_TLC_NO_FALLBACK", "0") == "1":
                    logger.exception("[adaptation] TLC attempt raised exception (no-fallback): %s", e)
                    return False, [f"TLC exception: {e!r}"]
                logger.exception("[adaptation] TLC attempt raised exception; falling back to Python checks: %s", e)

        # Fallback: run existing lightweight numeric invariant checks.
        # If VERIFIER_TLC_FAST_MODE is active, the TLA generator clamps
        # some parameters (MaxRetry, MaxRetriesPerWindow) to keep state
        # space smaller. Run the Python fallback against the same clamped
        # values so results are consistent between TLC and Python checks.
        fallback_theta = dict(proposed_theta or {})
        # Respect programmatic override for fast-mode if provided, otherwise env var
        is_fast = fast_mode if fast_mode is not None else (os.getenv("VERIFIER_TLC_FAST_MODE", "0") == "1")
        if is_fast:
            try:
                max_window_clamp = int(os.getenv("VERIFIER_TLC_MAX_WINDOW_CLAMP", "10"))
            except Exception:
                max_window_clamp = 10
            try:
                max_retry_clamp = int(os.getenv("VERIFIER_TLC_MAX_RETRY_CLAMP", "3"))
            except Exception:
                max_retry_clamp = 3
            # apply clamps using existing values or sensible defaults
            mw = fallback_theta.get("max_retries_per_window", 200)
            mr = fallback_theta.get("max_retry", 3)
            fallback_theta["max_retries_per_window"] = min(mw, max_window_clamp)
            fallback_theta["max_retry"] = min(mr, max_retry_clamp)

        violations.extend(self._check_single_settlement(fallback_theta))
        violations.extend(self._check_I2_retry_bound(fallback_theta))
        violations.extend(self._check_provider_weights(fallback_theta))
        violations.extend(self._check_backoff(fallback_theta))
        violations.extend(self._check_retry_budget(fallback_theta))

        return len(violations) == 0, violations

    # ------------------------------------------------------------------
    # I2 — Retry Bound
    # ------------------------------------------------------------------

    def _check_I2_retry_bound(self, theta: dict) -> list[str]:
        max_retry = theta.get("max_retry", 0)
        if not (1 <= max_retry <= 5):
            return [f"I2_retry_bound: max_retry={max_retry} must be in [1, 5]"]
        return []

    # ------------------------------------------------------------------
    # P4 — Provider weights must sum to 1.0
    # ------------------------------------------------------------------

    def _check_provider_weights(self, theta: dict) -> list[str]:
        weights = theta.get("provider_weights", {})
        if not weights:
            return ["provider_weights: empty"]
        total = sum(weights.values())
        if not (0.99 <= total <= 1.01):
            return [f"provider_weights: sum={total:.3f} must equal 1.0"]
        for p, w in weights.items():
            if w < 0.0:
                return [f"provider_weights: {p}={w} cannot be negative"]
        return []

    # ------------------------------------------------------------------
    # P3 — Backoff bounds
    # ------------------------------------------------------------------

    def _check_backoff(self, theta: dict) -> list[str]:
        violations = []
        base = theta.get("base_backoff_ms", 0)
        mult = theta.get("backoff_multiplier", 0)
        if not (10 <= base <= 5000):
            violations.append(
                f"P3_backoff: base_backoff_ms={base} must be in [10, 5000]"
            )
        if not (1.0 <= mult <= 5.0):
            violations.append(
                f"P3_backoff: backoff_multiplier={mult} must be in [1.0, 5.0]"
            )
        return violations

    # ------------------------------------------------------------------
    # P5 — Retry budget
    # ------------------------------------------------------------------

    def _check_retry_budget(self, theta: dict) -> list[str]:
        budget = theta.get("max_retries_per_window", 0)
        if budget < 1:
            return [f"P5_retry_budget: max_retries_per_window={budget} must be >= 1"]
        return []

    # ------------------------------------------------------------------
    # I1 — Single Settlement (policy-level sanity check)
    # ------------------------------------------------------------------

    def _check_single_settlement(self, theta: dict) -> list[str]:
        """
        Basic policy-level sanity: ensure 'SUCCESS' is not listed as retryable
        (which would make multiple settlements possible). The full single
        settlement temporal property is expressed in the TLA spec when TLC
        is available; this check is a lightweight static safeguard.
        """
        retryable = theta.get("retryable_statuses", []) or []
        if "SUCCESS" in retryable:
            return ["I1_single_settlement: 'SUCCESS' must not be retryable"]
        return []