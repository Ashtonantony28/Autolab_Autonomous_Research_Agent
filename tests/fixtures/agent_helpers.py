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


def _sym(agent: str, letter: str) -> str:
    """Per-agent symbol name so formalize-prompt keys never collide across agents."""
    sanitized = "".join(c if c.isalnum() else "_" for c in agent)
    return f"{sanitized}_{letter}"


def register_trivial_trace(
    fake: FakeLLMClient,
    *,
    agent: str,
    trace_purpose: str,
    trace_prompt: str,
) -> ReasoningTrace:
    """Register a 2-step trace (premise P, conclusion P) that verifies VALID/VALID.

    Symbol names are namespaced by `agent` so multiple agents' fakes can
    coexist on the same `FakeLLMClient` without colliding.
    """
    sym = _sym(agent, "P")
    claim = f"premise of {agent}"
    steps = (
        ReasoningStep(index=0, claim=claim, claim_type="premise"),
        ReasoningStep(index=1, claim=claim, claim_type="conclusion", referenced_step_indices=(0,)),
    )
    trace = ReasoningTrace(agent=agent, steps=steps)

    fake.register(
        trace_purpose,
        trace_prompt,
        json.dumps(
            {
                "steps": [
                    {"index": 0, "claim": claim, "claim_type": "premise"},
                    {
                        "index": 1,
                        "claim": claim,
                        "claim_type": "conclusion",
                        "referenced_step_indices": [0],
                    },
                ]
            }
        ),
    )

    v = Vocabulary()
    decl = f"(declare-const {sym} Bool)"
    fake.register(
        "formalize",
        prompt_for(steps[0], v),
        json.dumps({"declarations": decl, "assertion": sym}),
    )
    v.merge(decl)
    fake.register(
        "formalize",
        prompt_for(steps[1], v),
        json.dumps({"declarations": "", "assertion": sym}),
    )
    return trace


def register_failing_trace(
    fake: FakeLLMClient,
    *,
    agent: str,
    trace_purpose: str,
    trace_prompt: str,
) -> ReasoningTrace:
    """Register a 2-step trace whose conclusion (Q) is unrelated to its premise (P)."""
    sym_p = _sym(agent, "P")
    sym_q = _sym(agent, "Q")
    claim_p = f"premise of {agent}"
    claim_q = f"unrelated conclusion of {agent}"
    steps = (
        ReasoningStep(index=0, claim=claim_p, claim_type="premise"),
        ReasoningStep(
            index=1, claim=claim_q, claim_type="conclusion", referenced_step_indices=(0,)
        ),
    )
    trace = ReasoningTrace(agent=agent, steps=steps)

    fake.register(
        trace_purpose,
        trace_prompt,
        json.dumps(
            {
                "steps": [
                    {"index": 0, "claim": claim_p, "claim_type": "premise"},
                    {
                        "index": 1,
                        "claim": claim_q,
                        "claim_type": "conclusion",
                        "referenced_step_indices": [0],
                    },
                ]
            }
        ),
    )

    v = Vocabulary()
    decls = f"(declare-const {sym_p} Bool)\n(declare-const {sym_q} Bool)"
    fake.register(
        "formalize",
        prompt_for(steps[0], v),
        json.dumps({"declarations": decls, "assertion": sym_p}),
    )
    v.merge(decls)
    fake.register(
        "formalize",
        prompt_for(steps[1], v),
        json.dumps({"declarations": "", "assertion": sym_q}),
    )
    return trace
