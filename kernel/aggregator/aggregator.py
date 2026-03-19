"""
kernel/aggregator/aggregator.py

Aggregator — two responsibilities:

1. run_heartbeat() — async loop alongside simulator.
   Continuously computes snapshots and tracks last healthy baseline.

2. get_snapshot() — on-demand service for Adaptation Scheduler.
   Returns (current_snapshot, delta_from_last_healthy).
"""

from __future__ import annotations

import asyncio
from typing import Optional
import logging

from kernel.aggregator.snapshot import (
    MetricsSnapshot,
    SnapshotDelta,
)
from kernel.aggregator.window import WindowReader


class HealthThresholds:
    min_approval_rate        : float = 0.85
    max_p95_latency_ms       : float = 500.0
    max_timeout_rate         : float = 0.05
    max_sla_breach_rate      : float = 0.10
    max_retry_amplification  : float = 2.0
    max_circuit_open_rate    : float = 0.20


class Aggregator:

    def __init__(
        self,
        log_path            : str,
        window_size_ms      : int   = 5_000,
        heartbeat_interval_s: float = 5.0,
        max_retry           : int   = 3,
        thresholds          : HealthThresholds | None = None,
    ):
        self._reader              = WindowReader(log_path)
        self._window_size_ms      = window_size_ms
        self._heartbeat_interval  = heartbeat_interval_s
        self._max_retry           = max_retry
        self._thresholds          = thresholds or HealthThresholds()

        self._current             : Optional[MetricsSnapshot] = None
        self._last_healthy        : Optional[MetricsSnapshot] = None
        self._clock_ms            : int = 0
        self._gateway_regimes     : dict[str, str] = {}
        self._running             : bool = False

        self._breach_detected: bool = False

    def pop_breach(self) -> bool:
        """Returns True if breach was detected since last call. Clears the flag."""
        result = self._breach_detected
        self._breach_detected = False
        return result

    # ------------------------------------------------------------------
    # 1. Async heartbeat loop — runs alongside simulator
    # ------------------------------------------------------------------

    async def run_heartbeat(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            self._tick()

    def stop(self) -> None:
        self._running = False

    def _tick(self) -> None:
        snapshot = self._reader.compute(self._window_size_ms, self._max_retry)
        
        logging.info(
            f"[aggregator] tick — "
            f"txn_count={snapshot.window_txn_count} "
            f"approval={snapshot.approval_rate:.3f} "
            f"any_breach={snapshot.invariant_risk.any_breach} "
            f"healthy_baseline={'set' if self._last_healthy else 'none'}"
        )
        
        if not snapshot.has_sufficient_data:
            return
        
        self._current = snapshot
        if snapshot.invariant_risk.any_breach:
            self._breach_detected = True    # sticky flag — stays True until engine clears it
        if self._is_healthy(snapshot):
            self._last_healthy = snapshot

        
        

    # ------------------------------------------------------------------
    # 2. On-demand service — called by Adaptation Scheduler
    # ------------------------------------------------------------------

    def get_snapshot(self) -> tuple[
        Optional[MetricsSnapshot],
        SnapshotDelta,
    ]:
        """
        Returns (current_snapshot, delta_from_last_healthy).
        Delta fields are None if no healthy baseline exists yet.
        """
        if self._current is None:
            return None, SnapshotDelta()

        delta = self._compute_delta(self._current, self._last_healthy)
        return self._current, delta

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def _is_healthy(self, s: MetricsSnapshot) -> bool:
        t = self._thresholds
        return (
            s.approval_rate             >= t.min_approval_rate
            and s.p95_latency_ms        <= t.max_p95_latency_ms
            and s.timeout_rate          <= t.max_timeout_rate
            and s.sla_breach_rate       <= t.max_sla_breach_rate
            and s.retry_amplification_factor <= t.max_retry_amplification
            and s.circuit_open_rate     <= t.max_circuit_open_rate
        )

    # ------------------------------------------------------------------
    # Delta computation
    # ------------------------------------------------------------------

    def _compute_delta(
        self,
        current : MetricsSnapshot,
        baseline: Optional[MetricsSnapshot],
    ) -> SnapshotDelta:
        if baseline is None:
            return SnapshotDelta(has_baseline=False)

        return SnapshotDelta(
            has_baseline                   = True,
            approval_rate_delta            = current.approval_rate
                                             - baseline.approval_rate,
            rolling_success_rate_delta     = current.rolling_success_rate
                                             - baseline.rolling_success_rate,
            p95_latency_delta_ms           = current.p95_latency_ms
                                             - baseline.p95_latency_ms,
            timeout_rate_delta             = current.timeout_rate
                                             - baseline.timeout_rate,
            sla_breach_rate_delta          = current.sla_breach_rate
                                             - baseline.sla_breach_rate,
            retry_amplification_delta      = current.retry_amplification_factor
                                             - baseline.retry_amplification_factor,
            circuit_open_rate_delta        = current.circuit_open_rate
                                             - baseline.circuit_open_rate,
            average_decision_latency_delta = current.average_decision_latency
                                             - baseline.average_decision_latency,
        )