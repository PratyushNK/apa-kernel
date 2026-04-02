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
from typing import Any

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

    def check(self, proposed_theta: dict) -> tuple[bool, list[str]]:
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
                    logger.info("TLC skipped: could not construct PolicyParams; falling back to Python checks")
                else:
                    # Only generate TLA/CFG files if TLC (tla2tools.jar) is available
                    jar_path = spec_dir / "tla2tools.jar"
                    workers = int(os.getenv("VERIFIER_TLC_WORKERS", "2"))
                    tlc = TLCRunner(jar_path, workers=workers)
                    if not tlc.available():
                        logger.info("TLC unavailable (tla2tools.jar missing); falling back to Python checks")
                    else:
                        # unique spec name per invocation to avoid metadir collisions
                        nonce = uuid.uuid4().hex[:8]
                        spec_name = f"verifier_adapter_{int(time.time())}_{nonce}"
                        tla_path, cfg_path = TLCConfig(spec_dir).generate(spec_name, params)

                        logger.info("Running TLC for spec %s (workers=%s)", spec_name, workers)
                        ok, out = tlc.run(tla_path, cfg_path)

                        if ok:
                            logger.info("TLC passed for spec %s", spec_name)
                            return True, []

                        # TLC ran but reported a failure/counterexample — surface that result
                        try:
                            metadir = Path(tla_path.parent) / "states" / tla_path.stem
                            metadir.mkdir(parents=True, exist_ok=True)
                            out_path = metadir / "tlc_output.txt"
                            out_path.write_text(out or "")
                            logger.warning("TLC found counterexample for %s; output saved to %s", spec_name, out_path)
                        except Exception as e:
                            logger.exception("Failed to write TLC output: %s", e)

                        last_line = (out.splitlines()[-1] if out else "no TLC output")
                        return False, [f"TLC counterexample: {last_line}"]
            except Exception as e:
                logger.exception("TLC attempt raised exception; falling back to Python checks: %s", e)

        # Fallback: run existing lightweight numeric invariant checks.
        violations.extend(self._check_I2_retry_bound(proposed_theta))
        violations.extend(self._check_provider_weights(proposed_theta))
        violations.extend(self._check_backoff(proposed_theta))
        violations.extend(self._check_retry_budget(proposed_theta))

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