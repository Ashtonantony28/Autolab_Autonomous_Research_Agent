"""Verifier error-injection harness.

Runs every golden trace (`tests.fixtures.golden_traces.ALL`: the INVALID
traces carry deliberately injected reasoning errors, the VALID ones are
their controls) through the Verifier and reports:

- catch rate (recall): INVALID traces flagged / all INVALID traces;
- precision: INVALID traces flagged / all traces flagged;
- VALID traces admitted.

A trace is *flagged* when its `consistency_score < PASS_THRESHOLD`, i.e. the
orchestrator would not let it through. The SG1 target is 100% catch rate
with every VALID trace admitted.

Run directly (`python -m scripts.error_injection`) or import for tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from autolab.config import PASS_THRESHOLD
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import ALL, build


@dataclass(frozen=True)
class CatchRateReport:
    """Tally of verifier outcomes over the golden corpus."""

    invalid_total: int
    valid_total: int
    invalid_caught: int
    valid_flagged: int

    @property
    def valid_classified_valid(self) -> int:
        """VALID traces that scored at or above the pass threshold."""
        return self.valid_total - self.valid_flagged

    @property
    def catch_rate(self) -> float:
        """Recall: flagged INVALID traces over all INVALID traces."""
        return self.invalid_caught / max(self.invalid_total, 1)

    @property
    def precision(self) -> float:
        """Flagged INVALID traces over all flagged traces (1.0 if none flagged)."""
        flagged = self.invalid_caught + self.valid_flagged
        return self.invalid_caught / flagged if flagged else 1.0


async def run() -> CatchRateReport:
    """Run every golden trace through the Verifier and tally results."""
    invalid_total = valid_total = invalid_caught = valid_flagged = 0
    for golden in ALL:
        trace, fake = build(golden, agent="bench")
        result = await Verifier(fake).verify(trace)
        flagged = result.consistency_score < PASS_THRESHOLD
        if golden.expected_overall == "INVALID":
            invalid_total += 1
            invalid_caught += flagged
        else:
            valid_total += 1
            valid_flagged += flagged
    return CatchRateReport(invalid_total, valid_total, invalid_caught, valid_flagged)


def format_report(r: CatchRateReport) -> str:
    """Render catch rate, precision and VALID admission as printable lines."""
    return "\n".join(
        (
            f"Catch rate: {r.invalid_caught}/{r.invalid_total} ({100 * r.catch_rate:.1f}%)",
            f"Precision: {100 * r.precision:.1f}%",
            f"Valid traces correctly admitted: {r.valid_classified_valid}/{r.valid_total}",
        )
    )


def main() -> None:
    """Print the error-injection report for the golden corpus."""
    print(format_report(asyncio.run(run())))


if __name__ == "__main__":
    main()
