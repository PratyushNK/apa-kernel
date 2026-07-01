Liveness and recovery properties — notes for the paper
===================================================

Summary
-------
- We added short-term and long-term liveness checks to the TLA model.
- The model-level temporal checks are environment-sensitive: naive `[](... => <>(...))`
  formulas can be violated when the environment (provider up/down) continuously
  interferes with routing.

Formal properties (implemented or proposed)
-------------------------------------------

1. L1_EventualTerminal (implemented)
   - TLA: `[]( txn_status = "PENDING" => <> (txn_status \in {"SUCCESS","FAILED"}) )`
   - Intuition: a pending transaction eventually reaches a terminal state.

2. L1_BoundedAttempts (implemented)
   - TLA (pragmatic): `[]( txn_status = "PENDING" => <> (txn_status \in {"SUCCESS","FAILED"} \/ attempt_count >= MaxRetry) )`
   - Intuition: either the transaction reaches terminality or we exhaust configured attempts.
   - Rationale: easier to check in TLC; uses `MaxRetry` as a policy bound rather than a raw step counter.

3. Why unbounded liveness fails (provider churn)
  - The naive unbounded routing property
    `[]( UpProviders # {} => <> (current_provider # "") )` assumes the environment
    eventually allows routing to make progress. In practice, provider availability
    can churn (down/up) and that churn can indefinitely prevent the routing action
    from observing a stable `UpProviders` set. Even with `WF_vars(RouteAction)`, TLC
    finds counterexamples where the environment toggles before routing occurs.

4. L2_BoundedTerminal (implemented)
  - Rationale: a more honest claim is bounded eventuality relative to configured
    retry/attempt limits. We added a `step_counter` that increments on each
    `AttemptAction` and `RetryAction`, and assert that eventually either the
    system reaches a terminal state or the `step_counter` reaches `MaxRetry`.
  - Exact TLA+ formula (committed):

```tla
terminal_reached == txn_status \in {"SUCCESS", "FAILED"}

L2_BoundedTerminalProp == <>( step_counter >= MaxRetry \/ terminal_reached )
```

  - Intuition: within the policy-configured retry budget, the system either
    succeeds or exhausts retries (a bounded recovery claim). This avoids
    asserting progress under adversarial environment churn.

Fairness and modelling notes
----------------------------
- We added weak-fairness assumptions in the working template for `AttemptAction`, `RetryAction`,
  and `RouteAction` (i.e. `WF_vars(...)`) so TLC explores traces where enabled actions are not
  indefinitely ignored.
- Even with `WF_vars(RouteAction)`, `L2_EventualRoute` can fail: environment actions (provider down)
  can move the model into windows where `UpProviders` is empty before `RouteAction` executes.
- To get stronger guarantees you must either:
  - Strengthen the environment fairness (e.g., limit `ProviderDownAction` being enabled forever), or
  - Use stabilization-scoped liveness (e.g. `L2_StableRoute`), or
  - Introduce explicit step/time counters and assert bounded eventuality relative to those counters.

Encoding bounded eventuality
---------------------------
- TLC / LTL do not provide a built-in bounded-eventually operator; encode bounds via:
  - policy constants (e.g. `MaxRetry`) and predicates derived from them (as above), or
  - an explicit `step_counter` variable that increments on every `Next` and reset on terminal;
    assert `txn_status = "PENDING" => <> (txn_status \in {"SUCCESS","FAILED"} \/ step_counter - t0 <= K)`.

Recommended use in experiments
------------------------------
- For empirical experiments and CI, prefer `L1_BoundedAttempts` + `L2_StableRoute`.
- Keep `L2_EventualRoute` as an aspirational claim in the paper, but report the environment
  assumptions required for it to hold (scoped fairness, limited churn).

Next steps
----------
- (Optional) add `L2_StableRouteProp` to `TB_template.tla` when you decide the stabilization
  semantics and required fairness constraints for the paper.
- (Optional) encode a `step_counter` to express genuine bounded eventuality in TLC.

File references
---------------
- Template: `kernel/verification/tla_specs/TB_template.tla`
- Generator: `kernel/verification/verify.py`
- Tests: `scripts/test_tlc_integration.py`
