"""Helpers shared by the producing agents: trace parsing and verification grounding."""

from __future__ import annotations

import math
import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from autolab.state.models import ExperimentResult, FormalizedStep, ReasoningStep, ReasoningTrace


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


__all__ = ["conclusion_instruction", "measurement_facts", "parse_trace", "result_claim"]
