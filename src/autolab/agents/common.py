"""Helpers shared by the producing agents: reply schemas, trace parsing and grounding."""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from autolab.state.models import (
    AnalysisReport,
    ExperimentDesign,
    ExperimentResult,
    FormalizedStep,
    Hypothesis,
    ReasoningStep,
    ReasoningTrace,
)


def content_schema(model: type[BaseModel], server_owned: frozenset[str]) -> type[BaseModel]:
    """`model` without its `server_owned` fields: the schema the LLM is asked to fill.

    Extra keys in a reply (e.g. an `id` the model or a fixture supplies) are ignored,
    so ids, timestamps and cross-references can only come from code.
    """
    fields: dict[str, Any] = {
        name: (info.annotation, info)
        for name, info in model.model_fields.items()
        if name not in server_owned
    }
    return create_model(f"{model.__name__}Reply", __config__=ConfigDict(extra="ignore"), **fields)


HYPOTHESIS_REPLY: type[BaseModel] = content_schema(
    Hypothesis, frozenset({"id", "created_at", "question"})
)
DESIGN_REPLY: type[BaseModel] = content_schema(ExperimentDesign, frozenset({"id", "hypothesis_id"}))
REPORT_REPLY: type[BaseModel] = content_schema(
    AnalysisReport, frozenset({"id", "created_at", "result_id"})
)


class _TraceSteps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: tuple[ReasoningStep, ...]


def parse_trace(agent: str, raw: str) -> ReasoningTrace:
    """Validate an LLM `{"steps": [...]}` reply into a `ReasoningTrace`.

    Raises:
        pydantic.ValidationError: if `raw` is not a valid steps object.
    """
    return ReasoningTrace(agent=agent, steps=_TraceSteps.model_validate_json(raw).steps)


def conclusion_instruction(claim: str) -> str:
    """Prompt line telling the LLM which claim its final step must state."""
    return f"The final step must be a conclusion whose claim is exactly: {claim}"


def with_feedback(prompt: str, feedback: str) -> str:
    """Append feedback on a previous attempt (verifier summary or human note), if any."""
    return f"{prompt}\n\nfeedback_on_previous_attempt: {feedback}" if feedback else prompt


def result_claim(result: ExperimentResult) -> str:
    """The claim an Execute trace must conclude: the status and measured values."""
    readings = ", ".join(f"{m.name} = {_decimal(m.value)}" for m in result.measurements)
    return f"experiment {result.status}: {readings or 'no measurements'}"


def measurement_facts(result: ExperimentResult) -> tuple[FormalizedStep, ...]:
    """One `(= <name> <value>)` Real fact per measurement, built in code, not by the LLM.

    Raises:
        ValueError: if a measurement value is not finite.
    """
    facts = []
    for m in result.measurements:
        sym = _symbol(m.name)
        facts.append(
            FormalizedStep(
                declarations=f"(declare-const {sym} Real)", assertion=f"(= {sym} {_real(m.value)})"
            )
        )
    return tuple(facts)


def _symbol(name: str) -> str:
    sym = re.sub(r"\W", "_", name)
    return sym if sym[:1].isalpha() else f"m_{sym}"


def _decimal(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError(f"measurement value must be finite, got {value!r}")
    text = format(Decimal(repr(value)), "f")
    return text if "." in text else f"{text}.0"


def _real(value: float) -> str:
    text = _decimal(value)
    return f"(- {text[1:]})" if text.startswith("-") else text


__all__ = [
    "DESIGN_REPLY",
    "HYPOTHESIS_REPLY",
    "REPORT_REPLY",
    "conclusion_instruction",
    "content_schema",
    "measurement_facts",
    "parse_trace",
    "result_claim",
    "with_feedback",
]
