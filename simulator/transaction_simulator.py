"""
simulator/runner.py

TransactionSimulator — top-level coordinator for the APA Kernel transactions simulator.

Responsibilities:
    - Own the simulated clock
    - Drive the event loop (event-driven, asyncio-based)
    - Orchestrate the 6 sub-components
    - Terminate on txn count or manual signal
"""

import asyncio
import signal
from dataclasses import dataclass


@dataclass
class SimulatorConfig:
    max_transactions  : int   = 10_000   # auto-terminate after N txns
    speed_multiplier  : float = 1.0      # 1.0 = real-time, 10.0 = 10x faster
    clock_start_ms    : int   = 0        # simulated clock start (epoch ms)


class TransactionSimulator:

    def __init__(
        self,
        config          : SimulatorConfig,
        arrival_process,   # ArrivalProcess
        transaction_engine,# TransactionEngine
        policy_engine,     # PolicyEngine
        gateway_model,     # GatewayModel
        event_stream,      # EventStream
    ):
        self.config             = config
        self.arrival_process    = arrival_process
        self.transaction_engine = transaction_engine
        self.policy_engine      = policy_engine
        self.gateway_model      = gateway_model
        self.event_stream       = event_stream

        self._clock_ms          : int  = config.clock_start_ms
        self._txn_count         : int  = 0
        self._running           : bool = False

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    @property
    def clock_ms(self) -> int:
        return self._clock_ms

    def _advance_clock(self, delta_ms: int) -> None:
        self._clock_ms += int(delta_ms / self.config.speed_multiplier)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        self._register_signal_handlers()

        try:
            while self._running:
                await self._tick()

                if self._txn_count >= self.config.max_transactions:
                    print(f"[simulator] reached {self._txn_count} transactions. stopping.")
                    break

        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    # ------------------------------------------------------------------
    # Tick — one event-driven step
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        # 1. Advance clock by one inter-arrival step
        delta_ms = self.arrival_process.next_interarrival_ms(self._clock_ms)
        self._advance_clock(delta_ms)

        # 2. Generate arriving transactions at current clock
        txn = self.arrival_process.generate(self._clock_ms)

        # 3. Process txn through its full lifecycle
        events = await self.transaction_engine.process(
            txn            = txn,
            clock_ms       = self._clock_ms,
            policy_engine  = self.policy_engine,
            gateway_model  = self.gateway_model,
        )
        await self.event_stream.append(events)
        self._txn_count += 1

        # 4. Gateway model evaluates circuit state at each tick
        circuit_events = self.gateway_model.evaluate_circuits(self._clock_ms)
        if circuit_events:
            await self.event_stream.append(circuit_events)

        # 5. Yield control — allows manual cancel signal to be received
        await asyncio.sleep(0)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        self._running = False
        await self.event_stream.flush()
        print(f"[simulator] shutdown. total transactions: {self._txn_count}")

    # ------------------------------------------------------------------
    # Signal handling (Ctrl+C / UI stop signal)
    # ------------------------------------------------------------------

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_stop_signal)

    def _handle_stop_signal(self) -> None:
        print("[simulator] stop signal received.")
        self._running = False
        # Cancel all running tasks to immediately exit
        loop = asyncio.get_event_loop()
        for task in asyncio.all_tasks(loop):
            task.cancel()