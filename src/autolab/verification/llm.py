"""LLM client abstraction used by the verifier.

The formalizer and premise grounder need to call an LLM to translate
natural-language reasoning steps into SMT-LIB and to surface commonsense
premises. We hide the concrete provider behind a small protocol so tests
can inject a deterministic fake and so the codebase isn't married to a
specific SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from autolab.state.models import AnalysisReport, ExperimentDesign, Hypothesis, ReasoningStep

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


_TRACE_RULES = (
    "When asked for a reasoning trace, every step is formalized into first-order logic and "
    "checked by the Z3 solver, so write short, atomic, explicit claims, one proposition per "
    "step. `premise` steps state facts you rely on; `inference` and `conclusion` steps must "
    "follow strictly from the steps listed in `referenced_step_indices`. Do not skip steps "
    "and do not hedge. The final step is the conclusion you are asked to state, verbatim."
)

_AGENT_ROLES: Final[dict[str, str]] = {
    "research": (
        "You are the Research agent of AutoLab, an autonomous laboratory platform. Propose one "
        "testable, falsifiable hypothesis for the research question, grounded in real "
        "literature. Cite only sources you are confident exist; never invent identifiers."
    ),
    "design": (
        "You are the Design agent of AutoLab. Turn the given hypothesis into one concrete, "
        "safe experiment: protocol, parameters with units, honest cost (USD) and duration "
        "(minutes) estimates, and success criteria. Copy the given hypothesis_id exactly."
    ),
    "execute": (
        "You are the Execute agent of AutoLab. The experiment has already been run on the lab "
        "simulator. Explain what was run and what the measurements show; state measured values "
        "exactly as given and never invent data."
    ),
    "analysis": (
        "You are the Analysis agent of AutoLab. Interpret the experiment result with several "
        "ranked candidate interpretations (rank 0 is the most likely) and likelihoods in "
        "[0, 1]. Copy the given result_id exactly."
    ),
}

SYSTEM_PROMPTS: Final[dict[str, str]] = {
    "formalize": (
        "You translate one natural-language reasoning step into SMT-LIB 2 for the Z3 solver. "
        "`declarations` holds only new declare-const / declare-fun / declare-sort lines, one "
        "per line; never re-declare a symbol from the existing vocabulary, and reuse its "
        "symbols for the same concepts. `assertion` is a single Bool term without the "
        "surrounding (assert ...). Encode exactly what the claim states: do not add facts from "
        "other steps or background knowledge, and do not repair a claim that looks wrong. Use "
        "Bool for propositions, Int for whole quantities, Real for measurements, uninterpreted "
        "functions for predicates over individuals, and forall/exists for quantified claims. "
        "Ignore discourse markers such as 'therefore'."
    ),
    "faithfulness": (
        "You audit an SMT-LIB formalization of a natural-language claim. First translate the "
        "SMT assertion into plain English from the SMT alone (the declarations give the "
        "symbols' sorts) and put it in `back_translation`. Then set `faithful` to true only if "
        "it states the same proposition as the original claim: same quantifiers, the same "
        "direction of every inequality and implication, the same constants, nothing added or "
        "dropped. Ignore wording and discourse markers such as 'therefore'. Explain briefly."
    ),
    **{name: f"{role}\n\n{_TRACE_RULES}" for name, role in _AGENT_ROLES.items()},
}
"""System prompt per purpose family (the part of the purpose before the first `.`)."""

DEFAULT_SYSTEM_PROMPT: Final[str] = (
    "You are a careful scientific reasoning assistant inside AutoLab. Answer precisely."
)


class _TraceReply(BaseModel):
    steps: tuple[ReasoningStep, ...]


_REPLY_SCHEMAS: Final[dict[str, type[BaseModel]]] = {
    "research.hypothesis": Hypothesis,
    "design.experiment": ExperimentDesign,
    "analysis.report": AnalysisReport,
}


def system_prompt(purpose: LLMPurpose) -> str:
    """Return the system prompt for `purpose` (looked up by its family)."""
    return SYSTEM_PROMPTS.get(purpose.split(".", 1)[0], DEFAULT_SYSTEM_PROMPT)


def reply_schema(purpose: LLMPurpose) -> type[BaseModel] | None:
    """The schema a JSON-replying `call` purpose must satisfy, if any."""
    return _TraceReply if purpose.endswith(".trace") else _REPLY_SCHEMAS.get(purpose)


def is_live(llm: object) -> bool:
    """True if `llm` (or the client it wraps) calls a real model."""
    return getattr(llm, "live", False) is True


class ClaudeLLMClient:
    """Async adapter over one shared `anthropic.AsyncAnthropic` client.

    Every request carries the purpose's system prompt. `call_structured` uses
    the SDK's structured-output path (`messages.parse(output_format=schema)`),
    which constrains decoding to the schema and validates the reply with
    Pydantic. `call` routes the agents' JSON purposes (see `reply_schema`)
    through the same path and returns the validated JSON, so no agent parses
    free-form prose. Tests only construct it with a stub client; real calls
    are gated on `RUN_LIVE_TESTS=1` (`tests/live/`).
    """

    live: Final = True
    """Marks a real-model client: `Verifier` turns the faithfulness check on by default."""

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
        """Return Claude's text reply, or the validated JSON for a `reply_schema` purpose."""
        schema = reply_schema(purpose)
        if schema is not None:
            return (await self.call_structured(purpose, prompt, schema)).model_dump_json()
        # Lazy import so the verification module doesn't pay the anthropic
        # import cost when only FakeLLMClient is in use.
        from anthropic.types import TextBlock

        response = await self._get_client().messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_prompt(purpose),
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if isinstance(block, TextBlock):
                return block.text
        raise RuntimeError(f"Claude returned no text content for purpose={purpose!r}")

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        """Return a `schema` instance via the API's structured-output mode.

        Raises:
            pydantic.ValidationError: if the reply does not validate.
            RuntimeError: if the reply has no structured output (e.g. a refusal).
        """
        response = await self._get_client().messages.parse(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_prompt(purpose),
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        if response.parsed_output is None:
            raise RuntimeError(f"Claude returned no structured output for purpose={purpose!r}")
        return response.parsed_output


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "SYSTEM_PROMPTS",
    "ClaudeLLMClient",
    "FakeLLMClient",
    "LLMClient",
    "LLMPurpose",
    "SchemaT",
    "is_live",
    "reply_schema",
    "system_prompt",
]
