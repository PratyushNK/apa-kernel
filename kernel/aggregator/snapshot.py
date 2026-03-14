"""
kernel/aggregator/snapshot.py

Data structures for metrics snapshots and degradation deltas.
Pure data — no logic, no IO.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True, slots=True)
class InvariantRisk:
    I2_retry_bound      : bool = False
    I6_circuit_respect  : bool = False
    I7_sla_breach       : bool = False

    @property
    def any_breach(self) -> bool:
        return any([
            self.I2_retry_bound,
            self.I6_circuit_respect,
            self.I7_sla_breach,
        ])


@dataclass(frozen=True, slots=True)
class ProviderMetrics:
    provider                : str
    rolling_success_rate    : float = 0.0
    cost_per_successful_txn : float = 0.0
    p95_latency_ms          : float = 0.0
    timeout_rate            : float = 0.0
    attempt_count           : int   = 0
    circuit_open_rate       : float = 0.0


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    # Window metadata
    window_start_ms          : int
    window_end_ms            : int
    window_txn_count         : int

    # Transaction metrics
    approval_rate            : float = 0.0
    retry_distribution       : dict  = field(default_factory=dict)
    average_attempts_per_txn : float = 0.0

    # Gateway metrics
    rolling_success_rate     : float = 0.0
    cost_per_successful_txn  : float = 0.0
    p95_latency_ms           : float = 0.0
    timeout_rate             : float = 0.0

    # System metrics
    circuit_open_rate        : float = 0.0
    sla_breach_rate          : float = 0.0
    average_decision_latency : float = 0.0
    retry_amplification_factor: float = 0.0

    # Enrichment
    per_provider             : tuple = field(default_factory=tuple)
    gateway_regimes          : dict  = field(default_factory=dict)
    invariant_risk           : InvariantRisk = field(
                                   default_factory=InvariantRisk)

    @property
    def has_sufficient_data(self) -> bool:
        return self.window_txn_count >= 50


@dataclass(frozen=True, slots=True)
class SnapshotDelta:
    """
    Degradation from last healthy snapshot to current.
    Negative = degraded. Positive = improved.
    All None if no healthy baseline exists yet.
    """
    approval_rate_delta            : Optional[float] = None
    rolling_success_rate_delta     : Optional[float] = None
    p95_latency_delta_ms           : Optional[float] = None
    timeout_rate_delta             : Optional[float] = None
    sla_breach_rate_delta          : Optional[float] = None
    retry_amplification_delta      : Optional[float] = None
    circuit_open_rate_delta        : Optional[float] = None
    average_decision_latency_delta : Optional[float] = None
    has_baseline                   : bool             = False