"""
simulator/runner.py
Wires all components and runs the simulation.
"""

import asyncio

from arrival_process import ArrivalProcess, ArrivalConfig, BurstConfig
from transaction_engine import TransactionEngine
from policy_engine import PolicyEngine, PolicyStore
from gateway_model import GatewayModel, ProviderConfig, Regime
from event_stream import EventStream, JSONLBackend
from transaction_simulator import TransactionSimulator, SimulatorConfig
from kernel.aggregator.aggregator import Aggregator, HealthThresholds
import pathlib

ROOT = pathlib.Path(__file__).parent.parent  # apa-kernel/
STREAMS  = ROOT / "data" / "streams"
POLICIES = ROOT / "data" / "policies"

aggregator_path = str(STREAMS / "events.jsonl")
backend_path    = str(STREAMS / "events.jsonl")
store_path      = str(POLICIES / "policy.json")

aggregator = Aggregator(
    log_path             = aggregator_path,
    window_size_ms       = 60_000,
    heartbeat_interval_s = 5.0,
    thresholds           = HealthThresholds(),
)

async def main():
    events_path = STREAMS / "events.jsonl"
    if events_path.exists():
        events_path.unlink()

    # --- Gateway setup ---
    providers = [
        ProviderConfig(name="G1"),
        ProviderConfig(name="G2"),
    ]
    gateway_model = GatewayModel(providers)

    # --- Policy setup ---
    store         = PolicyStore(store_path)
    policy_engine = PolicyEngine(store, gateway_model)

    # --- Arrival process ---
    arrival_config = ArrivalConfig(
        lambda_base       = 10.0,
        diurnal_enabled   = True,
        diurnal_amplitude = 0.3,
        bursts            = [
            BurstConfig(start_ms=300_000, duration_ms=60_000, multiplier=3.0)
        ]
    )
    arrival_process = ArrivalProcess(arrival_config)

    # --- Event stream ---
    backend      = JSONLBackend(backend_path)
    event_stream = EventStream(backend, tail_size=100)

    # --- Transaction engine ---
    transaction_engine = TransactionEngine()

    # --- Simulator ---
    config = SimulatorConfig(
        max_transactions = 400,
        speed_multiplier = 10.0,    # 10x faster than real time
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

    # --- Live tail printer (runs alongside simulator) ---
    async def print_tail():
        seen = 0
        while True:
            tail = event_stream.get_tail(50)
            new  = tail[seen:]
            for record in new:
                print(record)
            seen = len(tail)
            await asyncio.sleep(0.1)

    async def inject_disturbance():
        await asyncio.sleep(0.005)   # let healthy baseline establish
        gateway_model.force_regime("G1", Regime.OUTAGE)
        print("[disturbance] G1 forced to OUTAGE")

    async def run_simulation():
        await simulator.run()
        aggregator.stop()          # stop heartbeat when simulator finishes

    try:
        await asyncio.gather(
            run_simulation(),
            aggregator.run_heartbeat(),
            inject_disturbance(),
            # print_tail()
        )
    except asyncio.CancelledError:
        pass
    finally:
        aggregator.stop()
        await event_stream.flush()
        print("[runner] done.")


if __name__ == "__main__":
    asyncio.run(main())