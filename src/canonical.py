"""
Canonical Survey Detector (simplified heuristic).

Design-doc vision (section 5.2.6):
  Full implementation fetches each paper's reference list from the Semantic
  Scholar API and counts how often a survey appears as a cited background
  reference in other top papers.  That cross-reference signal reliably
  identifies the community-acknowledged "standard survey" for a field.

Current status — simplified proxy:
  No SemanticScholarRetriever exists yet, so the cross-reference approach
  is not available.  Instead we approximate canonical status from the
  metadata we already have:

    canonical_score = 0.6 * norm(influential_ratio)
                    + 0.4 * norm(citations_per_year)

  where both terms are normalised to [0, 1] per-topic using min-max scaling.
  The result is written back to `paper.canonical_score` for every paper in
  the input set.

  Papers that genuinely function as the community's standard reference tend
  to have both high citation velocity AND a high fraction of those citations
  classified as "influential" by Semantic Scholar, so this proxy correlates
  reasonably well with the ground-truth canonical signal while remaining
  fully computable from existing data.

Upgrade path:
  Once a SemanticScholarRetriever is added and `background_citation_count`
  is populated, replace the formula with:
    canonical_score = 0.5 * norm(cross_reference_count)
                    + 0.5 * norm(background_ratio)
  as specified in the design doc.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

from .models import Paper

logger = logging.getLogger(__name__)

_CURRENT_YEAR = date.today().year


def detect_canonical_surveys(papers: list[Paper]) -> list[Paper]:
    """
    Compute and write `canonical_score` (0.0–1.0) for every paper.

    Scoring is done per-topic: each paper's metrics are normalised against
    other papers that share the same primary topic_query.  Papers without
    a topic_query are grouped under a synthetic "Uncategorised" bucket.

    Returns the same list with `canonical_score` mutated in place.
    """
    # Group paper indices by primary topic
    by_topic: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(papers):
        topic = p.topic_queries[0] if p.topic_queries else "Uncategorised"
        by_topic[topic].append(i)

    for topic, indices in by_topic.items():
        group = [papers[i] for i in indices]
        scores = _compute_scores(group)
        for i, score in zip(indices, scores):
            papers[i].canonical_score = score

    n_with_score = sum(1 for p in papers if p.canonical_score > 0)
    logger.info(
        "Canonical scores computed: %d / %d papers received a non-zero score",
        n_with_score, len(papers),
    )
    return papers


def _compute_scores(papers: list[Paper]) -> list[float]:
    """Compute canonical_score for a single topic group."""
    if not papers:
        return []

    iratios = [
        p.influential_citation_count / max(p.citation_count, 1)
        for p in papers
    ]
    cpys = [
        p.citation_count / max(_CURRENT_YEAR - (p.year or _CURRENT_YEAR - 1) + 1, 1)
        for p in papers
    ]

    iratio_scores = _minmax_normalize(iratios)
    cpy_scores = _minmax_normalize(cpys)

    return [
        round(0.6 * ir + 0.4 * cy, 4)
        for ir, cy in zip(iratio_scores, cpy_scores)
    ]


def _minmax_normalize(values: list[float]) -> list[float]:
    """Scale values to [0, 1] using min-max normalization."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]
