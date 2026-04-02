import asyncio, logging
from kernel.adaptation.loop import AdaptationLoop
from kernel.adaptation.schemas import AdaptationContext, AdaptationState, PolicyVectorSchema

# Minimal stubs
class DummyLLM: pass
class DummyAgg:
    def get_snapshot(self):
        return None, None
class DummyStore:
    @property
    def current(self):
        return {}
class DummyVerifier:
    def check(self, theta):
        return True, []

# configure logging to capture warnings
logging.basicConfig(level=logging.DEBUG)
loop = AdaptationLoop(DummyLLM(), DummyAgg(), DummyStore(), DummyVerifier())
ctx = AdaptationContext(
    approval_rate=0.9, rolling_success_rate=0.9, retry_amplification=1.0,
    circuit_open_rate=0.0, sla_breach_rate=0.0, timeout_rate=0.0,
    provider_success_rates={}, provider_circuit_states={},
    approval_rate_delta=None, success_rate_delta=None, circuit_open_rate_delta=None, retry_amplification_delta=None,
    invariant_breaches=[], current_theta={}, objective='cure'
)
pv = PolicyVectorSchema(
    provider_priority=["G1","G2"],
    provider_weights={"G1":0.5,"G2":0.5},
    weight_learning_rate=0.1,
    max_retry=3,
    retryable_statuses=["SOFT_DECLINE","TIMEOUT"],
    base_backoff_ms=100,
    backoff_multiplier=2.0,
    retry_budget_window_ms=60000,
    max_retries_per_window=200,
    timeout_ms={}
)
state = AdaptationState(context=ctx, proposed_theta=pv)

async def run_test():
    s = await loop._verify_invariants(state)
    print('verification_pass:', s.verification_pass, 'violations:', s.violations)

if __name__ == '__main__':
    asyncio.run(run_test())
