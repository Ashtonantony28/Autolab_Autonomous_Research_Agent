"""Per-category accuracy of the verifier with real (Claude) formalization.

Runs every case in `tests.live.corpus.CASES` through a `Verifier` whose
formalizer and faithfulness judge call Claude, and prints how many traces
per category land on the right side of `PASS_THRESHOLD`, plus the
misclassified ids. Exits 1 below the SG5 target (90% overall), 2 without
`ANTHROPIC_API_KEY`.

Run with `uv run python -m scripts.live_accuracy` (makes real API calls).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from autolab.config import PASS_THRESHOLD, get_settings
from autolab.orchestrator.recovery import RetryingLLMClient
from autolab.verification.llm import ClaudeLLMClient, LLMClient
from autolab.verification.vericot import Verifier
from tests.live.corpus import CASES, LiveCase

TARGET_ACCURACY: Final[float] = 0.9
CONCURRENCY: Final[int] = 4


@dataclass(frozen=True)
class AccuracyReport:
    """Correct/total per category and the ids that were misclassified."""

    per_category: dict[str, tuple[int, int]]
    misclassified: tuple[str, ...]

    @property
    def accuracy(self) -> float:
        """Correct traces over all traces."""
        correct = sum(c for c, _ in self.per_category.values())
        total = sum(t for _, t in self.per_category.values())
        return correct / max(total, 1)


async def classify(verifier: Verifier, case: LiveCase) -> bool:
    """True if the verifier's pass/fail decision matches the golden expectation."""
    result = await verifier.verify(case.trace())
    passed = result.consistency_score >= PASS_THRESHOLD
    return passed == (case.golden.expected_overall == "VALID")


async def run(llm: LLMClient, cases: Sequence[LiveCase] = CASES) -> AccuracyReport:
    """Verify `cases` with `llm` (faithfulness on) and tally per category."""
    verifier = Verifier(llm, check_faithfulness=True)
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(case: LiveCase) -> bool:
        async with gate:
            return await classify(verifier, case)

    outcomes = await asyncio.gather(*(one(c) for c in cases))
    per_category: dict[str, tuple[int, int]] = {}
    for case, ok in zip(cases, outcomes, strict=True):
        correct, total = per_category.get(case.category, (0, 0))
        per_category[case.category] = (correct + ok, total + 1)
    missed = tuple(c.id for c, ok in zip(cases, outcomes, strict=True) if not ok)
    return AccuracyReport(per_category, missed)


def format_report(r: AccuracyReport) -> str:
    """Render one line per category, the overall figure and the misclassified ids."""
    lines = [f"{cat:<16} {c}/{t}" for cat, (c, t) in r.per_category.items()]
    lines.append(f"Overall accuracy: {100 * r.accuracy:.1f}% (target {100 * TARGET_ACCURACY:.0f}%)")
    lines.append(f"Misclassified: {', '.join(r.misclassified) or 'none'}")
    return "\n".join(lines)


def main() -> None:
    """Run the live corpus against Claude and print the report."""
    api_key = get_settings().anthropic_api_key
    if not api_key:
        sys.stderr.write("live_accuracy: ANTHROPIC_API_KEY is not set.\n")
        raise SystemExit(2)
    report = asyncio.run(run(RetryingLLMClient(ClaudeLLMClient(api_key))))
    print(format_report(report))
    if report.accuracy < TARGET_ACCURACY:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
