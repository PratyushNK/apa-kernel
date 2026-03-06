**Minimal Closed Routing & Retry Micro-World** — the smallest system that is:

* Executable
* Formally specifiable
* Invariant-checkable
* Policy-driven
* Metric-observable
* Adaptation-ready

---

# 1️⃣ Minimal Closed Routing & Retry Micro-World

## 🎯 Scope Boundary (Hard Constraints)

We model only:

* **Single merchant**
* **Two gateways** (G1, G2)
* Authorization phase only (no settlement)
* Single transaction currency
* No partial captures
* No refunds
* No chargebacks
* No cross-border FX
* No asynchronous webhook races
* No external fraud ML (only static fraud score input)

Inspired structurally by processors like:

* Stripe
* Adyen

But this is a controlled micro-world.

---

# 2️⃣ State Space Definition (Fully Specified)

We define 3 state domains:

---

## A. Transaction State

Each transaction `T` has:

```
txn_id: UUID
amount: integer
state: {INIT, ATTEMPTING, SUCCESS, FAILED, BLOCKED}
attempt_count: integer
last_error: {NONE, SOFT_DECLINE, HARD_DECLINE, TIMEOUT, NETWORK_ERROR}
active_gateway: {G1, G2, NONE}
```

### Terminal States:

* SUCCESS
* FAILED
* BLOCKED

These are absorbing.

---

## B. Gateway State (Per Gateway)

For G ∈ {G1, G2}:

```
success_rate: float (rolling)
latency_p95: float
failure_rate: float (rolling)
health: {UP, DOWN}
```

Derived, not manually mutated.

---

## C. System State

```
retry_queue_depth: integer
circuit_breaker_G1: {OPEN, CLOSED}
circuit_breaker_G2: {OPEN, CLOSED}
MAX_RETRY: integer (e.g., 3)
```

---

# 3️⃣ Event Model (Bounded Event Set)

Only these events exist:

1. NewTransaction
2. RouteDecision
3. AttemptExecution
4. AttemptResult
5. RetryDecision
6. CircuitEvaluation

Nothing else exists in this world.

This keeps the transition graph finite.

---

# 4️⃣ Minimal Invariant Set (Safety Laws)

We now derive only what is necessary.

### I1 — Single Success Invariant

For any txn_id:

```
Count(state == SUCCESS) ≤ 1
```

---

### I2 — Retry Bound Invariant

```
attempt_count ≤ MAX_RETRY
```

---

### I3 — Terminal Absorption

If state ∈ {SUCCESS, FAILED, BLOCKED}
→ no further AttemptExecution allowed

---

### I4 — Single Active Attempt

If state == ATTEMPTING
→ active_gateway ≠ NONE
→ no concurrent attempt allowed

---

### I5 — Fraud Block Invariant

If fraud_score ≥ FRAUD_THRESHOLD
→ state must transition to BLOCKED
→ no routing allowed

---

### I6 — Circuit Respect

If gateway.health == DOWN
→ RouteDecision cannot select that gateway

---

### I7 — SLA Terminal Invariant

If latency_total > SLA_LIMIT
→ state ∈ {FAILED}
and no further AttemptExecution allowed

---

That’s it.

Seven invariants.
Closed.
Executable.

---

# 5️⃣ Minimal Policy Set (Decision Logic)

Policies operate within invariant bounds.

---

## P1 — Routing Policy

If both gateways UP:

Choose gateway with higher success_rate.

If one DOWN:

Choose the UP one.

If both DOWN:

Mark FAILED.

---

## P2 — Retry Eligibility Policy

Retry allowed only if:

* last_error ∈ {SOFT_DECLINE, TIMEOUT, NETWORK_ERROR}
* attempt_count < MAX_RETRY
* fraud_score < FRAUD_THRESHOLD

Else:

* HARD_DECLINE → FAILED
* fraud_score high → BLOCKED

---

## P3 — Backoff Policy

Delay = base_delay × 2^(attempt_count - 1)

(No concurrency modeling yet; delay simulated as step counter.)

---

## P4 — Circuit Breaker Policy

If gateway.failure_rate > FAILURE_THRESHOLD:
→ health = DOWN

If recovery window passes:
→ health = UP

---

That’s all.

No optimization yet.
No learning yet.

Just coherent behavior.

---

# 6️⃣ Monitorable Metrics (Derived Observables)

Metrics must serve:

* Policy inputs
* Invariant verification
* Adaptation hooks

---

## Transaction Metrics

* approval_rate
* retry_distribution
* average_attempts_per_txn

---

## Gateway Metrics

* rolling_success_rate
* cost_per_successful_txn
* p95_latency
* timeout_rate

---

## System Metrics

* circuit_open_rate
* sla_breach_rate
* average_decision_latency
* retry_amplification_factor

---

Each metric maps to:

| Metric        | Drives   | Protects              |
| ------------- | -------- | --------------------- |
| success_rate  | Routing  | Approval objective    |
| failure_rate  | Circuit  | Circuit invariant     |
| attempt_count | Retry    | Retry bound invariant |
| fraud_score   | Blocking | Fraud invariant       |

Nothing is unconnected.

---

# 7️⃣ Objective Function (Why Policies Exist)

We now define the system goal:

Maximize:

```
Expected_Approval_Rate
- λ1 * Retry_Cost
- λ2 * Latency
```

Subject to:

All invariants holding true.

This gives adaptation direction later.

---

# 8️⃣ State Transition Graph (Conceptual)

```
INIT
  ↓
RouteDecision
  ↓
ATTEMPTING
  ↓
AttemptResult
  ├── SUCCESS → SUCCESS (terminal)
  ├── HARD_DECLINE → FAILED (terminal)
  ├── FRAUD → BLOCKED (terminal)
  └── SOFT/TIMEOUT →
          RetryDecision
              ├── attempt_count < MAX → RouteDecision
              └── else → FAILED
```

Closed.
Finite.
Verifiable.

---

# 9️⃣ Why This Is Simulation-Ready

Because:

* State variables are explicit
* Events are finite
* Invariants are boolean predicates
* Policies are deterministic functions
* Metrics are derivable from state history

You can now:

1. Generate synthetic transactions
2. Inject random outcomes
3. Run event loop
4. Assert invariants after every transition
5. Log metrics

That is executable backend territory.

---

# 🔟 Why This Is the Correct Starting Point

Because:

* It is minimal but not trivial
* It contains routing + retry interplay
* It contains bounded adaptation surface
* It is formally specifiable (even in TLA+)
* It scales cleanly by adding dimensions later

If this micro-world works,
the macro-world is an extension problem.

If this fails,
macro complexity would have hidden the flaw.

---

# What We Do Next

We now choose one of three paths:

1. Convert this into formal state machine spec (precursor to TLA+)
2. Define synthetic dataset generator design
3. Architect backend module boundaries (engine, state store, metrics, policy engine)

Your system is now grounded.
No randomness.
No incoherence.
Just a minimal autonomous routing engine blueprint.
