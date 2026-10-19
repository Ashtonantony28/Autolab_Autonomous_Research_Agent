"""Property test: on random propositional traces the verifier agrees with a truth-table oracle."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from typing import Final

from hypothesis import event, given, settings
from hypothesis import strategies as st

from autolab.config import PASS_THRESHOLD
from autolab.state.models import ClaimType
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import GoldenStep, GoldenTrace, build

MAX_EXAMPLES: Final = 500
MIN_ATOMS: Final = 3
MAX_ATOMS: Final = 5
MAX_PREMISES: Final = 3
MAX_INFERENCES: Final = 3
MAX_LEAVES: Final = 6

# A formula is ("atom", name) | ("not", f) | (op, f, g) for op in and/or/=>.
Formula = tuple[object, ...]
Verdict = tuple[str, str]

_BINARY: Final[dict[str, Callable[[bool, bool], bool]]] = {
    "and": lambda a, b: a and b,
    "or": lambda a, b: a or b,
    "=>": lambda a, b: (not a) or b,
}


def _sub(f: Formula, i: int) -> Formula:
    child = f[i]
    assert isinstance(child, tuple)
    return child


def _smt(f: Formula) -> str:
    if f[0] == "atom":
        return str(f[1])
    if f[0] == "not":
        return f"(not {_smt(_sub(f, 1))})"
    return f"({f[0]} {_smt(_sub(f, 1))} {_smt(_sub(f, 2))})"


def _eval(f: Formula, env: Mapping[str, bool]) -> bool:
    if f[0] == "atom":
        return env[str(f[1])]
    if f[0] == "not":
        return not _eval(_sub(f, 1), env)
    return _BINARY[str(f[0])](_eval(_sub(f, 1), env), _eval(_sub(f, 2), env))


def _formulas(atoms: list[str]) -> st.SearchStrategy[Formula]:
    leaf = st.sampled_from(atoms).map(lambda a: ("atom", a))
    return st.recursive(
        leaf,
        lambda inner: st.one_of(
            inner.map(lambda f: ("not", f)),
            st.tuples(st.sampled_from(sorted(_BINARY)), inner, inner),
        ),
        max_leaves=MAX_LEAVES,
    )


@st.composite
def _traces(draw: st.DrawFn) -> tuple[list[str], list[tuple[ClaimType, Formula, tuple[int, ...]]]]:
    atoms = [f"a{i}" for i in range(draw(st.integers(MIN_ATOMS, MAX_ATOMS)))]
    formulas = _formulas(atoms)
    steps: list[tuple[ClaimType, Formula, tuple[int, ...]]] = [
        ("premise", draw(formulas), ()) for _ in range(draw(st.integers(1, MAX_PREMISES)))
    ]
    for _ in range(draw(st.integers(1, MAX_INFERENCES))):
        earlier = st.sampled_from([f for _, f, _ in steps])
        # Bias claims toward ones built from earlier steps so entailment actually occurs.
        claim = draw(
            st.one_of(
                formulas,
                earlier,
                st.tuples(st.just("or"), earlier, formulas),
                st.tuples(st.just("and"), earlier, earlier),
            )
        )
        refs = draw(st.lists(st.integers(0, len(steps) - 1), unique=True, max_size=len(steps)))
        kind: ClaimType = draw(st.sampled_from(["inference", "conclusion"]))
        steps.append((kind, claim, tuple(sorted(refs))))
    return atoms, steps


def _oracle(
    atoms: list[str], steps: list[tuple[ClaimType, Formula, tuple[int, ...]]]
) -> list[Verdict]:
    envs = [
        dict(zip(atoms, vals, strict=True))
        for vals in itertools.product((False, True), repeat=len(atoms))
    ]
    out: list[Verdict] = []
    for i, (kind, claim, refs) in enumerate(steps):
        if kind == "premise":
            out.append(("VALID", "assumed"))
            continue
        pool = [steps[j][1] for j in (refs or range(i)) if out[j][0] == "VALID"]
        if not pool:
            out.append(("UNGROUNDED", "no_premises"))
            continue
        models = [e for e in envs if all(_eval(p, e) for p in pool)]
        if not models:
            out.append(("INVALID", "premises_inconsistent"))
        elif all(_eval(claim, e) for e in models):
            out.append(("VALID", "entailed"))
        else:
            out.append(("INVALID", "not_entailed"))
    return out


@settings(max_examples=MAX_EXAMPLES, deadline=None)
@given(_traces())
async def test_verifier_matches_truth_table_oracle(
    case: tuple[list[str], list[tuple[ClaimType, Formula, tuple[int, ...]]]],
) -> None:
    atoms, steps = case
    decls = "\n".join(f"(declare-const {a} Bool)" for a in atoms)
    golden_steps = tuple(
        GoldenStep(f"step {i}", kind, decls if i == 0 else "", _smt(f), refs)
        for i, (kind, f, refs) in enumerate(steps)
    )
    expected = _oracle(atoms, steps)
    for _, reason in expected:
        event(reason)
    verdicts = tuple(v for v, _ in expected)
    overall = "INVALID" if "INVALID" in verdicts else "VALID"
    trace, fake = build(GoldenTrace("prop", "", overall, verdicts, golden_steps))

    result = await Verifier(fake).verify(trace)

    assert [(sv.verdict, sv.reason) for sv in result.step_verdicts] == expected
    if overall == "INVALID":
        assert result.consistency_score < PASS_THRESHOLD
