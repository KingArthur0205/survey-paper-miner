"""
arXiv retriever.

Uses the official arXiv Atom/XML API (no key required).
API docs: https://arxiv.org/help/api/basics

Notes:
- arXiv API returns real, live metadata — title, authors, abstract, arXiv ID,
  and submission date are always accurate.
- Citation counts are NOT available from arXiv; they remain 0 and are filled
  in at the deduplication/merge step if Semantic Scholar has the same paper.
- We search both cs.* and stat.* categories to cover ML/AI papers fully.
- The API asks for polite usage: at most 3 requests per second.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET

from ..models import Paper
from .base import BaseRetriever

logger = logging.getLogger(__name__)

_BASE_URL = "https://export.arxiv.org/api/query"
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"

# arXiv recommends at most 1 request per second for automated tools.
# With a shared thread pool there may be many concurrent callers, so we
# use a class-level lock + minimum interval to serialise all arXiv requests
# regardless of how many threads are running.
_ARXIV_LOCK = threading.Lock()
_ARXIV_MIN_INTERVAL = 10.0  # seconds between requests — conservative to avoid 429s
_arxiv_last_call: float = 0.0


class ArxivRetriever(BaseRetriever):
    source_name = "arxiv"

    def _fetch(
        self,
        query: str,
        topic: str,
        year_from: int,
        year_to: int,
        limit: int,
    ) -> list[Paper]:
        # Search title only (ti:).
        # Using abs: is too permissive — it matches papers that merely mention
        # "survey" in the abstract while being completely off-topic (e.g. an
        # astronomy paper returned for an AI education query).
        # The post-retrieval relevance filter provides a second safety net.
        search_query = f"ti:{query}"

        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": min(limit, 100),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }

        # Serialise all arXiv requests through a lock with a minimum gap.
        # This prevents concurrent threads from all firing at once and
        # triggering 429s even before the retry logic can kick in.
        global _arxiv_last_call
        with _ARXIV_LOCK:
            now = time.monotonic()
            gap = _ARXIV_MIN_INTERVAL - (now - _arxiv_last_call)
            if gap > 0:
                time.sleep(gap)
            # Stamp the timestamp BEFORE the request so the next queued thread
            # always waits the full interval, even if this request fails with a
            # 429 (in which case _get_text raises and the line after it is
            # never reached — causing the old bug where the next thread saw a
            # stale timestamp and fired immediately into an already-angry server).
            _arxiv_last_call = time.monotonic()
            xml_text = self._get_text(_BASE_URL, params=params)

        papers = _parse_atom(xml_text, topic, query, year_from, year_to)

        logger.info("[arxiv] query=%r  returned %d papers (after year filter)", query, len(papers))
        return papers


def _parse_atom(
    xml_text: str,
    topic: str,
    query: str,
    year_from: int,
    year_to: int,
) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers = []

    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        title_el = entry.find(f"{{{_ATOM_NS}}}title")
        title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
        if not title:
            continue

        # Published date gives the submission year
        published_el = entry.find(f"{{{_ATOM_NS}}}published")
        year: int | None = None
        if published_el is not None and published_el.text:
            try:
                year = int(published_el.text[:4])
            except ValueError:
                pass

        # Apply year filter — arXiv API doesn't support server-side date filtering
        if year is not None and not (year_from <= year <= year_to):
            continue

        abstract_el = entry.find(f"{{{_ATOM_NS}}}summary")
        abstract = (abstract_el.text or "").strip().replace("\n", " ") if abstract_el is not None else None

        authors = [
            (a.find(f"{{{_ATOM_NS}}}name").text or "").strip()
            for a in entry.findall(f"{{{_ATOM_NS}}}author")
            if a.find(f"{{{_ATOM_NS}}}name") is not None
        ]

        # The entry id is a URL like https://arxiv.org/abs/2303.12345v1
        id_el = entry.find(f"{{{_ATOM_NS}}}id")
        arxiv_url = (id_el.text or "").strip() if id_el is not None else ""
        arxiv_id = _extract_arxiv_id(arxiv_url)

        # Find PDF link
        pdf_url: str | None = None
        for link in entry.findall(f"{{{_ATOM_NS}}}link"):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href")
                break

        papers.append(Paper(
            title=title,
            year=year,
            authors=authors,
            venue="arXiv",
            abstract=abstract,
            doi=None,  # DOI not in arXiv Atom feed
            arxiv_id=arxiv_id,
            url=arxiv_url if arxiv_url else None,
            pdf_url=pdf_url,
            citation_count=0,
            influential_citation_count=0,
            sources=["arxiv"],
            topic_queries=[topic],
            generated_queries=[query],
        ))

    return papers


def _extract_arxiv_id(url: str) -> str | None:
    """Pull '2303.12345' from 'https://arxiv.org/abs/2303.12345v1'."""
    if "/abs/" in url:
        raw = url.split("/abs/")[-1].strip()
        # Strip version suffix vN
        return raw.split("v")[0] if "v" in raw else raw
    return None
