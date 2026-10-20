# AUDIT-9: Encoding soundness, round 3 (false negatives)

Auditor: an independent agent that wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg14-timing-tests` at `7b8c452` (SG11–SG14 merged).
Scope: `src/autolab/verification/*`, `agents/*`, `orchestrator/*`.

Every finding has a repro in `tests/audit/test_audit_9.py`. Each test asserts the correct
behaviour and is marked `xfail(strict=True, reason="AUDIT-9 #n")`. Each one fails today
on its stated assertion (checked with `--runxfail`) and will XPASS once the issue is
fixed. All repros run on `FakeLLMClient`. None calls a real LLM.

"Admitted" means `VerificationMonitor().decide(verdict)` returns PASS. That is the gate
that `BaseAgent.__call__` and the graph's `monitor` node both apply. Where a repro turns
faithfulness on, every registered judge verdict is the one an honest judge gives. Two
limits are accepted and are not reported here: premises are unverified assumptions, and
faithfulness is off for clients that are not live.

Summary: 1 critical, 2 high, 0 medium, 1 low.

---

## Critical

### #1 Z3 builtins missing from `RESERVED_SYMBOLS` can still be shadowed, which reopens AUDIT-5 #1

**Impact.** SG11 fixed AUDIT-5 #1 with a hand-written list of reserved names. Z3 accepts a
redeclaration of every builtin I tried that is not on the list:
- strings and sequences: `str.len`, `str.++`, `str.contains`, `str.prefixof`, `str.<`,
  `str.to_int`, `str.at`, `str.substr`, `seq.len`, `seq.unit`, `re.*`, `str.in_re`,
  `char.<=`;
- bit-vectors: `bvult`, `bvadd`, `bvneg`, `bv2int`, `int2bv`, `ubv_to_int`;
- floating point: `fp.lt`, `fp.abs`, `to_fp`;
- sets and arrays: `subset`, `union`, `intersection`, `setminus`, `complement`,
  `as-array`, `map`, `default`, `array-ext`;
- pseudo-Boolean: `pbeq`, `pble`, `pbge`;
- arithmetic: `~`, `sin`, `cos`, `asin`, `atan`, `sinh`, `/0`, `div0`, `mod0`, `^0`.

A redeclared builtin becomes uninterpreted in every check of the trace, because each
check declares the trace's final vocabulary. The attack is the one in AUDIT-5 #1.1, with
a string builtin in place of `>`. String claims are realistic in this domain, such as
primer and DNA sequences.
- Step 0 is the premise "The primer sequence is GAATTCAAGG", encoded
  `(= primer "GAATTCAAGG")`.
- Step 1 is the premise "The primer is longer than 20 bases", encoded
  `(> (str.len primer) 20)`. It contradicts step 0, because that sequence has 10 bases.
- Step 2 is the premise "If the primer is longer than 20 bases then it binds
  specifically".
- Step 3, "The PCR was run once", also declares `(declare-fun str.len (String) Int)`.

Every judge verdict is honest and every step is VALID. The conclusion "The primer binds
specifically" scores 1.0 and the monitor returns PASS. Without the step-3 declaration,
step 1 is INVALID/`premises_inconsistent` and the trace is blocked (score 0.39).

The same mechanism defeats the contradiction trigger (principle 7c) the way AUDIT-5 #1.2
did: the redeclaration goes in the ignored final step. It also defeats the circularity
check through any builtin that a premise uses.

**Repro.**
- `test_shadowed_str_len_hides_a_premise_that_contradicts_another`
- `test_vocabulary_rejects_every_z3_builtin` (8 cases)

**Root cause.**
- `src/autolab/verification/formalizer.py:157`: `RESERVED_SYMBOLS` is an explicit list
  that covers Core, Ints, Reals and Arrays only.
- `formalizer.py:61`: membership in that list is the only name check.
- SG11's "Open" list records this gap. The defence-in-depth items from AUDIT-5 #1 are also
  still open (`vericot.py:125`, `vericot.py:213-214`). Each check still declares the final
  vocabulary, including the declarations of later steps and of steps the judge rejected.

**Suggested fix.** Derive the check from Z3 instead of keeping a list.
1. Restrict declared names to `[A-Za-z][A-Za-z0-9_]*`. This alone rules out every dotted,
   punctuated and `~`/`^0`-style builtin.
2. Reject a name if `z3.parse_smt2_string(f"(assert (= {name} {name}))")` fails with any
   error other than `unknown constant`. That rejects every builtin, including `sin`,
   `subset` and `bvult`.
3. Defence in depth: give each check only the declarations of the formulas it contains,
   and drop the declarations of a step the judge rejects.

**Resolution (SG15).** `Vocabulary.merged` now checks each declared name in three steps.
`RESERVED_SYMBOLS` comes first, so its "reserved symbol" message is unchanged. The name
must then match `[A-Za-z_][A-Za-z0-9_]*`, which rules out `str.len`, `~`, `^0`, `as-array`
and every other dotted or punctuated name. Finally `_is_z3_builtin` asks Z3. It parses
`(assert (= N N))` and `(declare-const x N)` with no declarations, in the thread's parse
context. If either parse succeeds, or fails with anything but "unknown constant N" or
"unknown sort 'N'", the name is a builtin (`sin`, `subset`, `bvult`, `fp`, `RNE`, `Float32`,
`pbeq`, `div0`, …). Results are cached per name. A rejected name is a `FormalizationError`,
so the step is UNGROUNDED/`formalization_failed`. Both repros are flipped and their
assertions are unchanged. Not done: item 3 (per-check declarations, dropping a
judge-rejected step's declarations). That is in `vericot.py`, outside SG15.

---

## High

### #2 The `assertion` field can carry SMT commands, which bypasses AUDIT-1 #2 and AUDIT-5 #1 and can also change Z3's process-wide parameters

**Impact.** A premise formula is only ever parsed inside `(assert X)`: once by the
formalizer's parse check and once as a pool member (`solver._wrap_assert`). An assertion
such as `a) (cmd ...) (assert b` therefore parses in both places and runs `cmd` in the
middle of the check document. A conclusion or inference cannot do this, because the
solver wraps its claim as `(assert (not X))`, so the probe recorded in AUDIT-1 ("no
paren-balanced payload parses under both") held for claims but not for premises. The
shape check (AUDIT-1 #2) and the reserved-name check (AUDIT-5 #1) inspect only
`declarations`. Three effects follow:
1. **Builtin redeclaration.** Premise 0, "If absorbance exceeds 0.9 then compound K-17 is
   active", is formalized as
   `(=> (> absorbance 0.9) active)) (declare-fun = (Real Real) Bool) (assert true`. In
   every later check the pool lists this premise before `context.facts`, so the
   code-built fact `(= absorbance 0.42)` becomes an uninterpreted atom. Premise 1,
   "Absorbance exceeds 0.9", contradicts the measurement but is VALID/`assumed`. The
   conclusion "Compound K-17 is active" is VALID, the score is 1.0, and the monitor
   returns PASS. Without the injection, step 1 is `premises_inconsistent` (score 0.33).
   The judge sees the assertion text. As in AUDIT-1 #2, a judge that asks only whether
   the asserted proposition states the claim approves it.
2. **`(reset-assertions)`.** This drops every assertion before it in the document. In a
   premise's own consistency check the premise comes last, so it wipes the facts and the
   prior premises it would contradict. I confirmed in Z3 4.16 that `parse_smt2_string`
   honours this command. No separate repro: effect 1 already shows the bypass.
3. **`(set-option ...)` changes global state.** Z3 applies `set-option` in a parsed string
   to its process-wide parameters, even inside a fresh `z3.Context`. The formalizer's
   parse check is enough to trigger it, even if the step is later rejected. One trace
   carrying `(set-option :rlimit 1)` makes every later check in the process return
   `unknown`, across runs and threads. Each later trace is then UNGROUNDED/`timeout`
   throughout. That alone fails closed, but it turns every circularity check
   inconclusive, which is the precondition for #3. `Z3Solver` sets only `timeout` per
   solver, so other options (`rlimit`, rewriter and arithmetic options) persist.

**Repro.**
- `test_assertion_cannot_smuggle_a_builtin_redeclaration`
- `test_a_trace_cannot_change_z3_global_parameters`: after the poisoned verify, an
  unrelated honest run is UNGROUNDED at every step.

**Root cause.**
- `src/autolab/verification/formalizer.py:109`: the assertion is checked only by
  parsing `(assert {assertion})`, not by checking that it is exactly one term.
- `src/autolab/verification/solver.py:172` and `:177`: `_wrap_assert` splices the raw
  string into the document.
- `formalizer.py:109` and `solver.py:100`: `parse_smt2_string` runs any command,
  including `set-option`, `reset-assertions` and `declare-fun`.

**Suggested fix.**
1. Require the assertion to be exactly one balanced S-expression or atom: depth never
   below 0 and returning to 0 only at the end, with no top-level command heads. Use the
   same scanner as `_decl_name`.
2. Parse it as a term, for example `(assert (! X :named __step))`, and check that
   `parse_smt2_string` returns exactly one assertion.
3. Reject `set-option`, `set-info`, `reset`, `reset-assertions`, `push`, `pop` and
   `declare-*` anywhere in it.
4. As a backstop, call `z3.reset_params()` or re-assert the solver options before each
   check.

**Resolution (SG15).** `Formalizer.formalize` rejects the assertion before any parse
unless `_is_single_term` accepts it. That means exactly one atom or balanced
S-expression, with nothing after it, depth never below 0, no `|` or `;`, and no SMT
command head (`assert`, `declare-*`, `define-*`, `set-option`, `reset-assertions`,
`push`, `pop`, …) after any `(`. String literals are skipped, so `"a) (b"` inside a
string is allowed. No `set-option` reaches Z3, not even through the formalizer's own
parse check. Both repros are flipped and their assertions are unchanged. The
`_register` helper in `test_audit_9.py` now skips a step whose parse yields more than
one assertion, as the formalizer does. Without that, its prompts assumed the poisoned
step had extended the vocabulary. Not done: item 4, resetting global parameters before
each check. That is in `solver.py` (SG19). It is not needed while every formula
reaches the solver through the formalizer, and `context.facts` are built in code.

### #3 A premise that was INVALID in pass 1 is assumed in the pass-2 re-check and is never checked for circularity

**Impact.** `_payload_overrides` examines only premises that were VALID in pass 1. A
premise dropped by an override leaves the pool. Any premise that was
`premises_inconsistent` only because it contradicted the dropped one becomes
VALID/`assumed` in pass 2, and its circularity is never checked. The drop is non-blocking
when the alone check is inconclusive (`reason=timeout`, `vericot.py:266-267`). AUDIT-5
called this "latent". It is now reachable.
- The facts state absorbance = 0.42.
- Premise A is "The well is not contaminated, and either K-17 is active or the absorbance
  is not 0.42 and 2^r = 3". With the facts, A entails `active`. Alone, Z3 answers
  `unknown` (nonlinear `^`), so A is overridden UNGROUNDED/`timeout`.
- Premise B is "The well is contaminated and K-17 is active", which contains the payload
  claim. In pass 1, B is INVALID because it contradicts A.

In pass 2, B is assumed and proves "Compound K-17 is active" on its own. The verdicts are
[UNGROUNDED/timeout, VALID/assumed, VALID], the score is 0.83, and the monitor returns
PASS. Every judge verdict is honest. On its own, B is circular and is blocked (score
0.25). That defeats AUDIT-1 #1.

`unknown` arises from nonlinear arithmetic, quantifiers or a timeout. SG14 observed real
0.2 s premise-check timeouts under load. A process poisoned by #2.3 makes every check
`unknown`. In each case the alone check comes back inconclusive.

**Repro.** `test_premise_inconsistent_in_pass_one_cannot_prove_the_claim_in_pass_two`

**Root cause.**
- `src/autolab/verification/vericot.py:128-135`: overrides are computed once, from the
  pass-1 verdicts, and then pass 2 runs with no further circularity analysis.
- `vericot.py:254`: non-VALID premises are skipped.
- `vericot.py:266-267`: an inconclusive alone check yields a non-blocking reason.

**Suggested fix.** Iterate to a fixed point: after each re-check, run `_payload_overrides`
again on the new verdicts, merging with the earlier overrides, until no new premise is
overridden. A premise whose verdict changed between passes should never escape the
check. Alternatively, make an inconclusive circularity check blocking, with reason
`no_premises` and "could not rule out a circular premise". That keeps "fail closed" true
for the trace and not just for the premise. Doing both is cheap.

---

## Low

### #4 Integer Design parameters above 2^53 are bound to a rounded value; above ~1e308 they crash the graph

**Impact.** `design_facts` and `design_claim` render numeric parameters through
`float(p.value)`. The payload's `cell_count = 9007199254740993` is bound as
`(= param_cell_count 9007199254740992.0)`. The claim sentence says the same, so a trace
that states the rounded value verifies against a payload that holds a different value.
The stakes are low: the simulator also uses `float()`, and the cost trigger reads the
float fields. An integer above about 1e308 raises `OverflowError`.
`escalate_permanent_errors` catches only `ValueError`, `RuntimeError` and
`anthropic.APIError`, so the Design node raises out of the graph instead of pausing with
an `error` request.

**Repro.**
- `test_integer_parameter_is_bound_to_its_exact_value`
- `test_huge_integer_parameter_is_a_permanent_error`

**Root cause.** `src/autolab/agents/common.py:94` and `:109` use `float(p.value)`.
`common.py:135-138` formats via `repr(float)`. `src/autolab/orchestrator/recovery.py:151`
does not catch `OverflowError`.

**Suggested fix.** Render an `int` as an exact `Int` or decimal literal
(`str(value)` + `.0`, or declare the symbol `Int`). Raise `ValueError` for values outside
a configured bound. Optionally add `ArithmeticError` to the permanent errors.

---

## Probed and held up

- **Per-thread parse context (SG11).** Declarations do not persist between
  `parse_smt2_string` calls on one context: a second parse that uses an earlier symbol
  fails. Reusing the per-thread context is therefore equivalent to a fresh one for
  declarations and sorts. Its only leak is #2.3, and that is global, not per-context.
  `Z3Solver` uses a fresh context per check.
- **Quoted symbols and other spellings.** `|>|`, `|x|`, string literals and comments in
  `declarations` are rejected by `_decl_name` (`|`, `"` and `;`). A numeral or
  `#x..` name, or `(declare-fun(> ...)`, fails the shape check or Z3's parse
  (`formalization_failed`).
- **Reserved names (AUDIT-5 #1).** All 90 listed names are rejected in `declarations`,
  including `>`, `<`, `=`, `not`, `and`, `=>` and `true`. The three AUDIT-5 #1 repros pass.
  The bypasses are names that are not on the list (#1) and the `assertion` field (#2).
- **Command injection in a claim.** A conclusion or inference cannot inject commands:
  `(assert X)` and `(assert (not X))` cannot both parse when X leaves its term. Only
  `premise` formulas reach the solver in premise position alone (#2).
- **`design_facts` symbol collisions.**
  - Two parameter names that map to one symbol (`a-b` and `a_b`) with different values
    give inconsistent facts. Every step is then INVALID/`premises_inconsistent`, so this
    fails closed.
  - The `param_` prefix keeps parameter facts clear of `estimated_*`, reserved names and
    builtins.
  - A trace that redeclares a fact symbol with another sort is rejected by the
    vocabulary.
  - Non-ASCII parameter names (`μL`, `温度`) pass `\W` but fail Z3's lexer. Every step of
    that Design is then `formalization_failed`, which blocks it and escalates (fail
    closed; a usability issue).
- **Non-numeric parameters and units.**
  - String and bool values have no fact, but they are in the bound claim text. A faithful
    conclusion that includes them must be entailed, so a premise that states another
    value makes the conclusion INVALID.
  - Units appear only in the text. A conclusion that drops them is an unfaithful
    formalization (accepted limit).
  - A string value or protocol that imitates `estimated_cost_usd = …` cannot override
    the code-built fact.
- **Float formatting.** `_decimal` is exact for finite floats (`repr` → `Decimal`),
  negative values use `(- x)`, and `inf`/`nan` raise `ValueError`, which escalates.
  `inf` passes the `ge=0` validators on the cost and duration fields, but `design_facts`
  rejects it before verification.
- **The HITL policy re-check (AUDIT-5 #3).**
  - `approve` on a `verification` request re-runs cost/duration (Design) and
    contradiction (Research) on the withheld payload.
  - `approve` on `policy` merges, because a human saw the policy reason.
  - An `error` request carries `hitl_payload=None`, so its `approve` merges nothing.
  - A monitor retry clears the payload.
  - A policy that raises inside the `hitl` node propagates. That fails closed, and
    nothing is merged.
  - No path was found that merges a Design or Research payload without its policy.
- **Score cap and blocking.** No blocked verdict reaches PASS, with `retry_rework` on or
  off. `BaseAgent` and the monitor share `blocking_reason`.
- **Design trace circularity.** A one-step Design trace whose conclusion restates the
  code-built facts is admitted, because the facts are the payload. A faithful
  formalization of `design_claim` also has to entail the protocol conjunct. Whether the
  protocol tests the hypothesis is left to faithfulness and review (accepted limit).

## Re-check of earlier soundness fixes

| Fix | Repro(s) | Status at `7b8c452` |
|---|---|---|
| AUDIT-1 #1 circular premise | `test_circular_premise_restating_payload_is_not_admitted`, `test_retry_loop_does_not_launder_refuted_claim_into_premise` | Pass. Bypassed via the pass-2 re-check (#3). |
| AUDIT-1 #2 declaration smuggling | `test_declarations_cannot_smuggle_assertions[assert, define-fun]` | Pass for `declarations`. The `assertion` field is open (#2). |
| AUDIT-1 #3 UNGROUNDED conclusion | `test_conclusion_that_fails_to_formalize_…`, `…_rejected_by_faithfulness_judge…`, `…_times_out…`, `…_with_no_premises…` | Pass. Holds. |
| AUDIT-1 #4 premise consistency | `test_premise_contradicting_a_measurement_is_not_valid`, `test_mutually_contradictory_premises_are_not_admitted` | Pass. Bypassed by a shadowed builtin (#1) or an injected command (#2). |
| AUDIT-1 #5 tautological conclusion | `test_tautological_conclusion_is_not_valid[…]` | Pass. Holds. |
| AUDIT-1 #6 contradiction trigger fails closed | `test_contradiction_trigger_fires_when_reformalization_fails`, `…_fails_closed_when_prior_cannot_be_formalized` | Pass. Defeated by an unreserved builtin in the final step (#1). |
| AUDIT-5 #1 reserved symbols (SG11) | `test_shadowed_gt_…`, `test_shadowed_lt_…`, `test_shadowed_not_…` | Pass for listed names. Unlisted builtins (#1) and the `assertion` field (#2) are open. |
| AUDIT-5 #2 Design payload binding (SG12) | `test_design_cost_fields_that_contradict_the_verified_protocol_skip_the_pause` | Pass. Holds, apart from integer rounding (#4). |
| AUDIT-5 #3 approve re-runs the policy (SG12) | `test_approving_a_verification_escalation_still_applies_the_cost_trigger` | Pass. Holds. |

All of the listed repros pass at the base (`uv run pytest tests/audit -q`: 64 passed and
18 xfailed before this audit's file was added).
