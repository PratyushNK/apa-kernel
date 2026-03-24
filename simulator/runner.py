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
import json

ROOT = pathlib.Path(__file__).parent.parent  # apa-kernel/
STREAMS  = ROOT / "data" / "streams"
POLICIES = ROOT / "data" / "policies"
GATEWAY_CMD_PATH = ROOT / "data" / "gateway_commands.json"

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
        max_transactions  = 1200,
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
        await asyncio.sleep(3)   # let healthy baseline establish
        gateway_model.force_regime("G1", Regime.OUTAGE)
        print("[disturbance] G1 forced to OUTAGE")

    async def gateway_watcher():
        """Watch data/gateway_commands.json for external commands and apply them.

        This polls periodically and applies any commands found by calling
        `gateway_model.force_regime(provider, Regime.X)`. After applying,
        clears the commands list and writes the file back.
        """
        while True:
            try:
                if GATEWAY_CMD_PATH.exists():
                    raw = GATEWAY_CMD_PATH.read_text(encoding="utf-8")
                    try:
                        payload = json.loads(raw) if raw.strip() else {}
                    except Exception:
                        payload = {}

                    cmds = payload.get("commands", []) or []
                    regimes = payload.get("regimes", {}) or {}

                    # Apply explicit commands first
                    applied = False
                    for c in cmds:
                        p = str(c.get("provider", "")).upper()
                        action = str(c.get("action", "")).upper()
                        if p in {"G1", "G2"} and action:
                            if action == "OUTAGE":
                                gateway_model.force_regime(p, Regime.OUTAGE)
                                print(f"[gateway_watcher] applied {p} -> OUTAGE")
                                applied = True
                            elif action == "HEALTHY":
                                gateway_model.force_regime(p, Regime.HEALTHY)
                                print(f"[gateway_watcher] applied {p} -> HEALTHY")
                                applied = True
                            elif action == "DEGRADED":
                                gateway_model.force_regime(p, Regime.DEGRADED)
                                print(f"[gateway_watcher] applied {p} -> DEGRADED")
                                applied = True

                    # Apply regime map as authoritative states
                    for p, r in regimes.items():
                        pp = str(p).upper()
                        if pp in {"G1", "G2"}:
                            try:
                                rg = Regime(r)
                            except Exception:
                                rg = None
                            if rg is not None:
                                gateway_model.force_regime(pp, rg)
                                # don't spam prints when nothing changed
                                applied = True

                    if applied:
                        # clear commands and update timestamp atomically
                        payload["commands"] = []
                        payload["updated_at"] = int(time.time() * 1000)
                        tmp = GATEWAY_CMD_PATH.with_suffix(".tmp")
                        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                        tmp.replace(GATEWAY_CMD_PATH)
            except Exception as e:
                print(f"[gateway_watcher] error: {e}")
            await asyncio.sleep(0.75)

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
            gateway_watcher(),
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