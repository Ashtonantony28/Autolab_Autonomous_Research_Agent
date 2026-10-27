# AUDIT-39: API and SDK consistency, round 10

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg35-stopped-newcomers` @ `1e537af` (SG35 on top of the round-9 merges). Branch `claude/audit-39`.
- **Scope:** what changed since round 9 (`git diff 10ae977..1e537af -- src scripts docs`):
  - `verification/solver.py`: `_Interrupt.pause()/resume()/paused()` replace `_worker.overhead_s`; `_Interrupt.request()` now returns whether it stopped a target; `_Slots.arrive()` and `_Slots.acquire(short, new, arrival, lend)`; `run`'s re-queue and its new "`fn` must be pure" docstring.
  - `verification/solver_workers.py`: `Workers._adopt` removes the owner's workers from `multiprocessing.process._children`.
  - ADR 0009's SG35 amendment, and PLAN.md's SG35 row.
  - No agent, orchestrator, CLI, config, model or LLM-client code changed, so the SDK surface is as round 6 left it.
- **Re-checked:** AUDIT-35 #2 and #3, AUDIT-31 #1 to #6, and AUDIT-21 #1 / AUDIT-23 #1.
- **Repros:** `tests/audit/test_audit_39.py` has 3 strict-xfail tests (#1, #2, #3) and 1 passing control for #2. Each xfail asserts the correct behaviour. Under `--runxfail` all 3 fail on their stated assertion, 3 of 3 runs; without it the file gives 1 passed, 3 xfailed, 3 of 3 runs. There are no timing assertions, no network and no LLM. #4 is docs and records only, with `file:line` citations.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | Medium | PLAN.md marks SG35 "done (PR pending)" while two of its assigned flips (AUDIT-34 #2, #3) are still strict xfails and the ADR says an owner's choice is needed. The plan's rules call for stop-and-report (`blocked`) | test |
| 2 | Low | SG35's fork-exit fix keys on `Workers._live`, not on `_children`. A worker that has started but is not yet in `_live` when the fork happens is still SIGTERMed by the child's normal exit, and the owner's check on it comes back `worker_died (signal 15)` | test |
| 3 | Low | `run`'s new contract ("`fn` must be pure") is not enough. `stopped = interrupt.request()` assumes `fn` makes at most one check. A pure `fn` of two checks, stopped between them, gets back "interrupted" from an open executor | test |
| 4 | Low | Docs and records: AUDIT-35 #2's strict xfail now contradicts the documented contract; `run`'s ms covers only the last call; one more private-stdlib access; stale ADR bullet; no Result, Log or Resolution lines; Owns and Flip lists incomplete | citations |

**Count:** 0 critical, 0 high, 1 new medium, 3 new low.

**Carried over, all still open:**
- AUDIT-31 #1 (medium). #2 here adds another trigger.
- AUDIT-21 #1 / AUDIT-23 #1 (medium), open since round 5.
- AUDIT-31 #2 to #6 (low).
- AUDIT-35 #2 (low). It is half-addressed: see #4.
- AUDIT-35 #3 (low). It is partly fixed and partly widened: see #4.

**Why nothing is High.** No finding met the High bar.
- **Soundness held.** No path found here returns VALID for a check that is not entailed. #2 and #3 both fail closed (UNGROUNDED). #2 can only reach a pass through the still-open AUDIT-31 #1.
- **#1 is a process finding.** It is Medium, not Low, because it moves an open item that needs an owner's decision out of the plan's status table and into an ADR bullet.

---

## 1. Medium: SG35 is "done" with two of its flips still open

**What the plan says.**
- **Rules** (`PLAN.md:9`): "**Stop and report** if a task fails its verification 3 times, or if a fix needs a change to an SG0 interface. Do not work around it."
- **Status legend** (`PLAN.md:3`): `todo · active · blocked · done`.
- **SG35's flip list** (`PLAN.md:465`): "**Flip:** AUDIT-34 #1 ×2, #2, #3, AUDIT-35 #1, AUDIT-36 #1."
- **SG35's row** (`PLAN.md:53`): "done (PR pending)", with the title "(AUDIT-34 #1–#3, …)".

**What the code and tests say.**
- **AUDIT-34 #2 is still open.** `tests/audit/test_audit_34.py:215` and `:232` are still `xfail(strict=True, reason="AUDIT-34 #2")`.
- **AUDIT-34 #3 is still open.** `tests/audit/test_audit_34.py:250` (`(peak, calls) == (2, 3)`) is still `xfail(strict=True, reason="AUDIT-34 #3")`.
  - The commit message says the retuned tests "guard the slow cap … again (AUDIT-34 #3)". It added `slow_peak == 1` to `test_slow_checks_are_bounded_by_slow_workers`.
  - It kept `peak <= 3`, `calls in (3, 4)` and `threads <= 3`, which are the weakened asserts AUDIT-34 #3 named. So #3's own repro cannot flip until #2 is fixed.
- **The ADR asks for a decision.** The SG35 amendment (`docs/decisions/0009-…md:75`) says "AUDIT-34 #2 stays open … Keeping both needs an owner's choice between the bound, the AUDIT-30 #1 loads and SG30's aging bound."
  - That is the plan's "stop and report" case, and the row should read `blocked`, or at least not `done`.
  - Today the only place an owner learns a decision is pending is an ADR bullet.
- **Nothing else records SG35.** PLAN.md has no SG35 **Result** paragraph and no Log entry. The Verify list ("`make test` green 5 times under load · error injection (34/34, 26/26) · bench") is recorded nowhere.

**Repro.** `test_a_done_sub_goal_leaves_none_of_its_flips_open`:
1. It parses SG35's **Flip:** list from PLAN.md.
2. It looks for each item as a remaining `reason="AUDIT-n #m"` strict xfail under `tests/audit/`.

Observed, 3 of 3 runs:

```
AssertionError: ['AUDIT-34 #2', 'AUDIT-34 #3']
```

**Fix.** Mark SG35 `blocked` on the owner's choice, or narrow its flip list and title to what it flipped. Move AUDIT-34 #2 and #3 into a new row. Add the Result paragraph with the Verify numbers, and a Log line.

## 2. Low: a worker started at the fork is still killed by the child's exit

**What the docs claim.**
- ADR 0009 SG35 amendment (`0009-…md:74`): a forked child "drops the owner's worker processes from multiprocessing's `process._children`, so its normal exit … no longer sends them SIGTERM".
- `solver_workers.py:12-13`: the child "never uses, or kills, the owner's workers".

**What the code does.**
- `Workers._adopt` (`solver_workers.py:186-195`) discards from `_children()` only the processes of workers in `self._live`.
- `Workers._start` (`:209-213`) builds `Worker(...)`, whose `process.start()` adds the process to `multiprocessing.process._children`. Only after that returns does it take the lock and add the worker to `_live`. A fork in that window gives a child whose `_children` lists the new worker and whose `_live` does not.
- The child's normal exit then SIGTERMs that worker through `util._exit_function`. The child's stderr shows the same `AssertionError: can only join a child process` as AUDIT-35 #1.
- `acquire` starts a worker exactly when a caller needs one (no idle worker), so the check the owner was about to run on it comes back `worker_died`.
- The same keying misses an idle worker that `acquire` found dead and dropped from `_live` without joining it. Its `Process` stays in the owner's `_children` until multiprocessing's next `_cleanup`, so a child forked meanwhile signals that pid at exit.

**Repro.** `test_a_worker_started_at_the_fork_survives_a_child_that_exits_normally`:
1. A subprocess wraps `Worker.__init__` to fork once right after the real `__init__` returns. This is the interleaving where another thread forks between `process.start()` and `_live.add`.
2. The child exits through `sys.exit(0)`.
3. The parent then runs an easy `check_entailment` on that worker.

Observed, 3 of 3 runs (5 of 5 by hand):

```
{"reason": "worker_died", "details": "worker died (signal 15)"}
```

The control `test_a_worker_started_at_the_fork_survives_a_child_that_exits_hard` uses `os._exit(0)` and gets `entailed`. So the harness can pass once the bug is fixed.

**Why Low.**
- **The window is narrow.** It runs from `_children.add` at the end of `Process.start` to `_live.add`, microseconds wide. A fork during the rest of a start hits AUDIT-33 #1 (held locks) instead.
- **It fails closed.** The check comes back UNGROUNDED.
- **It matters through AUDIT-31 #1.** It adds another trigger, a sibling process's exit, to the still-open AUDIT-31 #1: a non-final `worker_died` step passes.

**Fix.** In the at-fork hook, drop every process named `z3-worker` from `_children`. Alternatively clear `_children` the way multiprocessing's own `_bootstrap` does. Either way, key on what multiprocessing will signal, not on `_live`.

## 3. Low: `run`'s "pure" contract does not cover a `fn` with two checks

**What changed.**
- `SolverExecutor.run`'s docstring (`solver.py:809-813`) now says "`fn` must be pure: a newcomer's call stopped on a lent place is made again". This answers AUDIT-35 #2's suggested fix.
- SG35 also changed the stop to `stopped = interrupt.request()` (`:842`). `request()` returns True only if a target was attached when it ran (`:514-523`), so that "a stop that comes too late keeps the result" (ADR `:73`).
- `request()` still sets `_requested`, which refuses every later `attach` (`:525-532`). `check_entailment` returns `_INTERRUPTED` when `interrupt.requested()` (`:236-237`).

**The gap.** Suppose `fn` makes two checks, and the stop lands after the first has detached and before the second attaches. Then:
1. `request()` finds no target and returns False, so `run` does not re-run.
2. The second check returns UNGROUNDED/`timeout` "interrupted".
3. `run` hands that back as `fn`'s result, though the executor was never shut down.

The `shutdown` docstring (`:903-909`) names shutdown as the only source of "interrupted" results. `fn` is pure in this case. The real precondition, "at most one Z3 check per `fn`", is stated nowhere.

**Repro.** `test_a_stop_between_two_checks_of_one_fn_is_never_its_result`:
- `SolverExecutor(1, 0)` gets a hog check, which sticks its slot and lends.
- A newcomer runs a `fn` that makes two checks: a 1 s hard check, then an easy check.
- The test fixes the interleaving with events. `_yields` returns True only after the first check has left its worker, and the `fn` makes its second check only after `request` has run.

Observed, 3 of 3 runs:

```
SolverOutcome(verdict='UNGROUNDED', reason='timeout', details='interrupted', counter_model='')
```

The asserted result is `entailed` (that is, the call is re-run).

**Impact.** No caller in `src/` does this. `vericot.py:217` passes one `check_entailment`. So this is an API edge, and it fails closed.

**Fix.** Treat a stop whose `request` found no target as a re-run whenever `fn` still has to call `check_entailment`. For example, re-run when the result is `_INTERRUPTED` and the executor is open. Or state "at most one check per `fn`" in `run`'s docstring.

## 4. Low: docs and records

- **AUDIT-35 #2 now contradicts the contract.**
  - `test_audit_35.py::test_run_calls_fn_once` is still a strict xfail asserting `calls == 1`. `run`'s docstring now says the call "is made again".
  - Either the finding is resolved by documentation, and the test should be retired with a **Resolution** line in AUDIT-35.md, or the owner wants once-only semantics, and the docstring documents a bug.
  - Today the docstring documents one and the test asserts the other.
- **`run`'s ms.**
  - The docstring still says "its run time in ms, queueing excluded" (`solver.py:810`).
  - Since SG35 the ms also excludes worker start, boot and replacement (`_Interrupt.ran_s`, `:483-489`). After a re-run it covers only the last call: the stopped call's run time counts as neither run nor queueing.
  - The ADR states the first point, but the public docstring states neither.
- **More private stdlib state.** `solver_workers._children()` (`:280-284`) reads `vars(multiprocessing.process)["_children"]`. This adds to the private attributes AUDIT-35 #3 listed (`forkserver._forkserver._*`). No comment by the `requires-python = "<3.13"` pin or in the ADR records which CPython internals the fork path depends on. `solver_workers` still has no `__all__` (AUDIT-31 #4, #6).
- **Stale ADR bullet.**
  - SG32's "**What this does not bound.** Runs that start together are all newcomers, and a fair queue serves them in arrival order" (`0009-…md:60`) is still unstruck, though SG34 superseded it (AUDIT-35 #3).
  - SG35's amendment supersedes part of SG34's bullet in prose ("Supersedes … in one respect", `:72`). The superseded words at `:64` are not marked.
- **What AUDIT-35 #3 got fixed.** The `_Slots.acquire` docstring now names both stop conditions (`solver.py:683-687`). That part of AUDIT-35 #3 is fixed.
- **Ownership and flips.**
  - SG35's row owns `tests/unit/verification/test_solver_*.py`, yet the commit rewrote AUDIT-28's W09/W10 mutant strings (`tests/audit/test_audit_28.py:215-220`).
  - It flipped AUDIT-33 #3 (`test_audit_33.py:277`), which is in neither SG35's title nor its flip list.
  - The new mutants are still meaningful: W09/W10 each drop one `interrupt.pause()`, and both stay strict xfails.
- **Resolution lines.** AUDIT-29, 30, 33, 34, 35 and 36.md have none for the items SG34/SG35 flipped (AUDIT-29 #1; AUDIT-30 #1 ×2, #2; AUDIT-33 #3; AUDIT-34 #1 ×2; AUDIT-35 #1; AUDIT-36 #1). There is no Result paragraph for SG28 to SG35. This is the same gap as AUDIT-23 #6, AUDIT-27 #5, AUDIT-31 #6 and AUDIT-35 #3.

---

## Earlier findings re-checked

| Item | Holds? |
|---|---|
| **AUDIT-35 #2** (low): `run` may call `fn` twice | **Behaviour unchanged.** The repro still fails under `--runxfail` ("fn was called 2 times"). The docstring now states purity (see #3 and #4). |
| **AUDIT-35 #3** (low): SG34 docs and records | **Partly fixed:** the `acquire` docstring. **Still open:** the ADR `:60` bullet, Result and Resolution lines. **Widened:** `_children()`. |
| **AUDIT-31 #1** (medium): `worker_died` on a non-final step passes the trace | **Still open.** `blocking_reason`/`consistency_score` are unchanged. `test_audit_31.py::test_a_check_whose_worker_died_is_rerun_or_escalated_not_passed` fails under `--runxfail`. #2 adds a trigger. SG35's own re-run path (`run`'s loop) is still used only for scheduling stops, never for a dead worker. |
| **AUDIT-21 #1 / AUDIT-23 #1** (medium): approving an escalated design re-checks cost on the design's own figures | **Still open.** No `agents/` or `orchestrator/` change since round 6. `test_audit_21.py::test_approving_a_figure_request_still_pauses_on_the_larger_figure` (both cases) and `test_audit_23.py::test_approving_a_disagreement_still_pauses_on_the_estimated_cost` fail under `--runxfail`. |
| AUDIT-31 #2 (low): `Settings.solver_timeout_s` unbounded | Still open. The repro fails. |
| AUDIT-31 #3 (low): `MAX_PROBE_CHARS` against its docstring | Still open. The repro fails. |
| AUDIT-31 #4 (low): stdlib patch; `__main__` target | Still open. The repro fails (`REPLY None 'failed to start'`). Widened by #4. |
| AUDIT-31 #5 (low): `Verifier()` starts processes | Still open. `test_audit_27.py::test_no_worker_is_left_running_after_shutting_down_the_default_executor` fails. |
| AUDIT-31 #6 (low): records and API docs | Still open (see #4). |

## What held up

- **Pause and resume against the docs.** Each path through `check_entailment` matches the ADR's "excludes the time it starts, boots or replaces its worker, while it waits as well as after":
  - `pause()` before `acquire`, then `resume()` after `ready()`.
  - `pause()` again before `detach`/`discard`/`release`, then `resume()`.
  - The `BaseException` path leaves the interrupt paused, and `ran_s` subtracts the open pause up to `ended`.

  `_yields`, `hard` and `solver_ms` all read `ran_s`. `stuck` is withheld while `paused()`. The W09/W10 mutants (each drops one pause) remain strict xfails, so the timing is pinned.
- **Arrival order.**
  - `_Slots.acquire` inserts by `arrival`, so a stopped caller goes ahead of later arrivals.
  - `lend=False` with `_grant(short, new and lend)` never lends it a place again.
  - `_Waiter.lendable()` keeps it out of lent aging.
  - That matches the SG35 amendment and the new `test_a_caller_queued_again_keeps_its_arrival_and_is_lent_nothing`.
- **A stopped check is never a result.** With one check per `fn` (every caller in `src/` and `scripts/`), `request()` returns True only if it killed an attached worker. The stopped call's `_INTERRUPTED` is then dropped. A stop that finds the check detached keeps the real outcome.
- **Fork exit for the cases the ADR names.** AUDIT-35 #1 and AUDIT-36 #1 now pass: idle workers and a check in flight survive a child's `sys.exit`.
- **SDK.** No change since round 6. `call_structured` still uses `messages.parse(output_format=...)`, and no `json.loads` or regex was added on LLM output.
- **Callers of `run` and the purity contract.**
  - `vericot.py:217-218` passes one pure `check_entailment` partial.
  - The `scripts/audit_load_9.py` wrappers (`timed`, `counted`) deliberately count re-runs. They are instruments, not callers that rely on once-only semantics.
  - `test_slow_checks_are_bounded_by_slow_workers` accepts `calls in (3, 4)`.

  No `src/` caller breaks the contract.

## How this was measured

- **Reading.** I read CLAUDE.md, PLAN.md (rules, SG35 row and round-9 section), AUDIT-35.md, AUDIT-34 and AUDIT-36 (summaries), ADR 0009 and `git show 1e537af`. I grepped `overhead_s`, `_children`, `.run(`, `check_entailment`, `xfail(strict=True, reason="AUDIT-3[3-6]` and `Resolution` across `src/`, `scripts/`, `tests/`, `docs/` and the AUDIT files.
- **#1.** Parsing PLAN.md and `tests/audit/*.py` text. No code is run.
- **#2.** A subprocess script with `Worker.__init__` wrapped to fork once after the real start returns. The child ends with `sys.exit(0)`, or with `os._exit(0)` as the control. The script then runs an easy check in the parent. Run 5 times by hand and 3 times under pytest.
- **#3.** `SolverExecutor(1, 0)` with a hog check, polled until `_Slots.stuck`. `_yields` and `_Interrupt.request` are wrapped with `threading.Event`s, so the stop lands between the two checks of one `fn`. No sleeps decide the outcome.
- **Re-checks.** `uv run pytest tests/audit/test_audit_{21,23,27,31,35}.py --runxfail -p no:randomly`: 16 failed, 6 passed. Every open repro named above failed on its asserted line.
- **Stability.** `uv run pytest tests/audit/test_audit_39.py`, 3 runs: 1 passed, 3 xfailed each time. With `--runxfail`, 3 runs: 3 failed, 1 passed each time.

## Gate

- `make lint typecheck`: ruff clean (164 files formatted) and `mypy --strict` clean (149 source files).
- `uv run pytest tests/audit/test_audit_39.py -q`: 1 passed, 3 xfailed.
- `uv run pytest -m "not perf" -q -p no:randomly`: 1403 passed, 13 skipped, 59 deselected, 103 xfailed (including the 3 here). `make test` (with `perf`) was not run.

## Bottom line

**SG35's code matches its amendment for the cases it names:**
- A stopped newcomer keeps its arrival.
- Worker waits are excluded from run time, hardness and stops.
- A late stop keeps the result.
- A forked child's normal exit no longer kills the owner's listed workers.

**The SDK surface is unchanged and consistent.**

**What does not hold:**
- **The record.** SG35 is marked done with AUDIT-34 #2 and #3 unflipped and an owner decision pending. The plan's rules call for stop-and-report. There is no Result paragraph and no Log line.
- **Fork exit, at the edges.** It is keyed on `_live` rather than on what multiprocessing will signal, so a worker caught mid-start is still SIGTERMed.
- **`run`'s new contract.** "`fn` must be pure" is not the whole precondition: a pure two-check `fn` stopped between its checks gets "interrupted" back from an open executor.

**Still open:**
- AUDIT-31 #1: an unchecked non-final step passes.
- AUDIT-21 #1 / AUDIT-23 #1: an approved estimate disagreement skips the cost pause.

Neither has changed since rounds 8 and 5.
