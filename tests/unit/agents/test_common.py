"""Shared agent helpers: trace parsing and code-built measurement facts."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
import z3
from pydantic import ValidationError

from autolab.agents.common import measurement_facts, parse_trace, result_claim
from autolab.state.models import ExperimentResult, Measurement
from autolab.verification.formalizer import Vocabulary


def _result(*measurements: tuple[str, float]) -> ExperimentResult:
    return ExperimentResult(
        design_id=uuid4(),
        status="partial",
        measurements=tuple(Measurement(name=n, value=v) for n, v in measurements),
    )


def test_parse_trace_builds_trace_for_agent() -> None:
    raw = json.dumps(
        {
            "steps": [
                {"index": 0, "claim": "a", "claim_type": "premise"},
                {
                    "index": 1,
                    "claim": "b",
                    "claim_type": "conclusion",
                    "referenced_step_indices": [0],
                },
            ]
        }
    )
    trace = parse_trace("design", raw)
    assert trace.agent == "design"
    assert [s.claim for s in trace.steps] == ["a", "b"]
    assert trace.steps[1].referenced_step_indices == (0,)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        '{"steps": [{"index": 0, "claim": "a"}]}',
        '{"steps": [], "extra": 1}',
        '{"steps": [{"index": 1, "claim": "a", "claim_type": "premise"}]}',
    ],
)
def test_parse_trace_rejects_malformed_replies(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse_trace("design", raw)


@pytest.mark.parametrize(
    ("name", "value", "decl", "assertion"),
    [
        ("yield_pct", 42.5, "(declare-const yield_pct Real)", "(= yield_pct 42.5)"),
        ("delta", -3.25, "(declare-const delta Real)", "(= delta (- 3.25))"),
        ("tiny", 1e-05, "(declare-const tiny Real)", "(= tiny 0.00001)"),
        ("huge", 1e20, "(declare-const huge Real)", "(= huge 100000000000000000000.0)"),
        (
            "OD 600",
            0.5,
            "(declare-const m_OD_600_f098d37748c9 Real)",
            "(= m_OD_600_f098d37748c9 0.5)",
        ),
        ("600nm", 7.0, "(declare-const m_600nm_f97f7fb63691 Real)", "(= m_600nm_f97f7fb63691 7.0)"),
    ],
)
def test_measurement_facts_are_parseable_real_equalities(
    name: str, value: float, decl: str, assertion: str
) -> None:
    [fact] = measurement_facts(_result((name, value)))
    assert (fact.declarations, fact.assertion) == (decl, assertion)
    sym = decl.split()[1]
    s = z3.Solver()
    s.add(z3.parse_smt2_string(f"{decl}\n(assert {assertion})"))
    assert s.check() == z3.sat
    model_value = s.model()[z3.Real(sym)]
    assert isinstance(model_value, z3.RatNumRef)
    assert model_value.as_fraction() == pytest.approx(value)


def test_measurement_facts_cover_every_measurement() -> None:
    facts = measurement_facts(_result(("a", 1.0), ("b", 2.0)))
    assert [f.assertion for f in facts] == ["(= a 1.0)", "(= b 2.0)"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_measurement_facts_reject_non_finite_values(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        measurement_facts(_result(("x", value)))


def test_result_claim_lists_status_and_values() -> None:
    assert result_claim(_result(("a", -1.5), ("b", 2.0))) == "experiment partial: a = -1.5, b = 2.0"
    assert result_claim(_result()) == "experiment partial: no measurements"


# SG21 (AUDIT-15 #1): fact symbols are plain ASCII identifiers and stay distinct.

ODD_NAMES = ["OD₆₀₀", "OD600", "OD 600", "OD_600", "m_OD_600", "température", "volume_µL", "_x"]


def test_measurement_fact_symbols_merge_into_a_vocabulary_and_stay_distinct() -> None:
    facts = measurement_facts(_result(*((n, 1.0) for n in ODD_NAMES)))
    vocab = Vocabulary()
    for fact in facts:
        vocab = vocab.merged(fact.declarations)
    assert len(vocab.declarations) == len(ODD_NAMES)
