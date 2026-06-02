"""
Deduplicator.

Merges papers that refer to the same work but were returned by different
sources (or by repeated queries hitting the same paper).

Deduplication priority (first match wins):
  1. DOI exact match
  2. arXiv ID exact match
  3. Normalised title exact match
  4. Fuzzy title similarity ≥ threshold (default 95/100)

When a duplicate is found the two records are *merged*:
  - Prefer non-empty DOI
  - Prefer longer / non-empty abstract
  - Take the higher citation count
  - Union the sources lists
  - Union the topic_queries and generated_queries lists
  - Prefer non-None pdf_url

Dependencies: rapidfuzz (pure-Python fuzzy matching, fast)
"""

from __future__ import annotations

import logging
from collections import defaultdict

from rapidfuzz import fuzz

from .models import Paper

logger = logging.getLogger(__name__)

# Similarity threshold (0–100) for fuzzy title matching.
# 95 catches OCR/spacing variants while avoiding false positives.
FUZZY_TITLE_THRESHOLD = 95


def deduplicate(papers: list[Paper], threshold: int = FUZZY_TITLE_THRESHOLD) -> list[Paper]:
    """
    Remove duplicates from `papers` and merge metadata across copies.

    Returns a new list with one Paper per unique work.
    """
    # Index structures for O(1) lookup on exact keys
    by_doi: dict[str, int] = {}          # doi → index in `merged`
    by_arxiv: dict[str, int] = {}        # arxiv_id → index in `merged`
    by_norm_title: dict[str, int] = {}   # normalised_title → index in `merged`

    merged: list[Paper] = []

    for paper in papers:
        idx = _find_duplicate(paper, by_doi, by_arxiv, by_norm_title, merged, threshold)

        if idx is not None:
            # Merge into existing record
            merged[idx] = _merge(merged[idx], paper)
        else:
            # New unique paper — register in all indices
            idx = len(merged)
            merged.append(paper)
            _register(paper, idx, by_doi, by_arxiv, by_norm_title)

    removed = len(papers) - len(merged)
    logger.info("Deduplication: %d → %d papers (%d duplicates removed)", len(papers), len(merged), removed)
    return merged


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_duplicate(
    paper: Paper,
    by_doi: dict[str, int],
    by_arxiv: dict[str, int],
    by_norm_title: dict[str, int],
    merged: list[Paper],
    threshold: int,
) -> int | None:
    """Return the index of an existing matching paper, or None."""

    # 1. DOI exact match
    if paper.doi:
        if paper.doi in by_doi:
            return by_doi[paper.doi]

    # 2. arXiv ID exact match
    if paper.arxiv_id:
        if paper.arxiv_id in by_arxiv:
            return by_arxiv[paper.arxiv_id]

    # 3. Normalised title exact match
    norm = paper.normalized_title()
    if norm in by_norm_title:
        return by_norm_title[norm]

    # 4. Fuzzy title match — only run against titles not yet matched exactly
    for idx, existing in enumerate(merged):
        score = fuzz.ratio(norm, existing.normalized_title())
        if score >= threshold:
            return idx

    return None


def _register(
    paper: Paper,
    idx: int,
    by_doi: dict[str, int],
    by_arxiv: dict[str, int],
    by_norm_title: dict[str, int],
) -> None:
    if paper.doi:
        by_doi[paper.doi] = idx
    if paper.arxiv_id:
        by_arxiv[paper.arxiv_id] = idx
    by_norm_title[paper.normalized_title()] = idx


def _merge(base: Paper, duplicate: Paper) -> Paper:
    """
    Combine two Paper records for the same work.
    `base` is the record that was seen first; `duplicate` is a later copy.
    """
    return Paper(
        title=base.title,  # Keep the first-seen title
        year=base.year or duplicate.year,
        authors=base.authors if base.authors else duplicate.authors,
        venue=base.venue or duplicate.venue,
        abstract=_longer(base.abstract, duplicate.abstract),
        doi=base.doi or duplicate.doi,
        arxiv_id=base.arxiv_id or duplicate.arxiv_id,
        url=base.url or duplicate.url,
        pdf_url=base.pdf_url or duplicate.pdf_url,
        # Take whichever citation count is higher (one source may be more current)
        citation_count=max(base.citation_count, duplicate.citation_count),
        influential_citation_count=max(
            base.influential_citation_count,
            duplicate.influential_citation_count,
        ),
        sources=_union(base.sources, duplicate.sources),
        topic_queries=_union(base.topic_queries, duplicate.topic_queries),
        generated_queries=_union(base.generated_queries, duplicate.generated_queries),
    )


def _longer(a: str | None, b: str | None) -> str | None:
    """Return whichever string is longer (non-None preferred)."""
    if a and b:
        return a if len(a) >= len(b) else b
    return a or b


def _union(a: list[str], b: list[str]) -> list[str]:
    """Return sorted deduplicated union of two lists."""
    return sorted(set(a) | set(b))
