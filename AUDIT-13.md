# AUDIT-13: Encoding soundness, round 4 (false negatives)

Auditor: an independent agent that wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg19-solver-pool` at `fee50eb` (SG15–SG19 merged).
Scope: `src/autolab/verification/*`, `agents/*`, `orchestrator/*`.

Every finding has a repro in `tests/audit/test_audit_13.py`. Each test asserts the correct
behaviour and is marked `xfail(strict=True, reason="AUDIT-13 #n")`. Each one fails today
on its stated assertion (checked with `--runxfail`) and will XPASS once the issue is
fixed. All repros run on `FakeLLMClient`. None calls a real LLM.

"Admitted" means `VerificationMonitor().decide(verdict)` returns PASS. That is the gate
that `BaseAgent.__call__` and the graph's `monitor` node both apply. Where a repro turns
faithfulness on, every registered judge verdict is the one an honest judge gives. Two
limits are accepted and are not reported here: premises are unverified assumptions, and
faithfulness is off for clients that are not live.

Summary: 2 critical, 1 high, 0 medium, 0 low.

---

## Critical

### #1 `equals`, `equiv` and `if_then_else` are Z3 builtins that `_is_z3_builtin` does not detect, so they can still be shadowed (AUDIT-9 #1 again)

**Impact.** In Z3's SMT-LIB parser, `equals` and `equiv` are aliases of `=`, and
`if_then_else` is an alias of `ite`. `(assert (equals 1.0 2.0))` parses to `1 == 2` and is
unsat with no declaration. All three exist only with arguments. SG15's probe asks Z3 about the bare name (`(assert (= N N))`)
and about the name as a sort (`(declare-const x N)`). Both answer "unknown constant" /
"unknown sort", so the name counts as free. `(declare-fun equals (Real Real) Bool)` is
accepted, and since every check declares the trace's final vocabulary, every `equals`
in the trace becomes an uninterpreted predicate. This is the AUDIT-5 #1 / AUDIT-9 #1
attack with a new name:
- The facts state absorbance = 0.42.
- Step 0 is the premise "Absorbance is 0.95", encoded `(equals absorbance 0.95)`. When
  it is formalized and judged, `equals` is equality. It contradicts the fact.
- Step 1 is "If absorbance is 0.95 then compound K-17 is active",
  `(=> (equals absorbance 0.95) active)`.
- Step 2, "The plate was read once", also declares `(declare-fun equals (Real Real) Bool)`.
- Step 3 concludes "Compound K-17 is active" from steps 0 and 1.

Every judge verdict is honest. Result: four VALID steps, score 1.0, PASS. Without the
step-2 declaration, step 0 is INVALID/`premises_inconsistent`, the conclusion is
`not_entailed`, and the score is 0.39. The same trick defeats the contradiction trigger
(principle 7c) and the circularity check, as AUDIT-5 #1 showed for `>`, `<` and `not`.

I searched for other names of this kind. I took every identifier-shaped string in
`libz3.so` that `Vocabulary.merged` accepts and parsed it, undeclared, in about 40
argument shapes (Int, Real, Bool, String, Seq, Array, Set, BitVec, FloatingPoint,
RegEx; one to three arguments). See the note below for the result.

**Repro.**
- `test_shadowed_equals_hides_a_premise_that_contradicts_a_measurement`
- `test_vocabulary_rejects_builtins_that_only_exist_with_arguments` (3 cases)

**Root cause.**
- `src/autolab/verification/formalizer.py:205-228`: `_is_z3_builtin` probes only nullary
  and sort uses. A builtin function whose only signatures take arguments reports
  "unknown constant N" for `(= N N)`, the same message as an undeclared name. Z3 also
  reports "unknown constant N (Bool)" when a builtin is applied to the wrong sorts, so
  the message alone cannot tell a builtin from a free name.
- `formalizer.py:179-199`: `RESERVED_SYMBOLS` lists none of the three.
- Still open from AUDIT-5/AUDIT-9 #1 item 3: each check declares the trace's final
  vocabulary, including the declarations of later steps (`vericot.py:125`, `:214`).

**Suggested fix.**
1. Add `equals`, `equiv` and `if_then_else` to `RESERVED_SYMBOLS`, with a pinning test.
2. Close the class rather than the instances. Re-parse every step's assertion *in the
   final vocabulary* and compare the result with the AST from its own formalization
   (or with the formula the judge saw). A later declaration that changes the meaning of
   an earlier step then shows up as a different AST. Alternatively, give each check only
   the declarations that were in force when each formula in it was formalized
   (AUDIT-9 #1 item 3).
3. Optionally, strengthen the probe: a name is a builtin if `(declare-fun N ...)`
   followed by a use of N parses to a different AST from the same use without the
   declaration, over a small set of signatures.

### #2 A `:named` annotation in one step redefines the builtin `false` (and `pi`, `euler`, rounding modes) in every later step

**Impact.** SG15 accepts `(! t :named n)` as a single term, and its tests pin that.
In an SMT-LIB document, `:named` defines `n` for every later command. Z3 lets that
definition shadow some builtin constants. Checked in Z3 4.16:
- `(assert (! p :named false)) (assert false)` parses to `[p, p]`;
- `(! p :named pi)` turns a later `(> pi 3.0)` into `If(p, 1, 0) > 3`;
- `(! p :named RNE)` and `:named sin` are also accepted. `true` and declared symbols are
  not shadowable (a named expression may not reuse a declared name).

The formalizer parses each step on its own, so its parse check and the judge see Z3's
`false`. The checks put the named premise ahead of later assertions in one document
(`solver.py:189-193`), so there `false` means the premise's term, which is asserted and
therefore true. Two repros:
1. **Admission.** Step 0 is the premise "The plate was read", encoded
   `(! plate_read :named false)`; the asserted proposition is `plate_read` and the
   annotation is only a label. Step 1 is the premise "The plate reader was calibrated",
   `calibrated`. Step 2 concludes "Compound K-17 is active" as
   `(or active (and false calibrated))`, which is `active` under Z3's `false`. Every
   judge verdict is honest. Result: [VALID/assumed, VALID/assumed, VALID], score 1.0,
   PASS. With the plain `plate_read` the conclusion is INVALID/`not_entailed` (0.39).
   Neither premise alone entails the conclusion, so the circularity check does not fire.
2. **Principle 7c.** In the contradiction trigger, the prior hypothesis "Compound K-17 is
   active" is formalized `(! active :named false)` and the new one, "Compound K-17 is not
   active", `(= active false)`. Under the shadow the new statement reads `active = active`.
   The trigger returns None: no pause for a hypothesis that contradicts verified state.

The trigger formalizes both statements afresh, so this needs only the formalizer's output,
not the agent's.

**Repro.**
- `test_named_premise_cannot_redefine_false_for_later_steps`
- `test_named_prior_hypothesis_cannot_hide_a_contradiction`

**Root cause.**
- `src/autolab/verification/formalizer.py:231-256`: `_is_single_term` checks for
  commands but not attributes. It accepts `!` with `:named`, and the name after it is
  never checked (`RESERVED_SYMBOLS`, `_IDENTIFIER`, `_is_z3_builtin` apply only to
  `declarations`).
- `formalizer.py:118`: the parse check sees each step alone, so a cross-step effect is
  invisible to it and to the judge.
- `src/autolab/verification/solver.py:189-193`: all premise assertions share one
  document, so a definition from one reaches the next.

**Suggested fix.** Reject `!` (and so every attribute) in `assertion`. A formalized step
has no use for `:named`, `:pattern` or `:weight`; the existing `(! p :named n)`
acceptance test should be flipped. If annotations are wanted later, accept only
`:pattern` inside a quantifier and run the name checks on any symbol that follows a
keyword. The re-parse in #1's fix 2 would catch this as well.

---

## High

### #3 `protocol_figures` misses common spellings of a duration or a cost, so the AUDIT-12 #1 attack still skips the cost/duration pause

**Impact.** SG17 closes AUDIT-12 #1 by refusing any protocol that states a currency
amount or a duration. The recogniser (`_PROTOCOL_FIGURE`) misses these spellings, each
of which is ordinary lab English, not figures in words:
- compound adjectives: "a 240-minute run", "4-hour cycles" (the hyphen is not `\s*`);
- units outside its list: "2 weeks", "3 wk", "3 months", "0.5 d";
- a thousands suffix: "5k USD" (`\d+` must be followed by the unit);
- "500 US dollars", "500$" (trailing symbol), "U.S.D.";
- other currencies: "40000 INR", "500 SEK", "₹40000".

With any of these, the AUDIT-12 #1 trace is admitted unchanged. Its premises formalize
"240 minutes" and "500 USD" under unrelated symbols, the conclusion is
`design_claim(design)` with `estimated_cost_usd = 5.0` and
`estimated_duration_minutes = 30.0`, and the design verifies at 1.0. The cost trigger
(`cost_trigger(10, 60)`) reads only the fields, so the design is merged with no pause.
This bypasses the principle-7b trigger.

**Repro.** `test_protocol_figures_cannot_hide_cost_or_duration` (6 cases).

**Root cause.** `src/autolab/agents/common.py:100-118`: a deny-list regex. `_NUMBER` must
be directly followed by `\s*` and a unit from a short list, and the currency list is
closed. `agents/design.py:65` trusts it as the only guard against figures in the text.

**Suggested fix.** Widen the pattern: allow `[\s-]*` between number and unit, accept a
`k`/`M` suffix, add `weeks?|wks?|months?|d|ms|milliseconds?`, a trailing currency
symbol, `US dollars?`, and any ISO 4217 code or currency symbol (`\p{Sc}` via the
`regex` module, or the Unicode category `Sc` via `unicodedata`). More robust: flip the
rule to an allow-list. Refuse any number in the protocol that is followed (after
optional hyphen or space) by an alphabetic token outside a list of lab units (mL, µL,
°C, rpm, nm, mM, …), and send everything else to review.

---

## Note on the builtin search (#1)

The search described in #1 tested 65 608 identifier-shaped strings from `libz3.so` that
`Vocabulary.merged` accepts. It found exactly three builtins: `equals`, `equiv` and
`if_then_else`. (Two other hits, `ai` and `st`, were the search's own declared helpers.) The search covers only names that appear as strings in
`libz3.so` and only the argument shapes listed; a builtin with another signature would
be missed. That is why the suggested fix compares parses instead of extending the list.

## Probed and held up

- **SG15 identifier rule.** `_IDENTIFIER` is ASCII-only (`[A-Za-z_]`), so `μ`, full-width
  and other Unicode letters and digits are refused. Names of 100 000 characters merge
  and parse; the `lru_cache` is keyed by name and the probe does not depend on context
  state. `|…|` symbols are refused in both fields. Numerals, `#x…` and dotted names fail
  the identifier rule.
- **SG15 single-term rule.** `"…"` with `""` escapes is tokenised the way Z3 does it
  (checked: Z3 reads `"a""b"` as one literal, and `\"` is not an escape in Z3), so no
  string boundary differs between the scanner and Z3. A command head after `(` is
  refused even with whitespace (`( assert x)`). `let` and `forall` bindings are local
  to the term; `match` needs a datatype, and `declare-datatype(s)` is not an accepted
  declaration head. Indexed `(_ …)` terms cannot open a command. A NUL byte truncates
  the C string Z3 reads, but a single term containing NUL fails the formalizer's parse,
  and a truncated check document fails to parse (UNGROUNDED, never VALID).
- **`:named` duplicates.** Two steps naming the same symbol, or a name that is declared
  in the vocabulary, make every check that contains both a parse error
  (`formalization_failed`). Such a premise is UNGROUNDED, never joins the pool, and the
  contradiction trigger fails closed on it. Only shadowing a builtin (#2) is open.
- **SG19 `claim!`.** No declaration can name it (not an identifier). A `:named claim!` in
  a premise or in the conclusion is a parse error ("already defined"), so the check is
  UNGROUNDED/`formalization_failed`, not VALID. `let`/`forall` binders named `claim!`
  are local. The `(= claim! (not C))` definition cannot make the consistency check unsat,
  because `claim!` is fresh and C is closed over declared symbols.
- **SG19 interrupts.** An interrupted check returns `_INTERRUPTED`
  (UNGROUNDED/`timeout`). Z3 reports `unknown` after an interrupt, never `unsat`, so an
  interrupt cannot become VALID; a parse cut short by an interrupt is also
  `_INTERRUPTED`. `z3.reset_params()` runs before each context; nothing in the codebase
  sets global parameters, and `set-option` cannot reach Z3 (AUDIT-9 #2).
- **SG16 carry-over.** A premise that is INVALID or UNGROUNDED in pass 1 keeps its
  verdict in pass 2 and never joins the pool. Inferences and the conclusion are always
  re-checked against the smaller pool, so a conclusion reached through an inference that
  rests on a dropped premise loses its support. A VALID premise kept in pass 2 was
  already checked alone against the conclusion. An inconclusive alone check blocks.
  Splitting an assumption over two premises (`P`, `P => C`) is an accepted limit
  (ADR-0007).
- **SG17 `_param_symbol`.** Plain names map to `param_<name>`, others to
  `paramx_<name>_<sha256[:12]>`; the prefixes differ at the sixth character, so the map
  is injective up to a 48-bit hash collision. A parameter name with non-ASCII word
  characters (`μL`) gives `param_μL`, which the vocabulary refuses; `verify` then raises
  `FormalizationError` (a `ValueError`), which `escalate_permanent_errors` turns into an
  `error` pause (fail closed, a usability issue). Scientific notation such as
  `1.5e2 minutes` or `5e2 USD` is caught, because the exponent digits match. A protocol
  that writes `estimated_cost_usd = 500.0` into the claim text gives a faithful
  conclusion that contradicts the code-built fact (INVALID).
- **SG17 withholding.** A flagged design is withheld in `hitl_payload` as a
  `verification` request, so `approve` re-runs the cost trigger (AUDIT-5 #3).
- **Monitor and score cap.** No blocked verdict reaches PASS with `retry_rework` on or off.

## Re-check of earlier soundness fixes

All listed repros pass at `fee50eb` (`uv run pytest tests/audit/test_audit_1.py
tests/audit/test_audit_5.py tests/audit/test_audit_9.py tests/audit/test_audit_12.py -q`).

| Fix | Repro(s) | Status at `fee50eb` |
|---|---|---|
| AUDIT-1 #1 circular premise | `test_circular_premise_restating_payload_is_not_admitted`, `test_retry_loop_does_not_launder_refuted_claim_into_premise` | Pass. Holds (split premises are an accepted limit). |
| AUDIT-1 #2 declaration smuggling | `test_declarations_cannot_smuggle_assertions[…]` | Pass. Holds. |
| AUDIT-1 #3 UNGROUNDED conclusion | `test_conclusion_that_fails_to_formalize_…`, `…_rejected_by_faithfulness_judge_…`, `…_times_out_…`, `…_with_no_premises_…` | Pass. Holds. |
| AUDIT-1 #4 premise consistency | `test_premise_contradicting_a_measurement_is_not_valid`, `test_mutually_contradictory_premises_are_not_admitted` | Pass. Bypassed by shadowing `equals` (#1). |
| AUDIT-1 #5 tautological conclusion | `test_tautological_conclusion_is_not_valid[…]` | Pass. Holds (the tautology check runs alone, without named premises). |
| AUDIT-1 #6 contradiction trigger fails closed | `test_contradiction_trigger_fires_when_reformalization_fails`, `…_fails_closed_when_prior_cannot_be_formalized` | Pass. Defeated by `:named false` (#2). |
| AUDIT-5 #1 reserved symbols | `test_shadowed_gt_…`, `test_shadowed_lt_…`, `test_shadowed_not_…` | Pass for listed names. `equals`/`equiv`/`if_then_else` open (#1). |
| AUDIT-5 #2 Design payload binding | `test_design_cost_fields_that_contradict_the_verified_protocol_skip_the_pause` | Pass. See AUDIT-12 #1. |
| AUDIT-5 #3 approve re-runs the policy | `test_approving_a_verification_escalation_still_applies_the_cost_trigger` | Pass. Holds. |
| AUDIT-9 #1 unlisted builtins | `test_shadowed_str_len_…`, `test_vocabulary_rejects_every_z3_builtin[…]` | Pass. Builtins with arguments only are missed (#1). |
| AUDIT-9 #2 commands in `assertion` | `test_assertion_cannot_smuggle_a_builtin_redeclaration`, `test_a_trace_cannot_change_z3_global_parameters` | Pass. Commands hold; attributes (`:named`) are open (#2). |
| AUDIT-9 #3 pass-2 circularity | `test_premise_inconsistent_in_pass_one_cannot_prove_the_claim_in_pass_two` | Pass. Holds. |
| AUDIT-12 #1 protocol figures | `test_design_concluding_the_bound_claim_cannot_hide_its_protocol_cost` | Pass for its protocol. Other spellings pass the check (#3). |
| AUDIT-12 #3 reserved names pinned | `test_each_core_builtin_is_reserved_by_name`, `test_merge_rejects_reserved_symbols` | Pass. Holds. |
