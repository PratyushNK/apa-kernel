"""
kernel/verification/verifier.py

Phase-1: numeric range invariant checks on proposed policy vector.
Future: TLA+ model checking integration point.

Each check maps to a named invariant from the project spec.
"""

from __future__ import annotations


class InvariantVerifier:

    def check(self, proposed_theta: dict) -> tuple[bool, list[str]]:
        """
        Validate proposed policy vector against all invariants.
        Returns (is_valid, list_of_violations).
        Empty violations list means valid.
        """
        violations = []

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