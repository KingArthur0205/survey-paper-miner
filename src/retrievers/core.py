"""
CORE retriever.

CORE (core.ac.uk) aggregates 200M+ open-access papers from institutional
repositories, preprint servers, and journals worldwide.  It fills coverage
gaps that arXiv and OpenAlex miss — especially workshop papers, technical
reports, and papers from non-English repositories.

API docs: https://api.core.ac.uk/docs/v3

Notes:
- A free API key is available at https://core.ac.uk/services/api
  Without one, requests are accepted but rate-limited.
  Set CORE_API_KEY in .env to use your key.
- Year filtering is supported server-side via the `filters` field.
- `citationCount` in CORE responses is often 0 or stale; citation counts
  from OpenAlex (which deduplicates against CORE papers by DOI) are more
  reliable and will be merged at the deduplication step.
- All metadata is returned verbatim from the CORE API.
"""

from __future__ import annotations

import logging
import threading
import time

from ..models import Paper
from .base import BaseRetriever

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.core.ac.uk/v3/search/works"

# Serialise all CORE requests with a minimum gap between them.
# A semaphore limits concurrency but doesn't control *rate* — when a slot frees
# up the next thread grabs it immediately, so CORE still sees rapid bursts.
# A lock + timestamp enforces a minimum inter-request interval regardless of
# how many threads are running, which is what rate-limited APIs actually need.
_CORE_LOCK = threading.Lock()
_CORE_MIN_INTERVAL = 1.5   # seconds between requests
_core_last_call: float = 0.0


class CoreRetriever(BaseRetriever):
    source_name = "core"

    def __init__(self, api_key: str = ""):
        super().__init__()
        self._api_key = api_key

    def _fetch(
        self,
        query: str,
        topic: str,
        year_from: int,
        year_to: int,
        limit: int,
    ) -> list[Paper]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # Send the query as plain text — embedding Lucene year-range syntax
        # (yearPublished:[2022 TO 2026]) causes consistent 500 errors from
        # CORE's query parser. Year filtering is applied post-fetch instead.
        # Fetch extra results to compensate for papers dropped by the year filter.
        payload = {
            "q": query,
            "limit": min(limit * 2, 100),
        }

        # Serialise through the lock and enforce a minimum inter-request gap.
        # This prevents the burst pattern that triggers CORE 429s even when
        # multiple threads are queued up waiting for results.
        global _core_last_call
        with _CORE_LOCK:
            now = time.monotonic()
            gap = _CORE_MIN_INTERVAL - (now - _core_last_call)
            if gap > 0:
                time.sleep(gap)
            # Stamp before the request so the interval is enforced even on failure.
            _core_last_call = time.monotonic()
            data = self._post(_SEARCH_URL, json=payload, headers=headers)

        papers = []
        for item in data.get("results", []):
            paper = _parse_item(item, topic, query)
            if paper:
                papers.append(paper)

        # Apply year filter post-fetch (year range is no longer embedded in query)
        if year_from or year_to:
            papers = [
                p for p in papers
                if p.year is None or (
                    (year_from is None or p.year >= year_from)
                    and (year_to is None or p.year <= year_to)
                )
            ]

        logger.info("[core] query=%r  returned %d papers", query, len(papers))
        return papers


def _parse_item(item: dict, topic: str, query: str) -> Paper | None:
    title = (item.get("title") or "").strip()
    if not title:
        return None

    year = item.get("yearPublished")

    authors = [
        a.get("name", "").strip()
        for a in (item.get("authors") or [])
        if a.get("name", "").strip()
    ]
    # CORE sometimes duplicates author entries — deduplicate while preserving order
    seen: set[str] = set()
    unique_authors: list[str] = []
    for a in authors:
        if a not in seen:
            seen.add(a)
            unique_authors.append(a)

    # Venue: prefer journal name, fall back to publisher
    venue: str | None = None
    journals = item.get("journals") or []
    if journals and journals[0].get("title"):
        venue = journals[0]["title"].strip()
    elif item.get("publisher"):
        venue = item["publisher"].strip()

    abstract = (item.get("abstract") or "").strip().replace("\n", " ") or None

    doi_raw = item.get("doi") or ""
    doi = doi_raw.replace("https://doi.org/", "").strip() or None

    arxiv_id = item.get("arxivId") or None
    if arxiv_id:
        # Strip version suffix if present (e.g. "2303.12345v2" → "2303.12345")
        arxiv_id = arxiv_id.split("v")[0] if "v" in arxiv_id else arxiv_id

    # Prefer a direct download URL; fall back to sourceFulltextUrls
    pdf_url: str | None = item.get("downloadUrl")
    if not pdf_url:
        urls = item.get("sourceFulltextUrls") or []
        pdf_url = urls[0] if urls else None

    # CORE's own page URL
    core_id = item.get("id")
    url = f"https://core.ac.uk/works/{core_id}" if core_id else None

    return Paper(
        title=title,
        year=year,
        authors=unique_authors,
        venue=venue,
        abstract=abstract,
        doi=doi,
        arxiv_id=arxiv_id,
        url=url,
        pdf_url=pdf_url,
        citation_count=item.get("citationCount") or 0,
        influential_citation_count=0,
        sources=["core"],
        topic_queries=[topic],
        generated_queries=[query],
    )
