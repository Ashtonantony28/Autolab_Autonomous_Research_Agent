"""LLM client abstraction used by the verifier.

The formalizer and premise grounder need to call an LLM to translate
natural-language reasoning steps into SMT-LIB and to surface commonsense
premises. We hide the concrete provider behind a small protocol so tests
can inject a deterministic fake and so the codebase isn't married to a
specific SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

# A short stable string identifying the call site (e.g. "formalize",
# "commonsense", "research.hypothesis"). Used for prompt routing and
# fake-LLM lookup keys.
LLMPurpose = str

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@runtime_checkable
class LLMClient(Protocol):
    """A minimal async LLM interface for verification-time calls."""

    async def call(self, purpose: LLMPurpose, prompt: str) -> str: ...

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT: ...


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

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        """Validate the registered JSON response against `schema`."""
        return schema.model_validate_json(await self.call(purpose, prompt))


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
        client: AsyncAnthropic | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ClaudeLLMClient requires a non-empty api_key")
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._client = client

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        # Lazy import so the verification module doesn't pay the anthropic
        # import cost when only FakeLLMClient is in use.
        from anthropic.types import TextBlock

        response = await self._get_client().messages.create(
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

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        """Return a `schema` instance via the API's structured-output mode."""
        response = await self._get_client().messages.parse(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": f"[purpose={purpose}]\n\n{prompt}"}],
            output_format=schema,
        )
        if response.parsed_output is None:
            raise RuntimeError(f"Claude returned no structured output for purpose={purpose!r}")
        return response.parsed_output


__all__ = ["ClaudeLLMClient", "FakeLLMClient", "LLMClient", "LLMPurpose", "SchemaT"]
