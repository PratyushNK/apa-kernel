"""
kernel/adaptation/schemas.py

Pydantic schemas for LLM structured input/output.
Kept minimal for token efficiency.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
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


class AdaptationDecision(BaseModel):
    """
    Structured output from LLM agent.
    Token budget: ~150 tokens.
    """
    reasoning          : str   = Field(description="Max 80 words. What is wrong and why.")
    proposed_theta     : dict  = Field(description="Full policy vector with proposed changes.")
    confidence         : float = Field(ge=0.0, le=1.0, description="0.0-1.0 confidence in proposal.")
    expected_improvement: str  = Field(description="Max 30 words. What metric should improve.")


class CorrectionContext(BaseModel):
    """
    Input to correction attempt when verification fails.
    Tells agent exactly what it violated.
    """
    original_decision  : AdaptationDecision
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
    decision            : AdaptationDecision | None = None

    # Verification
    verification_pass   : bool = False
    violations          : list[str] = []

    # Loop control
    cycle_count         : int = 0       # max 3 full cycles
    correction_count    : int = 0       # max 1 correction per cycle
    objective           : str = "cure"

    # Terminal status
    status              : str = "running"  # running | success | failed | max_cycles