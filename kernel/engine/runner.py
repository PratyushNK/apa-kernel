"""
kernel/engine/runner.py

Entry point for the APA Kernel.
Owns: Aggregator, AdaptationLoop, KernelEngine, LLM, PolicyStore.
Simulator runs independently and is not touched here.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
import time

sys.path.append(str(pathlib.Path(__file__).parent.parent.parent))

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)s %(message)s",
)

from kernel.aggregator.aggregator import Aggregator, HealthThresholds
from kernel.aggregator.snapshot import MetricsSnapshot, SnapshotDelta
from kernel.adaptation.loop import AdaptationLoop
from kernel.verification.verifier import InvariantVerifier
from simulator.policy_engine import PolicyStore
from services.llms.azure_openai import AzureOpenAILLM
from services.llms.mock import MockLLM

ROOT     = pathlib.Path(__file__).parent.parent.parent
STREAMS  = ROOT / "data" / "streams"
POLICIES = ROOT / "data" / "policies"

STREAMS.mkdir(parents=True, exist_ok=True)
POLICIES.mkdir(parents=True, exist_ok=True)


class KernelEngine:

    MODE_MONITORING = "monitoring"
    MODE_COOLDOWN   = "cooldown"

    def __init__(
        self,
        aggregator       : Aggregator,
        adaptation_loop  : AdaptationLoop,
        check_interval_s : float = 2.0,
        cooldown_s       : float = 30.0,
        min_approval_rate: float = HealthThresholds.min_approval_rate,
    ):
        self._aggregator         = aggregator
        self._adaptation_loop    = adaptation_loop
        self._check_interval_s   = check_interval_s
        self._adaptation_running : bool = False
        self._running            : bool = False
        self._cooldown_s         = cooldown_s
        self._min_approval_rate  = min_approval_rate
        self._mode               = self.MODE_MONITORING
        self._last_success_at_s  : float | None = None

    async def run(self) -> None:
        self._running = True
        logging.info("[engine] started")

        while self._running:
            await asyncio.sleep(self._check_interval_s)

            snapshot, delta = self._aggregator.get_snapshot()

            if snapshot is None:
                continue

            if self._adaptation_running:
                continue

            if self._should_trigger_cure(snapshot):
                logging.info(
                    f"[engine] cure trigger — "
                    f"approval_rate={snapshot.approval_rate:.3f}"
                )
                await self._run_adaptation("cure", snapshot, delta)

            # Future:
            # elif self._should_trigger_prevention(snapshot, delta): ...
            # elif self._should_trigger_evolution(snapshot): ...

    def stop(self) -> None:
        self._running = False
        logging.info("[engine] stopped")

    async def _run_adaptation(
        self,
        objective : str,
        snapshot  : MetricsSnapshot,
        delta     : SnapshotDelta | None,
    ) -> None:
        self._adaptation_running = True
        try:
            result = await self._adaptation_loop.run(objective=objective)
            logging.info(
                f"[engine] adaptation done — "
                f"status={result.status} cycles={result.cycle_count}"
            )

            if result.status == "success":
                self._mode = self.MODE_COOLDOWN
                self._last_success_at_s = time.monotonic()
                logging.info(
                    f"[engine] entering cooldown for {self._cooldown_s:.0f}s"
                )
            else:
                self._mode = self.MODE_MONITORING
                self._last_success_at_s = None
        except Exception as e:
            logging.error(f"[engine] adaptation error — {e}")
            self._mode = self.MODE_MONITORING
            self._last_success_at_s = None
        finally:
            self._adaptation_running = False

    def _should_trigger_cure(self, snapshot: MetricsSnapshot) -> bool:
        if not snapshot.has_sufficient_data:
            logging.info(
                f"[engine] trigger check — mode={self._mode} "
                f"sufficient=False result=False"
            )
            return False

        # During cooldown, ignore sticky breach and only trigger on real degradation.
        if self._mode == self.MODE_COOLDOWN:
            self._aggregator.pop_breach()

            elapsed = 0.0
            if self._last_success_at_s is not None:
                elapsed = time.monotonic() - self._last_success_at_s
            if elapsed >= self._cooldown_s:
                self._mode = self.MODE_MONITORING
                self._last_success_at_s = None
                logging.info("[engine] cooldown expired — resuming monitoring")
            else:
                degraded = snapshot.approval_rate < self._min_approval_rate
                logging.info(
                    f"[engine] trigger check — mode={self._mode} "
                    f"approval={snapshot.approval_rate:.3f} "
                    f"threshold={self._min_approval_rate:.3f} "
                    f"degraded={degraded} result={degraded}"
                )
                return degraded

        breach_detected = self._aggregator.pop_breach()
        result = breach_detected
        logging.info(
            f"[engine] trigger check — mode={self._mode} "
            f"sufficient=True "
            f"breach={breach_detected} "
            f"result={result}"
        )
        return result

    def _should_trigger_prevention(self, snapshot, delta) -> bool:
        return False

    def _should_trigger_evolution(self, snapshot) -> bool:
        return False


async def main() -> None:

    store = PolicyStore(str(POLICIES / "policy.json"))

    aggregator = Aggregator(
        log_path             = str(STREAMS / "events.jsonl"),
        window_size_ms       = 5_000,
        heartbeat_interval_s = 2.0,
        max_retry            = store.current.max_retry,
        thresholds           = HealthThresholds(),
    )

    #llm = AzureOpenAILLM("o4-mini")
    llm = MockLLM()

    adaptation_loop = AdaptationLoop(
        llm          = llm,
        aggregator   = aggregator,
        policy_store = store,
        verifier     = InvariantVerifier(),
    )

    engine = KernelEngine(
        aggregator       = aggregator,
        adaptation_loop  = adaptation_loop,
        check_interval_s = 2.0,
    )

    try:
        await asyncio.gather(
            aggregator.run_heartbeat(),
            engine.run(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        aggregator.stop()
        engine.stop()
        logging.info("[kernel] done")


if __name__ == "__main__":
    asyncio.run(main())