# AUDIT-7: API and SDK consistency, round 2

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg9-entity-identity` @ `8a1c1de` (SG9 with SG10 merged). Branch `claude/audit-7`.
- **Installed SDKs (from `.venv`):** anthropic 0.102.0, pydantic 2.13.4, langgraph-checkpoint 4.3.0. Every SDK claim below was settled against that source.
- **Repros:** `tests/audit/test_audit_7.py`, 9 strict-xfail tests. Each asserts the correct behaviour, so it XPASSes, and fails the suite, once its finding is fixed. Doc-only items cite `file:line` on both sides.
- **Gate:** `make lint typecheck test` gives 616 passed, 12 skipped, 10 xfailed (the 9 below plus AUDIT-3 #6).
- **Flaky test (not a finding of this audit):** `tests/audit/test_audit_2.py::test_slow_checks_do_not_starve_the_default_executor` asserts a wait of under 0.25 s. In 1 of 2 full-suite runs it measured 0.272 s, although it passes alone. That file runs before this one.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | High | A run killed by an exhausted transient error cannot be resumed, despite the docstring | test |
| 2 | Medium | The `(run_id, id)` key breaks every write to an existing (pre-SG9) database | test |
| 3 | Medium | The documented Postgres URL has no installed driver, and the dialect check fires mid-run | test |
| 4 | Medium | `max_tokens` and refusal replies raise `ValidationError`, not `StructuredOutputError` | 2 tests |
| 5 | Low | `content_schema` drops the domain model's validators | 2 tests |
| 6 | Low | `SolverExecutor` lifecycle: shutdown turns a queued check into a cancellation | test |
| 7 | Low | The simulator averages boolean parameters as numbers | test |
| 8 | Low | Stale docs, dead code and undocumented exceptions left by round 1 | citations |

**Count:** 0 critical, 1 high, 3 medium, 4 low.

---

## 1. High: a run killed by an exhausted transient error cannot be resumed

**Impact.**
- CLAUDE.md principle 6 (`CLAUDE.md:85`) requires that "a crash mid-experiment can resume without re-running expensive steps". The recovery docstring claims this holds: transient errors, "once exhausted ... propagate, and the checkpoint allows a resume" (`orchestrator/recovery.py:143-144`).
- It does not hold at the CLI. Three connection errors, or three 429/529 responses, within about 1.5 s of jittered backoff are enough. They end `autolab run` with a traceback.
- The thread id is never printed on that path. It is generated at `cli.py:65` and printed only on a pause or completion (`cli.py:106-116`).
- If the user finds the id anyway, `autolab resume <id> retry` exits 2 with "no paused run". `_drive` accepts only threads that have a pending `interrupt()` (`cli.py:99-101`), and it can only send a `Command(resume=HITLResolution)` (`cli.py:72-75`). There is no way to continue from the last checkpoint.
- The same holds for any exception raised by the tracer after an agent node (see #2). The agent's LLM spend is lost, and the run must start over.
- Round 1 recorded "exhausted transient errors still propagate" (`AUDIT-3.md:72`, PLAN.md SG8 Open). It did not record that the documented resume does not exist.

**Repro.** `test_run_killed_by_a_transient_outage_can_be_resumed`:
1. `autolab run` with an LLM that raises `APIConnectionError` dies.
2. The outage ends.
3. `autolab resume <thread> retry` exits 2: `autolab resume: no paused run ...`.

**Root cause.**
- `src/autolab/cli.py:99-101` treats "has interrupts" as the only resumable state.
- `src/autolab/cli.py:65` and `:106-116` print no thread id on failure.
- `src/autolab/orchestrator/recovery.py:143-144` promises behaviour the CLI does not offer.

**Fix.**
- Print the thread id before `ainvoke`, or on any exception.
- When `aget_state(config).next` is non-empty and there are no interrupts, let `autolab resume <id>` call `graph.ainvoke(None, config)`, which continues from the last checkpoint.
- Alternatively, catch an exhausted transient error at the node boundary and turn it into an `error` HITL request. The `retry` resolution then exists already.
- Correct the docstring either way.

**Resolution (SG13).** `autolab run` prints `Run <thread_id> started.` and flushes stdout before the graph runs, so a crash leaves the id on screen. `autolab resume <thread_id> [approve|retry|abort]` reads `aget_state` first. A thread paused at an interrupt still needs an action (exit 2 without one) and takes `Command(resume=HITLResolution)`. A thread with pending nodes and no interrupt continues from its last checkpoint with `ainvoke(None, config)`. A given action is ignored there, with a note on stderr. A finished or unknown thread exits 2 with "no paused run ... (it is finished or unknown)". The `escalate_permanent_errors` docstring now says an exhausted transient error ends the run and `autolab resume <thread_id>` continues it. The repro is flipped. Not done: the node boundary still does not turn an exhausted transient error into an `error` HITL request, so the run still ends with a traceback.

## 2. Medium: the `(run_id, id)` key breaks every write to an existing database

**Impact.**
- SG9 changed every entity table's primary key from `id` to `(run_id, id)`. The schema is still made by `create_all` (`state/store.py:159`), which never alters an existing table, and there is no Alembic history, although CLAUDE.md asks for "Migrations via Alembic" (`CLAUDE.md:173`).
- PLAN.md says an existing database "keeps the old single-column key until it is recreated" (`PLAN.md:192`; also `AUDIT-2.md:79`). That understates it. On SQLite, `ON CONFLICT (run_id, id)` matches no unique constraint, so every `append` raises `OperationalError`.
- The default `DATABASE_URL` is `./autolab.db` (`config.py:36-38`), and `.gitignore` does not ignore it. So any checkout that ran `autolab` before SG9 crashes on the first traced agent node.
- The crash comes after Research's LLM calls are paid. It escapes the graph from `_observed` (`orchestrator/graph.py:291`), outside `escalate_permanent_errors`, and per #1 it cannot be resumed.

**Repro.** `test_store_on_an_sg6_database_still_persists_entities` creates the SG6 tables, opens a `StateStore` on them, and appends a hypothesis: `sqlite3.OperationalError: ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint`.

**Root cause.**
- `src/autolab/state/store.py:60-66` defines the new composite key.
- `:136-139` makes the upsert target that key.
- `:155-160` uses `create_all` with no version check.

**Fix.**
- Add an Alembic migration that rebuilds the entity tables with the composite key.
- Or, at minimum, check the entity tables' primary-key columns in `StateStore.create` and fail fast with "recreate or migrate `<url>`" before any agent runs.
- Correct the PLAN.md and AUDIT-2.md wording.

## 3. Medium: the documented Postgres URL has no installed driver

**Impact.**
- `state/store.py:13` says "Postgres + `psycopg` is production".
- `.env.example:9` shows `DATABASE_URL=postgresql+psycopg://...`.
- `upsert_statement` has a `postgresql` branch (`store.py:114-117`).
- But no Postgres driver is a dependency (`pyproject.toml:25` says "Postgres adapter added once available in CI"), none is in `uv.lock`, and none is installed. Following `.env.example` gives `ModuleNotFoundError: No module named 'psycopg'` from `StateStore.create`.
- Separately, an unsupported dialect is accepted by `StateStore.create` and only raises `ValueError` at the first `append` (`store.py:131-132`). That is mid-run, after the first agent's LLM calls, and outside the escalation wrapper.
- SG9's "Postgres is verified only by compiling the SQL" (`PLAN.md:193`) is true, but production Postgres cannot even start.

**Repro.** `test_documented_postgres_url_has_an_installed_driver`: `create_async_engine("postgresql+psycopg://...")` raises `ModuleNotFoundError`.

**Root cause.** `pyproject.toml:9-40` has no `psycopg[binary]` or `asyncpg`. `src/autolab/state/store.py:155-160` does not validate `engine.dialect.name` against `_INSERTS`.

**Fix.**
- Add `psycopg[binary]>=3.2` (justified in `pyproject.toml`), perhaps as a `postgres` extra.
- Check the dialect in `StateStore.create`, so an unsupported URL fails before the run starts.
- Optionally, add a live-gated Postgres round-trip test.

## 4. Medium: `max_tokens` and refusal replies are not `StructuredOutputError`

**Impact.**
- The SG8 resolution says `ClaudeLLMClient` "raises `StructuredOutputError(RuntimeError)` naming the `stop_reason`" (`AUDIT-3.md:72`, `PLAN.md:171`). The class docstring lists "a refusal, `max_tokens`, an empty reply" (`verification/llm.py:148-149`).
- In SDK 0.102, `messages.parse` validates every text block inside the response parser. `parse_text` calls `TypeAdapter(output_format).validate_json(text)` (`anthropic/lib/_parse/_response.py:16-20`).
  - A truncated `max_tokens` reply is invalid JSON, so `pydantic.ValidationError` ("EOF while parsing") is raised inside `messages.parse`, before `call_structured` reaches its `parsed_output is None` check (`llm.py:243`).
  - A refusal whose text is prose does the same.
- `StructuredOutputError` fires only when the reply has no text block at all. That case is also reached when `parsed_output` is falsy (`types/parsed_message.py:52-56`).
- Consequences:
  - The HITL reason a human sees is a pydantic JSON error, with no `stop_reason`.
  - A `max_tokens` truncation of a long trace (`max_tokens=4096`, `llm.py:185`) is indistinguishable from a malformed reply, so nobody learns to raise the limit.
  - The `RuntimeError` branch of `Formalizer.formalize` (`formalizer.py:97-99`) is, in practice, only an empty-content path.
- The round-1 suggested fix, "check `response.stop_reason` before reading `parsed_output`", cannot work through `parse`, because the exception fires first.

**Repro.** `test_unusable_reply_raises_structured_output_error_naming_stop_reason[max_tokens|refusal]` drives a real `AsyncAnthropic` through an `httpx.MockTransport`. Both cases raise `ValidationError`.

**Root cause.** `src/autolab/verification/llm.py:236-248` relies on `parsed_output is None`. The SDK raises before that check is reached.

**Fix.**
- Call `messages.create(..., output_config={"format": {"type": "json_schema", "schema": transform_schema(schema)}})`. Then check `stop_reason` in (`max_tokens`, `refusal`) and raise `StructuredOutputError`, then `schema.model_validate_json` the text.
- Or wrap `parse` and re-raise a `ValidationError` as `StructuredOutputError` when the response is available.
- Update the docstrings and the Resolution line.

## 5. Low: `content_schema` drops the domain model's validators

**Impact.**
- `content_schema` is documented as "`model` without its `server_owned` fields" (`agents/common.py:23-34`). `create_model` copies fields only, so `AnalysisReport._ranks_are_dense` (`state/models.py:254-262`) is not part of `REPORT_REPLY`.
- An empty `interpretations` list, or ranks `[1]`, therefore passes `messages.parse` and `FakeLLMClient` validation. It then fails later, at `AnalysisReport(**dict(reply), result_id=...)` (`agents/analysis.py:45`).
- The end result is the same `error` escalation (AUDIT-3 #10). But the reply schema the tests validate against is looser than the contract, and the failure carries the wrong model name.
- The same gap would silently apply to any validator later added to `Hypothesis` or `ExperimentDesign`.

**Repro.** `test_report_reply_enforces_the_report_validators[empty|ranks-not-dense]`.

**Fix.** Pass `__base__` or `__validators__`, or define the reply schemas explicitly with the shared validators. Alternatively, document that domain validation happens after the call.

## 6. Low: `SolverExecutor` lifecycle

**Impact.**
- **Per loop, not per pool.** The class docstring says "At most `workers` checks are submitted at once" (`verification/solver.py:212`, also `AUDIT-2.md:46`). The semaphore is per event loop (`solver.py:244-250`), so two loops sharing the process-wide `default_executor()` queue work inside the pool. The CLI tests do this: `cli.main` runs on a worker thread.
- **Shutdown cancels queued checks.** `shutdown()` calls `ThreadPoolExecutor.shutdown(cancel_futures=True)` (`solver.py:242`). A queued check's future is cancelled, and its caller gets `asyncio.CancelledError` although nobody cancelled it. That is a `BaseException`, which escapes `escalate_permanent_errors` and the graph as if the run had been cancelled.
- **Callers after shutdown.** A caller that arrives after `shutdown()` gets the pool's raw `RuntimeError("cannot schedule new futures after shutdown")`. `escalate_permanent_errors` (`orchestrator/recovery.py:150`) converts it into a human review request.
- **Interrupted checks look like timeouts.** A check interrupted by `shutdown_default_executor()` returns UNGROUNDED/`timeout` (`solver.py:121-126`). The persisted verdict is indistinguishable from a real solver timeout.
- **No injection point in the graph.** `build_graph` has no `solver_executor` parameter (`orchestrator/graph.py:54-62`, `:77-82`), so graph runs and their tests always share the process-wide pool.
- **No close for the LLM client.** `ClaudeLLMClient` has no `aclose`, so the CLI never closes its `AsyncAnthropic` (`cli.py:78-81`).

**Repro.** `test_shutdown_does_not_turn_a_queued_check_into_a_cancellation`: two event-loop threads share a one-worker executor, and `shutdown()` gives the queued caller `CancelledError`.

**Fix.**
- Use one `threading.BoundedSemaphore`, or a process-wide counter, for admission.
- Map a cancelled concurrent future that the caller did not cancel, and a submit after shutdown, to a dedicated error, such as `SolverUnavailable(RuntimeError)`, or to an UNGROUNDED verdict with an `interrupted` detail.
- Add `build_graph(solver_executor=...)` and `ClaudeLLMClient.aclose()`, and have the CLI close both.

## 7. Low: the simulator averages boolean parameters as numbers

**Impact.**
- `ParameterValue` includes `bool` (`state/models.py:62`), and `DESIGN_REPLY` offers the model `{"type": "boolean"}` for `value`.
- `LabSimulator._baseline` filters with `isinstance(p.value, (int, float))` (`tools/lab_hardware/simulator.py:67`). That accepts `True`/`False` as 1/0, so `stirred=True` drags the simulated measurement toward 1.
- `_sigma` (`:75`) accepts `noise_sigma=True` as 1.0.
- These measurements become the code-built `context.facts` that Execute and Analysis are verified against.

**Repro.** `test_simulator_ignores_boolean_parameters_in_the_baseline`: `temperature=10.0, stirred=True` gives 5.5, not 10.0.

**Fix.** Exclude `bool` explicitly (`not isinstance(p.value, bool)`).

## 8. Low: stale docs, dead code and undocumented exceptions

Each item gives the claim first, then the code it contradicts.

- **"All writes go through `upsert_statement`."** `PLAN.md:188` and `AUDIT-2.md:65` ("every write is one statement from `store.upsert_statement`") say so. But `StateStore.create_run` (`state/store.py:170-182`) is a plain `session.add` insert. It is used by no production code, only by tests and `scripts/audit_load.py:314`. Delete it or route it through `upsert_statement`.
- **The simulator docstring.** `tools/lab_hardware/simulator.py:36` says "identical designs produce identical results across runs". Since SG9, `design.id` is a fresh `uuid4` per reply (`agents/design.py:38`), so identical model replies produce independent noise, as PLAN.md SG9 intends.
- **`ClaudeLLMClient` docstring.** It says "`call` routes the agents' JSON purposes ... so no agent parses free-form prose" (`verification/llm.py:169-171`). After SG9 only `.trace` purposes route (`llm.py:139-145`). The free-text branch (`llm.py:208-224`) is still unreachable from production (AUDIT-3 #13).
- **`Verifier.verify` lists no exceptions** (`verification/vericot.py:104-113`). It raises `FormalizationError` on a bad `context.facts` declaration (`vericot.py:122`) and propagates non-transient `anthropic.APIError`s from the formalizer and judge. Similarly, `result_claim` (`agents/common.py:71-74`) raises `ValueError` on non-finite values without saying so. `SolverExecutor.run` (`solver.py:225-234`) does not mention the `RuntimeError`/`CancelledError` from #6.
- **Leftovers that round 1 asked to remove**, still present:
  - `json.JSONDecodeError` and `import json` in `verification/formalizer.py:16,95`.
  - `_TraceReply` (`llm.py:130`) duplicates `_TraceSteps` (`agents/common.py:46`).
  - `HITL_THRESHOLD` in `agents/base.py:43`.
  - The `PASS_THRESHOLD` re-export in `orchestrator/routing.py:21,71`.
  - The `Checkpointer` mention in `orchestrator/checkpoints.py:12-14`.
- **`tests/audit/test_audit_3.py:3-4`** still says every test is a strict xfail. Only one is.

## Round-1 resolutions re-checked

| Round-1 item | Claimed | Holds? |
|---|---|---|
| AUDIT-3 #1 cap from configured thresholds | SG7 | Yes. `confidence.py:192-228`, `vericot.py:432-437`, `graph.py:77-82`; `blocking_reason` is checked first in `decide` and in `BaseAgent`. |
| AUDIT-3 #2 permanent errors escalate | SG7/SG8 | Partly. The graph pauses on `ValueError`/`RuntimeError`/non-transient `APIError` (`recovery.py:147-163`). The `stop_reason` claim fails (#4), and the exhausted-transient resume does not exist (#1). |
| AUDIT-3 #3 code-owned ids | SG9 | Yes for the schemas. `transform_schema` of all three reply schemas has no `id`, `created_at`, `question`, `hypothesis_id` or `result_id`. `additionalProperties: false`, 0–4 optional fields and at most 1 union each, all accepted. Agents set ids in code. The migration gap is #2; `confidence` is still self-reported (open). |
| AUDIT-3 #4 529 retried | SG8 | Yes. `is_transient` matches `_should_retry` (`anthropic/_base_client.py:804-837`) exactly, including `x-should-retry`. |
| AUDIT-3 #9 `is_live` wrapper contract | SG8 | Yes, as documented (`llm.py:152-160`). A wrapper without `live` or `_inner` still fails open. |
| AUDIT-3 #11 one retry layer | SG8 | Yes. `AsyncAnthropic(max_retries=0)` (`llm.py:200`). An injected client keeps its own setting, as documented. |
| AUDIT-3 #12 model id | SG8 (partial) | Yes, `DEFAULT_MODEL` (`llm.py:28`). The `Settings` field and the CLAUDE.md "Opus orchestrator" mismatch are still open. |
| AUDIT-3 #5, #6, #7, #8, #10, #13 | none | Still open, unchanged: `pyproject.toml:11-12`; `config.py:39,43`; `README.md:7`; `docs/architecture.md:3,28`; ADR-0004/0005/0006; `agents/research.py`; #13 as listed in #8 above. The AUDIT-3 #6 xfail still fails. |
| AUDIT-2 #1/#5/#6/#7 executor | SG10 | Yes for one event loop. The admission bound is per loop, and shutdown has the gaps in #6. |
| AUDIT-2 #2/#3 store keys | SG9 | Yes for fresh databases (concurrent upserts, cross-run ids). Not for existing files (#2), and `create_run` bypasses the upsert (#8). |
| AUDIT-2 #4 jittered retries | SG8 | Yes. Full jitter, `retry-after` honoured and capped at 60 s. |

## What held up

- **The SDK call shape.** `messages.parse(model, max_tokens, system, messages, output_format=<BaseModel>)` matches `AsyncMessages.parse` (`resources/messages/messages.py:2602-2700`), which sends `output_config.format = {"type": "json_schema", "schema": transform_schema(...)}`.
- **Every reply schema is acceptable to the API.** That covers `HypothesisReply`, `ExperimentDesignReply`, `AnalysisReportReply`, `_TraceReply`, `FormalizedStep` and `FaithfulnessVerdict`. Each passes `transform_schema` with only supported types and formats, and stays far below the optional-parameter and union limits.
- **Code-built entities match the domain models.** `Hypothesis(**reply, question=)`, `ExperimentDesign(**reply, hypothesis_id=)` and `AnalysisReport(**reply, result_id=)` supply every required field. Nested models arrive as instances, and the domain constructors re-validate.
- **Checkpoint round-trips.** LangGraph's msgpack decoder rebuilds allowlisted models with `cls(**model_dump())` (`langgraph/checkpoint/serde/jsonplus.py:737-750`), so nested `Citation`/`Measurement`/`StepVerdict` values are re-validated after a resume. Allowlisting only the top-level `CHECKPOINT_TYPES` is sufficient.
- **Transient-error classification** matches the SDK, and the payload of an escalated HTTP error carries its status.
- **Store keys for new databases.** The `(run_id, id)` key, `ON CONFLICT` upserts, the tracer's per-run dedup and `load_run` agree with each other. A resumed run in a new process re-records nothing twice.
- **The faithfulness and formalizer error paths fail closed** on `ValidationError` and `RuntimeError`, whichever of the two #4's replies raise.
- **The gate** (`make lint typecheck test`) is green, and `mypy --strict` is clean over `src` and `tests`.
