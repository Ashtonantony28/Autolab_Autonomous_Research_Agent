"""LLM client abstraction used by the verifier.

The formalizer and premise grounder need to call an LLM to translate
natural-language reasoning steps into SMT-LIB and to surface commonsense
premises. We hide the concrete provider behind a small protocol so tests
can inject a deterministic fake and so the codebase isn't married to a
specific SDK.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

LLMPurpose = Literal["formalize", "commonsense"]


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


__all__ = ["FakeLLMClient", "LLMClient", "LLMPurpose"]
