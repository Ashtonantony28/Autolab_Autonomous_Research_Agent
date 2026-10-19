"""SG2: solver latency, timeout honesty and event-loop friendliness."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Final

from autolab.config import SOLVER_TIMEOUT_S, STEP_BUDGET_MS
from autolab.state.models import ReasoningStep, ReasoningTrace
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient, SchemaT
from autolab.verification.solver import SmtFormula, Z3Solver
from autolab.verification.vericot import Verifier
from scripts import bench_verifier
from tests.fixtures.golden_traces import ALL, build

_XYZ: Final = "(declare-const x Int)\n(declare-const y Int)\n(declare-const z Int)"
_POSITIVE: Final = "(and (> x 0) (> y 0) (> z 0))"
# Fermat n=3: true, but beyond what Z3's nonlinear integer tactics can prove.
_NO_CUBES: Final = "(not (= (+ (* x x x) (* y y y)) (* z z z)))"
_SLOW_TIMEOUT_S: Final = 1.0
_SLACK_S: Final = 1.0


def _pathological_trace() -> tuple[ReasoningTrace, FakeLLMClient]:
    steps = (
        ReasoningStep(index=0, claim="x, y, z are positive", claim_type="premise"),
        ReasoningStep(index=1, claim="x^3 + y^3 != z^3", claim_type="conclusion"),
    )
    fake = FakeLLMClient()
    vocab = Vocabulary()
    fake.register(
        "formalize",
        prompt_for(steps[0], vocab),
        json.dumps({"declarations": _XYZ, "assertion": _POSITIVE}),
    )
    vocab.merge(_XYZ)
    fake.register(
        "formalize",
        prompt_for(steps[1], vocab),
        json.dumps({"declarations": "", "assertion": _NO_CUBES}),
    )
    return ReasoningTrace(agent="latency", steps=steps), fake


class _SlowLLM(FakeLLMClient):
    """FakeLLMClient whose structured calls take `delay_s` each."""

    def __init__(self, inner: FakeLLMClient, delay_s: float) -> None:
        super().__init__()
        self._inner = inner
        self._delay_s = delay_s

    async def call_structured(self, purpose: str, prompt: str, schema: type[SchemaT]) -> SchemaT:
        await asyncio.sleep(self._delay_s)
        return await self._inner.call_structured(purpose, prompt, schema)


def test_verifier_defaults_to_configured_timeout() -> None:
    assert Verifier(FakeLLMClient())._solver.timeout_seconds == SOLVER_TIMEOUT_S


async def test_solver_ms_excludes_formalization() -> None:
    delay_s = 0.2
    golden = next(g for g in ALL if g.expected_overall == "VALID")
    trace, fake = build(golden)
    result = await Verifier(_SlowLLM(fake, delay_s)).verify(trace)
    timed = [v.solver_ms for v in result.step_verdicts if v.reason != "assumed"]
    assert timed
    assert all(0 < ms < delay_s * 1000 for ms in timed)


async def test_pathological_formula_times_out_to_ungrounded() -> None:
    trace, fake = _pathological_trace()
    started = time.perf_counter()
    result = await Verifier(fake, solver_timeout_seconds=_SLOW_TIMEOUT_S).verify(trace)
    elapsed = time.perf_counter() - started
    last = result.step_verdicts[-1]
    assert (last.verdict, last.reason) == ("UNGROUNDED", "timeout")
    assert elapsed < _SLOW_TIMEOUT_S + _SLACK_S
    assert last.solver_ms >= _SLOW_TIMEOUT_S * 1000 * 0.9


def test_hard_premise_consistency_check_times_out() -> None:
    hard = f"(and {_POSITIVE} (= (+ (* x x x) (* y y y)) (* z z z)))"
    started = time.perf_counter()
    out = Z3Solver(timeout_seconds=_SLOW_TIMEOUT_S).check_entailment(
        [SmtFormula(_XYZ, hard)], SmtFormula(_XYZ, "false")
    )
    elapsed = time.perf_counter() - started
    assert (out.verdict, out.reason) == ("UNGROUNDED", "timeout")
    assert elapsed < _SLOW_TIMEOUT_S + _SLACK_S


async def test_event_loop_stays_responsive_during_slow_check() -> None:
    tick_s = 0.05
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(tick_s)
            ticks += 1

    trace, fake = _pathological_trace()
    task = asyncio.create_task(ticker())
    await Verifier(fake, solver_timeout_seconds=_SLOW_TIMEOUT_S).verify(trace)
    done.set()
    await task
    assert ticks >= (_SLOW_TIMEOUT_S / tick_s) / 2


async def test_concurrent_traces_do_not_serialize() -> None:
    (t1, f1), (t2, f2) = _pathological_trace(), _pathological_trace()
    started = time.perf_counter()
    r1, r2 = await asyncio.gather(
        Verifier(f1, solver_timeout_seconds=_SLOW_TIMEOUT_S).verify(t1),
        Verifier(f2, solver_timeout_seconds=_SLOW_TIMEOUT_S).verify(t2),
    )
    elapsed = time.perf_counter() - started
    assert r1.step_verdicts[-1].reason == r2.step_verdicts[-1].reason == "timeout"
    sequential_s = r1.step_verdicts[-1].solver_ms / 1000 + r2.step_verdicts[-1].solver_ms / 1000
    assert elapsed < 0.75 * sequential_s


async def test_bench_run_reports_within_budget() -> None:
    report = await bench_verifier.run(1)
    assert report.runs == 1
    assert len(report.trace_total_ms) == len(ALL)
    assert report.step_ms and all(ms > 0 for ms in report.step_ms)
    assert report.within_budget(STEP_BUDGET_MS)
    text = bench_verifier.format_report(report)
    assert "p50" in text and "p95" in text and "max" in text


async def test_bench_main_exit_code() -> None:
    assert await bench_verifier.amain(["--runs", "1"]) == 0
    assert await bench_verifier.amain(["--runs", "1", "--budget-ms", "0.000001"]) == 1
