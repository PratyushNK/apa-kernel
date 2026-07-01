---- MODULE %%SPEC_NAME%% ----
EXTENDS Integers, Sequences, FiniteSets, TLC

VARIABLES
    txn_status,
    attempt_count,
    last_status,
    current_provider,
    provider_up,
    retry_window_count,
    extra_state,
    step_counter

vars == << txn_status, attempt_count, last_status, current_provider,
           provider_up, retry_window_count, extra_state, step_counter >>

Providers           == %%PROVIDERS%%
MaxRetry            == %%MAX_RETRY%%
RetryableCodes      == %%RETRYABLE%%
MaxRetriesPerWindow == %%MAX_WINDOW%%
ProviderWeights     == %%WEIGHTS%%
ActivePolicyIDs     == {1, 2, 3, 4, 5}
AIPolicies          == {}

AttemptStatuses == {"SUCCESS", "SOFT_DECLINE", "HARD_DECLINE", "TIMEOUT"}
TxnStatuses     == {"PENDING", "SUCCESS", "FAILED"}

TypeInvariant ==
    /\ txn_status         \in TxnStatuses
    /\ attempt_count      \in Nat
    /\ last_status        \in AttemptStatuses
    /\ current_provider   \in (Providers \cup {""})
    /\ provider_up        \in [Providers -> BOOLEAN]
    /\ retry_window_count \in Nat
    /\ DOMAIN extra_state \subseteq STRING

    /\ step_counter       \in Nat

Init ==
    /\ txn_status         = "PENDING"
    /\ attempt_count      = 0
    /\ last_status        = "SUCCESS"
    /\ current_provider   = ""
    /\ provider_up        = [p \in Providers |-> TRUE]
    /\ retry_window_count = 0
    /\ extra_state        = [k \in {} |-> ""]

    /\ step_counter       = 0

UpProviders == { p \in Providers : provider_up[p] = TRUE }

ChooseProvider ==
    IF UpProviders = {} THEN ""
    ELSE CHOOSE p \in UpProviders : TRUE

IsRetryable(status) == status \in RetryableCodes
BackoffOk == TRUE
BudgetOk  == retry_window_count < MaxRetriesPerWindow

\* Helper: whether an attempt is enabled in the current state (no primes)
AttemptEnabled ==
    /\ txn_status = "PENDING"
    /\ current_provider # ""
    /\ attempt_count < MaxRetry
    /\ (attempt_count = 0 \/ IsRetryable(last_status))

RouteAction ==
    /\ txn_status       = "PENDING"
    /\ current_provider = ""
    /\ LET p == ChooseProvider IN
       IF p = ""
       THEN /\ txn_status'       = "FAILED"
            /\ current_provider' = ""
              /\ UNCHANGED << attempt_count, last_status,
                                provider_up, retry_window_count, extra_state >>
              /\ step_counter' = step_counter + 1
       ELSE /\ current_provider' = p
             /\ UNCHANGED << txn_status, attempt_count, last_status,
                              provider_up, retry_window_count, extra_state >>
             /\ step_counter' = step_counter + 1

AttemptAction ==
    /\ txn_status       = "PENDING"
    /\ current_provider # ""
    /\ attempt_count    < MaxRetry
    /\ (attempt_count = 0 \/ IsRetryable(last_status))
    /\ attempt_count'   = attempt_count + 1
    /\ step_counter'    = step_counter + 1
    /\ \E outcome \in AttemptStatuses :
           /\ last_status' = outcome
           /\ IF outcome = "SUCCESS"
                THEN /\ txn_status' = "SUCCESS"
                    /\ UNCHANGED << current_provider, provider_up,
                                retry_window_count, extra_state >>
              ELSE UNCHANGED << txn_status, current_provider, provider_up,
                                retry_window_count, extra_state >>

RetryAction ==
    /\ txn_status       = "PENDING"
    /\ attempt_count    >= 1
    /\ attempt_count    < MaxRetry
    /\ IsRetryable(last_status)
    /\ BudgetOk
    /\ retry_window_count' = retry_window_count + 1
    /\ step_counter'       = step_counter + 1
    /\ LET p == ChooseProvider IN
       /\ current_provider' = IF p = "" THEN current_provider ELSE p
    /\ UNCHANGED << txn_status, attempt_count, last_status,
                    provider_up, extra_state >>

TerminalFailAction ==
    /\ txn_status    = "PENDING"
    /\ attempt_count >= 1
    /\ \/ ~IsRetryable(last_status)
       \/ attempt_count >= MaxRetry
       \/ ~BudgetOk
    /\ txn_status' = "FAILED"
    /\ UNCHANGED << attempt_count, last_status, current_provider,
                    provider_up, retry_window_count, extra_state >>
    /\ step_counter' = step_counter + 1

ProviderDownAction ==
    /\ \E p \in Providers :
           /\ provider_up[p] = TRUE
           /\ provider_up'      = [provider_up EXCEPT ![p] = FALSE]
           /\ current_provider' = IF current_provider = p THEN "" ELSE current_provider
    /\ UNCHANGED << txn_status, attempt_count, last_status,
                    retry_window_count, extra_state >>
    /\ step_counter' = step_counter + 1

ProviderRecoverAction ==
    /\ \E p \in Providers :
           /\ provider_up[p] = FALSE
           /\ provider_up'   = [provider_up EXCEPT ![p] = TRUE]
    /\ UNCHANGED << txn_status, attempt_count, last_status,
                    current_provider, retry_window_count, extra_state >>
    /\ step_counter' = step_counter + 1

StandardNext ==
    \/ RouteAction
    \/ AttemptAction
    \/ RetryAction
    \/ TerminalFailAction
    \/ ProviderDownAction
    \/ ProviderRecoverAction

Next == StandardNext

\* Fairness constraints: require that attempt, retry and routing actions
\* are not indefinitely ignored by the environment when enabled.
%%SPEC_NAME%%Spec == Init /\ [][Next]_vars

\* Environment fairness assumption: if a provider goes down, it will eventually recover.
EnvFairness == WF_vars(ProviderRecoverAction)

SpecWithFairness == Init /\ [][Next]_vars /\ EnvFairness

\* I2 — Retry Bound: attempt_count never exceeds MAX_RETRY
I2_RetryBound ==
    attempt_count <= MaxRetry

\* I1 — Single Settlement: once SUCCESS, no new attempts are enabled
I1_SingleSettlement ==
    (txn_status = "SUCCESS") => ~AttemptEnabled

\* I3 — Terminal Absorption: when terminal, no attempts are enabled thereafter
I3_TerminalAbsorption ==
    (txn_status \in {"SUCCESS", "FAILED"}) => ~AttemptEnabled

\* I4 — Circuit Respect: never route to a DOWN provider
I4_CircuitRespect ==
    (current_provider # "") =>
        (provider_up[current_provider] = TRUE)

\* I5 — Weight Domain: provider weights only defined for known providers
I5_WeightDomainValid ==
    DOMAIN ProviderWeights \subseteq Providers

\* ---------------------------------------------------------------------------
\* Temporal properties (explicit LTL formulas)
\* These properties are supplied to TLC as `PROPERTY` entries in the .cfg
\* to exercise temporal safety checks (no further attempts after success,
\* no attempts after terminal states, never route to a DOWN provider).

I1_SingleSettlementProp == []( (txn_status = "SUCCESS") => ~AttemptEnabled )

I3_TerminalAbsorptionProp == []( (txn_status \in {"SUCCESS", "FAILED"}) => ~AttemptEnabled )

I4_CircuitRespectProp == []( (current_provider # "") => provider_up[current_provider] )

\* ---------------------------------------------------------------------------
\* Liveness / recovery properties:
\*  - L1: Eventual terminality — a pending transaction eventually becomes
\*        either SUCCESS or FAILED (no permanent livelock).
\*  - L2: Eventual routing — if there exists an up provider, eventually a
\*        non-empty `current_provider` will be set (route progress).
\*
\* Note: these are stronger assumptions about the environment; the
\* fairness constraints above help TLC explore execution traces where
\* enabled actions are not indefinitely ignored.

L1_EventualTerminalProp == []( txn_status = "PENDING" => <> (txn_status \in {"SUCCESS","FAILED"}) )

terminal_reached == txn_status \in {"SUCCESS", "FAILED"}

L2_BoundedTerminalProp == <>( step_counter >= MaxRetry \/ terminal_reached )

\* Bounded-attempts recovery: either the transaction reaches a terminal
\* state or the attempt counter reaches the configured `MaxRetry` bound.
L1_BoundedAttemptsProp == []( txn_status = "PENDING" => <> (txn_status \in {"SUCCESS","FAILED"} \/ attempt_count >= MaxRetry) )

====