"""
simulator/gateway_model.py

GatewayModel — models provider behavior using regime-switching Markov processes.

Each provider has:
    - A current regime: HEALTHY, DEGRADED, OUTAGE
    - A Markov transition matrix (configurable per provider)
    - A latency profile per regime (LogNormal, heavy-tailed)
    - A failure distribution per regime
    - A circuit breaker state: CLOSED, OPEN, HALF_OPEN

Emits:
    - CircuitEvaluation events when circuit state changes

P6 — Timeout threshold lives here.
     AttemptResult status is determined by comparing
     processing_latency_ms against timeout_ms[provider].
"""

import random
# import math
from dataclasses import dataclass, field
from enum import Enum
from decimal import Decimal
from typing import cast

from events import (
    AttemptStatus,
    CircuitState,
    circuit_evaluation,
    CircuitEvaluation,
)


# ---------------------------------------------------------------------------
# Regime
# ---------------------------------------------------------------------------

class Regime(str, Enum):
    HEALTHY  = "HEALTHY"
    DEGRADED = "DEGRADED"
    OUTAGE   = "OUTAGE"


# ---------------------------------------------------------------------------
# Provider Config
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    """
    Full configuration for one provider.
    Transition matrix rows must sum to 1.0.
    """
    name              : str

    # Markov transition matrix
    # transitions[from_regime][to_regime] = probability
    transitions       : dict[str, dict[str, float]] = field(default_factory=lambda: {
        Regime.HEALTHY : {Regime.HEALTHY: 0.97, Regime.DEGRADED: 0.02, Regime.OUTAGE: 0.01},
        Regime.DEGRADED: {Regime.HEALTHY: 0.40, Regime.DEGRADED: 0.50, Regime.OUTAGE: 0.10},
        Regime.OUTAGE  : {Regime.HEALTHY: 0.10, Regime.DEGRADED: 0.30, Regime.OUTAGE: 0.60},
    })

    # Latency profile per regime (LogNormal parameters)
    latency_mu        : dict[str, float] = field(default_factory=lambda: {
        Regime.HEALTHY : 4.5,   # e^4.5 ≈ 90ms median
        Regime.DEGRADED: 5.2,   # e^5.2 ≈ 181ms median
        Regime.OUTAGE  : 6.2,   # e^6.2 ≈ 493ms median
    })
    latency_sigma     : dict[str, float] = field(default_factory=lambda: {
        Regime.HEALTHY : 0.4,
        Regime.DEGRADED: 0.7,
        Regime.OUTAGE  : 1.0,
    })

    # Failure distribution per regime
    # success / soft_decline / hard_decline probabilities
    failure_rates     : dict[str, dict[str, float]] = field(default_factory=lambda: {
        Regime.HEALTHY : {"success": 0.95, "soft_decline": 0.03, "hard_decline": 0.02},
        Regime.DEGRADED: {"success": 0.70, "soft_decline": 0.20, "hard_decline": 0.10},
        Regime.OUTAGE  : {"success": 0.05, "soft_decline": 0.15, "hard_decline": 0.80},
    })

    # P6 — timeout threshold per regime (ms)
    timeout_ms        : dict[str, int] = field(default_factory=lambda: {
        Regime.HEALTHY : 500,
        Regime.DEGRADED: 750,
        Regime.OUTAGE  : 250,   # fail fast during outage
    })

    # Cost per attempt (Decimal)
    cost_per_attempt  : Decimal = Decimal("0.25")

    # Circuit breaker
    failure_threshold : float = 0.5    # failure_rate > this → OPEN circuit
    eval_window_ms    : int   = 60_000 # evaluate circuit every 60 simulated seconds
    recovery_window_ms: int   = 30_000 # time before OPEN → HALF_OPEN


# ---------------------------------------------------------------------------
# Provider State (mutable runtime state)
# ---------------------------------------------------------------------------

@dataclass
class ProviderState:
    regime              : Regime       = Regime.HEALTHY
    circuit             : CircuitState = CircuitState.CLOSED
    last_eval_ms        : int          = 0
    last_open_ms        : int          = 0   # when circuit last opened
    recent_attempts     : int          = 0
    recent_failures     : int          = 0


# ---------------------------------------------------------------------------
# GatewayModel
# ---------------------------------------------------------------------------

class GatewayModel:

    def __init__(self, providers: list[ProviderConfig]):
        self._configs : dict[str, ProviderConfig] = {p.name: p for p in providers}
        self._states  : dict[str, ProviderState]  = {p.name: ProviderState() for p in providers}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        provider : str,
        clock_ms : int,
    ) -> tuple[AttemptStatus, int, Decimal]:
        """
        Simulates one attempt execution against a provider.
        Returns (status, processing_latency_ms, cost).
        P6 — classifies TIMEOUT by comparing latency against threshold.
        """
        cfg   = self._configs[provider]
        state = self._states[provider]

        # Sample latency from LogNormal distribution
        latency_ms = self._sample_latency(cfg, state.regime)

        # P6 — classify as TIMEOUT if latency exceeds threshold
        timeout_ms = cfg.timeout_ms[state.regime]
        if latency_ms > timeout_ms:
            status = AttemptStatus.TIMEOUT
        else:
            status = self._sample_status(cfg, state.regime)

        # Track for circuit evaluation
        state.recent_attempts += 1
        if status != AttemptStatus.SUCCESS:
            state.recent_failures += 1

        return status, latency_ms, cfg.cost_per_attempt

    def is_up(self, provider: str) -> bool:
        """
        Returns False if circuit is OPEN.
        Used by RoutingHook to enforce I6.
        """
        return self._states[provider].circuit != CircuitState.OPEN

    def evaluate_circuits(self, clock_ms: int) -> list[CircuitEvaluation]:
        """
        Called every tick by TransactionSimulator.
        Evaluates each provider's circuit state over the window.
        Emits CircuitEvaluation events on state changes.
        """
        events = []
        for name, cfg in self._configs.items():
            state = self._states[name]

            # Only evaluate when window has elapsed
            if clock_ms - state.last_eval_ms < cfg.eval_window_ms:
                continue

            state.last_eval_ms = clock_ms

            # Advance Markov regime
            state.regime = self._transition_regime(cfg, state.regime)

            # Compute failure rate over window
            failure_rate = (
                state.recent_failures / state.recent_attempts
                if state.recent_attempts > 0 else 0.0
            )

            # Update circuit state
            new_circuit = self._evaluate_circuit(cfg, state, failure_rate, clock_ms)
            state.circuit = new_circuit

            # Reset window counters
            state.recent_attempts = 0
            state.recent_failures = 0

            events.append(circuit_evaluation(
                provider            = name,
                timestamp           = clock_ms,
                circuit_state       = new_circuit,
                failure_rate_window = failure_rate,
            ))

        return events

    # ------------------------------------------------------------------
    # Internal — regime
    # ------------------------------------------------------------------

    def _transition_regime(self, cfg: ProviderConfig, current: Regime) -> Regime:
        """Draw next regime from Markov transition matrix."""
        row    = cfg.transitions[current]
        states = list(row.keys())
        probs  = list(row.values())
        next_regime = random.choices(states, weights=probs, k=1)[0]
        return cast(Regime, next_regime)

    # ------------------------------------------------------------------
    # Internal — latency (LogNormal, heavy-tailed)
    # ------------------------------------------------------------------

    def _sample_latency(self, cfg: ProviderConfig, regime: Regime) -> int:
        mu    = cfg.latency_mu[regime]
        sigma = cfg.latency_sigma[regime]
        return max(1, int(random.lognormvariate(mu, sigma)))

    # ------------------------------------------------------------------
    # Internal — failure status
    # ------------------------------------------------------------------

    def _sample_status(self, cfg: ProviderConfig, regime: Regime) -> AttemptStatus:
        rates = cfg.failure_rates[regime]
        outcomes = [AttemptStatus.SUCCESS, AttemptStatus.SOFT_DECLINE, AttemptStatus.HARD_DECLINE]
        weights  = [rates["success"], rates["soft_decline"], rates["hard_decline"]]
        return random.choices(outcomes, weights=weights, k=1)[0]

    # ------------------------------------------------------------------
    # Internal — circuit breaker
    # ------------------------------------------------------------------

    def _evaluate_circuit(
        self,
        cfg         : ProviderConfig,
        state       : ProviderState,
        failure_rate: float,
        clock_ms    : int,
    ) -> CircuitState:
        if state.circuit == CircuitState.CLOSED:
            if failure_rate > cfg.failure_threshold:
                state.last_open_ms = clock_ms
                return CircuitState.OPEN

        elif state.circuit == CircuitState.OPEN:
            if clock_ms - state.last_open_ms >= cfg.recovery_window_ms:
                return CircuitState.HALF_OPEN

        elif state.circuit == CircuitState.HALF_OPEN:
            # Allow through if failure rate has recovered
            if failure_rate <= cfg.failure_threshold:
                return CircuitState.CLOSED
            else:
                state.last_open_ms = clock_ms
                return CircuitState.OPEN

        return state.circuit