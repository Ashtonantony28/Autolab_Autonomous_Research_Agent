"""Identify premises that support a given reasoning step.

For step N in a trace, the candidate premises are:
  1. The formalized assertions of all *prior* steps in the trace (premises,
     inferences, and conclusions alike — a later step may legitimately
     build on either).
  2. Optional "extra" formulas the caller supplies, e.g. commonsense facts
     surfaced by an LLM call or constants declared by the surrounding
     research question.

A `premise` claim_type is self-grounded — the verifier treats it as VALID
by construction (we still record a verdict so the trace stays auditable).
"""

from __future__ import annotations

from collections.abc import Mapping

from autolab.state.models import ReasoningStep
from autolab.verification.solver import SmtFormula


def premises_for(
    step: ReasoningStep,
    formalized: Mapping[int, SmtFormula],
    *,
    extra: list[SmtFormula] | None = None,
) -> list[SmtFormula]:
    """Return the list of premise formulas supporting `step`.

    `formalized` maps step_index -> its SmtFormula for already-processed steps.
    Steps with index >= `step.index` are ignored.
    `extra` is additional commonsense / contextual formulas the caller wants
    to include.
    """
    seen: set[tuple[str, str]] = set()
    out: list[SmtFormula] = []

    for idx in sorted(formalized.keys()):
        if idx >= step.index:
            continue
        f = formalized[idx]
        key = (f.declarations, f.assertion)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)

    for f in extra or ():
        key = (f.declarations, f.assertion)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)

    return out


__all__ = ["premises_for"]
