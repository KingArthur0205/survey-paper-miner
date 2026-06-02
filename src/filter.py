"""
Relevance filters applied after retrieval and after scoring.

Three independent checks form a pipeline:

  1. filter_all_topics()     — topic keyword overlap in title+abstract
                               (removes papers about a completely different domain)

  2. filter_survey_signal()  — title OR abstract must contain at least one
                               survey/review/taxonomy term
                               (removes primary research papers, not surveys)

  3. filter_min_score()      — drop papers below a minimum quality score
                               (removes low-signal results after scoring)

Each layer is independent so they can be tuned or disabled separately.
"""

from __future__ import annotations

import logging
import re

from .models import Paper, ScoredPaper

logger = logging.getLogger(__name__)

# ── Survey signal terms ───────────────────────────────────────────────────────
# A paper must contain at least one of these to be considered a survey.
SURVEY_SIGNAL_TERMS = {
    "survey", "review", "systematic review", "systematic literature review",
    "taxonomy", "overview", "comprehensive survey", "literature review",
    "bibliometric", "meta-analysis", "scoping review", "mapping study",
    "systematic mapping",
}

# ── Stopwords ─────────────────────────────────────────────────────────────────
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "is", "are", "was", "be", "as", "its", "it",
    "this", "that", "these", "those", "into", "about", "but", "not",
}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — Topic keyword relevance
# ─────────────────────────────────────────────────────────────────────────────

def filter_all_topics(
    papers: list[Paper],
    min_fraction: float = 0.5,
) -> list[Paper]:
    """
    Keep papers where at least `min_fraction` of the topic's key tokens appear
    in the title+abstract.  A paper passes if it satisfies the threshold for
    *any* of its assigned topic queries.

    min_fraction=0.5 means half the topic words must appear.
    E.g. topic "AI for computer science education" → tokens ["ai","computer","science","education"]
    Requires 2/4 to match.  An astronomy paper scores 0/4 → removed.
    """
    kept = []
    for paper in papers:
        passes = any(
            _relevance_score(paper, _topic_tokens(tq)) >= min_fraction
            for tq in paper.topic_queries
        )
        if passes:
            kept.append(paper)

    removed = len(papers) - len(kept)
    logger.info(
        "Topic relevance filter: kept %d / %d papers (%d removed)",
        len(kept), len(papers), removed,
    )
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Survey signal (hard filter)
# ─────────────────────────────────────────────────────────────────────────────

def filter_survey_signal(papers: list[Paper]) -> list[Paper]:
    """
    Remove papers that contain no survey/review/taxonomy signal in their
    title or abstract.

    Rationale: the whole purpose of this tool is to find *survey* papers.
    A primary research paper about, say, a new model architecture may score
    well on citation count and venue but is not what the user wants.

    Title matches are checked first (stronger signal); abstract matches
    are accepted as a fallback for papers whose titles are less descriptive.
    """
    kept, removed_titles = [], []
    for paper in papers:
        text = " ".join([
            (paper.title or "").lower(),
            (paper.abstract or "").lower(),
        ])
        if any(term in text for term in SURVEY_SIGNAL_TERMS):
            kept.append(paper)
        else:
            removed_titles.append(paper.title)

    if removed_titles:
        logger.debug(
            "Survey signal filter: removed %d papers with no survey terms. Examples: %s",
            len(removed_titles),
            "; ".join(t[:60] for t in removed_titles[:3]),
        )

    logger.info(
        "Survey signal filter: kept %d / %d papers (%d non-surveys removed)",
        len(kept), len(papers), len(removed_titles),
    )
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — Minimum quality score (applied post-scoring)
# ─────────────────────────────────────────────────────────────────────────────

def filter_min_score(
    scored_papers: list[ScoredPaper],
    min_score: float = 20.0,
) -> list[ScoredPaper]:
    """
    Drop papers below `min_score` after quality scoring.

    Default of 20 removes papers with almost no survey signal, no citations,
    unknown venue, and poor topic relevance — these are unlikely to be the
    high-quality surveys the user is looking for.
    """
    kept = [sp for sp in scored_papers if sp.quality_score >= min_score]
    removed = len(scored_papers) - len(kept)
    logger.info(
        "Min-score filter (>= %.0f): kept %d / %d papers (%d removed)",
        min_score, len(kept), len(scored_papers), removed,
    )
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _topic_tokens(topic: str) -> list[str]:
    """
    "AI for computer science education" → ["ai", "computer", "science", "education"]
    """
    raw = re.findall(r"[a-z0-9]+", topic.lower())
    return [t for t in raw if t not in _STOPWORDS and len(t) > 1]


def _relevance_score(paper: Paper, tokens: list[str]) -> float:
    """Fraction of topic tokens present in title + abstract."""
    if not tokens:
        return 1.0
    haystack = " ".join([
        (paper.title or "").lower(),
        (paper.abstract or "").lower(),
    ])
    hits = sum(1 for t in tokens if t in haystack)
    return hits / len(tokens)
