"""AUDIT-14: performance under load, round 4. Repros for the findings in `AUDIT-14.md`.

Every `xfail(strict=True)` test asserts the correct behaviour and fails today;
fixing the finding turns it into an XPASS, which fails the suite until the
marker is removed. Wall-clock repros carry `perf`. Heavier measurements live in
`scripts/audit_load_4.py`. FakeLLMClient and stubs only; nothing touches the network.
"""

from __future__ import annotations

from typing import Final

import pytest

from scripts.audit_load_4 import (
    cancel_release_s,
    fast_trace_under_slow_runs,
    lia_chain,
    overrun_ratio,
)

_OVERRUN_LIMIT: Final = 1.5
"""A check may outlive its timeout by scheduling noise, not by multiples of it."""


# --- #1 (high): the assumption-based entailment check outlives its timeout ---------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-14 #1")
def test_a_check_returns_within_its_timeout() -> None:
    # 1599 linear integer premises (an uncited numeric pool) and a 0.5 s timeout. The
    # pre-SG19 add-based check answers in ~0.6 s; checking `claim!` as an assumption runs
    # into a phase Z3 does not interrupt, and returns `timeout` after ~4.1 s (8.3x).
    assert overrun_ratio(1600, 0.5) <= _OVERRUN_LIMIT


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-14 #1")
async def test_cancelling_a_check_frees_its_worker_promptly() -> None:
    # Same check with a 30 s timeout, cancelled 0.8 s in. The re-send thread interrupts it
    # every 5 ms (~600 times), but the worker stays busy for another ~3.2 s.
    premises, claim = lia_chain(1600)
    freed_s, _, _ = await cancel_release_s(premises, claim, after_s=0.8)
    assert freed_s < 0.5


# --- #2 (high): two hard runs starve every other run of the 2-worker pool ----------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-14 #2")
async def test_hard_runs_do_not_starve_an_easy_verify() -> None:
    # Two runs whose checks each run to a 0.5 s timeout hold both workers of the default
    # pool. An easy 10-step verify (0.05 s alone) then waits behind a hard check for
    # every one of its checks: ~3 s. On 8 workers it took 0.2 s.
    timeout_s = 0.5
    _, loaded = await fast_trace_under_slow_runs(workers=2, slow_runs=2, timeout_s=timeout_s)
    assert loaded < timeout_s
