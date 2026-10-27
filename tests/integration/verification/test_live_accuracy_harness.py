"""Offline checks of the live corpus and the per-category accuracy script."""

from __future__ import annotations

import pytest

from autolab.config import Settings
from autolab.verification.faithfulness import FaithfulnessVerdict
from autolab.verification.llm import LLMPurpose, SchemaT
from scripts.live_accuracy import AccuracyReport, format_report, main, run
from tests.fixtures.golden_traces import build
from tests.live.corpus import CASES, CATEGORIES, ONE_PER_CATEGORY, LiveCase

PLACEHOLDERS = {"premise", "inference", "conclusion"}


class _Corpus:
    """Answers each case's formalize prompts from its golden fixture; the judge always agrees."""

    def __init__(self, cases: tuple[LiveCase, ...]) -> None:
        self._fakes = [build(c.golden)[1] for c in cases]

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        for fake in self._fakes:
            try:
                return await fake.call(purpose, prompt)
            except KeyError:
                continue
        raise KeyError((purpose, prompt))

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        if purpose == "faithfulness":
            return schema.model_validate(FaithfulnessVerdict(back_translation="", faithful=True))
        return schema.model_validate_json(await self.call(purpose, prompt))


def test_corpus_has_natural_language_claims_in_every_category() -> None:
    assert set(CATEGORIES) >= {"valid", "invalid", "explosion", "arithmetic", "quantifiers"}
    assert [c.category for c in ONE_PER_CATEGORY] == list(CATEGORIES)
    assert len({c.id for c in CASES}) == len(CASES)
    for case in CASES:
        claims = [s.claim for s in case.trace().steps]
        assert not PLACEHOLDERS & set(claims), case.id
        assert [s.claim_type for s in case.trace().steps] == [
            s.claim_type for s in case.golden.steps
        ]


def test_claim_count_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="claims for"):
        LiveCase("x", CASES[0].golden, ("only one",)).trace()


@pytest.mark.parametrize("case", [c for c in CASES if not c.claims], ids=lambda c: c.id)
async def test_fixture_formalizations_classify_correctly(case: LiveCase) -> None:
    report = await run(_Corpus((case,)), (case,))
    assert report.per_category == {case.category: (1, 1)} and report.misclassified == ()


async def test_run_tallies_per_category() -> None:
    cases = tuple(
        c for c in CASES if c.id in {"v02-syllogism", "v03-transitivity", "i04-arith-wrong"}
    )
    report = await run(_Corpus(cases), cases)
    assert report.per_category == {"valid": (2, 2), "invalid": (1, 1)}
    assert "Overall accuracy: 100.0%" in format_report(report)


def test_report_lists_misclassifications() -> None:
    report = AccuracyReport({"valid": (9, 10), "invalid": (8, 10)}, ("v01", "i01", "i02"))
    assert report.accuracy == pytest.approx(0.85)
    text = format_report(report)
    assert "valid            9/10" in text and "Misclassified: v01, i01, i02" in text


def test_main_without_api_key_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.live_accuracy.get_settings", lambda: Settings(anthropic_api_key="")
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
