"""Auto-formalize natural-language reasoning steps into SMT-LIB.

The formalizer maintains a growing **vocabulary** across the steps of a
trace: when step N is formalized, the symbol declarations from steps 1..N-1
are passed in so the LLM keeps names consistent (CLAUDE.md §7 Pattern C).
Re-declaring a symbol with a different signature is rejected.

The LLM is expected to return a JSON object with two fields, `declarations`
and `assertion`, both strings of SMT-LIB 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import z3

from autolab.state.models import ReasoningStep
from autolab.verification.llm import LLMClient
from autolab.verification.solver import SmtFormula


class FormalizationError(ValueError):
    """Raised when an LLM response cannot be turned into valid SMT."""


@dataclass
class Vocabulary:
    """Accumulated SMT-LIB declarations seen across formalized steps.

    Keyed by symbol name so re-declarations are detectable.
    """

    declarations: dict[str, str] = field(default_factory=dict)

    def smt(self) -> str:
        return "\n".join(self.declarations.values())

    def merge(self, new_decls: str) -> None:
        for line in new_decls.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            name = _decl_name(stripped)
            if name is None:
                raise FormalizationError(f"unrecognized declaration: {stripped!r}")
            existing = self.declarations.get(name)
            if existing is not None and existing != stripped:
                raise FormalizationError(
                    f"symbol {name!r} re-declared with conflicting signature: "
                    f"had {existing!r}, got {stripped!r}"
                )
            self.declarations[name] = stripped


class Formalizer:
    """Translate `ReasoningStep` → `SmtFormula`, accumulating a vocabulary."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self.vocabulary = Vocabulary()

    async def formalize(self, step: ReasoningStep) -> SmtFormula:
        prompt = prompt_for(step, self.vocabulary)
        raw = await self._llm.call("formalize", prompt)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FormalizationError(f"LLM did not return JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise FormalizationError(f"LLM JSON must be an object, got {type(payload).__name__}")
        decls = payload.get("declarations", "")
        assertion = payload.get("assertion", "")
        if not isinstance(decls, str) or not isinstance(assertion, str):
            raise FormalizationError("`declarations` and `assertion` must be strings")
        if not assertion.strip():
            raise FormalizationError("empty assertion")

        self.vocabulary.merge(decls)
        full_decls = self.vocabulary.smt()

        try:
            z3.parse_smt2_string(f"{full_decls}\n(assert {assertion})")
        except z3.Z3Exception as exc:
            raise FormalizationError(f"invalid SMT for step {step.index}: {exc}") from exc

        return SmtFormula(declarations=full_decls, assertion=assertion)


def prompt_for(step: ReasoningStep, vocab: Vocabulary) -> str:
    """Build the LLM prompt. Stable string so `FakeLLMClient` lookups work.

    Public so tests can pre-register responses keyed on the same prompt the
    formalizer will produce.
    """
    lines = [
        "Formalize the following reasoning step into SMT-LIB 2.",
        "",
        f"step_index: {step.index}",
        f"claim_type: {step.claim_type}",
        f"claim: {step.claim}",
        "",
        "Existing vocabulary (do not re-declare these):",
        vocab.smt() or "(none)",
        "",
        'Reply with JSON: {"declarations": "...", "assertion": "..."}',
    ]
    return "\n".join(lines)


def _decl_name(decl: str) -> str | None:
    """Pull the symbol name out of a `(declare-const X T)` / `(declare-fun X (...) T)`."""
    body = decl.strip()
    if not body.startswith("("):
        return None
    tokens = body.lstrip("(").split()
    if len(tokens) < 2:
        return None
    head = tokens[0]
    if head not in {"declare-const", "declare-fun", "declare-sort"}:
        return None
    return tokens[1]


__all__ = ["FormalizationError", "Formalizer", "Vocabulary", "prompt_for"]
