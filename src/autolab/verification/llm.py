"""LLM client abstraction used by the verifier.

The formalizer and premise grounder need to call an LLM to translate
natural-language reasoning steps into SMT-LIB and to surface commonsense
premises. We hide the concrete provider behind a small protocol so tests
can inject a deterministic fake and so the codebase isn't married to a
specific SDK.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# A short stable string identifying the call site (e.g. "formalize",
# "commonsense", "research.hypothesis"). Used for prompt routing and
# fake-LLM lookup keys.
LLMPurpose = str


@runtime_checkable
class LLMClient(Protocol):
    """A minimal async LLM interface for verification-time calls."""

    async def call(self, purpose: LLMPurpose, prompt: str) -> str: ...


class FakeLLMClient:
    """A deterministic lookup-table LLM, used in unit and integration tests.

    Lookups are keyed by `(purpose, prompt)`. Misses raise `KeyError` so
    tests fail loudly when they accidentally depend on an LLM call they
    didn't stub.
    """

    def __init__(self, responses: dict[tuple[LLMPurpose, str], str] | None = None) -> None:
        self._responses: dict[tuple[LLMPurpose, str], str] = dict(responses or {})

    def register(self, purpose: LLMPurpose, prompt: str, response: str) -> None:
        self._responses[(purpose, prompt)] = response

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        key = (purpose, prompt)
        if key not in self._responses:
            raise KeyError(f"FakeLLMClient: no response registered for {key!r}")
        return self._responses[key]


class ClaudeLLMClient:
    """Thin async adapter over `anthropic.AsyncAnthropic`.

    Constructed but never invoked during unit/integration tests — every
    test path uses `FakeLLMClient`. The `RUN_LIVE_TESTS=1` env flag gates
    any test that actually calls Claude (none in Phase 3).
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
    ) -> None:
        if not api_key:
            raise ValueError("ClaudeLLMClient requires a non-empty api_key")
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        # Lazy import so the verification module doesn't pay the anthropic
        # import cost when only FakeLLMClient is in use.
        from anthropic import AsyncAnthropic
        from anthropic.types import TextBlock

        client = AsyncAnthropic(api_key=self._api_key)
        response = await client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[
                {"role": "user", "content": f"[purpose={purpose}]\n\n{prompt}"},
            ],
        )
        for block in response.content:
            if isinstance(block, TextBlock):
                return block.text
        raise RuntimeError(f"Claude returned no text content for purpose={purpose!r}")


__all__ = ["ClaudeLLMClient", "FakeLLMClient", "LLMClient", "LLMPurpose"]
