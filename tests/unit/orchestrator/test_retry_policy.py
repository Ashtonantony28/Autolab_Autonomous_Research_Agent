"""SG8: which errors retry or escalate, jitter, `retry-after`, one HTTP request per attempt."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import anthropic
import httpx
import pytest
from anthropic._exceptions import (  # not re-exported from `anthropic`
    DeadlineExceededError,
    OverloadedError,
    RequestTooLargeError,
    ServiceUnavailableError,
)

from autolab.orchestrator.graph import build_graph
from autolab.orchestrator.recovery import (
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_S,
    AgentNode,
    RetryingLLMClient,
    escalate_permanent_errors,
    is_transient,
    retry_after_seconds,
    with_retries,
)
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.llm import ClaudeLLMClient, LLMPurpose, SchemaT, StructuredOutputError

_URL = "https://api.anthropic.com/v1/messages"
_REQUEST = httpx.Request("POST", _URL)

PERMANENT: list[tuple[type[anthropic.APIStatusError], int]] = [
    (anthropic.BadRequestError, 400),
    (anthropic.AuthenticationError, 401),
    (anthropic.PermissionDeniedError, 403),
    (anthropic.NotFoundError, 404),
    (RequestTooLargeError, 413),
    (anthropic.UnprocessableEntityError, 422),
]
TRANSIENT: list[tuple[type[anthropic.APIStatusError], int]] = [
    (anthropic.APIStatusError, 408),
    (anthropic.ConflictError, 409),
    (anthropic.RateLimitError, 429),
    (anthropic.InternalServerError, 500),
    (ServiceUnavailableError, 503),
    (DeadlineExceededError, 504),
    (OverloadedError, 529),
]


def _status_error(
    cls: type[anthropic.APIStatusError], status: int, headers: dict[str, str] | None = None
) -> anthropic.APIStatusError:
    response = httpx.Response(status, headers=headers, request=_REQUEST)
    return cls(f"HTTP {status}", response=response, body=None)


class _Failing:
    """An LLMClient that raises `errors` in turn, then answers "ok"."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        raise NotImplementedError


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Record every backoff `with_retries` sleeps, without sleeping."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    yield recorded


# --- classification -----------------------------------------------------------


@pytest.mark.parametrize(("cls", "status"), PERMANENT)
async def test_permanent_status_errors_are_tried_once(
    cls: type[anthropic.APIStatusError], status: int
) -> None:
    exc = _status_error(cls, status)
    assert not is_transient(exc)
    inner = _Failing(exc)
    with pytest.raises(cls):
        await RetryingLLMClient(inner, initial_backoff_seconds=0.0).call("p", "q")
    assert inner.calls == 1


@pytest.mark.parametrize(("cls", "status"), TRANSIENT)
async def test_transient_status_errors_are_retried(
    cls: type[anthropic.APIStatusError], status: int
) -> None:
    exc = _status_error(cls, status)
    assert is_transient(exc)
    inner = _Failing(exc)
    assert await RetryingLLMClient(inner, initial_backoff_seconds=0.0).call("p", "q") == "ok"
    assert inner.calls == 2


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (anthropic.APIConnectionError(request=_REQUEST), True),
        (anthropic.APITimeoutError(_REQUEST), True),
        (ConnectionError("reset"), True),
        (TimeoutError("slow"), True),
        (anthropic.APIResponseValidationError(httpx.Response(200, request=_REQUEST), None), False),
        (StructuredOutputError("refusal"), False),
        (ValueError("bad"), False),
        (KeyError("missing"), False),
    ],
)
def test_non_status_errors(exc: BaseException, transient: bool) -> None:
    assert is_transient(exc) is transient


def test_x_should_retry_header_overrides_the_status() -> None:
    assert is_transient(_status_error(anthropic.BadRequestError, 400, {"x-should-retry": "true"}))
    assert not is_transient(_status_error(OverloadedError, 529, {"x-should-retry": "false"}))


# --- escalation -----------------------------------------------------------------


def _raising(exc: BaseException) -> AgentNode:
    async def node(state: AutoLabState) -> dict[str, Any]:
        raise exc

    return node


@pytest.mark.parametrize(("cls", "status"), PERMANENT)
async def test_permanent_api_errors_escalate_with_status(
    cls: type[anthropic.APIStatusError], status: int
) -> None:
    update = await escalate_permanent_errors("design", _raising(_status_error(cls, status)))(
        empty_state("q")
    )
    pending = update["hitl_pending"]
    assert pending.agent == "design" and update["hitl_payload"] is None
    assert pending.payload == {"trigger": "error", "error": cls.__name__, "status_code": status}
    assert pending.reason == f"{cls.__name__}: HTTP {status}"


@pytest.mark.parametrize(
    "exc",
    [
        StructuredOutputError("no structured output (stop_reason='refusal')"),
        RuntimeError("no structured output"),
        anthropic.APIResponseValidationError(httpx.Response(200, request=_REQUEST), None),
    ],
)
async def test_refusals_and_bad_responses_escalate(exc: Exception) -> None:
    update = await escalate_permanent_errors("research", _raising(exc))(empty_state("q"))
    assert update["hitl_pending"].payload == {"trigger": "error", "error": type(exc).__name__}
    assert update["confidence"] == 0.0


@pytest.mark.parametrize(("cls", "status"), TRANSIENT)
async def test_exhausted_transient_api_errors_propagate(
    cls: type[anthropic.APIStatusError], status: int
) -> None:
    with pytest.raises(cls):
        await escalate_permanent_errors("design", _raising(_status_error(cls, status)))(
            empty_state("q")
        )


async def test_permanent_api_error_pauses_the_graph() -> None:
    class Rejecting(_Failing):
        async def call_structured(
            self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
        ) -> SchemaT:
            self.calls += 1
            raise _status_error(anthropic.NotFoundError, 404)

    llm = Rejecting()
    graph = build_graph(llm=llm, retry_backoff_seconds=0.0)
    final = await graph.ainvoke(empty_state("q"), {"configurable": {"thread_id": str(uuid4())}})
    assert "__interrupt__" in final and llm.calls == 1
    assert final["hitl_pending"].payload["status_code"] == 404


# --- backoff --------------------------------------------------------------------


def _always(exc: BaseException) -> Callable[[], Any]:
    async def op() -> None:
        raise exc

    return op


async def test_backoff_is_full_jitter_within_bounds(sleeps: list[float]) -> None:
    attempts, base, clients = 5, 1.0, 40
    for _ in range(clients):
        with pytest.raises(ConnectionError):
            await with_retries(
                _always(ConnectionError()), max_attempts=attempts, initial_backoff_seconds=base
            )
    rounds = [sleeps[i : i + attempts - 1] for i in range(0, len(sleeps), attempts - 1)]
    assert len(rounds) == clients
    for waits in rounds:
        assert all(0.0 <= w <= base * 2**k for k, w in enumerate(waits))
    first = [waits[0] for waits in rounds]
    assert len(set(first)) == clients  # no two clients wait the same time
    assert max(first) - min(first) > 0.5 * base


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "2"}, 2.0),
        ({"retry-after": "0.25"}, 0.25),
        ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
        ({"retry-after": "3600"}, MAX_RETRY_AFTER_S),
        ({"retry-after": "-1"}, 0.0),
        ({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, None),
        ({}, None),
    ],
)
def test_retry_after_parsing(headers: dict[str, str], expected: float | None) -> None:
    assert retry_after_seconds(_status_error(anthropic.RateLimitError, 429, headers)) == expected


def test_retry_after_needs_an_http_error() -> None:
    assert retry_after_seconds(ConnectionError()) is None


async def test_retry_after_is_a_floor_on_the_jittered_backoff(sleeps: list[float]) -> None:
    exc = _status_error(OverloadedError, 529, {"retry-after": "2"})
    inner = _Failing(exc, exc)
    assert await RetryingLLMClient(inner, initial_backoff_seconds=0.01).call("p", "q") == "ok"
    assert sleeps == [2.0, 2.0]


# --- one HTTP request per attempt -------------------------------------------------

_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-6",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


async def _http_attempts(
    monkeypatch: pytest.MonkeyPatch, statuses: list[int]
) -> tuple[list[int], str | BaseException]:
    """Run one logical `RetryingLLMClient(ClaudeLLMClient)` call against canned statuses."""
    hits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(hits), len(statuses) - 1)]
        hits.append(status)
        if status == 200:
            return httpx.Response(200, json=_MESSAGE)
        return httpx.Response(status, json={"type": "error", "error": {"message": "m"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real = anthropic.AsyncAnthropic

    def with_mock_transport(**kwargs: Any) -> anthropic.AsyncAnthropic:
        return real(**kwargs, http_client=http)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", with_mock_transport)
    client = RetryingLLMClient(ClaudeLLMClient("sk-test"), initial_backoff_seconds=0.0)
    try:
        outcome: str | BaseException = await client.call("commonsense", "hi")
    except anthropic.APIError as exc:
        outcome = exc
    finally:
        await http.aclose()
    return hits, outcome


async def test_overloaded_then_ok_makes_two_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    hits, outcome = await _http_attempts(monkeypatch, [529, 200])
    assert hits == [529, 200] and outcome == "ok"


async def test_persistent_overload_makes_one_request_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits, outcome = await _http_attempts(monkeypatch, [529])
    assert hits == [529] * MAX_ATTEMPTS
    assert isinstance(outcome, OverloadedError)


async def test_permanent_error_makes_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    hits, outcome = await _http_attempts(monkeypatch, [400])
    assert hits == [400] and isinstance(outcome, anthropic.BadRequestError)
