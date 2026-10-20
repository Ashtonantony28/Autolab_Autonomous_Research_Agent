# AUDIT-15: API and SDK consistency, round 4

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg19-solver-pool` @ `fee50eb` (SG19 with SG15–SG18 merged). Branch `claude/audit-15`.
- **Installed (from `.venv`):** anthropic 0.102.0, pydantic 2.13.4, langgraph 1.2.0, langgraph-checkpoint 4.3.0, langgraph-checkpoint-sqlite 3.1.2, z3-solver 4.16.0.0.
- **Repros:** `tests/audit/test_audit_15.py`, 10 strict-xfail tests for findings #1–#7. Each asserts the correct behaviour. Under `--runxfail` all 10 fail on their stated assertion. #8 is docs-only and cites `file:line`.
- **Gate:** lint and `mypy --strict` clean; 913 passed, 12 skipped, 36 xfailed (details and two pre-existing load flakes at the end of this file).

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | High | Code-built fact symbols break SG15's identifier rule: a design with a `µL` parameter (or a measurement named `OD₆₀₀`) can never be verified | 2 tests |
| 2 | Medium | The formalize prompts do not tell the model what SG15 now rejects | test |
| 3 | Medium | `protocol_figures` reads Svedberg units (`16S rRNA`, `70S ribosome`) as durations, and the escalation is never auto-retried | test (×3) |
| 4 | Medium | A cost or duration in a string parameter reaches the bound claim unchecked, so SG17's rule is bypassed through the schema | test |
| 5 | Medium | A premise whose term starts with `(assert…` is silently dropped by the solver, so a valid step is refuted | test |
| 6 | Low | The dashboard's event table drops the `agent` and `note` that SG18 added to `hitl` events | test |
| 7 | Low | No entry point builds `Metrics`, so SG18's `hitl_decisions` (and every metric) is unreachable outside tests | test |
| 8 | Low | Stale docs, docstrings and audit headers, including `hitl_node` and new ones from SG15–SG19 | citations |

**Count:** 0 critical, 1 high, 4 medium, 3 low.

---

## 1. High: code-built fact symbols break SG15's identifier rule

**Impact.**
- SG15 made `Vocabulary.merged` reject any declared name that does not match `[A-Za-z_][A-Za-z0-9_]*` (`verification/formalizer.py:66-67`).
- The code-built facts go through the same check: `Verifier.verify` merges every `context.facts` declaration into the formalizer's vocabulary, and does not catch the error (`verification/vericot.py:121-122`).
- But the fact symbols are built with `re.sub(r"\W", "_", name)`, and Python's `\W` is Unicode-aware. A name made only of Unicode word characters is kept as is:
  - `_param_symbol("volume_µL")` is `param_volume_µL` (`agents/common.py:142-146`). Names with `µ` (`volume_µL`), accented letters (`température`) or any other non-ASCII letter hit this.
  - `_symbol("OD₆₀₀")` is `OD₆₀₀` (`agents/common.py:155-157`): subscript digits are `\w`.
- So for an honest Design reply with a parameter such as `volume_µL = 50`, every `verify` raises `FormalizationError` before a single step is formalized. `escalate_permanent_errors` (`orchestrator/recovery.py:151`) turns it into an `error` HITL request:
  - The request is not retried, and a human `retry` sends the same reply shape back with feedback the model cannot act on ("declared name 'param_volume_µL' is not a plain identifier").
  - `approve` on an `error` request has no payload to merge, so Execute fails next on the missing design.
- The guarantee broken is SG12/SG17's "cost, duration and numeric parameters become code-built `context.facts`" (`agents/design.py:210-216`): the run cannot get past Design at all. The same holds for Execute and Analysis with any `HardwareAdapter` that names a measurement with a non-ASCII character. `LabSimulator`'s names are ASCII today.

**Repro.**
- `test_a_design_with_a_unicode_parameter_name_can_be_verified`: a bound one-step Design trace with a `volume_µL` parameter. `verify` raises `FormalizationError` (asserted as `error is None`).
- `test_measurement_fact_symbols_are_plain_identifiers`: `measurement_facts` for `OD₆₀₀` declares `OD₆₀₀`.

**Root cause.** `agents/common.py:143` and `:156` use `\W` without `re.ASCII`; `_param_symbol` treats a Unicode-word name as already plain (`:144`). SG15 tightened `formalizer.py:66` without updating the code that builds facts.

**Fix.** Sanitize with `re.sub(r"[^A-Za-z0-9_]", "_", name)` (or `re.ASCII`), so a non-ASCII name takes the hashed `paramx_` form; apply the same to `_symbol`, prefixing a non-letter start. Add a test that every `design_facts` / `measurement_facts` declaration merges into an empty `Vocabulary`.

**Resolution (SG21).** `_param_symbol` keeps `param_<name>` only for an ASCII `[A-Za-z0-9_]+` name; any other name becomes `paramx_<ascii>_<sha256[:12]>`. `_symbol` keeps a measurement name only if it is an ASCII identifier starting with a letter and not with `m_`; any other name becomes `m_<ascii>_<sha256[:12]>`, so the two forms cannot meet. Both are plain identifiers and stay injective up to the hash. Tests merge the facts for `volume_µL`, `OD₆₀₀`, `température` and look-alike ASCII names into one `Vocabulary`. Both repros are flipped with their assertions unchanged.

## 2. Medium: the formalize prompts do not state SG15's rules

**Impact.**
- SG15 rejects a declared name that is quoted (`|x|`), not a plain ASCII identifier (`temp-C`, `x.1`, `hot?`, all legal SMT-LIB), or a Z3 builtin. Asking Z3 finds names a model may well pick: `map`, `default`, `union`, `subset`, `complement`, `concat`, `char`, `is`, `sin`, `bv`, `fp`. It also rejects any `|` or `;` in the assertion, even inside a string literal (`formalizer.py:237-238`).
- The system prompt says only "declare-const / declare-fun / declare-sort lines, one per line" and "a single Bool term" (`verification/llm.py:102-112`). `prompt_for` adds nothing (`formalizer.py:141-159`).
- Each violation is an UNGROUNDED/`formalization_failed` step, which blocks the trace (it is the payload conclusion or a step it rests on), and so costs a retry or a human review. The feedback the agent gets is the verdict summary, not the rule.

**Repro.** `test_formalize_prompt_states_the_identifier_and_single_term_rules`.

**Fix.** Add one sentence to `SYSTEM_PROMPTS["formalize"]`: "Name every symbol with a plain identifier `[A-Za-z_][A-Za-z0-9_]*` that is not an SMT-LIB or Z3 builtin (for example not `map`, `union`, `default`); never use `|quoted|` symbols or `;` comments."

**Resolution (SG20).** `prompt_for` now lists what the formalizer rejects: names that are not plain identifiers `[A-Za-z_][A-Za-z0-9_]*`, `|quoted|` symbols, SMT-LIB reserved words and Z3 builtins with or without arguments (with examples), a name an earlier step used, anything but declare-const/declare-fun/declare-sort lines, and in the assertion SMT commands, a second term, `!` annotations, `|` and `;`. `SYSTEM_PROMPTS["formalize"]` is in `verification/llm.py` (an SG0 interface) and is unchanged. The repro passes as a plain test.

## 3. Medium: `protocol_figures` reads Svedberg units as durations

**Impact.**
- `_PROTOCOL_FIGURE` matches `<number> s` case-insensitively with a word boundary (`agents/common.py:101-107`, the duration branch at `:105`). `16S rRNA`, `18S`, `23S`, `30S`/`50S` subunits and `70S`/`80S` ribosomes all match. They are some of the most common numbers in molecular-biology protocols, and none is a duration.
- A Design whose protocol mentions one is withheld and escalated (`agents/design.py:244-261`).
- The request is labelled `trigger="verification"`, but the verdict it carries passes. The monitor retries a `verification` request only when `decide(history[-1])` is RETRY (`orchestrator/routing.py:208-213`), so this one goes straight to a human, against the retry-first default (ADR-0007, SG4 amendment). The SG17 test pins this (`tests/unit/agents/test_design.py:288-310`, confidence 1.0).
- The prompt cannot help: the model is told not to state durations (`design.py:267-270`), not to avoid `16S`.

**Repro.** `test_svedberg_units_are_not_protocol_figures[16S|70S|30S]`.

**Fix.** Require whitespace or a longer unit for seconds (`\d+\s+s\b`, `sec`, `secs`, `seconds`) and keep the case-sensitive `S` out, or exclude `\d+S\b` explicitly. Give the figures rejection its own trigger (`policy` or `figures`) or route it to a retry with the reason as feedback, since the model can fix it.

**Resolution (SG21).** Single-letter and two-letter units match in their own case only, so `16S`, `70S`, `30S` (and `3D`, `5 M`, `MS`) are not durations; the repro is flipped unchanged. Not done: the retry half. The monitor retries a `verification` request only when `decide(history[-1])` is RETRY (`orchestrator/routing.py`), and the verdict of a design rejected for figures passes. Retrying it needs a change in `orchestrator/` (for example, retry a `figures` trigger with the request reason as feedback), which SG21 may not touch. It still escalates to a human.

## 4. Medium: a string parameter carries a cost or duration into the bound claim

**Impact.**
- SG17's rule is that cost and duration figures live only in the bound numeric fields (`PLAN.md:309`, `design.py:267-270`). The check reads only `design.protocol` (`design.py:250`).
- `DESIGN_REPLY` allows `ExperimentParameter.value: str` and a free `unit` (`state/models.py:62-70`). `design_claim` writes a string value verbatim into the bound claim (`common.py:94-96`), with no fact behind it (`common.py:130-134`).
- So `reagent_budget = "$500"` next to `estimated_cost_usd = 5` is admitted with no pause: the cost trigger sees $5, and a human reading the design sees $500. This is the AUDIT-12 #1 scenario moved from the protocol into a parameter.

**Repro.** `test_a_cost_stated_in_a_string_parameter_is_not_admitted`: the Design agent returns the design with `hitl_pending is None`.

**Fix.** Run `protocol_figures` over every free-text field that reaches `design_claim` (string parameter values and units), or over the whole claim minus the code-built `name = value` parts. Mention parameters in `PROTOCOL_FIGURES_RULE`.

**Resolution (SG21).** `design_figures` runs `protocol_figures` over the protocol and over every parameter's name and `value unit` text, numeric values included. `DesignAgent` rejects a match in either and names it in the request (`parameters state cost or duration figures [...]`). `PROTOCOL_FIGURES_RULE` now names parameters. The repro is flipped with its assertions unchanged.

## 5. Medium: a premise whose term starts with `(assert…` is dropped

**Impact.**
- The formalize contract is "`assertion` is a single Bool term without the surrounding `(assert ...)`" (`llm.py:105-106`). SG15's `_is_single_term` refuses an `(assert …)` head (`formalizer.py:251`), so a formalized assertion never carries one.
- `solver._wrap_assert` still skips the wrapping when the body merely *starts with* the text `(assert` (`verification/solver.py:197-201`). A legal term such as `(assertive 1)` or `(asserted x)` is emitted as a bare top-level form.
- Z3 does not raise on it: it prints `; assertive line: 2 position: 12` and ignores the form. The premise silently leaves the check. The step that rests on it becomes INVALID/`not_entailed` with a counter-model, which is a false refutation that blocks the trace, and a premise that contradicts the dropped one is no longer caught as `premises_inconsistent`.

**Repro.** `test_a_premise_whose_predicate_starts_with_assert_is_kept`: `(assertive 1)` does not entail `(assertive 1)`.

**Fix.** Always wrap: `_wrap_assert` should return `f"(assert {body})"`. The `(assert` branch is dead for every formalized step since SG15, and `vericot` never passes one.

## 6. Low: the dashboard drops the `hitl` event's agent and note

**Impact.** SG18 added `TraceEvent.agent` and `TraceEvent.note` so a human decision can be audited (`monitoring/events.py:37-40`), and the store persists them. `EVENT_COLUMNS` and `render_event_table` show neither (`monitoring/dashboard.py:21`, `:45-61`). On the dashboard a `hitl` row reads `hitl / approve / policy / <reason>`: it does not say whose request was approved or what the human wrote. PLAN.md SG18 lists this as open (`PLAN.md:318`). The docstring "one row per agent execution" (`dashboard.py:46`) is also stale.

**Repro.** `test_dashboard_event_table_shows_the_hitl_agent_and_note`.

**Fix.** Add `agent` and `note` columns (blank for agent events) and update the docstring.

## 7. Low: no entry point builds `Metrics`

**Impact.** `build_graph(metrics=)` and SG18's `Metrics.observe_hitl` / `hitl_decisions` are wired, but nothing outside the tests constructs a `Metrics`. The CLI passes only a tracer (`cli.py:115`), and `scripts/` never passes one. CLAUDE.md Phase 6 asks for "per-agent latency, verification pass rate, HITL escalation rate, cost" (`CLAUDE.md:228`). None of these is produced by any run, and `hitl_decisions` is also kept out of `snapshot()` (`monitoring/metrics.py:62-73`), so even a library caller has to read the field directly.

**Repro.** `test_the_cli_collects_metrics`: `cli._drive` calls `build_graph` with no `metrics`.

**Fix.** Either have the CLI build a `Metrics` and print or persist its snapshot (including `hitl_decisions`) at the end of a run, or drop the parameter and record the gap in PLAN.md.

## 8. Low: stale docs and docstrings

Each item gives the claim first, then the code it contradicts.

- **`hitl_node` is still named.** `docs/status.md:142` says `checkpoints.py` provides "`hitl_node` using LangGraph's `interrupt()`". It was removed in SG18; the graph uses `make_hitl_node(policies, *, observer=None)` (`orchestrator/checkpoints.py:101-133`). That is the only remaining mention outside the audit reports and `PLAN.md`'s history. Also, `tests/audit/test_audit_11.py:249` still says "Today: `checkpoints.hitl_node` ... is exported", and `:108`, `:168` describe the pre-fix behaviour of the now-plain #1 and #3 tests.
- **Audit file headers.** `tests/audit/test_audit_11.py:3-4` says every test is a strict xfail; #1, #3, #6 and #7 are plain now. `test_audit_3.py:3-4` and `test_audit_7.py:3-5` have the same stale header (AUDIT-7 #8, AUDIT-11 #8).
- **SG19 vs ADR-0007.** `docs/decisions/0007-…md:27` says "the premises are added and checked, then the negated claim is added and checked again". Since SG19 the negated claim is asserted as `(= claim! (not C))` and checked as an assumption (`solver.py:144`, `:192-193`). The interrupted outcome is reported as `reason="timeout"`, `details="interrupted"` (`solver.py:163`); `StepReason` has no `interrupted` (`state/models.py:153-161`), though PLAN.md SG19 says "`unknown` after a request is `interrupted`" (`PLAN.md:328`).
- **No ADR for SG15–SG19's choices.** CLAUDE.md §10 asks for one per non-obvious choice. Rejecting all protocol figures instead of parsing them (SG17), the `min(2, cpu)` pool (SG19, `solver.py:42`), and `reset_params()` clearing a host's global Z3 parameters (`solver.py:102`) are recorded only in PLAN.md.
- **Pool size docs.** Nothing still states the old `min(32, cpu + 4)` default as current. `SOLVER_WORKERS` (`solver.py:42`) and its docstring agree. The old figure appears only in PLAN.md's SG10 as-built history (`PLAN.md:209`) and in `scripts/audit_load.py:50-52`, which measures asyncio's pool, not ours. PLAN.md's SG14 Open "the starvation test assumes `SOLVER_WORKERS` equals asyncio's default pool size" (`PLAN.md:280`) no longer holds, since SG19 pins the test's pool to 8 workers.
- **CLI docstring.** `cli.py:23-34` documents exit 2 for a missing key and for a finished or unknown run. It does not say that a pause and SG18's abort both print a message and exit 0 (`cli.py:144-153`), so a script cannot tell "paused", "aborted" and "complete" apart by exit status.
- **`docs/benchmarks/v1.md:11-14`, `:35`** report a 10/10 catch rate on 20 fixtures and "runs to completion ... all clearing the HITL threshold". The current figures are 34/34 on 60 traces, and each scenario pauses once on its policy trigger (PLAN.md SG12, SG17).
- **`PLAN.md:340-347`** (the Audits table) stops at round 3; round 4 (AUDIT-13..16) is not listed.
- **Still open from earlier rounds**, unchanged: `README.md:7`, `:20-21`; `docs/architecture.md:3`, `:28`; `orchestrator/checkpoints.py:16-18` (the `Checkpointer` as the "*external* checkpoint"); `verification/llm.py:3-5` (the grounder calls an LLM), `:169-171` (`call` routes agent JSON purposes); `pyproject.toml:11-12`; `.env.example` (`REDIS_URL` as the checkpointer, `LOG_LEVEL`); ADR-0004/0005/0006.

---

## Earlier fixes re-checked

| Item | Claimed | Holds? |
|---|---|---|
| AUDIT-3 #1 cap from configured thresholds | SG7 | Yes. `build_graph` passes `cfg.pass_threshold`/`retry_threshold` to `Verifier` and `VerificationMonitor` (`graph.py:84-105`); `blocking_reason` is checked first in `decide` (`verification_monitor.py:168`) and in `BaseAgent` (`base.py:91-92`). |
| AUDIT-3 #2 permanent errors escalate | SG7/SG8 | Partly. Agent nodes catch `ValueError`, `RuntimeError` and non-transient `APIError` (`recovery.py:151`). AUDIT-7 #4 (`max_tokens`/refusal raise `ValidationError`) still xfails. The `hitl` node's policy re-check is still unwrapped. #1 above is a new permanent error that escalates with nothing to approve. |
| AUDIT-3 #3 code-owned ids | SG9 | Yes. `content_schema` (`common.py:25-45`) still strips ids; agents build entities in code. `Hypothesis.confidence` is still self-reported. |
| AUDIT-3 #4 529 retried | SG8 | Yes. `is_transient` (`recovery.py:34-44`) is unchanged and matches SDK 0.102's `_should_retry`. |
| AUDIT-3 #5 `anthropic>=0.40` floor, Pattern E ADR | none | Open. `pyproject.toml:11-12` unchanged; no ADR supersedes Pattern E. The `messages.parse(output_format=)` call (`llm.py:235-241`) still matches the installed SDK. |
| AUDIT-3 #6 dead `Settings` fields, deps, `Checkpointer`/Redis | none | Open. `config.py:39-43`, `:50`; `structlog`, `fastapi`, `uvicorn`, `pyyaml`, `alembic` still unimported. The xfail still fails. |
| AUDIT-3 #7 stale README, architecture, ADRs | none | Open. See #8. |
| AUDIT-3 #8 Research ignores literature tools | none | Open. Nothing in `src/` imports `tools/literature.py`. |
| AUDIT-3 #9 `is_live` behind wrappers | SG8 | Yes, as documented (`llm.py:152-160`). |
| AUDIT-3 #10 schema constraints after decoding | none | Open. AUDIT-7 #5 still xfails; a `ValidationError` is an `error` escalation. |
| AUDIT-3 #11 one retry layer | SG8 | Yes. `AsyncAnthropic(max_retries=0)` (`llm.py:200`). |
| AUDIT-3 #12 model id | SG8 (partial) | Partly. `DEFAULT_MODEL` (`llm.py:28`); no `Settings` field; CLAUDE.md §3 still names Opus for the orchestrator. |
| AUDIT-7 #1 resume after a crash | SG13 | Yes. The repro and `tests/unit/test_cli.py` pass. AUDIT-11 #5 (failure before the first checkpoint) still xfails. |
| AUDIT-11 #1 approved Analysis report persisted | SG18 | Yes. The `hitl` observer writes a `hitl` event and `record_state`s the merged update (`graph.py:195-244`); the repro passes. The dashboard does not show the event's agent or note (#6). |
| AUDIT-11 #3 abort reported as complete | SG18 | Yes for a paused run: `Run <id> aborted at human review; it will not resume.`, exit 0 (`cli.py:151-153`). A later `resume` still says "finished or unknown" (`cli.py:135-139`), and the exit status does not distinguish abort (#8). |
| AUDIT-11 #6 injective parameter symbols | SG17 | Yes for ASCII names (`common.py:142-146`). Not for Unicode-word names, which stay un-hashed and are then rejected by SG15 (#1). |
| AUDIT-11 #7 `hitl_node` removed | SG18 | Yes in code: `checkpoints.__all__` (`checkpoints.py:233-242`) has no `hitl_node`, and `make_hitl_node` requires `policies`. Still named in `docs/status.md:142` and a stale comment in `test_audit_11.py:249` (#8). |

## What held up

- **SDK call shape.** `messages.parse(model, max_tokens, system, messages, output_format=<BaseModel>)` still matches `AsyncMessages.parse` in 0.102, and every reply schema (`HypothesisReply`, `ExperimentDesignReply`, `AnalysisReportReply`, `_TraceReply`, `FormalizedStep`, `FaithfulnessVerdict`) is unchanged since AUDIT-11.
- **`FormalizedStep` against SG15.** The schema (`declarations: str`, `assertion: str` with `min_length=1`) is what the formalizer validates. The single-term check runs before the vocabulary merge and the parse, and the system prompt's "single Bool term without the surrounding (assert ...)" matches it (apart from #2 and #5).
- **SG18 interfaces.** `make_hitl_node(policies, *, observer=None)`, `HITLOutcome`, `HITLObserver`, `NodeDecision` (`approve`/`abort`), `TraceEvent.agent`/`note`, `Metrics.observe_hitl`/`hitl_decisions`, the store (`TraceEvent` round-trips with the new fields; pre-SG18 rows load with defaults) and the CLI abort message agree with PLAN.md SG18. `hitl_node` is gone from `src/` and the tests.
- **SG19 pool size.** `SOLVER_WORKERS = min(2, cpu)` is the single source; `SolverExecutor`, `default_executor` and the tests read it; no config or doc states the old size as current.
- **SG17 prompt.** `build_design_prompt` carries `PROTOCOL_FIGURES_RULE`, and the rejection reason names the figures found.
- **CLI states.** Run complete (0), paused (0, prints the resume command), resume paused without an action (2), resume paused with `abort` (0, "aborted"), resume crashed (continues; a given action is ignored with a note), resume finished or unknown (2), no API key (2). Each matches `cli.py` and its tests.

## Gate

- With this audit, `make lint typecheck test`: lint and `mypy --strict` are clean (120 source files), and the suite gives 913 passed, 12 skipped, 36 xfailed (the base's 26 xfails plus these 10).
- `uv run pytest tests/audit/test_audit_15.py -q` gives 10 xfailed; with `--runxfail`, all 10 fail on their stated assertion.
- **Flaky tests (not findings of this audit):** in 5 full-suite runs while other agents' suites kept the load average at about 7, two runs had one failure each, in tests this audit does not touch: `tests/unit/verification/test_solver_executor.py::test_default_pool_stays_responsive_under_fifty_slow_checks` (a load-sensitive `perf` test that SG14 already reported failing once under contention; it passes alone) and `tests/integration/monitoring/test_graph_observability.py::test_concurrent_traced_runs_share_one_store[30]` (the `database is locked` flake AUDIT-11 reported). The other three runs were green.
