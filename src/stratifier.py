"""
Temporal Stratifier.

Classifies each paper into an authority tier based on its age and citation
velocity relative to other papers in the same topic.  Classification is
per-topic so that a paper is judged against its peers, not against papers
from unrelated fields.

Tiers (mutually exclusive; foundational takes precedence):

  "foundational"      age ≥ 4 years  AND  citations/year in top 10% of topic
  "current_standard"  age 1–4 years  AND  citations/year in top 25% of topic
  "emerging"          age ≤ 2 years  (regardless of citation velocity)
  None                doesn't qualify for any tier

The `authority_tier` is written directly onto each `Paper` object.
`ScoredPaper` objects are accepted for convenience (the tier is written to
the wrapped `Paper`).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

from .models import Paper, ScoredPaper

logger = logging.getLogger(__name__)

_CURRENT_YEAR = date.today().year


def stratify_papers(scored_papers: list[ScoredPaper]) -> list[ScoredPaper]:
    """
    Assign `authority_tier` to each paper's underlying `Paper` object.

    Operates per-topic to ensure citation velocity is judged relative
    to peers in the same research area.  Returns the same list (mutated).
    """
    by_topic: dict[str, list[int]] = defaultdict(list)
    for i, sp in enumerate(scored_papers):
        topic = sp.paper.topic_queries[0] if sp.paper.topic_queries else "Uncategorised"
        by_topic[topic].append(i)

    for topic, indices in by_topic.items():
        group = [scored_papers[i] for i in indices]
        _stratify_group(group)

    counts: dict[str, int] = defaultdict(int)
    for sp in scored_papers:
        counts[sp.paper.authority_tier or "none"] += 1
    logger.info(
        "Stratification complete: foundational=%d, current_standard=%d, "
        "emerging=%d, untiered=%d",
        counts["foundational"], counts["current_standard"],
        counts["emerging"], counts["none"],
    )
    return scored_papers


def _stratify_group(papers: list[ScoredPaper]) -> None:
    """Classify a single topic group in place."""
    if not papers:
        return

    # Compute citations/year for each paper
    cpys = []
    for sp in papers:
        year = sp.paper.year or _CURRENT_YEAR - 1
        age = max(_CURRENT_YEAR - year + 1, 1)
        cpys.append(sp.paper.citation_count / age)

    # Sort to find percentile thresholds
    sorted_cpys = sorted(cpys, reverse=True)
    n = len(sorted_cpys)
    top10_threshold = sorted_cpys[max(0, round(n * 0.10) - 1)]
    top25_threshold = sorted_cpys[max(0, round(n * 0.25) - 1)]

    for sp, cpy in zip(papers, cpys):
        year = sp.paper.year or _CURRENT_YEAR - 1
        age = _CURRENT_YEAR - year

        if age >= 4 and cpy >= top10_threshold:
            sp.paper.authority_tier = "foundational"
        elif 1 <= age <= 4 and cpy >= top25_threshold:
            sp.paper.authority_tier = "current_standard"
        elif age <= 2:
            sp.paper.authority_tier = "emerging"
        else:
            sp.paper.authority_tier = None
