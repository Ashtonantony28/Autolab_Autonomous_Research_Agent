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
import threading
from dataclasses import dataclass, field
from typing import Final

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
            FormalizationError: on an unrecognized or conflicting declaration,
                or one that names an SMT-LIB builtin or reserved word (AUDIT-5 #1).
        """
        out = dict(self.declarations)
        for line in new_decls.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            name = _decl_name(stripped)
            if name is None:
                raise FormalizationError(f"unrecognized declaration: {stripped!r}")
            if name in RESERVED_SYMBOLS:
                raise FormalizationError(f"declaration shadows reserved symbol {name!r}")
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
            z3.parse_smt2_string(f"{full_decls}\n(assert {out.assertion})", ctx=_parse_context())
        except z3.Z3Exception as exc:
            raise FormalizationError(f"invalid SMT for step {step.index}: {exc}") from exc

        self.vocabulary = candidate
        return SmtFormula(declarations=full_decls, assertion=out.assertion)


_PARSE_CONTEXTS = threading.local()


def _parse_context() -> z3.Context:
    """This thread's Z3 context for parse checks; Z3's global one is not thread-safe (AUDIT-6 #1).

    Declarations do not persist between `parse_smt2_string` calls, so reusing a
    per-thread context is equivalent to a fresh one and saves ~1.5 ms per step.
    """
    ctx: z3.Context | None = getattr(_PARSE_CONTEXTS, "ctx", None)
    if ctx is None:
        ctx = _PARSE_CONTEXTS.ctx = z3.Context()
    return ctx


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

# SMT-LIB core, Ints/Reals/Arrays theory symbols, sorts, syntax keywords and
# commands, plus common Z3 extensions. Z3 lets a declaration shadow most of them.
RESERVED_SYMBOLS: Final[frozenset[str]] = frozenset(
    {
        # Core
        "true", "false", "not", "=>", "and", "or", "xor", "=", "distinct", "ite",
        # Arithmetic
        "+", "-", "*", "/", "div", "mod", "abs", "<", "<=", ">", ">=",
        "to_real", "to_int", "is_int",
        # Arrays
        "select", "store",
        # Sorts
        "Bool", "Int", "Real", "Array", "String", "Seq", "RegLan", "BitVec",
        "FloatingPoint", "RoundingMode",
        # Syntax keywords
        "!", "_", "as", "let", "forall", "exists", "match", "par",
        "NUMERAL", "DECIMAL", "STRING", "BINARY", "HEXADECIMAL",
        # Commands
        "assert", "check-sat", "check-sat-assuming", "declare-const", "declare-datatype",
        "declare-datatypes", "declare-fun", "declare-sort", "define-fun", "define-fun-rec",
        "define-funs-rec", "define-sort", "echo", "exit", "get-assertions",
        "get-assignment", "get-info", "get-model", "get-option", "get-proof",
        "get-unsat-assumptions", "get-unsat-core", "get-value", "pop", "push",
        "reset", "reset-assertions", "set-info", "set-logic", "set-option",
        # Z3 extensions
        "rem", "^", "iff", "implies", "if", "divisible", "pi", "euler", "root-obj",
        "const", "lambda",
    }
)  # fmt: skip


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


__all__ = ["RESERVED_SYMBOLS", "FormalizationError", "Formalizer", "Vocabulary", "prompt_for"]
