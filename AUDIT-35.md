# AUDIT-35: API and SDK consistency, round 9

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg34-together-fork` @ `10ae977` (SG34 on top of the round-8 merges). Branch `claude/audit-35`.
- **Scope:** what changed since round 8 (`git diff f09ea60..10ae977 -- src scripts docs`): `verification/solver.py` (newcomer lending, `_LENT_AGING`, `_yields`, the re-run loop in `SolverExecutor.run`, `_adopt`, the at-fork hook), `verification/solver_workers.py` (pid check, `_adopt`, `_forget_forkserver`, the at-fork hook), ADR-0009's SG34 amendment, and PLAN.md. No agent, orchestrator, CLI, config, model or LLM-client code changed, so the SDK surface is as round 8 left it. I re-checked the open findings AUDIT-31 #1 to #6 and AUDIT-21 #1 / AUDIT-23 #1.
- **Repros:** `tests/audit/test_audit_35.py` has 2 strict-xfail tests, one each for #1 and #2. Each asserts the correct behaviour. Under `--runxfail` both fail on their stated assertion, 3 of 3 runs. There are no timing assertions, no network and no LLM. #3 is docs and records only, with `file:line` citations.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | Medium | A forked child that exits normally SIGTERMs every worker the owner has, idle and in flight. SG34's "never using or killing the parent's" holds only for `os._exit` | test |
| 2 | Low | `SolverExecutor.run(fn)` now may call `fn` twice. The public docstring does not say so, and the ADR's "every check is pure" is not part of the API | test |
| 3 | Low | SG34 docs and records: a stale ADR bullet, an incomplete `_Slots.acquire` docstring, more process-wide hooks on private stdlib state, no Result or Resolution lines | citations |

**Count:** 0 critical, 0 high, 1 new medium, 2 new low. Carried over, all still open: AUDIT-31 #1 (medium; #1 here gives it a new trigger), AUDIT-21 #1 / AUDIT-23 #1 (medium, open since round 5), and AUDIT-31 #2 to #6 (low).

No finding met the High bar. #1 fails closed on the step concerned: a killed check is `worker_died`, never VALID. It is Medium for three reasons:
- It contradicts what the SG34 amendment and the `solver_workers` module docstring promise.
- It hits the prefork-server case that AUDIT-30 #2 named.
- Through the open AUDIT-31 #1, a non-final step that was never checked still passes.

---

## 1. Medium: a forked child's normal exit kills the owner's workers

**Claim.**
- ADR-0009 SG34 amendment (`docs/decisions/0009-…md:67`): the child "starts its own, never using or killing the parent's (AUDIT-29 #1, AUDIT-30 #2)".
- `solver_workers.py:12-13`: "A process forked from the owner adopts its `Workers` empty … it never uses, or kills, the owner's workers."

**What the code does.**
- `Workers._adopt` (`solver_workers.py:186`) forgets the owner's `Worker` objects and closes the child's copies of the idle workers' pipes.
- The owner's `multiprocessing.Process` objects are still in the child's copy of `multiprocessing.process._children`. Only multiprocessing's own `_bootstrap` clears that set, and a raw `os.fork()` (a prefork server, a daemoniser, `pty.fork`) does not run it.
- Workers are started with `daemon=True` (`solver_workers.py:98`; ADR-0009 "Cleanup: Workers are daemonic").
- When the child exits through normal interpreter shutdown (`sys.exit`, or the end of `__main__`), multiprocessing's `atexit` hook `util._exit_function` calls `terminate()` (SIGTERM) on every daemonic process in `_children`. Those are the owner's workers. The hook then fails on `join()` with "can only join a child process".
- So every owner worker dies:
  - Idle workers die. Each later check pays a new worker's boot.
  - A check in flight in the owner comes back UNGROUNDED/`worker_died` (the worker got SIGTERM). Under the open AUDIT-31 #1, that step passes the trace when it is not the final step.
- The existing fork tests miss this because their child always leaves with `os._exit(0)`: `test_solver_workers.py::_FORKING`, `scripts/audit_load_8.py::FORK_CHILD`, and AUDIT-29's `mp.get_context("fork")`, whose `_bootstrap` clears `_children`.

**Repro.** `test_a_forked_child_that_exits_normally_leaves_the_owners_workers_alone` runs a script that:
1. waits for `default_executor()`'s pre-start to finish;
2. starts a runs-to-timeout check (`slow_check`, 5 s) in a thread;
3. waits until a worker is in use, then forks;
4. in the child, calls `sys.exit(0)`;
5. in the parent, waits for the child and the check.

Observed, 3 of 3 runs:

```
{'idle_alive': [False, False, False], 'in_flight': 'worker_died'}
child stderr: Exception ignored in atexit callback: <function _exit_function …>
              AssertionError: can only join a child process
```

When the same script's child uses `os._exit(0)`, it prints `{"idle_alive": [true, true, true], "in_flight": "timeout"}`. That is the asserted outcome, so the harness can pass once the bug is fixed.

**Fix.** In the at-fork child hook, or in `Workers._adopt`, remove the owner's workers' `Process` objects from `multiprocessing.process._children`, for idle and live workers alike, so the child's `atexit` never signals them. Pin this with a test whose child exits through `sys.exit`. Then the amendment's wording is true.

## 2. Low: `SolverExecutor.run` can run `fn` twice

- SG34 stops a newcomer's check on a lent place once it has run `_HARD_SLICES` slices, or one slice while a short caller waits (`solver.py:795`, `_yields` at `:832`). It then loops, queues the caller again for a slot (`:829`), and calls `fn` again. The first call's result is dropped unless the executor has closed (`:803`).
- The ADR justifies this with "A stopped check is re-run, as every check is pure" (`0009-…md:64`). `SolverExecutor` is public (`solver.__all__`), and `run(fn: Callable[[], T])` is documented only as "Run `fn` on the pool; return its result and its run time in ms, queueing excluded" (`:770`). The docstring states no purity or idempotence requirement. The returned ms covers only the last call, and the stopped call's time silently counts as queueing.
- The only caller in `src/` (`vericot.py:218`) passes a pure `check_entailment` partial, so nothing breaks today. A caller that counts, logs or meters inside `fn`, as the repro does, sees two runs.
- **Repro.** `test_run_calls_fn_once`:
  1. `SolverExecutor(1, 0)` gets a hog check (runs to a 20 s timeout) until its slot is stuck and lends.
  2. A newcomer task calls `run(counted)`. Its first call is hard and its second is easy.
  3. Once the newcomer has been stopped and queued again, the hog is cancelled.
  4. Observed, 3 of 3 runs: `fn was called 2 times`.
- **Fix.** State the contract in `run`'s docstring: `fn` must be pure, and it may run more than once when it holds a lent place. Or restrict re-runs to callables the executor knows are pure.

## 3. Low: SG34 docs and records

- **Stale ADR bullet.** The SG32 amendment's "**What this does not bound.** Runs that start together are all newcomers, and a fair queue serves them in arrival order" (`0009-…md:60`) is exactly what SG34 changes. The SG34 amendment supersedes only "It is lent no place" (`:64`). Unlike the struck-through main-module rule at `:44`, `:60` still reads as current.
- **`_Slots.acquire` docstring** (`solver.py:650-651`): "its caller stops the check once it shows it is hard". This omits the second stop condition, one slice while a short caller waits, which the ADR and `_yields` both state. A check stopped for that reason has not shown it is hard.
- **More process-wide state at import** (extends AUDIT-31 #4 and CLAUDE.md principle 5):
  - Two `os.register_at_fork` hooks are installed on import and cannot be removed (`solver_workers.py:297`, `solver.py:908`).
  - `_forget_forkserver` rewrites private CPython attributes of `multiprocessing.forkserver._forkserver`: `_forkserver_address`, `_forkserver_alive_fd`, `_forkserver_pid` and `_lock` (`solver_workers.py:277-288`). It does this in every `os.fork` child of any process that imports `autolab.verification`, including hosts that never build a `Verifier`.
  - The solver hook rebinds the module globals `_workers_lock` and `_default_lock` (`solver.py:902`).
  - The attribute set matches CPython 3.11 and 3.12, and `requires-python` is `<3.13`. The dependency on these internals is recorded in neither the ADR nor a comment beside the version pin.
  - `solver_workers` still has no `__all__` (AUDIT-31 #4, #6).
- **Records.**
  - PLAN.md marks SG34 "done (PR pending)" (`PLAN.md:52`), but there is no **Result** paragraph with the SG's own Verify numbers ("`make test` green 5 times under load · error injection · bench", `:457`). The ADR quotes the load numbers, but the plan has no record of the verify run.
  - AUDIT-29.md and AUDIT-30.md have no **Resolution** lines for the flipped AUDIT-29 #1 and AUDIT-30 #1 ×2 and #2.
  - This is the same gap AUDIT-23 #6, AUDIT-27 #5 and AUDIT-31 #6 reported.

---

## Earlier findings re-checked

| Item | Holds? |
|---|---|
| **AUDIT-31 #1** (medium): `worker_died` on a non-final step passes the trace | **Still open.** `blocking_reason` and `consistency_score` are unchanged, and ADR-0009 `:51` still says "the monitor's retry or escalation already re-runs the step". The repro still fails under `--runxfail` (PASS at 0.875). #1 adds a trigger that has nothing to do with the formula: a sibling process's exit. That undercuts the ADR's "a crash … would likely recur" argument against a re-run. SG34 itself re-runs stopped checks, so the executor now has a re-run path but uses it only for scheduling. |
| **AUDIT-21 #1 / AUDIT-23 #1** (medium): approving an escalated design re-checks cost on the design's own figures, not the estimate | **Still open**, unchanged since round 5. No `orchestrator/` or `agents/` code changed. Both `test_audit_21.py::test_approving_a_figure_request_still_pauses_on_the_larger_figure` cases and `test_audit_23.py::test_approving_a_disagreement_still_pauses_on_the_estimated_cost` fail under `--runxfail`. |
| AUDIT-31 #2 (low): `Settings.solver_timeout_s` unbounded | Still open. The repro fails under `--runxfail`. |
| AUDIT-31 #3 (low): `MAX_PROBE_CHARS` against its docstring and the prompt | Still open. The repro fails. |
| AUDIT-31 #4 (low): stdlib patch; a `Workers` target in `__main__` | Still open. The repro fails (`REPLY None 'failed to start'`). It is widened by #3 above. |
| AUDIT-31 #5 (low): `Verifier()` starts processes; AUDIT-27 #3 race | Still open. `build_graph`'s docstring (`graph.py:70-80`) and README still do not mention it. `test_audit_27.py::test_no_worker_is_left_running_after_shutting_down_the_default_executor` still fails under `--runxfail`. |
| AUDIT-31 #6 (low): records and API docs | Still open. These remain: PLAN SG0 `StepReason` list without `worker_died` (`PLAN.md:63`); dead `or "timeout"` (`vericot.py:250`); dashboard step table without `reason`; `bench_verifier._UNSOLVED` without `worker_died` (`scripts/bench_verifier.py:25`); no Result for SG28 to SG34. |

## What held up

- **Lending order against the docs.** `_lent_rank` gives an aged caller `(0, 0)`, a short caller `(1, 0)` and a newcomer `(2, -arrival)`. That matches "short callers first, then newcomers latest first" and `_LENT_AGING × workers` aging in the module docstring, the `_Slots` docstring, the constant's docstring and the ADR. `lent_past` counts only lent grants to later callers who could have taken the place (short or new), as "aged by lent places only" says. Slot grants still use `_rank` in arrival order, so the "Slots still go in arrival order (AUDIT-32 #4)" claim holds. A stopped newcomer is queued again with `short = new = False`: never lent again, and marked hard only if it ran `_HARD_SLICES`, as the ADR says.
- **Stopped checks are never results.** A stopped check returns `_INTERRUPTED` and is dropped. Only on executor shutdown is it returned as UNGROUNDED/`timeout` "interrupted", the same as any other check interrupted by shutdown. No stopped check can come back VALID.
- **Fork adoption in the child.** With `os._exit`, the child verifies with its own workers and forkserver, and the parent's workers stay alive and keep checking. `test_audit_29.py`, `test_audit_30.py` (non-perf) and `test_solver_workers.py` gave 58 passed and 2 xfailed (AUDIT-29 #2). The hooks also run harmlessly in the forkserver's own children, since the preload imports both modules: no executors or pools there, and no client fd to close.
- **SDK.** No change since round 6. `call_structured` still goes through `messages.parse(output_format=...)`, and no `json.loads` or regex was added on LLM output.

## How this was measured

- **Reading.** I read CLAUDE.md, PLAN.md (SG0, SG34, round-8 section), AUDIT-31.md, ADR-0009 and `git show 10ae977`. I grepped `register_at_fork`, `_adopt`, `_children`, `daemon`, `os._exit`, `.run(` and `Resolution` across `src/`, `tests/`, `scripts/` and the AUDIT files.
- **#1.**
  - A guarded script run in a subprocess: `sys.exit(0)` in the child compared with `os._exit(0)`, with idle-only and in-flight variants.
  - Liveness via `Worker.alive()`, and the in-flight reason from `slow_check`.
  - CPython 3.11 `multiprocessing/util.py::_exit_function` read to confirm the mechanism (the traceback names line 360).
- **#2.** `SolverExecutor(1, 0)` with a hog run to a timeout. The newcomer's `fn` is counted. The stop is detected by polling `_Slots.stuck` and `_waiters`, not by sleeping.
- **Re-checks.** `uv run pytest tests/audit/test_audit_{21,23,27,31}.py --runxfail -p no:randomly`: 15 failed, 5 passed. Every open repro named above failed on its asserted line.
- **Stability.** `uv run pytest tests/audit/test_audit_35.py` 3 times gave 2 xfailed each time. With `--runxfail -p no:randomly` 3 times, it gave 2 failed each time.

## Gate

- `make lint typecheck`: ruff clean (159 files formatted), `mypy --strict` clean (145 source files).
- `uv run pytest tests/audit/test_audit_35.py -q`: 2 xfailed.
- `uv run pytest -m "not perf" -q -p no:randomly`: 1380 passed, 13 skipped, 57 deselected, 82 xfailed (including the 2 here). `make test` (with `perf`) was not run.

## Bottom line

SG34's scheduling change is consistent with its documentation:
- lending order;
- aging per kind of place;
- the stop-and-requeue of a newcomer on a lent place;
- unchanged slot order.

Fork adoption works for children that leave with `os._exit`, which is every child the existing tests use. It does not hold for a child that exits normally. Multiprocessing's own `atexit` hook in that child SIGTERMs every daemonic worker the owner started. The owner's in-flight checks then come back `worker_died`, and a non-final one passes the trace, because AUDIT-31 #1 is still open. So the amendment's "never using or killing the parent's" is false for the prefork case it was written for.

The rest are API edges:
- `SolverExecutor.run` may call `fn` twice, and its public docstring does not say so.
- An ADR bullet is now stale.
- Two more process-wide hooks rewrite private stdlib state.
- SG34 has no Result paragraph, and the flipped audits have no Resolution lines.

The two open mediums are unchanged: AUDIT-31 #1 (an unchecked step passes), and AUDIT-21 #1 / AUDIT-23 #1 (an approved estimate disagreement skips the cost pause).
