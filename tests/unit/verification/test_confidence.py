"""Confidence aggregation tests."""

from __future__ import annotations

import pytest

from autolab.config import PASS_THRESHOLD, RETRY_THRESHOLD
from autolab.state.models import StepVerdict, Verdict
from autolab.verification.confidence import consistency_score, score_cap, summarize


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


def test_mixed_score_without_invalid() -> None:
    # 3 VALID, 1 UNGROUNDED -> (3 + 0.5) / 4 = 0.875
    verdicts = [_v(0, "VALID"), _v(1, "VALID"), _v(2, "VALID"), _v(3, "UNGROUNDED")]
    assert consistency_score(verdicts) == pytest.approx(0.875)


def test_any_invalid_caps_score_below_both_thresholds() -> None:
    # 9 VALID + 1 INVALID would be 0.9 under the plain formula.
    verdicts = [_v(i, "VALID") for i in range(9)] + [_v(9, "INVALID")]
    score = consistency_score(verdicts)
    assert score == score_cap() == pytest.approx(RETRY_THRESHOLD - 0.01)
    assert score < RETRY_THRESHOLD < PASS_THRESHOLD


@pytest.mark.parametrize(
    ("pass_threshold", "retry_threshold"), [(0.5, 0.4), (0.3, 0.2), (0.3, 0.6), (0.95, 0.9)]
)
def test_cap_follows_the_configured_thresholds(
    pass_threshold: float, retry_threshold: float
) -> None:
    verdicts = [_v(i, "VALID") for i in range(9)] + [_v(9, "INVALID")]
    score = consistency_score(
        verdicts, pass_threshold=pass_threshold, retry_threshold=retry_threshold
    )
    assert score == score_cap(pass_threshold, retry_threshold)
    assert score < min(pass_threshold, retry_threshold)


def test_cap_is_never_negative() -> None:
    assert score_cap(0.0, 0.0) == 0.0
    assert consistency_score([_v(0, "INVALID")], pass_threshold=0.0) == 0.0


def test_require_final_valid_caps_an_unproven_conclusion() -> None:
    verdicts = [_v(i, "VALID") for i in range(9)] + [_v(9, "UNGROUNDED")]
    assert consistency_score(verdicts) == pytest.approx(0.95)
    assert consistency_score(verdicts, require_final_valid=True) == score_cap()


def test_require_final_valid_keeps_a_proven_conclusion() -> None:
    verdicts = [_v(0, "UNGROUNDED"), _v(1, "VALID"), _v(2, "VALID")]
    assert consistency_score(verdicts, require_final_valid=True) == pytest.approx(2.5 / 3)


def test_cap_never_raises_a_low_score() -> None:
    verdicts = [_v(0, "VALID"), _v(1, "INVALID"), _v(2, "INVALID"), _v(3, "INVALID")]
    assert consistency_score(verdicts) == pytest.approx(0.25)


def test_summary_includes_counts() -> None:
    verdicts = [_v(0, "VALID"), _v(1, "INVALID"), _v(2, "UNGROUNDED")]
    s = summarize(verdicts)
    assert "1 valid" in s
    assert "1 invalid" in s
    assert "1 ungrounded" in s
