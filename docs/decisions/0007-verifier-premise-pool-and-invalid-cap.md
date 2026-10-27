# ADR-0007: Verifier premise pool rules and the INVALID score cap

- **Status:** Accepted
- **Date:** 2026-10-19
- **Amends:** ADR-0005 (consistency_score formula)

## Context

The SG1 audit found three ways an invalid trace could still pass:

1. **Explosion.** With premises `P` and `(not P)`, Z3 proves any claim. `premises ∧ ¬claim` is unsat because the premises are unsat, so the step came out VALID.
2. **Poisoned pools.** Every earlier step went into the premise pool, including INVALID and UNGROUNDED ones. An unsupported inference `Q` could be restated as a conclusion `Q`, and the conclusion was then "entailed".
3. **Dilution.** Under `(V + 0.5U)/N`, a single INVALID step among many VALID ones still scored at or above `PASS_THRESHOLD` (e.g. 4 VALID + 1 INVALID = 0.8).

`referenced_step_indices` was also ignored, so a step could cite the wrong premises and still be checked against all of them.

## Decision

### Premise pool

- Only **VALID** steps go into the pool: assumed `premise` steps and entailed inferences or conclusions. INVALID and UNGROUNDED steps never support a later step.
- If a step has `referenced_step_indices`, its pool is the cited steps that are VALID, plus `context.facts`. With no references, the pool is every earlier VALID step plus `context.facts`.
- An empty pool gives UNGROUNDED with reason `no_premises`. This covers a step that cites only refuted steps.

### Premise consistency

Before checking entailment, `Z3Solver.check_entailment` checks that the pool alone is satisfiable. If it is unsat, the step is INVALID with reason `premises_inconsistent`. If the result is unknown or times out, the step is UNGROUNDED with reason `timeout`. Both checks share one parse and one solver: the premises are added and checked, then the negated claim is added and checked again.

### Score cap

```
score = (#VALID + 0.5 * #UNGROUNDED) / N
if any step is INVALID: score = min(score, INVALID_SCORE_CAP)
INVALID_SCORE_CAP = RETRY_THRESHOLD - 0.01
```

Only non-premise steps can be INVALID, because premises are assumed. So this is exactly the rule "any refuted inference sinks the trace". The cap sits below `RETRY_THRESHOLD`, not just below `PASS_THRESHOLD`, so a trace with a refuted step routes to escalation rather than another retry (superseded by the SG4 amendment below). It is a `min`, so a trace that already scores lower keeps its lower score.

## Consequences

- On the golden corpus (60 traces) and in the property test, no invalid trace reaches `PASS_THRESHOLD`. The error-injection harness reports a 100% catch rate and 100% precision.
- A trace whose only problem is UNGROUNDED steps can still pass (for example 3 VALID + 1 UNGROUNDED = 0.875). UNGROUNDED means "could not decide", not "refuted". This is a deliberate gap for AUDIT-1 to probe.
- A step that cites only a consistent subset of an otherwise contradictory premise set is checked against that subset alone.
- The cap is computed from the module constant `RETRY_THRESHOLD`, not from `Settings.retry_threshold`. If an operator lowers the retry threshold below 0.39, capped traces would land in the retry band. Callers that override thresholds should check that the cap is still below them.
- Each non-premise step now costs two `check()` calls. On propositional and small arithmetic formulas the cost is negligible. SG2 measures it.

## Amendment (SG4, 2026-10-19): retry first

The owner's default is that a refuted trace is retried with the verifier's feedback before a human sees it. `INVALID_SCORE_CAP` is now `PASS_THRESHOLD - 0.01`: a refuted step still never passes, but a diluted trace lands in the retry band. `VerificationMonitor` (`retry_rework=True`) also routes any trace with an INVALID step or a `no_premises` step (an unbound conclusion, score 0.0) to RETRY whatever its score. It escalates after `max_retries`. To go back to escalate-first, set `retry_rework=False` and the cap back to `RETRY_THRESHOLD - 0.01`.

## Amendment (SG7, 2026-10-19): blocked traces, configured cap, premise checks

AUDIT-1 #1–#6 and AUDIT-3 #1 showed that a score threshold alone cannot keep wrong traces out.

- **Blocked traces never pass.** A verdict is *blocked* when it has an INVALID step or a `no_premises` step, or when its final step (the payload conclusion) is not VALID (`verification_monitor.blocking_reason`). `VerificationMonitor.decide` checks this before the score. `BaseAgent` gates with the same monitor, so the agent and the monitor always agree. Retry-first stays the default: a blocked trace goes to RETRY, then ESCALATE after `max_retries`. `retry_rework=False` routes it by score instead, which can never give PASS.
- **Cap from configured thresholds.** `consistency_score(verdicts, pass_threshold=, retry_threshold=, require_final_valid=)` caps a blocked trace at `score_cap = max(0, min(pass, retry) - 0.01)`. That is below both bands, 0.39 by default. `Verifier` takes both thresholds, and `build_graph` passes `Settings`. `INVALID_SCORE_CAP` is removed. To go escalate-first, set only `retry_rework=False`.
- **Premises are checked.** A premise is assumed only if it is satisfiable together with `context.facts` and every earlier VALID step. Otherwise it is INVALID/`premises_inconsistent`. Premises can therefore be INVALID now.
- **Bound conclusions must be proven, not assumed.** Every step is formalized first. Then, when the payload conclusion is VALID, the verifier checks three things. If the conclusion is a tautology, it becomes UNGROUNDED/`formalization_failed`. A VALID premise whose normalized claim equals the payload claim, or whose formula alone entails the conclusion, becomes UNGROUNDED/`no_premises` (circular), and the trace is re-checked without it. An inconclusive check on either fails closed. Two premises that only jointly entail the conclusion (`P`, `P => C`) are accepted. This is the shape `register_trivial_trace` now uses.
- **Declarations are declarations.** Each line must be exactly one `declare-const` / `declare-fun` / `declare-sort` form (AUDIT-1 #2).

Consequences: each premise now costs one solver check, and a bound trace with a VALID conclusion costs 1 + (#premises) more single-premise checks. These checks are small. The explosion golden traces now flag the contradicting premise itself (overall still INVALID).
