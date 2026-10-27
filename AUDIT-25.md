# AUDIT-25: Encoding soundness, round 7 (false negatives)

Auditor: an independent agent. It wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg30-fair-slots` at `7123eed` (SG28–SG30 merged on the round-6 base `492980e`).
Scope: what changed since round 6:
- SG28: the `MAX_ARITY` cap, and `_tokens` now strips trailing whitespace (`formalizer.py`).
- SG29: checks run in forkserver worker processes (`solver.py`, `solver_workers.py`, `_forkserver_init.py`, ADR 0009).
- SG30: fair slot scheduling (`solver.py`).

The agents' own paths (`agents/*`, `orchestrator/*`) were re-checked as well. They have not changed since round 6.

Every finding has a repro in `tests/audit/test_audit_25.py`. Each repro asserts the correct behaviour and is marked `xfail(strict=True, reason="AUDIT-25 #n")`. Under `--runxfail` each one fails on its stated assertion in 5 of 5 runs. There are two plain control tests, and both pass. Nothing in the file calls a real LLM.

"Admitted" means the same as in AUDIT-21:
- `VerificationMonitor().decide(verdict)` returns PASS; or
- a Design is merged without the cost/duration pause that principle 7b requires.

Summary: **0 critical, 0 high, 1 medium, 2 low.**

**Soundness held.** I found no way to get a wrong trace admitted, and no way to get an understated design past the triggers on the agent's own path. Specifically:
- No killed, crashed, stopped, stale or reused worker gave VALID for a step that is not entailed.
- No pickling path or reply mixup did either.
- Neither did the SG28 tokenizer change or SG30's scheduling.

Every worker failure I could cause ends as UNGROUNDED, as an error HITL request, or as an exception. All three findings below sit in the new worker plumbing, and all of them fail closed. Two of them are about what the trace records or what the process does on the side; the third leaks a worker process.

---

## Medium

### #1 A main script without a `__main__` guard re-runs in every solver worker, and every check it makes is UNGROUNDED

**Impact.** SG29 starts its workers from a forkserver, and multiprocessing re-runs the caller's main script in each worker as `__mp_main__`. ADR 0009 notes that the main module must be safe to import, but nothing checks this. Researchers often write a plain script that calls the verifier or runs the graph with no `if __name__ == "__main__":` guard. For such a script:

- **The script body runs again in every worker the pool starts.** That is the 4 prestarted workers here (`workers + slow_workers`), plus one replacement per check. The body runs up to its first solver check, and that check then raises in the worker (`RuntimeError` from multiprocessing's bootstrapping check). Any side effect before that check repeats once per worker. In a real run that means paid LLM calls (the Research agent calls Claude before its first verification), writes to the state store, and calls to tools. CLAUDE.md principle 9 asks for confirmation before calling external services, and these repeats get none.
- **Every check in the parent fails closed for a reason that is false.** The worker dies at boot, so the parent gets UNGROUNDED/`timeout`, "worker killed past the timeout", even for a trivially valid check (`p ⊨ p`). Every payload-bound trace is then blocked, retried and escalated. The human is told that the solver timed out.

Before SG29 (at `492980e`) the same script printed `VALID` and its body ran once.

**Repro.** The test writes a script that logs one line per run of its body, then checks `p ⊨ p` through `default_executor()`. It runs the script with `sys.executable`.

| Script | Body runs | Parent's verdict |
|---|---|---|
| guarded (control `test_a_guarded_script_checks_once_and_gets_valid`) | 1 | `VALID` |
| unguarded, at `7123eed` (`test_an_unguarded_script_is_not_rerun_in_the_workers`) | 6, 7, 6, 7 (4 runs) | `UNGROUNDED \| worker killed past the timeout` |
| unguarded, 10 checks in a loop | 15 (1 + 14 in workers) | 10 × `UNGROUNDED` |
| unguarded, pre-SG29 source (`492980e`, via `PYTHONPATH`) | 1 | `VALID` |

**Root cause.**
- `src/autolab/verification/solver_workers.py:140-156`: `_context` uses the forkserver context, and `Workers._start` starts each worker from it with the caller's main script.
- `src/autolab/verification/solver.py:254-255`: a reply of `None` is always reported as `_KILLED`, "past the timeout".

**Suggested fix.** Use any of:
- Before the first worker starts, check `sys.modules["__main__"]`. If the main module has a `__file__` and is being run as `__main__` with no guard, fall back to running checks in-process, or raise a clear error once.
- Set the workers' main to a stub. That is, do not ask multiprocessing to rebuild `__main__` in the child, since `_solve` lives in `autolab.verification.solver` and needs nothing from the main module.
- At minimum, report a worker that died before it was ready as "worker failed to start" (see #2), not as a timeout.

---

## Low

### #2 A worker that dies mid-check is recorded as "killed past the timeout"

**Impact.** `check_entailment` maps every missing reply to `_KILLED` (UNGROUNDED, reason `timeout`, "worker killed past the timeout"). That covers a worker killed by the OOM killer, a Z3 abort, a stray signal, or a failed boot. In the repro the worker is SIGKILLed 0.5 s into a 20 s timeout. The verdict correctly fails closed, but the trace records a timeout that never happened. Principle 8 makes traces the product, and the dashboard and the human see a timeout. The check is also not retried on a fresh worker, so a crash on an honest step costs that step its VALID.

On a non-final `premise`, the crash has the effect AUDIT-21 #3 describes (the premise leaves the pool). That is not reported again here.

**Repro.** `test_a_crashed_worker_is_not_reported_as_a_timeout`. The test records the busy worker by wrapping `Worker.call` in the parent, then SIGKILLs it while it runs the Fermat n=3 check (timeout 20 s).
- Preconditions: elapsed is less than half the timeout (observed 0.54 s), and the verdict is UNGROUNDED.
- Assertion: `"past the timeout" not in details`. It fails today.

**Root cause.** `src/autolab/verification/solver.py:254-255`, together with `solver_workers.Worker.call`, which returns `None` both for a timed-out poll and for EOF.

**Suggested fix.** Have `Worker.call` tell "budget exhausted" apart from "process gone". Report the second as, for example, UNGROUNDED/`timeout` with "worker died (exit code N)". Optionally retry once on a fresh worker when the death happened well inside the timeout.

### #3 An exception while a new worker boots leaks that worker

**Impact.** `check_entailment` calls `worker.ready()` (`solver.py:233`) after `interrupt.attach(worker)`, but outside the `try` that discards the worker on `BaseException`. If an exception is raised during the wait for a booting worker (up to `_BOOT_S` = 60 s), the worker is neither released nor discarded. Such an exception can be a per-test timeout's signal or Ctrl-C on a direct main-thread call. The worker stays alive, stays counted in `Workers.live()`, and cannot be reached until the interpreter exits. ADR 0009 says "a worker whose call raised in the caller (for example a per-test timeout) is killed". This path breaks that. No verdict is affected.

**Repro.** `test_an_error_while_a_worker_boots_does_not_leak_it`. The test patches `Worker.ready` in the parent to raise a `BaseException`, standing in for the signal. Assertion: the acquired worker is either idle or dead afterwards. It fails: `_idle == []` and `alive() is True`. Control: `test_a_check_returns_its_worker_to_the_pool`, where the worker is idle again after a normal check.

**Suggested fix.** Move `booted = worker.ready()` inside the `try`, so that the existing `except BaseException` discards the worker.

---

## Probed and held up

**SG29, verdict integrity.** For each item: what was probed, and what was found.
- **Reply mixup under cancellation.** `scratchpad/stress.py` (not committed) sent 24 concurrent checks per round through one `SolverExecutor`, with Z3 timeouts of 0.2–0.3 s. The cases were valid, not-entailed, inconsistent premises and Fermat (runs to its timeout). About 30% of the checks were cancelled at a random point in 0–50 ms. Over 100 rounds (two runs, 20 and 80 rounds) there were 1,766 completed checks and 634 cancellations. No case expected INVALID or UNGROUNDED ever returned VALID, and no valid case returned INVALID. The only verdict drift was one VALID case that came back UNGROUNDED ("too little time left") under load. That is a false positive.
- **Kill and request races.** A worker is killed only through an attached `_Interrupt`, and `request()` sets `_requested` under the lock before it kills. After `detach()`, line 244 discards the worker if a request has arrived. So a killed worker is never released to `_idle`, and a released worker always has an empty pipe (one call, one reply, read before release). An idle worker's unread boot message is consumed by `ready()` on its next use.
- **Partial or garbled replies.** A worker killed mid-`send` leaves a short frame, and `recv` raises `EOFError`. That is caught and gives `_KILLED`. Any other unpickling error propagates and discards the worker. It cannot become a `SolverOutcome`.
- **File descriptors.** The forkserver closes the child end of each pipe after it forks, and the parent's end never reaches another worker. No forked copy of this threaded process exists, so no second reader shares a pipe.
- **Errors in the worker.** An error inside `_solve`, for example a `Z3Exception` from `check()`, becomes `(False, text)`, then `SolverWorkerError`, a `RuntimeError`. `escalate_permanent_errors` turns that into an error request, which merges nothing.
- **Leftover state in a reused worker.** `_solve` calls `z3.reset_params()` and builds a fresh `Context` on each call, and there is no Python-level state in it. Assertions cannot carry SMT commands (`_is_single_term`), so no call can set a parameter for the next one.
- **Same verdicts in a worker and in-process.** The arguments are `(document, count, claim is "true", timeout)`, all built in the parent with the same code as before. `true_claim` is still `claim.assertion.strip() == "true"`, and the shortcut still requires `consistency == sat`. Since SG29, `_loaded` loads one formula at a time only for Real pools. Either loading path reaches the same verdict, as AUDIT-21 showed for SG27.
- **Killed or crashed workers.** A worker SIGKILLed mid-check (#2), one killed past its budget (33 times in the stress run), or one that dies at boot (#1) gives UNGROUNDED, never VALID.

**SG28.**
- `_tokens(text.rstrip())` drops only trailing characters that Python counts as whitespace. Some of them, such as `\x1c` and `\xa0`, Z3 may read as part of a symbol. But a trailing run of whitespace can form no token after a closed term that Z3 would accept. Z3 sees either nothing or a stray top-level symbol, which is a parse error and gives `formalization_failed`. `_is_single_term` already used `rstrip`.
- `_arity` counts the same S-expressions as `_applied_builtin`. It refuses arities over 32, and any numeral longer than two digits, before the probe runs. A non-ASCII digit gives arity 0, so the probe is skipped and Z3's parse check refuses the line.
- The cap only refuses declarations. It never lets one through that was refused before.

**SG30.** Slot ranking, aging and lending change only when a check starts. They do not change its Z3 deadline, which starts in the worker once the worker has the job. So scheduling can turn a slow check UNGROUNDED, but it cannot turn any check VALID.

**Agents' paths.** The Design, Research, Execute and Analysis code and `orchestrator/*` are unchanged since `492980e`. The AUDIT-21 probes of SG25 still apply. A worker failure on a check in the contradiction trigger gives "could not rule out a contradiction", which fails closed.

## Re-check of earlier soundness fixes

At `7123eed`, running `uv run pytest tests/audit/test_audit_{1,5,9,12,13,17,21}.py -q` gives 105 passed and 9 xfailed. The 9 are strict xfails that were already open, including AUDIT-21 #1–#3. None of them is a soundness fix that has regressed. AUDIT-21 #3 (an inconclusive premise check leaves the pool) is still open. SG29 adds new ways to reach it (a crashed or killed worker), but its effect is unchanged.

## How this was measured

- **Machine.** A 4-CPU container shared with other agents (load average 4–10 during the runs), CPython 3.11.17 and Z3 4.16.0. The commands were `uv run pytest tests/audit/test_audit_25.py -q`, and the same with `--runxfail`, 5 times each. They gave 2 passed / 3 xfailed, and 2 passed / 3 failed on the stated assertions, in every run.
- **Timing.** No repro asserts an absolute time. #2's only timing precondition compares the elapsed time with the check's own timeout (less than half of 20 s; observed 0.54 s). #1 and #3 do not depend on timing.
- **Pre-SG29 comparison.** `git archive 492980e src` was extracted to the scratchpad and put first on `PYTHONPATH` for the same unguarded script.
- **Gates.** `make lint typecheck` passes. `uv run pytest tests/unit/verification tests/audit/test_audit_21.py tests/audit/test_audit_25.py -q -m "not perf"` gives 542 passed, 8 xfailed.

## Bottom line

SG28–SG30 did not open a false-negative path. In every case I tried, the worker processes fail closed, and no wrong trace or understated design was admitted. The new risk is operational:
- an unguarded main script quietly re-runs, with its side effects, in every worker, and gets UNGROUNDED for everything (#1, medium);
- crashes are recorded as timeouts (#2);
- one exception path leaks a worker (#3).

The open soundness items are still AUDIT-21 #1 (medium) and #3 (low).
