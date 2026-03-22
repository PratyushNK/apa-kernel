"""
Component-level adaptation tests with no external LLM calls.

Usage:
    /Users/pratyushnk/Documents/projects/apa-kernel/.venv/bin/python tests/adaptation_test.py

This script validates:
1. MockLLM stage-1 and stage-2 structured outputs.
2. Adaptation loop two-stage behavior (reasoning -> theta proposal) without real model calls.
3. Engine failure backoff prevents rapid re-trigger after failed adaptation.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, cast

sys.path.append(str(pathlib.Path(__file__).parent / ".."))

from kernel.adaptation.loop import AdaptationLoop
from kernel.adaptation.schemas import (
    AdaptationContext,
    AdaptationDecision,
    AdaptationState,
    PolicyPatchSchema,
)
from kernel.engine.runner import KernelEngine
from kernel.verification.verifier import InvariantVerifier
from services.llms.azure_openai import AzureOpenAILLM
from services.llms.mock import MockLLM
from simulator.policy_engine import PolicyStore


class FakeAggregator:
    """Minimal aggregator stub for component tests."""

    def __init__(self) -> None:
        self._breach = True

    def get_snapshot(self):
        snapshot = SimpleNamespace(
            approval_rate=0.80,
            rolling_success_rate=0.78,
            retry_amplification_factor=1.5,
            circuit_open_rate=0.45,
            sla_breach_rate=0.25,
            timeout_rate=0.12,
            has_sufficient_data=True,
            per_provider=[
                SimpleNamespace(provider="G1", rolling_success_rate=0.20, circuit_open_rate=0.80),
                SimpleNamespace(provider="G2", rolling_success_rate=0.95, circuit_open_rate=0.00),
            ],
            invariant_risk=SimpleNamespace(
                any_breach=True,
                I2_retry_bound=False,
                I6_circuit_respect=True,
                I7_sla_breach=True,
            ),
        )
        delta = SimpleNamespace(
            has_baseline=False,
            approval_rate_delta=None,
            rolling_success_rate_delta=None,
            circuit_open_rate_delta=None,
            retry_amplification_delta=None,
        )
        return snapshot, delta

    def pop_breach(self) -> bool:
        current = self._breach
        self._breach = False
        return current


class AlwaysFailLoop:
    """Adaptation loop stub that always fails without calling an LLM."""

    async def run(self, objective: str = "cure"):
        return SimpleNamespace(status="failed", cycle_count=0)


class _FakeStructuredRunnable:
    """Emulates LangChain runnable returned by with_structured_output."""

    def __init__(self, result):
        self._result = result

    def with_config(self, configurable=None):
        return self

    def invoke(self, messages):
        return self._result


class _FakeAzureModel:
    """Captures schema requests and returns deterministic pydantic objects."""

    def __init__(self):
        self.calls = []

    def with_structured_output(self, schema, method="function_calling"):
        self.calls.append((schema.__name__, method))

        if schema.__name__ == "AdaptationDecision":
            result = schema(
                reasoning="G1 has degraded health; shift routing to G2.",
                confidence=0.84,
                expected_improvement="Approval rate should recover.",
            )
        elif schema.__name__ == "PolicyPatchSchema":
            result = schema(
                provider_weights={"G1": 0.15, "G2": 0.85},
                max_retry=3,
            )
        else:
            result = schema()

        return _FakeStructuredRunnable(result)


def _make_context() -> AdaptationContext:
    return AdaptationContext(
        approval_rate=0.80,
        rolling_success_rate=0.78,
        retry_amplification=1.5,
        circuit_open_rate=0.45,
        sla_breach_rate=0.25,
        timeout_rate=0.12,
        provider_success_rates={"G1": 0.20, "G2": 0.95},
        provider_circuit_states={"G1": "OPEN", "G2": "CLOSED"},
        invariant_breaches=["I6_circuit_respect", "I7_sla_breach"],
        current_theta={
            "provider_priority": ["G1", "G2"],
            "provider_weights": {"G1": 0.5, "G2": 0.5},
            "weight_learning_rate": 0.1,
            "max_retry": 3,
            "retryable_statuses": ["SOFT_DECLINE", "TIMEOUT"],
            "base_backoff_ms": 100,
            "backoff_multiplier": 2.0,
            "retry_budget_window_ms": 60000,
            "max_retries_per_window": 200,
        },
        objective="cure",
    )


def test_mock_llm_structured_outputs() -> None:
    llm = MockLLM()

    decision = llm.generate_structured(AdaptationDecision, prompt="stage1")
    assert decision is not None
    assert isinstance(decision, AdaptationDecision)
    assert decision.confidence == 0.9
    assert "routing away" in decision.reasoning

    patch = llm.generate_structured(PolicyPatchSchema, prompt="stage2")
    assert patch is not None
    assert isinstance(patch, PolicyPatchSchema)
    assert patch.provider_weights == {"G1": 0.1, "G2": 0.9}
    assert patch.max_retry == 3

    print("[ok] MockLLM structured outputs are valid for both stages")


def test_azure_structured_outputs_offline_monkeypatch() -> None:
    """
    Offline validation for Azure adapter structured parsing path.
    No network calls are made.
    """
    fake_model = _FakeAzureModel()

    azure = AzureOpenAILLM.__new__(AzureOpenAILLM)
    azure.deployment = "o4-mini"
    azure._llm = cast(Any, fake_model)

    stage1 = azure.generate_structured(
        AdaptationDecision,
        prompt="stage-1 prompt",
        system_prompt="stage-1 system",
        max_tokens=300,
    )
    assert isinstance(stage1, AdaptationDecision)
    assert stage1.confidence == 0.84

    stage2 = azure.generate_structured(
        PolicyPatchSchema,
        prompt="stage-2 prompt",
        system_prompt="stage-2 system",
        max_tokens=300,
    )
    assert isinstance(stage2, PolicyPatchSchema)
    assert stage2.provider_weights == {"G1": 0.15, "G2": 0.85}
    assert stage2.max_retry == 3

    assert fake_model.calls == [
        ("AdaptationDecision", "function_calling"),
        ("PolicyPatchSchema", "function_calling"),
    ]

    print("[ok] Azure adapter structured parsing path works offline")


async def test_two_stage_loop_components() -> None:
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=True) as tmp:
        tmp.write(
            """
{
  "provider_priority": ["G1", "G2"],
  "provider_weights": {"G1": 0.5, "G2": 0.5},
  "weight_learning_rate": 0.1,
  "max_retry": 3,
  "retryable_statuses": ["SOFT_DECLINE", "TIMEOUT"],
  "base_backoff_ms": 100,
  "backoff_multiplier": 2.0,
  "retry_budget_window_ms": 60000,
  "max_retries_per_window": 200
}
            """.strip()
        )
        tmp.flush()

        loop = AdaptationLoop(
            llm=MockLLM(),
            aggregator=cast(Any, FakeAggregator()),
            policy_store=PolicyStore(tmp.name),
            verifier=InvariantVerifier(),
        )

        state = AdaptationState(context=_make_context(), objective="cure")

        state = await loop._reason_and_propose(state)
        assert state.status == "running"
        assert state.decision is not None
        assert state.decision.expected_improvement

        state = await loop._propose_theta(state)
        assert state.status == "running"
        assert state.proposed_theta is not None
        assert state.proposed_theta.provider_weights == {"G1": 0.1, "G2": 0.9}

        state = await loop._verify_invariants(state)
        assert state.verification_pass is True

        state = await loop._deploy_policy(state)
        persisted = PolicyStore(tmp.name).current
        assert persisted.provider_weights == {"G1": 0.1, "G2": 0.9}

        print("[ok] Two-stage adaptation components work with MockLLM")


async def test_engine_failure_backoff() -> None:
    agg = FakeAggregator()
    engine = KernelEngine(
        aggregator=cast(Any, agg),
        adaptation_loop=cast(Any, AlwaysFailLoop()),
        check_interval_s=2.0,
        cooldown_s=30.0,
        max_cycles_cooldown_s=60.0,
        failure_backoff_s=20.0,
    )

    snapshot, delta = agg.get_snapshot()
    await engine._run_adaptation("cure", cast(Any, snapshot), cast(Any, delta))

    should_trigger = engine._should_trigger_cure(cast(Any, snapshot))
    assert should_trigger is False

    print("[ok] Engine failure backoff blocks immediate re-trigger")


async def main() -> None:
    test_mock_llm_structured_outputs()
    test_azure_structured_outputs_offline_monkeypatch()
    await test_two_stage_loop_components()
    await test_engine_failure_backoff()
    print("\nAll adaptation component tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
