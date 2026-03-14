"""
tests/simulator_tests.py

Simulator operational mode tests for APA Kernel.

Usage:
    Configure which scenario to run in main() at the bottom.
    Each scenario is a standalone async function.
    All scenarios accept total_txns as a parameter.

Output:
    - events written to scenario-specific .jsonl file
    - live tail printed to terminal via print_tail()
"""

import asyncio
import os
import sys
import pathlib

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "simulator"))
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from simulator.arrival_process import ArrivalProcess, ArrivalConfig, BurstConfig
from simulator.transaction_engine import TransactionEngine
from simulator.policy_engine import PolicyEngine, PolicyStore, PolicyVector
from simulator.gateway_model import GatewayModel, ProviderConfig, Regime
from simulator.event_stream import EventStream, JSONLBackend
from simulator.transaction_simulator import TransactionSimulator, SimulatorConfig


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _reset_log(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


async def _print_tail(event_stream: EventStream, stop_event: asyncio.Event) -> None:
    """Prints new events to terminal as they are written."""
    seen = 0
    while not stop_event.is_set():
        tail = event_stream.get_tail(100)
        new  = tail[seen:]
        for record in new:
            print(record)
        seen = len(tail)
        await asyncio.sleep(0.1)


async def _run(
    scenario_name  : str,
    providers      : list[ProviderConfig],
    arrival_config : ArrivalConfig,
    policy_vector  : PolicyVector,
    total_txns     : int,
    speed_multiplier: float = 50.0,
    print_live     : bool   = True,
) -> None:
    log_path = str(pathlib.Path(__file__).parent / "simulator_events" / f"{scenario_name.lower().replace(' ', '_')}.jsonl")
    pathlib.Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    _reset_log(log_path)

    gateway_model      = GatewayModel(providers)
    store              = PolicyStore.__new__(PolicyStore)
    store._theta       = policy_vector
    policy_engine      = PolicyEngine.__new__(PolicyEngine)
    policy_engine._store         = store
    policy_engine._gateway_model = gateway_model

    # Validate providers
    theta_providers   = set(policy_vector.provider_priority)
    gateway_providers = set(gateway_model._configs.keys())
    missing = theta_providers - gateway_providers
    if missing:
        raise ValueError(f"PolicyVector references unknown providers: {missing}")

    from simulator.policy_engine import RoutingHook, RetryHook
    policy_engine._routing_hook = RoutingHook(policy_vector, gateway_model)
    policy_engine._retry_hook   = RetryHook(policy_vector)

    arrival_process    = ArrivalProcess(arrival_config)
    backend            = JSONLBackend(log_path)
    event_stream       = EventStream(backend, tail_size=100)
    transaction_engine = TransactionEngine()

    config = SimulatorConfig(
        max_transactions = total_txns,
        speed_multiplier = speed_multiplier,
        clock_start_ms   = 0,
    )
    simulator = TransactionSimulator(
        config             = config,
        arrival_process    = arrival_process,
        transaction_engine = transaction_engine,
        policy_engine      = policy_engine,
        gateway_model      = gateway_model,
        event_stream       = event_stream,
    )

    print(f"\n{'='*60}")
    print(f"  SCENARIO: {scenario_name}")
    print(f"  Total transactions: {total_txns}")
    print(f"  Log: {log_path}")
    print(f"{'='*60}\n")

    stop_event = asyncio.Event()

    if print_live:
        await asyncio.gather(
            _run_simulator(simulator, stop_event),
            _print_tail(event_stream, stop_event),
        )
    else:
        await _run_simulator(simulator, stop_event)

    print(f"\n[done] {scenario_name} — {event_stream.total_events} events written to {log_path}")


async def _run_simulator(simulator: TransactionSimulator, stop_event: asyncio.Event) -> None:
    await simulator.run()
    stop_event.set()


# ---------------------------------------------------------------------------
# Scenario 1 — Healthy Baseline
# ---------------------------------------------------------------------------

async def scenario_healthy_baseline(total_txns: int = 500, print_live: bool = True) -> None:
    """
    Both gateways stable, low failure rates, no bursts.
    Expected: high SUCCESS rate, minimal retries, no circuit openings.
    """
    providers = [
        ProviderConfig(name="G1"),  # defaults — healthy behavior
        ProviderConfig(name="G2"),
    ]
    arrival_config = ArrivalConfig(
        lambda_base       = 10.0,
        diurnal_enabled   = False,
        bursts            = [],
    )
    policy_vector = PolicyVector(
        provider_priority      = ["G1", "G2"],
        provider_weights       = {"G1": 0.5, "G2": 0.5},
        max_retry              = 2,
        base_backoff_ms        = 100,
        backoff_multiplier     = 2.0,
        max_retries_per_window = 500,
    )
    await _run("Healthy Baseline", providers, arrival_config, policy_vector, total_txns, print_live=print_live)


# ---------------------------------------------------------------------------
# Scenario 2 — Gateway Degradation
# ---------------------------------------------------------------------------

async def scenario_gateway_degradation(total_txns: int = 500, print_live: bool = True) -> None:
    """
    G1 degrades aggressively. G2 remains healthy.
    Expected: increasing SOFT_DECLINE/TIMEOUT on G1, traffic shifts to G2 via weights.
    """
    providers = [
        ProviderConfig(
            name        = "G1",
            transitions = {
                Regime.HEALTHY : {Regime.HEALTHY: 0.4, Regime.DEGRADED: 0.5, Regime.OUTAGE: 0.1},
                Regime.DEGRADED: {Regime.HEALTHY: 0.1, Regime.DEGRADED: 0.6, Regime.OUTAGE: 0.3},
                Regime.OUTAGE  : {Regime.HEALTHY: 0.0, Regime.DEGRADED: 0.2, Regime.OUTAGE: 0.8},
            },
            failure_rates = {
                Regime.HEALTHY : {"success": 0.75, "soft_decline": 0.20, "hard_decline": 0.05},
                Regime.DEGRADED: {"success": 0.40, "soft_decline": 0.45, "hard_decline": 0.15},
                Regime.OUTAGE  : {"success": 0.05, "soft_decline": 0.45, "hard_decline": 0.50},
            },
        ),
        ProviderConfig(name="G2"),  # G2 stays healthy
    ]
    arrival_config = ArrivalConfig(lambda_base=10.0, diurnal_enabled=False, bursts=[])
    policy_vector  = PolicyVector(
        provider_priority      = ["G1", "G2"],
        provider_weights       = {"G1": 0.7, "G2": 0.3},
        max_retry              = 3,
        base_backoff_ms        = 100,
        backoff_multiplier     = 2.0,
        max_retries_per_window = 500,
    )
    await _run("Gateway Degradation", providers, arrival_config, policy_vector, total_txns, print_live=print_live)


# ---------------------------------------------------------------------------
# Scenario 3 — Full Outage
# ---------------------------------------------------------------------------

async def scenario_full_outage(total_txns: int = 500, print_live: bool = True) -> None:
    """
    G1 immediately enters outage and stays there.
    G2 partially degrades.
    Expected: heavy HARD_DECLINE, circuit opens on G1, load shifts entirely to G2.
    """
    providers = [
        ProviderConfig(
            name        = "G1",
            transitions = {
                Regime.HEALTHY : {Regime.HEALTHY: 0.0, Regime.DEGRADED: 0.0, Regime.OUTAGE: 1.0},
                Regime.DEGRADED: {Regime.HEALTHY: 0.0, Regime.DEGRADED: 0.0, Regime.OUTAGE: 1.0},
                Regime.OUTAGE  : {Regime.HEALTHY: 0.0, Regime.DEGRADED: 0.0, Regime.OUTAGE: 1.0},
            },
            failure_rates = {
                Regime.HEALTHY : {"success": 0.01, "soft_decline": 0.09, "hard_decline": 0.90},
                Regime.DEGRADED: {"success": 0.01, "soft_decline": 0.09, "hard_decline": 0.90},
                Regime.OUTAGE  : {"success": 0.01, "soft_decline": 0.09, "hard_decline": 0.90},
            },
            failure_threshold = 0.3,
            eval_window_ms    = 5_000,
        ),
        ProviderConfig(
            name        = "G2",
            transitions = {
                Regime.HEALTHY : {Regime.HEALTHY: 0.6, Regime.DEGRADED: 0.3, Regime.OUTAGE: 0.1},
                Regime.DEGRADED: {Regime.HEALTHY: 0.2, Regime.DEGRADED: 0.5, Regime.OUTAGE: 0.3},
                Regime.OUTAGE  : {Regime.HEALTHY: 0.1, Regime.DEGRADED: 0.3, Regime.OUTAGE: 0.6},
            },
        ),
    ]
    arrival_config = ArrivalConfig(lambda_base=10.0, diurnal_enabled=False, bursts=[])
    policy_vector  = PolicyVector(
        provider_priority      = ["G1", "G2"],
        provider_weights       = {"G1": 0.5, "G2": 0.5},
        max_retry              = 3,
        base_backoff_ms        = 100,
        backoff_multiplier     = 2.0,
        max_retries_per_window = 500,
    )
    await _run("Full Outage", providers, arrival_config, policy_vector, total_txns, print_live=print_live)


# ---------------------------------------------------------------------------
# Scenario 4 — Circuit Breaker Trigger
# ---------------------------------------------------------------------------

async def scenario_circuit_breaker(total_txns: int = 500, print_live: bool = True) -> None:
    """
    G1 failure rate exceeds threshold quickly.
    Low eval_window_ms to trigger circuit evaluation frequently.
    Expected: CircuitEvaluation events with OPEN state, then HALF_OPEN recovery.
    """
    providers = [
        ProviderConfig(
            name        = "G1",
            transitions = {
                Regime.HEALTHY : {Regime.HEALTHY: 0.3, Regime.DEGRADED: 0.5, Regime.OUTAGE: 0.2},
                Regime.DEGRADED: {Regime.HEALTHY: 0.1, Regime.DEGRADED: 0.4, Regime.OUTAGE: 0.5},
                Regime.OUTAGE  : {Regime.HEALTHY: 0.1, Regime.DEGRADED: 0.2, Regime.OUTAGE: 0.7},
            },
            failure_rates = {
                Regime.HEALTHY : {"success": 0.55, "soft_decline": 0.35, "hard_decline": 0.10},
                Regime.DEGRADED: {"success": 0.25, "soft_decline": 0.50, "hard_decline": 0.25},
                Regime.OUTAGE  : {"success": 0.05, "soft_decline": 0.35, "hard_decline": 0.60},
            },
            failure_threshold  = 0.3,
            eval_window_ms     = 5_000,
            recovery_window_ms = 10_000,
        ),
        ProviderConfig(name="G2"),
    ]
    arrival_config = ArrivalConfig(lambda_base=10.0, diurnal_enabled=False, bursts=[])
    policy_vector  = PolicyVector(
        provider_priority      = ["G1", "G2"],
        provider_weights       = {"G1": 0.6, "G2": 0.4},
        max_retry              = 3,
        base_backoff_ms        = 100,
        backoff_multiplier     = 2.0,
        max_retries_per_window = 500,
    )
    await _run("Circuit Breaker Trigger", providers, arrival_config, policy_vector, total_txns, print_live=print_live)


# ---------------------------------------------------------------------------
# Scenario 5 — Retry Amplification
# ---------------------------------------------------------------------------

async def scenario_retry_amplification(total_txns: int = 500, print_live: bool = True) -> None:
    """
    High retry limit, short backoff, loose budget, degraded gateways.
    Expected: retry_amplification_factor spikes, system load increases,
    P5 budget eventually exhausted.
    """
    providers = [
        ProviderConfig(
            name          = "G1",
            failure_rates = {
                Regime.HEALTHY : {"success": 0.50, "soft_decline": 0.40, "hard_decline": 0.10},
                Regime.DEGRADED: {"success": 0.30, "soft_decline": 0.55, "hard_decline": 0.15},
                Regime.OUTAGE  : {"success": 0.10, "soft_decline": 0.60, "hard_decline": 0.30},
            },
        ),
        ProviderConfig(
            name          = "G2",
            failure_rates = {
                Regime.HEALTHY : {"success": 0.50, "soft_decline": 0.40, "hard_decline": 0.10},
                Regime.DEGRADED: {"success": 0.30, "soft_decline": 0.55, "hard_decline": 0.15},
                Regime.OUTAGE  : {"success": 0.10, "soft_decline": 0.60, "hard_decline": 0.30},
            },
        ),
    ]
    arrival_config = ArrivalConfig(lambda_base=15.0, diurnal_enabled=False, bursts=[])
    policy_vector  = PolicyVector(
        provider_priority      = ["G1", "G2"],
        provider_weights       = {"G1": 0.5, "G2": 0.5},
        max_retry              = 5,
        base_backoff_ms        = 50,
        backoff_multiplier     = 1.2,
        max_retries_per_window = 10_000,
    )
    await _run("Retry Amplification", providers, arrival_config, policy_vector, total_txns, print_live=print_live)


# ---------------------------------------------------------------------------
# Scenario 6 — SLA Breach
# ---------------------------------------------------------------------------

async def scenario_sla_breach(total_txns: int = 500, print_live: bool = True) -> None:
    """
    High latency gateways with tight timeout thresholds.
    Expected: high TIMEOUT rate, SLA deadlines exceeded frequently.
    """
    providers = [
        ProviderConfig(
            name         = "G1",
            latency_mu   = {Regime.HEALTHY: 6.5, Regime.DEGRADED: 7.2, Regime.OUTAGE: 8.0},
            latency_sigma= {Regime.HEALTHY: 1.0, Regime.DEGRADED: 1.4, Regime.OUTAGE: 1.8},
            timeout_ms   = {Regime.HEALTHY: 150, Regime.DEGRADED: 200, Regime.OUTAGE: 100},
        ),
        ProviderConfig(
            name         = "G2",
            latency_mu   = {Regime.HEALTHY: 6.2, Regime.DEGRADED: 7.0, Regime.OUTAGE: 7.8},
            latency_sigma= {Regime.HEALTHY: 0.9, Regime.DEGRADED: 1.3, Regime.OUTAGE: 1.7},
            timeout_ms   = {Regime.HEALTHY: 150, Regime.DEGRADED: 200, Regime.OUTAGE: 100},
        ),
    ]
    arrival_config = ArrivalConfig(lambda_base=10.0, diurnal_enabled=False, bursts=[])
    policy_vector  = PolicyVector(
        provider_priority      = ["G1", "G2"],
        provider_weights       = {"G1": 0.5, "G2": 0.5},
        max_retry              = 2,
        base_backoff_ms        = 100,
        backoff_multiplier     = 2.0,
        max_retries_per_window = 500,
    )
    await _run("SLA Breach", providers, arrival_config, policy_vector, total_txns, print_live=print_live)


# ---------------------------------------------------------------------------
# Scenario 7 — Burst Traffic
# ---------------------------------------------------------------------------

async def scenario_burst_traffic(total_txns: int = 1000, print_live: bool = True) -> None:
    """
    Multiple traffic bursts over the simulation window.
    Expected: arrival rate spikes, retry budget pressure, latency increases.
    """
    providers = [
        ProviderConfig(name="G1"),
        ProviderConfig(name="G2"),
    ]
    arrival_config = ArrivalConfig(
        lambda_base       = 10.0,
        diurnal_enabled   = True,
        diurnal_amplitude = 0.3,
        bursts            = [
            BurstConfig(start_ms=50_000,  duration_ms=20_000, multiplier=4.0),
            BurstConfig(start_ms=150_000, duration_ms=30_000, multiplier=6.0),
            BurstConfig(start_ms=300_000, duration_ms=15_000, multiplier=8.0),
        ],
    )
    policy_vector = PolicyVector(
        provider_priority      = ["G1", "G2"],
        provider_weights       = {"G1": 0.5, "G2": 0.5},
        max_retry              = 3,
        base_backoff_ms        = 100,
        backoff_multiplier     = 2.0,
        max_retries_per_window = 1000,
    )
    await _run("Burst Traffic", providers, arrival_config, policy_vector, total_txns, print_live=print_live)


# ---------------------------------------------------------------------------
# Scenario 8 — Everything Breaks
# ---------------------------------------------------------------------------

async def scenario_everything_breaks(total_txns: int = 1000, print_live: bool = True) -> None:
    """
    Both gateways degrade aggressively, multiple bursts, high retry amplification,
    tight circuit breaker thresholds.
    Expected: circuit openings, retry cascades, SLA breaches, budget exhaustion.
    """
    providers = [
        ProviderConfig(
            name        = "G1",
            transitions = {
                Regime.HEALTHY : {Regime.HEALTHY: 0.4, Regime.DEGRADED: 0.4, Regime.OUTAGE: 0.2},
                Regime.DEGRADED: {Regime.HEALTHY: 0.1, Regime.DEGRADED: 0.3, Regime.OUTAGE: 0.6},
                Regime.OUTAGE  : {Regime.HEALTHY: 0.0, Regime.DEGRADED: 0.1, Regime.OUTAGE: 0.9},
            },
            failure_rates = {
                Regime.HEALTHY : {"success": 0.60, "soft_decline": 0.30, "hard_decline": 0.10},
                Regime.DEGRADED: {"success": 0.25, "soft_decline": 0.50, "hard_decline": 0.25},
                Regime.OUTAGE  : {"success": 0.02, "soft_decline": 0.18, "hard_decline": 0.80},
            },
            failure_threshold  = 0.3,
            eval_window_ms     = 5_000,
            recovery_window_ms = 15_000,
        ),
        ProviderConfig(
            name        = "G2",
            transitions = {
                Regime.HEALTHY : {Regime.HEALTHY: 0.5, Regime.DEGRADED: 0.3, Regime.OUTAGE: 0.2},
                Regime.DEGRADED: {Regime.HEALTHY: 0.1, Regime.DEGRADED: 0.4, Regime.OUTAGE: 0.5},
                Regime.OUTAGE  : {Regime.HEALTHY: 0.0, Regime.DEGRADED: 0.2, Regime.OUTAGE: 0.8},
            },
            failure_rates = {
                Regime.HEALTHY : {"success": 0.55, "soft_decline": 0.35, "hard_decline": 0.10},
                Regime.DEGRADED: {"success": 0.20, "soft_decline": 0.55, "hard_decline": 0.25},
                Regime.OUTAGE  : {"success": 0.02, "soft_decline": 0.18, "hard_decline": 0.80},
            },
            failure_threshold  = 0.3,
            eval_window_ms     = 5_000,
            recovery_window_ms = 15_000,
        ),
    ]
    arrival_config = ArrivalConfig(
        lambda_base       = 20.0,
        diurnal_enabled   = True,
        diurnal_amplitude = 0.4,
        bursts            = [
            BurstConfig(start_ms=30_000,  duration_ms=20_000, multiplier=5.0),
            BurstConfig(start_ms=100_000, duration_ms=30_000, multiplier=8.0),
            BurstConfig(start_ms=250_000, duration_ms=20_000, multiplier=6.0),
        ],
    )
    policy_vector = PolicyVector(
        provider_priority      = ["G1", "G2"],
        provider_weights       = {"G1": 0.5, "G2": 0.5},
        max_retry              = 5,
        base_backoff_ms        = 50,
        backoff_multiplier     = 1.5,
        max_retries_per_window = 10_000,
    )
    await _run("Everything Breaks", providers, arrival_config, policy_vector, total_txns, print_live=print_live)


# ---------------------------------------------------------------------------
# Main — configure which scenario to run here
# ---------------------------------------------------------------------------

async def main():
    # Uncomment the scenario you want to run.
    # All accept: total_txns (int), print_live (bool)

    await scenario_healthy_baseline(total_txns=1000,  print_live=False)
    # await scenario_gateway_degradation(total_txns=10,  print_live=False)
    # await scenario_full_outage(total_txns=10,  print_live=False)
    # await scenario_circuit_breaker(total_txns=15,  print_live=False)
    # await scenario_retry_amplification(total_txns=10,  print_live=False)
    # await scenario_sla_breach(total_txns=10,  print_live=False)
    # await scenario_burst_traffic(total_txns=20, print_live=False)
    await scenario_everything_breaks(total_txns=1000, print_live=False)


if __name__ == "__main__":
    asyncio.run(main())