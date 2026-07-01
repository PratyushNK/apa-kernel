"""
kernel/adaptation/loop.py

AdaptationLoop — the core agentic loop.

Structure (LangGraph-compatible):
    fetch_metrics
        ↓
    reason_and_propose
        ↓
    verify_invariants ──→ correction_attempt (max 1)
        ↓
    deploy_policy
        ↓
    observe_outcome ──→ back to fetch (max 3 cycles)
        ↓
    done

Each node is a pure async function:
    input  : AdaptationState
    output : AdaptationState
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import json
import time
import uuid
import os
from typing import Optional

from interfaces.llm import LLM
from kernel.adaptation.schemas import (
    AdaptationContext,
    AdaptationDecision,
    PolicyPatchSchema,
    PolicyVectorSchema,
    AdaptationState,
    CorrectionContext,
)
from kernel.adaptation.prompt_builder import (
    SYSTEM_PROMPT,
    CORRECTION_SYSTEM_PROMPT,
    THETA_SYSTEM_PROMPT,
    build_adaptation_prompt,
    build_theta_prompt,
    build_correction_prompt,
)
from kernel.aggregator.aggregator import Aggregator, HealthThresholds
from kernel.verification.verifier import InvariantVerifier
from simulator.policy_engine import PolicyStore, PolicyVector

logger = logging.getLogger(__name__)

# Structured adaptations trace path
ROOT = pathlib.Path(__file__).parent.parent.parent
ADAPTATIONS_PATH = ROOT / "data" / "streams" / "adaptations.jsonl"


def _emit_adaptation_record(stage: str, proposal_id: Optional[str], payload: dict) -> None:
    try:
        ADAPTATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            # emit epoch milliseconds for consistency with events.jsonl
            "ts": int(time.time() * 1000),
            "stage": stage,
            "proposal_id": proposal_id,
        }
        rec.update(payload or {})
        with ADAPTATIONS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        logger.debug("[adaptation] failed to emit adaptation record")

MAX_CYCLES     = 3
MAX_CORRECTIONS = 1
OBSERVE_WAIT_S  = 6.0   # seconds to wait before observing outcome


class AdaptationLoop:

    def __init__(
        self,
        llm          : LLM,
        aggregator   : Aggregator,
        policy_store : PolicyStore,
        verifier     : InvariantVerifier,
    ):
        self._llm          = llm
        self._aggregator   = aggregator
        self._policy_store = policy_store
        self._verifier     = verifier

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self, objective: str = "cure") -> AdaptationState:
        """
        Run the full adaptation loop.
        Returns final AdaptationState with status.
        """
        logger.info(f"[adaptation] starting loop — objective: {objective}")

        # Build initial state
        context = self._build_context(objective)
        state   = AdaptationState(context=context, objective=objective)

        while state.status == "running":
            # reset correction attempts per cycle (allow MAX_CORRECTIONS per cycle)
            state.correction_count = 0
            state = await self._fetch_metrics(state)
            state = await self._reason_and_propose(state)
            if state.status != "running":
                break
            state = await self._propose_theta(state)
            if state.status != "running":
                break
            state = await self._verify_invariants(state)

            if not state.verification_pass:
                state = await self._correction_attempt(state)

            if state.verification_pass:
                state = await self._deploy_policy(state)
                state = await self._observe_outcome(state)
            else:
                # Correction also failed — count as failed cycle
                state.cycle_count += 1
                logger.warning(
                    f"[adaptation] cycle {state.cycle_count} — "
                    f"verification failed after correction"
                )

            if state.cycle_count >= MAX_CYCLES and state.status == "running":
                state.status = "max_cycles"
                logger.warning("[adaptation] max cycles reached without recovery")

        logger.info(f"[adaptation] loop ended — status: {state.status}")
        return state

    # ------------------------------------------------------------------
    # Node 1 — Fetch metrics
    # ------------------------------------------------------------------

    async def _fetch_metrics(self, state: AdaptationState) -> AdaptationState:
        snapshot, delta = self._aggregator.get_snapshot()
        if snapshot is None:
            state.status = "failed"
            logger.error("[adaptation] no snapshot available")
            return state

        # Rebuild context with latest metrics
        state.context = self._build_context(state.objective, snapshot, delta)
        logger.info(
            f"[adaptation] fetched metrics — "
            f"approval_rate={snapshot.approval_rate:.3f} "
            f"any_breach={snapshot.invariant_risk.any_breach}"
        )
        # Diagnostic: include per-provider rates and explicit invariant list
        try:
            per_provider = {pm.provider: {
                'rolling_success_rate': pm.rolling_success_rate,
                'p95_latency_ms': pm.p95_latency_ms,
                'timeout_rate': pm.timeout_rate,
                'sla_breach_rate': pm.sla_breach_rate,
            } for pm in snapshot.per_provider}
            logger.info(f"[adaptation] per_provider={per_provider}")
            breaches = self._get_breaches(snapshot)
            logger.info(f"[adaptation] active_breaches={breaches}")
        except Exception:
            logger.debug("[adaptation] failed to emit per-provider fetch diagnostics")
        return state

    # ------------------------------------------------------------------
    # Node 2 — Reason and propose (Stage 1)
    # ------------------------------------------------------------------

    async def _reason_and_propose(self, state: AdaptationState) -> AdaptationState:
        prompt = build_adaptation_prompt(state.context)
        logger.debug(f"[adaptation] adaptation prompt length={len(prompt)}")
        logger.debug("[adaptation] adaptation prompt (trunc): %s", prompt[:1000])

        logger.info("[adaptation] calling LLM for reasoning")
        try:
            decision = self._llm.generate_structured(
                schema        = AdaptationDecision,
                prompt        = prompt,
                system_prompt = SYSTEM_PROMPT,
                max_tokens    = 250,
            )
        except Exception:
            logger.exception("[adaptation] exception calling LLM for reasoning")
            # Emit agent event for the failed LLM invocation
            try:
                payload = {
                    "ts": int(time.time() * 1000),
                    "stage": "reasoning",
                    "schema": "AdaptationDecision",
                    "system_prompt": SYSTEM_PROMPT,
                    "prompt": prompt,
                    "error": "exception calling LLM for reasoning",
                }
                logger.info("[adaptation][agent] %s", json.dumps(payload))
            except Exception:
                pass
            state.status = "failed"
            return state

        if decision is None:
            logger.error("[adaptation] LLM returned None — structured output failed")
            state.status = "failed"
            return state

        # Log the structured response for debugging
        try:
            logger.debug("[adaptation] decision model_dump: %s", decision.model_dump())
        except Exception:
            logger.debug("[adaptation] decision repr: %s", repr(decision))

        # Log the agent interaction (prompt + structured response)
        try:
            resp = None
            try:
                resp = decision.model_dump(exclude_none=True)
            except Exception:
                resp = repr(decision)
            payload = {
                "ts": int(time.time() * 1000),
                "stage": "reasoning",
                "schema": "AdaptationDecision",
                "system_prompt": SYSTEM_PROMPT,
                "prompt": prompt,
                "response": resp,
            }
            logger.info("[adaptation][agent] %s", json.dumps(payload))
        except Exception:
            pass

        state.decision          = decision
        state.proposed_theta    = None
        state.verification_pass = False
        state.violations        = []

        # safe slice of optional reasoning (may be None)
        _reasoning = getattr(decision, "reasoning", None)
        _reason_snip = (_reasoning[:60] + "...") if isinstance(_reasoning, str) and len(_reasoning) > 60 else (_reasoning if isinstance(_reasoning, str) else None)
        logger.info(
            f"[adaptation] reasoning received — "
            f"confidence={decision.confidence:.2f} "
            f"reasoning='{_reason_snip}'"
        )
        return state

    async def _propose_theta(self, state: AdaptationState) -> AdaptationState:
        if state.decision is None:
            state.status = "failed"
            logger.error("[adaptation] no reasoning decision for theta proposal")
            return state

        prompt = build_theta_prompt(state.decision, state.context.current_theta)
        logger.debug(f"[adaptation] theta prompt length={len(prompt)}")
        logger.debug("[adaptation] theta prompt (trunc): %s", prompt[:1000])

        logger.info("[adaptation] calling LLM for policy vector")
        try:
            theta_patch = self._llm.generate_structured(
                schema        = PolicyPatchSchema,
                prompt        = prompt,
                system_prompt = THETA_SYSTEM_PROMPT,
                max_tokens    = 300,
            )
        except Exception:
            logger.exception("[adaptation] exception calling LLM for policy vector")
            try:
                payload = {
                    "ts": int(time.time() * 1000),
                    "stage": "propose_theta",
                    "schema": "PolicyPatchSchema",
                    "system_prompt": THETA_SYSTEM_PROMPT,
                    "prompt": prompt,
                    "error": "exception calling LLM for policy vector",
                }
                logger.info("[adaptation][agent] %s", json.dumps(payload))
            except Exception:
                pass
            state.status = "failed"
            return state

        if theta_patch is None:
            logger.error("[adaptation] LLM returned None — policy vector generation failed")
            state.status = "failed"
            return state

        try:
            try:
                logger.debug("[adaptation] theta_patch model_dump: %s", theta_patch.model_dump(exclude_none=True))
            except Exception:
                logger.debug("[adaptation] theta_patch repr: %s", repr(theta_patch))
            merged_theta = self._merge_theta_patch(state.context.current_theta, theta_patch)
            logger.debug("[adaptation] merged_theta snapshot: %s", {k: merged_theta.get(k) for k in list(merged_theta)[:10]})
            state.proposed_theta = PolicyVectorSchema(**merged_theta)
            # emit agent event with prompt + response
            try:
                resp = None
                try:
                    resp = theta_patch.model_dump(exclude_none=True)
                except Exception:
                    resp = repr(theta_patch)
                payload = {
                    "ts": int(time.time() * 1000),
                    "stage": "propose_theta",
                    "schema": "PolicyPatchSchema",
                    "system_prompt": THETA_SYSTEM_PROMPT,
                    "prompt": prompt,
                    "response": resp,
                    "proposal_id": state.proposal_id,
                }
                logger.info("[adaptation][agent] %s", json.dumps(payload))
            except Exception:
                pass
        except Exception as e:
            logger.exception(f"[adaptation] policy vector invalid after merge — {e}")
            state.status = "failed"
            return state

        logger.info(
            f"[adaptation] policy vector received — "
            f"weights={state.proposed_theta.provider_weights}"
        )
        # emit structured proposal record
        try:
            proposal_id = str(uuid.uuid4())
            state.proposal_id = proposal_id
            try:
                pdump = state.proposed_theta.model_dump(exclude_none=True)
            except Exception:
                pdump = None
            # safe extraction of decision reasoning snippet
            _dec_reason = getattr(state.decision, "reasoning", None) if state.decision else None
            _dec_snip = _dec_reason[:200] if isinstance(_dec_reason, str) else None
            _emit_adaptation_record(
                "proposed",
                proposal_id,
                {
                    "objective": state.objective,
                    "decision_confidence": getattr(state.decision, "confidence", None) if state.decision else None,
                    "decision_reasoning_snip": _dec_snip,
                    "pre_approval": state.context.approval_rate if state.context else None,
                    "pre_invariant_breaches": getattr(state.context, "invariant_breaches", None) if state.context else None,
                    "proposed_theta": {
                        "provider_weights": (pdump.get("provider_weights") if pdump else None),
                        "max_retry": (pdump.get("max_retry") if pdump else None),
                    },
                },
            )
        except Exception:
            logger.debug("[adaptation] failed to emit proposed JSON record")
        return state

    # ------------------------------------------------------------------
    # Node 3 — Verify invariants
    # ------------------------------------------------------------------

    async def _verify_invariants(self, state: AdaptationState) -> AdaptationState:
        if state.proposed_theta is None:
            state.status = "failed"
            return state

        try:
            proposed_dump = state.proposed_theta.model_dump()
            logger.debug("[adaptation] proposed_theta dump: %s", proposed_dump)
        except Exception:
            logger.debug("[adaptation] could not dump proposed_theta")

        # Run the potentially blocking verifier in a thread to avoid blocking
        # the asyncio event loop (TLC may run as a subprocess and block).
        # Compute an async guard timeout (based on the TLC timeout env var)
        try:
            tlc_timeout = int(os.getenv("VERIFIER_TLC_TIMEOUT_ADAPTATION", "10"))
        except Exception:
            tlc_timeout = 10
        async_timeout = max(15, tlc_timeout + 5)

        # Run verification in executor, request fast-mode from verifier API
        try:
            loop = asyncio.get_running_loop()
            try:
                is_valid, violations = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        self._verifier.check,
                        state.proposed_theta.model_dump(),
                        True,
                        tlc_timeout,
                    ),
                    timeout=async_timeout,
                )
            except asyncio.TimeoutError:
                # Verifier thread hung or exceeded expected time; fall back to fast Python checks
                logger.warning("[adaptation] verification timed out after %ss; using fast Python checks", async_timeout)
                try:
                    proposed = dict(state.proposed_theta.model_dump())
                except Exception:
                    try:
                        proposed = dict(state.proposed_theta.__dict__)
                    except Exception:
                        proposed = {}
                violations = []
                try:
                    violations.extend(self._verifier._check_single_settlement(proposed))
                    violations.extend(self._verifier._check_I2_retry_bound(proposed))
                    violations.extend(self._verifier._check_provider_weights(proposed))
                    violations.extend(self._verifier._check_backoff(proposed))
                    violations.extend(self._verifier._check_retry_budget(proposed))
                except Exception:
                    logger.exception("[adaptation] fallback verification failed")
                is_valid = len(violations) == 0
            except Exception:
                # Fall back to synchronous call if executor fails for any other reason
                is_valid, violations = self._verifier.check(state.proposed_theta.model_dump(), True, tlc_timeout)
        except Exception:
            # If event loop not available or other issue, try synchronous check
            is_valid, violations = self._verifier.check(state.proposed_theta.model_dump(), True, tlc_timeout)
        state.verification_pass = is_valid
        state.violations        = violations

        if is_valid:
            logger.info("[adaptation] verification passed")
        # emit verification outcome
        try:
            tlc_indicators = ("TLC counterexample", "TLC attempt error", "TLC check failed")
            verification_tlc = any(any(ind in v for ind in tlc_indicators) for v in violations) if violations else False
            _emit_adaptation_record(
                "verified",
                state.proposal_id,
                {
                    "verification_pass": state.verification_pass,
                    "violations": state.violations,
                    "verification_tlc": verification_tlc,
                },
            )
        except Exception:
            logger.debug("[adaptation] failed to emit verification record")

        # Only log failures; avoid emitting misleading warnings when verification passed
        if not state.verification_pass:
            tlc_indicators = ("TLC counterexample", "TLC attempt error", "TLC check failed")
            if any(any(ind in v for ind in tlc_indicators) for v in violations):
                logger.warning("[adaptation] verification failed (TLC) — %s", violations)
            else:
                logger.warning("[adaptation] verification failed (Python fallback) — %s", violations)

        return state

    # ------------------------------------------------------------------
    # Node 4 — Correction attempt (max 1 per cycle)
    # ------------------------------------------------------------------

    async def _correction_attempt(self, state: AdaptationState) -> AdaptationState:
        if state.correction_count >= MAX_CORRECTIONS:
            logger.warning("[adaptation] max corrections reached")
            return state
        
        if state.proposed_theta is None:
            state.status = "failed"
            return state

        correction_ctx = CorrectionContext(
            rejected_theta    = state.proposed_theta,
            violations        = state.violations,
        )
        prompt = build_correction_prompt(correction_ctx)
        logger.debug(f"[adaptation] correction prompt length={len(prompt)}")
        logger.debug("[adaptation] correction prompt (trunc): %s", prompt[:1000])

        logger.info("[adaptation] calling LLM for correction")
        try:
            corrected_patch = self._llm.generate_structured(
                schema        = PolicyPatchSchema,
                prompt        = prompt,
                system_prompt = CORRECTION_SYSTEM_PROMPT,
                max_tokens    = 300,
            )
        except Exception:
            logger.exception("[adaptation] exception calling LLM for correction")
            try:
                payload = {
                    "ts": int(time.time() * 1000),
                    "stage": "correction",
                    "schema": "PolicyPatchSchema",
                    "system_prompt": CORRECTION_SYSTEM_PROMPT,
                    "prompt": prompt,
                    "error": "exception calling LLM for correction",
                }
                logger.info("[adaptation][agent] %s", json.dumps(payload))
            except Exception:
                pass
            state.status = "failed"
            return state

        if corrected_patch is None:
            logger.error("[adaptation] correction generation failed")
            state.status = "failed"
            return state

        try:
            try:
                logger.debug("[adaptation] corrected_patch model_dump: %s", corrected_patch.model_dump(exclude_none=True))
            except Exception:
                logger.debug("[adaptation] corrected_patch repr: %s", repr(corrected_patch))
            base_theta = state.proposed_theta.model_dump()
            merged_theta = self._merge_theta_patch(base_theta, corrected_patch)
            logger.debug("[adaptation] merged corrected theta keys: %s", list(merged_theta.keys()))
            corrected_theta = PolicyVectorSchema(**merged_theta)
        except Exception as e:
            logger.exception(f"[adaptation] corrected policy invalid after merge — {e}")
            state.status = "failed"
            return state

        state.proposed_theta    = corrected_theta
        state.correction_count += 1

        # emit agent event for correction response
        try:
            try:
                resp = corrected_patch.model_dump(exclude_none=True)
            except Exception:
                resp = repr(corrected_patch)
            payload = {
                "ts": int(time.time() * 1000),
                "stage": "correction",
                "schema": "PolicyPatchSchema",
                "system_prompt": CORRECTION_SYSTEM_PROMPT,
                "prompt": prompt,
                "response": resp,
                "proposal_id": state.proposal_id,
            }
            logger.info("[adaptation][agent] %s", json.dumps(payload))
        except Exception:
            pass

        # Re-verify correction with same async guard & fast-mode as _verify_invariants
        logger.info("[adaptation] re-verifying corrected proposal (proposal_id=%s)", state.proposal_id)
        try:
            try:
                tlc_timeout = int(os.getenv("VERIFIER_TLC_TIMEOUT_ADAPTATION", "10"))
            except Exception:
                tlc_timeout = 10
            async_timeout = max(15, tlc_timeout + 5)

            try:
                loop = asyncio.get_running_loop()
                try:
                    is_valid, violations = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            self._verifier.check,
                            corrected_theta.model_dump(),
                            True,
                            tlc_timeout,
                        ),
                        timeout=async_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[adaptation] correction verification timed out after %ss; using fast Python checks", async_timeout)
                    try:
                        proposed = dict(corrected_theta.model_dump())
                    except Exception:
                        try:
                            proposed = dict(corrected_theta.__dict__)
                        except Exception:
                            proposed = {}
                    violations = []
                    try:
                        violations.extend(self._verifier._check_single_settlement(proposed))
                        violations.extend(self._verifier._check_I2_retry_bound(proposed))
                        violations.extend(self._verifier._check_provider_weights(proposed))
                        violations.extend(self._verifier._check_backoff(proposed))
                        violations.extend(self._verifier._check_retry_budget(proposed))
                    except Exception:
                        logger.exception("[adaptation] correction fallback verification failed")
                    is_valid = len(violations) == 0
                except Exception:
                    # Fall back to synchronous call if executor fails for any other reason
                    is_valid, violations = self._verifier.check(corrected_theta.model_dump(), True, tlc_timeout)
            except Exception:
                # As a last resort, do a synchronous check
                is_valid, violations = self._verifier.check(corrected_theta.model_dump(), True, tlc_timeout)
        except Exception:
            # As a last resort, do a synchronous check
            is_valid, violations = self._verifier.check(corrected_theta.model_dump(), True, tlc_timeout)

        state.verification_pass = is_valid
        state.violations = violations

        if is_valid:
            logger.info("[adaptation] correction passed verification")
        else:
            logger.warning(f"[adaptation] correction still invalid — {violations}")

        # emit correction record
        try:
            _emit_adaptation_record(
                "correction",
                state.proposal_id,
                {
                    "correction_count": state.correction_count,
                    "corrected_theta": state.proposed_theta.model_dump(exclude_none=True),
                    "verification_pass": state.verification_pass,
                    "violations": state.violations,
                },
            )
        except Exception:
            logger.debug("[adaptation] failed to emit correction record")

        return state

    # ------------------------------------------------------------------
    # Node 5 — Deploy policy
    # ------------------------------------------------------------------

    async def _deploy_policy(self, state: AdaptationState) -> AdaptationState:
        if state.proposed_theta is None:
            state.status = "failed"
            return state

        try:
            new_theta = PolicyVector(**state.proposed_theta.model_dump())
            # Log prior and new policy for diagnostics
            try:
                prior_obj = self._policy_store.current
                prior = {
                    "provider_weights": getattr(prior_obj, "provider_weights", None),
                    "max_retry": getattr(prior_obj, "max_retry", None),
                }
                # save full prior theta for potential rollback
                try:
                    state.prior_theta = dict(prior_obj.__dict__)
                except Exception:
                    try:
                        state.prior_theta = dict(prior_obj.__dict__)
                    except Exception:
                        state.prior_theta = None
            except Exception:
                prior = None
            # Deploy new theta (canary-style: we keep prior in state to allow rollback)
            self._policy_store.update(new_theta)
            # record deployment time (ms) so observation can wait for
            # a snapshot that includes post-deploy traffic only
            try:
                state.deployed_at_ms = int(time.time() * 1000)
            except Exception:
                state.deployed_at_ms = None
            logger.info(
                f"[adaptation] policy deployed — "
                f"max_retry={new_theta.max_retry} "
                f"weights={new_theta.provider_weights} "
                f"prior={prior}"
            )
            # emit deployment record
            try:
                new_rec = {"provider_weights": getattr(new_theta, "provider_weights", None), "max_retry": getattr(new_theta, "max_retry", None)}
                _emit_adaptation_record(
                    "deployed",
                    state.proposal_id,
                    {
                        "prior": prior,
                        "new": new_rec,
                    },
                )
            except Exception:
                logger.debug("[adaptation] failed to emit deployed record")
        except Exception as e:
            logger.error(f"[adaptation] policy deploy failed — {e}")
            state.status = "failed"

        return state

    # ------------------------------------------------------------------
    # Node 6 — Observe outcome
    # ------------------------------------------------------------------

    async def _observe_outcome(self, state: AdaptationState) -> AdaptationState:
        logger.info(f"[adaptation] waiting {OBSERVE_WAIT_S}s to observe outcome")
        # Initial sleep gives the system a chance to apply the new policy
        await asyncio.sleep(OBSERVE_WAIT_S)

        # After the initial pause, prefer a snapshot that reflects post-deploy
        # traffic. Poll the aggregator until we observe a snapshot whose
        # `window_end_ms` is after the recorded deployment time and which has
        # sufficient data, up to a reasonable deadline. To reduce false
        # negatives from transient noise, require N consecutive good
        # post-deploy snapshots (configurable via `RECOVERY_CONSECUTIVE_GOOD`).
        try:
            consecutive_required = int(os.getenv("RECOVERY_CONSECUTIVE_GOOD", "2"))
        except Exception:
            consecutive_required = 2

        poll_deadline = time.time() + OBSERVE_WAIT_S + 10.0 + (consecutive_required * OBSERVE_WAIT_S)
        snapshot, _ = self._aggregator.get_snapshot()

        # Helper to check snapshot freshness and sufficiency
        def _is_post_deploy(snap: Optional[object]) -> bool:
            if snap is None:
                return False
            # prefer snapshots with sufficient data
            try:
                if not getattr(snap, "has_sufficient_data", False):
                    return False
            except Exception:
                return False
            # if we have a recorded deploy time, ensure snapshot covers later events
            if getattr(state, "deployed_at_ms", None):
                try:
                    # Prefer windows that start after deployment so the
                    # snapshot only contains post-deploy traffic. Using
                    # `window_start_ms` avoids contamination from earlier
                    # pre-deploy requests in sliding windows.
                    return getattr(snap, "window_start_ms", 0) >= int(state.deployed_at_ms)
                except Exception:
                    return False
            return True

        # First, wait until we see any post-deploy, sufficient snapshot (or timeout)
        while not _is_post_deploy(snapshot) and time.time() < poll_deadline:
            await asyncio.sleep(1.0)
            snapshot, _ = self._aggregator.get_snapshot()

        state.cycle_count += 1

        if snapshot is None:
            logger.warning("[adaptation] no snapshot for observation")
            return state

        # Diagnostic: log the observed snapshot metrics at observation time
        try:
            logger.info(
                f"[adaptation] observe_snapshot — approval={snapshot.approval_rate:.3f} "
                f"sla_breach_rate={snapshot.sla_breach_rate:.3f} "
                f"timeout_rate={snapshot.timeout_rate:.3f} "
                f"p95_latency_ms={snapshot.p95_latency_ms} "
                f"retry_amplification={snapshot.retry_amplification_factor:.3f} "
                f"invariants={self._get_breaches(snapshot)}"
            )
        except Exception:
            logger.debug("[adaptation] failed to emit observe snapshot diagnostics")

        # emit observed snapshot record
        try:
            post_snapshot = {
                "approval_rate": getattr(snapshot, "approval_rate", None),
                "p95_latency_ms": getattr(snapshot, "p95_latency_ms", None),
                "sla_breach_rate": getattr(snapshot, "sla_breach_rate", None),
                "timeout_rate": getattr(snapshot, "timeout_rate", None),
                "invariants": self._get_breaches(snapshot),
            }

            # Recovery gating: allow stricter research-grade criteria via
            # environment variable `RECOVERY_REQUIRE_APPROVAL`.
            require_approval = os.getenv("RECOVERY_REQUIRE_APPROVAL", "0") == "1"
            try:
                min_approval = float(os.getenv("RECOVERY_MIN_APPROVAL", str(HealthThresholds.min_approval_rate)))
            except Exception:
                min_approval = HealthThresholds.min_approval_rate

            # Require consecutive successful post-deploy snapshots to avoid
            # transient misclassification. Only count snapshots that are
            # post-deploy and have sufficient data.
            try:
                consecutive_required = int(os.getenv("RECOVERY_CONSECUTIVE_GOOD", "2"))
            except Exception:
                consecutive_required = 2

            consecutive_ok = 0
            # Allow a short additional window to collect consecutive snapshots
            consecutive_deadline = time.time() + (consecutive_required * OBSERVE_WAIT_S) + 5.0
            # Evaluate successive snapshots until we either reach the required
            # consecutive count or exhaust the deadline.
            while time.time() < consecutive_deadline and consecutive_ok < consecutive_required:
                # Only evaluate snapshots that cover post-deploy traffic
                if _is_post_deploy(snapshot):
                    try:
                        snapshot_ok = (not snapshot.invariant_risk.any_breach) and (
                            (not require_approval) or (snapshot.approval_rate >= min_approval)
                        )
                    except Exception:
                        snapshot_ok = False

                    if snapshot_ok:
                        consecutive_ok += 1
                    else:
                        consecutive_ok = 0

                if consecutive_ok >= consecutive_required:
                    break

                await asyncio.sleep(1.0)
                snapshot, _ = self._aggregator.get_snapshot()

            recovery_confirmed = consecutive_ok >= consecutive_required

            _emit_adaptation_record(
                "observed",
                state.proposal_id,
                {
                    "post_snapshot": post_snapshot,
                    "recovery_confirmed": recovery_confirmed,
                    "cycle_count": state.cycle_count,
                },
            )
        except Exception:
            logger.debug("[adaptation] failed to emit observed record")

        # Only mark the loop as successful when the recovery gating passes.
        if recovery_confirmed:
            state.status = "success"
            logger.info(
                f"[adaptation] recovery confirmed — "
                f"approval_rate={snapshot.approval_rate:.3f}"
            )
        else:
            # If invariants re-appear after deployment, revert to prior theta
            try:
                if snapshot.invariant_risk.any_breach:
                    if getattr(state, "prior_theta", None):
                        try:
                            prior_vec = PolicyVector(**state.prior_theta)
                            self._policy_store.update(prior_vec)
                            logger.warning("[adaptation] post-deploy invariants detected — reverted to prior policy (proposal_id=%s)", state.proposal_id)
                            try:
                                _emit_adaptation_record(
                                    "reverted",
                                    state.proposal_id,
                                    {
                                        "reverted_to": {
                                            "provider_weights": getattr(prior_vec, "provider_weights", None),
                                            "max_retry": getattr(prior_vec, "max_retry", None),
                                        },
                                        "post_snapshot": post_snapshot,
                                    },
                                )
                            except Exception:
                                logger.debug("[adaptation] failed to emit reverted record")
                        except Exception:
                            logger.exception("[adaptation] failed to revert to prior policy")
                    else:
                        logger.warning("[adaptation] post-deploy invariants detected but no prior policy available to revert to")
            except Exception:
                logger.exception("[adaptation] error while handling post-deploy revert logic")
            # If invariants cleared but approval below threshold, log and continue
            if not snapshot.invariant_risk.any_breach and require_approval:
                logger.info(
                    f"[adaptation] invariants cleared but approval below threshold "
                    f"({snapshot.approval_rate:.3f} < {min_approval:.3f}) — continuing observation"
                )
            else:
                logger.info(
                    f"[adaptation] not yet recovered — "
                    f"cycle {state.cycle_count}/{MAX_CYCLES}"
                )

        return state

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(
        self,
        objective : str,
        snapshot  = None,
        delta     = None,
    ) -> AdaptationContext:
        theta = self._policy_store.current

        if snapshot is None:
            snapshot, delta = self._aggregator.get_snapshot()

        provider_success = {}
        provider_circuit = {}
        if snapshot:
            for pm in snapshot.per_provider:
                provider_success[pm.provider] = pm.rolling_success_rate
                provider_circuit[pm.provider] = pm.circuit_open_rate > 0.3 and "OPEN" or "CLOSED"

        return AdaptationContext(
            approval_rate           = snapshot.approval_rate if snapshot else 0.0,
            rolling_success_rate    = snapshot.rolling_success_rate if snapshot else 0.0,
            retry_amplification     = snapshot.retry_amplification_factor if snapshot else 0.0,
            circuit_open_rate       = snapshot.circuit_open_rate if snapshot else 0.0,
            sla_breach_rate         = snapshot.sla_breach_rate if snapshot else 0.0,
            timeout_rate            = snapshot.timeout_rate if snapshot else 0.0,
            provider_success_rates  = provider_success,
            provider_circuit_states = provider_circuit,
            approval_rate_delta     = delta.approval_rate_delta if delta and delta.has_baseline else None,
            success_rate_delta      = delta.rolling_success_rate_delta if delta and delta.has_baseline else None,
            circuit_open_rate_delta = delta.circuit_open_rate_delta if delta and delta.has_baseline else None,
            retry_amplification_delta = delta.retry_amplification_delta if delta and delta.has_baseline else None,
            invariant_breaches      = self._get_breaches(snapshot),
            current_theta           = theta.__dict__,
            objective               = objective,
        )

    def _get_breaches(self, snapshot) -> list[str]:
        if snapshot is None:
            return []
        risk = snapshot.invariant_risk
        return [
            name for name, val in {
                "I2_retry_bound"    : risk.I2_retry_bound,
                "I6_circuit_respect": risk.I6_circuit_respect,
                "I7_sla_breach"     : risk.I7_sla_breach,
            }.items() if val
        ]

    @staticmethod
    def _merge_theta_patch(current_theta: dict, patch: PolicyPatchSchema) -> dict:
        patch_data = patch.model_dump(exclude_none=True)
        logger.debug("[adaptation] merging theta patch: %s", patch_data)
        merged = dict(current_theta)
        merged.update(patch_data)
        logger.debug("[adaptation] merged theta preview: %s", {k: merged.get(k) for k in list(merged)[:10]})
        return merged