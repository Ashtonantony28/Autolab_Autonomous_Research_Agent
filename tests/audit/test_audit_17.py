"""AUDIT-17: encoding soundness, round 5. Wrong designs and traces that are still admitted.

Every test asserts the *correct* behaviour and is a strict xfail until its
finding is fixed; see `AUDIT-17.md`. All of them run on `FakeLLMClient`.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Sequence
from functools import partial
from typing import Any

import pytest

from autolab.agents.common import design_claim, design_facts, protocol_figures
from autolab.agents.design import DesignAgent, build_design_prompt, build_design_trace_prompt
from autolab.orchestrator.checkpoints import cost_trigger
from autolab.state.models import ExperimentDesign, ReasoningStep
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.formalizer import FormalizationError, Formalizer, Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ
from tests.fixtures.agent_helpers import register_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator import test_graph as tg

_CLEAN_PROTOCOL = "Incubate the plate with the reagent kit and read absorbance"


async def _design_update(
    protocol: str,
    parameters: Sequence[dict[str, Any]] = (),
    success_criteria: Sequence[str] = (),
) -> dict[str, Any]:
    """Run the Design agent on a $5 / 30 min design with an honest trace that proves its claim.

    The trace's only premise is the protocol itself; the conclusion is the bound
    `design_claim`, formalized as that premise plus every code-built fact, so it
    is entailed, not circular, and scores 1.0. The cost trigger is the default
    graph's (cost > $10 or duration > 60 min).
    """
    reply: dict[str, Any] = {
        "protocol": protocol,
        "parameters": list(parameters),
        "estimated_cost_usd": 5.0,
        "estimated_duration_minutes": 30.0,
        "success_criteria": list(success_criteria),
    }
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(tg.HYPOTHESIS), json.dumps(reply))
    design = ExperimentDesign(hypothesis_id=tg.HYPOTHESIS.id, **reply)
    facts = design_facts(design)
    conclusion = " ".join(["protocol_ok", *(f.assertion for f in facts)])
    register_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(tg.HYPOTHESIS, design),
        steps=(
            GoldenStep(
                f"The protocol is: {protocol}",
                "premise",
                "(declare-const protocol_ok Bool)",
                "protocol_ok",
            ),
            GoldenStep(design_claim(design), "conclusion", "", f"(and {conclusion})", (0,)),
        ),
        facts=facts,
    )
    agent = DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))
    state: AutoLabState = {**empty_state(tg.QUESTION), "hypotheses": [tg.HYPOTHESIS]}
    return await agent(state)


# ---------------------------------------------------------------------------
# AUDIT-17 #1 (high): `design_figures` scans a parameter's name and its
# "value unit" text separately. A numeric parameter whose *name* carries the
# unit (`incubation_hours = 48`, `reagent_cost_usd = 500`) has no figure in
# either text, so a $5 / 30 min design that runs 48 h or costs $500 is admitted
# and the cost/duration pause (principle 7b) never fires.


@pytest.mark.xfail(strict=True, reason="AUDIT-17 #1")
@pytest.mark.parametrize(
    "parameter",
    [
        {"name": "incubation_hours", "value": 48},
        {"name": "reagent_cost_usd", "value": 500.0},
        {"name": "hours", "value": 48},
        {"name": "cost (USD)", "value": 500.0},
    ],
)
async def test_parameter_named_for_its_unit_cannot_hide_cost_or_duration(
    parameter: dict[str, Any],
) -> None:
    update = await _design_update(_CLEAN_PROTOCOL, parameters=[parameter])

    # Precondition: the trace verifies, so only the figure guard can stop the design.
    assert update["confidence"] == 1.0
    # Today: no pause, and the $5 / 30 min design is merged.
    assert update["hitl_pending"] is not None
    assert "current_experiment" not in update


# ---------------------------------------------------------------------------
# AUDIT-17 #2 (high): `protocol_figures` (SG21) still misses ordinary spellings:
# units outside its lists ("nights"), an article as the amount ("a week",
# "a thousand dollars"), a unit three words after the number (the scan collects
# a third word but never checks it), punctuation between amount and unit
# ("500 per well (USD)") and clock notation ("04:00:00").


@pytest.mark.xfail(strict=True, reason="AUDIT-17 #2")
@pytest.mark.parametrize(
    "protocol",
    [
        "Incubate the plate for 3 nights with the reagent kit",
        "Culture the cells for a week with the reagent kit",
        "Incubate the plate once using a thousand dollars of reagent",
        "Incubate the plate for 72 more full hours with the reagent kit",
        "Incubate the plate with reagent at 500 per well (USD)",
        "Incubate the plate with the reagent kit; run time 04:00:00 (hh:mm:ss)",
    ],
)
async def test_protocol_figures_cannot_hide_cost_or_duration(protocol: str) -> None:
    update = await _design_update(protocol)

    assert update["confidence"] == 1.0
    # Today: no pause, and the $5 / 30 min design is merged.
    assert update["hitl_pending"] is not None
    assert "current_experiment" not in update


# ---------------------------------------------------------------------------
# AUDIT-17 #3 (medium): `success_criteria` is neither in `design_claim` nor
# scanned by `design_figures`, so it can state the run's real duration or cost.


@pytest.mark.xfail(strict=True, reason="AUDIT-17 #3")
async def test_success_criteria_cannot_state_cost_or_duration() -> None:
    update = await _design_update(
        _CLEAN_PROTOCOL,
        success_criteria=["Cells reach confluence after a 48-hour incubation"],
    )

    assert update["confidence"] == 1.0
    # Today: no pause, and the $5 / 30 min design is merged.
    assert update["hitl_pending"] is not None
    assert "current_experiment" not in update


# ---------------------------------------------------------------------------
# AUDIT-17 #4 (low): look-alike letters, non-decimal Unicode digits and
# invisible characters between amount and unit pass the scan.


@pytest.mark.xfail(strict=True, reason="AUDIT-17 #4")
@pytest.mark.parametrize(
    "protocol",
    [
        f"Incubate for 48 {chr(0x04BB)}ours",  # Cyrillic shha for "h"
        f"Incubate for {chr(0x2463)} hours",  # circled digit four
        f"Incubate for 48{chr(0x00AD)}hours",  # soft hyphen
        f"Incubate for 48{chr(0x200B)}hours",  # zero-width space
    ],
)
def test_protocol_figures_see_through_unicode_spellings(protocol: str) -> None:
    assert protocol_figures(protocol)


# ---------------------------------------------------------------------------
# AUDIT-17 #5 (low): the earlier-use rule (SG20) tokenizes with `_TERM_TOKEN`,
# which keeps `k#b1` as one token. Z3's lexer ends a symbol at `#` (and `:`), so
# it reads `k` and `#b1`. A name an earlier step used is then missing from the
# used set, and a later step may declare it. The rule documents that such a
# declaration fails the step; here it is accepted. Harmless while the builtin
# probes are complete, since a binder is local, but the defence in depth is gone.


async def _formalize_two(first: str) -> None:
    fake = FakeLLMClient()
    steps = (
        ReasoningStep(index=0, claim="The flag bit is set", claim_type="premise"),
        ReasoningStep(index=1, claim="The well is ready", claim_type="premise"),
    )
    vocab = Vocabulary()
    fake.register(
        "formalize",
        prompt_for(steps[0], vocab),
        json.dumps({"declarations": "", "assertion": first}),
    )
    fake.register(
        "formalize",
        prompt_for(steps[1], vocab),
        json.dumps({"declarations": "(declare-const k Bool)", "assertion": "k"}),
    )
    formalizer = Formalizer(fake)
    await formalizer.formalize(steps[0])
    await formalizer.formalize(steps[1])


async def test_binder_named_like_a_later_declaration_fails_the_later_step() -> None:
    """Control: with a space before `#b1`, the rule refuses the later declaration."""
    with pytest.raises(FormalizationError, match="earlier step"):
        await _formalize_two("(let ((k #b1)) (= (bvor k #b0) #b1))")


@pytest.mark.xfail(strict=True, reason="AUDIT-17 #5")
async def test_binder_glued_to_a_literal_still_counts_as_used() -> None:
    # Today: both steps formalize; `k` never enters the used set.
    with pytest.raises(FormalizationError, match="earlier step"):
        await _formalize_two("(let ((k#b1)) (= (bvor k#b0) #b1))")


# ---------------------------------------------------------------------------
# AUDIT-17 #6 (medium, outside the soundness focus): SG22's slot hand-off grows
# `ThreadPoolExecutor._max_workers` and relies on the next `submit` starting a
# thread. CPython starts one only if `_idle_semaphore.acquire(timeout=0)` fails,
# and that semaphore gains a permit per *completed item*, not per idle thread. A
# pool that has done earlier work keeps surplus permits, so the next caller's
# check is queued behind the slow one instead of running (AUDIT-14 #2 again).

_HARD_TIMEOUT_S = 3.0


def _sleep_on(executor: SolverExecutor, seconds: float) -> None:
    """One short run from its own event loop (slots are per loop), in this thread."""

    async def one() -> None:
        await executor.run(partial(time.sleep, seconds))

    asyncio.run(one())


async def test_slot_hand_off_starts_a_thread_after_earlier_work() -> None:
    executor = SolverExecutor(1, slow_workers=1)
    try:
        # Earlier work: two runs from two loops. The second queues behind the first on
        # the pool's one thread, so two items complete for one thread: two permits.
        first = threading.Thread(target=_sleep_on, args=(executor, 0.2))
        second = threading.Thread(target=_sleep_on, args=(executor, 0.2))
        first.start()
        await asyncio.sleep(0.05)
        second.start()
        await asyncio.to_thread(first.join)
        await asyncio.to_thread(second.join)

        hard = partial(
            Z3Solver(_HARD_TIMEOUT_S).check_entailment,
            [SmtFormula(XYZ, POSITIVE)],
            SmtFormula(XYZ, NO_CUBES),
        )
        hog = asyncio.create_task(executor.run(hard))
        await asyncio.sleep(0.3)  # the hard check has left its slot by now
        started = time.perf_counter()
        await executor.run(lambda: None)
        waited = time.perf_counter() - started
        hog.cancel()
        await asyncio.gather(hog, return_exceptions=True)
    finally:
        executor.shutdown()

    # Today: the easy call waits for the hard check's timeout (~2.7 s).
    assert waited < 0.5
