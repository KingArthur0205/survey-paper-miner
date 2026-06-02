"""
OpenAlex retriever.

API docs: https://docs.openalex.org/

Notes:
- OpenAlex is fully open and free; no API key required for polite use.
- Providing your email as a "polite pool" header unlocks higher rate limits.
- Citation counts (cited_by_count) are kept up-to-date daily by OpenAlex.
- All metadata is returned verbatim from OpenAlex — nothing is invented.
- DOIs and external IDs (arXiv) are normalised where available.
"""

from __future__ import annotations

import logging
import time

from ..models import Paper
from .base import BaseRetriever

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.openalex.org/works"

# OpenAlex "mailto" polite pool — reduces rate limit risk
_MAILTO = "survey-miner@example.com"


class OpenAlexRetriever(BaseRetriever):
    source_name = "openalex"

    def _fetch(
        self,
        query: str,
        topic: str,
        year_from: int,
        year_to: int,
        limit: int,
    ) -> list[Paper]:
        params: dict = {
            "search": query,
            "filter": f"publication_year:{year_from}-{year_to}",
            "per-page": min(limit, 200),  # OpenAlex max is 200
            # Use relevance_score so each query surfaces its best *semantic*
            # match rather than the globally most-cited papers in the date
            # window.  cited_by_count would cause every query to return the
            # same handful of mega-cited papers, making deduplication collapse
            # 10 queries × 50 results into ~50 unique papers instead of ~400.
            "sort": "relevance_score:desc",
            "select": (
                "id,title,publication_year,authorships,primary_location,"
                "abstract_inverted_index,doi,ids,cited_by_count,"
                "open_access,best_oa_location"
            ),
            "mailto": _MAILTO,
        }

        data = self._get(_SEARCH_URL, params=params)

        papers = []
        for item in data.get("results", []):
            paper = _parse_item(item, topic, query)
            if paper:
                papers.append(paper)

        time.sleep(0.2)

        logger.info(
            "[openalex] query=%r  returned %d papers", query, len(papers)
        )
        return papers


def _parse_item(item: dict, topic: str, query: str) -> Paper | None:
    title = (item.get("title") or "").strip()
    if not title:
        return None

    year = item.get("publication_year")

    # Authors
    authors = []
    for authorship in item.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name", "").strip()
        if name:
            authors.append(name)

    # Venue name from primary_location → source
    venue: str | None = None
    primary_loc = item.get("primary_location") or {}
    source = primary_loc.get("source") or {}
    if source:
        venue = source.get("display_name")

    # Abstract: OpenAlex stores an inverted index; reconstruct the text
    abstract: str | None = None
    inverted = item.get("abstract_inverted_index")
    if inverted:
        abstract = _reconstruct_abstract(inverted)

    # DOI (OpenAlex returns "https://doi.org/10.xxx")
    doi_raw = item.get("doi") or ""
    doi = doi_raw.replace("https://doi.org/", "").strip() or None

    # arXiv ID from ids dict
    ids = item.get("ids") or {}
    arxiv_raw = ids.get("arxiv") or ""
    arxiv_id: str | None = None
    if arxiv_raw:
        # Format: "https://arxiv.org/abs/2303.12345"
        arxiv_id = arxiv_raw.rstrip("/").split("/")[-1]
        if "v" in arxiv_id:
            arxiv_id = arxiv_id.split("v")[0]

    # Open-access PDF
    pdf_url: str | None = None
    best_oa = item.get("best_oa_location") or {}
    pdf_url = best_oa.get("pdf_url")

    openalex_id = item.get("id") or ""

    return Paper(
        title=title,
        year=year,
        authors=authors,
        venue=venue,
        abstract=abstract,
        doi=doi,
        arxiv_id=arxiv_id,
        url=openalex_id if openalex_id else None,
        pdf_url=pdf_url,
        citation_count=item.get("cited_by_count") or 0,
        influential_citation_count=0,  # OpenAlex doesn't distinguish influential citations
        sources=["openalex"],
        topic_queries=[topic],
        generated_queries=[query],
    )


def _reconstruct_abstract(inverted_index: dict) -> str:
    """
    OpenAlex stores abstracts as {word: [position, ...]} dicts.
    This rebuilds the original text.
    """
    words: dict[int, str] = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    if not words:
        return ""
    return " ".join(words[i] for i in sorted(words))
