"""
simulator/policy_engine.py

PolicyEngine — evaluates routing and retry decisions against current policy vector θ.

Architecture:
    - Single shared instance across all transactions
    - θ loaded from local JSON file (replaceable with Postgres later)
    - Decision hooks are swappable units (routing, retry)
    - TransactionEngine calls decision points, never touches θ directly

Policy vector θ:
    P1 — gateway selection (binary UP/DOWN fallback)
    P2 — retry eligibility
    P3 — backoff timing
    P4 — provider weights (continuous)
    P5 — global retry budget
    P6 — timeout threshold (consumed by GatewayModel)
"""

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from events import AttemptStatus


# ---------------------------------------------------------------------------
# Policy Vector θ
# ---------------------------------------------------------------------------

@dataclass
class PolicyVector:
    # P1 — Gateway selection fallback order
    provider_priority       : list[str]   = field(default_factory=lambda: ["G1", "G2"])

    # P4 — Provider weights (must sum to 1.0)
    provider_weights        : dict[str, float] = field(default_factory=lambda: {"G1": 0.5, "G2": 0.5})
    weight_learning_rate    : float        = 0.1

    # P2 — Retry eligibility
    max_retry               : int          = 3
    retryable_statuses      : list[str]    = field(default_factory=lambda: ["SOFT_DECLINE", "TIMEOUT"])

    # P3 — Backoff
    base_backoff_ms         : int          = 100
    backoff_multiplier      : float        = 2.0   # exponential: base * 2^(attempt-1)

    # P5 — Global retry budget
    retry_budget_window_ms  : int          = 60_000  # 1 minute window
    max_retries_per_window  : int          = 200


# ---------------------------------------------------------------------------
# Routing Hook (P1 + P4)
# ---------------------------------------------------------------------------

class RoutingHook:

    def __init__(self, theta: PolicyVector, gateway_model):
        self.theta         = theta
        self.gateway_model = gateway_model

    def choose_provider(self, txn_id: str) -> str:
        """
        P1 — if one provider is DOWN, route to the UP one.
        P4 — if both UP, choose by weight.
        If both DOWN, return empty string (TransactionEngine marks FAILED).
        """
        up_providers = [
            p for p in self.theta.provider_priority
            if self.gateway_model.is_up(p)
        ]

        if not up_providers:
            return ""                          # I6 — never route to DOWN gateway

        if len(up_providers) == 1:
            return up_providers[0]

        # P4 — weighted selection among UP providers
        total = sum(self.theta.provider_weights.get(p, 1.0) for p in up_providers)
        weights = [self.theta.provider_weights.get(p, 1.0) / total for p in up_providers]

        import random
        return random.choices(up_providers, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# Retry Hook (P2 + P3 + P5)
# ---------------------------------------------------------------------------

class RetryHook:

    def __init__(self, theta: PolicyVector):
        self.theta           = theta
        self._retry_window   : deque[int] = deque()   # timestamps of retries in window

    def should_retry(
        self,
        txn_id       : str,
        attempt_count: int,
        last_status  : AttemptStatus,
        clock_ms     : int,
    ) -> tuple[bool, int]:
        # P2 — status must be retryable
        if last_status.value not in self.theta.retryable_statuses:
            return False, 0

        # P2 — attempt count must be within limit
        if attempt_count >= self.theta.max_retry:
            return False, 0

        # P5 — global retry budget check
        self._evict_expired(clock_ms)
        if len(self._retry_window) >= self.theta.max_retries_per_window:
            return False, 0

        self._retry_window.append(clock_ms)

        # P3 — exponential backoff
        backoff_ms = int(
            self.theta.base_backoff_ms * (self.theta.backoff_multiplier ** (attempt_count - 1))
        )
        return True, backoff_ms

    def _evict_expired(self, clock_ms: int) -> None:
        cutoff = clock_ms - self.theta.retry_budget_window_ms
        while self._retry_window and self._retry_window[0] < cutoff:
            self._retry_window.popleft()


# ---------------------------------------------------------------------------
# Policy Store (local JSON, replaceable)
# ---------------------------------------------------------------------------

class PolicyStore:

    def __init__(self, path: str = "policy.json"):
        self._path  = Path(path)
        self._theta = self._load()

    def _load(self) -> PolicyVector:
        if self._path.exists():
            raw = json.loads(self._path.read_text())
            return PolicyVector(**raw)
        # No file found — write and use defaults
        theta = PolicyVector()
        self.save(theta)
        return theta

    def save(self, theta: PolicyVector) -> None:
        self._path.write_text(json.dumps(theta.__dict__, indent=2))
        self._theta = theta

    @property
    def current(self) -> PolicyVector:
        return self._theta

    def update(self, theta: PolicyVector) -> None:
        """Called by adaptation scheduler to push new θ."""
        self.save(theta)


# ---------------------------------------------------------------------------
# PolicyEngine (coordinator)
# ---------------------------------------------------------------------------

class PolicyEngine:

    def __init__(self, store: PolicyStore, gateway_model):
        self._store        = store
        self._gateway_model = gateway_model

        # Validate θ providers match GatewayModel providers
        theta_providers   = set(store.current.provider_priority)
        gateway_providers = set(gateway_model._configs.keys())
        missing = theta_providers - gateway_providers
        if missing:
            raise ValueError(
                f"PolicyVector references providers not in GatewayModel: {missing}"
            )

        self._routing_hook = RoutingHook(store.current, gateway_model)
        self._retry_hook   = RetryHook(store.current)

    def choose_provider(self, txn_id: str) -> str:
        return self._routing_hook.choose_provider(txn_id)

    def should_retry(
        self,
        txn_id       : str,
        attempt_count: int,
        last_status  : AttemptStatus,
        clock_ms     : int,
    ) -> tuple[bool, int]:
        return self._retry_hook.should_retry(txn_id, attempt_count, last_status, clock_ms)

    def update_theta(self, theta: PolicyVector) -> None:
        """
        Adaptation scheduler calls this to push new θ.
        Rebuilds hooks with new policy vector immediately.
        """
        self._store.update(theta)
        self._routing_hook = RoutingHook(theta, self._gateway_model)
        self._retry_hook   = RetryHook(theta)