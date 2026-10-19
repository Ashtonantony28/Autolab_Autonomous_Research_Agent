"""Verifier latency benchmark over the golden corpus.

Runs every golden trace (`tests.fixtures.golden_traces.ALL`) `--runs` times
through the Verifier with its FakeLLMClient and reports Z3 wall time
(`StepVerdict.solver_ms`): p50, p95 and max per checked step (premises and
formalization failures never reach the solver, so they are excluded) and per
trace total. Exits 1 if the per-step p95 exceeds the budget (`STEP_BUDGET_MS`).

Run directly (`python -m scripts.bench_verifier --runs 20`) or import `run`.
"""

from __future__ import annotations

import argparse
import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from autolab.config import STEP_BUDGET_MS
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import ALL, build

_UNSOLVED: Final[frozenset[str]] = frozenset({"assumed", "formalization_failed"})
P50: Final[float] = 0.50
P95: Final[float] = 0.95
DEFAULT_RUNS: Final[int] = 20


def percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of `samples` (0.0 if empty)."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


@dataclass(frozen=True)
class BenchReport:
    """Solver wall times (ms) collected over `runs` passes of the corpus."""

    runs: int
    step_ms: tuple[float, ...]
    trace_total_ms: tuple[float, ...]

    def within_budget(self, budget_ms: float) -> bool:
        """True if the per-step p95 is at most `budget_ms`."""
        return percentile(self.step_ms, P95) <= budget_ms


async def run(runs: int) -> BenchReport:
    """Verify every golden trace `runs` times and collect solver timings."""
    step_ms: list[float] = []
    trace_total_ms: list[float] = []
    for _ in range(runs):
        for golden in ALL:
            trace, fake = build(golden, agent="bench")
            result = await Verifier(fake).verify(trace)
            timed = [v.solver_ms for v in result.step_verdicts if v.reason not in _UNSOLVED]
            step_ms.extend(timed)
            trace_total_ms.append(sum(timed))
    return BenchReport(runs, tuple(step_ms), tuple(trace_total_ms))


def format_report(r: BenchReport, budget_ms: float = STEP_BUDGET_MS) -> str:
    """Render p50 / p95 / max for steps and trace totals as printable lines."""

    def row(label: str, xs: Sequence[float]) -> str:
        return (
            f"{label:<6} n={len(xs):<5} p50={percentile(xs, P50):8.2f} ms  "
            f"p95={percentile(xs, P95):8.2f} ms  max={max(xs, default=0.0):8.2f} ms"
        )

    verdict = "within" if r.within_budget(budget_ms) else "OVER"
    return "\n".join(
        (
            f"Verifier solver latency: {len(ALL)} golden traces x {r.runs} runs",
            row("step", r.step_ms),
            row("total", r.trace_total_ms),
            f"Budget: step p95 <= {budget_ms:.1f} ms ({verdict} budget)",
        )
    )


async def amain(argv: Sequence[str] | None = None) -> int:
    """Parse args, run the benchmark, print the report; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--budget-ms", type=float, default=STEP_BUDGET_MS)
    args = parser.parse_args(argv)
    report = await run(args.runs)
    print(format_report(report, args.budget_ms))
    return 0 if report.within_budget(args.budget_ms) else 1


def main() -> None:
    """CLI entry point."""
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
