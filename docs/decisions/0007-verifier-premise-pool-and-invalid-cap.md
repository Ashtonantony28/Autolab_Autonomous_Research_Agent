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

Only non-premise steps can be INVALID, because premises are assumed. So this is exactly the rule "any refuted inference sinks the trace". The cap sits below `RETRY_THRESHOLD`, not just below `PASS_THRESHOLD`, so a trace with a refuted step routes to escalation rather than another retry. It is a `min`, so a trace that already scores lower keeps its lower score.

## Consequences

- On the golden corpus (60 traces) and in the property test, no invalid trace reaches `PASS_THRESHOLD`. The error-injection harness reports a 100% catch rate and 100% precision.
- A trace whose only problem is UNGROUNDED steps can still pass (for example 3 VALID + 1 UNGROUNDED = 0.875). UNGROUNDED means "could not decide", not "refuted". This is a deliberate gap for AUDIT-1 to probe.
- A step that cites only a consistent subset of an otherwise contradictory premise set is checked against that subset alone.
- The cap is computed from the module constant `RETRY_THRESHOLD`, not from `Settings.retry_threshold`. If an operator lowers the retry threshold below 0.39, capped traces would land in the retry band. Callers that override thresholds should check that the cap is still below them.
- Each non-premise step now costs two `check()` calls. On propositional and small arithmetic formulas the cost is negligible. SG2 measures it.
