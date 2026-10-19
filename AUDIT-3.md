# AUDIT-3: API and SDK consistency

- **Auditor:** an independent agent that wrote none of the audited code.
- **Base:** `origin/claude/sg6-persistence` @ `c1c4d15`. Branch `claude/audit-3`.
- **Installed SDKs (from `.venv`):** anthropic 0.102.0, pydantic 2.13.4.
- **Repros:** `tests/audit/test_audit_3.py`, 10 strict-xfail tests. Each test asserts the correct behaviour, so it starts to XPASS, and fails the suite, once its finding is fixed.
- **Gate:** `make lint typecheck test` gives 436 passed, 12 skipped, 10 xfailed.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | High | The INVALID-score cap ignores `Settings.pass_threshold`, so a refuted trace can pass | test |
| 2 | High | LLM failures that are not a `ValueError` (refusals, API 4xx) crash the graph instead of escalating | 2 tests |
| 3 | High | The model writes entity `id`/`created_at` values, and the store upserts by id across runs | 4 tests |
| 4 | Medium | HTTP 529 `OverloadedError` is not in `TRANSIENT_ERRORS` | test |
| 5 | Medium | `anthropic>=0.40` allows an SDK that has no `messages.parse(output_format=)` | citation |
| 6 | Medium | Dead `Settings` fields, dependencies and the `Checkpointer`/Redis interface, which also mangles `hitl_payload` | test + citations |
| 7 | Medium | README, architecture and ADRs describe a system that no longer exists | citations |
| 8 | Medium | The Research agent never uses the literature tools; citations are unverified model recall | citations |
| 9 | Low | Faithfulness fails open behind any wrapper other than `RetryingLLMClient` | test |
| 10 | Low | Schema constraints are not enforced at decode time, so violations escalate as `error` | citation |
| 11 | Low | Retries stack: SDK `max_retries=2` × `RetryingLLMClient` 3 attempts | citation |
| 12 | Low | The model id is hard-coded with no `Settings` field and does not match CLAUDE.md §3 | citation |
| 13 | Low | Small inconsistencies: duplicate trace schemas, a dead `except`, re-exports, docstrings | citations |

**Count:** 0 critical, 3 high, 6 medium, 4 low.

---

## 1. High: the INVALID-score cap ignores `Settings.pass_threshold`, so a refuted trace can pass

**Impact.** ADR-0007's core soundness guarantee, "any INVALID step means the trace can never pass", holds only at the default threshold. Suppose an operator sets `PASS_THRESHOLD=0.5` in `.env`, which `Settings` advertises as a supported knob. Every trace with an INVALID step still scores up to `0.69`. That score is at or above `0.5`, so both the agent gate and the monitor PASS it. A refuted reasoning step then flows downstream with no human review.

**Repro.** `test_refuted_trace_never_passes_under_configured_pass_threshold`: a trace with 9 VALID steps and 1 INVALID step scores 0.69, and `VerificationMonitor(pass_threshold=0.5)` returns `PASS`.

**Root cause.**
- `src/autolab/verification/confidence.py:28` sets `INVALID_SCORE_CAP = PASS_THRESHOLD - 0.01`, the module constant, at import time.
- `src/autolab/orchestrator/graph.py:81` and `:92` feed `cfg.pass_threshold` to the gate and the monitor.
- The cap and the gate therefore read two different sources. PLAN.md SG4 lists "the cap still derives from the module constants" as an open item, but it does not record that the consequence is a soundness hole.

**Fix.** Do not compare a capped score against a configurable threshold. Two options:
- Make the INVALID rule categorical: `VerificationMonitor.decide` and `BaseAgent.__call__` never PASS a verdict that has an INVALID step, whatever its score.
- Or pass the threshold into `consistency_score(verdicts, pass_threshold=...)`.

Also validate in `Settings` that `retry_threshold <= pass_threshold`.

## 2. High: LLM failures that are not a `ValueError` crash the graph instead of escalating

**Impact.** CLAUDE.md Phase 4 says "On permanent errors, escalate." In practice, `escalate_permanent_errors` converts only `ValueError` into an `error` HITL request. Several realistic live failures escape `graph.ainvoke` and end `autolab run` with a traceback.

- **A refusal or empty structured output.** `ClaudeLLMClient` raises `RuntimeError` for these (`llm.py:208`, `:227`). It escapes `Formalizer.formalize` (`formalizer.py:94` catches only `ValidationError`/`JSONDecodeError`), then `Verifier.verify`, then the agent, then the graph. `Formalizer.formalize`'s docstring promises `FormalizationError`. `faithfulness.py:51` does catch `RuntimeError`, so the same failure fails closed in one place and crashes in another.
- **Permanent API errors** such as `BadRequestError` (prompt too long, "Schema is too complex for compilation"), `NotFoundError` (bad model id), `AuthenticationError` and `PermissionDeniedError`. These are not transient, so they are not retried, and they are not `ValueError`, so they are not escalated.
- **Transient errors once retries run out.** These also propagate. PLAN.md SG4 records this case as an open item.

**Repro.**
- `test_formalizer_turns_a_refusal_into_formalization_error` uses a stub `messages.parse` that returns `parsed_output=None`. `RuntimeError` escapes.
- `test_llm_refusal_pauses_at_hitl_instead_of_crashing_the_graph` uses `build_graph` with a client that raises the same `RuntimeError`. `ainvoke` raises instead of pausing.

**Root cause.**
- `src/autolab/orchestrator/recovery.py:114` has `except ValueError`.
- `src/autolab/verification/formalizer.py:94`.
- `src/autolab/verification/llm.py:227` has no error type of its own.

**Fix.**
- Add a `StructuredOutputError(ValueError)`, or `LLMRefusalError`, raised by `ClaudeLLMClient` and caught by the formalizer, which turns it into UNGROUNDED.
- Have `escalate_permanent_errors` also catch `anthropic.APIStatusError` that is not in `TRANSIENT_ERRORS`.
- Check `response.stop_reason` (`refusal`, `max_tokens`) before reading `parsed_output`, so the HITL reason says why the call failed.

## 3. High: the model writes entity ids and timestamps, and the store upserts by id across runs

**Impact.** `messages.parse(output_format=Hypothesis)` sends the full Pydantic schema. It offers the model `id` (format `uuid`), `created_at` (format `date-time`), `confidence` and `question`, and the same holds for `ExperimentDesign.id` and `AnalysisReport.id`/`created_at`. Because those fields have `default_factory`, they are optional, so the model may fill them, and anything it fills is kept. This was verified with the SDK's own `transform_schema` (`anthropic/lib/_parse/_transform.py`). The consequences:

- **Cross-run data loss.** `StateStore.append` does `session.merge` keyed on `id` alone (`store.py:176`, `:204`). If two runs receive the same model-chosen UUID (models often reuse example UUIDs), the second run's write moves the row to its own `run_id`. The first run's audit trail silently loses its hypothesis.
- **Within-run dedup.** `RunTracer._mark` dedupes by id (`tracer.py`), so a later entity that repeats an earlier id is never persisted.
- **Non-independent noise.** `LabSimulator` seeds its noise from `design.id` (`simulator.py:60`). Reused ids give identical "random" results across designs.
- **Fabricated timestamps.** `created_at` comes from a model with no clock. The store and dashboard order rows by it (`store.py` `_load`).
- **Self-reported confidence.** `Hypothesis.confidence` is shown on the dashboard as "confidence" (`dashboard.py:127`), next to verified scores.

**Repro.**
- `test_structured_output_schema_does_not_let_the_model_pick_ids[...]` is parametrized over all three schemas.
- `test_an_id_reused_by_another_run_does_not_move_this_runs_row`: run A's hypothesis vanishes after run B appends one with the same id.

**Root cause.**
- `src/autolab/verification/llm.py:133-137` (`_REPLY_SCHEMAS`) passes the persistence models directly as LLM reply schemas.
- `src/autolab/state/store.py:204` makes the primary key `id` only.

**Fix.**
- Use separate LLM reply schemas that hold only content fields (`statement`, `rationale`, `citations`, and so on). Build the domain model in code with a fresh `uuid4()`, `datetime.now(UTC)`, and the known `question`, `hypothesis_id` and `result_id`. Copying the ids in code also removes the need for the model to echo `hypothesis_id`/`result_id`.
- Make the store key `(run_id, id)`, or refuse an upsert that would change `run_id`.

## 4. Medium: HTTP 529 `OverloadedError` is not treated as transient

**Impact.** Overloaded (529) is one of the most common transient API errors. In the installed SDK, `OverloadedError` subclasses `APIStatusError`, not `InternalServerError` (`anthropic/_exceptions.py:135`, mapped in `_client.py:526`, ahead of the `>= 500` branch). It is also not re-exported from `anthropic`. `TRANSIENT_ERRORS` lists `InternalServerError`, so after the SDK's own 2 retries a 529 skips `RetryingLLMClient`'s backoff and, per #2, crashes the graph.

**Repro.** `test_overloaded_error_is_retried`.

**Root cause.** `src/autolab/orchestrator/recovery.py:17-24`.

**Fix.** Classify by status code instead: retry `APIStatusError` when `status_code in {408, 409, 429}` or `status_code >= 500`. Or import `anthropic._exceptions.OverloadedError` explicitly, with a comment, since it is not public.

## 5. Medium: the `anthropic>=0.40` floor allows an SDK without `messages.parse(output_format=)`

**Impact.**
- `pyproject.toml:12` allows `anthropic` 0.40.0. Its wheel has no `messages.parse` and no `output_format`; I checked the downloaded wheel. On any resolve that is not locked, `ClaudeLLMClient.call_structured` fails with `AttributeError`.
- The justification comment at `pyproject.toml:11` says agents call Claude "via native tool-use for structured outputs". The code uses JSON structured outputs instead (`llm.py:220-226`).
- CLAUDE.md §7 Pattern E (`CLAUDE.md:285`) prescribes forced `tool_choice`. PLAN.md SG5 records the departure from Pattern E, but no ADR does.
- On the wire the call is correct: SDK 0.102 converts `output_format` into the current `output_config.format` (`resources/messages/messages.py:2652-2690`).

**Fix.**
- Raise the floor to the version that added `messages.parse(output_format=)`. The lock has 0.102.0; pin `>=0.102,<1` unless an older version is verified.
- Update the comment.
- Add an ADR that supersedes Pattern E.

## 6. Medium: dead `Settings` fields, dependencies and the Checkpointer/Redis interface

**Impact.** The configuration and dependencies describe features that do not exist, and the one live-looking interface among them is wrong.

- **`Settings` fields nothing reads.** `Settings.redis_url` (`config.py:39`), `log_level` (`:43`) and `step_budget_ms` (`:50`) are read by no code in `src/` or `scripts/`.
  - `scripts/bench_verifier.py` uses the module constant `STEP_BUDGET_MS` instead, so `STEP_BUDGET_MS=…` in the environment is ignored.
  - `scripts/error_injection.py` and `scripts/live_accuracy.py` judge against the module `PASS_THRESHOLD`, not `Settings`.
  - `run_live_tests` is read only by `tests/live/conftest.py`.
- **No logging at all.** No module imports `logging` or `structlog`, so `log_level` has nothing to configure. CLAUDE.md §9 requires a structured logger.
- **Unused dependencies.** `structlog`, `fastapi`, `uvicorn`, `pyyaml` (its comment says the golden traces are YAML, but they are Python) and `alembic` (there are no migrations) are imported nowhere (`pyproject.toml:23-38`). `redis` is used only by the dead `RedisCheckpointer`.
- **A dead, wrong checkpointer.** `Checkpointer`, `InMemoryCheckpointer` and `RedisCheckpointer` (`state/checkpointer.py`) are exported and documented as "the external checkpoint" (`orchestrator/checkpoints.py:13-14`, ADR-0004), but nothing uses them. The graph uses LangGraph savers. They are also wrong: `_loads` restores `hitl_payload` as raw JSON (`checkpointer.py:140-141`). After a restore, an `approve` would merge plain dicts into `hypotheses` and similar fields, and the next agent would fail on `target.id`.
- **A misleading `.env.example`.** It says `REDIS_URL` is the "LangGraph checkpointer", and it sets `LOG_LEVEL`, which nothing reads.

**Repro.** `test_checkpointer_round_trips_withheld_payload_models`. The remaining items have the citations above.

**Fix.**
- Delete `Checkpointer`/`InMemoryCheckpointer`/`RedisCheckpointer`, `redis_url`, `log_level` and the unused dependencies. Alternatively, wire them in: add a structlog setup driven by `log_level`, and read `step_budget_ms` in the bench.
- Add a `checkpoint_path` `Settings` field to replace the CLI-only default, which PLAN.md SG6 notes.
- Supersede ADR-0004.

## 7. Medium: the docs describe the wrong system

Each item gives the doc location first, then the code it contradicts.

- **`README.md:7`** says "Status: Phase 0 — foundation skeleton". The Quickstart (`README.md:20-21`) documents only `make run`, the version banner. Nothing documents `autolab run "<q>"`, `autolab resume <thread_id> approve|retry|abort [--note]`, `--checkpoints`, `make dashboard`, or the need for `ANTHROPIC_API_KEY` (`cli.py:30-47`, `Makefile:30`).
- **`docs/architecture.md:3`** says "Phase 0 captures the high-level intent". Line 28 says the state store is "Postgres + Redis", while the code uses SQLAlchemy (SQLite by default) plus an `AsyncSqliteSaver` checkpoint file and no Redis (`cli.py:94-98`, `state/checkpointer.py:76-87`). The file does not mention the `monitor`/`hitl` nodes, the retry loop, faithfulness checking or the tracer.
- **`docs/status.md:229`** says `autolab run` falls back to `FakeLLMClient` without a key. Since SG4 it exits with status 2 (`cli.py:53-60`).
- **ADR-0005** (`docs/decisions/0005-…md`) says `LLMClient` has a single `call` method, that `purpose` is `Literal["formalize","commonsense"]`, and that the Claude adapter is deferred. The current interface is `call_structured`, `purpose` is `str`, and the adapter is live (`llm.py:20-37`). No amendment records the change.
- **ADR-0006** says Analysis `_merge_payload` returns `{}`. It returns `{"analysis_reports": [...]}` (`agents/analysis.py:61-62`). The ADR's per-agent `hitl_threshold` "0.85 for Design" cannot be configured through `build_graph`, which uses one threshold for every agent (`graph.py:78-82`).
- **`verification/llm.py:3-5`** says the premise grounder calls an LLM to "surface commonsense premises". `premise_grounder.py` makes no LLM call, and the "commonsense" grounding in CLAUDE.md §6 Phase 2 step 3b is not implemented.
- **The `pyproject.toml:11` comment.** See #5.

**Fix.** Rewrite the README quickstart and status, refresh `architecture.md`, and add superseding notes to ADR-0004, 0005 and 0006.

## 8. Medium: the Research agent never uses the literature tools

**Impact.**
- CLAUDE.md §6 Phase 3 says the Research agent "queries literature tools (arxiv, PubMed), produces a `Hypothesis` with grounded citations".
- The agent instead asks the model to "Include at least one Citation" (`agents/research.py:55`), and the system prompt says "grounded in real literature… never invent identifiers" (`llm.py:78-79`).
- `tools/literature.py` (`LiteratureClient.search_arxiv` / `search_pubmed`) is imported by nothing in `src/`.
- Citations are therefore unverified model recall. They are never turned into `VerificationContext.facts`, yet they are persisted and shown as if grounded.

**Fix.** Query `LiteratureClient` in `ResearchAgent._produce`. Pass the results to the prompt and constrain `citations` to the returned identifiers. Alternatively, document this gap explicitly.

## 9. Low: faithfulness fails open behind any other wrapper

**Impact.**
- `Verifier(check_faithfulness=None)` turns the check on only when `is_live(llm)` finds `live is True` (`llm.py:148-150`).
- `live` is not part of the `LLMClient` protocol (`llm.py:29-37`). Only `ClaudeLLMClient` sets it (`:165`), and only `RetryingLLMClient` forwards it (`recovery.py:73`).
- Any other decorator, such as logging, caching or rate-limiting, silently turns faithfulness off for a real model. The safe default should be on.

**Repro.** `test_faithfulness_stays_on_for_a_wrapped_live_client`.

**Fix.** Add `live: bool` to the `LLMClient` protocol, with `FakeLLMClient.live = False`, so that mypy forces wrappers to declare it. Or default to `True` and turn the check off explicitly in tests.

## 10. Low: schema constraints are not enforced at decode time

**Impact.**
- The SDK removes `minimum`/`maximum`/`minLength` and moves them into the field description. This affects `Interpretation.likelihood` [0,1], `Hypothesis.confidence`, `estimated_cost_usd >= 0`, `rank >= 0`, `ReasoningStep.index >= 0` and `FormalizedStep.assertion` `minLength 1`.
- Model validators such as dense ranks and dense step indices, backward references and at least one interpretation are not expressible in the schema at all.
- A violation is a pydantic `ValidationError` after the call. It becomes an `error` HITL request with nothing to approve (PLAN.md SG4 open item) instead of a retry.
- The schemas are otherwise accepted by structured outputs. The `$defs`/`$ref` usage, the `uuid`/`date-time` formats, `additionalProperties: false`, and the optional and union counts are all well under the 24 and 16 limits.

**Citations.** `anthropic/lib/_parse/_transform.py` (the "leftover props go to description" branch), `state/models.py:24,60,…`.

**Fix.** Retry the producing agent once with the validation error as feedback before escalating.

## 11. Low: retries stack

`AsyncAnthropic` is built with the SDK default `max_retries=2` (`llm.py:187`). It is wrapped by `RetryingLLMClient(max_attempts=3)` (`recovery.py:27`, `graph.py:76`), so one logical call can make up to 9 HTTP attempts, each with the SDK's non-streaming timeout. Pick one layer: either `AsyncAnthropic(max_retries=0)`, or drop the API errors from `TRANSIENT_ERRORS`.

## 12. Low: the model id is hard-coded and does not match the brief

`ClaudeLLMClient(model="claude-sonnet-4-6")` (`llm.py:172`) is built with defaults by the CLI (`cli.py:81`), and no `Settings` field can change the model. The id is valid in SDK 0.102 (`types/model_param.py`), supports structured outputs (checked against the live structured-outputs docs), and is not in `DEPRECATED_MODELS`. CLAUDE.md §3 (`CLAUDE.md:55`) names "Claude Opus 4.7 for orchestrator", but the orchestrator makes no LLM calls, so the brief and the build disagree. Add `Settings.agent_model` and amend CLAUDE.md §3 or an ADR.

## 13. Low: small inconsistencies

- **Duplicate trace schemas.** `_TraceReply` (`llm.py:127`) and `_TraceSteps` (`agents/common.py:14`) are the same model. Only the second sets `extra="forbid"`; the SDK adds `additionalProperties: false` to the first anyway.
- **A dead `except`.** `formalizer.py:94` catches `json.JSONDecodeError`, which neither client raises: both validate through pydantic, which reports bad JSON as `ValidationError`.
- **Incomplete docstrings.** `ClaudeLLMClient.call_structured` documents `ValidationError` and `RuntimeError` but not the `anthropic.*` errors it can raise. `Verifier.verify` does not document that a bad `context.facts` declaration raises `FormalizationError` (`vericot.py:91`).
- **Re-exported constants.** `routing.py:71` re-exports `PASS_THRESHOLD`, and `agents/base.py:37` defines `HITL_THRESHOLD`. Neither is the threshold the graph actually uses (#1), so they invite the wrong one.
- **Unreachable text path.** `ClaudeLLMClient.call` has a free-text path (`messages.create`), but every production `call` purpose is in `reply_schema`, so the path is unreachable. A future purpose not in `_REPLY_SCHEMAS` would return prose that the agent then JSON-parses.
- **Dashboard gaps.** The store persists `ExperimentDesign` and `ExperimentResult` but has no loaders for them, so the dashboard cannot show the protocol or the measurements a decision was based on (`store.py`, `dashboard.py:33-42`).

---

## What held up

- **SDK call shape.** `messages.parse(model=, max_tokens=, system=<str>, messages=, output_format=<BaseModel>)` matches the installed `AsyncMessages.parse` signature (`resources/messages/messages.py:2602`). `parsed_output` is the SDK's own property: the first text block's parsed value, or `None`. The SDK sends the current `output_config.format` on the wire.
- **Schemas the API accepts.** All six reply schemas pass through `transform_schema` cleanly (`Hypothesis`, `ExperimentDesign`, `AnalysisReport`, `_TraceReply`, `FormalizedStep`, `FaithfulnessVerdict`). They use only supported features and stay far below the optional-property and union limits. `claude-sonnet-4-6` supports structured outputs.
- **One shared client.** A single lazily built `AsyncAnthropic` is shared per `ClaudeLLMClient`, and a system prompt is sent for each purpose family.
- **The SG0 contracts.** These match PLAN.md: `FormalizedStep`, `StepVerdict.reason`/`solver_ms`, `VerificationContext`, `Verifier.verify(trace, *, context=)`, `LLMClient.call_structured`, `HITLResolution`, `hitl_payload`, and the `Settings` threshold fields, apart from #1.
- **Fake and Claude agree.** `FakeLLMClient` and `ClaudeLLMClient` agree on `call_structured` semantics: both validate with pydantic and raise `ValidationError`. `RetryingLLMClient` forwards both methods and `live`.
- **Graph wiring.** Agents, monitor and `hitl` are wired as documented. The CLI `run`/`resume` match the SG6 description (`resume` checks for a pending interrupt first). The dashboard reads what the tracer writes, with run id = thread id, a UUID from the CLI.
- **The rest of `Settings`.** `pass_threshold` (apart from #1), `retry_threshold`, `max_retries`, `hitl_cost_usd`, `hitl_duration_min`, `solver_timeout_s` and `database_url` are all honoured by `build_graph` and the CLI.
- **No `Any` leaks.** `Any` appears only in state-update dicts, `HITLRequest.payload`, the compiled-graph return value and LangGraph `Command` types. No unvalidated LLM data reaches typed state without a pydantic validation first.
