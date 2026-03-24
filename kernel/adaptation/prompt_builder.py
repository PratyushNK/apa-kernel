"""
kernel/adaptation/prompt_builder.py

Builds minimal token-efficient prompts for the adaptation agent.
System prompt is fixed. User prompt is structured, not narrative.
"""

from __future__ import annotations
import json
from kernel.adaptation.schemas import (
  AdaptationContext,
  AdaptationDecision,
  CorrectionContext,
)


SYSTEM_PROMPT = """You are a payment routing policy optimizer.
Given degraded system metrics and the current policy vector, propose minimal conservative changes to restore system health.
Rules:
- Change as few policy fields as possible
- Never set max_retry > 5
- Never set provider_weights values that don't sum to 1.0
- Never set base_backoff_ms < 10 or > 5000
- Never set timeout thresholds outside 50-5000ms range
- Prefer routing away from failing providers over aggressive retry increases"""


CORRECTION_SYSTEM_PROMPT = """You are a payment routing policy optimizer.
Your previous proposal violated invariant constraints.
Correct only the fields that caused violations. Keep all other fields identical.
"""


THETA_SYSTEM_PROMPT = """You are a payment routing policy parameter setter.
Given a reasoning analysis of a degraded payment system, return the exact policy vector parameters to implement the proposed fix.
Return only the policy vector parameters needed to apply the fix."""

# When SLA breaches or timeout_rate are present, prefer decisive mitigation
# over minor cost optimizations: reduce traffic to failing providers
# (set that provider weight <= 0.1), increase traffic to healthier providers,
# set `max_retry` <= 1 and reduce `base_backoff_ms` (e.g. 50ms). Always keep
# weights summing to 1.0 and respect other constraints.



def build_adaptation_prompt(ctx: AdaptationContext) -> str:
    delta_section = ""
    if ctx.approval_rate_delta is not None:
        delta_section = f"""
Degradation from last healthy baseline:
  approval_rate_delta: {ctx.approval_rate_delta:+.3f}
  success_rate_delta: {ctx.success_rate_delta:+.3f}
  circuit_open_rate_delta: {ctx.circuit_open_rate_delta:+.3f}
  retry_amplification_delta: {ctx.retry_amplification_delta:+.3f}"""
    else:
        delta_section = "\nNo healthy baseline captured yet."

    return f"""Current metrics:
  approval_rate: {ctx.approval_rate:.3f}
  rolling_success_rate: {ctx.rolling_success_rate:.3f}
  retry_amplification: {ctx.retry_amplification:.3f}
  circuit_open_rate: {ctx.circuit_open_rate:.3f}
  sla_breach_rate: {ctx.sla_breach_rate:.3f}
  timeout_rate: {ctx.timeout_rate:.3f}
{delta_section}

Provider states:
{json.dumps(ctx.provider_success_rates, indent=2)}
{json.dumps(ctx.provider_circuit_states, indent=2)}

Active invariant breaches: {ctx.invariant_breaches}

Current policy vector:
{json.dumps(ctx.current_theta, indent=2)}

Objective: {ctx.objective}

Assess system state and decide the smallest effective adaptation.
Reason about likely causes, expected impact, and confidence.
Do not propose policy vector values in this stage.
Return the answer via the structured response channel."""


def build_theta_prompt(decision: AdaptationDecision, current_theta: dict) -> str:
    return f"""Reasoning: {decision.reasoning}
Expected improvement: {decision.expected_improvement}
Confidence: {decision.confidence}

Current policy vector:
{json.dumps(current_theta, indent=2)}

Propose policy vector parameter updates that implement the reasoning.
Prefer minimal changes.
You may leave unchanged fields untouched.
Return the answer via the structured response channel."""


def build_correction_prompt(ctx: CorrectionContext) -> str:
    return f"""Your previous proposal was rejected.
Violations: {ctx.violations}

Original proposal:
{json.dumps(ctx.rejected_theta.model_dump(), indent=2)}

{ctx.correction_hint}
Correct and resubmit.
Return the answer via the structured response channel."""