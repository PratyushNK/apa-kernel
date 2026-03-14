"""
tests/aggregator_tests.py

Tests for kernel/aggregator.

Prerequisites:
    Run simulator scenarios first to generate JSONL logs:
        python tests/simulator_tests.py

Usage:
    Uncomment the test you want in main() and run:
        python tests/aggregator_tests.py
"""

import asyncio
import json
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).parent / ".."))

from kernel.aggregator.aggregator import Aggregator, HealthThresholds
from kernel.aggregator.snapshot import MetricsSnapshot, SnapshotDelta


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _log_path(scenario: str) -> pathlib.Path:
    return pathlib.Path(__file__).parent / "simulator_events" / f"{scenario}.jsonl"


def _get_clock_range(log_path: pathlib.Path) -> tuple[int, int]:
    timestamps = []
    with open(log_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            for key in ("created_at", "timestamp", "started_at", "completed_at"):
                if key in e:
                    timestamps.append(e[key])
                    break
    return min(timestamps), max(timestamps)


def _print_snapshot(snapshot: MetricsSnapshot | None, label: str = "Snapshot") -> None:
    if snapshot is None:
        print(f"\n[{label}] No snapshot available yet.")
        return
    
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  Window : {snapshot.window_start_ms}ms → {snapshot.window_end_ms}ms")
    print(f"  Txn count : {snapshot.window_txn_count}  |  "
          f"Sufficient data: {snapshot.has_sufficient_data}")
    print(f"{'='*60}")

    print("\n── Transaction Metrics ──")
    print(f"  approval_rate             : {snapshot.approval_rate:.3f}")
    print(f"  average_attempts_per_txn  : {snapshot.average_attempts_per_txn:.3f}")
    print(f"  retry_distribution        : {dict(sorted(snapshot.retry_distribution.items()))}")

    print("\n── Gateway Metrics ──")
    print(f"  rolling_success_rate      : {snapshot.rolling_success_rate:.3f}")
    print(f"  cost_per_successful_txn   : {snapshot.cost_per_successful_txn:.4f}")
    print(f"  p95_latency_ms            : {snapshot.p95_latency_ms:.1f}")
    print(f"  timeout_rate              : {snapshot.timeout_rate:.3f}")

    print("\n── System Metrics ──")
    print(f"  circuit_open_rate         : {snapshot.circuit_open_rate:.3f}")
    print(f"  sla_breach_rate           : {snapshot.sla_breach_rate:.3f}")
    print(f"  average_decision_latency  : {snapshot.average_decision_latency:.1f}")
    print(f"  retry_amplification_factor: {snapshot.retry_amplification_factor:.3f}")

    print("\n── Per-Provider ──")
    for pm in snapshot.per_provider:
        print(f"  [{pm.provider}]"
              f"  success={pm.rolling_success_rate:.3f}"
              f"  timeout={pm.timeout_rate:.3f}"
              f"  p95={pm.p95_latency_ms:.1f}ms"
              f"  attempts={pm.attempt_count}"
              f"  circuit_open={pm.circuit_open_rate:.3f}")

    print("\n── Gateway Regimes ──")
    for provider, regime in snapshot.gateway_regimes.items():
        print(f"  {provider}: {regime}")

    print("\n── Invariant Risk ──")
    risk = snapshot.invariant_risk
    print(f"  I2_retry_bound     : {risk.I2_retry_bound}")
    print(f"  I6_circuit_respect : {risk.I6_circuit_respect}")
    print(f"  I7_sla_breach      : {risk.I7_sla_breach}")
    print(f"  any_breach         : {risk.any_breach}")


def _print_delta(delta: SnapshotDelta) -> None:
    print(f"\n── Delta from Last Healthy Baseline ──")
    if not delta.has_baseline:
        print("  No healthy baseline captured yet.")
        return

    def fmt(val):
        if val is None:
            return "N/A"
        sign = "+" if val >= 0 else ""
        return f"{sign}{val:.3f}"

    print(f"  approval_rate_delta            : {fmt(delta.approval_rate_delta)}")
    print(f"  rolling_success_rate_delta     : {fmt(delta.rolling_success_rate_delta)}")
    print(f"  p95_latency_delta_ms           : {fmt(delta.p95_latency_delta_ms)}")
    print(f"  timeout_rate_delta             : {fmt(delta.timeout_rate_delta)}")
    print(f"  sla_breach_rate_delta          : {fmt(delta.sla_breach_rate_delta)}")
    print(f"  retry_amplification_delta      : {fmt(delta.retry_amplification_delta)}")
    print(f"  circuit_open_rate_delta        : {fmt(delta.circuit_open_rate_delta)}")
    print(f"  average_decision_latency_delta : {fmt(delta.average_decision_latency_delta)}")


def _build_aggregator(
    scenario      : str,
    window_ms     : int = 5_000,
    max_retry     : int = 3,
    thresholds    : HealthThresholds | None = None,
) -> tuple[Aggregator, int, int] | None:
    path = _log_path(scenario)
    if not path.exists():
        print(f"[skip] log not found: {path}")
        print(f"       Run scenario '{scenario}' in simulator_tests.py first.")
        return None
    min_clock, max_clock = _get_clock_range(path)
    aggregator = Aggregator(
        log_path             = str(path),
        window_size_ms       = window_ms,
        heartbeat_interval_s = 1.0,
        max_retry            = max_retry,
        thresholds           = thresholds or HealthThresholds(),
    )
    return aggregator, min_clock, max_clock


# ---------------------------------------------------------------------------
# Test 1 — Single snapshot (basic sanity check)
# ---------------------------------------------------------------------------

def test_single_snapshot(
    scenario  : str = "healthy_baseline",
    window_ms : int = 5_000,
) -> None:
    """
    Manually tick the aggregator once at end of log.
    Verifies all 11 metrics compute without error.
    """
    result = _build_aggregator(scenario, window_ms)
    if result is None:
        return
    aggregator, _, max_clock = result

    aggregator.update_context(max_clock, {"G1": "HEALTHY", "G2": "HEALTHY"})
    aggregator._tick()

    snapshot, delta = aggregator.get_snapshot()
    _print_snapshot(snapshot, label=f"Single Snapshot — {scenario}")
    _print_delta(delta)


# ---------------------------------------------------------------------------
# Test 2 — Healthy baseline capture
# ---------------------------------------------------------------------------

def test_healthy_baseline_capture(
    scenario  : str = "healthy_baseline",
    window_ms : int = 5_000,
) -> None:
    """
    Tick aggregator multiple times across the log.
    Verifies _last_healthy gets populated during healthy periods.
    """
    result = _build_aggregator(scenario, window_ms)
    if result is None:
        return
    aggregator, min_clock, max_clock = result

    step     = window_ms // 2
    clock    = min_clock + window_ms
    ticks    = 0
    captured = 0

    print(f"\n{'='*60}")
    print(f"  Healthy Baseline Capture — {scenario}")
    print(f"{'='*60}")

    while clock <= max_clock:
        aggregator.update_context(clock, {"G1": "HEALTHY", "G2": "HEALTHY"})
        aggregator._tick()
        ticks += 1

        if aggregator._last_healthy is not None:
            captured += 1

        snapshot, _ = aggregator.get_snapshot()
        if snapshot:
            print(f"  t={clock:>8}ms  "
                  f"approval={snapshot.approval_rate:.3f}  "
                  f"healthy_baseline={'✓' if aggregator._last_healthy else '✗'}")
        clock += step

    print(f"\n  Total ticks: {ticks}  |  Baseline captured: {captured > 0}")


# ---------------------------------------------------------------------------
# Test 3 — Delta from healthy baseline
# ---------------------------------------------------------------------------

def test_delta_from_healthy(
    healthy_scenario : str = "healthy_baseline",
    stress_scenario  : str = "everything_breaks",
    window_ms        : int = 5_000,
) -> None:
    """
    Capture healthy baseline from one scenario.
    Apply it as the baseline when reading a stress scenario.
    Prints the degradation delta.
    """
    # Step 1 — capture healthy baseline
    result = _build_aggregator(healthy_scenario, window_ms)
    if result is None:
        return
    aggregator, _, max_clock_healthy = result

    aggregator.update_context(max_clock_healthy, {"G1": "HEALTHY", "G2": "HEALTHY"})
    aggregator._tick()

    if aggregator._last_healthy is None:
        print("[fail] healthy baseline not captured — "
              "check thresholds or increase total_txns")
        return

    print(f"\n[ok] Healthy baseline captured from '{healthy_scenario}'")

    # Step 2 — read stress scenario into same aggregator
    stress_path = _log_path(stress_scenario)
    if not stress_path.exists():
        print(f"[skip] stress log not found: {stress_path}")
        return

    _, max_clock_stress = _get_clock_range(stress_path)

    # Swap reader to stress log without resetting baseline
    from kernel.aggregator.window import WindowReader
    aggregator._reader   = WindowReader(str(stress_path))
    aggregator._clock_ms = max_clock_stress
    aggregator._gateway_regimes = {"G1": "DEGRADED", "G2": "DEGRADED"}
    aggregator._tick()

    snapshot, delta = aggregator.get_snapshot()

    _print_snapshot(snapshot, label=f"Current — {stress_scenario}")
    _print_delta(delta)

    print("\n── Interpretation ──")
    if delta.has_baseline:
        if delta.approval_rate_delta is not None and delta.approval_rate_delta < -0.1:
            print(f"  ⚠  Approval rate dropped {delta.approval_rate_delta:.3f}"
                  f" from healthy baseline")
        if delta.p95_latency_delta_ms is not None and delta.p95_latency_delta_ms > 100:
            print(f"  ⚠  p95 latency increased by {delta.p95_latency_delta_ms:.1f}ms")
        if delta.retry_amplification_delta is not None \
                and delta.retry_amplification_delta > 0.5:
            print(f"  ⚠  Retry amplification increased by "
                  f"{delta.retry_amplification_delta:.3f}")
        if snapshot is not None and not snapshot.invariant_risk.any_breach:
            print("  ✓  No invariant breach detected")
        else:
            print("  ⚠  Invariant breach detected — adaptation should trigger")


# ---------------------------------------------------------------------------
# Test 4 — Simulated async heartbeat
# ---------------------------------------------------------------------------

async def test_async_heartbeat(
    scenario             : str   = "healthy_baseline",
    window_ms            : int   = 5_000,
    heartbeat_interval_s : float = 0.5,
    run_seconds          : float = 3.0,
) -> None:
    """
    Runs aggregator heartbeat as a real async loop for run_seconds.
    Simulates how it runs alongside the simulator in production.
    Prints snapshot state after heartbeat completes.
    """
    result = _build_aggregator(
        scenario,
        window_ms,
        thresholds=HealthThresholds()
    )
    if result is None:
        return
    aggregator, _, max_clock = result

    aggregator._heartbeat_interval = heartbeat_interval_s
    aggregator.update_context(max_clock, {"G1": "HEALTHY", "G2": "HEALTHY"})

    print(f"\n{'='*60}")
    print(f"  Async Heartbeat Test — {scenario}")
    print(f"  Running heartbeat for {run_seconds}s "
          f"(interval={heartbeat_interval_s}s)")
    print(f"{'='*60}")

    async def stop_after(seconds: float) -> None:
        await asyncio.sleep(seconds)
        aggregator.stop()

    await asyncio.gather(
        aggregator.run_heartbeat(),
        stop_after(run_seconds),
    )

    snapshot, delta = aggregator.get_snapshot()
    if snapshot:
        _print_snapshot(snapshot, label="Snapshot after heartbeat")
        _print_delta(delta)
        print(f"\n  Last healthy baseline: "
              f"{'captured ✓' if aggregator._last_healthy else 'not captured ✗'}")
    else:
        print("  No snapshot computed yet.")


# ---------------------------------------------------------------------------
# Test 5 — Side by side scenario comparison
# ---------------------------------------------------------------------------

def test_compare_scenarios(
    scenario_a : str = "healthy_baseline",
    scenario_b : str = "everything_breaks",
    window_ms  : int = 5_000,
) -> None:
    """
    Compute final snapshot for two scenarios.
    Print key metrics side by side for comparison.
    """
    snapshots = {}
    for scenario in [scenario_a, scenario_b]:
        result = _build_aggregator(scenario, window_ms)
        if result is None:
            continue
        aggregator, _, max_clock = result
        aggregator.update_context(max_clock, {"G1": "UNKNOWN", "G2": "UNKNOWN"})
        aggregator._tick()
        snapshot, _ = aggregator.get_snapshot()
        if snapshot:
            snapshots[scenario] = snapshot

    if len(snapshots) < 2:
        print("[skip] need both scenarios — run simulator tests first")
        return

    a = snapshots[scenario_a]
    b = snapshots[scenario_b]

    col_w = 20
    print(f"\n{'='*65}")
    print(f"  Scenario Comparison")
    print(f"{'='*65}")
    print(f"  {'Metric':<32} {scenario_a:<{col_w}} {scenario_b:<{col_w}}")
    print(f"  {'-'*65}")

    rows = [
        ("approval_rate",              a.approval_rate,              b.approval_rate),
        ("avg_attempts_per_txn",       a.average_attempts_per_txn,   b.average_attempts_per_txn),
        ("rolling_success_rate",       a.rolling_success_rate,       b.rolling_success_rate),
        ("p95_latency_ms",             a.p95_latency_ms,             b.p95_latency_ms),
        ("timeout_rate",               a.timeout_rate,               b.timeout_rate),
        ("sla_breach_rate",            a.sla_breach_rate,            b.sla_breach_rate),
        ("retry_amplification_factor", a.retry_amplification_factor, b.retry_amplification_factor),
        ("circuit_open_rate",          a.circuit_open_rate,          b.circuit_open_rate),
        ("avg_decision_latency_ms",    a.average_decision_latency,   b.average_decision_latency),
    ]
    for name, val_a, val_b in rows:
        print(f"  {name:<32} {val_a:<{col_w}.3f} {val_b:<{col_w}.3f}")

    print(f"\n  Invariant risk [{scenario_a}]: {a.invariant_risk.any_breach}")
    print(f"  Invariant risk [{scenario_b}]: {b.invariant_risk.any_breach}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # Test 1 — basic sanity, all 11 metrics compute correctly
    # test_single_snapshot(scenario="healthy_baseline", window_ms=5_000)

    # Test 2 — verify healthy baseline gets captured
    # test_healthy_baseline_capture(scenario="healthy_baseline", window_ms=5_000)

    # Test 3 — delta from healthy to degraded
    # test_delta_from_healthy(
    #     healthy_scenario = "healthy_baseline",
    #     stress_scenario  = "everything_breaks",
    #     window_ms        = 5_000,
    # )

    # Test 4 — real async heartbeat loop
    # asyncio.run(test_async_heartbeat(
    #     scenario             = "healthy_baseline",
    #     heartbeat_interval_s = 0.5,
    #     run_seconds          = 3.0,
    # ))

    # Test 5 — side by side comparison
    test_compare_scenarios(
        scenario_a = "healthy_baseline",
        scenario_b = "everything_breaks",
        window_ms  = 5_000,
    )