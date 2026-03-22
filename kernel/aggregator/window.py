"""
kernel/aggregator/window.py

WindowReader — incremental JSONL reader and metric computation.
Reads only new lines since last call via byte offset.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kernel.aggregator.snapshot import (
    InvariantRisk,
    MetricsSnapshot,
    ProviderMetrics,
)


@dataclass
class TxnAccumulator:
    txn_id          : str
    created_at      : int
    sla_deadline_ms : int
    attempt_count   : int = 0
    success         : bool = False
    terminal        : bool = False


class WindowReader:

    def __init__(self, log_path: str):
        self._path   = Path(log_path)
        self._offset : int = 0

    def compute(self, window_size_ms: int, max_retry: int = 3) -> MetricsSnapshot:
        events       = self._read_new_events()
        latest_clock = max((self._timestamp(e) for e in events), default=0)
        window_start = latest_clock - window_size_ms
        regimes      = self._infer_regimes(events)
        return self._compute(events, window_start, latest_clock, regimes, max_retry)
    
    def _infer_regimes(self, events: list[dict]) -> dict[str, str]:
        regimes = {}
        for e in events:
            if e.get("event_type") == "CircuitEvaluation":
                regimes[e["provider"]] = e["circuit_state"]
        return regimes

    # ------------------------------------------------------------------
    # Incremental reader
    # ------------------------------------------------------------------

    def _read_new_events(self) -> list[dict]:
        if not self._path.exists():
            return []
        events = []
        with open(self._path, "r", encoding="utf-8") as f:
            f.seek(self._offset)
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            self._offset = f.tell()
        return events

    # ------------------------------------------------------------------
    # Metric computation
    # ------------------------------------------------------------------

    def _compute(
        self,
        events          : list[dict],
        window_start_ms : int,
        window_end_ms   : int,
        gateway_regimes : dict[str, str],
        max_retry       : int,
    ) -> MetricsSnapshot:

        txns         : dict[str, TxnAccumulator] = {}
        provider_data: dict[str, dict] = defaultdict(lambda: {
            "attempts": 0, "successes": 0, "timeouts": 0,
            "latencies": [], "costs": [],
            "circuit_opens": 0, "circuit_evals": 0,
        })
        decision_latencies : list[int] = []
        total_attempts     : int       = 0

        for e in events:
            etype = e.get("event_type")

            if etype == "NewTransaction":
                txns[e["txn_id"]] = TxnAccumulator(
                    txn_id          = e["txn_id"],
                    created_at      = e["created_at"],
                    sla_deadline_ms = e["sla_deadline_ms"],
                )

            elif etype == "RouteDecision":
                decision_latencies.append(e["decision_latency_ms"])

            elif etype == "AttemptExecution":
                if e["txn_id"] in txns:
                    txns[e["txn_id"]].attempt_count += 1
                total_attempts += 1
                provider_data[e["provider"]]["attempts"] += 1

            elif etype == "AttemptResult":
                p    = e["provider"]
                pdat = provider_data[p]
                pdat["latencies"].append(e["processing_latency_ms"])
                pdat["costs"].append(float(e["provider_cost"]))
                if e["status"] == "SUCCESS":
                    pdat["successes"] += 1
                    if e["txn_id"] in txns:
                        txns[e["txn_id"]].success  = True
                        txns[e["txn_id"]].terminal = True
                elif e["status"] == "TIMEOUT":
                    pdat["timeouts"] += 1
                elif e["status"] == "HARD_DECLINE":
                    if e["txn_id"] in txns:
                        txns[e["txn_id"]].terminal = True

            elif etype == "CircuitEvaluation":
                p = e["provider"]
                provider_data[p]["circuit_evals"] += 1
                if e["circuit_state"] == "OPEN":
                    provider_data[p]["circuit_opens"] += 1

        # ------------------------------------------------------------------
        # Derive metrics
        # ------------------------------------------------------------------

        txn_list      = list(txns.values())
        txn_count     = len(txn_list)
        success_count = sum(1 for t in txn_list if t.success)

        approval_rate = success_count / txn_count if txn_count > 0 else 0.0

        retry_dist: dict[int, int] = defaultdict(int)
        for t in txn_list:
            retry_dist[t.attempt_count] += 1

        avg_attempts = (
            sum(t.attempt_count for t in txn_list) / txn_count
            if txn_count > 0 else 0.0
        )

        all_latencies       = []
        all_costs           = []
        total_success       = 0
        total_timeout       = 0
        total_prov_attempts = 0
        total_circuit_opens = 0
        total_circuit_evals = 0

        for pdat in provider_data.values():
            all_latencies.extend(pdat["latencies"])
            all_costs.extend(pdat["costs"])
            total_success       += pdat["successes"]
            total_timeout       += pdat["timeouts"]
            total_prov_attempts += pdat["attempts"]
            total_circuit_opens += pdat["circuit_opens"]
            total_circuit_evals += pdat["circuit_evals"]

        rolling_success_rate = (
            total_success / total_prov_attempts
            if total_prov_attempts > 0 else 0.0
        )
        cost_per_success = (
            sum(all_costs) / total_success
            if total_success > 0 else 0.0
        )
        p95_latency = (
            sorted(all_latencies)[int(len(all_latencies) * 0.95)]
            if all_latencies else 0.0
        )
        timeout_rate = (
            total_timeout / total_prov_attempts
            if total_prov_attempts > 0 else 0.0
        )
        circuit_open_rate = (
            total_circuit_opens / total_circuit_evals
            if total_circuit_evals > 0 else 0.0
        )

        # SLA breach — completed_at > sla_deadline_ms
        completed_at_map = {
            e["txn_id"]: e["completed_at"]
            for e in events
            if e.get("event_type") == "AttemptResult"
            and e.get("status") == "SUCCESS"
        }
        sla_breaches = sum(
            1 for t in txn_list
            if t.terminal and
            completed_at_map.get(t.txn_id, 0) > t.sla_deadline_ms
        )
        sla_breach_rate = sla_breaches / txn_count if txn_count > 0 else 0.0

        avg_decision_latency = (
            sum(decision_latencies) / len(decision_latencies)
            if decision_latencies else 0.0
        )
        retry_amplification = (
            total_attempts / txn_count
            if txn_count > 0 else 0.0
        )

        per_provider = tuple(
            ProviderMetrics(
                provider                = p,
                rolling_success_rate    = pdat["successes"] / pdat["attempts"]
                                          if pdat["attempts"] > 0 else 0.0,
                cost_per_successful_txn = sum(pdat["costs"]) / pdat["successes"]
                                          if pdat["successes"] > 0 else 0.0,
                p95_latency_ms          = sorted(pdat["latencies"])[
                                              int(len(pdat["latencies"]) * 0.95)]
                                          if pdat["latencies"] else 0.0,
                timeout_rate            = pdat["timeouts"] / pdat["attempts"]
                                          if pdat["attempts"] > 0 else 0.0,
                attempt_count           = pdat["attempts"],
                circuit_open_rate       = pdat["circuit_opens"] / pdat["circuit_evals"]
                                          if pdat["circuit_evals"] > 0 else 0.0,
            )
            for p, pdat in provider_data.items()
        )

        invariant_risk = InvariantRisk(
            I2_retry_bound     = avg_attempts >= max_retry * 0.8,
            I6_circuit_respect = circuit_open_rate > 0.3,
            I7_sla_breach      = sla_breach_rate > 0.05,
        )

        return MetricsSnapshot(
            window_start_ms           = window_start_ms,
            window_end_ms             = window_end_ms,
            window_txn_count          = txn_count,
            approval_rate             = approval_rate,
            retry_distribution        = dict(retry_dist),
            average_attempts_per_txn  = avg_attempts,
            rolling_success_rate      = rolling_success_rate,
            cost_per_successful_txn   = cost_per_success,
            p95_latency_ms            = p95_latency,
            timeout_rate              = timeout_rate,
            circuit_open_rate         = circuit_open_rate,
            sla_breach_rate           = sla_breach_rate,
            average_decision_latency  = avg_decision_latency,
            retry_amplification_factor= retry_amplification,
            per_provider              = per_provider,
            gateway_regimes           = gateway_regimes,
            invariant_risk            = invariant_risk,
        )

    def _timestamp(self, event: dict) -> int:
        for key in ("created_at", "timestamp", "started_at", "completed_at"):
            if key in event:
                return event[key]
        return 0