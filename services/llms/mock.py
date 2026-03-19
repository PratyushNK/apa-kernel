class MockLLM:
    def generate_structured(self, schema, prompt, system_prompt=None, max_tokens=500):
        return schema(
            reasoning           = "G1 is down, routing to G2",
            proposed_theta      = {
                "provider_priority"      : ["G1", "G2"],
                "provider_weights"       : {"G1": 0.1, "G2": 0.9},
                "weight_learning_rate"   : 0.1,
                "max_retry"              : 3,
                "retryable_statuses"     : ["SOFT_DECLINE", "TIMEOUT"],
                "base_backoff_ms"        : 100,
                "backoff_multiplier"     : 2.0,
                "retry_budget_window_ms" : 60000,
                "max_retries_per_window" : 200,
            },
            confidence          = 0.9,
            expected_improvement= "Route away from G1",
        )
    
    def generate(self, prompt, system_prompt=None, max_tokens=4000) -> str:
        return "mock response"

    def chat(self, message) -> str:
        return "mock response"