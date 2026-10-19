"""Aggregate per-step verdicts into a single consistency_score.

Definition (ADR-0005, amended by ADR-0007):
    score = (#VALID + 0.5 * #UNGROUNDED) / N

INVALID contributes 0. UNGROUNDED contributes 0.5 because "we couldn't
prove it either way" is weaker than "we proved it valid" but stronger than
"we proved it invalid." Empty traces score 0.0.

Any INVALID step caps the score at `INVALID_SCORE_CAP`, just below
`PASS_THRESHOLD`, so a refuted step can never be diluted into a pass by
surrounding VALID steps, but lands in the retry band (SG4 retry-first
default; set the cap below `RETRY_THRESHOLD` to escalate instead).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from autolab.config import PASS_THRESHOLD
from autolab.state.models import StepVerdict

_VALID_WEIGHT: Final[float] = 1.0
_UNGROUNDED_WEIGHT: Final[float] = 0.5
_INVALID_WEIGHT: Final[float] = 0.0
_INVALID_CAP_MARGIN: Final[float] = 0.01
INVALID_SCORE_CAP: Final[float] = PASS_THRESHOLD - _INVALID_CAP_MARGIN


def consistency_score(verdicts: Sequence[StepVerdict]) -> float:
    """Return the aggregate consistency score for a list of step verdicts."""
    if not verdicts:
        return 0.0
    total = 0.0
    has_invalid = False
    for v in verdicts:
        if v.verdict == "VALID":
            total += _VALID_WEIGHT
        elif v.verdict == "UNGROUNDED":
            total += _UNGROUNDED_WEIGHT
        else:
            total += _INVALID_WEIGHT
            has_invalid = True
    score = total / len(verdicts)
    return min(score, INVALID_SCORE_CAP) if has_invalid else score


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


__all__ = ["INVALID_SCORE_CAP", "consistency_score", "summarize"]
