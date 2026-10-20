"""Helpers shared by the producing agents: reply schemas, trace parsing and grounding."""

from __future__ import annotations

import hashlib
import math
import re
from decimal import Decimal
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, create_model

from autolab.state.models import (
    AnalysisReport,
    ExperimentDesign,
    ExperimentResult,
    FormalizedStep,
    Hypothesis,
    ParameterValue,
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
    return tuple(_real_fact(_symbol(m.name), m.value) for m in result.measurements)


def design_claim(design: ExperimentDesign) -> str:
    """The claim a Design trace must conclude: the protocol and every structured field."""
    values = [
        f"estimated_cost_usd = {_decimal(design.estimated_cost_usd)}",
        f"estimated_duration_minutes = {_decimal(design.estimated_duration_minutes)}",
    ]
    for p in design.parameters:
        text = _decimal(float(p.value)) if _numeric(p.value) else str(p.value)
        values.append(f"{_param_symbol(p.name)} = {text}" + (f" {p.unit}" if p.unit else ""))
    return f"design protocol: {design.protocol}; {', '.join(values)}"


_NUMBER: Final = r"\d+(?:[.,]\d+)*"
_PROTOCOL_FIGURE: Final = re.compile(
    rf"(?:[$€£¥]\s*{_NUMBER})"
    rf"|(?:\b(?:USD|EUR|GBP|JPY|CHF|CAD|AUD|CNY)\s*{_NUMBER})"
    rf"|(?:{_NUMBER}\s*(?:USD|EUR|GBP|JPY|CHF|CAD|AUD|CNY|dollars?|euros?|pounds?)\b)"
    rf"|(?:{_NUMBER}\s*(?:s|secs?|seconds?|min|mins|minutes?|h|hrs?|hours?|days?)\b)",
    re.IGNORECASE,
)


def protocol_figures(protocol: str) -> tuple[str, ...]:
    """Currency amounts and durations stated in a protocol's text.

    The protocol may not state its own cost or duration: those live only in the
    bound `estimated_cost_usd` / `estimated_duration_minutes` fields. This is a
    rejection check, not parsing into state (CLAUDE.md §9): a match only blocks
    admission, and no value is read back from the text.
    """
    return tuple(m.group(0) for m in _PROTOCOL_FIGURE.finditer(protocol))


def design_facts(design: ExperimentDesign) -> tuple[FormalizedStep, ...]:
    """Code-built Real facts for the Design's cost, duration and numeric parameters.

    Raises:
        ValueError: if a value is not finite.
    """
    return (
        _real_fact("estimated_cost_usd", design.estimated_cost_usd),
        _real_fact("estimated_duration_minutes", design.estimated_duration_minutes),
        *(
            _real_fact(_param_symbol(p.name), float(p.value))
            for p in design.parameters
            if _numeric(p.value)
        ),
    )


def _numeric(value: ParameterValue) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _param_symbol(name: str) -> str:
    sym = re.sub(r"\W", "_", name)
    if sym == name:
        return f"param_{sym}"
    return f"paramx_{sym}_{hashlib.sha256(name.encode()).hexdigest()[:12]}"


def _real_fact(sym: str, value: float) -> FormalizedStep:
    return FormalizedStep(
        declarations=f"(declare-const {sym} Real)", assertion=f"(= {sym} {_real(value)})"
    )


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
    "design_claim",
    "design_facts",
    "measurement_facts",
    "parse_trace",
    "protocol_figures",
    "result_claim",
    "with_feedback",
]
