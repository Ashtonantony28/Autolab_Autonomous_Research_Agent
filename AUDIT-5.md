# AUDIT-5: Encoding soundness, round 2 (false negatives)

Auditor: an independent agent that wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg9-entity-identity` at `8a1c1de` (SG7–SG10 merged).
Scope: `src/autolab/verification/*`, `agents/*`, `orchestrator/*`.

Every finding has a repro in `tests/audit/test_audit_5.py`. Each test asserts the correct
behaviour and is marked `xfail(strict=True, reason="AUDIT-5 #n")`. It fails today and will
XPASS (a strict failure) once the issue is fixed. All repros run on `FakeLLMClient`. None
calls a real LLM.

"Admitted" means `VerificationMonitor().decide(verdict)` returns PASS. That is the gate
`BaseAgent.__call__` and the graph's `monitor` node both apply. Where a repro turns
faithfulness on, the registered judge verdicts are the ones an honest judge gives. Each
judged assertion is a verbatim encoding of its claim under the declarations the judge is
shown. Two limits are accepted and are not reported here: premises are unverified
assumptions (`P`, `P ⇒ C`), and faithfulness is off for clients that are not live.

Summary: 1 critical, 1 high, 2 medium, 1 low.

---

## Critical

### #1 Declarations can redeclare SMT-LIB builtins (`>`, `<`, `=`, `not`, `and`, `=>`), and the redeclaration silently changes the meaning of formulas the judge already approved

**Impact.** Z3 accepts `(declare-fun > (Real Real) Bool)`, `(declare-fun not (Bool) Bool)`,
`(declare-const true Bool)` and similar declarations. The uninterpreted symbol then shadows
the builtin in every later term. That includes the solver's own `(assert (not claim))`
wrapper. `_decl_name` checks only the declaration's shape, not its name. Since SG10,
every check declares the trace's final vocabulary, so a declaration in a later step
changes how earlier steps are checked. The vocabulary also keeps the declarations of a
step the faithfulness judge rejected. Each affected step was judged under the declarations
it had when it was formalized, so its judge never sees the shadowing declaration. Three
round-1 resolutions fall to this:

1. **Premise consistency (AUDIT-1 #4).** The measurement fact is `absorbance = 0.42`. The
   premise is "Absorbance exceeds 0.9", encoded `(> absorbance 0.9)`, and its judge
   approves it. A later throwaway premise, "The plate was read once", declares
   `(declare-fun > (Real Real) Bool)`. Its judge rejects it, so the step is
   UNGROUNDED/`formalization_failed`, which does not block. The false premise is now
   VALID/`assumed`. Together with "if absorbance > 0.9 then K-17 is active", it proves
   "Compound K-17 is active". The score is 0.875 and the monitor returns PASS. Without the
   declaration, the premise is INVALID/`premises_inconsistent` and the trace is blocked.
2. **Contradiction trigger (AUDIT-1 #6).** The prior hypothesis "Yield exceeds 0.5" is
   encoded `(> yld 0.5)`. The new hypothesis "Yield is below 0.2" is encoded `(< yld 0.2)`.
   Both are faithful, and in real arithmetic they are jointly unsat. The trigger's last
   step is the redundant inference that restates the new statement. Its formalization
   declares `(declare-fun < (Real Real) Bool)`. The trigger ignores that step's verdict
   (`step_verdicts[:-1]`), but its declaration still enters every check. No step is
   `premises_inconsistent`, so the trigger returns None and the contradicting hypothesis
   is admitted with no pause.
3. **Circularity (AUDIT-1 #1).** A later step declares `(declare-fun not (Bool) Bool)`.
   The trace then has two premises: "For every proposition b, if b holds then not-b is
   false", encoded `(forall ((b Bool)) (=> b (= (not b) false)))`, and "K-17 cures disease
   D in people", encoded `cures`, which paraphrases the payload. Each premise is checked
   alone against the conclusion and neither entails it, because `cures ∧ not(cures)` is
   satisfiable once `not` is uninterpreted. Together they entail it. The circular
   restatement passes with score 0.875.

**Repro.**
- `test_shadowed_gt_hides_a_premise_that_contradicts_the_measurement`
- `test_shadowed_lt_in_the_ignored_final_step_defeats_the_contradiction_trigger`
- `test_shadowed_not_lets_a_restated_payload_premise_prove_itself`

**Root cause.**
- `src/autolab/verification/formalizer.py:156`. `_decl_name` accepts any token as the
  declared name, including SMT-LIB core, arithmetic and array symbols. Z3 does not reject
  a function redeclaration of a builtin. Only builtin sorts are protected.
- `src/autolab/verification/formalizer.py:108` and `vericot.py:125`. The vocabulary
  commits before the judge runs, and `verify` declares the final vocabulary in every
  check (`vericot.py:213-214`). The vocabulary includes later steps and judge-rejected
  steps.
- `src/autolab/verification/faithfulness.py:38`. The judge sees only the declarations
  that existed when its step was formalized, not the ones the check will use.
- `src/autolab/orchestrator/checkpoints.py:136`. The trigger skips the final step's
  verdict but not its declarations.
- `src/autolab/verification/solver.py:173`. The negation wrapper uses the name `not`,
  which the trace can rebind.

**Suggested fix.**
- In `Vocabulary.merged`, reject any declared name that Z3 already resolves. One way is
  to parse `(assert (= NAME NAME))` (or an application) in a fresh context before
  declaring it. Another is to require user symbols to match `[A-Za-z][A-Za-z0-9_]*` and
  not appear on a reserved list (`not and or xor => = distinct ite true false abs div mod
  to_real to_int is_int select store`, and so on).
- Defence in depth: declare in each check only the declarations of the formulas in that
  check. Drop a step's declarations when its judge rejects it. Have the judge see exactly
  the declaration block the check will use.
- In `contradiction_trace`, do not formalize the redundant final step, or fail closed on
  it too.

**Resolution (SG11).** `Vocabulary.merged` rejects any declaration whose name is in
`formalizer.RESERVED_SYMBOLS` (90 names: SMT-LIB core, Ints/Reals/Arrays symbols, sorts,
syntax keywords, commands, and common Z3 extensions such as `rem`, `^`, `implies`,
`lambda`). The step raises `FormalizationError` and becomes UNGROUNDED/`formalization_failed`,
and its declarations never enter the vocabulary, so no later check can shadow a builtin.
The same rule covers `context.facts`, which seed the vocabulary through `merge`. All three
repros now pass. Not done: the defence-in-depth items (per-check declarations, dropping a
judge-rejected step's declarations, showing the judge the check's declaration block, and
not formalizing the trigger's redundant final step).

## High

### #2 Payload binding covers one text field; the Design fields that drive the cost/duration pause and the simulator are never verified

**Impact.** The Design trace must conclude `protocol` (`design.py:44`). Here the protocol
is "Incubate the plate for 240 minutes using 500 USD of reagent", and the trace proves it
honestly (score 1.0). The same reply sets `estimated_cost_usd=5.0` and
`estimated_duration_minutes=30.0`. `cost_trigger` reads only those two fields, so there is
no pause, and a design whose own verified protocol costs $500 over 4 hours reaches
`current_experiment`. That bypasses principle 7b. `parameters`, which `LabSimulator.run`
consumes, are unbound in the same way: the experiment that runs is not the experiment
that was verified. The rest of the payloads have the same gap at lower stakes: the
Hypothesis's `rationale`, `citations` and `confidence`, and every Analysis interpretation
except rank 0, plus its `summary`.

**Repro.** `test_design_cost_fields_that_contradict_the_verified_protocol_skip_the_pause`

**Root cause.**
- `src/autolab/agents/design.py:44`. `payload_claim` is the protocol text only.
- `src/autolab/agents/common.py:40`. `DESIGN_REPLY` takes cost, duration and parameters
  from the model.
- `src/autolab/orchestrator/checkpoints.py:89`. The policy trigger trusts those fields.

**Suggested fix.** Treat the structured fields the way Execute treats measurements. Inject
code-built facts (`(= estimated_cost_usd 5.0)`, `(= estimated_duration_minutes 30.0)`, one
per numeric parameter) into `context.facts`. Then a trace whose premises state other
values is `premises_inconsistent`. Also make the bound claim a code-built sentence that
names those values (as `result_claim` does), so the trace must prove them. A cheaper
partial fix is to also run the policy trigger on any cost or duration figure that the
verified trace formalizes.

**Resolution (SG12).** `common.design_facts` builds one Real fact in code for each of `estimated_cost_usd`, `estimated_duration_minutes` and every numeric (non-bool) parameter (`param_<name>`). The facts go into the Design `context.facts`. The bound claim is now `common.design_claim(design)`: "design protocol: <protocol>; estimated_cost_usd = …, estimated_duration_minutes = …, param_<name> = …" (string and bool parameters are named in the sentence too). The trace prompt asks for that conclusion through `conclusion_instruction`, and `llm.py` is unchanged. A trace that still concludes only the protocol is unbound, so it scores 0.0 and gets a `verification` pause. A trace that concludes the new claim, but whose premise states another value for a bound symbol (for example `(= estimated_cost_usd 500.0)` against the 5.0 fact), is INVALID/`premises_inconsistent` and is not admitted. That case is covered in `tests/unit/agents/test_design.py`. The repro is flipped. **One assertion changed:** `update["confidence"] == 1.0` is now `< 1.0`. The repro's trace concludes the bare protocol, which is no longer the payload claim, so no trace can both score 1.0 and pause unless the trigger guesses which formalized symbols mean cost. That partial fix was not built. The two assertions that matter (`hitl_pending` is set and `current_experiment` is withheld) are unchanged. Limit: a trace that formalizes the protocol's own figures under unrelated symbols (`usd`, `minutes`) is still not linked to the facts. Only an honest formalizer, or faithfulness checking, ties "500 USD" in the protocol text to `estimated_cost_usd`.

## Medium

### #3 `approve` on a verification escalation merges the payload without running the policy trigger

**Impact.** `BaseAgent.__call__` runs `hitl_trigger` only after verification passes
(`base.py:92-99`). Consider a $50 design whose trace fails verification on every attempt.
It escalates with `trigger="verification"`, and the interrupt shows only the blocking
reason and verdict summary. It shows no cost and no payload. When the human approves the
reasoning, `hitl_node` merges the withheld design and goes to Execute. The cost pause
(principle 7b) never happens, and the run completes. The same path skips the
contradiction trigger (7c) for a Research hypothesis.

**Repro.** `test_approving_a_verification_escalation_still_applies_the_cost_trigger`

**Root cause.**
- `src/autolab/agents/base.py:92-99`. The policy trigger is evaluated only on the PASS
  branch.
- `src/autolab/orchestrator/checkpoints.py:76-78`. `approve` merges `hitl_payload`
  without re-checking policy.

**Suggested fix.** Evaluate `hitl_trigger` on every produced payload. Put the policy
reasons in the escalation's `reason`/`payload`, so one pause covers both. Or have
`hitl_node` re-run the agent's policy check before it merges on `approve`. Also include a
payload summary (statement, cost, duration) in the interrupt value, so the human can see
what they are approving.

**Resolution (SG12).** `checkpoints.make_hitl_node(policies)` replaces the graph's `hitl` node. `build_graph` passes `merged_policy(contradiction_trigger, "hypotheses")` for Research and `merged_policy(cost_trigger, "current_experiment")` for Design: the same trigger objects the agents use, applied to the withheld `hitl_payload`. On `approve` of a `verification` request, the agent's policy runs first. If it fires, the node pauses again (`goto="hitl"`) with a `policy` request that carries the policy reason and keeps `hitl_payload`. A second `approve` merges. `approve` on a `policy` or `error` request merges without re-checking. The repro is flipped and its assertions are unchanged. The module-level `hitl_node` is `make_hitl_node()` with no policies. Not done: the interrupt value still carries no payload summary (cost, statement) for the first, verification-only pause.

### #4 The contradiction trigger misses contradictions that need any background knowledge

**Impact.** The prior hypothesis "Additive X increases crystal yield" is encoded
`x_increases_yield`, and the new one, "Additive X decreases crystal yield", is encoded
`x_decreases_yield`. Both encodings are faithful (the judge approves them). The formalizer
prompt forbids adding background knowledge (`llm.py:107`), and no commonsense premises are
grounded (CLAUDE.md Phase 2 step 3b). The two statements are therefore jointly
satisfiable, the trigger returns None, and a hypothesis that flatly contradicts the one in
state is admitted. The trigger fires only when a contradiction is unsat in the vocabulary
the formalizer happened to choose, for example `P` vs `(not P)`, or a shared Real with
opposite bounds.

**Repro.** `test_opposite_effect_hypothesis_is_flagged_as_a_contradiction`

**Root cause.**
- `src/autolab/orchestrator/checkpoints.py:101-141`. Contradiction is decided only by Z3
  unsat over independently formalized statements.
- `src/autolab/verification/premise_grounder.py`. Grounding has no commonsense source.

**Suggested fix.** Add a structured LLM-as-judge pairwise contradiction check
(`call_structured`, failing closed) next to the Z3 check. Or formalize the prior
statements and the new statement in one call that must use a shared vocabulary and state
the mutual-exclusion axioms it relies on, and send those axioms through the faithfulness
judge.

## Low

### #5 Payload binding casefolds, conflating case-distinct scientific claims

**Impact.** The binding check compares the conclusion and the payload claim after
whitespace collapse and `casefold()`. In scientific text, case changes meaning: Co
(cobalt) vs CO (carbon monoxide), mS vs ms, human BRCA1 vs mouse Brca1. A trace that
faithfully proves "CO exposure inhibits enzyme E" binds to the hypothesis "Co exposure
inhibits enzyme E" and scores 1.0. The faithfulness judge does not catch this, because it
compares the SMT with the trace's text, not with the payload.

**Repro.** `test_binding_does_not_conflate_cobalt_with_carbon_monoxide`

**Root cause.** `src/autolab/verification/vericot.py:282` (`_normalize`), used at
`vericot.py:291` (and for the restatement check at `vericot.py:259`).

**Suggested fix.** Bind after whitespace collapse (and Unicode NFC) only, keeping case.
The agents already tell the model to state the conclusion verbatim. If the restatement
check should stay lenient, keep casefold there only.

---

## Probed and held up

- **Circularity variants (AUDIT-1 #1).** These single premises that equal or strengthen
  the conclusion are all UNGROUNDED/`no_premises`, and the trace is blocked:
  `(not (not c))`, `(forall ((x Int)) c)`, `(ite d c c)`, an existential witness
  `(exists ((n Int)) (and (cur n) (= c (cur n))))`, and `(and c d)` routed through an
  inference step. A timeout in the circularity check fails closed for that premise. (The
  only bypass found is #1.3, which needs a shadowed `not`.)
- **A swapped circular premise in the re-check.** Take premises `(and c d)` and
  `(and c (not d))`. In pass 1 the second is `premises_inconsistent`. In the pass-2
  re-check it becomes VALID/`assumed` and is never checked for circularity itself. The
  trace is still blocked, but only because the first premise's `no_premises` verdict
  blocks it (score 0.83 without that rule). It is latent. If the first premise is ever
  overridden with a non-blocking reason, this becomes a bypass.
- **Tautologies (AUDIT-1 #5).** `(or c (not c))` as the conclusion is
  UNGROUNDED/`formalization_failed`, and the trace is blocked.
- **Declaration smuggling (AUDIT-1 #2).** `define-sort`, `define-fun`, `assert`, and
  extra forms on a line are all rejected. Redeclaring a builtin *sort* (`Int`, `Bool`,
  `Real`) is rejected by Z3. Only function and constant names leak (#1).
- **Score cap and blocking.** No blocked verdict reaches PASS with `retry_rework` either
  on or off. The cap uses the configured thresholds, and `BaseAgent` and the monitor node
  share `blocking_reason`.
- **The retry loop.** Only `trigger="verification"` requests are retried. The withheld
  payload is cleared on retry. After `max_retries` the request escalates.
- **HITL `retry`/`abort`.** `retry` re-runs the agent with the note as feedback and gives
  a fresh budget. `abort` ends the run. Neither merges the payload.
- **Parallel faithfulness judges (SG10).** A judge `ValidationError` or `RuntimeError`
  fails closed for its own step only. Any other judge exception cancels the remaining
  judges and propagates, so nothing is admitted. Formalization stays strictly ordered.
- **Executor cancellation (SG10).** An interrupted check returns UNGROUNDED/`timeout`,
  which can never make a payload conclusion VALID. After `shutdown()`, new submissions
  raise `RuntimeError`, which becomes an `error` HITL request. An interrupted
  *intermediate* step is just the UNGROUNDED gap ADR-0007 already accepts.
- **Faithfulness on and off.** `is_live` sees through `RetryingLLMClient._inner`, so
  `build_graph` with a `ClaudeLLMClient` gets faithfulness for agents and for the
  contradiction trigger.
- **Payload binding versus the SG9 reply schemas.** Ids, timestamps, `question`,
  `hypothesis_id` and `result_id` are code-owned. Analysis ranks are validated as dense,
  so no two interpretations can tie at rank 0. Execute's bound claim and facts are
  code-built.

Observation (not a finding): in a CLI run, Research is the first node and `empty_state`
has no hypotheses, so the contradiction trigger (principle 7c) fires only when the initial
state is seeded with hypotheses.
