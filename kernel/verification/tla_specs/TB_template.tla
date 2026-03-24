---- MODULE %%SPEC_NAME%% ----
EXTENDS Integers, Sequences, FiniteSets, TLC

VARIABLES
    txn_status,
    attempt_count,
    last_status,
    current_provider,
    provider_up,
    retry_window_count,
    extra_state

vars == << txn_status, attempt_count, last_status, current_provider,
           provider_up, retry_window_count, extra_state >>

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

Init ==
    /\ txn_status         = "PENDING"
    /\ attempt_count      = 0
    /\ last_status        = "SUCCESS"
    /\ current_provider   = ""
    /\ provider_up        = [p \in Providers |-> TRUE]
    /\ retry_window_count = 0
    /\ extra_state        = [k \in {} |-> ""]

UpProviders == { p \in Providers : provider_up[p] = TRUE }

ChooseProvider ==
    IF UpProviders = {} THEN ""
    ELSE CHOOSE p \in UpProviders : TRUE

IsRetryable(status) == status \in RetryableCodes
BackoffOk == TRUE
BudgetOk  == retry_window_count < MaxRetriesPerWindow

RouteAction ==
    /\ txn_status       = "PENDING"
    /\ attempt_count    = 0
    /\ current_provider = ""
    /\ LET p == ChooseProvider IN
       IF p = ""
       THEN /\ txn_status'       = "FAILED"
            /\ current_provider' = ""
            /\ UNCHANGED << attempt_count, last_status,
                            provider_up, retry_window_count, extra_state >>
       ELSE /\ current_provider' = p
            /\ UNCHANGED << txn_status, attempt_count, last_status,
                            provider_up, retry_window_count, extra_state >>

AttemptAction ==
    /\ txn_status       = "PENDING"
    /\ current_provider # ""
    /\ attempt_count    < MaxRetry
    /\ (attempt_count = 0 \/ IsRetryable(last_status))
    /\ attempt_count'   = attempt_count + 1
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

ProviderDownAction ==
    /\ \E p \in Providers :
           /\ provider_up[p] = TRUE
           /\ provider_up'      = [provider_up EXCEPT ![p] = FALSE]
           /\ current_provider' = IF current_provider = p THEN "" ELSE current_provider
    /\ UNCHANGED << txn_status, attempt_count, last_status,
                    retry_window_count, extra_state >>

ProviderRecoverAction ==
    /\ \E p \in Providers :
           /\ provider_up[p] = FALSE
           /\ provider_up'   = [provider_up EXCEPT ![p] = TRUE]
    /\ UNCHANGED << txn_status, attempt_count, last_status,
                    current_provider, retry_window_count, extra_state >>

StandardNext ==
    \/ RouteAction
    \/ AttemptAction
    \/ RetryAction
    \/ TerminalFailAction
    \/ ProviderDownAction
    \/ ProviderRecoverAction

Next == StandardNext

%%SPEC_NAME%%Spec == Init /\ [][Next]_vars

\* I2 — Retry Bound: attempt_count never exceeds MAX_RETRY
I2_RetryBound ==
    attempt_count <= MaxRetry

\* I3 — Terminal Absorption: no attempts after terminal state
I3_TerminalAbsorption ==
    (txn_status \in {"SUCCESS", "FAILED"}) =>
        attempt_count <= MaxRetry

\* I4 — Circuit Respect: never route to a DOWN provider
I4_CircuitRespect ==
    (current_provider # "") =>
        (provider_up[current_provider] = TRUE)

\* I5 — Weight Domain: provider weights only defined for known providers
I5_WeightDomainValid ==
    DOMAIN ProviderWeights \subseteq Providers

====