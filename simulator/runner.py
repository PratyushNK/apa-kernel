"""
simulator/runner.py
Wires all components and runs the simulation.
"""

import asyncio

from arrival_process import ArrivalProcess, ArrivalConfig, BurstConfig
from transaction_engine import TransactionEngine
from policy_engine import PolicyEngine, PolicyStore
from gateway_model import GatewayModel, ProviderConfig
from event_stream import EventStream, JSONLBackend
from transaction_simulator import TransactionSimulator, SimulatorConfig

import os
if os.path.exists("events.jsonl"):
    os.remove("events.jsonl")

async def main():
    # --- Gateway setup ---
    providers = [
        ProviderConfig(name="G1"),
        ProviderConfig(name="G2"),
    ]
    gateway_model = GatewayModel(providers)

    # --- Policy setup ---
    store         = PolicyStore("policy.json")
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
    backend      = JSONLBackend("events.jsonl")
    event_stream = EventStream(backend, tail_size=100)

    # --- Transaction engine ---
    transaction_engine = TransactionEngine()

    # --- Simulator ---
    config = SimulatorConfig(
        max_transactions = 30,
        speed_multiplier = 50.0,    # 50x faster than real time
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

    await asyncio.gather(
        simulator.run(),
        #print_tail(),
    )


if __name__ == "__main__":
    asyncio.run(main())