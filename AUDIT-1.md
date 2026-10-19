# AUDIT-1: Encoding soundness (false negatives)

Auditor: an independent agent that wrote none of the code. Base: `origin/claude/sg6-persistence`.
Scope: `src/autolab/verification/*`, `agents/*`, `orchestrator/*`.

Every finding below has a repro in `tests/audit/test_audit_1.py`. Each test asserts the
correct behavior and is marked `xfail(strict=True, reason="AUDIT-1 #n")`, so it fails
today and starts passing (XPASS, which strict mode reports as a failure) once the issue
is fixed. All repros run on `FakeLLMClient`; none calls a real LLM. "Admitted" means
`consistency_score >= PASS_THRESHOLD` (0.7) and `VerificationMonitor().decide(...)` returns
PASS. Those are the two gates `BaseAgent.__call__` and the `monitor` node apply.

Summary: 1 critical, 2 high, 3 medium, 0 low.

---

## Critical

### #1 A trace can assume its payload claim as a `premise` and then conclude it

**Impact.** Research question: "Does compound K-17 cure disease D?" Trace:
`0 premise "Compound K-17 cures disease D in humans"` (P), then
`1 conclusion "Compound K-17 cures disease D in humans"` (P, cites 0). The verifier
scores it 1.0. The agent merges the hypothesis with `hitl_pending=None`, and nothing in
the trace is grounded in a source, a citation or a measurement. The same thing happens
through the orchestrator. If a first attempt concludes the claim from an unrelated
premise and gets refuted (INVALID), the retry with feedback can move the claim into a
premise and conclude it. That retry passes, and the refuted hypothesis is admitted with
no human pause, even though `verification_history[0]` still records the refutation.
That is a retry loop laundering a bad trace. The project's own fixture
`register_trivial_trace` is exactly this circular shape, and most "passing" agent and
graph tests rely on it.

**Repro.** `test_circular_premise_restating_payload_is_not_admitted`,
`test_retry_loop_does_not_launder_refuted_claim_into_premise`.

**Root cause.** `src/autolab/verification/vericot.py:133` tags every `premise` step
VALID/`assumed` before any check. The payload binding (`vericot.py:173-181`) only checks
the conclusion's text. Nothing requires the conclusion to depend on anything other than
assumptions the trace made itself. On retry, `orchestrator/routing.py:51-65` re-runs the
agent, and the new trace is judged in isolation from the refuted one.

**Suggested fix.** Do not let LLM-asserted premises alone entail the payload claim. Treat
a premise whose formula entails, or equals, the conclusion as UNGROUNDED. Better, require
the conclusion to be entailed by premises that include at least one grounded
`context.facts` entry or entailed inference, and score assumed premises as UNGROUNDED
rather than VALID. Fixing this needs the `register_trivial_trace` fixtures to change too.

**Resolution (SG7).** The rule applies when a trace is bound to a payload claim and its conclusion is VALID. Every VALID `premise` step whose normalized claim equals the payload claim, or whose formula alone entails the conclusion, is marked UNGROUNDED/`no_premises` (circular). That covers the same formula, an equivalent formula, and a conjunction that contains the conclusion. The trace is then re-checked without those premises, so any inference that rests on them also loses support. The conclusion passes only if the remaining premises and `context.facts` still entail it. An inconclusive circularity check fails closed. A premise that restates a measurement does not hurt a conclusion that the facts already prove. Limitation: an assumption split over two premises (`P`, `P => C`) is accepted. Premises remain assumptions, which faithfulness and human review cover. The retry repro now escalates after `max_retries` instead of admitting the circular retry. `register_trivial_trace` is now premise P, premise P => C, conclusion C. The audit repros use the new `register_circular_trace`, which keeps the circular shape.

---

## High

### #2 `declarations` can smuggle SMT commands (`assert`, `define-fun`) that make the claim self-entailing

**Impact.** Conclusion "Compound K-17 cures disease D in humans" has declarations
`(declare-const cures Bool) (assert cures)` and assertion `cures`, and cites an
unrelated premise "K-17 was tested in a cell assay". The conclusion is VALID/`entailed`
and the trace scores 1.0. The `(declare-const k Bool) (define-fun cures () Bool true)`
variant does the same. This holds **with the faithfulness check on** and a judge that
correctly approves the assertion, because `cures` really does state the claim. The
smuggled command sits in the declarations block, which the judge prompt presents as sort
information only. The command then stays in the vocabulary, so every later step's
premise set carries it too, regardless of `referenced_step_indices` or that step's
verdict. Any formalizer output that does this defeats the solver: a misbehaving model,
or claim text that prompt-injects the formalizer.

**Repro.** `test_declarations_cannot_smuggle_assertions[assert]`,
`test_declarations_cannot_smuggle_assertions[define-fun]`.

**Root cause.** `src/autolab/verification/formalizer.py:129-140` (`_decl_name`) looks only
at a line's first two tokens, so `(declare-const X T) <anything>` is accepted, and
`formalizer.py:51-64` stores the whole line. `solver.py:146-152` (`_build_smt2`) copies
declaration lines verbatim ahead of the premises. `z3.parse_smt2_string` executes the
extra commands, and they enter the solver as premises (`solver.py:91-96`).

**Suggested fix.** Validate each declaration line on its own. It must be exactly one
`declare-const`, `declare-fun` or `declare-sort` S-expression. One check is to parse it
alone in a fresh context and require zero assertions and nothing after the closing
paren. Reject anything else as `FormalizationError`.

**Resolution (SG7).** Each `declarations` line must be exactly one `declare-const`, `declare-fun` or `declare-sort` form. That means balanced parens, nothing after the closing paren, and no `|`, `"` or `;` (`formalizer._decl_name`). Anything else raises `FormalizationError`, so the step becomes UNGROUNDED/`formalization_failed` and its declarations never enter the vocabulary. Both repros now pass.

### #3 An UNGROUNDED payload conclusion is admitted: formalization failure, faithfulness rejection, timeout, or no premises

**Impact.** The conclusion is the payload. Unproven conclusions pass whenever the trace
has enough assumed premises:
- The conclusion's SMT fails to parse. 1 premise plus 1 UNGROUNDED conclusion scores 0.75, admitted.
- **The faithfulness judge rejects the conclusion** ("the SMT does not state the claim").
  2 premises plus the rejected conclusion score 0.83, admitted. The SG5 defense against
  unfaithful formalization therefore only costs 0.5 points and never blocks.
- The solver times out on the conclusion (`reason="timeout"`). It scores 0.75, admitted.
- The conclusion cites only an UNGROUNDED step (`reason="no_premises"`). 3 premises, 1
  bad inference and the conclusion score 0.8, admitted. The monitor's `no_premises`
  rework rule is never reached because `decide` returns PASS on score first.

In probing, every SMT-injection attempt the parser *did* block (assertion paren
injection, `(reset)` in declarations, `|t|` quoted-symbol aliasing, re-declaring a symbol
with another sort) still produced a 0.75 trace through this path.

**Repro.** `test_conclusion_that_fails_to_formalize_is_not_admitted`,
`test_conclusion_rejected_by_faithfulness_judge_is_not_admitted`,
`test_conclusion_that_times_out_is_not_admitted`,
`test_conclusion_with_no_premises_is_not_admitted`.

**Root cause.** `src/autolab/verification/confidence.py:25,41` gives UNGROUNDED 0.5 weight
whichever step it is. `agents/base.py:85` and `agents/verification_monitor.py:45` gate on
the score alone. `vericot.py:126-131` maps formalization or judge failure to UNGROUNDED,
which the score then dilutes. ADR-0007 and PLAN SG1 already list this as open; it is
still exploitable at default settings.

**Suggested fix.** Require the final, payload-bound step to be VALID/`entailed` for a
trace to pass. Otherwise cap the score below `PASS_THRESHOLD`, as already done for
INVALID, and route it to RETRY. Also check `_needs_rework` before the score shortcut in
`VerificationMonitor.decide`.

**Resolution (SG7).** A bound trace whose final step is not VALID is capped below both thresholds (`consistency_score(require_final_valid=True)`). `VerificationMonitor.decide` now checks `blocking_reason` before the score: any INVALID or `no_premises` step, or a final step that is not VALID. Such a trace never passes. Under the retry-first default it goes to RETRY first, then ESCALATE after `max_retries`. `BaseAgent` gates with the same monitor, so the agent and the monitor never disagree. All four repros now pass.

---

## Medium

### #4 Premise steps are never checked for consistency with `context.facts` or with each other

**Impact.** Execute: the simulator measures `absorbance = 0.3`, a code-built fact. The
trace's step 0 premise says "The measured absorbance was 5.0", `(= absorbance 5.0)`, and
it is recorded as **VALID**. The conclusion `result_claim` cites a different premise and
is entailed, so the trace scores 1.0. Separately, premises "K-17 is toxic" and "K-17 is
not toxic" are both VALID, and a conclusion citing just one of them scores 1.0. An
auditor sees a false data claim with a VALID verdict in a trace that passed. The payload
itself is protected here only because the conclusion avoided citing the bad premise.

**Repro.** `test_premise_contradicting_a_measurement_is_not_valid`,
`test_mutually_contradictory_premises_are_not_admitted`.

**Root cause.** `src/autolab/verification/vericot.py:133-142` returns early for premises,
with no solver call. The `premises_inconsistent` check (`solver.py:98-104`) only covers
the premise pool of a non-premise step, and `premise_grounder.py` limits that pool to
the cited steps.

**Suggested fix.** Check each premise step for satisfiability together with
`context.facts` and the prior VALID steps, and mark it INVALID/`premises_inconsistent`
when that is unsat. Also run one final joint-consistency check over every VALID formula
in the trace.

**Resolution (SG7).** A `premise` step is assumed only if it is satisfiable together with `context.facts` and every earlier VALID step. The premise that makes the set unsat becomes INVALID/`premises_inconsistent` and leaves the pool. Every VALID inference is entailed by a subset of a consistent pool, so the VALID formulas of a trace are jointly consistent and no separate final check is needed. Both repros now pass. The explosion golden traces now flag their second premise.

### #5 A tautological conclusion is VALID whenever faithfulness is off

**Impact.** The conclusion text equals the payload claim, so binding passes, but it is
formalized as `true` or `(or t (not t))`. It is VALID/`entailed` and the trace scores
1.0. The formula says nothing about the claim. `Verifier(llm)` turns faithfulness off
for any client that lacks `live = True`. That includes `FakeLLMClient` (tests,
benchmark, error-injection harness) and any third-party `LLMClient`, since `live` is not
part of the protocol. In the shipped CLI path (`ClaudeLLMClient` wrapped in
`RetryingLLMClient`) the judge is on, and it is then the only defense.

**Repro.** `test_tautological_conclusion_is_not_valid[true]`,
`test_tautological_conclusion_is_not_valid[(or t (not t))]`.

**Root cause.** `src/autolab/verification/vericot.py:68-70` makes faithfulness
default-off unless `is_live(llm)` is true (`llm.py`, `is_live`).
`solver.py:83-130` has no check that the claim is non-trivial, meaning it is not valid
without premises.

**Suggested fix.** Before the entailment check, test the claim alone: if `not claim` is
unsat, it is a tautology, so mark it UNGROUNDED (a new reason such as `trivial`).
Consider requiring an explicit `check_faithfulness` choice rather than inferring it from
`live`.

**Resolution (SG7).** For a bound trace with a VALID conclusion, the verifier also checks the conclusion against `true` alone. A tautology becomes UNGROUNDED/`formalization_failed` ("formalization is a tautology"). An inconclusive check fails closed (UNGROUNDED/`timeout`). Both repros now pass. Faithfulness is still off by default for clients that are not live.

### #6 The contradiction trigger (principle 7c) fails open

**Impact.** The prior hypothesis "K-17 is toxic to human cells" formalizes to `toxic`,
and the new one "K-17 is not toxic to human cells" to `(not toxic)`. These are jointly
unsat. But `contradiction_trace` formalizes the new statement a second time for its
final `inference` step. If that redundant formalization fails, the step never reaches
the solver, its reason is `formalization_failed` instead of `premises_inconsistent`, and
the trigger returns None. The contradictory hypothesis is admitted without a pause. In
the same way, a prior hypothesis that fails to formalize is silently dropped from the
pool, and the check passes.

**Repro.** `test_contradiction_trigger_fires_when_reformalization_fails`,
`test_contradiction_trigger_fails_closed_when_prior_cannot_be_formalized`.

**Root cause.** `src/autolab/orchestrator/checkpoints.py:130-131` fires only on
`step_verdicts[-1].reason == "premises_inconsistent"`. Every other non-VALID outcome is
treated as "no contradiction". The consistency check depends on the final step
formalizing (`vericot.py:124-127`).

**Suggested fix.** Check joint satisfiability of the formalized statements directly, with
no re-formalized final step. Fail closed (pause) when any statement fails to formalize or
the check times out.

**Resolution (SG7).** `contradiction_trigger` (minimal edit in `orchestrator/checkpoints.py`) now fires on any `premises_inconsistent` step, since premises are checked against each other (#4). It fails closed, with "could not rule out a contradiction", when any statement (a premise step) is not VALID. The redundant final inference's own verdict no longer matters. Both repros now pass.

---

## Probed and held up

- **Dilution with an INVALID step.** 10 VALID premises plus 1 refuted conclusion scores
  0.69 (`INVALID_SCORE_CAP`), and the monitor routes it to RETRY. Dilution only works
  with UNGROUNDED steps (#3).
- **SMT injection through `assertion`.** The formalizer's `(assert X)` and the solver's
  `(assert (not X))` wrap the assertion differently, so no paren-balanced payload parses
  under both. The attempts tried (`c)) (assert (not true`, `c) (assert true`) are
  rejected as `formalization_failed`. A premise assertion that starts with `(assert`
  (the raw path in `_wrap_assert`) is rejected by the formalizer parse check first.
- **`(push)`, `(check-sat)` and `(reset)` in declarations.** `push` and `check-sat` do not
  change `parse_smt2_string`'s result: the conclusion stayed INVALID. `reset` wipes the
  declarations, so parsing fails. These are harmless, except that failures feed #3.
- **Redeclaring a symbol.** The same name with a different sort is rejected by the
  vocabulary. Quoted aliasing (`|t|` for `t`) and a second declaration hidden on one line
  are both rejected by Z3 as ambiguous or duplicate, giving `formalization_failed`
  (again, only #3 lets these pass).
- **Payload binding by text.** Near-matches (punctuation), a non-`conclusion` last step,
  and an unrelated conclusion all score 0.0 (existing `test_payload_binding.py`, which I
  reviewed). Binding has no other gap, apart from the formalization not being bound
  (#2, #3, #5).
- **Citing an INVALID or UNGROUNDED step.** That step is excluded from the premise pool,
  so a conclusion resting on a refuted inference gets no support (`no_premises`), and
  the INVALID cap still applies.
- **Measurement-fact injection.** Measurement names come from code
  (`LabSimulator._INSTRUMENT_KEYWORDS`), not from an LLM. Values are rendered as exact
  decimals (`_decimal`), and non-finite values raise. No path for an LLM to choose a fact
  name or value was found. The fact symbol cannot be redeclared with a different sort,
  because facts seed the vocabulary first.
- **INVALID steps reaching later steps.** Only VALID formulas join the premise pool
  (`vericot.py:104-106`). The exception is declarations, which a non-VALID step still
  adds to the vocabulary. This matters only together with #2.
