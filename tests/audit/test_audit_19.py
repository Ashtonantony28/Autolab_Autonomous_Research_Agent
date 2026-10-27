"""AUDIT-19 repros: API/SDK consistency, round 5 (see AUDIT-19.md).

Every test asserts the *correct* behaviour and is a strict xfail, so it
starts to XPASS, and fails the suite, once its finding is fixed. No network:
every LLM is a seeded `FakeLLMClient`.
"""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import uuid4

import pytest

from autolab.agents.analysis import build_analysis_prompt
from autolab.agents.common import (
    design_claim,
    design_facts,
    measurement_facts,
    protocol_figures,
    result_claim,
)
from autolab.agents.design import DesignAgent, build_design_prompt, build_design_trace_prompt
from autolab.agents.verification_monitor import VerificationMonitor
from autolab.monitoring.dashboard import load_run
from autolab.orchestrator.routing import make_monitor_node
from autolab.state.checkpointer import sqlite_saver
from autolab.state.models import (
    ExperimentDesign,
    ExperimentResult,
    Hypothesis,
    Measurement,
    ReasoningStep,
)
from autolab.state.schema import AutoLabState, empty_state
from autolab.state.store import SQLITE_BUSY_TIMEOUT_S, StateStore
from autolab.verification.formalizer import FormalizationError, Formalizer, prompt_for
from autolab.verification.llm import FakeLLMClient, LLMPurpose, SchemaT
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import (
    register_estimate,
    register_failing_trace,
    register_trivial_trace,
)

HYPOTHESIS = Hypothesis(question="Q?", statement="S", rationale="R")


def _design(protocol: str) -> ExperimentDesign:
    return ExperimentDesign(
        hypothesis_id=HYPOTHESIS.id,
        protocol=protocol,
        estimated_cost_usd=5.0,
        estimated_duration_minutes=30.0,
    )


def _design_fake(design: ExperimentDesign, *, passing: bool) -> FakeLLMClient:
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), design.model_dump_json())
    register = register_trivial_trace if passing else register_failing_trace
    register(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, design),
        conclusion=design_claim(design),
        facts=design_facts(design),
    )
    register_estimate(fake, design)
    return fake


def _state() -> AutoLabState:
    return {**empty_state("Q?"), "hypotheses": [HYPOTHESIS]}


def _with(update: dict[str, Any]) -> AutoLabState:
    # The monitor node sees the agent's update merged into the state.
    return cast(AutoLabState, {**_state(), **update})


# --- #1 (medium): the figure guard rejects ordinary designs ---------------------
# Gene and strain names that are ISO 4217 codes (TOP = Tongan pa'anga, SHP = St
# Helena pound, SOS = Somali shilling, PGK = kina, MAD = dirham, CUP = Cuban peso),
# animal ages, day-numbered schedules, British "per cent" and metres. None states a
# cost or a duration of the experiment, so a model following PROTOCOL_FIGURES_RULE
# writes them, and the design is withheld for a human.


@pytest.mark.parametrize(
    "protocol",
    [
        "Transform the plasmid into One Shot TOP10 E. coli cells",
        "Treat HEK293 cells with the SHP2 inhibitor",
        "Knock down SOS1 with siRNA and normalize to PGK1 expression",
        "Image MAD2 foci on a plate reader",
        "Use 8-week-old female C57BL/6 mice",
        "Day 1: seed cells. Day 2: add the compound.",
        "Cast a 2 per cent agarose gel",
        "Mount the detector 2 m from the source",
    ],
)
def test_ordinary_protocol_text_is_not_a_figure(protocol: str) -> None:
    assert protocol_figures(protocol) == ()


async def test_a_design_on_8_week_old_mice_is_admitted() -> None:
    design = _design("Dose 8-week-old C57BL/6 mice on a liquid handler")
    fake = _design_fake(design, passing=True)

    update = await DesignAgent(verifier=Verifier(fake), llm=fake)(_state())

    assert update["confidence"] == 1.0  # the trace verifies
    assert update["hitl_pending"] is None, update["hitl_pending"].reason


# --- #2 (medium): a figure rejection never reaches the model --------------------


async def test_a_figure_rejection_is_retried_with_its_reason() -> None:
    # SG21 fold-in (PLAN.md): "a figure rejection is retried with feedback before
    # it escalates". The verdict passes, so the monitor sends it to `hitl`.
    design = _design("Dispense $500 of reagent with the liquid handler")
    fake = _design_fake(design, passing=True)
    update = await DesignAgent(verifier=Verifier(fake), llm=fake)(_state())
    assert update["hitl_pending"].payload["trigger"] == "verification"
    assert update["confidence"] == 1.0

    command = make_monitor_node(VerificationMonitor(), max_retries=2)(_with(update))

    assert command.goto == "design"
    assert isinstance(command.update, dict) and "$500" in command.update["feedback"]


async def test_a_retried_design_is_told_about_its_figures() -> None:
    # With a blocked trace the monitor does retry, but the feedback is the verdict
    # summary ("0 valid, ..."); the figures the guard found never reach the model.
    design = _design("Dispense $500 of reagent with the liquid handler")
    fake = _design_fake(design, passing=False)
    update = await DesignAgent(verifier=Verifier(fake), llm=fake)(_state())
    assert "$500" in update["hitl_pending"].reason

    command = make_monitor_node(VerificationMonitor(), max_retries=2)(_with(update))

    assert command.goto == "design"
    assert isinstance(command.update, dict)
    assert "$500" in command.update["feedback"], command.update["feedback"]


# --- #3 (medium): the earlier-use rule names nothing the model can see ----------


class _Recording(FakeLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        self.prompts.append(prompt)
        return await super().call_structured(purpose, prompt, schema)


@pytest.mark.xfail(strict=True, reason="AUDIT-19 #3")
async def test_the_formalize_prompt_names_the_symbols_earlier_steps_used() -> None:
    # Step 0 quantifies with a bound variable, as the system prompt asks
    # ("forall/exists for quantified claims"). Step 1 then declares a constant of
    # the same name. SG20 rejects it, but no prompt ever showed the model the name:
    # `prompt_for` lists only the vocabulary's declarations.
    llm = _Recording()
    formalizer = Formalizer(llm)
    every = ReasoningStep(index=0, claim="every specimen is stable", claim_type="premise")
    llm.register(
        "formalize",
        prompt_for(every, formalizer.vocabulary),
        json.dumps(
            {
                "declarations": "(declare-sort Specimen 0)\n(declare-fun stable (Specimen) Bool)",
                "assertion": "(forall ((spx Specimen)) (stable spx))",
            }
        ),
    )
    await formalizer.formalize(every)
    one = ReasoningStep(index=1, claim="the first specimen is stable", claim_type="inference")
    llm.register(
        "formalize",
        prompt_for(one, formalizer.vocabulary),
        json.dumps({"declarations": "(declare-const spx Specimen)", "assertion": "(stable spx)"}),
    )
    with pytest.raises(FormalizationError, match="earlier step"):
        await formalizer.formalize(one)

    assert "spx" in llm.prompts[-1], "the prompt never names the bound variable it forbids"


# --- #4 (medium): the Analysis report prompt withholds the data -----------------


@pytest.mark.xfail(strict=True, reason="AUDIT-19 #4")
def test_the_analysis_prompt_states_the_measurements() -> None:
    result = ExperimentResult(
        design_id=uuid4(),
        status="success",
        measurements=(Measurement(name="absorbance", value=0.4213, unit="arb."),),
    )

    prompt = build_analysis_prompt(result)

    assert "absorbance" in prompt and "0.4213" in prompt, prompt


# --- #5 (medium): designs and results are written to the store, never read -----


@pytest.mark.xfail(strict=True, reason="AUDIT-19 #5")
async def test_the_dashboard_view_holds_the_runs_design_and_result() -> None:
    store = await StateStore.create("sqlite+aiosqlite:///:memory:")
    try:
        run_id = await store.create_run("Q?")
        design = _design("plate reader screen")
        result = ExperimentResult(
            design_id=design.id,
            status="success",
            measurements=(Measurement(name="absorbance", value=0.42),),
        )
        await store.append(run_id, design)
        await store.append(run_id, result)

        view = await load_run(store, run_id)
    finally:
        await store.dispose()

    shown = repr(view)
    assert str(design.id) in shown and design.protocol in shown
    assert str(result.id) in shown


# --- #6 (low): the Execute/Analysis claim does not name the fact symbols -------


@pytest.mark.xfail(strict=True, reason="AUDIT-19 #6")
@pytest.mark.parametrize("name", ["OD₆₀₀", "m_absorbance", "600nm_absorbance"])
def test_result_claim_names_each_measurement_fact_symbol(name: str) -> None:
    result = ExperimentResult(
        design_id=uuid4(), status="success", measurements=(Measurement(name=name, value=0.4),)
    )
    symbols = [fact.declarations.split()[1] for fact in measurement_facts(result)]

    # design_claim names `_param_symbol(name)`, the symbol the facts declare.
    assert all(sym in result_claim(result) for sym in symbols), (symbols, result_claim(result))


# --- #7 (low): the checkpoint database did not get SG23's busy timeout ---------


@pytest.mark.xfail(strict=True, reason="AUDIT-19 #7")
async def test_the_checkpoint_connection_waits_as_long_as_the_store(tmp_path: Any) -> None:
    async with (
        sqlite_saver(tmp_path / "c.db") as saver,
        saver.conn.execute("PRAGMA busy_timeout") as cursor,
    ):
        row = await cursor.fetchone()

    assert row is not None
    assert row[0] >= SQLITE_BUSY_TIMEOUT_S * 1000
