"""Helpers for writing concise agent tests.

The agent tests need to register both the agent's "produce" responses *and*
the formalizations the verifier will request for the resulting reasoning
trace. These helpers cover the trace + formalizations; the caller still
registers their own payload response.
"""

from __future__ import annotations

import json

from autolab.state.models import ReasoningStep, ReasoningTrace
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient


def register_trivial_trace(
    fake: FakeLLMClient,
    *,
    agent: str,
    trace_purpose: str,
    trace_prompt: str,
) -> ReasoningTrace:
    """Register a 2-step trace (premise P, conclusion P) that verifies VALID/VALID.

    Returns the trace so tests can reference it in assertions.
    """
    steps = (
        ReasoningStep(index=0, claim="P", claim_type="premise"),
        ReasoningStep(index=1, claim="P", claim_type="conclusion", referenced_step_indices=(0,)),
    )
    trace = ReasoningTrace(agent=agent, steps=steps)

    fake.register(
        trace_purpose,
        trace_prompt,
        json.dumps(
            {
                "steps": [
                    {"index": 0, "claim": "P", "claim_type": "premise"},
                    {
                        "index": 1,
                        "claim": "P",
                        "claim_type": "conclusion",
                        "referenced_step_indices": [0],
                    },
                ]
            }
        ),
    )

    v = Vocabulary()
    fake.register(
        "formalize",
        prompt_for(steps[0], v),
        json.dumps({"declarations": "(declare-const P Bool)", "assertion": "P"}),
    )
    v.merge("(declare-const P Bool)")
    fake.register(
        "formalize",
        prompt_for(steps[1], v),
        json.dumps({"declarations": "", "assertion": "P"}),
    )
    return trace


def register_failing_trace(
    fake: FakeLLMClient,
    *,
    agent: str,
    trace_purpose: str,
    trace_prompt: str,
) -> ReasoningTrace:
    """Register a 2-step trace whose conclusion (Q) is unrelated to its premise (P).

    Verifier should classify the conclusion as INVALID, dropping the
    consistency_score below the HITL threshold.
    """
    steps = (
        ReasoningStep(index=0, claim="P", claim_type="premise"),
        ReasoningStep(index=1, claim="Q", claim_type="conclusion", referenced_step_indices=(0,)),
    )
    trace = ReasoningTrace(agent=agent, steps=steps)

    fake.register(
        trace_purpose,
        trace_prompt,
        json.dumps(
            {
                "steps": [
                    {"index": 0, "claim": "P", "claim_type": "premise"},
                    {
                        "index": 1,
                        "claim": "Q",
                        "claim_type": "conclusion",
                        "referenced_step_indices": [0],
                    },
                ]
            }
        ),
    )

    v = Vocabulary()
    fake.register(
        "formalize",
        prompt_for(steps[0], v),
        json.dumps(
            {
                "declarations": "(declare-const P Bool)\n(declare-const Q Bool)",
                "assertion": "P",
            }
        ),
    )
    v.merge("(declare-const P Bool)\n(declare-const Q Bool)")
    fake.register(
        "formalize",
        prompt_for(steps[1], v),
        json.dumps({"declarations": "", "assertion": "Q"}),
    )
    return trace
