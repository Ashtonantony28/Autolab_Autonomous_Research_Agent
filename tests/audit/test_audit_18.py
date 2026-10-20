"""AUDIT-18: performance under load, round 5. Repros for the findings in `AUDIT-18.md`.

Every `xfail(strict=True)` test asserts the correct behaviour and fails today;
fixing the finding turns it into an XPASS, which fails the suite until the
marker is removed. Wall-clock repros carry `perf`; each orders its events with
events or by waiting on a condition, not by sleeping. Heavier measurements live in
`scripts/audit_load_5.py`. FakeLLMClient and stubs only; nothing touches the network.
"""

from __future__ import annotations

import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

import pytest
import z3

from autolab.state.models import ReasoningStep
from autolab.verification import formalizer as formalizer_mod
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import Formalizer, Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, Z3Solver
from scripts.audit_load_4 import cancel_release_s, timed_check
from scripts.audit_load_5 import (
    bool_chain,
    checkpoint_write_under_lock,
    easy_check_beside_a_slow_one,
    easy_verify_beside_hard_runs,
    figures_ms,
    int_function,
    oversubscribed,
    real_chain,
    real_int_ratio,
    tokenized_chars,
)

# --- #1 (critical): the applied-builtin probe is exponential in the arity ------------------

_ARITY: Final = 16
_PARSE_CAP: Final = 1000
"""Linear probing needs a few parses per argument; today a 16-ary function needs 2**16."""


class _ProbeLimit(Exception):
    pass


async def test_formalizing_a_wide_function_makes_a_bounded_number_of_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One LLM reply declaring a 16-argument Int function. `_applied_builtin` parses it
    # under every Int/Real combination of its arguments, 2**16 parses at ~0.3 ms each,
    # synchronously on the event loop: 21 s during which nothing else runs (each 2 more
    # arguments: 4x the time and memory). No timeout or cancel can fire. Stops at the cap.
    step = ReasoningStep(index=0, claim="f holds of 16 integers", claim_type="premise")
    zeros = " ".join(["0"] * _ARITY)
    fake = FakeLLMClient()
    reply = {"declarations": int_function(_ARITY), "assertion": f"(f{_ARITY} {zeros})"}
    fake.register("formalize", prompt_for(step, Vocabulary()), json.dumps(reply))
    real = z3.Z3_parse_smtlib2_string
    parses = [0]

    def counting(*args: Any) -> Any:
        parses[0] += 1
        if parses[0] > _PARSE_CAP:
            raise _ProbeLimit
        return real(*args)

    formalizer_mod._applied_builtin.cache_clear()
    monkeypatch.setattr(z3, "Z3_parse_smtlib2_string", counting)
    with suppress(_ProbeLimit):
        await Formalizer(fake).formalize(step)
    assert parses[0] <= _PARSE_CAP


# --- #2 (high): a stale idle permit stops the pool from growing ---------------------------


@pytest.mark.perf
async def test_an_easy_check_never_queues_behind_a_check_that_gave_up_its_slot() -> None:
    # One stale idle permit (the worker had not marked itself idle when the next job was
    # submitted; forced here, natural under load) makes the pool skip the thread
    # `_to_slow` made room for. The easy check then waits in the pool's queue for the
    # whole 3 s hard check, on the pool's only thread. Without the permit: 17 ms.
    took, threads = await easy_check_beside_a_slow_one(3.0)
    assert took < 1.0, f"easy check took {took:.2f} s on {threads} pool thread(s)"


# --- #3 (high): Real premise pools ignore the timeout and the cancel ----------------------

_REAL_POOL: Final = 1000
_OVERRUN_LIMIT: Final = 1.5
_SHARE: Final = 5
"""Timeouts and cancels land at 1/_SHARE of the check's own run time on this machine."""


def _real_premise_check() -> tuple[list[SmtFormula], SmtFormula, float]:
    """The premise check of step 1000 of a Real-valued trace, and its untimed wall s."""
    premises, claim = real_chain(_REAL_POOL, claim="true")
    return premises, claim, timed_check(premises, claim, 300.0)[1]


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-18 #3")
def test_a_real_premise_check_returns_within_its_timeout() -> None:
    # 999 chained `>` premises: Z3's consistency check never polls its limit, so a
    # timeout at a fifth of the check's run time is overrun ~5x. Over Int it holds.
    premises, claim, alone_s = _real_premise_check()
    timeout_s = alone_s / _SHARE
    reason, wall_s = timed_check(premises, claim, timeout_s)
    assert reason == "timeout"
    assert wall_s / timeout_s <= _OVERRUN_LIMIT, f"{wall_s:.2f} s of {timeout_s:.2f} s"


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-18 #3")
async def test_cancelling_a_real_premise_check_frees_its_worker_promptly() -> None:
    # Same check, cancelled a tenth of the way in: the re-send thread interrupts every
    # 5 ms, and the worker stays busy until the consistency check ends by itself.
    premises, claim, alone_s = _real_premise_check()
    freed_s, sent, _ = await cancel_release_s(premises, claim, after_s=alone_s / (2 * _SHARE))
    assert freed_s < alone_s / _SHARE, f"freed after {freed_s:.2f} s, {sent} interrupts"


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-18 #3")
def test_a_real_premise_pool_costs_no_more_than_its_int_twin() -> None:
    # The premise check re-checks all 999 earlier premises from scratch. Over Int it
    # takes ~0.2 s; over Real ~2.1 s (9-11x).
    real_ms, int_ms = real_int_ratio(_REAL_POOL)
    assert real_ms <= 4 * int_ms, f"Real {real_ms:.0f} ms, Int {int_ms:.0f} ms"


# --- #4 (medium): above `slow_workers`, hard checks hold every slot -------------------------


@pytest.mark.perf
async def test_hard_runs_above_the_slow_cap_do_not_starve_an_easy_verify() -> None:
    # 1 worker, room for 1 slow check, 3 hard runs whose checks run to a 0.5 s timeout.
    # The check that cannot go slow keeps the slot until a slow check ends, so every
    # check of the easy 10-step verify waits behind a hard check (alone: 0.05 s).
    timeout_s = 0.5
    took, _, _ = await easy_verify_beside_hard_runs(1, 3, timeout_s, slow_workers=1)
    assert took < timeout_s


# --- #5 (medium): the slow lane runs CPU-bound checks beyond the CPUs ------------------------


@pytest.mark.perf
def test_concurrent_checks_reach_the_verdict_they_reach_alone() -> None:
    # A child pinned to one CPU while os.cpu_count() says 4, as in a 1-CPU container on a
    # 4-CPU host: 2 workers, 8 slow. The timeout is 4x one lia_chain(1600) check's run
    # time alone. Ten at once all run (2 slots + 8 slow), share the CPU, and each reaches
    # its wall-clock deadline first. With slow_workers=0 all ten decide.
    alone_s = oversubscribed(0, cpus=4, n=1600, timeout_s=60.0)["alone_s"]
    report = oversubscribed(10, cpus=4, n=1600, timeout_s=4 * alone_s)
    assert report["alone"] == "not_entailed"
    assert report["reasons"] == ["not_entailed"] * 10


# --- #6 (medium): the LangGraph checkpointer gives up on a lock after 5 s -------------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-18 #6")
async def test_a_checkpoint_write_waits_out_a_lock_like_the_store_does(tmp_path: Path) -> None:
    # Another writer holds the checkpoint file's lock until the write returns (at most 8 s).
    # The store waits `SQLITE_BUSY_TIMEOUT_S` (30 s); `sqlite_saver` keeps aiosqlite's 5 s.
    outcome, took = await checkpoint_write_under_lock(tmp_path / "ckpt.db", hold_cap_s=8.0)
    assert outcome == "ok", f"{outcome} after {took:.2f} s"


# --- #7 (low): `_SETUP_SHARE` skips entailment checks that need no time ---------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-18 #7")
def test_a_premise_check_finishes_when_its_entailment_step_needs_no_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fake clock: the consistency check of a 10-premise Boolean pool "takes" 70% of a
    # 10 s timeout. 3 s are left, and the entailment step of a premise check (`claim!` is
    # `(not true)`) takes well under a millisecond, but the guard wants 3.5 s.
    timeout_s = 10.0
    offset = [0.0]

    class Clock:
        @staticmethod
        def monotonic() -> float:
            return time.monotonic() + offset[0]

        perf_counter = staticmethod(time.perf_counter)

    real_check = z3.Solver.check
    calls = [0]

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        try:
            return real_check(self, *assumptions)
        finally:
            calls[0] += 1
            if calls[0] == 1:
                offset[0] += 0.7 * timeout_s

    monkeypatch.setattr(solver_mod, "time", Clock)
    monkeypatch.setattr(z3.Solver, "check", check)
    premises, claim = bool_chain(10, claim="true")
    outcome = Z3Solver(timeout_s).check_entailment(premises, claim)
    assert outcome.verdict == "VALID", outcome.details


# --- #8 (low): the figure scan is quadratic in the protocol's numbers -----------------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-18 #8")
def test_the_figure_scan_is_linear_in_the_protocol() -> None:
    # `_words_after` copies the rest of the token list for every number. 4x the numbers
    # should cost about 4x; it costs about 30x.
    small, large = figures_ms(2000), figures_ms(8000)
    assert large <= 8 * small, f"{small:.1f} ms -> {large:.1f} ms"


# --- #9 (low): the earlier-use rule re-tokenizes the vocabulary for every step -------------


@pytest.mark.xfail(strict=True, reason="AUDIT-18 #9")
async def test_the_earlier_use_rule_tokenizes_each_declaration_once() -> None:
    # Formalizing a 200-step chain tokenizes the whole vocabulary again for every step:
    # 63x the SMT text the rule needs to see once.
    scanned, size = await tokenized_chars(200)
    assert scanned <= 4 * size, f"scanned {scanned} chars for {size}"
