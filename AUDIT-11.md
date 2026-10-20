# AUDIT-11: API and SDK consistency, round 3

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg14-timing-tests` @ `7b8c452` (SG14 with SG13 merged). Branch `claude/audit-11`.
- **Installed (from `.venv`):** anthropic 0.102.0, pydantic 2.13.4, langgraph 1.2.0, langgraph-checkpoint 4.3.0, langgraph-checkpoint-sqlite 3.1.2, pytest 9.0.3, pytest-timeout 2.4.0. SDK and plugin claims below were checked against that source or by running it.
- **Repros:** `tests/audit/test_audit_11.py`, 7 strict-xfail tests, one per finding #1 to #7. Each asserts the correct behaviour. Under `--runxfail` all 7 fail on their stated assertion. #8 is docs-only and cites `file:line`.
- **Gate:** before this audit, `make test` gave 756 passed, 13 skipped, 18 xfailed. With this audit it gives 756 passed, 13 skipped, 25 xfailed. Lint and `mypy --strict` are clean.
- **Flaky test (not a finding of this audit):** `tests/integration/monitoring/test_graph_observability.py::test_concurrent_traced_runs_share_one_store[30]` failed in 1 of 2 gate runs and in 1 of 3 runs on its own. Each time, `sqlite3.OperationalError: database is locked` was raised while other agents' suites kept the machine at a load average of about 12. It passes when the machine is less loaded. It needs a busy timeout or a retry on `database is locked`, or a `perf`-style marker.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | High | An approved Analysis escalation is never persisted, so the audit store and dashboard lose the run's final report and the decision | test |
| 2 | Medium | `pytest-timeout` cannot end a hang in the 12 CLI tests: the suite hangs and no test is named | test (subprocess) |
| 3 | Medium | `autolab resume <id> abort` prints "Run ... complete." and exits 0 | test |
| 4 | Medium | The contradiction trigger (principle 7c) and SG12's Research re-check cannot fire from `autolab run` | test |
| 5 | Low | `autolab run` prints a thread id before the store and checkpointer open, so a run that fails there cannot be resumed | test |
| 6 | Low | `design_facts` maps distinct parameter names to one symbol, so an honest design gets contradictory facts | test |
| 7 | Low | `checkpoints.hitl_node` is a dead, policy-free export | test |
| 8 | Low | Stale docs and docstrings, including new ones from SG12 to SG14 | citations |

**Count:** 0 critical, 1 high, 3 medium, 4 low.

---

## 1. High: an approved Analysis escalation is never persisted

**Impact.**
- Principle 8 (`CLAUDE.md:89`) says "Every decision writes a structured trace... These traces are the product". The Phase 6 DoD (`CLAUDE.md:230`) says "Dashboard renders a completed run end-to-end".
- SG6 says the tracer writes "any new entities (now including `AnalysisReport`) to the `StateStore`". The tracer docstring says the same (`monitoring/tracer.py:9-10`).
- Entities are recorded only by `_observed`, around agent nodes (`orchestrator/graph.py:183`). A withheld payload is merged by the `hitl` node (`orchestrator/checkpoints.py:121`), which is not observed. The merge gets persisted only because the *next* agent records its input state. SG6 relies on this: "visible only as the following agent's events" (`PLAN.md:134`).
- Analysis has no following agent: `NEXT_NODE["analysis"] = END` (`orchestrator/routing.py:29`). So when a human approves an Analysis escalation, which happens after any blocked Analysis trace once retries run out:
  - The final graph state holds the report.
  - The store never gets it.
  - The dashboard (`monitoring/dashboard.py:33`, `make dashboard`) shows a finished run whose last event is `analysis / escalate`. It has no analysis report and no record that anyone approved it.
- The same gap drops every human decision, and any `policy` re-pause raised by the `hitl` node itself (SG12). Neither one produces a `TraceEvent`.

**Repro.** `test_an_approved_analysis_report_is_persisted`: a full traced graph run with `max_retries=0` and a failing Analysis trace. After `approve`, `final["analysis_reports"]` has one report and `store.load_reports(run)` is `[]`.

**Root cause.** `orchestrator/graph.py:105-116`: only agent nodes are wrapped in `_observed`, and `hitl` is added bare. `orchestrator/checkpoints.py:121` merges the payload and can go straight to `END`.

**Fix.** Observe the `hitl` node as well. After `interrupt()` returns, record a `TraceEvent` for the resolution (`node="hitl"`, `decision` = the action, `trigger`, the note). Before returning, call `tracer.record_state(run_id, merged)` on the merged update. Add a test that approves at every agent and compares the store with the final state.

## 2. Medium: `pytest-timeout` cannot end a hung CLI test

**Impact.**
- SG14 added `pytest-timeout` "so a hang fails with the test named (AUDIT-8 H1)" (`PLAN.md:261`, `pyproject.toml:67-68`, `timeout = 60` at `pyproject.toml:83`). On Linux the plugin uses the `signal` method.
- That works for hangs on the test's own thread, including a solver check with no time limit. I checked AUDIT-8's S08 mutant (`_remaining_ms` returning 0) by running it: the test fails at the limit and is named, and SG10's cancel path interrupts the Z3 context.
- The CLI tests do not get that protection. They call `cli.main` through a `with ThreadPoolExecutor(max_workers=1) as pool: pool.submit(...).result()` helper (`tests/unit/test_cli.py:30-33`, `tests/integration/test_cli_resume.py:27-31`, `tests/audit/test_audit_7.py:60-63`). When SIGALRM fires, `pytest.fail` is raised out of `.result()`, and then `with` exits and calls `shutdown(wait=True)` on the hung worker. That call never returns.
- The session then hangs, never prints a report, and is stopped only by CI's 10-minute job timeout (`.github/workflows/ci.yml`). With the default `-q`, no test is named. This hits exactly the 12 tests that drive the whole graph through the public entry point.

**Repro.** `test_a_hung_cli_test_fails_and_is_named` runs a child `pytest --timeout=1` on a test that uses the repo's own `tests.unit.test_cli.autolab` helper around a `_drive` that never returns. The child does not exit within 12 s. With a daemon-thread helper, the same child exits in about 1.3 s with `FAILED ::test_hung_cli - Failed: Timeout`. I checked this by hand.

**Root cause.** The `with ThreadPoolExecutor` helpers listed above, combined with `timeout_method = signal` (the default; it is not set in `pyproject.toml:79-88`).

**Fix.** Run `cli.main` on a `threading.Thread(daemon=True)` and re-raise its exception in the helpers. Or use `pool.shutdown(wait=False, cancel_futures=True)` in a `finally`, with a daemon worker, because `concurrent.futures` joins its workers at exit. Alternatively, set `timeout_method = "thread"` for these tests, which dumps every stack and exits. Factor the three copies of the helper into one fixture.

## 3. Medium: an aborted run is reported as complete

**Impact.**
- `abort` is one of the three documented resolutions. It routes to `END` with a cleared state (`orchestrator/checkpoints.py:125`).
- `_drive` treats any end without an interrupt as success. It prints `Run <id> complete. confidence=<last agent's score>, hypotheses=<n>` and returns, so the exit status is 0 (`cli.py:149-152`). After a $50 design is aborted, the CLI prints `complete. confidence=1.00, hypotheses=1`.
- SG13 now names four CLI states (crash, paused, finished, unknown). "Aborted" is a fifth one that the CLI cannot tell apart from "finished", either when the run ends or when it is resumed later ("no paused run ... (it is finished or unknown)", `cli.py:133-137`).
- SG6 listed this as open (`PLAN.md:134`). The SG13 rework of `_drive` left it in place. `test_resume_abort_ends_and_a_second_resume_is_rejected` throws the output away (`tests/integration/test_cli_resume.py:91`), so no test notices.

**Repro.** `test_resume_abort_does_not_report_a_completed_run`.

**Root cause.** `cli.py:142-152` checks only `__interrupt__`. The `hitl` node leaves no trace of the abort in state (`checkpoints.py:125`).

**Fix.** Have the `abort` branch set a terminal marker (for example `current_agent="aborted"`, or an `outcome` field). `_drive` then prints `Run <id> aborted at <agent>.` and exits non-zero. Use the same marker in the resume "finished or unknown" message.

## 4. Medium: the contradiction trigger cannot fire from `autolab run`

**Impact.**
- Principle 7c (`CLAUDE.md:87`) and SG4 (`PLAN.md:102`) require a pause before "a new hypothesis that contradicts an earlier verified one". SG12 extends this to approved Research escalations (`orchestrator/graph.py:112`).
- `contradiction_trigger` returns `None` when `state["hypotheses"]` is empty (`orchestrator/checkpoints.py:177-179`).
- In every graph run, Research runs only from `START` (`graph.py:117`). It is retried without merging, and no edge leads back to it after its payload is merged (`routing.py:24-29`, `checkpoints.py:121`). `autolab run` always starts from `empty_state` (`cli.py:74`), and nothing loads earlier verified hypotheses from the store.
- So on every CLI path, `prior` is `[]` and the trigger is dead code. That covers the agent gate and SG12's `merged_policy(contradiction, "hypotheses")`, each of which costs one verify per prior hypothesis when it does run.
- The tests reach the trigger only by seeding `state["hypotheses"]` by hand (`tests/integration/orchestrator/test_graph.py:362-365`). That makes the feature look covered.
- A side effect: the `hitl` node runs this policy outside `escalate_permanent_errors`. If it ever became reachable, a permanent API error during the re-check would crash the graph instead of pausing.

**Repro.** `test_a_hypothesis_contradicting_a_stored_one_pauses`: the store holds a verified hypothesis from an earlier run on the same question ("Y plays no role in X", formalized `(not hyp_Y)`), and the contradiction check is seeded. `autolab run` then completes instead of pausing.

**Root cause.** `cli.py:74` (`empty_state`), `orchestrator/checkpoints.py:177-179`, and the graph topology (`graph.py:117`, `routing.py:24-29`).

**Fix.** Decide what "verified prior state" means.
- If it means earlier runs, have `autolab run` seed `hypotheses` (or a separate `prior_hypotheses` field, so they are not re-persisted) from `StateStore` for the same question. Wrap the `hitl` policy call with the same error escalation as the agents.
- If it means only the current run, delete the trigger and the SG12 Research policy, and say in CLAUDE.md and PLAN.md that 7c cannot occur in a single linear run.

## 5. Low: a printed thread id is not always resumable

**Impact.** SG13 prints `Run <id> started.` before `asyncio.run(_drive(...))` (`cli.py:71-74`). But `_drive` opens the store (`cli.py:111`) and the checkpointer (`cli.py:113`) before the graph writes its first checkpoint. If either fails, the user has an id, and `autolab resume <id>` answers "no paused run ... (it is finished or unknown)" (`cli.py:133-137`). Examples are the documented Postgres URL without a driver (AUDIT-7 #3), an unwritable `--checkpoints` path, or the old-schema database from AUDIT-7 #2. The recovery docstring's "The last checkpoint is kept, and `autolab resume <thread_id>` continues from it" (`orchestrator/recovery.py:144-145`) has no checkpoint to continue from in these cases.

**Repro.** `test_a_printed_thread_id_is_resumable`: `DATABASE_URL=postgresql+psycopg://...` raises `ModuleNotFoundError` after the id is printed. With the URL fixed, resuming that id exits 2.

**Fix.** Open the store and the saver, then print the id, then run. Or print the id from inside `_drive` once both are open. The id must be printed before the graph's first node, which SG13's crash test already requires.

## 6. Low: distinct design parameters can share one fact symbol

**Impact.** `_param_symbol` replaces every non-word character with `_` (`agents/common.py:120-121`). So `temp-C`, `temp C` and `temp_C` all become `param_temp_C`. `ExperimentParameter.name` is any string (`state/models.py:68`), and nothing makes names unique. For such a design, `design_facts` (`agents/common.py:99-113`) emits two `(declare-const param_temp_C Real)` lines (the vocabulary accepts the duplicate) and the contradictory facts `(= param_temp_C 4.0)` and `(= param_temp_C 37.0)`. Every premise of an honest trace is then `premises_inconsistent`. Design retries and then escalates, and the human sees a refuted trace that the model did not write. `measurement_facts` has the same pattern (`_symbol`, `common.py:130-132`), but its names come from code, so it is safe today.

**Repro.** `test_distinct_parameters_get_distinct_fact_symbols`: 4 facts, 3 distinct declarations.

**Fix.** Make the symbol injective, for example by appending a short hash or the parameter index on a collision. Alternatively, reject duplicate sanitized names in `DESIGN_REPLY` validation, so the reply is retried.

## 7. Low: `hitl_node` is a dead, policy-free export

**Impact.**
- SG12 replaced the graph's `hitl` node with `make_hitl_node(policies)` (`orchestrator/graph.py:110-116`), but kept `hitl_node = make_hitl_node()` exported (`orchestrator/checkpoints.py:130`, `:202`).
- No production code uses it; only `tests/audit/test_audit_4.py:23,228` does.
- Anyone who builds a graph from the exported node, as `docs/status.md:142` still describes, silently loses the AUDIT-5 #3 policy re-check. An approved verification escalation then merges a $50 design with no cost pause.

**Repro.** `test_no_policy_free_hitl_node_is_exported`.

**Fix.** Drop `hitl_node`, and point `test_audit_4.py` M16/M21 at `make_hitl_node({})`. If a policy-free node is wanted, make `policies` a required argument, so that dropping the policies is an explicit choice.

## 8. Low: stale docs and docstrings

Each item gives the claim first, then the code it contradicts.

- **New since round 2:**
  - `tests/audit/test_audit_7.py:3-5` says every test is a strict xfail, but #1 is now a plain test. The same holds for `tests/audit/test_audit_3.py:3-4` (AUDIT-7 #8, still open).
  - `docs/status.md:142` says `orchestrator/checkpoints.py` provides "`hitl_node` using LangGraph's `interrupt()`". The graph uses `make_hitl_node(policies)`.
  - The `cli.py:24-33` docstring documents exit 2 for a missing `ANTHROPIC_API_KEY` only for `run`. `resume` exits 2 the same way (`cli.py:59-65`). Asking `resume` whether a run is finished needs an API key, although no LLM call is made.
  - `orchestrator/recovery.py:144-145` says the last checkpoint is kept for every exhausted transient error. That is not true before the first checkpoint (#5).
  - `PLAN.md:261`, `pyproject.toml:67`: "a hang fails with the test named" does not hold for the CLI tests (#2).
- **README.** `README.md:7` still says "Phase 0 — foundation skeleton". The Quickstart (`README.md:20-21`) documents only `make run`. Neither documents `autolab run "<q>"`, SG13's `autolab resume <thread_id> [approve|retry|abort] [--note]` forms, the printed thread id, `--checkpoints`, `make dashboard` or `ANTHROPIC_API_KEY`.
- **Package docstrings.** `src/autolab/orchestrator/__init__.py:1` and `src/autolab/agents/__init__.py:1` say "Placeholder ... implemented in a later phase".
- **Still open from earlier rounds**, unchanged:
  - `orchestrator/checkpoints.py:14-16` (the `Checkpointer` as "*external* checkpoint").
  - `verification/llm.py:3-5` (the premise grounder calls an LLM).
  - `verification/llm.py:169-171` (`call` routes agent JSON purposes).
  - `docs/architecture.md:3,28`, `docs/status.md:229`, ADR-0004, ADR-0005 and ADR-0006.
  - `.env.example` (`REDIS_URL` as the checkpointer, `LOG_LEVEL`).

---

## Earlier fixes re-checked

| Item | Claimed | Holds? |
|---|---|---|
| AUDIT-3 #1 INVALID cap from configured thresholds | SG7 | Yes. `confidence.py:31-67` caps at `score_cap(pass, retry)`. `blocking_reason` is checked first in `VerificationMonitor.decide` (`verification_monitor.py:43-54`) and in `BaseAgent` (`base.py:91-93`). `build_graph` passes the `Settings` thresholds (`graph.py:79-84`, `:98-100`). |
| AUDIT-3 #2 permanent errors escalate | SG7/SG8 | Yes for agent nodes (`recovery.py:151-164`). Still partial: AUDIT-7 #4 (`max_tokens` and refusal raise `ValidationError`, not `StructuredOutputError`) is still a failing xfail, and the `hitl` node's policy call is not wrapped (#4). |
| AUDIT-3 #3 code-owned ids, `(run_id, id)` keys | SG9 | Yes. `common.py:24-44`, `store.py:63-64`, `:136-139`. `Hypothesis.confidence` is still self-reported. AUDIT-7 #2 (old databases) is still open. |
| AUDIT-3 #4 529 retried | SG8 | Yes. `recovery.py:34-44` matches the SDK's `_should_retry`. |
| AUDIT-3 #5 `anthropic>=0.40` floor, no ADR for Pattern E | none | Open. `pyproject.toml:11-12` is unchanged, and no ADR supersedes Pattern E. |
| AUDIT-3 #6 dead `Settings` fields, dependencies, `Checkpointer`/Redis | none | Open. `config.py:39-50`; `structlog`, `fastapi`, `uvicorn`, `pyyaml`, `alembic` and `redis` have no importer except `state/checkpointer.py`. The xfail still fails. |
| AUDIT-3 #7 stale README, architecture and ADRs | none | Open. See #8. |
| AUDIT-3 #8 Research ignores the literature tools | none | Open. Nothing in `src/` imports `tools/literature.py`. |
| AUDIT-3 #9 `is_live` behind wrappers | SG8 | Yes, as documented (`llm.py:152-160`). A wrapper with neither `live` nor `_inner` still fails open. |
| AUDIT-3 #10 schema constraints only after decoding | none | Open. A `ValidationError` is an `error` escalation, not a retry (`recovery.py:151`). AUDIT-7 #5's xfail still fails. |
| AUDIT-3 #11 one retry layer | SG8 | Yes. `AsyncAnthropic(max_retries=0)` (`llm.py:200`). |
| AUDIT-3 #12 model id | SG8 (partial) | Partly. `DEFAULT_MODEL` (`llm.py:28`) is valid in SDK 0.102 (`types/model_param.py:15`) and is not in `DEPRECATED_MODELS`. There is no `Settings` field, and CLAUDE.md §3 still names Opus for the orchestrator. |
| AUDIT-7 #1 resume after a crash | SG13 | Yes for a crash inside the graph. The repro passes, and the 5 `test_cli.py` cases hold. I also checked a crash in Research (three `ConnectionError`s on `research.trace`): `resume <id>` completes. Gaps: a failure before the first checkpoint (#5); an aborted run reads as finished (#3); an exhausted transient error still ends the run with a traceback. |
| SG12 `make_hitl_node` / `merged_policy` | SG12 | Yes for Design. `merged_policy(cost, "current_experiment")` matches `DesignAgent._merge_payload`, and an approved expensive verification escalation re-pauses as `policy`. The Research entry is unreachable (#4). The exported `hitl_node` is dead (#7). |
| SG14 `pytest-timeout` | SG14 | Partly. `timeout = 60` with the `signal` method bounds hangs on the test thread, including the S08 solver mutant. It does not bound the CLI tests (#2). `perf` is declared under `--strict-markers`. |

## What held up

- **SDK call shape.** `messages.parse(model, max_tokens, system, messages, output_format=<BaseModel>)` matches `AsyncMessages.parse` in 0.102 (`resources/messages/messages.py:2602-2700`), which merges `output_format` into `output_config.format` with `transform_schema`.
- **Purpose routing.** Every purpose the code sends (`formalize`, `faithfulness`, `research.*`, `design.*`, `execute.trace`, `analysis.*`) has a system-prompt family. Every `call` purpose ends in `.trace` and goes through `_TraceReply`. The agents re-validate with `_TraceSteps` (`extra="forbid"`).
- **SG12 binding.** `design_claim` and `design_facts` agree on symbol names and exact decimals. Boolean and string parameters are named in the claim but get no fact. The trace prompt names the same claim the verifier binds.
- **SG13 CLI states.** Paused without an action exits 2. Paused with an action resumes. A crash with pending nodes continues, and an action given there is ignored with a note. A finished or unknown run exits 2. The paused output names the agent and prints the resume command.
- **Checkpoint allowlist.** `CHECKPOINT_TYPES` covers every top-level model in `AutoLabState`. `sqlite_saver` and `new_in_process_saver` share it.
- **Store, tracer and dashboard** agree on `(run_id, id)` keys and `trace_id` for verdicts, for every entity an agent node records.
