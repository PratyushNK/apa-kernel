"""Minimal demo performance test.

Runs N trials: for each trial start the simulator, start the engine after
1s, run for a fixed wall-time (62s by default), tail `data/streams/events.jsonl`,
and compute simplified versions of the 9 performance metrics. Results are
appended as JSON lines to `tests/performance_metrics.jsonl`.

This is intentionally compact and only touches this test file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import sys
import time
import logging
from collections import defaultdict
from pathlib import Path

from simulator.runner import simulation_runner
from kernel.engine.runner import engine_runner


# Configuration via env
RUNS = int(os.getenv("DEMO_PERF_RUNS", "1"))
RUN_SECONDS = int(os.getenv("DEMO_PERF_RUNTIME_S", "50"))
ENGINE_DELAY_S = float(os.getenv("DEMO_ENGINE_DELAY_S", "1.0"))


async def _tail_events(path, records, stop_event):
    offset = 0
    while not stop_event.is_set():
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as fh:
                    fh.seek(offset)
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append((time.time(), json.loads(line)))
                        except Exception:
                            continue
                    offset = fh.tell()
        except Exception:
            pass
        await asyncio.sleep(0.05)


def _compute_metrics(records, policy_mtime_after, policy_mtime_before):
    # minimal transaction-centric aggregation
    txns = {}
    attempts = 0
    latencies = []
    costs = 0.0
    circuit_open_time = None

    for wall, ev in records:
        et = ev.get("event_type")
        tid = ev.get("txn_id")
        if et == "NewTransaction":
            txns[tid] = {"created_wall": wall, "sla_deadline": ev.get("sla_deadline_ms"), "created_sim": ev.get("created_at")}
        elif et == "AttemptExecution":
            attempts += 1
            txns.setdefault(tid, {}).setdefault("attempts", 0)
            txns[tid]["attempts"] += 1
        elif et == "AttemptResult":
            lat = ev.get("processing_latency_ms")
            if lat is not None:
                latencies.append(float(lat))
            costs += float(ev.get("provider_cost", 0) or 0)
            if ev.get("status") == "SUCCESS":
                txns.setdefault(tid, {})["success"] = True
                txns[tid]["completed_sim"] = ev.get("completed_at")
        elif et == "CircuitEvaluation" and ev.get("circuit_state") == "OPEN":
            if circuit_open_time is None:
                circuit_open_time = wall

    total_txns = len(txns)
    sla_ok = 0
    for t in txns.values():
        if t.get("success") and t.get("completed_sim") is not None:
            if t["completed_sim"] <= (t.get("sla_deadline") or 0):
                sla_ok += 1

    sla_rate_total = (sla_ok / total_txns) if total_txns else None
    p50 = statistics.median(latencies) if latencies else None
    p95 = (sorted(latencies)[int(len(latencies)*0.95)-1]) if latencies else None
    p99 = (sorted(latencies)[int(len(latencies)*0.99)-1]) if latencies else None
    retry_amp = (attempts / total_txns) if total_txns else None
    avg_cost = (costs / attempts) if attempts else None

    e2e_recovery = None
    if circuit_open_time and policy_mtime_after and policy_mtime_after > policy_mtime_before:
        e2e_recovery = float(policy_mtime_after - circuit_open_time)

    # simplified adaptation metrics: count policy writes as deployments
    deployments = 1 if policy_mtime_after and policy_mtime_after > policy_mtime_before else 0
    flapping_rate = deployments / max(1.0, RUN_SECONDS)

    metrics = {
        "total_txns": total_txns,
        "total_attempts": attempts,
        "sla_rate": sla_rate_total,
        "p95_latency_ms": p95,
        "p50_latency_ms": p50,
        "p99_latency_ms": p99,
        "retry_amplification": retry_amp,
        "avg_cost_per_attempt": avg_cost,
        "circuit_open_count": (1 if circuit_open_time else 0),
        "e2e_recovery_time_s": e2e_recovery,
        "deployments": deployments,
        "adaptation_flapping_rate": flapping_rate,
    }
    return metrics


def _parse_engine_log(log_path: Path) -> dict:
    """Parse engine+simulator run log to extract adaptation-cycle events.

    Returns a dict with per-run component metrics needed by the user:
    - llm_proposals, llm_acceptance_rate, proposal_effectiveness (approval_rate),
    - avg_improvement_p95_ms, tlc_{tp,fp,tn,fn}, tlc_tpr, tlc_fpr,
    - recovery_confirmation_precision/recall
    """
    text = ""
    try:
        text = log_path.read_text(encoding="utf-8")
    except Exception:
        return {}

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    cycles = []
    current = None

    for line in lines:
        if "[adaptation] fetched metrics" in line:
            m = re.search(r"approval_rate=([0-9.]+)\s+any_breach=(True|False)", line)
            pre_approval = float(m.group(1)) if m else None
            any_breach = (m.group(2) == "True") if m else None
            current = {
                "pre_approval": pre_approval,
                "pre_any_breach": any_breach,
                "proposed": False,
                "verification_pass": None,
                "verification_tlc": False,
                "deployed": False,
                "post_approval": None,
                "post_p95": None,
                "recovery_confirmed": False,
            }
            cycles.append(current)
            continue

        if current is None:
            # try to capture disturbances even if no cycle started
            continue

        if "[adaptation] policy vector received" in line:
            current["proposed"] = True
            continue

        if "[adaptation] verification passed" in line:
            current["verification_pass"] = True
            continue

        if "verification failed (TLC)" in line or "verification failed (TLC)" in line:
            current["verification_pass"] = False
            current["verification_tlc"] = True
            continue

        if "[adaptation] policy deployed" in line:
            current["deployed"] = True
            continue

        if "[adaptation] observe_snapshot" in line:
            m = re.search(r"approval=([0-9.]+)\s+sla_breach_rate=([0-9.]+)\s+timeout_rate=([0-9.]+)\s+p95_latency_ms=([0-9.]+)", line)
            if m:
                current["post_approval"] = float(m.group(1))
                try:
                    current["post_p95"] = float(m.group(4))
                except Exception:
                    current["post_p95"] = None
            continue

        if "[adaptation] recovery confirmed" in line:
            current["recovery_confirmed"] = True
            continue

    # derive metrics from cycles
    proposals = sum(1 for c in cycles if c.get("proposed"))
    accepted = sum(1 for c in cycles if c.get("deployed"))

    improvements = []
    improvements_p95 = []
    tlc_tp = tlc_fp = tlc_fn = tlc_tn = 0
    recovery_confirmed = 0
    true_recoveries = 0
    recovery_confirmed_and_true = 0

    for c in cycles:
        pre = c.get("pre_approval")
        post = c.get("post_approval")
        pre_p95 = None
        post_p95 = c.get("post_p95")

        if c.get("verification_tlc"):
            if c.get("pre_any_breach"):
                tlc_tp += 1
            else:
                tlc_fp += 1
        else:
            if c.get("pre_any_breach"):
                tlc_fn += 1
            else:
                tlc_tn += 1

        if c.get("deployed") and pre is not None and post is not None:
            delta = post - pre
            improvements.append(delta)
            if pre_p95 is not None and post_p95 is not None:
                improvements_p95.append(pre_p95 - post_p95)
            # true recovery predicate: positive improvement in approval_rate
            true = delta > 0
            if true:
                true_recoveries += 1
        if c.get("recovery_confirmed"):
            recovery_confirmed += 1
            # check whether this confirmed event corresponds to a recorded improvement
            if pre is not None and post is not None and (post - pre) > 0:
                recovery_confirmed_and_true += 1

    acceptance_rate = (accepted / proposals) if proposals else None
    proposal_effectiveness = float(statistics.mean(improvements)) if improvements else None
    avg_improvement_p95 = float(statistics.mean(improvements_p95)) if improvements_p95 else None

    tlc_tpr = None
    tlc_fpr = None
    try:
        tlc_tpr = tlc_tp / (tlc_tp + tlc_fn) if (tlc_tp + tlc_fn) else None
        tlc_fpr = tlc_fp / (tlc_fp + tlc_tn) if (tlc_fp + tlc_tn) else None
    except Exception:
        tlc_tpr = tlc_fpr = None

    recovery_precision = (recovery_confirmed_and_true / recovery_confirmed) if recovery_confirmed else None
    recovery_recall = (recovery_confirmed_and_true / true_recoveries) if true_recoveries else None

    return {
        "llm_proposals": proposals,
        "llm_accepted": accepted,
        "llm_acceptance_rate": acceptance_rate,
        "proposal_effectiveness_approval_delta": proposal_effectiveness,
        "improvements_count": len(improvements),
        "improvements_sum": sum(improvements) if improvements else 0.0,
        "avg_improvement_p95_ms": avg_improvement_p95,
        "improvements_p95_count": len(improvements_p95),
        "improvements_p95_sum": sum(improvements_p95) if improvements_p95 else 0.0,
        "tlc_tp": tlc_tp,
        "tlc_fp": tlc_fp,
        "tlc_fn": tlc_fn,
        "tlc_tn": tlc_tn,
        "tlc_tpr": tlc_tpr,
        "tlc_fpr": tlc_fpr,
        "recovery_confirmed_count": recovery_confirmed,
        "true_recoveries_count": true_recoveries,
        "recovery_confirmed_and_true": recovery_confirmed_and_true,
        "recovery_confirmation_precision": recovery_precision,
        "recovery_confirmation_recall": recovery_recall,
    }


def _parse_adaptations_file(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return {}

    by_pid = {}
    for rec in lines:
        pid = rec.get("proposal_id") or "__none__"
        by_pid.setdefault(pid, {})[rec.get("stage")] = rec

    proposals = 0
    accepted = 0
    improvements = []
    tlc_tp = tlc_fp = tlc_fn = tlc_tn = 0
    recovery_confirmed = 0
    true_recoveries = 0
    recovery_confirmed_and_true = 0

    for pid, stages in by_pid.items():
        if pid == "__none__":
            continue
        if "proposed" in stages:
            proposals += 1
            pre = stages["proposed"].get("pre_approval")
            pre_breach = bool(stages["proposed"].get("pre_invariant_breaches"))
        else:
            pre = None
            pre_breach = False

        verified = stages.get("verified")
        if verified:
            is_tlc = bool(verified.get("verification_tlc"))
            # compare to ground truth breach
            if is_tlc and pre_breach:
                tlc_tp += 1
            elif is_tlc and not pre_breach:
                tlc_fp += 1
            elif not is_tlc and pre_breach:
                tlc_fn += 1
            else:
                tlc_tn += 1

        if "deployed" in stages:
            accepted += 1

        if "observed" in stages and pre is not None:
            post = stages["observed"].get("post_snapshot", {}).get("approval_rate")
            if post is not None:
                improvements.append(post - pre)
                # recovery confirmed logic
                if stages["observed"].get("recovery_confirmed"):
                    recovery_confirmed += 1
                    if (post - pre) > 0:
                        recovery_confirmed_and_true += 1
                if (post - pre) > 0:
                    true_recoveries += 1

    acceptance_rate = (accepted / proposals) if proposals else None
    proposal_effectiveness = float(statistics.mean(improvements)) if improvements else None

    tlc_tpr = (tlc_tp / (tlc_tp + tlc_fn)) if (tlc_tp + tlc_fn) else None
    tlc_fpr = (tlc_fp / (tlc_fp + tlc_tn)) if (tlc_fp + tlc_tn) else None

    recovery_precision = (recovery_confirmed_and_true / recovery_confirmed) if recovery_confirmed else None
    recovery_recall = (recovery_confirmed_and_true / true_recoveries) if true_recoveries else None

    return {
        "llm_proposals": proposals,
        "llm_accepted": accepted,
        "llm_acceptance_rate": acceptance_rate,
        "proposal_effectiveness_approval_delta": proposal_effectiveness,
        "improvements_count": len(improvements),
        "improvements_sum": sum(improvements) if improvements else 0.0,
        "tlc_tp": tlc_tp,
        "tlc_fp": tlc_fp,
        "tlc_fn": tlc_fn,
        "tlc_tn": tlc_tn,
        "tlc_tpr": tlc_tpr,
        "tlc_fpr": tlc_fpr,
        "recovery_confirmed_count": recovery_confirmed,
        "true_recoveries_count": true_recoveries,
        "recovery_confirmed_and_true": recovery_confirmed_and_true,
        "recovery_confirmation_precision": recovery_precision,
        "recovery_confirmation_recall": recovery_recall,
    }


def _append_jsonl(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


async def _run_once(runtime_s, engine_delay_s, root: Path):
    events_path = root / "data" / "streams" / "events.jsonl"
    policy_path = root / "data" / "policies" / "policy.json"
    log_path = root / "data" / "streams" / "last_run.log"

    # clean events
    try:
        if events_path.exists():
            events_path.unlink()
    except Exception:
        pass

    policy_before = policy_path.stat().st_mtime if policy_path.exists() else 0

    records = []
    stop = asyncio.Event()
    tail = asyncio.create_task(_tail_events(events_path, records, stop))

    # Capture engine+simulator logs for per-run component metrics
    root_logger = logging.getLogger()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # truncate existing
        log_path.write_text("", encoding="utf-8")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root_logger.addHandler(fh)
    except Exception:
        fh = None

    sim = asyncio.create_task(simulation_runner())
    await asyncio.sleep(engine_delay_s)
    eng = asyncio.create_task(engine_runner())

    await asyncio.sleep(runtime_s)

    for t in (eng, sim):
        if t and not t.done():
            t.cancel()
    await asyncio.gather(sim, eng, return_exceptions=True)

    stop.set()
    await tail

    # remove file handler and flush
    try:
        if fh is not None:
            root_logger.removeHandler(fh)
            fh.flush()
            fh.close()
    except Exception:
        pass

    policy_after = policy_path.stat().st_mtime if policy_path.exists() else 0
    return records, policy_after, policy_before


def test_demo_performance_driver():
    root = Path(__file__).resolve().parents[1]
    out = Path(__file__).resolve().parent / "performance_metrics.jsonl"
    # preserve originals so we can restore after each run
    policy_path = root / "data" / "policies" / "policy.json"
    gateway_path = root / "data" / "gateway_commands.json"
    streams_path = root / "data" / "streams"

    if policy_path.exists():
        default_policy_text = policy_path.read_text(encoding="utf-8")
    else:
        # explicit default policy as requested
        default_policy_text = json.dumps({
            "provider_priority": ["G1", "G2"],
            "provider_weights": {"G1": 0.5, "G2": 0.5},
            "weight_learning_rate": 0.1,
            "max_retry": 3,
            "retryable_statuses": ["SOFT_DECLINE", "TIMEOUT"],
            "base_backoff_ms": 100,
            "backoff_multiplier": 2.0,
            "retry_budget_window_ms": 60000,
            "max_retries_per_window": 200
        }, indent=2)

    if gateway_path.exists():
        default_gateway_text = gateway_path.read_text(encoding="utf-8")
    else:
        # explicit default gateway file as requested (fixed updated_at)
        default_gateway_text = json.dumps({
            "regimes": {"G1": "HEALTHY", "G2": "HEALTHY"},
            "commands": [],
            "updated_at": 1775114772150
        }, indent=2)

    def _reset_environment():
        # restore policy
        try:
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(default_policy_text, encoding="utf-8")
        except Exception:
            pass
        # restore gateway commands
        try:
            gateway_path.parent.mkdir(parents=True, exist_ok=True)
            gateway_path.write_text(default_gateway_text, encoding="utf-8")
        except Exception:
            pass
        # clear event stream so aggregator restarts fresh next run
        try:
            streams_path.mkdir(parents=True, exist_ok=True)
            ev = streams_path / "events.jsonl"
            ev.write_text("", encoding="utf-8")
        except Exception:
            pass

    run_results = []
    for i in range(RUNS):
        print(f"[demo_perf] run {i+1}/{RUNS} (wall {RUN_SECONDS}s)")
        try:
            records, pm_after, pm_before = asyncio.run(_run_once(RUN_SECONDS, ENGINE_DELAY_S, root))
        except Exception as e:
            # ensure environment is reset even on error
            _reset_environment()
            raise
        metrics = _compute_metrics(records, pm_after, pm_before)
        # parse structured adaptations JSONL if present (preferred), else fallback to plain log
        adapt_path = root / "data" / "streams" / "adaptations.jsonl"
        log_path = root / "data" / "streams" / "last_run.log"
        if adapt_path.exists():
            comp = _parse_adaptations_file(adapt_path)
        elif log_path.exists():
            comp = _parse_engine_log(log_path)
        else:
            comp = {}
        metrics.update(comp)
        metrics["run_index"] = i + 1
        metrics["timestamp"] = time.time()
        run_results.append(metrics)
        print(f"[demo_perf] recorded run {i+1}")

        # reset policy/gateway/event stream to defaults before next run
        _reset_environment()
        # small pause for filesystem timestamps to stabilise
        time.sleep(0.1)

    # Aggregate across runs and append a single cumulative JSON line
    def _safe_mean(xs):
        vals = [x for x in xs if x is not None]
        return float(statistics.mean(vals)) if vals else None

    aggregated = {
        "total_txns_mean": _safe_mean([r.get("total_txns") for r in run_results]),
        "total_attempts_mean": _safe_mean([r.get("total_attempts") for r in run_results]),
        "sla_rate_mean": _safe_mean([r.get("sla_rate") for r in run_results]),
        "p95_latency_ms_mean": _safe_mean([r.get("p95_latency_ms") for r in run_results]),
        "p50_latency_ms_mean": _safe_mean([r.get("p50_latency_ms") for r in run_results]),
        "p99_latency_ms_mean": _safe_mean([r.get("p99_latency_ms") for r in run_results]),
        "retry_amplification_mean": _safe_mean([r.get("retry_amplification") for r in run_results]),
        "avg_cost_per_attempt_mean": _safe_mean([r.get("avg_cost_per_attempt") for r in run_results]),
        "circuit_open_count_sum": sum(r.get("circuit_open_count", 0) for r in run_results),
        "e2e_recovery_time_s_mean": _safe_mean([r.get("e2e_recovery_time_s") for r in run_results]),
        "deployments_sum": sum(r.get("deployments", 0) for r in run_results),
        "adaptation_flapping_rate_mean": _safe_mean([r.get("adaptation_flapping_rate") for r in run_results]),
        # Component-level aggregated metrics (LLM, TLC, adaptation effectiveness)
        "llm_proposals_sum": sum(r.get("llm_proposals", 0) for r in run_results),
        "llm_accepted_sum": sum(r.get("llm_accepted", 0) for r in run_results),
        "llm_acceptance_rate_overall": None,
        "proposal_effectiveness_mean": None,
        "improvements_count_sum": sum(r.get("improvements_count", 0) for r in run_results),
        "improvements_sum": sum(r.get("improvements_sum", 0.0) for r in run_results),
        "avg_improvement_p95_ms_mean": None,
        "improvements_p95_count_sum": sum(r.get("improvements_p95_count", 0) for r in run_results),
        "improvements_p95_sum": sum(r.get("improvements_p95_sum", 0.0) for r in run_results),
        "tlc_tp_sum": sum(r.get("tlc_tp", 0) for r in run_results),
        "tlc_fp_sum": sum(r.get("tlc_fp", 0) for r in run_results),
        "tlc_fn_sum": sum(r.get("tlc_fn", 0) for r in run_results),
        "tlc_tn_sum": sum(r.get("tlc_tn", 0) for r in run_results),
        "tlc_tpr_overall": None,
        "tlc_fpr_overall": None,
        "recovery_confirmed_sum": sum(r.get("recovery_confirmed_count", 0) for r in run_results),
        "true_recoveries_sum": sum(r.get("true_recoveries_count", 0) for r in run_results),
        "recovery_confirmed_and_true_sum": sum(r.get("recovery_confirmed_and_true", 0) for r in run_results),
        "recovery_confirmation_precision_overall": None,
        "recovery_confirmation_recall_overall": None,
        "runs_aggregated": len(run_results),
        "timestamp": time.time(),
    }
    # Derive overall rates from summed counts
    try:
        tp = aggregated.get("tlc_tp_sum", 0)
        fn = aggregated.get("tlc_fn_sum", 0)
        fp = aggregated.get("tlc_fp_sum", 0)
        tn = aggregated.get("tlc_tn_sum", 0)
        aggregated["tlc_tpr_overall"] = (tp / (tp + fn)) if (tp + fn) else None
        aggregated["tlc_fpr_overall"] = (fp / (fp + tn)) if (fp + tn) else None

        total_props = aggregated.get("llm_proposals_sum", 0)
        total_accepted = aggregated.get("llm_accepted_sum", 0)
        aggregated["llm_acceptance_rate_overall"] = (total_accepted / total_props) if total_props else None

        imp_cnt = aggregated.get("improvements_count_sum", 0)
        imp_sum = aggregated.get("improvements_sum", 0.0)
        aggregated["proposal_effectiveness_mean"] = (imp_sum / imp_cnt) if imp_cnt else None

        imp95_cnt = aggregated.get("improvements_p95_count_sum", 0)
        imp95_sum = aggregated.get("improvements_p95_sum", 0.0)
        aggregated["avg_improvement_p95_ms_mean"] = (imp95_sum / imp95_cnt) if imp95_cnt else None

        rec_conf = aggregated.get("recovery_confirmed_sum", 0)
        rec_true = aggregated.get("true_recoveries_sum", 0)
        rec_tp = aggregated.get("recovery_confirmed_and_true_sum", 0)
        aggregated["recovery_confirmation_precision_overall"] = (rec_tp / rec_conf) if rec_conf else None
        aggregated["recovery_confirmation_recall_overall"] = (rec_tp / rec_true) if rec_true else None
    except Exception:
        pass

    _append_jsonl(out, aggregated)
    print(f"[demo_perf] appended aggregated metrics (runs={len(run_results)})")


if __name__ == "__main__":
    test_demo_performance_driver()
