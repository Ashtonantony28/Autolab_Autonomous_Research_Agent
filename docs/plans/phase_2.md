# Phase 2 — VeriCoT Verification Engine ⭐ Plan

**Goal:** A Z3-backed reasoning verifier that classifies each step of a `ReasoningTrace` as `VALID` / `INVALID` / `UNGROUNDED` and aggregates them into a `VerificationResult` with `consistency_score ∈ [0,1]`. TDD-first. ≥90% coverage on `verification/`. All 20 golden traces classified correctly.

**Out of scope (later phases):** real Claude API integration (we'll define a small LLM client protocol and inject a fake in tests); orchestrator routing; agent integration.

---

## 1. Architectural shape

The pipeline (CLAUDE.md §6 Phase 2):

```
ReasoningTrace
   │
   ▼
extract steps  ──►  formalize (NL → SMT-LIB)  ──►  ground premises  ──►  Z3 entailment check  ──►  StepVerdict
                              │
                              ▼
                   growing vocabulary across steps (Pattern C)
```

### Modules

- `verification/llm.py` — *new*, not in CLAUDE.md §5 but needed for testability. Defines `LLMClient` protocol with one method: `async def formalize(prompt: str) -> str` returning SMT-LIB. Two implementations: `FakeLLMClient` (lookup table for tests) and `ClaudeLLMClient` (real, exercised only under `RUN_LIVE_TESTS=1`).
- `verification/formalizer.py` — Given a `ReasoningStep` + accumulated vocabulary, produces `SmtFormula` (an explicit SMT-LIB string + declared symbols). Uses the `LLMClient`. Maintains the growing vocabulary per Pattern C.
- `verification/premise_grounder.py` — Given a step + the source `AutoLabState`, identifies supporting premises: (a) text in source context, (b) prior verified steps, (c) "commonsense" (queried from LLM as a separate call in the live path; in fake mode, lookup). Produces a list of `Premise` records.
- `verification/solver.py` — `Z3Solver` wraps Z3: `check_entailment(premises: list[SmtFormula], claim: SmtFormula) -> Verdict`. Returns:
  - `VALID` if `premises ⊨ claim` (i.e. `premises ∧ ¬claim` is `unsat`).
  - `INVALID` if `premises ∧ ¬claim` is `sat`.
  - `UNGROUNDED` if there are no premises (or only commonsense premises and the LLM declined to commit), or Z3 returns `unknown`.
- `verification/confidence.py` — `aggregate(step_verdicts) -> consistency_score`. Simple definition: `(#VALID + 0.5 × #UNGROUNDED) / N`. Documented in module + ADR.
- `verification/vericot.py` — public `Verifier.verify(trace, context=None) -> VerificationResult` orchestrating the pipeline.

### Public API

```python
class Verifier:
    def __init__(self, llm: LLMClient, *, solver_timeout_seconds: float = 5.0): ...
    async def verify(
        self,
        trace: ReasoningTrace,
        *,
        context: AutoLabState | None = None,
    ) -> VerificationResult: ...
```

The `Verifier` is the single entry point. Agents (Phase 3) will hold one and call it on their output traces.

---

## 2. Tests (TDD — write these before implementation)

### Unit tests (`tests/unit/verification/`)

- `test_solver.py` — direct Z3 wrapper tests:
  - Tautology (`A ⊨ A`) → VALID.
  - Modus ponens (`{P, P→Q} ⊨ Q`) → VALID.
  - Contradictory premises detected.
  - Empty premise set → UNGROUNDED.
  - Solver timeout returns UNGROUNDED rather than raising.
  - Arithmetic example (`x > 0 ⊨ x + 1 > 1`) → VALID.
  - Quantifier example.

- `test_formalizer.py` — vocabulary growth, schema validation:
  - Formalizer accumulates predicates across calls.
  - When fake LLM returns malformed SMT-LIB, formalizer raises a typed error (not silently swallows).
  - Symbol renaming preserved between steps.

- `test_premise_grounder.py` — premise extraction:
  - Premise step's claim becomes available to later steps.
  - Source-context grounding pulls from the supplied state.
  - Commonsense premises labelled distinctly.

- `test_confidence.py` — aggregation:
  - All-VALID → 1.0.
  - All-INVALID → 0.0.
  - Mix produces the right weighted score.
  - Empty trace → 0.0 with explicit summary.

### Integration tests (`tests/integration/verification/`)

- `test_vericot_pipeline.py` — end-to-end with `FakeLLMClient`:
  - Trace with one premise + one inference referencing it + one conclusion: VALID/VALID/VALID, score 1.0.
  - Trace where step 2 contradicts step 1: INVALID at step 2, score < 1.
  - Trace with ungrounded step: UNGROUNDED, score reflects partial.
  - Edge: empty steps tuple → ValidationError (Pydantic catches before us, but verify it's surfaced cleanly).
  - Edge: single step trace, claim_type="conclusion" with no refs → UNGROUNDED.

### Golden traces (`tests/fixtures/golden_traces/`)

Twenty hand-crafted YAML files: 10 valid, 10 invalid. Each file:

```yaml
id: golden-001
description: Modus ponens about temperature thresholds.
expected_overall: VALID    # or INVALID
expected_step_verdicts: [VALID, VALID, VALID]
trace:
  agent: research
  steps:
    - {index: 0, claim: "...", claim_type: premise}
    - ...
fake_formalizations:
  - {step_index: 0, smt: "(declare-const T Real)"}
  - ...
```

A `tests/integration/verification/test_golden_traces.py` loads each YAML, builds a `FakeLLMClient` from `fake_formalizations`, runs the verifier, and asserts the per-step and overall verdicts match.

This is the headline DoD check.

### Performance

- `test_perf_10_steps.py` — a 10-step trace verifies in < 30 s on the test machine. (Loose budget; matters mostly for the real-LLM path.)

---

## 3. Concrete tasks (in TDD order)

1. **Tests scaffolding.** Create `tests/unit/verification/__init__.py` and `tests/integration/verification/__init__.py`. Add `pyyaml` dependency for golden-trace loading; justify in pyproject.
2. **`verification/llm.py`** — `LLMClient` protocol + `FakeLLMClient` + `ClaudeLLMClient` stub. Tests for `FakeLLMClient` lookup semantics.
3. **`verification/solver.py`** — Z3 wrapper. Tests first. Z3 raw API is fiddly; use `z3.Solver`, `set("timeout", ms)`, parse SMT-LIB via `z3.parse_smt2_string` for safety.
4. **`verification/formalizer.py`** — formalizer with growing vocab. Tests first.
5. **`verification/premise_grounder.py`** — grounding. Tests first.
6. **`verification/confidence.py`** — aggregation. Tests first.
7. **`verification/vericot.py`** — assemble pipeline. Integration tests.
8. **Golden traces** — write 20, then run the suite. Iterate until all pass.
9. **Coverage gate** — `pytest --cov=autolab.verification --cov-fail-under=90`.
10. ADR-0005 documenting the `LLMClient` protocol shape and why we don't bind to `anthropic.AsyncAnthropic` directly.

---

## 4. Definition of Done

1. ≥90% coverage on `autolab.verification`.
2. All 20 golden traces classified correctly (10 VALID, 10 INVALID; per-step verdicts also match).
3. `make lint typecheck test` all green.
4. `Verifier.verify` returns a well-formed `VerificationResult` on every trace, including edge cases (empty, single-step, ungrounded).
5. No live Anthropic calls — `FakeLLMClient` everywhere unless `RUN_LIVE_TESTS=1`.
6. ADR-0005 explains the LLMClient protocol decision.

---

## 5. Risks

- **`parse_smt2_string` is strict.** Malformed SMT from the formalizer will raise; we catch and degrade to `UNGROUNDED` with details, rather than crashing the verifier.
- **20 golden traces is real authoring work.** Mitigation: use a small templating helper that generates trivially-VALID and trivially-INVALID skeletons, then hand-edit ~10 to add quantifier / arithmetic complexity.
- **Vocabulary growth across steps** is easy to get subtly wrong (re-declarations break Z3). Each step's SMT is concatenated **prepended by the cumulative declarations**, and the formalizer is forbidden from re-declaring a symbol. Enforced by parsing on each step.
- **Confidence aggregation is a choice, not a derivation.** Documented in ADR-0005 alongside an alternative (Bayesian) approach we're not taking yet.
