"""Tests for the AutoLabState TypedDict and its additive reducers."""

from __future__ import annotations

from operator import add
from typing import get_type_hints

from autolab.state.models import Hypothesis, ReasoningStep, ReasoningTrace
from autolab.state.schema import AutoLabState, empty_state


def test_empty_state_initializes_all_collections() -> None:
    s = empty_state("why does X?")
    assert s["research_question"] == "why does X?"
    assert s["hypotheses"] == []
    assert s["results"] == []
    assert s["reasoning_traces"] == []
    assert s["verification_history"] == []
    assert s["current_experiment"] is None
    assert s["hitl_pending"] is None
    assert s["confidence"] == 0.0


def test_reducer_is_add_for_list_fields() -> None:
    # Inspect the annotation metadata to confirm Annotated[list[X], add].
    hints = get_type_hints(AutoLabState, include_extras=True)
    for field in ("hypotheses", "results", "reasoning_traces", "verification_history"):
        metadata = getattr(hints[field], "__metadata__", ())
        assert add in metadata, f"{field} missing additive reducer"


def test_list_concatenation_semantics() -> None:
    # Simulate what LangGraph does when two updates target the same list field.
    h1 = Hypothesis(question="q", statement="s1", rationale="r")
    h2 = Hypothesis(question="q", statement="s2", rationale="r")
    merged = add([h1], [h2])
    assert merged == [h1, h2]


def test_trace_with_reducer_is_appendable() -> None:
    t1 = ReasoningTrace(agent="a", steps=(ReasoningStep(index=0, claim="c", claim_type="premise"),))
    t2 = ReasoningTrace(
        agent="b", steps=(ReasoningStep(index=0, claim="d", claim_type="conclusion"),)
    )
    assert add([t1], [t2]) == [t1, t2]
