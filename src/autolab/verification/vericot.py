"""VeriCoT pipeline — verify a ReasoningTrace step-by-step with Z3.

Public entry point:

    verifier = Verifier(llm=FakeLLMClient(...))
    result = await verifier.verify(trace)

Every step is formalized first (the vocabulary grows in trace order), then
checked in order:
  - a `premise` is assumed (VALID/`assumed`) only if it is consistent with
    `context.facts` and every prior VALID step; otherwise it is INVALID with
    reason `premises_inconsistent` (AUDIT-1 #4).
  - any other step must be entailed by its premise pool: prior VALID steps
    (only the cited ones when it has `referenced_step_indices`) plus
    `context.facts` (ADR-0007).

The Z3 check runs on a worker thread (`asyncio.to_thread`), so a slow check
never blocks the event loop and concurrent `verify` calls overlap.
`StepVerdict.solver_ms` is the wall time of that check alone.

Faithfulness (on by default for a live LLM): after a step is formalized, a
judge back-translates its SMT and checks it states the claim; a mismatch is
UNGROUNDED with reason `formalization_failed` (`faithfulness.py`).

A formalization failure on a step yields an UNGROUNDED verdict — the
trace remains verifiable as a whole even if one step's LLM output was
malformed.

Payload binding: when `context.payload_claim` is set, the trace's last step
must be a `conclusion` whose claim equals it after whitespace and case
normalization. Otherwise nothing is formalized; every step is UNGROUNDED
with reason `no_premises` and the score is 0.0, so the trace cannot pass at
any threshold. A bound trace must also prove its conclusion: the conclusion
may not be a tautology, and no single premise may restate the payload claim
or entail the conclusion on its own (circular; such premises are UNGROUNDED
and the trace is re-checked without them). A bound trace whose last step is
not VALID is capped below both thresholds (`confidence.score_cap`).
"""

from __future__ import annotations

import asyncio
import time

from autolab.config import PASS_THRESHOLD, RETRY_THRESHOLD, SOLVER_TIMEOUT_S
from autolab.state.models import (
    ReasoningStep,
    ReasoningTrace,
    StepReason,
    StepVerdict,
    VerificationContext,
    VerificationResult,
)
from autolab.verification.confidence import consistency_score, summarize
from autolab.verification.faithfulness import unfaithful_reason
from autolab.verification.formalizer import FormalizationError, Formalizer
from autolab.verification.llm import LLMClient, is_live
from autolab.verification.premise_grounder import premises_for
from autolab.verification.solver import SmtFormula, Z3Solver

_TRUE = SmtFormula("", "true")

StepFormula = SmtFormula | StepVerdict
"""A formalized step, or the UNGROUNDED verdict of a step that failed to formalize."""


class Verifier:
    """High-level VeriCoT verifier."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        solver_timeout_seconds: float = SOLVER_TIMEOUT_S,
        check_faithfulness: bool | None = None,
        pass_threshold: float = PASS_THRESHOLD,
        retry_threshold: float = RETRY_THRESHOLD,
    ) -> None:
        """`check_faithfulness=None` turns the check on only for a live LLM (`is_live`).

        The thresholds are the configured ones; a blocked trace's score is capped below
        both (`confidence.score_cap`).
        """
        self._llm = llm
        self._check_faithfulness = (
            is_live(llm) if check_faithfulness is None else check_faithfulness
        )
        self._solver = Z3Solver(timeout_seconds=solver_timeout_seconds)
        self._pass_threshold = pass_threshold
        self._retry_threshold = retry_threshold

    async def verify(
        self,
        trace: ReasoningTrace,
        *,
        context: VerificationContext | None = None,
    ) -> VerificationResult:
        """Run the pipeline and return a VerificationResult.

        `context.facts` are added to every step's premises and their
        declarations seed the formalizer's vocabulary.
        """
        payload_claim = context.payload_claim if context else None
        if payload_claim is not None:
            unbound = _unbound_result(trace, payload_claim)
            if unbound is not None:
                return unbound
        formalizer = Formalizer(self._llm)
        facts = context.facts if context else ()
        for fact in facts:
            formalizer.vocabulary.merge(fact.declarations)
        extra_premises = [SmtFormula(f.declarations, f.assertion) for f in facts]
        formulas = [await self._formalize(step, formalizer) for step in trace.steps]

        verdicts = await self._check_steps(trace, formulas, extra_premises, {})
        if payload_claim is not None and verdicts[-1].verdict == "VALID":
            overrides = await self._payload_overrides(trace, formulas, verdicts, payload_claim)
            if overrides:
                verdicts = await self._check_steps(trace, formulas, extra_premises, overrides)

        score = consistency_score(
            verdicts,
            pass_threshold=self._pass_threshold,
            retry_threshold=self._retry_threshold,
            require_final_valid=payload_claim is not None,
        )
        return VerificationResult(
            trace_id=trace.id,
            step_verdicts=tuple(verdicts),
            consistency_score=score,
            summary=summarize(verdicts),
        )

    async def _formalize(self, step: ReasoningStep, formalizer: Formalizer) -> StepFormula:
        try:
            formula = await formalizer.formalize(step)
        except FormalizationError as exc:
            return _formalization_failed(step, f"formalization failed: {exc}")
        if self._check_faithfulness:
            unfaithful = await unfaithful_reason(self._llm, step, formula)
            if unfaithful is not None:
                return _formalization_failed(step, unfaithful)
        return formula

    async def _check_steps(
        self,
        trace: ReasoningTrace,
        formulas: list[StepFormula],
        extra_premises: list[SmtFormula],
        overrides: dict[int, StepVerdict],
    ) -> list[StepVerdict]:
        """Check every step in order; `overrides` fixes some steps' verdicts up front."""
        formalized: dict[int, SmtFormula] = {}
        verdicts: list[StepVerdict] = []
        for step, formula in zip(trace.steps, formulas, strict=True):
            if isinstance(formula, StepVerdict):
                verdict = formula
            elif step.index in overrides:
                verdict = overrides[step.index]
            elif step.claim_type == "premise":
                # Assumed, but only if consistent with the facts and every prior VALID step.
                pool = [*formalized.values(), *extra_premises, formula]
                verdict = await self._check(step, pool, _TRUE)
                if verdict.verdict == "VALID":
                    verdict = verdict.model_copy(
                        update={"reason": "assumed", "details": "premise; assumed"}
                    )
            else:
                pool = premises_for(step, formalized, extra=extra_premises)
                verdict = await self._check(step, pool, formula)
            verdicts.append(verdict)
            # Only VALID steps (assumed premises, entailed inferences) may support later steps.
            if isinstance(formula, SmtFormula) and verdict.verdict == "VALID":
                formalized[step.index] = formula
        return verdicts

    async def _check(
        self, step: ReasoningStep, premises: list[SmtFormula], claim: SmtFormula
    ) -> StepVerdict:
        started = time.perf_counter()
        outcome = await asyncio.to_thread(self._solver.check_entailment, premises, claim)
        return StepVerdict(
            step_index=step.index,
            verdict=outcome.verdict,
            reason=outcome.reason,
            details=outcome.details,
            solver_ms=(time.perf_counter() - started) * 1000,
        )

    async def _payload_overrides(
        self,
        trace: ReasoningTrace,
        formulas: list[StepFormula],
        verdicts: list[StepVerdict],
        payload_claim: str,
    ) -> dict[int, StepVerdict]:
        """Verdicts that stop a VALID payload conclusion from proving itself.

        A tautological conclusion says nothing about the claim (AUDIT-1 #5). A
        premise that restates the payload claim, or entails the conclusion on its
        own, assumes what the trace must prove (AUDIT-1 #1); it is UNGROUNDED, so it
        and every step resting on it leave the premise pool. An inconclusive check
        fails closed.
        """
        last, conclusion = trace.steps[-1], formulas[-1]
        if not isinstance(conclusion, SmtFormula):
            return {}
        trivial = await self._check(last, [_TRUE], conclusion)
        if trivial.verdict != "INVALID":
            reason: StepReason = trivial.reason or "timeout"
            details = f"could not rule out a tautology: {trivial.details}"
            if trivial.verdict == "VALID":
                reason, details = "formalization_failed", "formalization is a tautology"
            return {last.index: _ungrounded(last, reason, details)}
        overrides: dict[int, StepVerdict] = {}
        for step, formula, verdict in zip(trace.steps, formulas, verdicts, strict=True):
            if (
                step.claim_type != "premise"
                or verdict.verdict != "VALID"
                or not isinstance(formula, SmtFormula)
            ):
                continue
            reason = "no_premises"
            if _normalize(step.claim) == _normalize(payload_claim):
                details = "premise restates the payload claim (circular)"
            else:
                alone = await self._check(step, [formula], conclusion)
                if alone.verdict == "INVALID":
                    continue
                details = "premise alone entails the payload conclusion (circular)"
                if alone.verdict == "UNGROUNDED":
                    reason = alone.reason or "timeout"
                    details = f"could not rule out a circular premise: {alone.details}"
            overrides[step.index] = _ungrounded(step, reason, details)
        return overrides


def _ungrounded(step: ReasoningStep, reason: StepReason, details: str) -> StepVerdict:
    return StepVerdict(step_index=step.index, verdict="UNGROUNDED", reason=reason, details=details)


def _formalization_failed(step: ReasoningStep, details: str) -> StepVerdict:
    return _ungrounded(step, "formalization_failed", details)


def _normalize(claim: str) -> str:
    return " ".join(claim.split()).casefold()


def _unbound_result(trace: ReasoningTrace, payload_claim: str) -> VerificationResult | None:
    """Return a failing result if `trace` does not conclude `payload_claim`, else None."""
    last = trace.steps[-1] if trace.steps else None
    if (
        last is not None
        and last.claim_type == "conclusion"
        and _normalize(last.claim) == _normalize(payload_claim)
    ):
        return None
    details = f"trace does not conclude payload claim {payload_claim!r}"
    verdicts = tuple(
        StepVerdict(step_index=s.index, verdict="UNGROUNDED", reason="no_premises", details=details)
        for s in trace.steps
    )
    return VerificationResult(
        trace_id=trace.id,
        step_verdicts=verdicts,
        consistency_score=0.0,
        summary=f"{details}; {summarize(verdicts)}",
    )


__all__ = ["Verifier"]
