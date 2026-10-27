"""AUDIT-15 repros: API/SDK consistency, round 4 (see AUDIT-15.md).

Every test asserts the *correct* behaviour and is a strict xfail, so it
starts to XPASS, and fails the suite, once its finding is fixed. No network:
every LLM is a seeded `FakeLLMClient`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from autolab import cli
from autolab.agents.common import design_claim, design_facts, measurement_facts, protocol_figures
from autolab.agents.design import DesignAgent, build_design_prompt, build_design_trace_prompt
from autolab.config import get_settings
from autolab.monitoring.dashboard import render_event_table
from autolab.monitoring.events import TraceEvent
from autolab.orchestrator import graph as graph_module
from autolab.state.models import (
    ExperimentDesign,
    ExperimentParameter,
    ExperimentResult,
    Hypothesis,
    Measurement,
    ReasoningStep,
    ReasoningTrace,
    VerificationContext,
    VerificationResult,
)
from autolab.state.schema import empty_state
from autolab.verification.formalizer import FormalizationError, Vocabulary, prompt_for
from autolab.verification.llm import SYSTEM_PROMPTS, FakeLLMClient
from autolab.verification.solver import SmtFormula, Z3Solver
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_trivial_trace

HYPOTHESIS = Hypothesis(question="Q?", statement="S", rationale="R")

MICRO_LITRE = "volume_µL"  # volume_µL (MICRO SIGN), a common lab parameter name
OD600 = "OD₆₀₀"  # OD₆₀₀ (subscript digits), a common measurement name


def _design(**update: object) -> ExperimentDesign:
    base = ExperimentDesign(
        hypothesis_id=HYPOTHESIS.id,
        protocol="dispense the buffer into a 96-well plate",
        estimated_cost_usd=5.0,
        estimated_duration_minutes=10.0,
    )
    return base.model_copy(update=update)


# --- #1 (high): code-built fact symbols break SG15's identifier rule --------


async def test_a_design_with_a_unicode_parameter_name_can_be_verified() -> None:
    # `_param_symbol` keeps a name that is all `\w` (Unicode-aware), so
    # `volume_µL` becomes `param_volume_µL`, which `Vocabulary.merged` now
    # rejects: `Verifier.verify` raises before any step is formalized.
    design = _design(parameters=(ExperimentParameter(name=MICRO_LITRE, value=50.0, unit="uL"),))
    claim = design_claim(design)
    trace = ReasoningTrace(
        agent="design", steps=(ReasoningStep(index=0, claim=claim, claim_type="conclusion"),)
    )
    context = VerificationContext(facts=design_facts(design), payload_claim=claim)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(trace.steps[0], _vocab(context)),
        '{"declarations": "", "assertion": "(= estimated_cost_usd 5.0)"}',
    )

    result: VerificationResult | None = None
    error: FormalizationError | None = None
    try:
        result = await Verifier(fake).verify(trace, context=context)
    except FormalizationError as exc:
        error = exc

    assert error is None, f"verify raised on the code-built facts: {error}"
    assert result is not None and result.step_verdicts[0].verdict == "VALID"


def _vocab(context: VerificationContext) -> Vocabulary:
    # The vocabulary the verifier would build, without its identifier checks.
    return Vocabulary({f.declarations.split()[1]: f.declarations for f in context.facts})


def test_measurement_fact_symbols_are_plain_identifiers() -> None:
    result = ExperimentResult(
        design_id=uuid4(), status="success", measurements=(Measurement(name=OD600, value=0.4),)
    )
    names = [fact.declarations.split()[1] for fact in measurement_facts(result)]
    # `Vocabulary.merged` accepts only names matching [A-Za-z_][A-Za-z0-9_]* (SG15).
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) for n in names), names


# --- #2 (medium): the formalize prompt does not state SG15's rules -----------


def test_formalize_prompt_states_the_identifier_and_single_term_rules() -> None:
    step = ReasoningStep(index=0, claim="the sample is hot", claim_type="premise")
    text = f"{SYSTEM_PROMPTS['formalize']}\n{prompt_for(step, Vocabulary())}".lower()
    # Declared names must match [A-Za-z_][A-Za-z0-9_]* and not be Z3 builtins;
    # `|quoted|` symbols and `;` comments are rejected.
    assert "identifier" in text
    assert "builtin" in text or "reserved" in text
    assert "comment" in text


# --- #3 (medium): `protocol_figures` reads Svedberg units as durations -------


@pytest.mark.parametrize(
    "protocol",
    ["Sequence the 16S rRNA gene", "Isolate 70S ribosomes", "Purify the 30S subunit"],
)
def test_svedberg_units_are_not_protocol_figures(protocol: str) -> None:
    assert protocol_figures(protocol) == ()


# --- #4 (medium): a string parameter carries a cost into the bound claim -----


async def test_a_cost_stated_in_a_string_parameter_is_not_admitted() -> None:
    # SG17: cost and duration figures live only in the bound numeric fields.
    # A `str` parameter (allowed by DESIGN_REPLY) is named in `design_claim`
    # but never checked, so "$500" next to estimated_cost_usd = 5 is admitted.
    priced = _design(parameters=(ExperimentParameter(name="reagent_budget", value="$500"),))
    assert "$500" in design_claim(priced)
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), priced.model_dump_json())
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, priced),
        conclusion=design_claim(priced),
        facts=design_facts(priced),
    )

    update = await DesignAgent(verifier=Verifier(fake), llm=fake)(
        {**empty_state("Q?"), "hypotheses": [HYPOTHESIS]}
    )

    assert update["hitl_pending"] is not None
    assert "current_experiment" not in update


# --- #5 (medium): a premise whose head starts with "assert" is dropped ------


def test_a_premise_whose_predicate_starts_with_assert_is_kept() -> None:
    decl = "(declare-fun assertive (Int) Bool)"
    Vocabulary().merge(decl)  # the formalizer accepts the name and the term
    premise = SmtFormula(decl, "(assertive 1)")

    outcome = Z3Solver().check_entailment([premise], SmtFormula(decl, "(assertive 1)"))

    assert outcome.verdict == "VALID", outcome


# --- #6 (low): the dashboard drops the HITL event's agent and note ----------


@pytest.mark.xfail(strict=True, reason="AUDIT-15 #6")
def test_dashboard_event_table_shows_the_hitl_agent_and_note() -> None:
    event = TraceEvent(
        run_id=uuid4(),
        node="hitl",
        attempt=1,
        latency_ms=1.0,
        decision="approve",
        trigger="policy",
        hitl_reason="estimated cost $15.00 > $10.00",
        agent="design",
        note="budget signed off by PI",
    )

    [_header, row] = render_event_table([event])

    assert "design" in row
    assert "budget signed off by PI" in row


# --- #7 (low): no entry point builds `Metrics` (SG18's `hitl_decisions`) ----


class _Stop(Exception):
    pass


@pytest.mark.xfail(strict=True, reason="AUDIT-15 #7")
async def test_the_cli_collects_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    get_settings.cache_clear()
    seen: dict[str, Any] = {}

    def fake_build_graph(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(graph_module, "build_graph", fake_build_graph)
    try:
        with pytest.raises(_Stop):
            await cli._drive(FakeLLMClient(), tmp_path / "c.db", str(uuid4()), empty_state("Q?"))
    finally:
        get_settings.cache_clear()

    assert seen.get("metrics") is not None
