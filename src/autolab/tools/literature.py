"""Async clients for arxiv and PubMed literature search.

Both clients take an injected `httpx.AsyncClient` so tests can use
`httpx.MockTransport`. The container has no outbound network, so live
tests are gated on `RUN_LIVE_TESTS=1` and only documented here, not run
by default.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx

from autolab.state.models import Citation

ARXIV_API = "http://export.arxiv.org/api/query"
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


class LiteratureError(RuntimeError):
    """Raised when a literature query returns an unparseable response."""


@dataclass
class LiteratureClient:
    """Async client for arxiv + PubMed (NCBI E-utilities)."""

    http: httpx.AsyncClient

    async def search_arxiv(self, query: str, *, max_results: int = 5) -> tuple[Citation, ...]:
        params: dict[str, str | int] = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
        }
        response = await self.http.get(ARXIV_API, params=params)
        response.raise_for_status()
        return _parse_arxiv_atom(response.text)

    async def search_pubmed(self, query: str, *, max_results: int = 5) -> tuple[Citation, ...]:
        search_params: dict[str, str | int] = {
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
        }
        search_resp = await self.http.get(PUBMED_ESEARCH, params=search_params)
        search_resp.raise_for_status()
        ids = _extract_pubmed_ids(search_resp.json())
        if not ids:
            return ()

        summary_params: dict[str, str] = {
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "json",
        }
        summary_resp = await self.http.get(PUBMED_ESUMMARY, params=summary_params)
        summary_resp.raise_for_status()
        return _parse_pubmed_summaries(summary_resp.json(), ids)


def _parse_arxiv_atom(xml_text: str) -> tuple[Citation, ...]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise LiteratureError(f"arxiv response is not valid XML: {exc}") from exc

    citations: list[Citation] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title_el = entry.find(f"{_ATOM_NS}title")
        id_el = entry.find(f"{_ATOM_NS}id")
        published_el = entry.find(f"{_ATOM_NS}published")
        if title_el is None or id_el is None or id_el.text is None:
            continue
        url = id_el.text.strip()
        identifier = url.rsplit("/", 1)[-1]
        year = _year_from_iso(published_el.text if published_el is not None else None)
        citations.append(
            Citation(
                source="arxiv",
                identifier=identifier,
                title=(title_el.text or "").strip(),
                url=url,
                year=year,
            )
        )
    return tuple(citations)


def _extract_pubmed_ids(payload: dict[str, Any]) -> list[str]:
    return list(payload.get("esearchresult", {}).get("idlist", []) or [])


def _parse_pubmed_summaries(payload: dict[str, Any], ids: list[str]) -> tuple[Citation, ...]:
    result = payload.get("result", {})
    citations: list[Citation] = []
    for pmid in ids:
        record = result.get(pmid)
        if not record:
            continue
        title = record.get("title", "").strip() or "<no title>"
        year = _year_from_pubmed_date(record.get("pubdate"))
        citations.append(
            Citation(
                source="pubmed",
                identifier=pmid,
                title=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                year=year,
            )
        )
    return tuple(citations)


def _year_from_iso(text: str | None) -> int | None:
    if not text or len(text) < 4 or not text[:4].isdigit():
        return None
    return int(text[:4])


def _year_from_pubmed_date(text: str | None) -> int | None:
    if not text:
        return None
    head = text.split(" ", 1)[0]
    return int(head) if head.isdigit() else None


__all__ = ["LiteratureClient", "LiteratureError"]
