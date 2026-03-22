"""
Simple smoke test for generate_structured() on Gemini and Azure.
"""

from __future__ import annotations

import pathlib
import sys

from pydantic import BaseModel, Field

sys.path.append(str(pathlib.Path(__file__).parent / ".."))

from interfaces.llm import LLM
from services.llms.azure_openai import AzureOpenAILLM
from services.llms.gemini import GeminiLLM
from kernel.adaptation.schemas import (
    AdaptationContext,
    AdaptationDecision,
    PolicyPatchSchema,
    PolicyVectorSchema,
)
from kernel.adaptation.prompt_builder import (
    SYSTEM_PROMPT,
    THETA_SYSTEM_PROMPT,
    build_adaptation_prompt,
    build_theta_prompt,
)


class SimpleStructuredResponse(BaseModel):
    topic: str = Field(description="Short topic extracted from the prompt")
    response: str = Field(description="A concise response in about fifty words")


def _count_words(text: str) -> int:
    return len([w for w in text.strip().split() if w])


def _build_input_prompt() -> str:
    prompt = (
        "Explain why structured outputs improve reliability in automated software workflows. "
        "Include one testing example and one monitoring example. Use simple language, mention "
        "one tradeoff, avoid marketing tone, and focus on practical engineering use in "
        "production services. Keep it concise, clear, and directly actionable for teams "
        "working under real operational constraints."
    )
    assert _count_words(prompt) == 50, "Input prompt must be exactly 50 words"
    return prompt


def test_llm(name: str, llm: LLM) -> None:
    prompt = _build_input_prompt()

    print(f"\n--- {name} Test ---")
    print(f"Input words: {_count_words(prompt)}")

    result = llm.generate_structured(
        schema=SimpleStructuredResponse,
        prompt=prompt,
        system_prompt="Return structured response with topic and response. Keep response around fifty words.",
        max_tokens=120,
    )

    if result is None:
        raise RuntimeError(f"{name} returned None")

    output_words = _count_words(result.response)
    print(f"Topic: {result.topic}")
    print(f"Output words: {output_words}")
    print(f"Response: {result.response}")

    if output_words < 25 or output_words > 90:
        raise AssertionError(f"{name} word count out of range: {output_words}")

    print(f"✓ {name} passed")


def test_stage1_reasoning(name: str, llm: LLM) -> None:
    """Stage-1: Test LLM reasoning about degraded metrics (AdaptationDecision)."""
    print(f"\n--- {name} Stage-1 Reasoning Test ---")

    # Realistic degraded context
    ctx = AdaptationContext(
        approval_rate=0.75,
        rolling_success_rate=0.82,
        retry_amplification=2.1,
        circuit_open_rate=0.15,
        sla_breach_rate=0.08,
        timeout_rate=0.12,
        provider_success_rates={"G1": 0.95, "G2": 0.65, "G3": 0.88},
        provider_circuit_states={"G1": "healthy", "G2": "degraded", "G3": "healthy"},
        approval_rate_delta=-0.05,
        success_rate_delta=-0.08,
        circuit_open_rate_delta=+0.12,
        retry_amplification_delta=+0.9,
        invariant_breaches=["approval_rate < 0.80", "retry_amplification > 2.0"],
        current_theta={
            "provider_priority": ["G1", "G2", "G3"],
            "provider_weights": {"G1": 0.5, "G2": 0.3, "G3": 0.2},
            "weight_learning_rate": 0.1,
            "max_retry": 3,
            "retryable_statuses": ["SOFT_DECLINE", "TIMEOUT"],
            "base_backoff_ms": 50,
            "backoff_multiplier": 2.0,
            "retry_budget_window_ms": 5000,
            "max_retries_per_window": 2,
        },
    )

    prompt = build_adaptation_prompt(ctx)
    print(f"Prompt length: {len(prompt)} chars")

    result = llm.generate_structured(
        schema=AdaptationDecision,
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        max_tokens=150,
    )

    if result is None:
        raise RuntimeError(f"{name} returned None")

    print(f"Reasoning: {result.reasoning}")
    print(f"Confidence: {result.confidence:.2f}")
    print(f"Expected improvement: {result.expected_improvement}")

    # Validate result
    if not (0.0 <= result.confidence <= 1.0):
        raise AssertionError(f"Invalid confidence: {result.confidence}")
    if len(result.reasoning) > 200:
        raise AssertionError(f"Reasoning too long: {len(result.reasoning)} chars")

    print(f"✓ {name} Stage-1 passed")



def test_stage2_theta(name: str, llm: LLM, decision: AdaptationDecision) -> None:
    """Stage-2: Test LLM policy vector generation (PolicyPatchSchema, then merged)."""
    print(f"\n--- {name} Stage-2 Theta Test ---")

    current_theta = {
        "provider_priority": ["G1", "G2", "G3"],
        "provider_weights": {"G1": 0.5, "G2": 0.3, "G3": 0.2},
        "weight_learning_rate": 0.1,
        "max_retry": 3,
        "retryable_statuses": ["SOFT_DECLINE", "TIMEOUT"],
        "base_backoff_ms": 50,
        "backoff_multiplier": 2.0,
        "retry_budget_window_ms": 5000,
        "max_retries_per_window": 2,
    }

    prompt = build_theta_prompt(decision, current_theta)
    print(f"Prompt length: {len(prompt)} chars")

    # Stage-2 returns a partial patch, not full schema
    result = llm.generate_structured(
        schema=PolicyPatchSchema,
        prompt=prompt,
        system_prompt=THETA_SYSTEM_PROMPT,
        max_tokens=200,
    )

    if result is None:
        raise RuntimeError(f"{name} returned None")

    print(f"Patch: {result.model_dump(exclude_none=True)}")

    # Merge patch with current theta
    merged = dict(current_theta)
    for key, value in result.model_dump(exclude_none=True).items():
        merged[key] = value

    # Validate merged result is a valid full PolicyVectorSchema
    full_theta = PolicyVectorSchema(**merged)

    print(f"Merged provider priority: {full_theta.provider_priority}")
    print(f"Merged provider weights: {full_theta.provider_weights}")
    print(f"Merged max retry: {full_theta.max_retry}")
    print(f"Merged base backoff ms: {full_theta.base_backoff_ms}")

    # Validate constraints on final merged schema
    if not (1 <= full_theta.max_retry <= 5):
        raise AssertionError(f"max_retry out of range: {full_theta.max_retry}")
    if not (10 <= full_theta.base_backoff_ms <= 5000):
        raise AssertionError(f"base_backoff_ms out of range: {full_theta.base_backoff_ms}")
    weights_sum = sum(full_theta.provider_weights.values())
    if not (0.99 <= weights_sum <= 1.01):
        raise AssertionError(f"provider_weights don't sum to 1.0: {weights_sum}")

    print(f"✓ {name} Stage-2 passed")


def main() -> None:
    failures = []

    # print("=" * 60)
    # print("TEST 1: Basic Structured Output (SimpleStructuredResponse)")
    # print("=" * 60)

    # # Test Gemini
    # try:
    #     gemini = GeminiLLM(model="gemini-2.5-flash", max_tokens=120)
    #     test_llm("Gemini", gemini)
    # except Exception as e:
    #     failures.append(f"Gemini basic: {e}")
    #     print(f"✗ Gemini failed: {e}")

    # # Test Azure
    # try:
    #     azure = AzureOpenAILLM("o4-mini")
    #     test_llm("Azure", azure)
    # except Exception as e:
    #     failures.append(f"Azure basic: {e}")
    #     print(f"✗ Azure failed: {e}")

    # print("\n" + "=" * 60)
    print("TEST 2: Stage-1 Reasoning (AdaptationDecision)")
    print("=" * 60)

    gemini_decision = None
    azure_decision = None

    # Test Gemini Stage-1
    try:
        gemini = GeminiLLM(model="gemini-2.5-flash", max_tokens=200)
        gemini_decision = test_stage1_reasoning("Gemini", gemini)
    except Exception as e:
        failures.append(f"Gemini stage-1: {e}")
        print(f"✗ Gemini Stage-1 failed: {e}")

    # # Test Azure Stage-1
    # try:
    #     azure = AzureOpenAILLM("o4-mini")
    #     azure_decision = test_stage1_reasoning("Azure", azure)
    # except Exception as e:
    #     failures.append(f"Azure stage-1: {e}")
    #     print(f"✗ Azure Stage-1 failed: {e}")

    print("\n" + "=" * 60)
    print("TEST 3: Stage-2 Theta (PolicyVectorSchema)")
    print("=" * 60)

    # Test Gemini Stage-2 (if Stage-1 succeeded)
    if gemini_decision is not None:
        try:
            gemini = GeminiLLM(model="gemini-2.5-flash", max_tokens=250)
            test_stage2_theta("Gemini", gemini, gemini_decision)
        except Exception as e:
            failures.append(f"Gemini stage-2: {e}")
            print(f"✗ Gemini Stage-2 failed: {e}")
    else:
        print("⊘ Skipping Gemini Stage-2 (Stage-1 failed)")

    # # Test Azure Stage-2 (if Stage-1 succeeded)
    # if azure_decision is not None:
    #     try:
    #         azure = AzureOpenAILLM("o4-mini")
    #         test_stage2_theta("Azure", azure, azure_decision)
    #     except Exception as e:
    #         failures.append(f"Azure stage-2: {e}")
    #         print(f"✗ Azure Stage-2 failed: {e}")
    # else:
    #     print("⊘ Skipping Azure Stage-2 (Stage-1 failed)")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if failures:
        for item in failures:
            print(item)
        raise SystemExit(1)

    print("All tests passed.")


if __name__ == "__main__":
    main()
