"""AUDIT-29: encoding soundness, round 8. SG31-SG33's worker replies, start-up and probe budget.

Every test asserts the *correct* behaviour and is a strict xfail until its
finding is fixed; see `AUDIT-29.md`. The controls are plain tests. None of
them calls a real LLM, and none asserts an absolute time. No finding admits a
wrong trace: both make an honest check or step fail, closed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from autolab.state.models import ReasoningStep
from autolab.verification.formalizer import (
    MAX_ARITY,
    FormalizationError,
    Formalizer,
    Vocabulary,
    prompt_for,
)
from autolab.verification.llm import FakeLLMClient

_ROOT: Final = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# AUDIT-29 #1 (medium): SG32 starts the shared worker pool (and multiprocessing's
# process-wide forkserver) when a `Verifier` is built, so `build_graph()` now
# does too. A program that builds its graph once and then forks (a
# `multiprocessing.Pool` under Linux's default "fork" start method, a pre-forking
# server) gives each child the parent's `Workers` object: its idle workers are
# not the child's children (`is_alive` asserts) and the forkserver is not either
# (`waitpid` raises ChildProcessError). Every check in the child raises
# AssertionError or ChildProcessError, which `escalate_permanent_errors` does
# not catch. Before SG32 (41348b0) the same program verified in the child.

_FORK_SCRIPT: Final = """
import asyncio, json, multiprocessing as mp, time
from autolab.state.models import ReasoningStep, ReasoningTrace
from autolab.verification import solver
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier

STEPS = (("x is above 5", "premise", "(declare-const x Int)", "(> x 5)"),
         ("x is positive", "conclusion", "", "(> x 0)"))

def make():
    fake, vocab, steps = FakeLLMClient(), Vocabulary(), []
    for i, (claim, kind, decls, a) in enumerate(STEPS):
        step = ReasoningStep(index=i, claim=claim, claim_type=kind)
        reply = json.dumps({"declarations": decls, "assertion": a})
        fake.register("formalize", prompt_for(step, vocab), reply)
        vocab.merge(decls)
        steps.append(step)
    trace = ReasoningTrace(agent="design", steps=tuple(steps))
    return Verifier(fake, solver_timeout_seconds=5.0), trace

def run(q):
    verifier, trace = make()
    try:
        r = asyncio.run(asyncio.wait_for(verifier.verify(trace), 60))
        q.put(" ".join(v.verdict for v in r.step_verdicts))
    except BaseException as e:
        q.put(f"{type(e).__name__}: {e}")

if __name__ == "__main__":
    make()  # as build_graph() does at start-up
    pool, want = solver._workers(), solver.SOLVER_WORKERS + solver.SOLVER_SLOW_WORKERS
    started = solver._default_executor is not None  # False before SG32
    deadline = time.monotonic() + 30
    while started and len(pool._idle) < want and time.monotonic() < deadline:
        time.sleep(0.05)  # fork once prestart is done, not with its lock held
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=run, args=(q,))
    p.start()
    print("child:", q.get(timeout=120), flush=True)
    p.join(30)
    q2 = mp.Queue()
    run(q2)
    print("parent:", q2.get(), flush=True)
"""


@pytest.fixture(scope="module")
def forked_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Build a Verifier, fork, verify `x > 5 |= x > 0` in the child and then the parent."""
    script = tmp_path_factory.mktemp("audit29") / "fork_after_build.py"
    script.write_text(_FORK_SCRIPT)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = dict(line.split(": ", 1) for line in proc.stdout.splitlines() if ": " in line)
    assert "parent" in lines, proc.stdout + proc.stderr
    return lines


fork_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fork start method")


@fork_only
def test_the_parent_verifies_after_forking(forked_run: dict[str, str]) -> None:
    """Control: the process that built the Verifier still gets VALID for both steps."""
    assert forked_run["parent"] == "VALID VALID"


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-29 #1")
def test_a_child_forked_after_build_can_verify(forked_run: dict[str, str]) -> None:
    # Today: "AssertionError: can only test a child process" (then ChildProcessError
    # from the forkserver on later checks). Before SG32: "VALID VALID".
    assert forked_run["child"] == "VALID VALID"


# ---------------------------------------------------------------------------
# AUDIT-29 #2 (low): SG33 charges every line of a reply against
# `MAX_PROBE_CHARS`, including a line identical to one already in the vocabulary.
# That line needs no new probe (it was probed when first merged, and both probes
# are cached), and `Formalizer` accepts re-declaring it. So a reply that repeats
# the vocabulary, as the model often does, is refused once the vocabulary is
# large: after 133 one-constant steps, or with one `MAX_ARITY` function and 22
# new constants. The step fails formalization (UNGROUNDED), and the error tells
# the model to "declare fewer" symbols, which it did not. Before SG33 (7123eed)
# both replies were accepted.

_WIDE: Final = f"(declare-fun rate ({' '.join(['Int'] * MAX_ARITY)}) Real)"
_NEW: Final = "\n".join(f"(declare-const c{i} Int)" for i in range(30))


def test_the_new_constants_alone_fit_the_probe_budget() -> None:
    """Control: the step's new declarations, without the repeated line, are accepted."""
    vocab = Vocabulary().merged(_WIDE)

    assert len(vocab.merged(_NEW).declarations) == 31


def _formalize_both(second_decls: str) -> list[str]:
    """Formalize step 0 (declares `_WIDE`) then step 1 (`second_decls`); return their outcomes."""
    fake = FakeLLMClient()
    zero = " ".join(["0"] * MAX_ARITY)
    steps = (
        (ReasoningStep(index=0, claim="the rate is one", claim_type="premise"), _WIDE,
         f"(= (rate {zero}) 1.0)"),
        (ReasoningStep(index=1, claim="c0 is positive", claim_type="premise"), second_decls,
         "(> c0 0)"),
    )  # fmt: skip
    vocab = Vocabulary()
    for step, decls, assertion in steps:
        reply = json.dumps({"declarations": decls, "assertion": assertion})
        fake.register("formalize", prompt_for(step, vocab), reply)
        vocab.merge(decls)

    async def run() -> list[str]:
        formalizer, out = Formalizer(fake), []
        for step, _, _ in steps:
            try:
                await formalizer.formalize(step)
                out.append("ok")
            except FormalizationError as exc:
                out.append(str(exc))
        return out

    return asyncio.run(run())


@pytest.mark.xfail(strict=True, reason="AUDIT-29 #2")
def test_repeating_a_vocabulary_line_costs_no_probe_budget() -> None:
    outcomes = _formalize_both(f"{_WIDE}\n{_NEW}")

    # Today: step 1 fails, "declarations up to 'c21' need over 163840 characters
    # of builtin probes; declare fewer or narrower symbols".
    assert outcomes == ["ok", "ok"], outcomes


@pytest.mark.xfail(strict=True, reason="AUDIT-29 #2")
def test_a_reply_that_repeats_a_growing_vocabulary_is_accepted() -> None:
    vocab = Vocabulary()
    for step in range(200):
        reply = "\n".join([*vocab.declarations.values(), f"(declare-const k{step} Int)"])
        # Today: refused at step 133, with 133 constants in the vocabulary.
        vocab = vocab.merged(reply)
    assert len(vocab.declarations) == 200
