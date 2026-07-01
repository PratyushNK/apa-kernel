import importlib
from pathlib import Path

verify = importlib.import_module("kernel.verification.verify")


def test_policy_params_variants():
    p_base = verify.PolicyParams.base()
    p_aggr = verify.PolicyParams.adapted_aggressive()
    p_cons = verify.PolicyParams.adapted_conservative()
    assert isinstance(p_base, verify.PolicyParams)
    assert p_aggr.max_retry > p_base.max_retry
    assert p_cons.max_retry <= p_base.max_retry


def test_invariant_checks_direct():
    params = verify.PolicyParams.base()
    checker = verify.InvariantChecker(params)
    providers = params.provider_priority
    all_up = {p: True for p in providers}

    # I2 failure: attempt_count > max_retry
    s_over = verify.TxnState("PENDING", params.max_retry + 1, "SUCCESS", "", all_up, 0, {})
    assert checker.check_i2(s_over).status == verify.VerificationStatus.FAIL

    # I3 failure: terminal state with attempt > max
    s_term = verify.TxnState("SUCCESS", params.max_retry + 1, "SUCCESS", "", all_up, 0, {})
    assert checker.check_i3(s_term).status == verify.VerificationStatus.FAIL

    # I4 failure: routed to DOWN provider
    bad_up = {p: True for p in providers}
    bad_up[providers[0]] = False
    s_down = verify.TxnState("PENDING", 1, params.retryable_statuses[0], providers[0], bad_up, 0, {})
    assert checker.check_i4(s_down).status == verify.VerificationStatus.FAIL

    # I5 failure: unknown provider in weights
    p_bad = verify.PolicyParams(provider_priority=providers, provider_weights={providers[0]: 0.5, "X": 0.5})
    checker_bad = verify.InvariantChecker(p_bad)
    assert checker_bad.check_i5(verify.TxnState.fresh(providers)).status == verify.VerificationStatus.FAIL

    # Type invariant failure: negative attempt_count
    s_badtype = verify.TxnState("PENDING", -1, "SUCCESS", "", all_up, 0, {})
    assert checker.check_type_invariant(s_badtype).status == verify.VerificationStatus.FAIL


def test_reachable_states_contains_invariants():
    params = verify.PolicyParams.base()
    checker = verify.InvariantChecker(params)
    invs = checker.run_all()
    names = {i.name for i in invs}
    expected = {"TypeInvariant", "I2_RetryBound", "I3_TerminalAbsorption", "I4_CircuitRespect", "I5_WeightDomainValid"}
    assert expected.issubset(names)
    for inv in invs:
        assert inv.status in verify.VerificationStatus


def test_tlcconfig_generate_writes_files(tmp_path):
    # copy template into tmp_path
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "kernel" / "verification" / "tla_specs" / "TB_template.tla"
    assert src.exists(), f"Template not found at {src}"
    (tmp_path / "TB_template.tla").write_text(src.read_text())

    cfg = verify.TLCConfig(tmp_path)
    params = verify.PolicyParams.base()
        tla_path, cfg_path, cfg_fair = cfg.generate("unittest", params)
    assert tla_path.exists()
    assert cfg_path.exists()
        assert cfg_fair.exists()
    content = tla_path.read_text()
    assert f"MaxRetry            == {params.max_retry}" in content


def test_sany_and_tlc_runner_unavailable(tmp_path):
    # SANY without jar should be reported as skipped
    s = verify.SANYChecker(None)
    assert not s.available()
    dummy = tmp_path / "d.tla"
    dummy.write_text("---- MODULE X ----\n====")
    ok, out = s.check(dummy)
    assert ok and "SANY skipped" in out

    # TLC runner without jar reports not available
    t = verify.TLCRunner(None)
    assert not t.available()
    ok2, out2 = t.run(dummy, tmp_path / "x.cfg")
    assert ok2 is False and "TLC not available" in out2


def test_policy_verifier_verify_base_no_jar():
    pv = verify.PolicyVerifier(jar_path=None)
    res = pv.verify_base()
    assert res.suite_name == "base"
    assert isinstance(res.invariants, list)
