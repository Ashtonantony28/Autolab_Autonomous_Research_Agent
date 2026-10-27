"""Aggregate per-step verdicts into a single consistency_score.

Definition (ADR-0005, amended by ADR-0007):
    score = (#VALID + 0.5 * #UNGROUNDED) / N

INVALID contributes 0. UNGROUNDED contributes 0.5 because "we couldn't
prove it either way" is weaker than "we proved it valid" but stronger than
"we proved it invalid." Empty traces score 0.0.

A *blocked* trace (any INVALID step or, with `require_final_valid`, a final
step that is not VALID) is capped at `score_cap(pass_threshold,
retry_threshold)`: just below both configured thresholds, so it never scores
a pass at any threshold. Whether it is retried is the monitor's call
(retry-first, ADR-0007), not the score's.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from autolab.config import PASS_THRESHOLD, RETRY_THRESHOLD
from autolab.state.models import StepVerdict

_VALID_WEIGHT: Final[float] = 1.0
_UNGROUNDED_WEIGHT: Final[float] = 0.5
_INVALID_WEIGHT: Final[float] = 0.0
_CAP_MARGIN: Final[float] = 0.01


def score_cap(
    pass_threshold: float = PASS_THRESHOLD, retry_threshold: float = RETRY_THRESHOLD
) -> float:
    """The highest score a blocked trace may get: below both thresholds, and at least 0."""
    return max(0.0, min(pass_threshold, retry_threshold) - _CAP_MARGIN)


def consistency_score(
    verdicts: Sequence[StepVerdict],
    *,
    pass_threshold: float = PASS_THRESHOLD,
    retry_threshold: float = RETRY_THRESHOLD,
    require_final_valid: bool = False,
) -> float:
    """Return the aggregate consistency score for a list of step verdicts.

    Args:
        verdicts: per-step verdicts, in trace order.
        pass_threshold: the configured pass threshold the cap stays below.
        retry_threshold: the configured retry threshold the cap stays below.
        require_final_valid: also cap unless the last step is VALID (the trace
            is bound to a payload claim it must prove).
    """
    if not verdicts:
        return 0.0
    total = 0.0
    blocked = require_final_valid and verdicts[-1].verdict != "VALID"
    for v in verdicts:
        if v.verdict == "VALID":
            total += _VALID_WEIGHT
        elif v.verdict == "UNGROUNDED":
            total += _UNGROUNDED_WEIGHT
        else:
            total += _INVALID_WEIGHT
            blocked = True
    score = total / len(verdicts)
    return min(score, score_cap(pass_threshold, retry_threshold)) if blocked else score


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


__all__ = ["consistency_score", "score_cap", "summarize"]
