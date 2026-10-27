# AUDIT-29: Encoding soundness, round 8 (false negatives)

Auditor: an independent agent. It wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg32-newcomers` at `f09ea60` (SG31–SG33 on the round-7 base `7123eed`).
Scope: what changed since round 7:
- SG31: worker requests and replies tagged with sequence numbers, `Worker.fault`, the `worker_died` reason, the `TIMEOUT_MAX_S` clamp (`solver.py`, `solver_workers.py`).
- SG33: the `MAX_NESTING` cap and the `MAX_PROBE_CHARS` probe budget in `Vocabulary.merged` (`formalizer.py`).
- SG32: workers prepared without the parent's `__main__` (the wrapper on `multiprocessing.spawn.get_preparation_data`), newcomer ranking, and workers started when a `Verifier` is built.

`agents/*`, `orchestrator/*` and `tools/*` have not changed since `492980e` (round 6), so the AUDIT-21 and AUDIT-25 probes of the Design cost/duration path still apply.

Every finding has a repro in `tests/audit/test_audit_29.py`. Each repro asserts the correct behaviour and is marked `xfail(strict=True, reason="AUDIT-29 #n")`. With `--runxfail`, each one fails on its stated assertion in 5 of 5 runs. Two plain control tests pass. Nothing in the file calls a real LLM.

"Admitted" means the same as in AUDIT-21:
- `VerificationMonitor().decide(verdict)` returns PASS; or
- a Design is merged without the cost/duration pause that principle 7b requires.

Summary: **0 critical, 0 high, 1 medium, 1 low.**

**Soundness held.** I found no way to get a wrong trace admitted, and no way to get an understated design past the triggers on the agent's own path. Specifically:
- No stale, out-of-turn, killed or never-started worker reply gave VALID.
- A reply cannot skip the builtin probe. The new budget and nesting checks can only refuse a reply.
- The way `Vocabulary.merged` now orders its work (measure every line, then probe) still probes every line it adds.

Both findings fail closed. Each is a regression on an honest path: SG32 breaks verification in any process forked after the graph is built, and SG33 refuses honest replies that repeat the vocabulary.

---

## Medium

### #1 A process forked after `build_graph()` cannot verify anything: each check raises an exception the agents do not catch

**Impact.** SG32 calls `default_executor()` in `Verifier.__init__`. That starts the shared `Workers` pool and multiprocessing's process-wide forkserver as soon as a `Verifier` is built, and `build_graph()` builds one. Before SG32, both started on the first check.

A common pattern is to build the graph once and then fork:
- a `multiprocessing.Pool` that runs several research questions in parallel, where "fork" is the default start method on Linux for CPython 3.11; or
- a pre-forking server (`gunicorn --preload` in front of the FastAPI gateway).

Each forked child inherits the parent's `Workers` object:
- **The first checks raise `AssertionError`.** `Workers.acquire` calls `worker.alive()` on an idle worker. That worker is the parent's child, not this process's, so `Process.is_alive` asserts "can only test a child process". Each check pops one idle worker (4 by default) and raises.
- **Every later check raises `ChildProcessError`.** Once the idle list is empty, `_start` asks the inherited forkserver for a new worker. `forkserver.ensure_running` calls `waitpid` on a forkserver that is not this process's child.

Neither exception is a `ValueError`, a `RuntimeError` or an `anthropic.APIError`. So `escalate_permanent_errors` (`orchestrator/recovery.py:151`) does not turn it into an error HITL request. The agent node raises and the run crashes. Nothing is admitted, but no run in the child can finish. The parent keeps working.

**Repro.** The fixture runs a script with `sys.executable`. The script builds a `Verifier` (as `build_graph()` does), waits until the prestart has filled the idle list, and forks one child with the "fork" context. The child then verifies a two-step trace: `x > 5` (premise) ⊨ `x > 0` (conclusion). The parent verifies the same trace after the child exits.

| Source | Child | Parent |
|---|---|---|
| `f09ea60` (head) | `AssertionError: can only test a child process` | `VALID VALID` |
| `41348b0` (SG31+SG33, before SG32), via `PYTHONPATH` | `VALID VALID` | `VALID VALID` |

A direct `Z3Solver.check_entailment` loop in each of two forked children gave 4 × `AssertionError`, then `ChildProcessError` on every remaining check (46 of 50), in 3 of 3 runs. Tests:
- `test_a_child_forked_after_build_can_verify` (xfail).
- Control: `test_the_parent_verifies_after_forking`.

**Soundness note.** The `is_alive` assertion is also why this fails closed. A forked child cannot reach an inherited worker's pipe, so two processes never share one pipe. If they could, SG31's sequence numbers would not tell their replies apart: both processes start from the same `_seq` and would send the same number. Any fix that makes the pool usable after a fork, such as a `try` around `alive()`, must not reuse inherited workers.

**Root cause.**
- `src/autolab/verification/vericot.py:103-104` starts the pool when the `Verifier` is built.
- `src/autolab/verification/solver_workers.py:203-211` (`acquire`) and `175-185` (`_context`) assume that they run in the process that started the workers and the forkserver.
- `src/autolab/verification/solver.py:844-850` keeps a process-wide executor that a child inherits.

**Suggested fix.** Record `os.getpid()` in `Workers` (and in `default_executor`'s singleton). In a process with a different pid, drop the inherited idle list and thread pool without touching them, and start fresh. `os.register_at_fork(after_in_child=...)` is the natural hook for this. The forkserver is stdlib state: create a new `multiprocessing.get_context("forkserver")` object, or reset `multiprocessing.forkserver._forkserver` in the child. Alternatively, make `escalate_permanent_errors` treat `AssertionError`/`OSError` from the solver as a permanent error. That only turns the crash into an escalation.

---

## Low

### #2 The probe budget charges for lines that repeat the vocabulary, so a long trace's honest steps are refused

**Impact.** `Vocabulary.merged` adds `_probe_chars` for every line of a reply. That includes a line identical to one already in the vocabulary (`existing == stripped`). Such a line needs no new probe. It was probed when it was first merged, and both `_is_z3_builtin` and `_applied_builtin` are `lru_cache`d on it. `Formalizer` accepts re-declaring it, and models often do, even though the prompt says not to.

Once the vocabulary is large, a reply that repeats it goes over `MAX_PROBE_CHARS` and is refused. The step becomes UNGROUNDED/`formalization_failed`. On a payload-bound conclusion, the trace is blocked, retried and escalated, although every symbol in it is legal. The error also misleads the retry: "declare fewer or narrower symbols", but the step added only a few.

**Repro.**

| Reply | `f09ea60` | `7123eed` (before SG33) |
|---|---|---|
| step 1 repeats step 0's `MAX_ARITY` Int function, plus 30 new Int constants (`test_repeating_a_vocabulary_line_costs_no_probe_budget`, through `Formalizer.formalize`) | refused at `c21`: "need over 163840 characters of builtin probes" | accepted |
| each step repeats the whole vocabulary plus one new constant (`test_a_reply_that_repeats_a_growing_vocabulary_is_accepted`) | refused at step 133 (133 constants) | 200 steps accepted, last merge 3.7 ms |
| the same 30 new constants without the repeated line (control `test_the_new_constants_alone_fit_the_probe_budget`) | accepted | accepted |

**Root cause.** `src/autolab/verification/formalizer.py:100`: `cost += _probe_chars(...)` runs before the line is compared with `self.declarations`.

**Suggested fix.** Charge nothing, and do not probe again, when `self.declarations.get(name) == stripped`. That line was already accepted under the same rules. Because the line is identical, its `sorts` tuple holds the same declarations as before. Lines repeated within one reply can be skipped the same way.

---

## Probed and held up

**SG31, matched replies.**
- `Worker.call` returns a reply only when `case (self._seq, (bool(), _))` matches. `self._seq` is a value pattern, compared with `==`, so a stale reply (lower sequence number) is never returned. Anything else sets `OUT_OF_TURN` and returns None. That gives UNGROUNDED/`worker_died`, and the worker is discarded.
- `ready()` takes the first message as the boot message. No request is sent before `ready()` returns True, so that message cannot be a reply.
- After a None reply, a requested interrupt, or any `BaseException` (now including one raised in `ready()`), the worker is discarded, never released. A released worker always has an empty pipe.
- Stress: `scratchpad/stress.py` (not committed) ran 60 rounds of 24 concurrent checks through `default_executor()`. Each check was valid, not-entailed, inconsistent-premises or Fermat n=3, with a Z3 timeout of 0.2–0.3 s, and 30% of checks were cancelled at a random point in 0–50 ms. Results: 1,060 completed and 380 cancelled. Every valid case was VALID, and no not-entailed or inconsistent case was VALID.
- `worker_died` is an UNGROUNDED reason:
  - `blocking_reason` blocks it as a final step.
  - `_payload_overrides` turns an inconclusive tautology or circularity check into UNGROUNDED, either way.
  - `contradiction_trigger` fails closed on any non-VALID step.
- No downstream code branches on the reason `timeout`, so the new reason takes no more lenient path.
- **`TIMEOUT_MAX_S`.** `Settings.solver_timeout_s` is `gt=0`, which also refuses NaN. `inf` clamps to 1e6 s. With its grace that is 1.1e9 ms, inside 2^31. A clamped check is decided the same way as before; the clamp only shortens the wait.

**SG33, the probe cannot be skipped.**
- Every line that reaches `out` is appended to `probes` first. The probes run before `Vocabulary(out)` is returned, and a refused probe raises, so nothing is merged.
- The budget and `_too_deep` checks can only raise.
- The `sorts` tuple is built from `out` exactly as before SG33: earlier vocabulary, then earlier lines of the same reply. A function line that names a sort declared later in the same reply is still refused, by the parse check (unknown sort, in dict order). A line identical to a vocabulary entry keeps that entry's position in the dict.
- `str.splitlines` is used both here and in `_build_smt2`, so the lines the probe sees are the lines the solver gets.
- Timing: a reply near the budget probes in 23–27 ms (one 32-arity Int function, or 125 constants). A declaration nested past the cap is refused before any parse.

**SG32, the worker without `__main__`.**
- `_solve` and `SolverOutcome` are pickled by reference to `autolab.verification.solver`, which the forkserver preloads. Dropping `init_main_from_*` changes no code that a check runs.
- The `sys_path`, `dir` and authkey preparation keys still reach the worker.
- The wrapper acts only on processes named `z3-worker`.

**SG32, newcomer ranking.** It changes only the order in which checks get a place. A check's Z3 deadline starts in the worker, so ranking cannot turn a check VALID.

**Assertions.** Assertions have no nesting cap. A 100,000-deep `(not ...)` passes `_is_single_term` in 0.11 s and Z3's parse check in 0.05 s in the parent. In a worker, a crash would give `worker_died`.

## Re-check of earlier soundness fixes

At `f09ea60` I ran `uv run pytest tests/audit/test_audit_{1,5,9,12,13,17,21,25,28}.py -q -p no:randomly`. It gave 125 passed and 24 xfailed. None of the xfails is a soundness fix that has regressed. AUDIT-21 #3 (an inconclusive premise check leaves the pool) is still open, and `worker_died` is one more way to reach it, with the same effect.

## How this was measured

- **Machine.** A 4-CPU container shared with other agents (load average about 3), CPython 3.11.17, Z3 4.16.0.
- **Repeat runs.** `uv run pytest tests/audit/test_audit_29.py -q` gave 2 passed / 3 xfailed in 5 of 5 runs. With `--runxfail` it gave 2 passed / 3 failed, each on its stated assertion, in 5 of 5 runs.
- **Before-change comparisons.**
  - #1: `git archive 41348b0 src` was put first on `PYTHONPATH` (the fixture's subprocess inherits it). There, `test_a_child_forked_after_build_can_verify` passes under `--runxfail`.
  - #2: `git archive 7123eed src` was put first on `PYTHONPATH`. There, both #2 repros pass under `--runxfail`.
- **Timing.** No repro asserts a time. #1's script waits, for at most 30 s, until the prestart has filled the idle list, so it never forks while the prestart thread holds the pool's lock. Before SG32 nothing is prestarted, so the script does not wait.
- **Gates.** `make lint typecheck` passes.

## Bottom line

SG31–SG33 did not open a false-negative path. I found no stale, crossed or lost worker reply that comes back VALID, and no reply that skips the builtin probe. The two findings are regressions on honest paths, and both fail closed:
- SG32's start-up at `Verifier` build makes every check raise, uncaught, in any process forked after `build_graph()` (#1, medium).
- SG33's budget refuses replies that repeat the vocabulary (#2, low).

The open soundness items are still AUDIT-21 #1 (medium) and #3 (low).
