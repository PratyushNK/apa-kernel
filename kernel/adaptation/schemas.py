"""
kernel/adaptation/schemas.py

Pydantic schemas for LLM structured input/output.
Kept minimal for token efficiency.
"""

from __future__ import annotations
from pydantic import AliasChoices, BaseModel, Field
from typing import Optional


class AdaptationContext(BaseModel):
    """
    Input context provided to the LLM agent.
    Contains only what is necessary for reasoning — no noise.
    """
    # Current metrics
    approval_rate              : float
    rolling_success_rate       : float
    retry_amplification        : float
    circuit_open_rate          : float
    sla_breach_rate            : float
    timeout_rate               : float

    # Per-provider breakdown
    provider_success_rates     : dict[str, float]
    provider_circuit_states    : dict[str, str]

    # Degradation delta from last healthy baseline
    # None if no healthy baseline has been captured yet
    approval_rate_delta        : Optional[float] = None
    success_rate_delta         : Optional[float] = None
    circuit_open_rate_delta    : Optional[float] = None
    retry_amplification_delta  : Optional[float] = None

    # Active invariant breaches
    invariant_breaches         : list[str]

    # Current policy vector — agent sees what it is changing
    current_theta              : dict

    # Adaptation objective
    objective                  : str = "cure"


class PolicyVectorSchema(BaseModel):
    """
    Pydantic mirror of simulator.policy_engine.PolicyVector.
    Used for LLM structured output — ensures type safety and schema enforcement.
    """
    provider_priority      : list[str]        = Field(description="Ordered list of provider names e.g. ['G1', 'G2']")
    provider_weights       : dict[str, float] = Field(description="Routing weights per provider. Must sum to 1.0 e.g. {'G1': 0.1, 'G2': 0.9}")
    weight_learning_rate   : float            = Field(ge=0.0, le=1.0, description="Weight update rate 0.0-1.0")
    max_retry              : int              = Field(ge=1, le=5, description="Max retry attempts per transaction 1-5")
    retryable_statuses     : list[str]        = Field(description="Statuses eligible for retry e.g. ['SOFT_DECLINE', 'TIMEOUT']")
    base_backoff_ms        : int              = Field(ge=10, le=5000, description="Base backoff in ms 10-5000")
    backoff_multiplier     : float            = Field(ge=1.0, le=5.0, description="Exponential backoff multiplier 1.0-5.0")
    retry_budget_window_ms : int              = Field(ge=1000, description="Retry budget window in ms")
    max_retries_per_window : int              = Field(ge=1, description="Max retries allowed per window")


class PolicyPatchSchema(BaseModel):
    """
    Relaxed stage-2 output schema.
    Allows partial updates and common alias keys from model outputs.
    """
    provider_priority      : Optional[list[str]]        = Field(default=None)
    provider_weights       : Optional[dict[str, float]] = Field(
        default=None,
        validation_alias=AliasChoices("provider_weights", "weights", "routing_weights"),
    )
    weight_learning_rate   : Optional[float]            = Field(default=None, ge=0.0, le=1.0)
    max_retry              : Optional[int]              = Field(default=None, ge=1, le=5)
    retryable_statuses     : Optional[list[str]]        = Field(default=None)
    base_backoff_ms        : Optional[int]              = Field(default=None, ge=10, le=5000)
    backoff_multiplier     : Optional[float]            = Field(default=None, ge=1.0, le=5.0)
    retry_budget_window_ms : Optional[int]              = Field(default=None, ge=1000)
    max_retries_per_window : Optional[int]              = Field(default=None, ge=1)


class AdaptationDecision(BaseModel):
    """
    Stage 1 structured output from LLM reasoning call.
    """
    reasoning           : str   = Field(description="Max 40 words. What is wrong and why.")
    confidence          : float = Field(ge=0.0, le=1.0, description="0.0-1.0 confidence in proposal.")
    expected_improvement: str   = Field(description="Max 40 words. What metric should improve.")


class CorrectionContext(BaseModel):
    """
    Input to correction attempt for Stage 2 policy vector generation.
    """
    rejected_theta     : PolicyVectorSchema
    violations         : list[str]
    correction_hint    : str = "Adjust only the fields causing violations. Keep all other fields unchanged."


class AdaptationState(BaseModel):
    """
    Full state object passed between loop nodes.
    LangGraph-compatible — each node receives and returns this.
    """
    # Input
    context             : AdaptationContext

    # Agent output
    decision            : Optional[AdaptationDecision] = None
    proposed_theta      : Optional[PolicyVectorSchema] = None

    # Verification
    verification_pass   : bool = False
    violations          : list[str] = []

    # Loop control
    cycle_count         : int = 0       # max 3 full cycles
    correction_count    : int = 0       # max 1 correction per cycle
    objective           : str = "cure"

    # Terminal status
    status              : str = "running"  # running | success | failed | max_cycles