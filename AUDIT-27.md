# AUDIT-27: API and SDK consistency, round 7

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg30-fair-slots` @ `7123eed` (SG28, SG29 and SG30 merged). Branch `claude/audit-27`.
- **Scope:** what changed since round 6 (`git diff bc0b2a4..7123eed`). Only `verification/formalizer.py`, `verification/solver.py`, the new `verification/solver_workers.py`, `verification/_forkserver_init.py`, ADR-0009, PLAN.md, scripts and tests changed. No agent, orchestrator, CLI, state or LLM-client code changed, so the round-6 SDK findings stand as they were (see "Earlier findings re-checked").
- **Repros:** `tests/audit/test_audit_27.py`, 4 strict-xfail tests for findings #1 to #3. Each asserts the correct behaviour. Under `--runxfail` all 4 fail on their stated assertion, 5 of 5 runs. No timing assertions, no network, no LLM. #4 and #5 are docs-only and cite `file:line`.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | Medium | A worker that dies or never starts is reported as `timeout`, "worker killed past the timeout". A script without a `__main__` guard gets this on every check, with no error | 2 tests |
| 2 | Low | A `solver_timeout_s` above about 22.6 days raises `OverflowError` from every check on the worker path, and it is not escalated; in-process it is decided | test |
| 3 | Low | `shutdown_default_executor()` races the constructor's pre-start thread: worker processes are started after it returns and stay idle | test |
| 4 | Low | SG29/SG28 API docs: `check_entailment` contradicts itself on errors, `SolverWorkerError` is not documented where it lands, "workers" means two things, missing `__all__` | citations |
| 5 | Low | Records: SG28 to SG30 have no Result paragraph, AUDIT-22/24 no Resolution lines, ADR-0007 not marked as amended, the main-module rule is not in any user-facing doc | citations |

**Count:** 0 critical, 0 high, 1 medium, 4 low. One medium is carried over from earlier rounds: AUDIT-21 #1 / AUDIT-23 #1 is still open.

No finding met the High bar. Every SG29 failure mode found here fails closed: a missing reply is never VALID, as ADR-0009 promises. #1 is ranked Medium because the reason and details the audit trail records are wrong, and the failure is silent (CLAUDE.md §8, "No silent failures"; principle 8).

---

## 1. Medium: a dead or unstarted worker is reported as "killed past the timeout"

**Impact.**
- `check_entailment` maps every `None` reply to one of two outcomes (`verification/solver.py:254-255`): `_INTERRUPTED` if a cancel was requested, else `_KILLED`, which is `UNGROUNDED`/`timeout`, details `"worker killed past the timeout"` (`:281-283`).
- `Worker.call` returns `None` in three cases (`verification/solver_workers.py:95-109`): the budget ran out, the worker died (`EOFError`), or it never became ready (`ready()` is False, `:83-93`; `check_entailment` also skips the call when `booted` is False, `solver.py:233-237`). ADR-0009 names only the first two causes of a kill (timeout and cancel, `docs/decisions/0009-…md:17-20`).
- So a worker that crashes (a Z3 abort or segfault, the OOM killer, a `kill`) or fails to start is recorded in `StepVerdict.details` (copied as is by `vericot.py:216-222`) as a timeout kill. Its `solver_ms` can be a few ms against a 5 s timeout. A reviewer reading the trace is told the check timed out when it did not run.
- The main-module rule makes this easy to hit. ADR-0009 (`:44`) and `solver_workers.py:9-11` say the main module must be import-safe. Without the guard, each worker re-runs the script, the script's own `check_entailment` tries to start a process inside the worker, and the worker dies. The caller is not told anything: every check comes back `UNGROUNDED`/`timeout`, "worker killed past the timeout", in about 0.3 s with a 5 s timeout. A trace scores at most 0.5 and is retried or escalated for no reason. The only sign is a child traceback on stderr. The vericot quickstart (`vericot.py:3-6`) and README do not mention the rule (#5).
- Measured with a 4-line script and no guard: `SolverOutcome(verdict='UNGROUNDED', reason='timeout', details='worker killed past the timeout')` for a claim `p` from premise `p`, 0.31 s. The same code under `python -c` returns VALID.

**Repro.**
- `test_a_worker_that_dies_is_not_reported_as_killed_past_the_timeout`: the worker is killed as the request is sent (as in `test_solver_executor.py::test_a_check_whose_worker_dies_is_never_valid`, which checks only verdict and reason). Timeout 30 s. Observed `details == "worker killed past the timeout"`.
- `test_a_script_without_a_main_guard_is_not_told_its_checks_timed_out`: runs an unguarded script in a subprocess. Observed: exit 0, stdout `OUTCOME UNGROUNDED timeout worker killed past the timeout`.

**Fix.** Tell the causes apart. `Worker.call` can return a reason with `None` (budget spent, died with exit code or signal, never ready), and `check_entailment` can use its own details for each, for example "worker died (signal 9)" or "worker failed to start". A worker that never starts more than once in a row is a configuration error: raise `SolverWorkerError` naming the main-module rule instead of returning UNGROUNDED. Then `escalate_permanent_errors` turns it into an `error` request.

## 2. Low: a long `solver_timeout_s` raises `OverflowError` on the worker path

**Impact.**
- `Settings.solver_timeout_s` is `Field(gt=0.0)` with no upper bound (`config.py:49`). It reaches `Z3Solver.timeout_seconds` through `build_graph` (`orchestrator/graph.py:86`).
- The caller waits with `conn.poll(timeout + grace)` (`solver.py:236-237`, `solver_workers.py:104`). `poll` takes a C `int` of milliseconds. Past 2^31 ms of budget, that is a timeout above about 1.95e6 s (22.6 days, with the 10% grace), it raises `OverflowError: timeout is too large`. That happens on every check.
- In-process, `_check_here` decides the same check (VALID). Before SG29 so did `check_entailment`. Z3 accepted the large `timeout` parameter.
- `OverflowError` is an `ArithmeticError`. `escalate_permanent_errors` catches `ValueError`, `RuntimeError` and `anthropic.APIError` (`orchestrator/recovery.py:151`), so the error ends the run instead of pausing it. Measured: `Verifier(fake, solver_timeout_seconds=2.25e6).verify(trace)` raises `OverflowError`. `Settings(solver_timeout_s=2.25e6)` is accepted.
- A timeout of weeks is unusual, but an operator who wants "effectively no limit" may set one. `float("inf")` already failed in-process before SG29 (`int(inf)` in `_remaining_ms`).

**Repro.** `test_a_long_timeout_is_decided_in_a_worker_as_it_is_here`: `Z3Solver(2.25e6)._check_here(...)` is VALID. `check_entailment(...)` raises `OverflowError: timeout is too large` (from `selectors.py:415`).

**Fix.** Bound `solver_timeout_s` in `Settings` (`le=` a `Final` in `config.py`), or clamp the wait in `Worker.call` to a maximum poll and loop.

## 3. Low: `shutdown_default_executor()` races the pre-start thread

**Impact.**
- ADR-0009 says "`shutdown_default_executor()` stops them [idle workers] as well" (`0009-…md:31`). Its own test checks `_workers().live() == 0` straight after (`test_solver_workers.py:248-257`).
- `SolverExecutor.__init__` starts a daemon thread running `_workers().prestart(workers + slow_workers)` (`solver.py:710-713`). `shutdown_default_executor` shuts down the executor's pool, which does not know about that thread, and calls `Workers.close()` (`solver.py:830-837`). `close` stops the workers that are idle at that moment (`solver_workers.py:206-213`). `prestart` keeps going and adds the rest to the idle list (`:158-166`).
- So after `default_executor()` and then `shutdown_default_executor()` close together, for example a CLI run that ends before its first check, worker processes are left running until the atexit hook. `close` is not terminal either: it does not stop a later `prestart`, `release` or `discard` (whose replacement is started, `:196-199`) from refilling the list.
- Also, `shutdown_default_executor()` calls `_workers()`, which creates and registers the shared `Workers` if it does not exist yet (`solver.py:428-438`, `:837`).

**Repro.** `test_no_worker_is_left_running_after_shutting_down_the_default_executor`: process start-up is made slow for the pre-start thread only (it waits on an event). `default_executor()` and then `shutdown_default_executor()` are called, the event is set, and the thread is joined. Observed: `4 worker processes started after shutdown_default_executor()` (4 CPUs: `prestart(2 + 2)`).

**Fix.** Give `Workers` a closed flag that `prestart`, `release` and `discard` respect (stop instead of adding to the idle list), and have `shutdown_default_executor` join the pre-start thread or set the flag before `close`. Reset the flag when `default_executor()` creates a new executor.

## 4. Low: SG28 and SG29 API documentation

- **`check_entailment` contradicts itself on errors.** The conventions say "Any solver error / unknown / timeout / killed worker → UNGROUNDED". Immediately below, the docstring says it raises `SolverWorkerError` when "the check raised in its worker" (`solver.py:216-219`).
- **`SolverWorkerError` is not documented where it lands.**
  - It is a `RuntimeError` (`solver.py:194-195`). One raising check fails the whole `Verifier.verify`, whose docstring lists no raises (`vericot.py:109-113`). The module docstring says a failing step "yields an UNGROUNDED verdict — the trace remains verifiable as a whole" (`vericot.py:32-34`). That is written about formalization, but it is the only failure contract stated there.
  - `escalate_permanent_errors` then turns it into an `error` request. Its docstring lists `RuntimeError` as "a refusal or empty structured output, see `StructuredOutputError`" (`recovery.py:136-141`), not a solver error.
  - Before SG29 a Z3 error during a check raised `z3.Z3Exception`, which ended the run. The new behaviour is better, but it is recorded nowhere: not in ADR-0009 or in either docstring.
  - The worker reduces the exception to `"Type: message"` text (`solver_workers.py:54-57`). The type and traceback are lost. `_check_here`, which the ADR offers as the in-process equivalent (`0009-…md:45`), raises the original exception instead. The worker's `except Exception` is the broad catch CLAUDE.md §9 rules out. It has a comment, but no narrower type.
- **"Workers" means two things.**
  - `SolverExecutor(workers, slow_workers)`, `SOLVER_WORKERS`, `SOLVER_SLOW_WORKERS` and `_Pool`'s "worker threads" count slots and threads (`solver.py:134-139`, `:521`, `:696-699`).
  - `solver_workers.Worker`/`Workers`, `_IDLE_WORKERS` and "a worker process" count processes (`:159-160`, `:209`). The thread-local `_worker` (`:496`) is pool-thread state. `SolverExecutor.shutdown` "join[s] the worker threads" (`:777`) and stops no worker process.
  - ADR-0009 mixes the two in one line: "Up to `2 × SOLVER_WORKERS + SOLVER_SLOW_WORKERS` workers stay idle" (`:28`).
- **CPU budget.** `SOLVER_SLOW_WORKERS`' docstring says "slots and slow checks together do not outnumber the CPUs (AUDIT-18 #5)" (`solver.py:137-139`). A stuck slot also lends a place (SG30, `:644-650`), so up to `2 × workers + slow_workers` CPU-bound checks run at once. `_IDLE_WORKERS` is sized for exactly that (`:159-160`). Both of these are true, but the first reads as a bound the scheduler does not keep.
- **Exports.**
  - `solver_workers.py` is the only module in `src/` apart from `cli.py` and `config.py` with no `__all__`.
  - `formalizer.MAX_ARITY` is named in the model-facing prompt (`formalizer.py:190`) and in error messages, but not in `__all__` (`:434`). It is `Final` without a type (`:268`), unlike the other `Final[int]`s.
  - `Vocabulary.merge` and `Formalizer.formalize` list their `FormalizationError` causes without the new arity cap (`formalizer.py:95-101`, `:112-122`). `merged` does list it (`:57-61`).
- **ADR-0009 consistency held elsewhere.** These match the code: kill at `timeout + max(10 ms, 10%)`, the budget starting once the worker is ready, the replacement started before the place is returned, daemonic workers with `PR_SET_PDEATHSIG`, the atexit hook, Real-only loading above `_LOAD_ABOVE`, and cgroup v1/v2 sizing. The IPC cost claim also held: trivial check median 4.3 to 4.7 ms in a worker against 4.4 to 4.6 ms in-process (100 runs each, 4 CPUs).

## 5. Low: records and user-facing docs

- PLAN.md marks SG28, SG29 and SG30 "done (PR pending)" (`PLAN.md:46-48`). None has the **Result** paragraph (files changed, tests added, verify output, open issues) that the Rules require (`PLAN.md:8`). This is the same gap AUDIT-23 #6 found for SG24 to SG27, which is also still open.
- AUDIT-22.md and AUDIT-24.md have no **Resolution** lines for the findings SG28 to SG30 flipped (AUDIT-22 #1 to #6, AUDIT-24 #1 to #6). The flips themselves match the plan: only AUDIT-22 #7 and AUDIT-24 L1 to L8 still xfail.
- ADR-0009 says "Amends: ADR-0007" (`:5`). ADR-0007 has no amendment pointing back, and its premise-consistency paragraph still describes the in-process, add-then-check flow (`docs/decisions/0007-…md:27`).
- The main-module requirement (#1) is stated only in ADR-0009 (`:44`) and in `solver_workers`' module docstring. It is not in the vericot entry-point docstring (`vericot.py:3-6`), the `Verifier`/`SolverExecutor`/`Z3Solver` docstrings or README.md, which a library user reads. Every script in `scripts/` that verifies has the guard. `scripts/dashboard.py` has none but runs no check.

---

## Earlier findings re-checked

| Item | Holds? |
|---|---|
| **AUDIT-21 #1 / AUDIT-23 #1** (medium): approving an escalated design re-checks cost on the design's own figures, not the estimate | **Still open.** `orchestrator/checkpoints.py:149-157` runs `policy(merged, state)` on the merged design. `agents/design.py:96-102` withholds a disagreement without running the trigger on the raised figures. Under `--runxfail`, `test_audit_23.py::test_approving_a_disagreement_still_pauses_on_the_estimated_cost` and both `test_audit_21.py::test_approving_a_figure_request_still_pauses_on_the_larger_figure` cases still fail. No file on that path changed since round 6. |
| AUDIT-23 #2 to #5 | Unchanged, because no agent, orchestrator or LLM code changed. All 5 `test_audit_23.py` repros still fail under `--runxfail`. |
| AUDIT-23 #6 (stale records) | Partly resolved. ADR-0009 now records SG26's scheduling (as unchanged) and the SG27 loading kept for Real pools. The `_REARM_S` and `_Interrupt` docstrings were updated, and z3 spies now call `_check_here` (`test_audit_14.py:42-61`, `test_audit_20.py:282`, `:303`, `:327`, `:369`). All other items in #6 are still open. |
| AUDIT-11 (placeholder `__init__` docstrings), README status line, `docs/status.md`, `.env.example`, `anthropic>=0.40` | Unchanged. For example, `verification/__init__.py:1` still reads "Placeholder … implemented in a later phase". |

## What held up

- **SG28.** `MAX_ARITY` (32) is enforced in `Vocabulary.merged` before the probe runs (`formalizer.py:75-79`). The cap is stated in the formalize prompt (`:190`), so the formalizer's retry gets an actionable reason. `_arity` counts nested sorts as one argument (`((Array Int Int) Int)` gives 2), and leading-zero or huge numerals give `MAX_ARITY + 1` (`0000000000000000033` gives 33). Checked by running it: 33 arguments are refused with `declaration of 'f' takes 33 arguments; at most 32 allowed`, and 32 are accepted.
- **SG29 contracts.**
  - `SolverExecutor` and `Z3Solver.check_entailment` keep their signatures, as PLAN.md requires. Every exported name in `solver.__all__` exists and is used.
  - A missing reply is never VALID.
  - Worker and in-process outcomes agree on every golden trace (pinned by `test_solver_workers.py`).
  - Start-up time is excluded from `solver_ms` and from the hardness measure (`solver.py:248-253`, `:809-810`).
- **SG30.** `_Slots` matches its docstring. A waiter passed over `workers` times ranks first (`:664-665`), only a slot lends (`stick` refuses a lent place, `:644-650`), and hardness is decided by run time minus overhead (`:756`).
- **SDK.** No change since round 6. `call_structured` still goes through `messages.parse(output_format=...)`, and no `json.loads` or regex was added on LLM output.

## How this was measured

- Read CLAUDE.md, PLAN.md, AUDIT-23.md, ADR-0007 to ADR-0009 and `git diff bc0b2a4..7123eed` for `src/`. Checked every claim in ADR-0009 and in the new docstrings against the code.
- #1: an unguarded 4-line script run with `.venv/bin/python` (stderr shows the child's `RuntimeError: An attempt has been made to start a new process before the current process has finished its bootstrapping phase`). The same lines under `python -c` return VALID.
- #2: `Z3Solver(t).check_entailment` against `_check_here` for `t` = 4e6 and 5e6, then the 2.25e6 repro, then `Verifier(...).verify`.
- #3: the gated pre-start repro. Process start-up is slowed for the background thread only, with an `Event`. No sleeps.
- IPC cost: 100 trivial checks each way, median, twice, in one process.
- Repro stability: `uv run pytest tests/audit/test_audit_27.py --runxfail -p no:randomly` 5 times: 4 failed each time, on the asserted lines.

## Gate

- `make lint typecheck`: ruff clean (150 files formatted), `mypy --strict` clean (138 source files).
- `uv run pytest tests/audit/test_audit_27.py -q`: 4 xfailed. With `--runxfail`, all 4 fail on their stated assertion, 5 of 5 runs.
- `uv run pytest -m "not perf" -q`: 1279 passed, 13 skipped, 45 deselected, 54 xfailed (including the 4 here). `make test` (with `perf`) was not run.

## Bottom line

SG28 to SG30 do what PLAN.md asked, and ADR-0009 describes the code accurately on the paths it covers. The interface (`SolverExecutor`, `check_entailment`, `__all__`) is unchanged. The gaps are at the edges of the worker model:
- A crashed or unstartable worker is recorded as a timeout. An unguarded script silently gets UNGROUNDED on every step.
- A very long timeout overflows the pipe wait.
- Shutdown does not stop a pre-start that is still running.
- The new error type is undocumented where it lands.

None of these admits a wrong trace. The open medium from rounds 5 and 6, an approved estimate disagreement that skips the cost pause, is unchanged and still the most consequential consistency issue in the repo.
