"""
simulator/transaction_engine.py

TransactionEngine — core state machine for transaction lifecycle.

States:
    INIT → ROUTE → ATTEMPT → RESULT → RETRY → TERMINAL

Produces events:
    NewTransaction
    RouteDecision
    AttemptExecution
    AttemptResult
    RetryDecision

Retry eligibility (industry standard):
    SOFT_DECLINE → retryable
    TIMEOUT      → retryable
    HARD_DECLINE → terminal
    SUCCESS      → terminal
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum

from events import (
    AttemptStatus,
    new_transaction,
    route_decision,
    attempt_execution,
    attempt_result,
    retry_decision,
    BaseEvent,
)


# ---------------------------------------------------------------------------
# Transaction state machine
# ---------------------------------------------------------------------------

class TxnState(Enum):
    INIT     = "INIT"
    ROUTE    = "ROUTE"
    ATTEMPT  = "ATTEMPT"
    RESULT   = "RESULT"
    RETRY    = "RETRY"
    TERMINAL = "TERMINAL"


RETRYABLE_STATUSES = {AttemptStatus.SOFT_DECLINE, AttemptStatus.TIMEOUT}


# ---------------------------------------------------------------------------
# Internal transaction context
# ---------------------------------------------------------------------------

@dataclass
class TxnContext:
    """
    Mutable per-transaction state owned by TransactionEngine.
    Never exposed outside the engine.
    """
    txn_id              : str
    created_at          : int
    state               : TxnState             = TxnState.INIT
    attempt_count       : int                  = 0
    active_provider     : str                  = ""
    last_status         : AttemptStatus | None = None
    _current_attempt_id : str                  = ""


# ---------------------------------------------------------------------------
# TransactionEngine
# ---------------------------------------------------------------------------

class TransactionEngine:

    def __init__(self):
        # In-flight transactions keyed by txn_id
        self._active: dict[str, TxnContext] = {}

    async def process(
        self,
        txn           : dict,
        clock_ms      : int,
        policy_engine,        # PolicyEngine
        gateway_model,        # GatewayModel
    ) -> list[BaseEvent]:
        """
        Drives one transaction through its full lifecycle.
        Returns all events emitted during processing.
        """
        events: list[BaseEvent] = []

        ctx = TxnContext(
            txn_id     = txn["txn_id"],
            created_at = txn["created_at"],
        )

        # INIT → emit NewTransaction
        events.append(self._init(ctx))

        # Drive state machine until terminal
        while ctx.state != TxnState.TERMINAL:
            if ctx.state == TxnState.ROUTE:
                events.append(self._route(ctx, clock_ms, policy_engine))

            elif ctx.state == TxnState.ATTEMPT:
                events.append(self._attempt(ctx, clock_ms))

            elif ctx.state == TxnState.RESULT:
                result_event, latency_ms = self._result(ctx, clock_ms, gateway_model)
                events.append(result_event)
                clock_ms += latency_ms      # advance local clock by processing time

            elif ctx.state == TxnState.RETRY:
                retry_event, should_retry = self._retry(ctx, clock_ms, policy_engine)
                events.append(retry_event)
                if not should_retry:
                    ctx.state = TxnState.TERMINAL

        return events

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _init(self, ctx: TxnContext) -> BaseEvent:
        ctx.state = TxnState.ROUTE
        return new_transaction(
            txn_id          = ctx.txn_id,
            created_at      = ctx.created_at,
            amount          = __import__('decimal').Decimal("0"),  # amount comes from outside scope
            currency        = "USD",
            sla_deadline_ms = ctx.created_at + 5_000,  # 5 second SLA default
        )

    def _route(
        self,
        ctx          : TxnContext,
        clock_ms     : int,
        policy_engine,
    ) -> BaseEvent:
        t_start = clock_ms
        ctx.active_provider = policy_engine.choose_provider(ctx.txn_id)
        ctx.state = TxnState.ATTEMPT
        decision_latency = 2    # routing decision takes ~2ms

        return route_decision(
            txn_id              = ctx.txn_id,
            decision_id         = f"dec_{uuid.uuid4().hex[:8]}",
            timestamp           = t_start,
            selected_provider   = ctx.active_provider,
            decision_latency_ms = decision_latency,
        )

    def _attempt(self, ctx: TxnContext, clock_ms: int) -> BaseEvent:
        ctx.attempt_count += 1
        attempt_id = f"att_{uuid.uuid4().hex[:8]}"
        ctx._current_attempt_id = attempt_id     # stash for result stage
        ctx.state = TxnState.RESULT

        return attempt_execution(
            txn_id         = ctx.txn_id,
            attempt_id     = attempt_id,
            provider       = ctx.active_provider,
            attempt_number = ctx.attempt_count,
            started_at     = clock_ms,
        )

    def _result(
        self,
        ctx          : TxnContext,
        clock_ms     : int,
        gateway_model,
    ) -> tuple[BaseEvent, int]:
        status, latency_ms, cost = gateway_model.execute(ctx.active_provider)
        ctx.last_status = status

        # Determine next state
        if status == AttemptStatus.SUCCESS:
            ctx.state = TxnState.TERMINAL
        elif status == AttemptStatus.HARD_DECLINE:
            ctx.state = TxnState.TERMINAL
        else:
            ctx.state = TxnState.RETRY

        event = attempt_result(
            txn_id                = ctx.txn_id,
            attempt_id            = ctx._current_attempt_id,
            provider              = ctx.active_provider,
            completed_at          = clock_ms + latency_ms,
            status                = status,
            processing_latency_ms = latency_ms,
            provider_cost         = cost,
        )
        return event, latency_ms

    def _retry(
        self,
        ctx          : TxnContext,
        clock_ms     : int,
        policy_engine,
    ) -> tuple[BaseEvent, bool]:
        allowed, backoff_ms = policy_engine.should_retry(
            txn_id        = ctx.txn_id,
            attempt_count = ctx.attempt_count,
            last_status   = ctx.last_status,
        )

        if allowed:
            ctx.state = TxnState.ROUTE   # re-route on retry
        else:
            ctx.state = TxnState.TERMINAL

        event = retry_decision(
            txn_id        = ctx.txn_id,
            attempt_id    = ctx._current_attempt_id,
            timestamp     = clock_ms,
            retry_allowed = allowed,
            backoff_ms    = backoff_ms if allowed else 0,
        )
        return event, allowed