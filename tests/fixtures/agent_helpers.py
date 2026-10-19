"""Helpers for writing concise agent tests.

The agent tests need to register both the agent's "produce" responses *and*
the formalizations the verifier will request for the resulting reasoning
trace. These helpers cover the trace + formalizations; the caller still
registers their own payload response.

Agents bind traces to their payload (`VerificationContext.payload_claim`), so
a passing trace must conclude the agent's payload claim: pass it as
`conclusion`. Agents that inject measurement facts seed the formalizer's
vocabulary with them, so pass the same `facts` to keep prompts aligned.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final

from pydantic import BaseModel

from autolab.state.models import FormalizedStep, ReasoningStep, ReasoningTrace
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from tests.fixtures.golden_traces import GoldenStep

CODE_OWNED: Final = frozenset({"id", "created_at", "hypothesis_id", "result_id"})
"""Fields agents assign in code, never from the model's reply."""


def content(*models: BaseModel) -> list[dict[str, Any]]:
    """Each model without its `CODE_OWNED` fields, for comparing agent output to a fixture."""
    return [m.model_dump(exclude=set(CODE_OWNED)) for m in models]


def _sym(agent: str, letter: str) -> str:
    """Per-agent symbol name so formalize-prompt keys never collide across agents."""
    sanitized = "".join(c if c.isalnum() else "_" for c in agent)
    return f"{sanitized}_{letter}"


def register_trace(
    fake: FakeLLMClient,
    *,
    agent: str,
    trace_purpose: str,
    trace_prompt: str,
    steps: Sequence[GoldenStep],
    facts: Sequence[FormalizedStep] = (),
) -> ReasoningTrace:
    """Register an arbitrary trace reply plus every formalization it will request."""
    vocab = Vocabulary()
    for fact in facts:
        vocab.merge(fact.declarations)
    reasoning = tuple(
        ReasoningStep(
            index=i,
            claim=s.claim,
            claim_type=s.claim_type,
            referenced_step_indices=s.referenced_step_indices,
        )
        for i, s in enumerate(steps)
    )
    fake.register(
        trace_purpose,
        trace_prompt,
        json.dumps({"steps": [step.model_dump(mode="json") for step in reasoning]}),
    )
    for step, spec in zip(reasoning, steps, strict=True):
        fake.register(
            "formalize",
            prompt_for(step, vocab),
            json.dumps({"declarations": spec.declarations, "assertion": spec.assertion}),
        )
        vocab.merge(spec.declarations)
    return ReasoningTrace(agent=agent, steps=reasoning)


def register_trivial_trace(
    fake: FakeLLMClient,
    *,
    agent: str,
    trace_purpose: str,
    trace_prompt: str,
    conclusion: str | None = None,
    facts: Sequence[FormalizedStep] = (),
) -> ReasoningTrace:
    """Register a 3-step trace (premise P, premise P => C, conclusion C) that verifies VALID.

    The conclusion states `conclusion` (default: a placeholder) and is genuinely
    entailed: neither premise proves it alone. Symbol names are namespaced by
    `agent` so multiple agents' fakes can coexist on the same `FakeLLMClient`.
    """
    sym_p = _sym(agent, "P")
    sym_c = _sym(agent, "C")
    claim = conclusion or f"conclusion of {agent}"
    return register_trace(
        fake,
        agent=agent,
        trace_purpose=trace_purpose,
        trace_prompt=trace_prompt,
        steps=(
            GoldenStep(
                f"premise of {agent}",
                "premise",
                f"(declare-const {sym_p} Bool)\n(declare-const {sym_c} Bool)",
                sym_p,
            ),
            GoldenStep(
                f"premise of {agent} implies {claim}", "premise", "", f"(=> {sym_p} {sym_c})"
            ),
            GoldenStep(claim, "conclusion", "", sym_c, (0, 1)),
        ),
        facts=facts,
    )


def register_circular_trace(
    fake: FakeLLMClient,
    *,
    agent: str,
    trace_purpose: str,
    trace_prompt: str,
    conclusion: str,
    facts: Sequence[FormalizedStep] = (),
) -> ReasoningTrace:
    """Register a circular 2-step trace: premise C, then conclusion C citing it (AUDIT-1 #1)."""
    sym = _sym(agent, "P")
    return register_trace(
        fake,
        agent=agent,
        trace_purpose=trace_purpose,
        trace_prompt=trace_prompt,
        steps=(
            GoldenStep(conclusion, "premise", f"(declare-const {sym} Bool)", sym),
            GoldenStep(conclusion, "conclusion", "", sym, (0,)),
        ),
        facts=facts,
    )


def register_failing_trace(
    fake: FakeLLMClient,
    *,
    agent: str,
    trace_purpose: str,
    trace_prompt: str,
    conclusion: str | None = None,
    facts: Sequence[FormalizedStep] = (),
) -> ReasoningTrace:
    """Register a 2-step trace whose conclusion (Q) is not entailed by its premise (P)."""
    sym_p = _sym(agent, "P")
    sym_q = _sym(agent, "Q")
    return register_trace(
        fake,
        agent=agent,
        trace_purpose=trace_purpose,
        trace_prompt=trace_prompt,
        steps=(
            GoldenStep(
                f"observation of {agent}",
                "premise",
                f"(declare-const {sym_p} Bool)\n(declare-const {sym_q} Bool)",
                sym_p,
            ),
            GoldenStep(
                conclusion or f"unrelated conclusion of {agent}", "conclusion", "", sym_q, (0,)
            ),
        ),
        facts=facts,
    )
