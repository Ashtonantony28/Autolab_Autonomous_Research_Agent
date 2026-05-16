"""LiteratureClient parses arxiv XML + PubMed JSON without hitting the network."""

from __future__ import annotations

import json

import httpx
import pytest

from autolab.tools.literature import (
    ARXIV_API,
    PUBMED_ESEARCH,
    PUBMED_ESUMMARY,
    LiteratureClient,
    LiteratureError,
)

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.0001v1</id>
    <title>A neat paper on X</title>
    <published>2024-01-15T00:00:00Z</published>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2402.0002v2</id>
    <title>Another result</title>
    <published>2024-02-03T00:00:00Z</published>
  </entry>
</feed>
"""

PUBMED_ESEARCH_JSON = {"esearchresult": {"idlist": ["111", "222"]}}
PUBMED_ESUMMARY_JSON = {
    "result": {
        "111": {"title": "Paper One", "pubdate": "2023 Jan"},
        "222": {"title": "Paper Two", "pubdate": "2024"},
    }
}


def _client(handler: httpx.MockTransport) -> LiteratureClient:
    return LiteratureClient(http=httpx.AsyncClient(transport=handler))


async def test_search_arxiv_parses_atom_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/query"
        return httpx.Response(200, text=ARXIV_XML)

    cite = await _client(httpx.MockTransport(handler)).search_arxiv("X")
    assert len(cite) == 2
    assert cite[0].source == "arxiv"
    assert cite[0].identifier == "2401.0001v1"
    assert cite[0].title == "A neat paper on X"
    assert cite[0].year == 2024
    assert cite[1].identifier == "2402.0002v2"


async def test_search_arxiv_handles_empty_feed() -> None:
    empty = '<?xml version="1.0"?>\n<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    handler = httpx.MockTransport(lambda _r: httpx.Response(200, text=empty))
    assert await _client(handler).search_arxiv("nope") == ()


async def test_search_arxiv_raises_on_invalid_xml() -> None:
    handler = httpx.MockTransport(lambda _r: httpx.Response(200, text="not xml"))
    with pytest.raises(LiteratureError):
        await _client(handler).search_arxiv("q")


async def test_search_arxiv_propagates_http_errors() -> None:
    handler = httpx.MockTransport(lambda _r: httpx.Response(500, text="boom"))
    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).search_arxiv("q")


async def test_search_pubmed_two_call_flow() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.host == httpx.URL(PUBMED_ESEARCH).host and "esearch" in request.url.path:
            return httpx.Response(200, content=json.dumps(PUBMED_ESEARCH_JSON))
        if "esummary" in request.url.path:
            return httpx.Response(200, content=json.dumps(PUBMED_ESUMMARY_JSON))
        return httpx.Response(404)

    cites = await _client(httpx.MockTransport(handler)).search_pubmed("y")
    assert [c.identifier for c in cites] == ["111", "222"]
    assert cites[0].source == "pubmed"
    assert cites[0].year == 2023
    assert cites[1].year == 2024
    assert any("esearch" in c for c in calls) and any("esummary" in c for c in calls)


async def test_search_pubmed_short_circuits_when_no_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            return httpx.Response(200, content=json.dumps({"esearchresult": {"idlist": []}}))
        return httpx.Response(500)  # should not be called

    assert await _client(httpx.MockTransport(handler)).search_pubmed("q") == ()


def test_endpoints_are_https_or_arxiv_http() -> None:
    # PubMed must be https; arxiv historically uses http for the API.
    assert PUBMED_ESEARCH.startswith("https://")
    assert PUBMED_ESUMMARY.startswith("https://")
    assert ARXIV_API.startswith("http")
