"""VeriCoT pipeline — verify a ReasoningTrace step-by-step with Z3.

Public entry point:

    verifier = Verifier(llm=FakeLLMClient(...))
    result = await verifier.verify(trace)

For each step:
  1. Formalize the step into SMT (using the growing vocabulary).
  2. Collect premises (prior steps + caller-supplied extras).
  3. Ask Z3 whether `premises ⊨ claim`.
  4. Record a `StepVerdict`. `premise`-typed steps are tagged VALID by
     construction since they're definitions, not inferences.

A formalization failure on a step yields an UNGROUNDED verdict — the
trace remains verifiable as a whole even if one step's LLM output was
malformed.
"""

from __future__ import annotations

from autolab.state.models import (
    ReasoningStep,
    ReasoningTrace,
    StepVerdict,
    VerificationResult,
)
from autolab.verification.confidence import consistency_score, summarize
from autolab.verification.formalizer import FormalizationError, Formalizer
from autolab.verification.llm import LLMClient
from autolab.verification.premise_grounder import premises_for
from autolab.verification.solver import SmtFormula, Z3Solver


class Verifier:
    """High-level VeriCoT verifier."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        solver_timeout_seconds: float = 5.0,
    ) -> None:
        self._llm = llm
        self._solver = Z3Solver(timeout_seconds=solver_timeout_seconds)

    async def verify(
        self,
        trace: ReasoningTrace,
        *,
        extra_premises: list[SmtFormula] | None = None,
    ) -> VerificationResult:
        """Run the pipeline and return a VerificationResult."""
        formalizer = Formalizer(self._llm)
        formalized: dict[int, SmtFormula] = {}
        verdicts: list[StepVerdict] = []

        for step in trace.steps:
            verdict, formula = await self._verify_step(
                step,
                formalizer=formalizer,
                formalized=formalized,
                extra_premises=extra_premises or [],
            )
            verdicts.append(verdict)
            if formula is not None:
                formalized[step.index] = formula

        score = consistency_score(verdicts)
        return VerificationResult(
            trace_id=trace.id,
            step_verdicts=tuple(verdicts),
            consistency_score=score,
            summary=summarize(verdicts),
        )

    async def _verify_step(
        self,
        step: ReasoningStep,
        *,
        formalizer: Formalizer,
        formalized: dict[int, SmtFormula],
        extra_premises: list[SmtFormula],
    ) -> tuple[StepVerdict, SmtFormula | None]:
        try:
            formula = await formalizer.formalize(step)
        except FormalizationError as exc:
            return (
                StepVerdict(
                    step_index=step.index,
                    verdict="UNGROUNDED",
                    details=f"formalization failed: {exc}",
                ),
                None,
            )

        if step.claim_type == "premise":
            return (
                StepVerdict(
                    step_index=step.index,
                    verdict="VALID",
                    details="premise; assumed",
                ),
                formula,
            )

        premises = premises_for(step, formalized, extra=extra_premises)
        outcome = self._solver.check_entailment(premises, formula)
        return (
            StepVerdict(
                step_index=step.index,
                verdict=outcome.verdict,
                details=outcome.details,
            ),
            formula,
        )


__all__ = ["Verifier"]
