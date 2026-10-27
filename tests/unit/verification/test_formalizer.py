"""Tests for the vocabulary-growing formalizer."""

from __future__ import annotations

import functools
import itertools
import json
import pathlib
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
import z3
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from autolab.state.models import ClaimType, FormalizedStep, ReasoningStep, ReasoningTrace
from autolab.verification import formalizer
from autolab.verification.formalizer import (
    FormalizationError,
    Formalizer,
    Vocabulary,
    _parse_context,
    prompt_for,
)
from autolab.verification.llm import FakeLLMClient, LLMPurpose, SchemaT
from autolab.verification.vericot import Verifier


def _step(index: int, claim: str = "claim", claim_type: ClaimType = "inference") -> ReasoningStep:
    return ReasoningStep(index=index, claim=claim, claim_type=claim_type)


async def test_formalizer_returns_formula_and_accumulates_vocab() -> None:
    step0 = _step(0, "p is true", "premise")
    step1 = _step(1, "q follows from p", "inference")

    fake = FakeLLMClient()
    voc_empty = Vocabulary()
    fake.register(
        "formalize",
        prompt_for(step0, voc_empty),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": "p"}),
    )
    voc_one = Vocabulary({"p": "(declare-const p Bool)"})
    fake.register(
        "formalize",
        prompt_for(step1, voc_one),
        json.dumps({"declarations": "(declare-const q Bool)", "assertion": "(=> p q)"}),
    )

    f = Formalizer(fake)
    out0 = await f.formalize(step0)
    out1 = await f.formalize(step1)

    assert "p Bool" in out0.declarations
    assert out0.assertion == "p"
    # Vocabulary grew across calls and is included in step1's declarations.
    assert "p Bool" in out1.declarations
    assert "q Bool" in out1.declarations
    assert set(f.vocabulary.declarations.keys()) == {"p", "q"}


async def test_formalizer_rejects_non_json() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register("formalize", prompt_for(step, Vocabulary()), "not json at all")
    with pytest.raises(FormalizationError, match="JSON"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_empty_assertion() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "", "assertion": "   "}),
    )
    with pytest.raises(FormalizationError, match="assertion"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_non_object() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register("formalize", prompt_for(step, Vocabulary()), json.dumps(["array"]))
    with pytest.raises(FormalizationError, match="object"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_unknown_decl_form() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "garbage line", "assertion": "true"}),
    )
    with pytest.raises(FormalizationError, match="unrecognized declaration"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_conflicting_redeclaration() -> None:
    step0 = _step(0)
    step1 = _step(1)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step0, Vocabulary()),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": "p"}),
    )
    fake.register(
        "formalize",
        prompt_for(step1, Vocabulary({"p": "(declare-const p Bool)"})),
        json.dumps({"declarations": "(declare-const p Int)", "assertion": "(> p 0)"}),
    )
    f = Formalizer(fake)
    await f.formalize(step0)
    with pytest.raises(FormalizationError, match="re-declared"):
        await f.formalize(step1)


async def test_formalizer_rejects_invalid_smt() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": "(this is broken)"}),
    )
    with pytest.raises(FormalizationError, match="invalid SMT"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_schema_violation() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize", prompt_for(step, Vocabulary()), json.dumps({"declarations": "", "x": 1})
    )
    with pytest.raises(FormalizationError):
        await Formalizer(fake).formalize(step)


async def test_formalizer_uses_structured_call() -> None:
    calls: list[tuple[str, type[BaseModel]]] = []

    class _Recording(FakeLLMClient):
        async def call(self, purpose: str, prompt: str) -> str:
            raise AssertionError("formalizer must use call_structured")

        async def call_structured(
            self, purpose: str, prompt: str, schema: type[SchemaT]
        ) -> SchemaT:
            calls.append((purpose, schema))
            return schema.model_validate(
                {"declarations": "(declare-const p Bool)", "assertion": "p"}
            )

    out = await Formalizer(_Recording()).formalize(_step(0))
    assert out.assertion == "p"
    assert calls == [("formalize", FormalizedStep)]


async def test_unparseable_step_leaves_vocabulary_unchanged() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "(declare-const q Bool)", "assertion": "(and q r)"}),
    )
    f = Formalizer(fake)
    with pytest.raises(FormalizationError, match="invalid SMT"):
        await f.formalize(step)
    assert f.vocabulary.declarations == {}


async def test_conflicting_step_leaves_vocabulary_unchanged() -> None:
    step = _step(1)
    vocab = {"p": "(declare-const p Bool)"}
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary(dict(vocab))),
        json.dumps(
            {"declarations": "(declare-const q Bool)\n(declare-const p Int)", "assertion": "q"}
        ),
    )
    f = Formalizer(fake)
    f.vocabulary = Vocabulary(dict(vocab))
    with pytest.raises(FormalizationError, match="re-declared"):
        await f.formalize(step)
    assert f.vocabulary.declarations == vocab


def test_merge_is_atomic() -> None:
    v = Vocabulary({"p": "(declare-const p Bool)"})
    with pytest.raises(FormalizationError):
        v.merge("(declare-const q Bool)\n(declare-const p Int)")
    assert v.declarations == {"p": "(declare-const p Bool)"}
    with pytest.raises(FormalizationError):
        v.merge("(declare-const r Bool)\ngarbage")
    assert v.declarations == {"p": "(declare-const p Bool)"}


def test_merged_returns_copy() -> None:
    v = Vocabulary({"p": "(declare-const p Bool)"})
    w = v.merged("(declare-const q Bool)")
    assert set(w.declarations) == {"p", "q"}
    assert set(v.declarations) == {"p"}


@pytest.mark.parametrize(
    "bad", ["declare-const p Bool", "(declare-const)", "(define-fun p () Bool true)"]
)
def test_merge_rejects_malformed_declarations(bad: str) -> None:
    v = Vocabulary()
    with pytest.raises(FormalizationError, match="unrecognized declaration"):
        v.merge(f"(declare-const ok Bool)\n\n{bad}")
    assert v.declarations == {}


@pytest.mark.parametrize(
    "bad",
    [
        "(declare-const cures Bool) (assert cures)",
        "(declare-const k Bool) (define-fun cures () Bool true)",
        "(declare-const k Bool)(reset)",
        "(declare-const k Bool))",
        "(declare-fun f (Int) Bool",
        "(declare-const |k) (assert true| Bool)",
        "(declare-const k Bool) ; (assert k)",
        '(declare-const k "Bool")',
        "(declare-const ( Bool)",
        "(push)",
        "()",
    ],
)
def test_merge_rejects_anything_but_one_declaration_form(bad: str) -> None:
    # AUDIT-1 #2: no SMT command other than a declaration may ride along.
    with pytest.raises(FormalizationError, match="unrecognized declaration"):
        Vocabulary().merge(bad)


@pytest.mark.parametrize(
    ("decl", "name"),
    [
        ("(declare-const k Bool)", "k"),
        ("  (declare-fun f ((Array Int Int) Int) Bool)  ", "f"),
        ("(declare-sort S 0)", "S"),
        ("(declare-fun g () Real)", "g"),
    ],
)
def test_merge_accepts_single_declaration_forms(decl: str, name: str) -> None:
    assert name in Vocabulary().merged(decl).declarations


async def test_smuggled_assertion_fails_formalization() -> None:
    fake = FakeLLMClient()
    step = _step(0)
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "(declare-const c Bool) (assert c)", "assertion": "c"}),
    )
    f = Formalizer(fake)
    with pytest.raises(FormalizationError):
        await f.formalize(step)
    assert f.vocabulary.declarations == {}


class _RefusingLLM(FakeLLMClient):
    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        raise RuntimeError("model refused")


async def test_refusal_becomes_formalization_error() -> None:
    with pytest.raises(FormalizationError, match="no structured output"):
        await Formalizer(_RefusingLLM()).formalize(_step(0))


# Written out rather than read from `RESERVED_SYMBOLS`, so deleting a name from the set
# fails its case here (AUDIT-12 #3).
RESERVED_NAMES = (
    "true", "false", "not", "=>", "and", "or", "xor", "=", "distinct", "ite",
    "+", "-", "*", "/", "div", "mod", "abs", "<", "<=", ">", ">=",
    "to_real", "to_int", "is_int", "select", "store",
    "Bool", "Int", "Real", "Array", "String", "Seq", "RegLan", "BitVec",
    "FloatingPoint", "RoundingMode",
    "!", "_", "as", "let", "forall", "exists", "match", "par",
    "NUMERAL", "DECIMAL", "STRING", "BINARY", "HEXADECIMAL",
    "assert", "check-sat", "check-sat-assuming", "declare-const", "declare-datatype",
    "declare-datatypes", "declare-fun", "declare-sort", "define-fun", "define-fun-rec",
    "define-funs-rec", "define-sort", "echo", "exit", "get-assertions",
    "get-assignment", "get-info", "get-model", "get-option", "get-proof",
    "get-unsat-assumptions", "get-unsat-core", "get-value", "pop", "push",
    "reset", "reset-assertions", "set-info", "set-logic", "set-option",
    "rem", "^", "iff", "implies", "if", "divisible", "pi", "euler", "root-obj",
    "const", "lambda",
)  # fmt: skip


@pytest.mark.parametrize("name", RESERVED_NAMES)
def test_merge_rejects_reserved_symbols(name: str) -> None:
    # AUDIT-5 #1: a declaration must not shadow an SMT-LIB builtin.
    v = Vocabulary({"p": "(declare-const p Bool)"})
    with pytest.raises(FormalizationError, match="reserved symbol"):
        v.merge(f"(declare-const ok Bool)\n(declare-fun {name} (Real Real) Bool)")
    assert set(v.declarations) == {"p"}


@pytest.mark.parametrize(
    "name",
    ["gt", "not_ready", "and_gate", "Truth", "x_1", "_tmp", "absorbance", "sinus", "str_len"],
)
def test_merge_accepts_user_symbols_near_reserved_words(name: str) -> None:
    assert name in Vocabulary().merged(f"(declare-fun {name} (Real) Bool)").declarations


@pytest.mark.parametrize("name", ["str.len", "x-1", "a.b", "~", "^0", "/0", "as-array", "k'"])
def test_merge_rejects_names_that_are_not_plain_identifiers(name: str) -> None:
    # AUDIT-9 #1: dotted and punctuated Z3 builtins never reach the builtin probe.
    with pytest.raises(FormalizationError, match="not a plain identifier"):
        Vocabulary().merged(f"(declare-fun {name} (Real) Real)")


@pytest.mark.parametrize(
    "name",
    [
        "sin", "cos", "asin", "atan", "sinh", "subset", "union", "intersection",
        "setminus", "complement", "map", "default", "bvult", "bvadd", "bvneg", "bv2int",
        "int2bv", "ubv_to_int", "fp", "to_fp", "RNE", "Float32", "Unicode", "char",
        "pbeq", "pble", "pbge", "div0", "mod0",
    ],
)  # fmt: skip
def test_merge_rejects_z3_builtins_found_by_asking_z3(name: str) -> None:
    # AUDIT-9 #1: none of these is on the hand-written list.
    assert name not in RESERVED_NAMES
    with pytest.raises(FormalizationError, match="Z3 builtin"):
        Vocabulary().merged(f"(declare-fun {name} (Real) Real)")


def test_builtin_probe_ignores_earlier_declarations() -> None:
    z3.parse_smt2_string("(declare-const later Bool)", ctx=_parse_context())
    assert "later" in Vocabulary().merged("(declare-const later Bool)").declarations


@pytest.mark.parametrize(
    "assertion",
    [
        "a) (assert b",
        "p) (declare-fun = (Real Real) Bool) (assert true",
        "p) (set-option :rlimit 1) (assert p",
        "p) (reset-assertions) (assert p",
        "(not (set-option :rlimit 1))",
        "p q",
        "(p) q",
        "(and p",
        "p)",
        ") (assert p",
        "|p|",
        "p ; comment",
        '"unterminated',
    ],
)
async def test_assertion_must_be_exactly_one_term(
    assertion: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AUDIT-9 #2: rejected before any parse, so no command runs, not even `set-option`.
    parses: list[str] = []
    monkeypatch.setattr(z3, "parse_smt2_string", lambda s, **_: parses.append(s))
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": assertion}),
    )
    f = Formalizer(fake)
    with pytest.raises(FormalizationError, match="not exactly one term"):
        await f.formalize(step)
    assert parses == []
    assert f.vocabulary.declarations == {}


@pytest.mark.parametrize(
    "assertion",
    ["p", "  (not p)\n", '(= s "a) (assert true")', '(= s "say ""hi""")', '(= s ":named")'],
)
async def test_single_term_assertions_are_accepted(assertion: str) -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps(
            {
                "declarations": "(declare-const p Bool)\n(declare-const s String)",
                "assertion": assertion,
            }
        ),
    )
    assert (await Formalizer(fake).formalize(step)).assertion == assertion.strip()


async def test_reserved_declaration_makes_the_step_formalization_failed() -> None:
    fake = FakeLLMClient()
    steps = (_step(0, "x exceeds 1", "premise"), _step(1, "y holds", "premise"))
    fake.register(
        "formalize",
        prompt_for(steps[0], Vocabulary()),
        json.dumps({"declarations": "(declare-fun > (Real Real) Bool)", "assertion": "true"}),
    )
    fake.register(
        "formalize",
        prompt_for(steps[1], Vocabulary()),
        json.dumps({"declarations": "(declare-const y Bool)", "assertion": "y"}),
    )

    result = await Verifier(fake).verify(ReasoningTrace(agent="t", steps=steps))

    first, second = result.step_verdicts
    assert (first.verdict, first.reason) == ("UNGROUNDED", "formalization_failed")
    assert "reserved symbol '>'" in first.details
    assert second.verdict == "VALID"


def test_parse_context_is_per_thread_and_never_the_global_one() -> None:
    # AUDIT-6 #1: Z3's main context is not thread-safe.
    with ThreadPoolExecutor(1) as pool:
        other = pool.submit(_parse_context).result()
        assert pool.submit(_parse_context).result() is other
    here = _parse_context()
    assert here is _parse_context()
    assert here is not other
    assert z3.main_ctx() not in (here, other)


async def _formalize_all(
    steps: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch | None = None
) -> list[str | None]:
    """Formalize (declarations, assertion) pairs in order; None for success, else the error."""
    fake = FakeLLMClient()
    f = Formalizer(fake)
    out: list[str | None] = []
    for i, (decls, assertion) in enumerate(steps):
        step = _step(i)
        fake.register(
            "formalize",
            prompt_for(step, f.vocabulary),
            json.dumps({"declarations": decls, "assertion": assertion}),
        )
        try:
            await f.formalize(step)
        except FormalizationError as exc:
            out.append(str(exc))
        else:
            out.append(None)
    return out


@pytest.mark.parametrize(
    "decl",
    [
        "(declare-fun equals (Int Int) Bool)",
        "(declare-fun equals (Real Int Real) Bool)",
        "(declare-fun equiv (Bool Bool) Bool)",
        "(declare-fun if_then_else (Bool Int Int) Int)",
        "(declare-fun if_then_else (Int Int Int) Int)",
        "(declare-sort U 0)\n(declare-fun equals (U U) Bool)",
    ],
)
def test_merge_rejects_builtins_applied_as_declared(decl: str) -> None:
    # AUDIT-13 #1: Z3 resolves these only with arguments; Int and Real coerce.
    with pytest.raises(FormalizationError, match="Z3 builtin"):
        Vocabulary().merged(decl)


@pytest.mark.parametrize(
    "decl",
    [
        "(declare-fun equals (Int) Bool)",
        "(declare-fun if_then_else (Bool Int) Int)",
        "(declare-fun f (Real Real) Bool)",
        "(declare-sort Box 2)",
    ],
)
def test_merge_accepts_names_z3_does_not_resolve_at_the_declared_arity(decl: str) -> None:
    Vocabulary().merged(decl)


@pytest.mark.parametrize(
    "assertion",
    [
        "(! p :named false)",
        "(! p :named pi)",
        "(! p :named RNE)",
        "(and q (! p :named n))",
        "(forall ((x Int)) (! (> x 0) :pattern ((+ x 1))))",
        "(! p :weight 1)",
    ],
)
async def test_assertions_with_annotations_are_rejected(assertion: str) -> None:
    # AUDIT-13 #2: `:named` would define a symbol for every later check.
    (err,) = await _formalize_all([("(declare-const p Bool)\n(declare-const q Bool)", assertion)])
    assert err is not None and "annotation" in err


async def test_a_step_cannot_declare_a_name_an_earlier_step_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The structural half of AUDIT-13 #1: even a builtin the probes miss cannot be
    # declared once a step has used it, so no earlier step changes meaning.
    monkeypatch.setattr(formalizer, "_is_z3_builtin", lambda _name: False)
    monkeypatch.setattr(formalizer, "_applied_builtin", lambda *_: False)
    errors = await _formalize_all(
        [
            ("(declare-const a Real)", "(equals a 0.95)"),
            ("(declare-fun equals (Real Real) Bool)", "(equals a 0.5)"),
            ("(declare-const p Bool)", "(let ((t p)) t)"),
            ("(declare-const t Bool)", "t"),
            ("(declare-const s Bool)", "s"),
        ]
    )
    assert errors[0] is None and errors[2] is None and errors[4] is None
    for err in (errors[1], errors[3]):
        assert err is not None and "meaning of an earlier step" in err


async def test_a_step_may_reuse_its_own_and_earlier_declarations() -> None:
    errors = await _formalize_all(
        [
            ("(declare-const p Bool)\n(declare-const q Bool)", "(and p q)"),
            ("(declare-const p Bool)", "(not p)"),
        ]
    )
    assert errors == [None, None]


def test_formalize_prompt_states_what_is_rejected() -> None:
    # AUDIT-15 #2.
    text = prompt_for(_step(0), Vocabulary())
    for rule in ("plain identifier", "builtin", "|quoted|", ";", "commands", "one Bool term", "!"):
        assert rule in text


# --- Every identifier in libz3 (AUDIT-13's search, as a property) ---------------

_PROBE_SORTS = (
    "Int", "Real", "Bool", "String", "(Seq Int)", "(Array Int Int)", "(_ BitVec 8)",
    "(_ FloatingPoint 8 24)", "RoundingMode", "RegLan", "(Set Int)",
)  # fmt: skip
_SIGNATURES = tuple(
    sig for k in range(4) for sig in itertools.combinations_with_replacement(_PROBE_SORTS[:4], k)
)


@functools.cache
def _libz3_identifiers() -> list[str]:
    lib = pathlib.Path(z3.__file__).parent / "lib" / "libz3.so"
    raw = re.findall(rb"[A-Za-z_][A-Za-z0-9_]*", lib.read_bytes())
    return sorted({m.decode() for m in raw if len(m) < 64})


def _parse(smt: str) -> str:
    try:
        return str(z3.parse_smt2_string(smt, ctx=z3.Context()))
    except z3.Z3Exception:
        return "error"


@settings(max_examples=150, deadline=None)
@given(
    name=st.deferred(lambda: st.sampled_from(_libz3_identifiers())),
    sig=st.sampled_from(_SIGNATURES),
    use_sort=st.sampled_from(_PROBE_SORTS),
)
@example(name="equals", sig=("Real", "Real"), use_sort="Real")
@example(name="equals", sig=("Int", "Int"), use_sort="Real")
@example(name="equiv", sig=("Bool", "Bool"), use_sort="Bool")
@example(name="if_then_else", sig=("Bool", "Bool", "Bool"), use_sort="Bool")
@example(name="sin", sig=("Real",), use_sort="Real")
@example(name="false", sig=(), use_sort="Bool")
@example(name="pi", sig=(), use_sort="Real")
async def test_no_libz3_identifier_can_be_declared_or_named_over_a_builtin(
    name: str, sig: tuple[str, ...], use_sort: str
) -> None:
    decl = f"(declare-fun {name} ({' '.join(sig)}) Bool)"
    try:
        Vocabulary().merged(decl)
    except FormalizationError:
        pass
    else:
        # Accepted: Z3 resolves no use of the name at the declared arity, whatever
        # the argument sort, so the declaration takes nothing over.
        args = " ".join(f"a{i}" for i in range(len(sig)))
        consts = "".join(f"(declare-const a{i} {use_sort})" for i in range(len(sig)))
        use = f"({name} {args})" if sig else name
        assert _parse(f"{consts}(assert (= {use} {use}))") == "error"
    (err,) = await _formalize_all([("(declare-const p Bool)", f"(! p :named {name})")])
    assert err is not None


# --- SG24: a bounded applied probe (AUDIT-18 #1) ---------------------------------

# Every libz3 identifier Z3 resolves with 1-4 arguments of one sort (Int, Real or
# Bool), found by probing the whole list: the names an applied probe can hit.
# `foo` and `f` are controls.
_APPLIED_BUILTINS = (
    "Char", "NaN", "RNA", "RNE", "RTN", "RTP", "RTZ", "abs", "acos", "acosh", "and",
    "asin", "asinh", "atan", "atanh", "bit0", "bit1", "bit2bool", "bv2int", "bv2nat",
    "bvadd", "bvand", "bvashr", "bvcomp", "bvlshr", "bvmul", "bvnand", "bvneg", "bvnego",
    "bvnor", "bvnot", "bvor", "bvredand", "bvredor", "bvsaddo", "bvsdiv", "bvsdiv0",
    "bvsdiv_i", "bvsdivo", "bvsge", "bvsgt", "bvshl", "bvsle", "bvslt", "bvsmod",
    "bvsmod0", "bvsmod_i", "bvsmul_noovfl", "bvsmul_noudfl", "bvsmulo", "bvsrem",
    "bvsrem0", "bvsrem_i", "bvssubo", "bvsub", "bvuaddo", "bvudiv", "bvudiv0", "bvudiv_i",
    "bvuge", "bvugt", "bvule", "bvult", "bvumul_noovfl", "bvumulo", "bvurem", "bvurem0",
    "bvurem_i", "bvusubo", "bvxnor", "bvxor", "char", "complement", "concat", "const",
    "cos", "default", "distinct", "div", "div0", "equals", "equiv", "euler", "exists",
    "ext_rotate_left", "ext_rotate_right", "extract", "false", "forall", "fp", "if",
    "if_then_else", "iff", "implies", "int2bv", "int_to_bv", "intersection", "is",
    "is_int", "ite", "lambda", "let", "map", "match", "mkbv", "mod", "mod0", "not", "or",
    "pbeq", "pbge", "pble", "pi", "rem", "repeat", "roundNearestTiesToAway",
    "roundNearestTiesToEven", "roundTowardNegative", "roundTowardPositive",
    "roundTowardZero", "sbv_to_int", "select", "setminus", "sign_extend", "sin", "store",
    "subset", "tan", "to_fp", "to_fp_unsigned", "to_ieee_bv", "to_int", "to_real", "true",
    "ubv_to_int", "union", "xor", "zero_extend", "foo", "f",
)  # fmt: skip


def _resolves_anywhere(name: str, args: tuple[str, ...]) -> bool:
    """The pre-SG24 probe: every Int/Real combination of `args` (2^k parses)."""
    choices = [("Int", "Real") if a in ("Int", "Real") else (a,) for a in args]
    ref = _parse_context().ref()
    for sorts in itertools.product(*choices):
        use = formalizer._applied_use(name, sorts)
        try:
            z3.Z3_parse_smtlib2_string(ref, use, 0, None, None, 0, None, None)
        except z3.Z3Exception as exc:
            msg = exc.value.decode() if isinstance(exc.value, bytes) else str(exc.value)
            if "unknown constant" not in msg:
                return True
        else:
            return True
    return False


class _Parses:
    """Counts `Z3_parse_smtlib2_string` calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = z3.Z3_parse_smtlib2_string

        def counting(*args: object) -> object:
            self.count += 1
            return real(*args)

        monkeypatch.setattr(z3, "Z3_parse_smtlib2_string", counting)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("Int",), {("Int",), ("Real",)}),
        (("Real", "Bool"), {("Int", "Bool"), ("Real", "Bool")}),
        (("Int", "Real", "Int"), set(itertools.product(("Int", "Real"), repeat=3))),
        (("Bool", "U"), {("Bool", "U")}),
    ],
)
def test_numeric_variants_are_every_combination_up_to_three_arguments(
    args: tuple[str, ...], expected: set[tuple[str, ...]]
) -> None:
    variants = list(formalizer._numeric_variants(list(args)))
    assert len(variants) == len(set(variants))
    assert set(variants) == expected


@pytest.mark.parametrize("k", [4, 8, 16, 64])
def test_numeric_variants_are_linear_in_the_arity(k: int) -> None:
    variants = list(formalizer._numeric_variants(["Int", "Bool"] * k))
    assert len(variants) == len(set(variants)) == 2 * k + 2
    assert all(v[1::2] == ("Bool",) * k for v in variants)
    assert {("Int", "Bool") * k, ("Real", "Bool") * k} <= set(variants)


@pytest.mark.parametrize("k", [8, formalizer.MAX_ARITY // 3])
def test_a_wide_function_is_probed_with_a_bounded_number_of_parses(
    monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    # AUDIT-18 #1: a 16-argument function took 2^16 parses (21 s on the event loop).
    formalizer._applied_builtin.cache_clear()
    parses = _Parses(monkeypatch)
    decl = f"(declare-fun wide ({' '.join(['Int', 'Real', 'Bool'] * k)}) Bool)"
    assert "wide" in Vocabulary().merged(decl).declarations
    assert parses.count <= 2 * (2 * k) + 2 + 2  # the applied variants and the two bare probes


async def test_a_step_with_a_function_of_the_largest_arity_formalizes() -> None:
    formalizer._applied_builtin.cache_clear()
    k = formalizer.MAX_ARITY
    decl = f"(declare-fun wide ({' '.join(['Int'] * k)}) Bool)"
    (err,) = await _formalize_all([(decl, f"(wide {' '.join(['0'] * k)})")])
    assert err is None


@pytest.mark.parametrize(
    "decl",
    [
        "(declare-fun equals (Int Real Int Real Int Real Int Real) Bool)",
        "(declare-fun equiv (Bool Bool Bool Bool Bool Bool) Bool)",
        "(declare-fun if_then_else (Bool Real Int) Real)",
        "(declare-fun bvadd (Int Int Int Int Int) Int)",
    ],
)
def test_the_bounded_probe_still_refuses_builtins_at_any_width(
    monkeypatch: pytest.MonkeyPatch, decl: str
) -> None:
    monkeypatch.setattr(formalizer, "_is_z3_builtin", lambda _name: False)
    formalizer._applied_builtin.cache_clear()
    with pytest.raises(FormalizationError, match="Z3 builtin"):
        Vocabulary().merged(decl)


@settings(max_examples=200, deadline=None)
@given(
    name=st.sampled_from(_APPLIED_BUILTINS),
    args=st.lists(st.sampled_from(("Int", "Real", "Bool")), min_size=1, max_size=6),
)
@example(name="equals", args=["Int", "Real", "Int", "Real"])
@example(name="if_then_else", args=["Bool", "Int", "Real"])
@example(name="foo", args=["Int", "Int", "Int", "Int"])
def test_the_bounded_probe_agrees_with_every_combination(name: str, args: list[str]) -> None:
    # Same soundness: the 2k + 2 variants refuse exactly what all 2^k combinations refuse.
    decl = f"(declare-fun {name} ({' '.join(args)}) Bool)"
    assert formalizer._applied_builtin(decl, ()) == _resolves_anywhere(name, tuple(args))


def test_the_probe_cache_ignores_sorts_the_declaration_does_not_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AUDIT-18 #1: each new `declare-sort` re-ran every later probe.
    formalizer._applied_builtin.cache_clear()
    decl = "(declare-fun g (U Int Int Int) Bool)"
    base = Vocabulary().merged("(declare-sort U 0)")
    base.merged(decl)
    wider = base.merged("(declare-sort V 0)\n(declare-sort W 0)")
    parses = _Parses(monkeypatch)
    wider.merged(decl)
    assert parses.count == 0


def test_a_declared_sort_still_reaches_the_applied_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # `equals` resolves on two U arguments only with U declared in the probe.
    monkeypatch.setattr(formalizer, "_is_z3_builtin", lambda _name: False)
    formalizer._applied_builtin.cache_clear()
    vocab = Vocabulary().merged("(declare-sort V 0)\n(declare-sort U 0)")
    with pytest.raises(FormalizationError, match="Z3 builtin"):
        vocab.merged("(declare-fun equals (U U) Bool)")


# --- SG24: names are tokenized as Z3 lexes them (AUDIT-20 #4, AUDIT-17 #5) ------


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("(! p:named false)", {"!", "p", ":named", "false"}),
        ("(let ((k#b1)) (= (bvor k#b0) #b1))", {"let", "k", "#b1", "=", "bvor", "#b0"}),
        ("(bvnot#b1)", {"bvnot", "#b1"}),
        ("(f#xA0g)", {"f", "#xA0", "g"}),
        ("(f 1a 2.5x 3.)", {"f", "1", "a", "2.5", "x", "3."}),
        ("(and b::x)", {"and", "b", ":", ":x"}),
        ('(= s "a b:c")', {"=", "s", '"a b:c"'}),
        ("(- x1 +y.z?)", {"-", "x1", "+y.z?"}),
        ("k'", {"k", "'"}),
    ],
)
def test_tokens_split_where_z3_splits(text: str, tokens: set[str]) -> None:
    assert formalizer._tokens(text) == tokens


def test_z3_ends_a_symbol_at_colon_and_hash() -> None:
    # The tokenizer's premise: `k#b1` is `k` and `#b1`; `x:y` is `x` and the keyword `:y`.
    smt = "(declare-fun f (Int (_ BitVec 1)) Bool)(declare-const k Int)(assert (f k#b1))"
    assert str(z3.parse_smt2_string(smt, ctx=z3.Context())[0]) == "f(k, 1)"
    with pytest.raises(z3.Z3Exception, match="keyword"):
        z3.parse_smt2_string("(declare-const x Bool)(assert (and x:y))", ctx=z3.Context())


@pytest.mark.parametrize(
    "assertion",
    [
        "(! p:named false)",
        "(! p:named pi)",
        "(and q (! p:named n))",
        "(! q:weight 1)",
        "(forall ((x Int)) (! (> x 0):pattern((+ x 1))))",
    ],
)
async def test_glued_annotations_are_rejected(assertion: str) -> None:
    # AUDIT-20 #4: with no space before the keyword.
    (err,) = await _formalize_all([("(declare-const p Bool)\n(declare-const q Bool)", assertion)])
    assert err is not None and "annotation" in err


@pytest.mark.parametrize(
    "first", ["(let ((k#b1)) (= (bvor k#b0) #b1))", "(let ((k#xA)) (= k #xA))"]
)
async def test_a_binder_glued_to_a_literal_counts_as_used(
    monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    # AUDIT-17 #5.
    monkeypatch.setattr(formalizer, "_is_z3_builtin", lambda _name: False)
    monkeypatch.setattr(formalizer, "_applied_builtin", lambda *_: False)
    errors = await _formalize_all([("", first), ("(declare-const k Bool)", "k")])
    assert errors[0] is None
    assert errors[1] is not None and "meaning of an earlier step" in errors[1]


async def test_a_builtin_glued_to_its_argument_counts_as_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `(bvnot#b1)` uses `bvnot`; with both probes out, the earlier-use rule must see it.
    monkeypatch.setattr(formalizer, "_is_z3_builtin", lambda _name: False)
    monkeypatch.setattr(formalizer, "_applied_builtin", lambda *_: False)
    errors = await _formalize_all(
        [("", "(= (bvnot#b1) #b0)"), ("(declare-fun bvnot ((_ BitVec 1)) (_ BitVec 1))", "true")]
    )
    assert errors[0] is None
    assert errors[1] is not None and "meaning of an earlier step" in errors[1]


# --- SG28: the applied probe is bounded by a declaration arity cap (AUDIT-22 #1) ----

_MAX = formalizer.MAX_ARITY


def _fun(k: int, sort: str = "Int", name: str = "wide") -> str:
    return f"(declare-fun {name} ({' '.join([sort] * k)}) Bool)"


class _ParsedChars:
    """Total characters handed to `Z3_parse_smtlib2_string`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.chars = 0
        real = z3.Z3_parse_smtlib2_string

        def counting(*args: object) -> object:
            text = args[1]
            assert isinstance(text, str)
            self.chars += len(text)
            return real(*args)

        monkeypatch.setattr(z3, "Z3_parse_smtlib2_string", counting)


@pytest.mark.parametrize(
    "decl",
    [
        _fun(_MAX + 1),
        _fun(_MAX + 1, "Bool"),
        _fun(1024),
        _fun(4096, "Real"),
        f"(declare-fun wide ({' '.join(['(Array Int Int)'] * (_MAX + 1))}) Int)",
        f"(declare-sort Wide {_MAX + 1})",
        "(declare-sort Wide 100)",
        f"(declare-sort Wide {'9' * 5000})",
    ],
)
def test_a_declaration_over_the_arity_cap_is_refused_before_any_parse(
    monkeypatch: pytest.MonkeyPatch, decl: str
) -> None:
    formalizer._applied_builtin.cache_clear()
    formalizer._is_z3_builtin.cache_clear()
    parses = _Parses(monkeypatch)
    with pytest.raises(FormalizationError, match=rf"arguments; at most {_MAX} allowed"):
        Vocabulary().merged(decl)
    assert parses.count == 0


@pytest.mark.parametrize(
    ("decl", "name"),
    [
        (_fun(_MAX), "wide"),
        (_fun(_MAX, "Bool"), "wide"),
        (f"(declare-fun wide ({' '.join(['(Array Int Real)'] * _MAX)}) Int)", "wide"),
        (f"(declare-sort Wide {_MAX})", "Wide"),
        (f"(declare-sort Wide 0{_MAX})", "Wide"),
    ],
)
def test_a_declaration_at_the_arity_cap_is_accepted(decl: str, name: str) -> None:
    formalizer._applied_builtin.cache_clear()
    assert name in Vocabulary().merged(decl).declarations


@pytest.mark.parametrize("digit", ["²", "٣", "①"])
async def test_a_non_ascii_sort_arity_fails_formalization_instead_of_escaping(digit: str) -> None:
    # `str.isdigit` accepts these but `int` does not: a bare ValueError escaped `merged`.
    (err,) = await _formalize_all([(f"(declare-sort S {digit})", "true")])
    assert err is not None and "invalid SMT" in err


@pytest.mark.parametrize("k", [1, 4, 16, _MAX])
def test_the_probe_parses_a_bounded_amount_of_text_up_to_the_cap(
    monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    formalizer._applied_builtin.cache_clear()
    formalizer._is_z3_builtin.cache_clear()
    parsed = _ParsedChars(monkeypatch)
    sorts = ["Int", "Real"] * (k // 2) + ["Bool"] * (k % 2)
    Vocabulary().merged(f"(declare-fun wide ({' '.join(sorts)}) Int)")
    assert parsed.chars <= (2 * k + 2) * (40 * k + 60) + 200
    assert parsed.chars <= 100_000


async def test_an_over_wide_step_fails_with_the_cap_in_its_reason() -> None:
    fake = FakeLLMClient()
    f = Formalizer(fake)
    step = _step(0)
    fake.register(
        "formalize",
        prompt_for(step, f.vocabulary),
        json.dumps({"declarations": _fun(1024), "assertion": "true"}),
    )
    with pytest.raises(FormalizationError, match=f"'wide' takes 1024 arguments; at most {_MAX}"):
        await f.formalize(step)
    assert f.vocabulary.declarations == {}


def test_the_prompt_states_the_arity_cap() -> None:
    assert f"at most {_MAX} arguments" in prompt_for(_step(0), Vocabulary())


def test_a_wide_line_after_valid_ones_leaves_the_merge_atomic() -> None:
    base = Vocabulary().merged("(declare-const p Bool)")
    with pytest.raises(FormalizationError, match="arguments"):
        base.merged(f"(declare-const q Bool)\n{_fun(_MAX + 1)}")
    assert base.declarations.keys() == {"p"}


@settings(max_examples=200, deadline=None)
@given(
    sorts=st.lists(
        st.sampled_from(("Int", "Real", "Bool", "(Array Int Int)", "(_ BitVec 4)")), max_size=48
    ),
    ret=st.sampled_from(("Int", "Bool")),
)
def test_the_cap_counts_top_level_argument_sorts(sorts: list[str], ret: str) -> None:
    decl = f"(declare-fun g ({' '.join(sorts)}) {ret})"
    assert formalizer._arity(formalizer._decl_tokens(decl)) == len(sorts)
    if len(sorts) > _MAX:
        with pytest.raises(FormalizationError, match="arguments"):
            Vocabulary().merged(decl)
    else:
        assert "g" in Vocabulary().merged(decl).declarations


@settings(max_examples=60, deadline=None)
@given(
    name=st.sampled_from(_APPLIED_BUILTINS),
    args=st.lists(st.sampled_from(("Int", "Real", "Bool")), min_size=7, max_size=8),
)
def test_the_bounded_probe_agrees_with_every_combination_at_wider_arities(
    name: str, args: list[str]
) -> None:
    decl = f"(declare-fun {name} ({' '.join(args)}) Bool)"
    assert formalizer._applied_builtin(decl, ()) == _resolves_anywhere(name, tuple(args))


# --- SG28: the tokenizer is linear in trailing whitespace (AUDIT-22 #1, low) --------


def test_tokens_of_text_with_long_trailing_blanks_stay_fast() -> None:
    # Each `\s*` used to scan to the end: 16 000 blanks took 12.6 s.
    assert formalizer._tokens("(and a b)" + " \t\n" * 20_000) == {"and", "a", "b"}


@settings(max_examples=300, deadline=None)
@given(
    text=st.text(alphabet=st.sampled_from(list('ab1:#x"() \t\n.')), max_size=40),
    tail=st.text(alphabet=" \t\n", max_size=8),
)
def test_tokens_match_the_unstripped_scan(text: str, tail: str) -> None:
    unstripped = {m.group(1) for m in formalizer._TERM_TOKEN.finditer(text + tail)} - {"(", ")"}
    assert formalizer._tokens(text + tail) == unstripped


# --- SG33: probe work per reply and sort nesting are bounded (AUDIT-26 #2) ---------

_NEST = formalizer.MAX_NESTING
_BUDGET = formalizer.MAX_PROBE_CHARS


def _nested(depth: int, inner: str = "Int") -> str:
    for _ in range(depth):
        inner = f"(Array Int {inner})"
    return inner


def _consts(n: int, sort: str = "Int") -> str:
    return "\n".join(f"(declare-const c{i} {sort})" for i in range(n))


def _funs(n: int, k: int, sort: str = "Int") -> str:
    return "\n".join(_fun(k, sort, f"f{i}") for i in range(n))


def _clear_probe_caches() -> None:
    formalizer._applied_builtin.cache_clear()
    formalizer._is_z3_builtin.cache_clear()


def _charge(decls: str, vocab: Vocabulary | None = None) -> int:
    """What `merged` charges `vocab` (default empty) for `decls`."""
    out = dict(vocab.declarations) if vocab else {}
    total = 0
    for line in filter(None, map(str.strip, decls.splitlines())):
        name = formalizer._decl_name(line)
        assert name is not None
        mentioned = formalizer._tokens(line)
        sorts = tuple(d for n, d in out.items() if d.startswith("(declare-sort") and n in mentioned)
        total += formalizer._probe_chars(formalizer._decl_tokens(line), line, sorts)
        out[name] = line
    return total


@pytest.mark.parametrize(
    "decls",
    [
        _funs(100, _MAX),  # AUDIT-26 #2: 6 800 parses, 1.8-3.2 s on the event loop
        _funs(300, 8),
        _funs(600, 1),
        _funs(2, _MAX, "Real"),
        _consts(1000),
        _consts(200, _nested(_NEST - 1)),
        f"{_fun(_MAX)}\n{_fun(_MAX, name='other')}",
        f"(declare-fun g ({' '.join(['Int'] * 16 + [_nested(_NEST - 2)] * 16)}) Bool)",
    ],
)
def test_a_reply_over_the_probe_budget_is_refused_before_any_parse(
    monkeypatch: pytest.MonkeyPatch, decls: str
) -> None:
    _clear_probe_caches()
    parses = _Parses(monkeypatch)
    with pytest.raises(FormalizationError, match=rf"over {_BUDGET} characters of builtin probes"):
        Vocabulary().merged(decls)
    assert parses.count == 0


@pytest.mark.parametrize(
    "decls",
    [
        _fun(_MAX),
        _fun(_MAX, "Real"),
        f"{_fun(_MAX)}\n{_consts(12)}",
        _consts(100),
        _funs(30, 2),
        f"(declare-fun g ({' '.join([_nested(_NEST - 1)] * _MAX)}) {_nested(_NEST - 1)})",
    ],
)
def test_a_reply_within_the_probe_budget_is_accepted(decls: str) -> None:
    _clear_probe_caches()
    assert _charge(decls) <= _BUDGET
    lines = [ln for ln in decls.splitlines() if ln.strip()]
    assert len(Vocabulary().merged(decls).declarations) == len(lines)


@settings(max_examples=150, deadline=None)
@given(
    n_sorts=st.integers(0, 3),
    sort_name=st.from_regex(r"Srt[a-z]{0,40}", fullmatch=True),
    sort_arity=st.integers(0, 3),
    lines=st.lists(
        st.tuples(
            st.sampled_from(("const", "fun", "sort")),
            st.lists(
                st.sampled_from(("Int", "Real", "Bool", "(Array Int Int)", "(Array Int Real)")),
                max_size=12,
            ),
            st.booleans(),
        ),
        min_size=1,
        max_size=10,
    ),
)
def test_the_charge_bounds_the_characters_the_probe_parses(
    n_sorts: int,
    sort_name: str,
    sort_arity: int,
    lines: list[tuple[str, list[str], bool]],
) -> None:
    sorts = [f"{sort_name}{i}" for i in range(n_sorts)]
    decls = [f"(declare-sort {s} 0)" for s in sorts]
    for i, (kind, args, use_sorts) in enumerate(lines):
        args = [*args, *sorts] if use_sorts else args
        if kind == "const":
            decls.append(f"(declare-const k{i} {args[0] if args else 'Int'})")
        elif kind == "fun":
            decls.append(f"(declare-fun k{i} ({' '.join(args)}) {sorts[0] if sorts else 'Int'})")
        else:
            decls.append(f"(declare-sort K{i} {sort_arity})")
    text = "\n".join(decls)
    _clear_probe_caches()
    with pytest.MonkeyPatch.context() as mp:
        parsed = _ParsedChars(mp)
        try:
            Vocabulary().merged(text)
        except FormalizationError as exc:
            refusal: str | None = str(exc)
        else:
            refusal = None
    if refusal is not None:
        assert "builtin probes" in refusal
        assert parsed.chars == 0
    else:
        assert parsed.chars <= _charge(text) <= _BUDGET


@pytest.mark.parametrize("names", [["Uu"], ["Uu", "Vv" * 200]])
def test_the_charge_counts_the_sort_declarations_each_probe_carries(
    monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> None:
    base = Vocabulary().merged("\n".join(f"(declare-sort {n} 0)" for n in names))
    decl = f"(declare-fun g ({' '.join(['Int'] * 8 + names)}) Bool)"
    _clear_probe_caches()
    parsed = _ParsedChars(monkeypatch)
    base.merged(decl)
    assert parsed.chars <= _charge(decl, base)
    assert _charge(decl, base) > _charge(decl)


def test_the_budget_is_charged_across_lines_not_per_line() -> None:
    one, other = _fun(_MAX), _fun(_MAX, name="other")
    assert _charge(one) <= _BUDGET < _charge(f"{one}\n{other}")
    assert "wide" in Vocabulary().merged(one).declarations
    with pytest.raises(FormalizationError, match="'other' need over"):
        Vocabulary().merged(f"{one}\n{other}")


def test_an_over_budget_reply_after_valid_lines_leaves_the_merge_atomic() -> None:
    base = Vocabulary().merged("(declare-const p Bool)")
    with pytest.raises(FormalizationError, match="builtin probes"):
        base.merged(f"(declare-const q Bool)\n{_funs(100, _MAX)}")
    assert base.declarations.keys() == {"p"}


@pytest.mark.parametrize(
    "decls",
    [
        # A builtin anywhere in a reply within budget is still found.
        f"{_consts(20)}\n(declare-fun equals (Int Real) Bool)",
        f"{_fun(_MAX)}\n(declare-fun if_then_else (Bool Real Int) Real)",
        # Over budget and shadowing a builtin: refused either way, never admitted.
        f"(declare-fun equals (Int Real) Bool)\n{_funs(100, _MAX)}",
        # A builtin line before a conflicting one: refused, whichever reason comes first.
        "(declare-fun union (Int Int) Int)\n(declare-const p Bool)\n(declare-const p Int)",
    ],
)
def test_the_budget_does_not_weaken_the_builtin_probe(decls: str) -> None:
    _clear_probe_caches()
    with pytest.raises(FormalizationError):
        Vocabulary().merged(decls)


@pytest.mark.parametrize(
    "decl",
    [
        f"(declare-fun g ({_nested(_NEST)} Int) Bool)",
        f"(declare-fun g (Int) {_nested(_NEST + 1)})",
        f"(declare-const g {_nested(_NEST + 1)})",
        f"(declare-fun g ({_nested(400)} Int) Bool)",  # AUDIT-26 #2: 0.3 s
        f"(declare-fun g ({_nested(1400)} Int) Bool)",  # AUDIT-26 #2: 4.2 s
        f"(declare-const g {'(Array Int ' * 5000}",  # also unbalanced: depth is checked first
    ],
)
def test_a_declaration_nested_over_the_cap_is_refused_before_any_parse(
    monkeypatch: pytest.MonkeyPatch, decl: str
) -> None:
    _clear_probe_caches()
    parses = _Parses(monkeypatch)
    with pytest.raises(FormalizationError, match=rf"more than {_NEST} deep"):
        Vocabulary().merged(decl)
    assert parses.count == 0


@pytest.mark.parametrize(
    "decl",
    [
        f"(declare-fun g ({_nested(_NEST - 1)} Int) Bool)",
        f"(declare-fun g (Int) {_nested(_NEST)})",
        f"(declare-const g {_nested(_NEST)})",
        f"(declare-fun g ((_ BitVec 8) {_nested(_NEST - 2, '(_ BitVec 4)')}) Bool)",
    ],
)
def test_a_declaration_nested_at_the_cap_is_accepted(decl: str) -> None:
    _clear_probe_caches()
    assert "g" in Vocabulary().merged(decl).declarations


def test_the_depth_refusal_reads_only_up_to_the_first_paren_past_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real, seen = formalizer._PAREN, [0]

    class _Counting:
        def finditer(self, text: str) -> Iterator[re.Match[str]]:
            for m in real.finditer(text):
                seen[0] += 1
                yield m

    monkeypatch.setattr(formalizer, "_PAREN", _Counting())
    assert formalizer._too_deep(f"(declare-fun g ({_nested(100_000)} Int) Bool)")
    assert seen[0] == _NEST + 2


@settings(max_examples=200, deadline=None)
@given(
    depth=st.integers(0, 3 * _NEST),
    inner=st.sampled_from(("Int", "(_ BitVec 4)")),
    fun=st.booleans(),
)
def test_the_nesting_cap_agrees_with_the_depth_of_the_sort(
    depth: int, inner: str, fun: bool
) -> None:
    sort = _nested(depth, inner)
    decl = f"(declare-fun g ({sort}) Bool)" if fun else f"(declare-const g {sort})"
    nesting = depth + inner.startswith("(") + fun
    assert formalizer._too_deep(decl) == (nesting > _NEST)


async def test_an_over_budget_step_fails_with_the_budget_in_its_reason() -> None:
    (err,) = await _formalize_all([(_funs(100, _MAX), "true")])
    assert err is not None and f"over {_BUDGET} characters of builtin probes" in err
    assert "declare fewer or narrower symbols" in err


async def test_an_over_nested_step_fails_and_the_next_step_still_formalizes() -> None:
    errors = await _formalize_all(
        [
            (f"(declare-fun g ({_nested(800)} Int) Bool)", "true"),
            ("(declare-fun g (Int) Bool)", "(g 0)"),
        ]
    )
    assert errors[0] is not None and f"more than {_NEST} deep" in errors[0]
    assert errors[1] is None


def test_the_prompt_states_the_nesting_cap_and_asks_for_few_symbols() -> None:
    text = prompt_for(_step(0), Vocabulary())
    assert f"at most {_NEST} deep" in text
    assert "only the symbols this step needs" in text
