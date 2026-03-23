"""
simulator/runner.py
Wires all components and runs the simulation.
"""

import argparse
import os
import asyncio

from arrival_process import ArrivalProcess, ArrivalConfig, BurstConfig
from transaction_engine import TransactionEngine
from policy_engine import PolicyEngine, PolicyStore
from gateway_model import GatewayModel, ProviderConfig, Regime
from event_stream import EventStream, JSONLBackend
from transaction_simulator import TransactionSimulator, SimulatorConfig
from kernel.aggregator.aggregator import Aggregator, HealthThresholds
import pathlib
import time

ROOT = pathlib.Path(__file__).parent.parent  # apa-kernel/
STREAMS  = ROOT / "data" / "streams"
POLICIES = ROOT / "data" / "policies"

aggregator_path = str(STREAMS / "events.jsonl")
backend_path    = str(STREAMS / "events.jsonl")
store_path      = str(POLICIES / "policy.json")

aggregator = Aggregator(
    log_path             = aggregator_path,
    window_size_ms       = 5_000,
    heartbeat_interval_s = 2.0,
    thresholds           = HealthThresholds(),
)

async def main(debug_eval_ms: int | None = None):
    events_path = STREAMS / "events.jsonl"
    if events_path.exists():
        events_path.unlink()

    # --- Gateway setup ---
    # Allow a short debug eval window via CLI flag or env var for rapid testing.
    env_ms = os.getenv("SIM_DEBUG_EVAL_MS")
    if debug_eval_ms is None and env_ms is not None:
        try:
            debug_eval_ms = int(env_ms)
        except Exception:
            debug_eval_ms = None

    default_eval = 5_000 if debug_eval_ms is None else int(debug_eval_ms)

    providers = [
        ProviderConfig(name="G1", eval_window_ms=default_eval),
        ProviderConfig(name="G2", eval_window_ms=default_eval),
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
        max_transactions  = 800,
        speed_multiplier  = 1,    # 10x faster than real time
        clock_start_ms    = 0,
        real_tick_delay_s = 0.05
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
        await asyncio.sleep(10)   # let healthy baseline establish
        gateway_model.force_regime("G1", Regime.OUTAGE)
        print("[disturbance] G1 forced to OUTAGE")

    async def run_simulation() -> None:
        start = time.time()
        await simulator.run()
        elapsed = time.time() - start
        print(f"[simulator] completed in {elapsed:.2f} seconds")
        aggregator.stop()

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
    parser = argparse.ArgumentParser(description="Run the transaction simulator")
    parser.add_argument("--debug-eval-ms", type=int, default=None,
                        help="Shorten provider eval window (ms) for debugging")
    args = parser.parse_args()
    # prefer CLI flag, fall back to env var `SIM_DEBUG_EVAL_MS`
    cli_value = args.debug_eval_ms
    env_value = os.getenv("SIM_DEBUG_EVAL_MS")
    try:
        env_value = int(env_value) if env_value is not None else None
    except Exception:
        env_value = None

    chosen = cli_value if cli_value is not None else env_value
    asyncio.run(main(debug_eval_ms=chosen))