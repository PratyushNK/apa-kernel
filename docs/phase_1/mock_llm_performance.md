Completed: *Analyze adaptation triggers and logs* (4/6)

**Summary — what the test computes now**
- Computed (present in latest aggregated JSON): SLA rate, p50/p95/p99 latencies, total txns/attempts, retry amplification, avg cost/attempt, circuit open count, e2e recovery time, deployments, adaptation flapping rate.
- Newly added component metrics (from engine logs): LLM proposals/accepted, acceptance rate, proposal effectiveness (approval Δ), counts/sums for per-adaptation improvements, TLC counts (TP/FP/FN/TN), and recovery confirmation precision/recall.

**Key numbers (latest aggregated over 8 runs)**
- SLA rate (mean): 0.9731 (≈97.3%)
- p95 (ms): 182.125; p50: 91.25; p99: 327.5
- Retry amplification (mean): 1.045
- Avg cost / attempt: 0.24997
- Circuit opens (sum): 3
- Deployments (sum): 5
- E2E recovery time (mean): 5.80 s
- Adaptation flapping rate (mean): 0.0101
- LLM proposals: 9; accepted: 9; acceptance rate: 1.0
- Proposal effectiveness (approval Δ mean): +0.0214 (≈ +2.14 percentage points)
- Improvements recorded: 8 (sum Δ ≈ 0.171)
- TLC counts: TP=0, FP=0, FN=9, TN=0 → TLC TPR = 0.0, FPR undefined (no negatives)
- Recovery confirmations: confirmed=4; true recoveries=7; confirmed∧true=3 → precision=0.75, recall≈0.429

**Interpretation / insights**
- System-level health: Monitoring→Adaptation loop maintains high SLA (~97%) with low retry amplification and modest latency — the loop is effective at preserving primary objectives.
- Adaptation behavior: Adaptations occur (5 deployments over 8 runs); LLM proposals are always accepted by the pipeline (MockLLM + verifier path), and accepted proposals produce small positive approval improvements on average (~+2%).
- Recovery performance: When a circuit open occurs, mean end-to-end recovery (as instrumented) is fast (~6 s) in this run set — verification+deploy+observation latency is low.
- Verification (TLC) coverage problem: The parser shows all 9 verification events recorded as TLC false negatives (FN) and zero TP/FP/TN — this indicates our instrumentation/heuristics failed to capture TLC outputs correctly (not that TLC is necessarily broken). As a result, TLC detection metrics are unreliable.
- Recovery confirmation quality: Precision 0.75 (most confirmed recoveries corresponded to real positive improvement), but recall ≈ 0.43 (the system missed many true recoveries when confirming). This suggests the “recovery confirmed” log is conservative/undersensitive or our pairing logic missed matches.
- Test reliability notes:
  - Good: runs are isolated (policy/gateway/events reset), events are tailed, engine logs captured to last_run.log.
  - Fragile: current instrumentation relies on file mtimes, plain-text log parsing, and ad-hoc parsing regexes — this is brittle and can misclassify events (explains TLC counts).

**Limitations / correctness risks**
- Percentile calculation is fragile (index math can be wrong for small N); use statistics.quantiles/numpy.percentile instead.
- SLA computation compares simulation timestamps to stored deadlines — check units/clock source alignment.
- Using policy file mtime as “deploy” proxy is coarse; race conditions may cause missed/duplicate counts.
- Log parsing heuristics (string contains, regex) depend on exact log text and can miss structured verifier outputs (TLC).
- No unique proposal IDs or structured adaptation trace recorded, so linking proposal → verification → deploy → observe is heuristic.

**Concrete next steps I recommend (pick 1)**
- Short (fast): I can run a log analysis pass to produce a per-deployment table (timestamp, pre/post approval, TLC result) from the last runs — gives concrete examples of proposal effectiveness. (I can do this now.)
- Medium (recommended): Instrument the code to emit structured adaptation traces (write a JSONL `data/streams/adaptations.jsonl` with proposal_id, timestamps, verifier_result, deployed_bool, observed_snapshot) and switch percentiles/SLA calc to robust methods — then re-run N=8 for reliable metrics (will remove log-parsing fragility).
- Long: Add labeled ground-truth runs (inject known violations) to compute true TLC ROC/PR curves.

Which next step do you want? If you pick the Medium option, I’ll patch minimal structured tracing in loop.py (emit JSONL records at propose/verify/deploy/observe) and update the test to consume those records.