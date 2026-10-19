"""Golden reasoning traces — the headline soundness check for the verifier."""

from __future__ import annotations

import pytest

from autolab.config import PASS_THRESHOLD
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import ALL, GoldenTrace, build

MIN_CORPUS_SIZE = 50


def test_corpus_size_and_unique_ids() -> None:
    assert len(ALL) >= MIN_CORPUS_SIZE
    assert len({g.id for g in ALL}) == len(ALL)


@pytest.mark.parametrize("golden", ALL, ids=lambda g: g.id)
async def test_golden_trace(golden: GoldenTrace) -> None:
    trace, fake = build(golden)
    result = await Verifier(fake).verify(trace)
    details = [sv.details for sv in result.step_verdicts]

    actual = tuple(sv.verdict for sv in result.step_verdicts)
    assert actual == golden.expected_step_verdicts, (
        f"{golden.id}: expected {golden.expected_step_verdicts}, got {actual} ({details})"
    )
    if golden.expected_reasons:
        reasons = tuple(sv.reason for sv in result.step_verdicts)
        assert reasons == golden.expected_reasons, f"{golden.id}: reasons {reasons}"

    overall = "INVALID" if "INVALID" in actual else "VALID"
    assert overall == golden.expected_overall, f"{golden.id}: overall mismatch"
    if golden.expected_overall == "INVALID":
        assert result.consistency_score < PASS_THRESHOLD, f"{golden.id}: invalid trace passes"
    else:
        assert result.consistency_score >= PASS_THRESHOLD, f"{golden.id}: valid trace rejected"
