"""
simulator/events.py

Bounded event model for the APA Kernel transaction simulator.

Design decisions:
    - All events are frozen dataclasses (immutable once emitted)
    - Timestamps are int epoch milliseconds (financial system standard)
    - event_type is auto-set via factory functions — callers never pass it
    - Shared BaseEvent carries event_type + txn_id
    - EVENT_REGISTRY maps event_type string -> class for log deserialization
    - slots=True on all classes for memory efficiency at scale
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Type


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class AttemptStatus(str, Enum):
    SUCCESS      = "SUCCESS"
    SOFT_DECLINE = "SOFT_DECLINE"
    HARD_DECLINE = "HARD_DECLINE"
    TIMEOUT      = "TIMEOUT"


class CircuitState(str, Enum):
    OPEN      = "OPEN"
    CLOSED    = "CLOSED"
    HALF_OPEN = "HALF_OPEN"


# ---------------------------------------------------------------------------
# Registry (populated at class-definition time below)
# ---------------------------------------------------------------------------

EVENT_REGISTRY: dict[str, type] = {}


# ---------------------------------------------------------------------------
# Base Event
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BaseEvent:
    """
    Fields present on every event.

    event_type  -- discriminator; set by factory functions, never by caller.
    txn_id      -- transaction this event belongs to.
                   CircuitEvaluation is provider-scoped; txn_id = "" by
                   convention for log uniformity.
    """
    event_type : str
    txn_id     : str


# ---------------------------------------------------------------------------
# Event 1 -- NewTransaction
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NewTransaction(BaseEvent):
    """
    Emitted once when the simulator creates a new transaction.

    amount          -- Decimal for exact monetary arithmetic; never float.
    currency        -- ISO 4217 code (e.g. "USD", "EUR").
    sla_deadline_ms -- Absolute epoch-ms deadline by which the transaction
                       must settle. Derived as created_at + allowed_window_ms.
    created_at      -- Epoch ms when the transaction was created.
    """
    created_at      : int     = field(default=0)
    amount          : Decimal = field(default_factory=lambda: Decimal("0"))
    currency        : str     = field(default="")
    sla_deadline_ms : int     = field(default=0)

EVENT_REGISTRY["NewTransaction"] = NewTransaction

def new_transaction(
    *,
    txn_id         : str,
    created_at     : int,
    amount         : Decimal,
    currency       : str,
    sla_deadline_ms: int,
) -> NewTransaction:
    return NewTransaction(
        event_type      = "NewTransaction",
        txn_id          = txn_id,
        created_at      = created_at,
        amount          = amount,
        currency        = currency,
        sla_deadline_ms = sla_deadline_ms,
    )


# ---------------------------------------------------------------------------
# Event 2 -- RouteDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RouteDecision(BaseEvent):
    """
    Emitted by the router after selecting a provider for a transaction.

    decision_id         -- Unique ID for this routing decision.
                           One txn may have multiple RouteDecisions on failover.
    selected_provider   -- Provider identifier (e.g. "stripe", "adyen").
    decision_latency_ms -- Wall-clock time the routing logic took, in ms.
    timestamp           -- Epoch ms when the decision was made.
    """
    decision_id         : str = field(default="")
    timestamp           : int = field(default=0)
    selected_provider   : str = field(default="")
    decision_latency_ms : int = field(default=0)

EVENT_REGISTRY["RouteDecision"] = RouteDecision

def route_decision(
    *,
    txn_id             : str,
    decision_id        : str,
    timestamp          : int,
    selected_provider  : str,
    decision_latency_ms: int,
) -> RouteDecision:
    return RouteDecision(
        event_type          = "RouteDecision",
        txn_id              = txn_id,
        decision_id         = decision_id,
        timestamp           = timestamp,
        selected_provider   = selected_provider,
        decision_latency_ms = decision_latency_ms,
    )


# ---------------------------------------------------------------------------
# Event 3 -- AttemptExecution
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AttemptExecution(BaseEvent):
    """
    Emitted when a single execution attempt is dispatched to a provider.

    attempt_id     -- Stable join key with AttemptResult and RetryDecision.
    attempt_number -- 1-based. Values > 1 indicate a retry attempt.
    started_at     -- Epoch ms when the attempt was sent to the provider.
    """
    attempt_id     : str = field(default="")
    provider       : str = field(default="")
    attempt_number : int = field(default=1)
    started_at     : int = field(default=0)

EVENT_REGISTRY["AttemptExecution"] = AttemptExecution

def attempt_execution(
    *,
    txn_id        : str,
    attempt_id    : str,
    provider      : str,
    attempt_number: int,
    started_at    : int,
) -> AttemptExecution:
    return AttemptExecution(
        event_type     = "AttemptExecution",
        txn_id         = txn_id,
        attempt_id     = attempt_id,
        provider       = provider,
        attempt_number = attempt_number,
        started_at     = started_at,
    )


# ---------------------------------------------------------------------------
# Event 4 -- AttemptResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AttemptResult(BaseEvent):
    """
    Emitted when a provider returns a result for an attempt.

    status                -- AttemptStatus enum value.
    processing_latency_ms -- Provider-side processing time; drives p95 metrics.
    provider_cost         -- Decimal cost charged by provider for this attempt,
                             regardless of outcome.
    completed_at          -- Epoch ms when result was received.
    """
    attempt_id            : str           = field(default="")
    provider              : str           = field(default="")
    completed_at          : int           = field(default=0)
    status                : AttemptStatus = field(default=AttemptStatus.SUCCESS)
    processing_latency_ms : int           = field(default=0)
    provider_cost         : Decimal       = field(default_factory=lambda: Decimal("0"))

EVENT_REGISTRY["AttemptResult"] = AttemptResult

def attempt_result(
    *,
    txn_id               : str,
    attempt_id           : str,
    provider             : str,
    completed_at         : int,
    status               : AttemptStatus,
    processing_latency_ms: int,
    provider_cost        : Decimal,
) -> AttemptResult:
    return AttemptResult(
        event_type            = "AttemptResult",
        txn_id                = txn_id,
        attempt_id            = attempt_id,
        provider              = provider,
        completed_at          = completed_at,
        status                = status,
        processing_latency_ms = processing_latency_ms,
        provider_cost         = provider_cost,
    )


# ---------------------------------------------------------------------------
# Event 5 -- RetryDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RetryDecision(BaseEvent):
    """
    Emitted by the retry engine after evaluating whether to retry.

    retry_allowed -- False means the transaction is terminal after this attempt.
    backoff_ms    -- Wait before next attempt. Set to 0 when retry_allowed=False.
    timestamp     -- Epoch ms when the retry decision was made.
    """
    attempt_id    : str  = field(default="")
    timestamp     : int  = field(default=0)
    retry_allowed : bool = field(default=False)
    backoff_ms    : int  = field(default=0)

EVENT_REGISTRY["RetryDecision"] = RetryDecision

def retry_decision(
    *,
    txn_id       : str,
    attempt_id   : str,
    timestamp    : int,
    retry_allowed: bool,
    backoff_ms   : int,
) -> RetryDecision:
    return RetryDecision(
        event_type    = "RetryDecision",
        txn_id        = txn_id,
        attempt_id    = attempt_id,
        timestamp     = timestamp,
        retry_allowed = retry_allowed,
        backoff_ms    = backoff_ms,
    )


# ---------------------------------------------------------------------------
# Event 6 -- CircuitEvaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CircuitEvaluation(BaseEvent):
    """
    Emitted by the circuit breaker when it evaluates a provider's health.

    Provider-scoped, not transaction-scoped.
    txn_id = "" by convention for log uniformity.

    circuit_state       -- Current state of the circuit breaker.
    failure_rate_window -- Failure rate [0.0, 1.0] over the evaluation window.
    timestamp           -- Epoch ms of evaluation.
    """
    provider            : str          = field(default="")
    timestamp           : int          = field(default=0)
    circuit_state       : CircuitState = field(default=CircuitState.CLOSED)
    failure_rate_window : float        = field(default=0.0)

EVENT_REGISTRY["CircuitEvaluation"] = CircuitEvaluation

def circuit_evaluation(
    *,
    provider           : str,
    timestamp          : int,
    circuit_state      : CircuitState,
    failure_rate_window: float,
) -> CircuitEvaluation:
    return CircuitEvaluation(
        event_type          = "CircuitEvaluation",
        txn_id              = "",
        provider            = provider,
        timestamp           = timestamp,
        circuit_state       = circuit_state,
        failure_rate_window = failure_rate_window,
    )