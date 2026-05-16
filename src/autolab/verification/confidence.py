"""Aggregate per-step verdicts into a single consistency_score.

Definition (intentionally simple, ADR-0005):
    score = (#VALID + 0.5 * #UNGROUNDED) / N

INVALID contributes 0. UNGROUNDED contributes 0.5 because "we couldn't
prove it either way" is weaker than "we proved it valid" but stronger than
"we proved it invalid." Empty traces score 0.0.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from autolab.state.models import StepVerdict

_VALID_WEIGHT: Final[float] = 1.0
_UNGROUNDED_WEIGHT: Final[float] = 0.5
_INVALID_WEIGHT: Final[float] = 0.0


def consistency_score(verdicts: Sequence[StepVerdict]) -> float:
    """Return the aggregate consistency score for a list of step verdicts."""
    if not verdicts:
        return 0.0
    total = 0.0
    for v in verdicts:
        if v.verdict == "VALID":
            total += _VALID_WEIGHT
        elif v.verdict == "UNGROUNDED":
            total += _UNGROUNDED_WEIGHT
        else:
            total += _INVALID_WEIGHT
    return total / len(verdicts)


def summarize(verdicts: Sequence[StepVerdict]) -> str:
    """One-line human-readable summary of a verdict set."""
    if not verdicts:
        return "no steps verified"
    counts = {"VALID": 0, "INVALID": 0, "UNGROUNDED": 0}
    for v in verdicts:
        counts[v.verdict] += 1
    return (
        f"{counts['VALID']} valid, {counts['INVALID']} invalid, "
        f"{counts['UNGROUNDED']} ungrounded across {len(verdicts)} steps"
    )


__all__ = ["consistency_score", "summarize"]
