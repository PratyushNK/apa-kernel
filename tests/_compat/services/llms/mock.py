import os
import random
import time


class MockLLM:
    """Compatibility shim: a non-static mock LLM used only for test runs.

    This shim returns diverse, randomized but valid structured outputs so the
    adaptation loop exhibits realistic variability without changing kernel
    source files. Control with env vars:
      - MOCK_LLM_SEED: integer seed for reproducibility
      - MOCK_DECISION_CONF_MIN/MAX: float range for decision confidence
    """

    def __init__(self):
        seed = os.getenv("MOCK_LLM_SEED")
        if seed is not None:
            try:
                random.seed(int(seed))
            except Exception:
                pass

    def _rand_conf(self, low=0.3, high=0.95):
        try:
            low = float(os.getenv("MOCK_DECISION_CONF_MIN", low))
            high = float(os.getenv("MOCK_DECISION_CONF_MAX", high))
        except Exception:
            low, high = 0.3, 0.95
        return max(0.0, min(1.0, random.uniform(low, high)))

    def generate_structured(self, schema, prompt, system_prompt=None, max_tokens=500):
        name = getattr(schema, "__name__", "")

        if name == "AdaptationDecision":
            conf = self._rand_conf()
            reasoning = f"auto-detected (mock) — conf={conf:.2f}"
            return schema(
                reasoning=reasoning,
                confidence=conf,
                expected_improvement="improve approval and reduce circuit opens",
            )

        # PolicyVectorSchema or PolicyPatchSchema
        if name in ("PolicyVectorSchema", "PolicyPatchSchema"):
            # two providers fixed for tests: G1, G2
            w1 = random.uniform(0.0, 1.0)
            w2 = random.uniform(0.0, 1.0)
            s = w1 + w2 if (w1 + w2) > 0 else 1.0
            weights = {"G1": round(w1 / s, 3), "G2": round(w2 / s, 3)}
            # ensure sum to 1.0 (adjust last)
            weights["G2"] = round(1.0 - weights["G1"], 3)
            lr = round(random.uniform(0.01, 0.3), 3)
            max_retry = random.randint(1, 5)
            base_backoff_ms = random.choice([50, 100, 200, 500])
            backoff_multiplier = round(random.uniform(1.0, 3.0), 2)
            retry_budget_window_ms = random.choice([10000, 30000, 60000])
            max_retries_per_window = random.choice([10, 50, 200])
            timeout_ms = {"G1": random.choice([200, 300, 500]), "G2": random.choice([200, 300, 500])}

            # construct a full PolicyVectorSchema instance (pydantic will validate)
            return schema(
                provider_priority=["G1", "G2"],
                provider_weights=weights,
                weight_learning_rate=lr,
                max_retry=max_retry,
                retryable_statuses=["SOFT_DECLINE", "TIMEOUT"],
                base_backoff_ms=base_backoff_ms,
                backoff_multiplier=backoff_multiplier,
                retry_budget_window_ms=retry_budget_window_ms,
                max_retries_per_window=max_retries_per_window,
                timeout_ms=timeout_ms,
            )

        # Fallback: attempt to return a minimal instance
        try:
            return schema()
        except Exception:
            # give a safe dict if schema instantiation fails
            return {}

    def generate(self, prompt, system_prompt=None, max_tokens=4000) -> str:
        # Slightly varied textual reply to emulate model diversity
        tokens = len(prompt.split())
        out = f"mock response (len={tokens})"
        # small delay to emulate model latency
        time.sleep(random.uniform(0.01, 0.05))
        return out

    def chat(self, message) -> str:
        return "mock chat response"
