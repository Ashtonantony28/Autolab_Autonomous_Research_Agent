"""Golden traces verified end to end with real formalization and the faithfulness judge."""

from __future__ import annotations

import pytest

from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier
from scripts.live_accuracy import TARGET_ACCURACY, classify, format_report, run
from tests.live.corpus import ONE_PER_CATEGORY, LiveCase


@pytest.mark.parametrize("case", ONE_PER_CATEGORY, ids=lambda c: f"{c.category}-{c.id}")
async def test_one_golden_trace_per_category(case: LiveCase, claude: LLMClient) -> None:
    assert await classify(Verifier(claude, check_faithfulness=True), case), case.id


async def test_live_corpus_accuracy(claude: LLMClient) -> None:
    report = await run(claude)
    print(format_report(report))
    assert report.accuracy >= TARGET_ACCURACY, format_report(report)
