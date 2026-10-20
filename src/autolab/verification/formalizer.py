"""Auto-formalize natural-language reasoning steps into SMT-LIB.

The formalizer maintains a growing **vocabulary** across the steps of a
trace: when step N is formalized, the symbol declarations from steps 1..N-1
are passed in so the LLM keeps names consistent (CLAUDE.md §7 Pattern C).
Re-declaring a symbol with a different signature is rejected.

No step can change the meaning of a symbol it did not introduce (AUDIT-13 #1, #2):
a declared name may not be a Z3 builtin, bare or applied to its declared
argument sorts, nor a name an earlier step already used, and an assertion may
not carry `!` annotations, whose `:named` would define a symbol for later checks.

The LLM returns a `FormalizedStep` (via `LLMClient.call_structured`) whose
`declarations` and `assertion` are SMT-LIB 2 strings. The vocabulary is only
extended once the step's SMT parses, and a merge either applies fully or not
at all, so a rejected step never leaks symbols into later prompts.
"""

from __future__ import annotations

import functools
import itertools
import json
import re
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
                or one that names an SMT-LIB reserved word (AUDIT-5 #1), a Z3
                builtin, or anything but a plain identifier (AUDIT-9 #1).
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
            if not _IDENTIFIER.fullmatch(name):
                raise FormalizationError(f"declared name {name!r} is not a plain identifier")
            sorts = tuple(d for d in out.values() if d.startswith("(declare-sort"))
            if _is_z3_builtin(name) or _applied_builtin(stripped, sorts):
                raise FormalizationError(f"declaration shadows Z3 builtin {name!r}")
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
        self._used: set[str] = set()

    async def formalize(self, step: ReasoningStep) -> SmtFormula:
        """Formalize `step` and, on success only, extend the vocabulary.

        Raises:
            FormalizationError: if the LLM refuses or returns no structured
                output, if its output fails schema validation (including an
                empty assertion), conflicts with the vocabulary, declares a
                name an earlier step used, has an assertion that is not exactly
                one term or carries a `!` annotation, or does not parse as SMT-LIB.
        """
        prompt = prompt_for(step, self.vocabulary)
        try:
            out = await self._llm.call_structured("formalize", prompt, FormalizedStep)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise FormalizationError(f"LLM output is not a valid FormalizedStep: {exc}") from exc
        except RuntimeError as exc:
            # A refusal or empty structured output (AUDIT-3 #2): this step fails, not the run.
            raise FormalizationError(f"LLM gave no structured output: {exc}") from exc

        if not _is_single_term(out.assertion):
            raise FormalizationError(f"assertion of step {step.index} is not exactly one term")
        tokens = _tokens(out.assertion)
        if "!" in tokens or any(t.startswith(":") for t in tokens):
            raise FormalizationError(f"assertion of step {step.index} has a `!` annotation")
        candidate = self.vocabulary.merged(out.declarations)
        used = self._used | _tokens(self.vocabulary.smt())
        for name in candidate.declarations.keys() - self.vocabulary.declarations.keys():
            if name in used:
                raise FormalizationError(
                    f"declaration of {name!r} would change the meaning of an earlier step"
                )
        full_decls = candidate.smt()
        try:
            z3.parse_smt2_string(f"{full_decls}\n(assert {out.assertion})", ctx=_parse_context())
        except z3.Z3Exception as exc:
            raise FormalizationError(f"invalid SMT for step {step.index}: {exc}") from exc

        self.vocabulary = candidate
        self._used |= tokens
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
        "Rules (a step that breaks one fails formalization):",
        "- Every declared name is a plain identifier [A-Za-z_][A-Za-z0-9_]*, never a "
        "|quoted| symbol, and not an SMT-LIB reserved word or Z3 builtin, with or without "
        "arguments (for example not map, union, default, sin, equals, if_then_else).",
        "- Do not declare a name that an earlier step already used.",
        "- declarations holds only declare-const, declare-fun or declare-sort lines, one per line.",
        "- assertion is exactly one Bool term: no SMT commands, no second term, no ! "
        "annotations (:named, :pattern), and no | or ; anywhere (so no ; comments).",
        "",
        'Reply with JSON: {"declarations": "...", "assertion": "..."}',
    ]
    return "\n".join(lines)


_DECL_HEADS = frozenset({"declare-const", "declare-fun", "declare-sort"})

_SMT_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "assert", "check-sat", "check-sat-assuming", "declare-const", "declare-datatype",
        "declare-datatypes", "declare-fun", "declare-sort", "define-fun", "define-fun-rec",
        "define-funs-rec", "define-sort", "echo", "exit", "get-assertions",
        "get-assignment", "get-info", "get-model", "get-option", "get-proof",
        "get-unsat-assumptions", "get-unsat-core", "get-value", "pop", "push",
        "reset", "reset-assertions", "set-info", "set-logic", "set-option",
    }
)  # fmt: skip

# SMT-LIB core, Ints/Reals/Arrays theory symbols, sorts, syntax keywords and
# commands, plus common Z3 extensions. Z3 lets a declaration shadow most of them;
# `_is_z3_builtin` covers the rest, but the parser reports some of these (`ite`,
# `let`, sorts) as unknown constants, so this list stays.
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
        # Z3 extensions
        "rem", "^", "iff", "implies", "if", "divisible", "pi", "euler", "root-obj",
        "const", "lambda",
    }
    | _SMT_COMMANDS
)  # fmt: skip

_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@functools.lru_cache(maxsize=4096)
def _is_z3_builtin(name: str) -> bool:
    """True if Z3 already knows `name` as a term or a sort with no declaration (AUDIT-9 #1).

    Uses the low-level parser in this thread's context; any error other than
    "unknown constant/sort <name>" means the parser resolved the name.
    """
    ctx = _parse_context()
    probes = (
        (f"(assert (= {name} {name}))", f"unknown constant {name}"),
        (f"(declare-const x {name})", f"unknown sort '{name}'"),
    )
    for smt, unknown in probes:
        try:
            z3.Z3_parse_smtlib2_string(ctx.ref(), smt, 0, None, None, 0, None, None)
        except z3.Z3Exception as exc:
            msg = exc.value.decode() if isinstance(exc.value, bytes) else str(exc.value)
            if unknown not in msg:
                return True
        else:
            return True
    return False


_COERCIBLE: Final[dict[str, tuple[str, ...]]] = {"Int": ("Int", "Real"), "Real": ("Real", "Int")}


@functools.lru_cache(maxsize=4096)
def _applied_builtin(decl: str, sort_decls: tuple[str, ...]) -> bool:
    """True if Z3 resolves the declared name, applied as `decl` declares, with no declaration.

    Z3 resolves a user function only at its declared arity, with Int and Real
    coerced, so probing those argument sorts covers every use the declaration
    could take over (AUDIT-13 #1). An unknown sort is left to the parse check.
    """
    tokens = decl[1:-1].replace("(", " ( ").replace(")", " ) ").split()
    head, name = tokens[0], tokens[1]
    if head == "declare-sort":
        arity = int(tokens[2]) if len(tokens) > 2 and tokens[2].isdigit() else 0
        if arity == 0:
            return False
        uses = [f"(declare-const x ({name}{' Int' * arity}))"]
    else:
        args = _sexprs(tokens[3:]) if head == "declare-fun" and tokens[2] == "(" else []
        if not args:
            return False
        uses = []
        for sorts in itertools.product(*(_COERCIBLE.get(a, (a,)) for a in args)):
            consts = "".join(f"(declare-const a{i} {s})" for i, s in enumerate(sorts))
            app = f"({name} {' '.join(f'a{i}' for i in range(len(sorts)))})"
            uses.append(f"{consts}(assert (= {app} {app}))")
    ctx = _parse_context()
    for use in uses:
        try:
            z3.Z3_parse_smtlib2_string(
                ctx.ref(), "".join(sort_decls) + use, 0, None, None, 0, None, None
            )
        except z3.Z3Exception as exc:
            msg = exc.value.decode() if isinstance(exc.value, bytes) else str(exc.value)
            if "unknown constant" not in msg and "unknown sort" not in msg:
                return True
        else:
            return True
    return False


def _sexprs(tokens: list[str]) -> list[str]:
    """Split tokens up to the first unmatched `)` into top-level S-expressions."""
    out: list[str] = []
    depth, cur = 0, []
    for tok in tokens:
        if tok == ")" and depth == 0:
            break
        cur.append(tok)
        depth += {"(": 1, ")": -1}.get(tok, 0)
        if depth == 0:
            out.append(" ".join(cur).replace("( ", "(").replace(" )", ")"))
            cur = []
    return out


def _tokens(text: str) -> set[str]:
    """Every symbol, keyword and literal token in `text`."""
    return {m.group(1) for m in _TERM_TOKEN.finditer(text)} - {"(", ")"}


_TERM_TOKEN: Final = re.compile(r'\s*("(?:[^"]|"")*"|[()]|[^\s()"]+)')


def _is_single_term(text: str) -> bool:
    """True if `text` is exactly one atom or balanced S-expression with no SMT command.

    So the assertion cannot close its `(assert ...)` and run commands such as
    `declare-fun` or `set-option` (AUDIT-9 #2). Quoted symbols and comments are refused.
    """
    if any(c in text for c in "|;"):
        return False
    pos, end, depth, done, prev = 0, len(text.rstrip()), 0, False, ""
    while pos < end:
        m = _TERM_TOKEN.match(text, pos)
        if m is None or done:
            return False
        tok, pos = m.group(1), m.end()
        if tok == ")":
            depth -= 1
            if depth < 0:
                return False
        elif tok == "(":
            depth += 1
        elif prev == "(" and tok in _SMT_COMMANDS:
            return False
        done, prev = depth == 0, tok
    return done


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
