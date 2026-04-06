#!/usr/bin/env python3
"""
scripts/test_gap_fixes.py

Unit tests validating gap fixes:
 - TLA `.cfg` generation includes `PROPERTY` entries
 - TLC run passes for base params and finds counterexample when I1 violated
 - InvariantVerifier Python fallback detects I1 violation
 - P6 `timeout_ms` wiring is applied to GatewayModel configs
 - No adapter TLA files are generated when TLC is unavailable

Run with:
  PYTHONPATH=. .venv/bin/python scripts/test_gap_fixes.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
import tempfile
import shutil
import os
import json
import glob

from kernel.verification.verify import TLCConfig, TLCRunner, PolicyParams
from kernel.verification.verifier import InvariantVerifier
from simulator.gateway_model import GatewayModel, ProviderConfig
from simulator.policy_engine import PolicyStore, PolicyEngine


class GapFixesTest(unittest.TestCase):

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.specs_root = self.repo_root / 'kernel' / 'verification' / 'tla_specs'
        self.jar_path = self.specs_root / 'tla2tools.jar'
        self.template = self.specs_root / 'TB_template.tla'

    def _copy_template(self, tmpdir: Path) -> None:
        dst = tmpdir / 'TB_template.tla'
        shutil.copy(self.template, dst)

    def test_cfg_and_property_generation(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._copy_template(tmp)
            cfggen = TLCConfig(tmp)
            tla_path, cfg_path, cfg_fair = cfggen.generate('unit_test', PolicyParams.base())
            self.assertTrue(tla_path.exists())
            self.assertTrue(cfg_path.exists())
            self.assertTrue(cfg_fair.exists())
            cfg_text = cfg_path.read_text(encoding='utf-8')
            self.assertIn('INVARIANT I1_SingleSettlement', cfg_text)
            fair_text = cfg_fair.read_text(encoding='utf-8')
            self.assertIn('PROPERTY I1_SingleSettlementProp', fair_text)

    def test_tlc_pass_and_detects_counterexample(self):
        if not self.jar_path.exists():
            self.skipTest('tla2tools.jar not available; skipping TLC integration tests')

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._copy_template(tmp)
            cfggen = TLCConfig(tmp)

            # Good params -> TLC should pass
            tla1, cfg1, fair1 = cfggen.generate('unit_base', PolicyParams.base())
            tlc = TLCRunner(self.jar_path, workers=1)
            ok, out = tlc.run(tla1, cfg1)
            self.assertTrue(ok, f'TLC should pass for base params; output: {out[:200]}')

            # Create a modified TLA to intentionally make AttemptEnabled unconstrained
            bad = PolicyParams()
            bad.retryable_statuses = ['SUCCESS', 'TIMEOUT']
            tla2, cfg2, fair2 = cfggen.generate('unit_bad', bad)
            # Edit the generated TLA so AttemptEnabled is always TRUE (to force a counterexample)
            txt = tla2.read_text(encoding='utf-8')
            idx = txt.find('AttemptEnabled ==')
            if idx != -1:
                # find end of block (next blank line) and replace block with an unconstrained definition
                end_idx = txt.find('\n\n', idx)
                if end_idx == -1:
                    # fallback to next top-level token
                    end_idx = txt.find('\nRouteAction', idx)
                    if end_idx == -1:
                        end_idx = idx + 200
                new_block = 'AttemptEnabled ==\n    \\ TRUE\n\n'
                txt = txt[:idx] + new_block + txt[end_idx+2:]
                tla2.write_text(txt, encoding='utf-8')
            ok2, out2 = tlc.run(tla2, cfg2)
            self.assertFalse(ok2, 'TLC should find a counterexample when AttemptEnabled is unconstrained')
            self.assertTrue(('violated' in out2.lower()) or ('counterexample' in out2.lower()) or ('error' in out2.lower()),
                            f'Expected counterexample text; got: {out2[:200]}')

    def test_invariant_verifier_python_fallback_detects_I1(self):
        # Force Python fallback
        os.environ['VERIFIER_DISABLE_TLC'] = '1'
        try:
            verifier = InvariantVerifier()
            theta = {
                'max_retry': 3,
                'retryable_statuses': ['SUCCESS', 'TIMEOUT'],
                'provider_priority': ['G1', 'G2'],
                'provider_weights': {'G1': 0.5, 'G2': 0.5},
                'weight_learning_rate': 0.1,
                'base_backoff_ms': 100,
                'backoff_multiplier': 2.0,
                'retry_budget_window_ms': 60000,
                'max_retries_per_window': 200,
            }
            ok, violations = verifier.check(theta)
            self.assertFalse(ok)
            self.assertTrue(any('i1_single_settlement' in v.lower() or 'success' in v for v in violations))
        finally:
            os.environ.pop('VERIFIER_DISABLE_TLC', None)

    def test_p6_timeout_wiring(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            policy_path = tmp / 'policy.json'
            policy = {
                'provider_priority': ['G1', 'G2'],
                'provider_weights': {'G1': 0.5, 'G2': 0.5},
                'weight_learning_rate': 0.1,
                'max_retry': 3,
                'retryable_statuses': ['SOFT_DECLINE', 'TIMEOUT'],
                'base_backoff_ms': 100,
                'backoff_multiplier': 2.0,
                'retry_budget_window_ms': 60000,
                'max_retries_per_window': 200,
                'timeout_ms': {'G1': 123, 'G2': 456},
            }
            policy_path.write_text(json.dumps(policy), encoding='utf-8')
            providers = [ProviderConfig(name='G1'), ProviderConfig(name='G2')]
            gateway = GatewayModel(providers)
            store = PolicyStore(str(policy_path))
            _ = PolicyEngine(store, gateway)
            for prov, t in policy['timeout_ms'].items():
                cfg = gateway._configs[prov]
                for regime_val in cfg.timeout_ms.values():
                    self.assertEqual(regime_val, t)

    def test_no_adapter_files_generated_when_tlc_unavailable(self):
        pattern = str(self.specs_root / 'TB_verifier_adapter_*.tla')
        before = set(Path(p).name for p in glob.glob(pattern))

        import importlib
        verify_mod = importlib.import_module('kernel.verification.verify')
        orig_avail = verify_mod.TLCRunner.available
        try:
            # force TLCRunner.available() -> False
            verify_mod.TLCRunner.available = lambda self: False
            verifier = InvariantVerifier()
            theta = {
                'max_retry': 3,
                'retryable_statuses': ['TIMEOUT'],
                'provider_priority': ['G1', 'G2'],
                'provider_weights': {'G1': 0.5, 'G2': 0.5},
                'weight_learning_rate': 0.1,
                'base_backoff_ms': 100,
                'backoff_multiplier': 2.0,
                'retry_budget_window_ms': 60000,
                'max_retries_per_window': 200,
            }
            _ok, _viol = verifier.check(theta)
            after = set(Path(p).name for p in glob.glob(pattern))
            self.assertEqual(before, after, 'No new adapter TLA files should be generated when TLC unavailable')
        finally:
            verify_mod.TLCRunner.available = orig_avail


if __name__ == '__main__':
    unittest.main(verbosity=2)
