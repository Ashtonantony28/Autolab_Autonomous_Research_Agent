"""Identify premises that support a given reasoning step.

For step N in a trace, the candidate premises are:
  1. Formalized assertions of *prior verified* steps. The caller passes only
     VALID steps (assumed premises and entailed inferences) in `formalized`,
     so INVALID and UNGROUNDED steps never support later ones (ADR-0007).
     If the step cites `referenced_step_indices`, only those steps are used;
     otherwise every prior verified step is.
  2. Optional "extra" formulas the caller supplies, e.g. grounded context
     facts (measurements, citations). These are always included.

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

    `formalized` maps step_index -> SmtFormula for already-verified steps.
    Steps with index >= `step.index` are ignored; when `step` has references,
    only the referenced indices present in `formalized` are used.
    `extra` is additional contextual formulas the caller wants to include.
    """
    cited = set(step.referenced_step_indices)
    seen: set[tuple[str, str]] = set()
    out: list[SmtFormula] = []

    candidates = [
        formalized[idx]
        for idx in sorted(formalized)
        if idx < step.index and (not cited or idx in cited)
    ]
    for f in (*candidates, *(extra or ())):
        key = (f.declarations, f.assertion)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)

    return out


__all__ = ["premises_for"]
