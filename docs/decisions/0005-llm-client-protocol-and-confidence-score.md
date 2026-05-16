# ADR-0005: LLM client protocol & consistency_score definition

- **Status:** Accepted
- **Date:** 2026-05-16

## Context

The VeriCoT formalizer needs to call an LLM to translate NL → SMT-LIB. Two questions:

1. **What's the seam?** Bind directly to `anthropic.AsyncAnthropic`, or introduce a protocol?
2. **How do we aggregate per-step verdicts** into the `consistency_score ∈ [0,1]` that VerificationResult carries? The brief specifies the range but not the formula.

## Decision

### LLM client protocol

We define a minimal `LLMClient` protocol in `verification/llm.py` with a single async method `call(purpose, prompt) -> str`. Phase 2 ships:

- `FakeLLMClient` — dict-backed lookup, used by all tests.
- `ClaudeLLMClient` — real adapter, deferred to Phase 3 when agents actually need it.

Rationale: tests must run without network or API keys; binding directly to `anthropic.AsyncAnthropic` would tangle the verifier to the SDK and force every test to mock that surface area. The protocol is narrow enough that the future Claude adapter is a thin wrapper.

`purpose` is a `Literal["formalize", "commonsense"]` so different call sites can use distinct system prompts on the real path without changing the protocol.

### consistency_score formula

```
score = (#VALID + 0.5 * #UNGROUNDED) / N
```

INVALID contributes 0. UNGROUNDED contributes 0.5 because "we couldn't prove it either way" is meaningfully weaker than VALID but stronger than INVALID. Empty traces score 0.0 with an explicit summary string.

Alternative considered: a Bayesian model with prior beliefs per step. Rejected for now because (a) the brief doesn't ask for it, (b) the simple formula composes well with the orchestrator's `< 0.7` HITL threshold, and (c) we can swap implementations later without changing the public API.

## Consequences

- Real Anthropic integration lands in Phase 3 without touching the verifier.
- The 0.5 weight for UNGROUNDED is a tuning knob; we'll revisit if Phase 7's benchmark shows it under/over-flags HITL escalations.
- If the eventual real LLM produces malformed SMT, the formalizer raises `FormalizationError` and the verifier records an `UNGROUNDED` verdict for that step rather than crashing the whole trace.
