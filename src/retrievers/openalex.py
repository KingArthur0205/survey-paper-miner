"""
OpenAlex retriever.

API docs: https://docs.openalex.org/

Notes:
- OpenAlex is fully open and free; no API key required for polite use.
- Providing your email as a "polite pool" header unlocks higher rate limits.
- Citation counts (cited_by_count) are kept up-to-date daily by OpenAlex.
- All metadata is returned verbatim from OpenAlex — nothing is invented.
- DOIs and external IDs (arXiv) are normalised where available.
- OpenAlex caps results at 200 per page.  When limit > 200 this retriever
  uses cursor-based pagination to collect up to `limit` papers across
  multiple requests.
"""

from __future__ import annotations

import logging
import time

from ..models import Paper
from .base import BaseRetriever

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.openalex.org/works"
_PAGE_SIZE   = 200        # OpenAlex hard maximum per request
_MAILTO      = "survey-miner@example.com"

# Polite delay between paginated requests (seconds).
# OpenAlex asks for ≤ 10 req/s in the polite pool; 0.15 s keeps us well under.
_PAGE_DELAY  = 0.15


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
        """
        Fetch up to `limit` papers from OpenAlex.

        OpenAlex returns at most 200 results per page.  When `limit` exceeds
        200 the retriever automatically paginates using cursor-based iteration
        (cursor=* on the first request, then the value of meta.next_cursor on
        each subsequent request) until `limit` papers have been collected or
        there are no more results.
        """
        base_params: dict = {
            "search": query,
            "filter": f"publication_year:{year_from}-{year_to}",
            "per-page": _PAGE_SIZE,
            # relevance_score gives diverse results per query rather than the
            # same globally-most-cited papers on every query.
            "sort": "relevance_score:desc",
            "select": (
                "id,title,publication_year,authorships,primary_location,"
                "abstract_inverted_index,doi,ids,cited_by_count,"
                "open_access,best_oa_location"
            ),
            "mailto": _MAILTO,
        }

        papers: list[Paper] = []
        cursor: str | None = "*"   # sentinel value for the first page
        page = 0

        while cursor and len(papers) < limit:
            params = {**base_params, "cursor": cursor}
            data   = self._get(_SEARCH_URL, params=params)

            for item in data.get("results", []):
                paper = _parse_item(item, topic, query)
                if paper:
                    papers.append(paper)
                    if len(papers) >= limit:
                        break

            # OpenAlex returns the next cursor in meta.next_cursor; None / ""
            # means we have reached the last page.
            cursor = (data.get("meta") or {}).get("next_cursor") or None
            page  += 1

            if cursor and len(papers) < limit:
                time.sleep(_PAGE_DELAY)

        logger.info(
            "[openalex] query=%r  returned %d papers (%d page%s)",
            query, len(papers), page, "s" if page != 1 else "",
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
