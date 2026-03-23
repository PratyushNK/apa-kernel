import os
from kernel.verification import verifier
import kernel.verification.verify as verify_mod


def test_tlc_pass():
    # Fake TLCRunner that reports available and a successful run
    class FakeTLCRunner:
        def __init__(self, jar_path, workers=2):
            self.jar = jar_path

        def available(self):
            return True

        def run(self, tla_path, cfg_path):
            return True, "Model checking completed. No error has been found."

    orig = getattr(verify_mod, "TLCRunner", None)
    verify_mod.TLCRunner = FakeTLCRunner

    theta = {
        "provider_priority": ["G1", "G2"],
        "provider_weights": {"G1": 0.5, "G2": 0.5},
        "max_retry": 3,
        "max_retries_per_window": 200,
    }

    try:
        v = verifier.InvariantVerifier()
        ok, violations = v.check(theta)
    finally:
        if orig is None:
            delattr(verify_mod, "TLCRunner")
        else:
            verify_mod.TLCRunner = orig
    assert ok is True
    assert violations == []


def test_tlc_fail_then_fallback():
    # Fake TLCRunner that reports available but a failing run
    class FakeTLCRunnerFail:
        def __init__(self, jar_path, workers=2):
            self.jar = jar_path

        def available(self):
            return True

        def run(self, tla_path, cfg_path):
            return False, "Invariant I5 violated"

    orig = getattr(verify_mod, "TLCRunner", None)
    verify_mod.TLCRunner = FakeTLCRunnerFail

    # Provide a theta that will also fail the Python provider_weights check
    theta = {
        "provider_priority": ["G1", "G2"],
        "provider_weights": {"G1": 0.5, "X": 0.5},
        "max_retry": 3,
        "max_retries_per_window": 200,
    }

    try:
        v = verifier.InvariantVerifier()
        ok, violations = v.check(theta)
    finally:
        if orig is None:
            delattr(verify_mod, "TLCRunner")
        else:
            verify_mod.TLCRunner = orig
    assert ok is False
    # TLC ran and reported a counterexample; the adapter should surface TLC result
    assert any("TLC counterexample" in s for s in violations)


def test_disable_tlc_env():
    # Ensure env var skips TLC even if TLCRunner would be available
    os.environ["VERIFIER_DISABLE_TLC"] = "1"

    class FakeTLCRunnerBad:
        def __init__(self, jar_path, workers=2):
            raise RuntimeError("Should not be instantiated")

    orig = getattr(verify_mod, "TLCRunner", None)
    verify_mod.TLCRunner = FakeTLCRunnerBad

    theta = {
        "provider_priority": ["G1", "G2"],
        "provider_weights": {"G1": 0.3, "G2": 0.2},  # sums to 0.5 -> fail
        "max_retry": 3,
        "max_retries_per_window": 200,
    }
    try:
        v = verifier.InvariantVerifier()
        ok, violations = v.check(theta)
        assert ok is False
        assert any("provider_weights" in s for s in violations)
    finally:
        del os.environ["VERIFIER_DISABLE_TLC"]
        if orig is None:
            delattr(verify_mod, "TLCRunner")
        else:
            verify_mod.TLCRunner = orig
