# AUDIT-28: Test coverage and test quality (round 7)

Branch `claude/audit-28`, based on `origin/claude/sg30-fair-slots` (`7123eed`, SG28–SG30 merged). This is an independent audit. No production code or existing test was changed.

**Bottom line:** coverage is still high: 97.2% line and branch overall, 97.0% on `verification/` (98.5% with this audit's tests). The round-6 repros that SG28–SG30 flipped mostly fail against the pre-fix code on their own assertions. The exceptions are the AUDIT-22 #2 pair, which miss the pre-fix code in 2 and 3 of 8 runs, and the AUDIT-14 #1 repros, which can no longer run against pre-SG29 code at all. I ran 67 one-line mutations by hand over the code SG28–SG30 changed:
- The existing suite killed 43. One of those (F09) was killed only by pytest-timeout's 60 s, after 76 s.
- 7 are equivalent.
- 17 survived. One of them (W03) is killed by a `perf` test only.

`tests/audit/test_audit_28.py` adds a plain test that kills each non-equivalent survivor, and a strict xfail that applies the mutant.

Severity counts: **0 critical, 1 high, 6 medium, 9 low.** The main problems:
- **A check cut short in the caller can hand its reply to the next check (#1, high).** The branch that kills such a worker is the only guard. Nothing covers it (`solver.py` 238-241). With `release` in its place, a not-entailed claim comes back VALID. The pipe carries no request id that would catch this.
- **SG29's own tests are weaker than they read (#2, #4, #5).** Nothing pins AUDIT-22 #5's "slow place returns when the job ends", not even its own `perf` repro. A cancel at `acquire` is not covered. The `solver_ms` test does not test its "replacing" half at all, and tests its "start-up" half only when it runs first.
- **Timing tests (#3, #7, #8).** `test_judges_overlap_but_formalization_stays_ordered` fails whenever it runs alone (9 of 9). The AUDIT-22 #2 repros detect the pre-fix code only 6 and 5 times out of 8. AUDIT-10 #4's strict xfail XPASSes in the full coverage run (2 of 2), so the audit's own coverage command is red on the base.

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing --cov-report=json tests` on the base gave 1 failed (AUDIT-10 #4 XPASS, #8), 1320 passed, 13 skipped and 53 xfailed in 5 min 53 s, at a load average of 4–10 from other sessions. With this audit's tests it gave 1 failed (the same), 1334 passed and 69 xfailed. Package figures are (covered lines + covered branches) / (lines + branches), from the JSON report.
- **Worker processes:**
  - pytest-cov does not see code that runs only in a worker (`_serve`, `_die_with_parent`, `_forkserver_init`).
  - I also re-ran the solver tests under `coverage run` with `concurrency = multiprocessing,thread`, `parallel = True` and `sigterm = True` (scratchpad `childcov.py`). It still combined only the parent's data file. Workers end by `SIGKILL` (`Worker.stop`/`interrupt`), so a child never saves its data.
  - So child-only code is handled three ways:
    - Its behaviour is measured by mutants. The mutated copy is first on `PYTHONPATH`, which the forkserver and its workers inherit, so a mutant in `_solve` or `_serve` runs in the workers too.
    - `_solve` is covered in-process through `Z3Solver._check_here`.
    - The new L4 tests run `_serve` in-process on the main thread, which also covers `solver_workers.py` 48-60.
- **Mutations:**
  - A scratchpad harness (`harness.py`, not committed) copies `src/`, `tests/`, `scripts/` and `pyproject.toml` to a fresh directory per mutant. It applies one exact-match replacement, asserted to occur once, and runs pytest there with `PYTHONPATH` set to the copy. Before each run it checks that `autolab` is imported from the copy. The worktree was never edited.
  - Each mutant ran `pytest -x -m "not perf" tests/unit/verification tests/audit`, about 780 tests.
  - Survivors that a `perf` test could kill were re-run against `test_audit_22.py -m perf`: W02, W03, W16 and W17.
  - Two streams ran in parallel on 4 CPUs. The host rebooted midway. Mutants run after the reboot (W17 onwards, A01–A17) also saw a draft of `test_audit_28.py`. The two they reported as killed only by it (W25, A11) were re-run with it ignored, and the ID A07 "kill" is an artifact (see table).
- **Earlier repros:** each flipped round-6 repro was run in a fresh copy with the pre-fix production file swapped in, using `--runxfail`, twice or more:
  - `formalizer.py` and `solver.py` from `492980e`;
  - `solver.py` from `8f3cf14` (SG29 merged, before SG30);
  - `solver.py` from `176d019` (pre-SG22) for AUDIT-14 #1.
  - SG19's assumption check (ASSUME) was also restored on today's `solver.py`, so in the workers too.

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/* | 407 | 1 | 82 | 1 | 99.6% | design 90 (AUDIT-20 L6, carried) |
| cli.py | 79 | 6 | 22 | 0 | 92% | 75, 84-86, 115-117 |
| monitoring/* | 150 | 0 | 38 | 1 | 99.5% | tracer 77->76 |
| orchestrator/* | 288 | 0 | 64 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-73, 116->150, 162 (AUDIT-8 M6, carried) |
| state/ (other 3) | 297 | 0 | 36 | 0 | 100% | |
| tools/* | 122 | 5 | 26 | 4 | 94% | simulator 89; literature 81, 107, 124, 130 |
| verification/formalizer.py | 209 | 0 | 84 | 1 | 99% | 357->365 |
| verification/solver.py | 508 | 7 | 152 | 3 | 98% | **231-232, 238-241** (#1, #4), 486, 835->837 |
| verification/solver_workers.py | 149 | 21 | 28 | 2 | **85%** | 39-43, 48-60 (child only, L4), 88, 102, 227-228 |
| verification/_forkserver_init.py | 2 | 2 | 0 | 0 | **0%** | 7-9 (forkserver only) |
| verification/llm.py | 69 | 1 | 16 | 2 | 96% | 189, 219->218 |
| verification/vericot.py | 128 | 1 | 50 | 1 | 99% | 244 |
| verification/ (other 3) | 71 | 0 | 18 | 0 | 100% | |
| **TOTAL** | 2606 | 55 | 654 | 29 | **97.2%** | |

| Package | Lines | Branches | Cover (line + branch) | With `test_audit_28.py` | Target |
|---|---:|---:|---:|---:|---|
| verification/ | 97.2% | 96.3% | **97.0%** | 98.5% | ≥90% ✔ |
| agents/ | 99.8% | 98.8% | 99.6% | 99.6% | ≥70% ✔ |
| orchestrator/ | 100% | 100% | 100% | 100% | ≥70% ✔ |
| monitoring/ | 100% | 97.4% | 99.5% | 99.5% | ≥70% ✔ |
| state/ | 97.2% | 78.4% | 94.2% | 94.2% | ≥70% ✔ |
| tools/ | 95.9% | 84.6% | 93.9% | 93.9% | ≥70% ✔ |
| cli.py | 92.4% | 90.9% | 92.1% | 92.1% | ≥70% ✔ |

- The uncovered SG29 lines in the parent are the two cancel paths in `check_entailment`: 231-232 (attach refused) and 238-241 (raised while waiting). The second is the stale-reply guard (#1).
- With this audit's tests, `solver.py` is fully covered, apart from 835->837. `solver_workers.py` is left with `_die_with_parent`'s body (prctl, child only), the `OSError` arms of `ready`/`call`, and the `atexit` hook.

## Mutation table

ID prefixes: F `formalizer.py` (SG28), W `solver.py` and `solver_workers.py` worker processes (SG29), A `solver.py` scheduling (SG30). In the last column, "→" names the `test_audit_28.py` test that now kills a survivor.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| F01 | `arity > MAX_ARITY + 1` | killed | test_formalizer::…_over_the_arity_cap_is_refused_before_any_parse |
| F02 | `arity >= MAX_ARITY` | killed | test_formalizer::test_a_step_with_a_function_of_the_largest_arity_formalizes |
| F03 | `MAX_ARITY = 64` | killed | test_formalizer::test_the_probe_parses_a_bounded_amount_of_text_up_to_the_cap[64] |
| F04 | `MAX_ARITY = 128` | killed | …_refused_before_any_parse[(declare-sort Wide 100)] |
| F05 | sort numeral length `<` | killed | …_at_the_arity_cap_is_accepted[(declare-sort Wide 32)] |
| F06 | no leading-zero strip | killed | …_accepted[(declare-sort Wide 032)] |
| F07 | no `isascii` | killed | test_a_non_ascii_sort_arity_fails_formalization_instead_of_escaping |
| F08 | arity = tokens // 2 | killed | …_refused_before_any_parse |
| F09 | `_tokens` without `rstrip` | killed (**pytest-timeout only**, 76 s) | test_tokens_of_text_with_long_trailing_blanks_stay_fast → test_a_blank_tail_costs_no_more_than_a_tail_of_tokens (#6) |
| F10 | declare-fun arity without `tokens[2] == "("` | survived (**equivalent**) | only malformed lines differ, and Z3 refuses them either way |
| F11 | non-numeral sort arity is over the cap | killed | test_a_non_ascii_sort_arity… |
| F12 | probe takes `int(tokens[2])` again | killed | same |
| F13 | long numeral counts as `MAX_ARITY` | killed | …_refused_before_any_parse[(declare-sort Wide 100)] |
| W01 | no kill grace | killed | test_solver_executor::test_a_check_past_its_timeout_and_grace_is_killed |
| **W02** | grace `min` for `max` | **survived** (`perf` too) | → test_the_kill_grace_is_the_documented_one (L2) |
| **W03** | grace share 1.0 | **survived** (killed by AUDIT-22 #3 `perf` only) | → same (L2) |
| **W04** | late cancel: dead worker released | **survived** | → test_a_worker_killed_after_its_reply_is_replaced (L1) |
| W05 | killed always reported "worker killed" | killed | test_solver_executor::test_cancelling_a_caller_kills_its_worker |
| **W06** | raised while waiting: worker released | **survived** | → test_a_check_cut_short_never_answers_the_next_one (#1) |
| **W07** | attach refusal ignored | **survived** | → test_a_cancel_at_acquire_sends_no_request (#4) |
| W08 | no early `requested()` check | survived (**equivalent**) | `attach` refuses the same interrupt |
| **W09** | post-check overhead not excluded | **survived** | → test_solver_ms_excludes_starting_and_replacing_a_killed_worker (#5) |
| **W10** | pre-check overhead not excluded | **survived** | → same (#5) |
| W11 | overhead not reset per call | killed | test_faithfulness (AttributeError) |
| W13 | every long pool loaded | killed | test_solver_loading::test_a_long_pool_without_reals_is_asserted_in_one_call |
| W14 | nothing loaded | killed | test_solver_loading::…_checked_between_formulas_then_twice_as_a_whole |
| **W15** | `_IDLE_WORKERS = 1` | **survived** | → test_the_shared_pool_keeps_a_worker_idle_for_every_place (L3) |
| **W16** | no prestart thread | **survived** (`perf` too) | → test_an_executor_prestarts_its_workers_off_the_callers_thread (L3) |
| **W17** | slow place returned at once on cancel | **survived** (AUDIT-22 #5 `perf` too) | → test_a_cancelled_slow_check_keeps_its_place_until_it_ends (#2) |
| W18 | shutdown leaves workers | killed | test_solver_workers::…_stops_idle_workers |
| W19 | quota floored | killed | test_solver_workers::…[v2-rounded-up] |
| W20 | no default period | survived (**equivalent**) | `cpu.max` always has both fields |
| W21 | `/` cgroup path read | survived (**equivalent**) | re-reads the root `cpu.max` already counted |
| W22 | `"cpu" in controllers` substring | survived (**equivalent**) | `cpuset` dirs have no cfs files |
| W23 | quota ignored | killed | test_solver_workers::test_the_pool_takes_the_smaller_of_affinity_and_quota |
| W24 | largest quota wins | killed | …[smallest-wins] |
| W25 | `booted = True` | survived (**equivalent**) | `call` checks `ready()` itself |
| **W26** | worker does not ignore SIGINT | **survived** | → test_a_worker_ignores_ctrl_c_and_asks_to_die_with_its_parent (L4) |
| **W27** | no `_die_with_parent()` | **survived** | → same (L4) |
| W28 | `stop` does not join | killed | test_solver_workers::…_discard_waits_for_the_process |
| W29 | release ignores `idle_max` | killed | test_solver_workers::test_beyond_idle_max_a_released_worker_is_stopped |
| W30 | no replacement on discard | killed | …_discard_waits_for_the_process |
| W31 | dead idle worker handed out | killed | test_solver_workers::test_a_dead_idle_worker_is_never_handed_out |
| W32 | prestart ignores `idle_max` | killed | test_solver_workers::test_prestart_fills_the_idle_list_up_to_idle_max |
| **W33** | `_main_imports` always empty | **survived** | → test_a_main_module_run_with_dash_m_is_preloaded (L5) |
| W34 | `call` does not wait for ready | killed | test_solver_workers::test_a_worker_answers_calls_and_is_reused |
| W35 | worker error kills the worker | killed | test_solver_workers::test_an_error_in_a_worker_is_its_reply_… |
| **W36** | `close` forgets idle workers without stopping them | **survived** | → test_closing_a_pool_stops_its_idle_processes (L5) |
| **W37** | dead idle worker stays in `live()` | **survived** | → test_a_dead_idle_worker_is_no_longer_counted (L5) |
| W38 | closed executor does not interrupt a queued check | killed | test_solver_scheduling::test_shutdown_interrupts_a_queued_z3_check_… |
| A01 | aged at `passed > workers` | killed | test_solver_scheduling::test_a_caller_overtaken_for_every_slot_is_served_next[1] |
| A02 | no aging | killed | same |
| A03 | aged at `2 × workers` | killed | same |
| A04 | lent grants age callers ahead | killed | test_solver_scheduling::test_a_lent_place_does_not_overtake_… |
| A05 | grants age callers behind too | killed | test_solver_scheduling::test_a_new_runs_first_check_is_served_within_a_grant_per_slot |
| A06 | `stick(lent=True)` counts | killed | test_solver_scheduling::test_a_lent_place_never_sticks_or_lends |
| A07 | `run` sticks a lent check (AUDIT-24's S06) | survived (**equivalent**) | `_Slots.stick` refuses a lent place. Reported "killed" by `test_audit_24::…_under_s06` only because that test's own `_mutant` finds its S06 text already replaced |
| A08 | hard at `>` | killed | test_solver_scheduling::test_a_run_is_hard_by_its_checks_run_time[4-False] |
| **A09** | `_HARD_SLICES = 8` | **survived** | → test_a_check_past_the_documented_run_time_makes_its_run_hard (L6) |
| A10 | start time on `perf_counter`, not `_clock` | killed | …_hard_by_its_checks_run_time[0-True] |
| **A11** | hardness/ms count worker start-up | **survived** | → test_solver_ms_excludes_starting_and_replacing_a_killed_worker (#5) |
| A12 | a running check has run 0 s | killed | test_solver_scheduling::test_a_cancelled_check_counts_the_time_it_ran |
| A13 | waiters FIFO | killed | test_solver_scheduling::test_a_free_slot_goes_to_a_short_caller_first |
| A14 | `passed` never grows | killed | test_a_caller_overtaken_for_every_slot_is_served_next[1] |
| A15 | lend to any caller | killed | test_solver_executor::test_slow_checks_are_bounded_by_slow_workers |
| A16 | hard only if it went slow | killed | …_hard_by_its_checks_run_time[4-False] |
| A17 | `stick` does not wake | killed | test_solver_scheduling::test_a_stuck_slot_lends_one_place_… |

**Totals:** 67 mutations. The existing suite killed 43 (F 12, W 17, A 14). F09 is among them, killed only by pytest-timeout. 7 are equivalent: F10, W08, W20, W21, W22, W25 and A07. 17 non-equivalent survived: W02, W03, W04, W06, W07, W09, W10, W15, W16, W17, W26, W27, W33, W36, W37, A09 and A11. W03 is killed by a `perf` test only. Each survivor is killed by a plain test in `test_audit_28.py`, and a strict xfail applies the mutant and runs that test. A11 has no xfail of its own: the #5 test kills it alongside W09 and W10.

Re-checks of round-6 pins:
- AUDIT-24 #4–#6's `L01`, `L02` and `L03` are now killed by the twins in `test_solver_loading.py` alone (`-x`, no audit files).
- ASSUME is killed by the `check`-argument count test, and by the AUDIT-14 #1 timeout repro (`perf`, 2 of 2). The AUDIT-14 #1 cancel repro passes under ASSUME (2 of 2), as designed: since SG29 a cancel kills the worker, whatever phase Z3 is in.

## Findings

### Critical

None.

### High

**#1. A check cut short in the caller can answer the next check (W06 survived; `solver.py` 238-241 uncovered; repro: `test_a_check_cut_short_never_answers_the_next_one_under_w06`, strict xfail "AUDIT-28 #1").**
- *The guard:* when anything raises in `check_entailment` while it waits on the pipe (a per-test timeout's signal, `KeyboardInterrupt`, any `BaseException`), `except BaseException: … pool.discard(worker)` kills the worker. Its request is already sent, and its reply will arrive.
- *Untested:* no test reaches that branch. With `pool.release(worker)` there instead, the whole suite passes. That one-word slip parks a worker with an unread reply in the idle list.
- *Measured:* the repro cuts the check of `p ⊢ p` short once its reply is waiting, then checks `p ⊢ q` on the same pool.
  - Today the second check is INVALID.
  - Under W06 it is **VALID**: the first check's reply, read as the second's (deterministic, 5 of 5 runs).
- *Why high:* this is the one place in SG29 where a regression turns an unentailed step into a VALID one, which breaks the verifier's soundness. The pipe protocol has no request id that would catch a mismatched reply, so this branch is the only line of defence.
- *Added:* `test_a_check_cut_short_never_answers_the_next_one` (passes today).
- *Suggested:* also tag each request with a sequence number in `Worker.call`, and treat a reply with the wrong number as a kill. A future path that forgets to discard then fails safe.

### Medium

**#2. Nothing pins that a cancelled slow check keeps its slow place until it ends (W17 survived, also the AUDIT-22 #5 `perf` repro; repro: `…_keeps_its_place_under_w17`, strict xfail "AUDIT-28 #2").**
- With `self._end_slow(job)` in place of `job.add_done_callback(self._end_slow)`, every test passes, including `test_cancelling_a_slow_check_does_not_hold_up_the_next_one` (6 passed under `-m perf`).
- That repro now passes either way, because the cancel kills the worker within milliseconds. So the slow-lane CPU bound (AUDIT-18 #5) is unguarded for a worker whose kill is slow.
- *Added:* `test_a_cancelled_slow_check_keeps_its_place_until_it_ends`. A target that ignores the interrupt holds the check. After the cancel `_slow` must still be 1, and under W17 it is 0. No clock is involved.

**#3. `test_judges_overlap_but_formalization_stays_ordered` fails whenever it runs alone (repro: run it alone; calibrated replacement: `test_judges_overlap_against_a_baseline_without_latency`, `perf`).**
- The test bounds wall time at 8 LLM delays (0.8 s) in absolute terms.
- *Measured:*
  - A warm run takes 0.76–0.78 s at a load of about 5, which leaves 20–40 ms of slack.
  - Since SG29 the first check of a process also starts the worker processes. The first `Verifier.verify` took 5.9 s at a load of 8, and the first check alone 0.4–0.55 s.
  - Run alone, the test failed 6 of 6 at a load of 4–10, and 3 of 3 at a load of 1, even after a test that had booted the pool.
  - In the full suite it passes because earlier tests warmed the workers. It is order-dependent.
- *Added:* a calibrated version.
  - It warms the workers, then takes the same run with no latency as the baseline (best of 2).
  - It requires `wall − baseline < (7 + 12) / 2` delays: halfway between full overlap (6 formalize calls in turn plus the last judge) and none (12 calls).
  - It also asserts that the 12 calls make the bound discriminating. It passed 3 of 3 alone and in the full suite.
- *Suggested:* replace the original with this, or warm the workers in a fixture and subtract a baseline.

**#4. A cancel that lands while a worker is being acquired is untested (W07 survived; `solver.py` 231-232 uncovered; repro: `…_under_w07`, strict xfail "AUDIT-28 #4").**
- If `interrupt.request()` runs between the early `requested()` check and `attach`, `attach` refuses and the check returns `interrupted` without a request.
- With the refusal ignored, the request goes to a worker that no cancel can reach. The cancelled (or shut-down) check then returns a verdict, and runs to its full timeout.
- *Added:* `test_a_cancel_at_acquire_sends_no_request`.

**#5. The `solver_ms` start-up test is vacuous in both halves (W09, W10, A11 survived; repro: `…_under_w09`, `…_under_w10`, strict xfails "AUDIT-28 #5").**
- `test_solver_ms_excludes_worker_start_up` has two halves, and neither holds up:
  - *"nor is replacing the timed-out one":* its hard check ends by Z3's own `timeout` inside the worker, which replies in time (as `test_a_worker_times_out_where_solve_times_out_here` shows). No worker is killed or replaced, so the slow `stop` never runs.
  - *"booting the worker is not counted":* its slow boot sleeps only for a worker whose `_ready` is unset. After any earlier test has used the shared pool, every worker it gets is booted. So W10 and A11 also pass the suite.
- *Added:* `test_solver_ms_excludes_starting_and_replacing_a_killed_worker`. The clock is fake and the check is killed. Start-up costs 10 s, the call 1 s and the stop 100 s, and `solver_ms` must be exactly 1000. Under W10 it is 11 000, under W09 101 000.

**#7. The AUDIT-22 #2 repros miss the pre-fix code in some runs (weak repro, measured against `492980e`).**
- *Measured:* across 8 runs against the pre-SG29 `solver.py`, `…_waits_for_the_gil_a_bounded_number_of_times` failed in 6 and `…_reaches_its_quiet_verdict` in 5.
- *Cause:* the GIL contention they measure depends on the busy thread getting CPU. Under load from other processes it is often descheduled, and the overhead drops below the bound (0.54 s against 0.2 s, but also ≤ 0.2 s).
- *Why it matters:* they are the only tests of the GIL hand-off that motivated SG29. An in-process revert would be caught only about 70% of the time.
- *Suggested:* repeat the measurement (best of 3) and assert on the largest overhead. Or count GIL waits directly (`sys.setswitchinterval` raised, and the busy thread's iterations counted) instead of wall time.

**#8. AUDIT-10 #4's strict xfail XPASSes in the full coverage run (2 of 2), so the audit's coverage command is red on the base.**
- `test_a_trivial_check_is_not_dominated_by_creating_a_context` compares `check_entailment` with the same parse and checks on a reused context in this process. The ratio is 2.06–2.28 standalone, with or without `--cov`, against a limit of 1.5. It xfails 4 of 4 alone and passes as an xfail with `test_audit_10.py` alone.
- In the full run under `--cov` it XPASSed twice. Since SG29 the measured side runs in an untraced worker, and the baseline in the long-lived, traced parent. Anything that slows the parent and not the worker (coverage tracing, a process heavy after 1300 tests) moves the ratio below 1.5.
- The repro now compares two different processes, so it no longer measures the overhead it names.
- *Suggested:* measure `_check_here` (the worker's code, in this process) against the reused-context baseline, or mark the finding resolved by SG29 and drop the strict xfail.

### Low

Each low finding with a mutant has a plain test that passes today, and a strict xfail `AUDIT-28 L<n>` that applies the mutant.
- **#6 (F09).** `test_tokens_of_text_with_long_trailing_blanks_stay_fast` has no bound of its own. Here the quadratic scan was caught only by pytest-timeout (60 s), after 76 s. On a machine about 3× faster, or with `--timeout=0`, it passes. *Added (`perf`):* the blank tail is timed against a twin of the same length that ends in tokens, best of 3, and may cost at most 20×. Today it costs well under 1×, and under F09 about 1000×.
- **L1 (W04).** A worker that a cancel killed after its reply arrived must be replaced, not parked dead in the idle list (the next check would pay a start-up).
- **L2 (W02, W03).** The kill grace is ADR 0009's `max(10 ms, 10% of the timeout)`. It is now pinned with the documented share hard-coded. Before, only the AUDIT-22 #3 `perf` repro noticed a 100% share.
- **L3 (W15, W16).** Pre-starting workers off the caller's thread, and keeping one idle worker for every place of the default pool, are pinned by no test. Without them every check pays worker start-ups.
- **L4 (W26, W27; child-only code).** Coverage cannot see worker-only code: workers end by `SIGKILL` (see "How this was measured"). No test checked that a worker ignores Ctrl-C or asks to die with its parent. `_serve` now runs in-process on the main thread (prctl replaced by a recorder), with SIGINT raised mid-loop. W27's test pins the call, not the kernel's `PR_SET_PDEATHSIG` effect. A child-process test that `SIGKILL`s a parent mid-check would pin that too.
- **L5 (W33, W36, W37).** Three `Workers` details had no test:
  - `close` stopped tracking idle workers without stopping them, and `live()` read 0 while they ran on.
  - `acquire` kept counting a dead idle worker in `live()`.
  - The forkserver preload of a `-m` main module was never checked.
- **L6 (A09).** Every SG30 test reads `_HARD_SLICES` itself, so 8 slices pass the suite. SG30's 4 × 50 ms is now hard-coded: a 150 ms check is not hard, a 250 ms one is.
- **L7.** `_tokens` stays quadratic in one shape that SG28's `rstrip` does not cover: blanks followed by an unterminated `"`. 8000 blanks then `"` take 2.8 s. No caller can reach it today: assertions pass `_is_single_term` first, and declaration lines with `"` are refused by `_decl_name`. A later caller would inherit it. No test was added; anchoring the scan (`match` from the last end, as `_is_single_term` does) would remove it.
- **L8.** `test_start_up_does_not_count_against_the_budget` asserts `call(..., 0.0001) in ((True, 0.5), None)`, which accepts the outcome the test exists to rule out. Its second half (W34) does catch a budget that includes start-up. The first assertion could be dropped or tightened.
- **L9.** The rewritten AUDIT-14 #1 repros call `Z3Solver._check_here` and `attached_worker`. Against any pre-SG29 `solver.py` they fail with `AttributeError` (2 of 2 against `176d019` and `492980e`), not on their assertions. They still catch the SG19 regression they name (ASSUME: 2 of 2), but they can no longer confirm a file-level revert.

### Notes (no finding)

- **Carried:** coverage targets are not enforced by `make test` or CI (AUDIT-8 M5). `state/checkpointer.py` is still at 80% (AUDIT-8 M6). The `perf` tests are still in the default run (AUDIT-8 H1, AUDIT-20 #7).
- AUDIT-24 #4–#6 were converted from strict xfails into tests that assert the guard fails under the mutant. That is the same check with its sign inverted. The pinning claimed by SG29 holds: L01–L03 are killed by `test_solver_loading.py` alone.
- `test_audit_24::test_a_lent_check_never_lends_a_place_under_s06` now tests a doubly guarded path. Both `run` and `_Slots.stick` refuse a lent place, so neither guard alone is observable (A06 is killed, A07 is equivalent).
- The ADR's "the main module must be safe to import" is real: a script without `if __name__ == "__main__":` re-runs its body in the forkserver and floods tracebacks. That is documented and not a test gap.

## Earlier repros flipped by SG28–SG30, against the pre-fix code

| Repro | Pre-fix file | Result | Verdict |
|---|---|---|---|
| AUDIT-22 #1 | `formalizer.py` @ 492980e | DID NOT RAISE, 2 of 2 | not weakened |
| AUDIT-22 #2 ×2 (`perf`) | `solver.py` @ 492980e | fail 6/8 and 5/8 (e.g. "quiet 25 ms, beside Python 1.94 s") | **weak (#7)** |
| AUDIT-22 #3 ×2 (`perf`) | same | fail 2/2 ("1.05 s of 0.21 s"; "freed after 0.98 s") | not weakened |
| AUDIT-22 #4 (`perf`) | same; `solver.py` @ 8f3cf14 (pre-SG30) | fail 2/2 each ("waited 1.92 s; … 1.91 s left") | not weakened |
| AUDIT-22 #5 (`perf`) | `solver.py` @ 492980e | fail 2/2 ("easy check 0.98 s") | not weakened against the revert, but misses W17 (#2) |
| AUDIT-22 #6 | same | fail 2/2 (`workers 2, slow 2` under a 1-CPU quota) | not weakened |
| AUDIT-24 #1 (`perf`) | ASSUME on today's code | AUDIT-14 #1 timeout repro fails 2/2; pre-SG29 files: AttributeError | holds (L9) |
| AUDIT-24 #2 | `solver.py` @ 492980e and @ 8f3cf14 | fail 2/2 each (task still short) | not weakened |
| AUDIT-24 #3 | same | `(1, 2) == (1, 1)` fails 2/2 each | not weakened |
| AUDIT-24 #4–#6 | today | converted to "the guard fails under the mutant"; L01–L03 killed by `test_solver_loading.py` alone | not weakened |

## Tests that bound wall time in absolute seconds

| Test | Bound | Assessment |
|---|---|---|
| test_solver_executor::test_judges_overlap_but_formalization_stays_ordered (`perf`) | `< 0.8 s` | fails alone 9/9 (#3); calibrated replacement added |
| test_solver_executor::test_solver_ms_excludes_worker_start_up (not `perf`) | `< 500 ms`, `< 700 ms` | vacuous in both halves (#5); deterministic replacement added |
| test_formalizer::test_tokens_of_text_with_long_trailing_blanks_stay_fast | pytest-timeout 60 s | machine-dependent (#6); ratio test added |
| test_solver_workers::test_an_interrupted_call_returns_none_at_once | `< 5 s` | generous; fine |
| test_solver_executor::…_kills_its_worker, …_kills_the_running_worker | `< _HANG_S / 2` (10 s) | generous; fine |

## Added tests (`tests/audit/test_audit_28.py`)

| Test | Kills / reproduces |
|---|---|
| test_a_check_cut_short_never_answers_the_next_one (+ xfail) | W06, #1 |
| test_a_cancelled_slow_check_keeps_its_place_until_it_ends (+ xfail) | W17, #2 |
| test_judges_overlap_against_a_baseline_without_latency (`perf`) | #3 (calibrated replacement) |
| test_a_cancel_at_acquire_sends_no_request (+ xfail) | W07, #4 |
| test_solver_ms_excludes_starting_and_replacing_a_killed_worker (+ 2 xfails) | W09, W10, A11, #5 |
| test_a_blank_tail_costs_no_more_than_a_tail_of_tokens (`perf`, + xfail) | F09, #6 |
| test_a_worker_killed_after_its_reply_is_replaced (+ xfail) | W04, L1 |
| test_the_kill_grace_is_the_documented_one (+ 2 xfails) | W02, W03, L2 |
| test_an_executor_prestarts_its_workers_off_the_callers_thread (+ xfail) | W16, L3 |
| test_the_shared_pool_keeps_a_worker_idle_for_every_place (+ xfail) | W15, L3 |
| test_a_worker_ignores_ctrl_c_and_asks_to_die_with_its_parent (+ 2 xfails) | W26, W27, L4 |
| test_closing_a_pool_stops_its_idle_processes (+ xfail) | W36, L5 |
| test_a_dead_idle_worker_is_no_longer_counted (+ xfail) | W37, L5 |
| test_a_main_module_run_with_dash_m_is_preloaded (+ xfail) | W33, L5 |
| test_a_check_past_the_documented_run_time_makes_its_run_hard (+ xfail) | A09, L6 |

`_mutant` (from `test_audit_24.py`) recompiles one function from its source with one exact-match edit and binds it to the module's own globals. `monkeypatch` restores the original. No source file is edited. Every xfail fails on its assertion under `--runxfail`, not on setup.

Verification:
- `make lint typecheck`: ruff clean, 150 files already formatted, mypy strict clean on 138 source files.
- `uv run pytest tests/audit/test_audit_28.py -q`: 15 passed, 17 xfailed.
- `--runxfail`: 17 failed, 15 passed.
- `uv run pytest` (no coverage): **1335 passed, 13 skipped, 71 xfailed** in 3 min 26 s, green. AUDIT-10 #4 xfails here; it XPASSed in both `--cov` runs (#8). The base gives 1320 passed and 54 xfail-marked tests, so this adds 15 passing tests and 17 strict xfails.
