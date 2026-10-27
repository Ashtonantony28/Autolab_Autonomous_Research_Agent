"""Auto-formalize natural-language reasoning steps into SMT-LIB.

The formalizer maintains a growing **vocabulary** across the steps of a
trace: when step N is formalized, the symbol declarations from steps 1..N-1
are passed in so the LLM keeps names consistent (CLAUDE.md §7 Pattern C).
Re-declaring a symbol with a different signature is rejected.

The LLM returns a `FormalizedStep` (via `LLMClient.call_structured`) whose
`declarations` and `assertion` are SMT-LIB 2 strings. The vocabulary is only
extended once the step's SMT parses, and a merge either applies fully or not
at all, so a rejected step never leaks symbols into later prompts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import z3
from pydantic import ValidationError

from autolab.state.models import FormalizedStep, ReasoningStep
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
        """Return all declarations as one SMT-LIB block."""
        return "\n".join(self.declarations.values())

    def merged(self, new_decls: str) -> Vocabulary:
        """Return a copy extended with `new_decls`; `self` is never modified.

        Raises:
            FormalizationError: on an unrecognized or conflicting declaration.
        """
        out = dict(self.declarations)
        for line in new_decls.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            name = _decl_name(stripped)
            if name is None:
                raise FormalizationError(f"unrecognized declaration: {stripped!r}")
            existing = out.get(name)
            if existing is not None and existing != stripped:
                raise FormalizationError(
                    f"symbol {name!r} re-declared with conflicting signature: "
                    f"had {existing!r}, got {stripped!r}"
                )
            out[name] = stripped
        return Vocabulary(out)

    def merge(self, new_decls: str) -> None:
        """Atomically extend this vocabulary in place (all lines or none).

        Raises:
            FormalizationError: on an unrecognized or conflicting declaration.
        """
        self.declarations = self.merged(new_decls).declarations


class Formalizer:
    """Translate `ReasoningStep` → `SmtFormula`, accumulating a vocabulary."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self.vocabulary = Vocabulary()

    async def formalize(self, step: ReasoningStep) -> SmtFormula:
        """Formalize `step` and, on success only, extend the vocabulary.

        Raises:
            FormalizationError: if the LLM refuses or returns no structured
                output, if its output fails schema validation (including an
                empty assertion), conflicts with the vocabulary,
                or does not parse as SMT-LIB.
        """
        prompt = prompt_for(step, self.vocabulary)
        try:
            out = await self._llm.call_structured("formalize", prompt, FormalizedStep)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise FormalizationError(f"LLM output is not a valid FormalizedStep: {exc}") from exc
        except RuntimeError as exc:
            # A refusal or empty structured output (AUDIT-3 #2): this step fails, not the run.
            raise FormalizationError(f"LLM gave no structured output: {exc}") from exc

        candidate = self.vocabulary.merged(out.declarations)
        full_decls = candidate.smt()
        try:
            z3.parse_smt2_string(f"{full_decls}\n(assert {out.assertion})")
        except z3.Z3Exception as exc:
            raise FormalizationError(f"invalid SMT for step {step.index}: {exc}") from exc

        self.vocabulary = candidate
        return SmtFormula(declarations=full_decls, assertion=out.assertion)


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


_DECL_HEADS = frozenset({"declare-const", "declare-fun", "declare-sort"})


def _decl_name(decl: str) -> str | None:
    """Return X if `decl` is exactly one `(declare-const|declare-fun|declare-sort X ...)` form.

    Anything else (a second command after the form, unbalanced parens, quoted
    symbols, strings or comments) returns None, so no SMT command other than a
    declaration can reach the solver through `declarations` (AUDIT-1 #2).
    """
    body = decl.strip()
    if not body.startswith("(") or any(c in body for c in '|";'):
        return None
    depth = 0
    for i, c in enumerate(body):
        depth += {"(": 1, ")": -1}.get(c, 0)
        if depth == 0:
            if i != len(body) - 1:
                return None
            break
    if depth != 0:
        return None
    tokens = body[1:-1].replace("(", " ( ").replace(")", " ) ").split()
    if len(tokens) < 2 or tokens[0] not in _DECL_HEADS or tokens[1] in {"(", ")"}:
        return None
    return tokens[1]


__all__ = ["FormalizationError", "Formalizer", "Vocabulary", "prompt_for"]
