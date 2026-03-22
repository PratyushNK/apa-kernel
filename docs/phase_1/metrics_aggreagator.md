## Metrics Aggregator — Decided Working

**What it is:** A passive, stateless computation unit. Not a server, not a running process. Called on demand by the Adaptation Scheduler.

**What it owns:**
- JSONL log reader
- Window computation logic
- Previous snapshot (one stored object for delta)

**What it computes per call:**
```
Input:  current_clock_ms, window_size_ms
Output: (MetricsSnapshot_t, MetricsSnapshot_{t-1})
```

**MetricsSnapshot contains:**

Transaction metrics:
```
approval_rate
retry_distribution
average_attempts_per_txn
```

Gateway metrics (system-wide AND per-provider breakdown):
```
rolling_success_rate        ← per provider + overall
cost_per_successful_txn     ← per provider + overall
p95_latency                 ← per provider + overall
timeout_rate                ← per provider + overall
```

System metrics:
```
circuit_open_rate
sla_breach_rate
average_decision_latency
retry_amplification_factor
```

Enrichment fields (beyond the 11):
```
gateway_regimes             ← current regime per provider
invariant_risk              ← pre-computed breach signals per invariant
window_txn_count            ← how many txns the window covered (statistical confidence)
window_start_ms
window_end_ms
```

**Lifecycle:** Instantiated once at simulation start. Holds reference to JSONL path and previous snapshot. Called by Adaptation Scheduler, returns snapshot pair, stores current as new previous.

---

## Adaptation Scheduler — Decided Working

**What it is:** The active control unit. Owns the adaptation schedule and threshold definitions. Calls Aggregator, reasons over snapshot, proposes new θ.

**What it owns:**
- Adaptation trigger logic (interval + breach detection)
- Threshold definitions per metric
- Invariant risk evaluation
- Agent call (LLM)
- PolicyStore writer

**Trigger conditions (any one sufficient):**
```
1. Fixed simulated time interval elapsed
2. Fixed transaction count elapsed  
3. Any metric breaches its threshold in current snapshot
```

**Lifecycle per adaptation cycle:**
```
1. Check trigger conditions
2. Call Aggregator.compute_snapshot()
3. Receive (Snapshot_t, Snapshot_{t-1})
4. Enrich with gateway_states from GatewayModel
5. Construct agent input:
       Snapshot_t
       Snapshot_{t-1}
       θ_t
       gateway_states
       invariant_risk
6. Call LLM agent → proposed θ_{t+1}
7. Validate θ_{t+1} against TLA+ invariants
8. If valid → push to PolicyEngine via update_theta()
9. If invalid → reject, log reason, keep θ_t
10. Record adaptation event (for audit trail)
```

**What it does NOT own:**
- Metrics computation — that's Aggregator
- Policy storage — that's PolicyStore
- Invariant formal verification — that's TLA+ verification module
- Gateway state — that's GatewayModel

**Lifecycle:** Runs alongside simulator. Checks trigger conditions every tick or on a separate async loop.

---

## Ownership Summary

```
Aggregator          owns: window computation, snapshot storage, JSONL reading
Adaptation Scheduler owns: trigger logic, thresholds, agent call, θ validation, audit log
PolicyStore         owns: θ persistence, θ history
GatewayModel        owns: regime state (read by Adaptation Scheduler)
TLA+ module         owns: formal invariant verification
```
