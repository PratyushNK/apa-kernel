"""
simulator/arrival_process.py

ArrivalProcess — generates transaction arrivals following:
    λ(t) = λ_0 * (1 + s(t)) + b(t)

Where:
    λ_0   = base arrival rate (transactions per second)
    s(t)  = diurnal seasonal modulation
    b(t)  = burst injection

Inter-arrival gaps are exponentially distributed — the standard
consequence of a Poisson process (queueing theory, M/M/1).
"""

import math
import random
import uuid
from dataclasses import dataclass, field


@dataclass
class BurstConfig:
    start_ms    : int   # simulated clock ms when burst begins
    duration_ms : int   # how long the burst lasts
    multiplier  : float # λ multiplied by this during burst (e.g. 5.0 = 5x spike)


@dataclass
class ArrivalConfig:
    lambda_base      : float            = 10.0  # txns per second at baseline
    diurnal_enabled  : bool             = True  # enable day/night seasonality
    diurnal_amplitude: float            = 0.4   # s(t) amplitude — 0.4 = ±40% modulation
    diurnal_period_ms: int              = 86_400_000  # 24 hours in ms
    bursts           : list[BurstConfig] = field(default_factory=list)


class ArrivalProcess:

    def __init__(self, config: ArrivalConfig):
        self.config = config

    # ------------------------------------------------------------------
    # Public API (called by TransactionSimulator._tick)
    # ------------------------------------------------------------------

    def next_interarrival_ms(self, clock_ms: int) -> int:
        """
        Draw the next inter-arrival gap from Exp(λ(t)).
        Returns milliseconds until next transaction arrival.
        """
        lam = self._lambda(clock_ms)                  # txns per second
        lam_per_ms = lam / 1000.0                     # convert to per-ms
        gap_ms = random.expovariate(lam_per_ms)       # Exp(λ) draw
        return max(1, int(gap_ms))                    # minimum 1ms gap

    def generate(self, clock_ms: int) -> dict:
        """
        Generate exactly one transaction at the current clock time.
        In a Poisson process, each inter-arrival gap produces one arrival.
        Returns a list for interface consistency with TransactionSimulator.
        """
        return self._make_transaction(clock_ms)

    # ------------------------------------------------------------------
    # λ(t) = λ_0 * (1 + s(t)) + b(t)
    # ------------------------------------------------------------------

    def _lambda(self, clock_ms: int) -> float:
        """
        λ(t) - transactions per second for a given time t.
        """
        lam = self.config.lambda_base
        lam *= (1.0 + self._seasonal(clock_ms))
        lam += self._burst(clock_ms)
        return max(lam, 0.01)               # never zero or negative

    def _seasonal(self, clock_ms: int) -> float:
        """
        s(t) — sinusoidal diurnal modulation.
        Peak at midday, trough at midnight.
        """
        if not self.config.diurnal_enabled:
            return 0.0
        phase = (2 * math.pi * clock_ms) / self.config.diurnal_period_ms
        return self.config.diurnal_amplitude * math.sin(phase)

    def _burst(self, clock_ms: int) -> float:
        """
        b(t) — burst injection.
        Adds λ_base * (multiplier - 1) during active burst windows.
        """
        for burst in self.config.bursts:
            if burst.start_ms <= clock_ms <= burst.start_ms + burst.duration_ms:
                return self.config.lambda_base * (burst.multiplier - 1.0)
        return 0.0

    # ------------------------------------------------------------------
    # Transaction factory
    # ------------------------------------------------------------------

    def _make_transaction(self, clock_ms: int) -> dict:
        """
        Produces a minimal transaction dict.
        TransactionEngine will build the full lifecycle from this.
        """
        return {
            "txn_id"    : f"txn_{uuid.uuid4().hex[:12]}",
            "created_at": clock_ms,
        }