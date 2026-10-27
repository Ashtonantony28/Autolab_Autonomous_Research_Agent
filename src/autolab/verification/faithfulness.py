"""Faithfulness check: does a step's SMT formalization say what its claim says?

The solver can only check the formula it is given. A formalizer that drops a
quantifier or flips an inequality would let a wrong step verify, so after a
step is formalized a judge back-translates the SMT assertion to English and
decides whether it states the same proposition as the original claim. A
mismatch, or a judge reply that cannot be used, fails closed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from autolab.state.models import ReasoningStep
from autolab.verification.llm import LLMClient
from autolab.verification.solver import SmtFormula


class FaithfulnessVerdict(BaseModel):
    """The judge's structured reply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    back_translation: str
    faithful: bool
    explanation: str = ""


def faithfulness_prompt(step: ReasoningStep, formula: SmtFormula) -> str:
    """Build the judge prompt. Stable string so `FakeLLMClient` lookups work."""
    return "\n".join(
        [
            "Does this SMT-LIB formalization faithfully state the original claim?",
            "",
            f"claim: {step.claim}",
            "",
            "declarations:",
            formula.declarations or "(none)",
            "",
            f"assertion: {formula.assertion}",
        ]
    )


async def unfaithful_reason(llm: LLMClient, step: ReasoningStep, formula: SmtFormula) -> str | None:
    """Return None if the judge accepts `formula` for `step`, else why not."""
    try:
        verdict = await llm.call_structured(
            "faithfulness", faithfulness_prompt(step, formula), FaithfulnessVerdict
        )
    except (ValidationError, RuntimeError) as exc:
        # Fail closed: an unusable judge reply must not let the step verify.
        return f"faithfulness judge failed: {exc}"
    if verdict.faithful:
        return None
    return (
        f"unfaithful formalization: SMT says {verdict.back_translation!r}, "
        f"claim says {step.claim!r}; {verdict.explanation}"
    )


__all__ = ["FaithfulnessVerdict", "faithfulness_prompt", "unfaithful_reason"]
