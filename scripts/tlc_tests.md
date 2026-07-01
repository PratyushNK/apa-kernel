TLC-only verification experiments — unsafe proposals & timing
===========================================================

This document summarizes the TLC-only experiments used to (1) validate that the model checker detects deliberately unsafe proposals that violate core invariants, and (2) measure wall-clock cost of authoritative TLC verification on a small set of valid proposals. The tests were executed with a micro-spec approach (single-state TLA modules) so that TLC is the sole verifier and each invocation completes quickly.

Methodology
-----------
Each test case is compiled into a tiny TLA module that encodes a single initial state and the invariants from the policy verification harness. The transition relation is replaced by `Next == UNCHANGED vars` so TLC evaluates invariants on the provided initial state only; this ensures any invariant violations are immediate and unambiguous in the TLC output.

Runtime settings used for all runs

- `VERIFIER_TLC_FAST_MODE=1`
- `VERIFIER_TLC_MAX_STEPS=512`
- `VERIFIER_TLC_NO_FALLBACK=1` (TLC-only; no Python fallback)
- `MICRO_TLC_TIMEOUT=1.5` (per-run wall clock timeout, seconds)
- `VERIFIER_TLC_XMX=1g` (JVM heap for TLC)
- `-workers 1` (single-worker TLC runs)

All runs used `kernel/verification/tla_specs/tla2tools.jar` and wrote outputs (generated .tla/.cfg and TLC metadirs) under `kernel/verification/tla_specs` and its `states/` subdirectory. Results and TCL stdout snippets were written to `scripts/tlc_results_results.json`.

Unsafe-proposal injection experiment (5 cases)
---------------------------------------------
Purpose: verify that TLC independently flags proposals constructed to violate one of the invariants. Each case was encoded as a single initial state expected to violate the named invariant.

- I1_single_settlement — invariant `I1_SingleSettlement` violated by initial state. TLC: violation reported. Elapsed = 0.9426 s.
- I2_retry_bound — invariant `I2_RetryBound` violated (attempt_count > MaxRetry). TLC: violation reported. Elapsed = 0.8074 s.
- I4_circuit_respect — invariant `I4_CircuitRespect` violated (routed to a DOWN provider). TLC: violation reported. Elapsed = 0.8103 s.
- I5_weight_domain — invariant `I5_WeightDomainValid` violated (unknown provider key in weights). TLC: violation reported. Elapsed = 0.3929 s.
- weight_sum — invariant `ProviderWeightsSumOk` violated (weights do not sum to 100). TLC: violation reported. Elapsed = 0.2890 s.

All 5 injected unsafe proposals were detected by TLC (5/5).

Timing experiment (10 valid proposals)
------------------------------------
Ten valid proposals of increasing syntactic complexity (single-parameter changes up to five-parameter variants) were verified with the same micro-spec harness. Results:

- Pass count: 10/10 (no invariants violated).
- Per-run elapsed times: range 0.7810 s → 0.8518 s; mean ≈ 0.8104 s; total for timing suite ≈ 8.1040 s.

Aggregate runtime
-----------------
- Unsafe-suite total ≈ 3.2422 s.
- Timing-suite total ≈ 8.1040 s.
- Overall elapsed ≈ 11.3462 s for all 15 TLC invocations under the listed fast-mode bounds.

Interpretation and limitations
------------------------------
The micro-spec approach provides a fast, authoritative check using TLC alone. It is ideal for short, reproducible experiments where invariant violations can be encoded as initial-state predicates. It does not exercise the full transition space; for end-to-end temporal exploration the original multi-step models should be used (with larger `MaxSteps` and longer timeouts), at the cost of higher runtime and potential TLC internal errors (which can be mitigated by JVM tuning, worker adjustments, or simplifying expressions).

Reproducibility
---------------
To reproduce these exact results on a machine with Java installed and `tla2tools.jar` present at `kernel/verification/tla_specs/tla2tools.jar`:

```bash
MICRO_TLC_TIMEOUT=1.5 VERIFIER_TLC_XMX=1g python3 -u scripts/tlc_results_test.py
```

The full machine-readable output is in `scripts/tlc_results_results.json` and the generated TLA modules and `states/` directories are available under `kernel/verification/tla_specs`.

If you would like a LaTeX-ready table or CSV for the paper, I can generate that from the JSON and include it in the repository.

Run metadata (per-case details)
-------------------------------
The micro-spec approach intentionally constrains the model to a single initial state (the `Init` predicate) and a no-op transition (`Next == UNCHANGED vars`). As a result, the following exploration metadata applies to each TLC invocation in these experiments and is reported in the JSON results alongside the elapsed times.

- Reasoning about counts: because `Next` does not produce new successor states, TLC computes exactly one initial state and either (a) immediately reports an invariant violation at depth 0 (the initial state) or (b) validates the invariant on that single state and completes. Therefore the observable exploration metrics are deterministic and lightweight.

Unsafe cases (violation metadata)

- I1_single_settlement
	- Elapsed: 0.9426 s
	- Violation found at depth: 0 (initial state)
	- Initial states computed: 1
	- States generated: 1
	- Distinct states: 1
	- States left on queue when terminated: 0

- I2_retry_bound
	- Elapsed: 0.8074 s
	- Violation found at depth: 0 (initial state)
	- Initial states computed: 1
	- States generated: 1
	- Distinct states: 1
	- States left on queue when terminated: 0

- I4_circuit_respect
	- Elapsed: 0.8103 s
	- Violation found at depth: 0 (initial state)
	- Initial states computed: 1
	- States generated: 1
	- Distinct states: 1
	- States left on queue when terminated: 0

- I5_weight_domain
	- Elapsed: 0.3929 s
	- Violation found at depth: 0 (initial state)
	- Initial states computed: 1
	- States generated: 1
	- Distinct states: 1
	- States left on queue when terminated: 0

- weight_sum
	- Elapsed: 0.2890 s
	- Violation found at depth: 0 (initial state)
	- Initial states computed: 1
	- States generated: 1
	- Distinct states: 1
	- States left on queue when terminated: 0

Timing cases (exploration metadata)

For all 10 timing proposals (valid configurations) TLC completed successfully on the single initial state. The recorded elapsed seconds per case are available in `scripts/tlc_results_results.json`; representative per-case elapsed times (seconds) were: 0.7810, 0.8152, 0.7985, 0.7983, 0.8206, 0.8139, 0.8139, 0.8023, 0.8085, 0.8518.

Common exploration metrics for these timing cases (per-invocation):

- Violation depth: N/A (no violations)
- Initial states computed: 1
- States generated: 1
- Distinct states: 1
- States left on queue at completion: 0

Implication for paper
---------------------
Including these metadata values in the paper clarifies that these measurements reflect authoritative, per-state checks by TLC rather than deep temporal exploration. The micro-spec design makes per-run wall clock times repeatable and comparable across proposals; larger-scale temporal verification would naturally increase the state counts and require different experimental settings (larger `MaxSteps`, extended timeouts, and potentially JVM tuning).
