# AUDIT-17: Encoding soundness, round 5 (false negatives)

Auditor: an independent agent that wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg22-solver-fairness` at `603e452` (SG20–SG23 merged).
Scope: `src/autolab/verification/*`, `agents/*`, `orchestrator/*`.

Every finding has a repro in `tests/audit/test_audit_17.py`. Each test asserts the correct
behaviour and is marked `xfail(strict=True, reason="AUDIT-17 #n")`. Under `--runxfail`
each one fails on its stated assertion, 10 of 10 runs (16 failing cases, plus one plain
control test that passes). All repros run on `FakeLLMClient`. None calls a real LLM.

"Admitted" means `VerificationMonitor().decide(verdict)` returns PASS and, for the Design
agent, that its update merges `current_experiment` with no `hitl_pending`. The repros use
the default graph's cost trigger (`cost_trigger(10, 60)`). Two limits are accepted and
are not reported: premises are unverified assumptions, and faithfulness is off for
clients that are not live.

Summary: 0 critical, 2 high, 2 medium, 2 low.

No wrong trace is admitted through the verifier itself. The formalizer, solver and
payload-binding fixes all held under the attacks below (see "Probed and held up"). Both
high findings are in the Design agent's figure guard. That guard is the only thing that
connects a design's text to the fields the principle-7b pause reads.

---

## High

### #1 A numeric parameter whose name carries the unit (`incubation_hours = 48`) skips the cost/duration pause

**Impact.** SG21 made `design_figures` scan each parameter's name, and its value with its
unit, as two separate texts. A parameter such as `{"name": "incubation_hours", "value": 48}`
has no figure in either text. `incubation_hours` has no number, and `"48 "` has no unit.
The same holds for `reagent_cost_usd = 500`, `hours = 48` and `cost (USD) = 500`. This is
the normal way a model or an engineer names a numeric field, so no unusual model output
is needed.

The repro design has `estimated_cost_usd = 5` and `estimated_duration_minutes = 30`. Its
one parameter says the run takes 48 h, or costs $500. The trace is honest and proves the
bound claim:
- premise: the protocol;
- conclusion: `design_claim`, formalized as the premise and every code-built fact,
  including `(= param_incubation_hours 48.0)`.

The trace verifies at 1.0. The cost trigger reads only the two fields and passes. The
figure guard finds nothing, so the design is merged and principle 7b never fires. AUDIT-15
#4 reported this for a string value (`"$500"`). Here the parameter is numeric, and its
fact is even bound into the verified claim.

**Repro.** `test_parameter_named_for_its_unit_cannot_hide_cost_or_duration` (4 cases). It
first asserts that confidence is 1.0, then that a pause is set (it fails here).

**Root cause.** `src/autolab/agents/common.py:224`: `texts` is
`(p.name, f"{p.value} {p.unit or ''}")`, two strings scanned on their own. The unit
inside the name is never next to the value. `agents/design.py:67` trusts
`design_figures` as the only link between parameter text and the cost/duration fields.

**Suggested fix.** Scan `f"{p.value} {p.unit or ''} {p.name}"` and
`f"{p.name} {p.value} {p.unit or ''}"`, with `_` split as the scanner already does. Better,
refuse any numeric parameter whose name or unit contains a time or currency word, with
no number needed. Such a parameter is a cost or a duration by name. Fail closed.

### #2 `protocol_figures` still misses ordinary spellings of a duration or a cost

**Impact.** This is the AUDIT-12 #1 / AUDIT-13 #3 attack again. Each protocol below is
admitted on a $5 / 30 min design whose honest trace verifies at 1.0, with no pause:
- **A unit outside the lists.** "for 3 nights". `overnight`, `shift`, `semester`, `BTC`,
  `credits` and non-English units (`Stunden`, `heures`, `horas`, `jours`) are also missed.
- **An article as the amount.** "for a week", "a thousand dollars", "a million dollars".
  Only number words count as amounts, and `thousand`/`million` count only after one.
- **A unit three words after the number.** "for 72 more full hours". `_words_after`
  collects up to `_WINDOW` = 3 words, but only `words[0]` and `words[1]` are checked.
- **Punctuation between amount and unit.** "500 per well (USD)", "240 (minutes)",
  "48/hours", "48. hours", "4 [h]". Any `other` token ends the look-ahead.
- **Clock notation.** "run time 04:00:00 (hh:mm:ss)".
- **A currency or time word before a spelled-out number.** "USD five hundred",
  "hours: four". The word-first branch accepts only digits.

**Repro.** `test_protocol_figures_cannot_hide_cost_or_duration` (6 cases, one per form
above except the last).

**Root cause.** `src/autolab/agents/common.py`:
- `:122-127`: the unit lists are closed.
- `:160-163`: `_is_amount` accepts only digits and number words.
- `:190-196`: `words[2]` is never examined.
- `:197-202`: the word-first branch needs a `num` token.
- `:206-213`: `_words_after` stops at any token that is not a word or a separator.

**Suggested fix.** A deny-list has now failed three rounds in a row (AUDIT-12 #1,
AUDIT-13 #3, here). Flip it to an allow-list, which fails closed. A protocol may state a
number only when the number is followed by a unit from an explicit lab-unit list (`mL`,
`µL`, `°C`, `rpm`, `nm`, `mM`, `g`, `x g`, `%`, `OD`, …), or when it stands alone as a
count. Anything else goes to review. Until then:
- check every collected word;
- treat `a`/`an` before a time or currency word as an amount;
- let `(`, `[`, `/` and `.` sit between an amount and its unit;
- flag `\d+:\d\d`.

---

## Medium

### #3 `success_criteria` can state the run's duration or cost unseen

**Impact.** `success_criteria` is not part of `design_claim` and is not scanned by
`design_figures`. A design whose criteria say "Cells reach confluence after a 48-hour
incubation" is admitted at 1.0 with a 30-minute field and no pause. A human reviewing the
merged design reads 48 h; the trigger read 30 min. SG21 lists this as open. It is Medium
rather than High because no code reads `success_criteria`. The simulator routes on
`protocol` only.

**Repro.** `test_success_criteria_cannot_state_cost_or_duration`.

**Root cause.** `src/autolab/agents/common.py:89-98` (`design_claim` omits it) and
`:216-225` (`design_figures` omits it).

**Suggested fix.** Scan each criterion with the same guard. Optionally, also add the
criteria to `design_claim`, so the trace has to conclude them.

### #6 SG22's slot hand-off does not start a thread once the pool has done earlier work (AUDIT-14 #2 again; outside the soundness focus)

**Impact.** A check still in Z3 after `_SLICE_S` gives up its slot and raises
`ThreadPoolExecutor._max_workers` by one, so that the next `submit` starts a thread.
CPython starts a thread only if `_idle_semaphore.acquire(timeout=0)` fails. That semaphore
gains one permit per completed work item, not per idle thread
(`concurrent/futures/thread.py:90`, `:182`). A pool that has already done work can
therefore hold surplus permits. The next caller's check is then queued behind the slow
check and does not run. In the repro, an empty call waits 2.70 s, 10 of 10 runs, for a
3 s hard check to finish. With the idle semaphore reset to 0, the same test passes in
2.5 s total.

This makes `make test` red on the base branch in this environment:
- `tests/unit/verification/test_solver_executor.py::test_hard_runs_do_not_starve_an_easy_verify_on_one_worker`
  fails 4 of 4 runs alone (10.0 s against < 0.5 s);
- `tests/audit/test_audit_14.py::test_hard_runs_do_not_starve_an_easy_verify` fails 2 of 3.

Both first run a 10-step verify alone, which leaves surplus permits whenever a submit
races a worker's release. A cold pool passes (0.43 s).

No verdict changes. The starved check eventually runs, and it is never VALID by timing
(see below). The cost is the starvation AUDIT-14 #2 described.

**Repro.** `test_slot_hand_off_starts_a_thread_after_earlier_work`. Two short runs from
two event loops (slots are per loop) put two items through the pool's one thread, which
leaves two permits. A hard check then takes one permit and leaves its slot. The next
call takes the other permit, so no thread is started.

**Root cause.** `src/autolab/verification/solver.py:324-332` (`_to_slow`) raises
`_max_workers` and relies on `ThreadPoolExecutor._adjust_thread_count` to use it.
`solver.py:305` submits through that path.

**Suggested fix.** Do not rely on the private executor's spawning rule. Use either of:
- start a dedicated thread for a check that turns slow, or for the next caller's work;
- keep a pool of `workers + slow_workers` threads and gate admission with your own
  semaphore, which is the slot count you already keep.

---

## Low

### #4 Look-alike letters, non-decimal digits and invisible characters hide a figure

**Impact.** The following all pass `protocol_figures`:
- "48 һours" (Cyrillic `һ`);
- "④ hours" (`④` is category `No`, so `\d` does not match it and it lexes as a word);
- "48­hours" (soft hyphen);
- "48​hours" (zero-width space).

An honest model rarely writes these. They do appear in text pasted from PDFs, and the
guard is meant to fail closed.

**Repro.** `test_protocol_figures_see_through_unicode_spellings` (4 cases).

**Root cause.** `src/autolab/agents/common.py:138-145`. `_NUMERALS` lists only some `No`
digits. The `sep` class does not include `Cf` format characters, and the text is not
normalized.

**Suggested fix.**
- Apply NFKC and drop `Cf` characters before scanning.
- Treat any character with a numeric value (`unicodedata.numeric`) as part of an amount.
- Refuse a protocol that mixes scripts within a word.

### #5 The earlier-use rule's tokenizer is not Z3's lexer

**Impact.** SG20's earlier-use rule says a step may not declare a name that an earlier
step used. It collects those names with `_TERM_TOKEN`, which splits only on whitespace,
parentheses and quotes. Z3 also ends a symbol at `#` and `:`. In
`(let ((k#b1)) (= (bvor k#b0) #b1))`, Z3 reads the binder `k` and the literal `#b1`. The
token set holds only `k#b1` and `k#b0`, so a later `(declare-const k Bool)` is accepted.
With a space before `#b1`, the same declaration is refused, as PLAN.md documents. The
same split hides an applied builtin: `(bvnot#b1)` gives the token `bvnot#b1`.

Nothing is admitted today. A binder is local. Every builtin that was hidden this way
also fails the bare or the applied probe. But the rule exists as the defence for any
builtin the probes miss, and that defence has a gap.

**Repro.** `test_binder_glued_to_a_literal_still_counts_as_used`. Its control,
`test_binder_named_like_a_later_declaration_fails_the_later_step`, passes.

**Root cause.** `src/autolab/verification/formalizer.py:313-318` (`_tokens`,
`_TERM_TOKEN`), used at `:124-133`.

**Suggested fix.** Refuse `#` and `:` in an assertion except at the start of a token, or
split tokens on them as Z3 does. A more robust option is to take the used names from
Z3's own AST of the parsed step: every constant and function symbol, plus the bound
variable names.

---

## Probed and held up

**SG20, the earlier-use rule, the applied probe and the annotation ban.**
- **Bound variables.** A `let`, `forall` or `exists` binder named like an earlier
  declaration shadows it locally. The judge sees the shadowing in the step's own text. A
  binder named like a later declaration fails that step, apart from #5.
- **Overloads and arity.** `Vocabulary.merged` accepts
  `(declare-fun equals (Real) Bool)` and `(declare-fun if_then_else (Bool Real) Real)`.
  Z3 still resolves `(equals a 0.95)` and `(if_then_else p a a)` to the builtins, because
  it binds a user function only at its declared arity (checked in Z3 4.16). So a
  declaration at another arity changes no use. At the builtin's own arity, the applied
  probe refuses every Int/Real combination and user-sort arguments.
- **Builtin sorts.** `Set`, `Char`, `Float32`, `RegEx`, `Unicode` and `List` are refused
  as `declare-sort`. `Bag`, `Option`, `Pair` and `Tuple` are free names.
- **Facts.** Code-built fact assertions use only `=`, `-`, a declared symbol and a
  literal. Their declarations seed the vocabulary before any step, so no step precedes
  them.
- **Declare before use.** A step that declares a name and uses it in the same step sees
  the declaration in its own parse and in the judge prompt. A later declaration cannot
  change it (vocabulary conflict check).
- **Annotations.** `!` must be its own Z3 token, so it needs a delimiter on both sides,
  and `_TERM_TOKEN` splits on the same delimiters. The only characters where Z3 splits
  and the scanner does not are `#` and `:`, and no Bool term starts with either. A
  hidden `!` therefore fails to parse.
- **Lexer gaps.** Unicode whitespace, `\x0b`, `\x0c`, `` ` ``, `'`, `[`, `{`, `\` and
  control characters are all lexer errors in Z3. That is `formalization_failed`, never
  VALID.

**SG21.**
- Fullwidth and Arabic-Indic digits are `\d`, so "５００ minutes" and "٥ hours" are
  caught.
- Case-insensitive long units ("4 HOURS", "500 DOLLARS"), glued forms ("240min",
  "500.00USD") and hyphenated number words ("forty-eight-hour") are caught.
- The fact symbols are ASCII. `paramx_`/`m_` and a sha256 suffix keep them injective.
- Two parameters with one name give inconsistent facts, so every step is INVALID (fail
  closed).

**SG22.**
- The solver returns VALID only when the asserted `claim!` check is `unsat`. A timeout,
  an interrupt, the `_SETUP_SHARE` guard and every `unknown` give UNGROUNDED.
- A fuzz run checked this with timeouts from 10 µs to 50 ms on a satisfiable nonlinear
  pool and on an inconsistent pool (360 checks), plus 200 random cancels through a
  2-worker `SolverExecutor`. Results: 166 UNGROUNDED/timeout, 163
  INVALID/premises_inconsistent, 180 INVALID/not_entailed, 51 cancelled, and 0 VALID.
- `claim!` cannot be declared (it is not an identifier) or used free (the formalizer's
  parse fails). A binder named `claim!` is local.
- A cancel raises out of `verify`, so no partial result is scored.

**Verifier and monitor.**
- The payload binding (casefold and whitespace only) held.
- Pass-2 carry-over (SG16) held.
- The tautology and circularity overrides held. An inconclusive check blocks.
- The score cap held.
- `blocking_reason` is shared by `BaseAgent` and the monitor node.
- `ReasoningTrace` enforces dense indices and backward references.
- The Analysis report enforces dense ranks, so exactly one interpretation is the verified
  top one.

**Orchestrator.**
- A figure rejection is a `verification` request. `approve` re-runs the cost trigger.
- An `error` request merges nothing.
- The monitor retries only a RETRY verdict.
- No path merges a Design or Research payload without its policy.

## Re-check of earlier soundness fixes

All listed repros pass at `603e452`
(`uv run pytest tests/audit/test_audit_{1,5,9,12,13}.py -q`: every listed test passes;
the 4 xfails there are AUDIT-5 #4, #5 and AUDIT-9 #4, which are not soundness fixes).

| Fix | Repro(s) | Status at `603e452` |
|---|---|---|
| AUDIT-1 #1 circular premise | `test_circular_premise_restating_payload_is_not_admitted`, `test_retry_loop_does_not_launder_refuted_claim_into_premise` | Pass. Holds (split premises are an accepted limit). |
| AUDIT-1 #2 declaration smuggling | `test_declarations_cannot_smuggle_assertions[…]` | Pass. Holds. |
| AUDIT-1 #3 UNGROUNDED conclusion | `test_conclusion_that_fails_to_formalize_…`, `…_rejected_by_faithfulness_judge_…`, `…_times_out_…`, `…_with_no_premises_…` | Pass. Holds. |
| AUDIT-1 #4 premise consistency | `test_premise_contradicting_a_measurement_is_not_valid`, `test_mutually_contradictory_premises_are_not_admitted` | Pass. Holds. |
| AUDIT-1 #5 tautological conclusion | `test_tautological_conclusion_is_not_valid[…]` | Pass. Holds. |
| AUDIT-1 #6 contradiction trigger fails closed | `test_contradiction_trigger_fires_when_reformalization_fails`, `…_fails_closed_when_prior_cannot_be_formalized` | Pass. Holds. |
| AUDIT-5 #1 reserved symbols | `test_shadowed_gt_…`, `test_shadowed_lt_…`, `test_shadowed_not_…` | Pass. Holds. |
| AUDIT-5 #2 Design payload binding | `test_design_cost_fields_that_contradict_the_verified_protocol_skip_the_pause` | Pass for its protocol. Bypassed through parameter names (#1), other spellings (#2) and `success_criteria` (#3). |
| AUDIT-5 #3 approve re-runs the policy | `test_approving_a_verification_escalation_still_applies_the_cost_trigger` | Pass. Holds. |
| AUDIT-9 #1 unlisted builtins | `test_shadowed_str_len_…`, `test_vocabulary_rejects_every_z3_builtin[…]` | Pass. Holds. |
| AUDIT-9 #2 commands in `assertion` | `test_assertion_cannot_smuggle_a_builtin_redeclaration`, `test_a_trace_cannot_change_z3_global_parameters` | Pass. Holds. |
| AUDIT-9 #3 pass-2 circularity | `test_premise_inconsistent_in_pass_one_cannot_prove_the_claim_in_pass_two` | Pass. Holds. |
| AUDIT-12 #1 protocol figures | `test_design_concluding_the_bound_claim_cannot_hide_its_protocol_cost` | Pass for its protocol. Other spellings pass the check (#2). |
| AUDIT-12 #3 reserved names pinned | `test_each_core_builtin_is_reserved_by_name[…]` (35), `test_merge_rejects_reserved_symbols` | Pass. Holds. |
| AUDIT-13 #1 `equals`/`equiv`/`if_then_else` | `test_shadowed_equals_…`, `test_vocabulary_rejects_builtins_that_only_exist_with_arguments[…]` | Pass. Holds. Other arities are accepted but change no use. The earlier-use rule has a tokenizer gap (#5). |
| AUDIT-13 #2 `:named` | `test_named_premise_cannot_redefine_false_for_later_steps`, `test_named_prior_hypothesis_cannot_hide_a_contradiction` | Pass. Holds. |
| AUDIT-13 #3 protocol figure spellings | `test_protocol_figures_cannot_hide_cost_or_duration[…]` (6) | Pass for its six forms. Other forms pass the check (#2, #4). |
