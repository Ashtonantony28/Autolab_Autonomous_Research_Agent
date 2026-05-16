"""Confidence aggregation tests."""

from __future__ import annotations

import pytest

from autolab.state.models import StepVerdict, Verdict
from autolab.verification.confidence import consistency_score, summarize


def _v(idx: int, kind: Verdict) -> StepVerdict:
    return StepVerdict(step_index=idx, verdict=kind)


def test_empty_trace_scores_zero() -> None:
    assert consistency_score([]) == 0.0
    assert "no steps" in summarize([])


def test_all_valid_scores_one() -> None:
    verdicts = [_v(i, "VALID") for i in range(3)]
    assert consistency_score(verdicts) == 1.0


def test_all_invalid_scores_zero() -> None:
    verdicts = [_v(i, "INVALID") for i in range(3)]
    assert consistency_score(verdicts) == 0.0


def test_all_ungrounded_scores_half() -> None:
    verdicts = [_v(i, "UNGROUNDED") for i in range(4)]
    assert consistency_score(verdicts) == 0.5


def test_mixed_score() -> None:
    # 2 VALID, 1 INVALID, 1 UNGROUNDED -> (2 + 0.5) / 4 = 0.625
    verdicts = [_v(0, "VALID"), _v(1, "VALID"), _v(2, "INVALID"), _v(3, "UNGROUNDED")]
    assert consistency_score(verdicts) == pytest.approx(0.625)


def test_summary_includes_counts() -> None:
    verdicts = [_v(0, "VALID"), _v(1, "INVALID"), _v(2, "UNGROUNDED")]
    s = summarize(verdicts)
    assert "1 valid" in s
    assert "1 invalid" in s
    assert "1 ungrounded" in s
