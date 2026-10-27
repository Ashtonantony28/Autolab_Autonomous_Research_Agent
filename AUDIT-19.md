# AUDIT-19: API and SDK consistency, round 5

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg22-solver-fairness` @ `603e452` (SG22 with SG20, SG21 and SG23 merged). Branch `claude/audit-19`.
- **Installed (from `.venv`):** anthropic 0.102.0, pydantic 2.13.4, z3-solver 4.16.0, langgraph-checkpoint-sqlite 3.1.2. SDK claims below were checked against that source (`anthropic/resources/messages/messages.py`, `anthropic/lib/_parse/_response.py`, `anthropic/types/parsed_message.py`) or by running it.
- **Repros:** `tests/audit/test_audit_19.py`, 18 strict-xfail cases for findings #1 to #7. Each asserts the correct behaviour. Under `--runxfail` all 18 fail on their stated assertion, deterministically (no timing, no network, `FakeLLMClient` only). #8 is docs-only and cites `file:line`.
- **Gate:** see the end of this file.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | Medium | The design figure guard rejects ordinary protocols: gene and strain names that are ISO 4217 codes (`TOP10`, `SHP2`, `SOS1`, `PGK1`, `MAD2`), animal ages (`8-week-old`), `Day 1:` schedules, "per cent", metres | 9 tests |
| 2 | Medium | A figure rejection never reaches the model: with a passing verdict it goes straight to a human, and with a blocked one the retry's feedback drops the figures | 2 tests |
| 3 | Medium | SG20's earlier-use rule forbids names the formalize prompt never shows (bound variables of earlier steps) | test |
| 4 | Medium | The Analysis report prompt gives the model only the number of measurements, not their names or values | test |
| 5 | Medium | Designs and results are written to the store but can never be read back; the audit dashboard cannot show the design a human approved | test |
| 6 | Low | `result_claim` names measurements by their raw name while the facts use SG21's hashed symbol | 3 tests |
| 7 | Low | SG23's busy timeout covers the state store but not the checkpoint database the CLI writes every step (5 s against 30 s) | test |
| 8 | Low | Stale docs, docstrings and audit headers, including new ones from SG20 to SG23 | citations |

**Count:** 0 critical, 0 high, 5 medium, 3 low.

No finding met the High bar. The closest is #1 with #2: on an ordinary mouse or gene-knockdown study `autolab run` stops for a human with a reason that names a gene or an age as a "cost or duration figure". It does not fail: `approve` merges the design, and the cost policy still runs.

---

## 1. Medium: the figure guard rejects ordinary designs

**Impact.**
- SG21 made `protocol_figures` fail closed (`agents/common.py:166-203`): any ISO 4217 code followed by a number, any amount next to a time word, and any lowercase single-letter unit next to an amount count as a figure. `DesignAgent.__call__` withholds every design with a match (`agents/design.py:67-86`).
- The model is told only "Do not state any cost (currency amount) or duration (time) figure" (`PROTOCOL_FIGURES_RULE`, `agents/design.py:92-96`). None of the following is a cost or a duration, so a model that follows the rule writes them, and each is rejected:
  - Gene and strain names that start with an ISO 4217 code: `TOP10` (the common *E. coli* cloning strain; TOP = Tongan paʻanga), `SHP2` (SHP = St Helena pound), `SOS1` (Somali shilling), `PGK1` (a standard qPCR reference gene; PGK = kina), `MAD2` (dirham), `CUP1` (Cuban peso), `TOP2A`. The code-and-number branch (`common.py:197-202`) matches `TOP` then `10`.
  - Animal ages: `8-week-old C57BL/6 mice`, the standard phrasing in mouse protocols, is `8-week`.
  - Day-numbered schedules: `Day 1: seed cells. Day 2: ...` gives `Day 1`, `Day 2`.
  - British "per cent": `2 per cent agarose` (`cent` is a currency word, `common.py:118`, matched as the second word after an amount, `:192-196`).
  - Distances in metres (`2 m`, since lowercase `m` is a time unit, `common.py:127`), `min 3` (minimum), `5 pounds` of ice.
- The Design system prompt adds a pull towards the rejected form: it asks for "parameters with units" (`verification/llm.py:85-87`), while `design_figures` rejects any parameter whose `value unit` text is a time (`common.py:224`). So any timing parameter (an incubation, an exposure) cannot be stated at all, and the guard's rule is the only thing that tells the model so.
- The rejection goes to a human without a retry (#2). SG21 checked the benchmark and the fixtures only (`PLAN.md:367`); none uses a gene name, an age or a schedule.

**Repro.** `test_ordinary_protocol_text_is_not_a_figure[...]` (8 protocols, each returns its match). `test_a_design_on_8_week_old_mice_is_admitted`: a design whose trace verifies (confidence 1.0) is withheld with `protocol states cost or duration figures ['8-week']`.

**Root cause.** `agents/common.py:197-202` (an ISO code needs only a following number, with no separator), `:186-196` (any amount followed by a time word in a 3-word window, including the age compound `8-week-old` and `per cent`), and `:127` (lowercase `m` and `d` as times). `agents/design.py:92-96` describes a narrower rule than the guard enforces.

**Fix.** Keep the guard fail-closed but narrow the obvious non-figures: require a separator (space, `:` or `.`) between an ISO code and its amount, so `TOP10`/`SHP2`/`PGK1` pass while `USD 500` and `GBP 5` do not; treat `<n>-<time unit>-old` as an age; drop `m` from `_TIME_EXACT` (`min`, `mins` remain). Make the prompt state what the guard does ("no number next to a time word or a currency code, including ages and day numbers"), so a model can comply. Add an allowed-table case for each example above.

## 2. Medium: a figure rejection never reaches the model

**Impact.**
- PLAN.md SG21 lists as a fold-in: "a figure rejection is retried with feedback before it escalates (AUDIT-15 #3)" (`PLAN.md:363`). The SG21 result records it as not done (`PLAN.md:367`, "Open"). AUDIT-15 #3's retry half had no repro, so nothing tracks it.
- `DesignAgent` labels the rejection `trigger="verification"` (`agents/design.py:82-84`). The monitor retries a `verification` request only if `decide(history[-1])` is RETRY (`orchestrator/routing.py:51-56`). Two cases:
  - **The trace verified.** `decide` returns PASS, so the run goes to `hitl` with retries unused. This is the case for every honest design that trips #1.
  - **The trace was also blocked.** The monitor retries, but with `feedback = history[-1].summary` (`routing.py:63`), for example `1 valid, 1 invalid, 0 ungrounded across 2 steps`. The figures the guard found, which are in `pending.reason`, are dropped. The model writes the same protocol again, the retries are spent, and the run escalates anyway.
- Only a human `retry` without a note passes the figure reason on (`orchestrator/checkpoints.py:163-165`).
- `agents/base.py:15-18` says `verification` means the verifier's verdict did not pass; here it does pass.

**Repro.**
- `test_a_figure_rejection_is_retried_with_its_reason`: a `$500` protocol with a verified trace. The monitor node returns `goto="hitl"`, not `"design"`.
- `test_a_retried_design_is_told_about_its_figures`: the same protocol with a failing trace. The monitor retries, but the feedback lacks `$500`.

**Root cause.** `orchestrator/routing.py:51-65` (RETRY only from the verdict; feedback only from the verdict summary) and `agents/design.py:82-84` (a figure rejection reuses the `verification` trigger).

**Fix.** Give the rejection its own trigger (`figures`). In `make_monitor_node`, retry a `figures` request while retries remain, with `pending.reason` as feedback, and append `pending.reason` to the feedback whenever a `verification` retry carries a request whose reason is not the verdict's. After `max_retries`, escalate as now.

## 3. Medium: the earlier-use rule forbids names the prompt never shows

**Impact.**
- SG20's structural fix: a step may not declare a name that is a token of any earlier accepted assertion (`verification/formalizer.py:124-133`, `:141`; `PLAN.md:353`). Those tokens include bound variables of `forall`, `exists` and `let`.
- The model is told "Do not declare a name that an earlier step already used" (`formalizer.py:180`). But `prompt_for` shows only the vocabulary's declarations (`formalizer.py:173-174`), not earlier assertions, so the model cannot know which bound names were used. The system prompt asks for exactly the forms that create them: "forall/exists for quantified claims" (`verification/llm.py:110`).
- A realistic trace: step 0 "every specimen is stable" is `(forall ((spx Specimen)) (stable spx))`. Step 1 "the first specimen is stable" declares `spx`. Step 1 fails formalization (UNGROUNDED, `formalization_failed`), which blocks the trace when it is the conclusion or a step it rests on. The retry feedback is the verdict summary, which does not name the rule (#2's feedback path), so the same choice is likely on the retry.
- PLAN.md SG20 records "A let/forall binder named like a later declaration fails that step (fail closed)" as open (`PLAN.md:358`), but not that the prompt cannot help the model avoid it.

**Repro.** `test_the_formalize_prompt_names_the_symbols_earlier_steps_used`: step 1's declaration of `spx` is rejected with "would change the meaning of an earlier step", and the prompt the formalizer sent for step 1 never contains `spx`.

**Root cause.** `formalizer.py:113` builds the prompt from the vocabulary alone (`prompt_for(step, self.vocabulary)`), while the rule at `:128-133` reads `self._used`.

**Fix.** Pass the used names to the prompt, for example a line "Names already used by earlier steps (do not declare): ..." listing `self._used` minus SMT keywords, numerals and the vocabulary's own names. Or exempt bound variables from the rule by tracking binder scopes, since a binder cannot change an earlier step's meaning; only free uses can.

## 4. Medium: the Analysis report prompt withholds the data

**Impact.**
- The Analysis agent asks for the ranked interpretations with `build_analysis_prompt(result)` (`agents/analysis.py:42-45`), which states only `status` and `measurement_summary: <n> measurements` (`analysis.py:61-66`). It gives no measurement name, value or unit, and not the hypothesis or the design.
- The system prompt asks the model to "Interpret the experiment result" (`verification/llm.py:94-98`), and CLAUDE.md Phase 3 asks for "multiple ranked hypotheses for what the data means". The model has to invent its interpretations.
- The measurements appear only in the second call, the trace prompt (`analysis.py:69-75`). By then the rank-0 interpretation is fixed as the payload claim the trace must prove from the measurement facts (`analysis.py:52-54`).
- So, with a real model, an Analysis report is either unrelated to the data or fails verification and is retried with the verdict summary as feedback, which still gives no data. This is the one agent whose payload prompt omits its input. Research gets the question, Design the hypothesis, and Execute has no payload prompt.

**Repro.** `test_the_analysis_prompt_states_the_measurements`: the prompt for a result with `absorbance = 0.4213` contains neither.

**Root cause.** `agents/analysis.py:61-66`.

**Fix.** Put `result_claim(result)` (or each `name = value unit`) in `build_analysis_prompt`, plus the hypothesis statement from state. Register the new prompt in the agent tests and the benchmark fakes.

## 5. Medium: designs and results are written to the store but never read

**Impact.**
- `RunTracer.record_state` persists the run's `ExperimentDesign` and every `ExperimentResult` (`monitoring/tracer.py:55-63`). `StateStore` has rows for both (`state/store.py:79-86`).
- `StateStore` has no loader for either (`store.py:270-284`: hypotheses, traces, verifications, reports, events). `load_run` therefore rebuilds a run without them (`monitoring/dashboard.py:33-42`), and `render_run` shows only events, hypotheses, the analysis summary and traces (`dashboard.py:116-131`).
- So the dashboard cannot show the design a human approved: its protocol, cost, duration and parameters, which are what the cost/duration pause and the figure guard (#1) are about. It also cannot show the measured values. A `hitl` row says `design / approve`, but not what was approved. CLAUDE.md Phase 6 asks that "A scientist can audit every decision" and that the dashboard "renders a completed run end-to-end" (`CLAUDE.md:230`).
- The rows are only written, never read: no code in `src/`, `scripts/` or `tests/` (outside `test_store.py`'s round trip of other models) loads them.

**Repro.** `test_the_dashboard_view_holds_the_runs_design_and_result`: a design and a result appended to an in-memory store. Neither id appears in the `RunView` that `load_run` returns.

**Root cause.** `state/store.py:270-284` (no `load_designs` / `load_results`) and `monitoring/dashboard.py:33-42`, `:116-131`.

**Fix.** Add `load_designs` and `load_results` to `StateStore`. Carry them in `RunView` (a run can have several designs across retries, so a list, not `current_experiment`). Render a design table (protocol, cost, duration, parameters) and the measurements. Test that every entity type the tracer writes is loaded by `load_run`.

## 6. Low: the result claim does not name the measurement fact symbols

**Impact.**
- SG21 hashes a measurement name that is not a plain identifier, or that starts with `m_` or a digit, into `m_<ascii>_<sha256[:12]>` (`agents/common.py:264-267`). `measurement_facts` declares and constrains that symbol (`:80-86`).
- `result_claim`, the payload claim the Execute trace must conclude and the "measurements" line of the Analysis trace prompt, still writes the raw name (`common.py:74-77`). For example, `OD₆₀₀ = 0.4` is beside the fact `(= m_OD____7728e7c0596a 0.4)`.
- `design_claim` writes `_param_symbol(p.name)` (`common.py:97`), so the Design trace prompt names the exact fact symbol, while the Execute and Analysis prompts do not. A model formalizing the claim either guesses that the hashed vocabulary symbol is `OD₆₀₀`, or declares a fresh constant (`OD600`), which leaves the claim unconnected to the fact, so it is not entailed.
- `LabSimulator`'s names are plain today (`tools/lab_hardware/simulator.py:24-28`), so this needs another `HardwareAdapter`.

**Repro.** `test_result_claim_names_each_measurement_fact_symbol[OD₆₀₀|m_absorbance|600nm_absorbance]`.

**Root cause.** `agents/common.py:76` uses `m.name` where the facts use `_symbol(m.name)`.

**Fix.** Write `_symbol(m.name)` in `result_claim`, as `design_claim` does for parameters. Optionally keep the raw name in parentheses for the reader.

## 7. Low: the checkpoint database did not get SG23's busy timeout

**Impact.** SG23 gave the state store a 30 s SQLite busy timeout (`SQLITE_BUSY_TIMEOUT_S`, `state/store.py:50-51`, `:148-164`) so that concurrent writers wait instead of failing. The CLI also writes a second SQLite file, the LangGraph checkpoints, on every super-step: `sqlite_saver` opens it with `aiosqlite.connect(str(path))` (`state/checkpointer.py:82`), which keeps sqlite3's default 5 s timeout. Its `setup()` already puts it in WAL mode. Two `autolab` processes share `autolab_checkpoints.db` by default (`cli.py:19`, `:49-51`). Under load like SG23's, the checkpoint write fails first: an `OperationalError: database is locked`, which ends the run with a traceback. The cause SG23 fixed for the store remains in the other database.

**Repro.** `test_the_checkpoint_connection_waits_as_long_as_the_store`: `PRAGMA busy_timeout` on the saver's connection is 5000, not ≥ 30000.

**Root cause.** `state/checkpointer.py:82`.

**Fix.** `aiosqlite.connect(str(path), timeout=SQLITE_BUSY_TIMEOUT_S)`, with the constant moved to one module both use (or to `config.py`).

## 8. Low: stale docs, docstrings and audit headers

Each item gives the claim first, then the code it contradicts.

- **New since round 4:**
  - `tests/audit/test_audit_15.py:3-4` says every test "is a strict xfail". #1 to #5 are plain tests since SG20 to SG22.
  - No ADR records SG20's earlier-use rule (`formalizer.py:124-133`), SG21's fail-closed figure scan (`common.py:166-203`), SG22's slow-check scheduling and `SOLVER_SLOW_WORKERS` (`solver.py:44-50`, `:275-332`), or SG23's WAL and busy timeout (`store.py:148-164`). CLAUDE.md §10 asks for one per non-obvious choice. The newest ADR is ADR-0007 (2026-10-19). The SG15–SG19 gap is still open (AUDIT-15 #8).
  - `SOLVER_SLOW_WORKERS` and `SQLITE_BUSY_TIMEOUT_S` are module `Final`s, which CLAUDE.md §8 allows. Neither is in `Settings` or `.env.example`, which is consistent. But `build_graph` takes no `SolverExecutor` (`orchestrator/graph.py:61-70`), so the CLI cannot tune the pool, and `SQLITE_BUSY_TIMEOUT_S` is missing from `store.__all__` (`store.py:295-307`). The `store.py:1-14` module docstring does not mention WAL or the timeout, and the WAL listener's `-wal`/`-shm` files next to `./autolab.db` (`.env.example:8`) are not in `.gitignore`. Neither is `autolab.db` or `autolab_checkpoints.db`.
  - `agents/base.py:15-18` says `trigger="verification"` means the verdict did not pass. A Design figure rejection uses it with a passing verdict (#2).
  - `verification/vericot.py:17-19` says checks run on "a bounded `SolverExecutor`". Since SG22 the bound is `workers + slow_workers` threads (up to 38 on a large host), not `workers`. `SOLVER_WORKERS`' docstring "Default size of the Z3 pool" (`solver.py:44-45`) reads the same way.
- **Corrected since round 4:** ADR-0007's "the premises are added and checked, then the negated claim is added and checked again" (`docs/decisions/0007-…md:47`) matches the code again, since SG22 asserts `claim!` rather than assuming it (`solver.py:156-161`). PLAN.md's Audits table now lists round 4 (`PLAN.md:406`).
- **Still open from earlier rounds**, unchanged:
  - `docs/status.md:142` names the removed `hitl_node`.
  - `README.md:7` ("Phase 0 — foundation skeleton") and `README.md:20-21` (no `autolab run`, `resume`, `make dashboard` or `ANTHROPIC_API_KEY`).
  - `docs/architecture.md:3`, `:28`.
  - `orchestrator/checkpoints.py:16-18` (the `Checkpointer` as the "*external* checkpoint").
  - `verification/llm.py:3-5` (the premise grounder calls an LLM) and `:21-23` (a `commonsense` purpose that no code sends).
  - `verification/llm.py:169-171` (`call` routes "the agents' JSON purposes"; every `call` purpose is a `.trace`).
  - `.env.example:12-16` (`REDIS_URL` as "LangGraph checkpointer + agent comms"; `LOG_LEVEL`, which nothing reads).
  - `pyproject.toml:11-12` (`anthropic>=0.40`, and "native tool-use for structured outputs": the code uses `messages.parse(output_format=)`, which 0.40 does not have).
  - `docs/benchmarks/v1.md:11-14`, `:35`.
  - The `cli.py:23-34` exit statuses.
  - ADR-0004, ADR-0005 and ADR-0006.

---

## Earlier fixes re-checked

| Item | Claimed | Holds? |
|---|---|---|
| AUDIT-3 #1 cap from configured thresholds | SG7 | Yes. `build_graph` passes `cfg.pass_threshold`/`retry_threshold` to `Verifier` and `VerificationMonitor` (`graph.py:84-105`). `blocking_reason` is checked first in `decide` (`verification_monitor.py:46`) and gates `BaseAgent` through the same monitor (`base.py:91-92`). |
| AUDIT-3 #2 permanent errors escalate | SG7/SG8 | Partly, as before. Agent nodes catch `ValueError`, `RuntimeError` and non-transient `APIError` (`recovery.py:151-153`). AUDIT-7 #4 still xfails: a refusal or `max_tokens` reply makes the SDK's `parse_text` raise `ValidationError` (`anthropic/lib/_parse/_response.py:16-19`), not `StructuredOutputError`. The `hitl` node's policy re-check is still unwrapped (`checkpoints.py:151-153`). |
| AUDIT-3 #3 code-owned ids, `(run_id, id)` keys | SG9 | Yes. `content_schema` (`common.py:26-37`) drops server-owned fields; the store upserts by `(run_id, id)` (`store.py:127-145`). `Hypothesis.confidence` is still self-reported. |
| AUDIT-3 #4 529 retried | SG8 | Yes. `is_transient` (`recovery.py:34-44`) matches SDK 0.102's `_should_retry` (`anthropic/_base_client.py:804-837`): `x-should-retry`, 408, 409, 429, ≥500. |
| AUDIT-3 #5 `anthropic>=0.40`, Pattern E ADR | none | Open. `pyproject.toml:11-12` unchanged; no ADR supersedes Pattern E. The call shape `messages.parse(model, max_tokens, system, messages, output_format=schema)` (`llm.py:236-242`) still matches `AsyncMessages.parse` in 0.102 (`messages.py:2602-2700`), and `parsed_output` is the first text block's parsed value (`types/parsed_message.py:51-56`). |
| AUDIT-3 #6 dead `Settings` fields, deps, `Checkpointer`/Redis | none | Open. `redis_url`, `log_level`, `step_budget_ms` (`config.py:39-43`, `:50`) have no reader; `structlog`, `fastapi`, `uvicorn`, `pyyaml`, `alembic` have no importer in `src/`. The xfail still fails. |
| AUDIT-3 #7 stale README, architecture, ADRs | none | Open. See #8. |
| AUDIT-3 #8 Research ignores literature tools | none | Open. Nothing in `src/` imports `tools/literature.py`. `build_hypothesis_prompt` asks for "at least one Citation" (`research.py:58-63`), and `HYPOTHESIS_REPLY` accepts none. |
| AUDIT-3 #9 `is_live` behind wrappers | SG8 | Yes, as documented (`llm.py:152-160`; `RetryingLLMClient._inner`, `recovery.py:105`). |
| AUDIT-3 #10 schema constraints after decoding | none | Open. AUDIT-7 #5 still xfails. |
| AUDIT-3 #11 one retry layer | SG8 | Yes. `AsyncAnthropic(api_key=..., max_retries=0)` (`llm.py:200`). |
| AUDIT-3 #12 model id | SG8 (partial) | Partly. `DEFAULT_MODEL = "claude-sonnet-4-6"` (`llm.py:28`) is in SDK 0.102's `ModelParam` (`types/model_param.py:15`) and not in `DEPRECATED_MODELS`. There is no `Settings` field, and CLAUDE.md §3 still names Opus 4.7 for the orchestrator (which makes no LLM call). |
| AUDIT-7 #1 resume after a crash | SG13 | Yes. The repro passes. AUDIT-11 #5 (a failure before the first checkpoint) still xfails. |
| AUDIT-11 #1 approved Analysis report persisted | SG18 | Yes. `_hitl_observer` records the decision and `record_node`s the merged update (`graph.py:195-244`); the repro passes. The dashboard still drops the event's `agent` and `note` (AUDIT-15 #6 xfails). |
| AUDIT-11 #3 abort reported as complete | SG18 | Yes for a paused run (`cli.py:151-153`). A later `resume` still says "finished or unknown" (`cli.py:135-139`). |
| AUDIT-11 #6 injective parameter symbols | SG17/SG21 | Yes. `_param_symbol` keeps `param_<name>` only for ASCII `[A-Za-z0-9_]+` and hashes anything else (`common.py:252-255`, `:270-273`). Two parameters with the identical name still share a symbol. |
| AUDIT-11 #7 `hitl_node` removed | SG18 | Yes in code (`checkpoints.__all__`, `checkpoints.py:233-242`; `make_hitl_node` requires `policies`, `:101-103`). Still named in `docs/status.md:142`. |
| AUDIT-15 #1 Unicode fact symbols | SG21 | Yes. Both repros pass. `_symbol` and `_param_symbol` emit plain ASCII identifiers. But the result claim still uses the raw name (#6). |
| AUDIT-15 #2 formalize prompt states SG15's rules | SG20 | Yes. The repro passes, and `prompt_for` (`formalizer.py:176-183`) lists each rejection `formalize` applies (identifier, quoting, builtins bare and applied, declaration heads, single term, `!`/`:keyword`, `|`, `;`). One rule cannot be followed from the prompt (#3). `SYSTEM_PROMPTS["formalize"]` (`llm.py:102-112`) does not contradict it. |
| AUDIT-15 #3 Svedberg units; retry before escalating | SG21 | Half. `16S`/`70S`/`30S` pass (repro green). The retry half is not done and had no repro. It is now #2. |
| AUDIT-15 #4 figures in string parameters | SG21 | Yes. `design_figures` scans every parameter's name and `value unit` (`common.py:216-225`). The repro passes. It also rejects every time-valued parameter (#1). |
| AUDIT-15 #5 `(assert…`-prefixed premise dropped | SG22 | Yes. `_wrap_assert` always wraps (`solver.py:214-215`); the repro passes. |

## What held up

- **SDK call shape.** `ClaudeLLMClient.call_structured` and the `.trace` routing of `call` (`llm.py:203-248`) match `AsyncMessages.parse` in 0.102: the `output_format` type is turned into `output_config.format` with `transform_schema`, and `parsed_output` is validated with a `TypeAdapter`. Every reply schema (`HYPOTHESIS_REPLY`, `DESIGN_REPLY`, `REPORT_REPLY`, `_TraceReply`, `FormalizedStep`, `FaithfulnessVerdict`) is unchanged since AUDIT-15. Every `call` purpose in `src/` ends in `.trace`.
- **Formalize prompts against SG20.** Apart from #3, each rule `formalize` and `Vocabulary.merged` enforce is stated in `prompt_for`. The system prompt's "never re-declare a symbol from the existing vocabulary" is stricter than the code, which accepts an identical re-declaration, so it never leads the model into a rejection.
- **SG21 binding.** `design_claim` and `design_facts` agree on symbols and exact decimals for ASCII, Unicode and look-alike names. The Design trace prompt states the bound claim verbatim.
- **SG22 config.** `SOLVER_WORKERS = min(2, cpu)` and `SOLVER_SLOW_WORKERS = min(32, cpu + 4)` (`solver.py:44-47`) are the only definitions. `SolverExecutor`, `default_executor` and the tests read them, and PLAN.md SG22 states the same values. No doc claims they are configurable through the environment.
- **SG23 store.** The engine is created with `connect_args={"timeout": 30.0}`, and a file database reports `journal_mode=wal` and `busy_timeout=30000` (checked by running it). `:memory:` is left alone, as ADR-0003's test URL needs.
- **CLI, checkpointer, store and models.** The CLI states (complete, paused, aborted, resume without an action, crash resume, finished or unknown, no key) still match `cli.py` and its tests. `CHECKPOINT_TYPES` covers every top-level model that `AutoLabState` and `hitl_payload` hold. The store and tracer agree on `(run_id, id)` keys and `trace_id` for verdicts.

## Gate

- `make lint typecheck`: ruff clean (134 files formatted), `mypy --strict` clean (124 source files).
- `uv run pytest tests/audit/test_audit_19.py -q`: 18 xfailed. With `--runxfail`, all 18 fail on their stated assertion (run 3 times).
- `uv run pytest -m "not perf"`: 1000 passed, 12 skipped, 28 deselected, 43 xfailed. The base's 27 xfails gain the 18 here (45 in the full suite).
- **`make test` was not green on this machine, and the failures are not in this audit's files.** Other agents' suites kept the 4-CPU host at a load average of 5 to 13. Over four full runs, 1 to 3 `perf` tests failed each time, from this set:
  - `test_audit_14.py::test_cancelling_a_check_frees_its_worker_promptly` (freed after 0.95 s, bound 0.5 s)
  - `test_audit_14.py::test_hard_runs_do_not_starve_an_easy_verify`
  - `test_solver_executor.py::test_hard_runs_do_not_starve_an_easy_verify_on_one_worker`
  - `test_solver_executor.py::test_shutdown_interrupts_running_checks` (9.99 s, bound 0.5 s)
  - `test_solver_executor.py::test_judges_overlap_but_formalization_stays_ordered`

  Run on their own at load 7 to 13, the two starvation tests failed 3 of 3: the easy verify took 0.55–0.99 s on 2 workers and 10.1–10.6 s on 1 worker (bound 0.5 s). The shutdown test failed 1 of 9 runs on its own. The 10 s figures are the hard checks' full duration, not a slowdown. So SG22's fairness guarantee (AUDIT-14 #2) and SG23's "green while another suite runs alongside" do not hold at this load, and the shutdown result also looks like a race in which a check is submitted after `shutdown()` has sent its interrupts. These belong to the performance audit and are not counted as findings here.
