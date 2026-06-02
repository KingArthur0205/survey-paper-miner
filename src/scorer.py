"""
Quality Scorer.

Assigns each paper a score from 0–100 based on six components:

  Component                   Max pts
  ─────────────────────────── ───────
  Venue / source quality         20
  Citation impact                20
    └─ citations/year              8
    └─ influential-citation ratio  12
  Recency                        10
  Survey signal (title+abstract) 15
  Structure signal (abstract)    15
  Canonical survey score         20
  ─────────────────────────── ───────
  Total                         100

All scoring is deterministic and rule-based — no LLM involved.
Scores are transparent and reproducible.
"""

from __future__ import annotations

import math
from datetime import date

from .config import AppConfig
from .models import Paper, ScoredPaper

# --------------------------------------------------------------------------
# Signal term lists
# --------------------------------------------------------------------------

SURVEY_TERMS = {
    "survey", "review", "systematic review", "taxonomy",
    "overview", "comprehensive", "literature review", "bibliometric",
    "meta-analysis", "state of the art", "state-of-the-art",
}

STRUCTURE_TERMS = {
    "taxonomy", "benchmark", "dataset", "evaluation",
    "future directions", "open challenges", "limitations",
    "comparison", "classification", "categorization",
    "future work", "open problems", "discussion",
}

_CURRENT_YEAR = date.today().year


class QualityScorer:
    """
    Stateless scorer.  Construct once, call `score(paper)` for each paper.
    """

    def __init__(self, cfg: AppConfig):
        # Normalise venue names to lowercase for case-insensitive lookup
        self._venue_scores: dict[str, int] = {
            k.lower(): v for k, v in cfg.venue_scores.items()
        }
        self._year_to = cfg.year_to

    def score(self, paper: Paper) -> ScoredPaper:
        venue_score = self._venue_score(paper)
        citation_score = self._citation_score(paper)
        recency_score = self._recency_score(paper)
        survey_signal = self._survey_signal(paper)
        structure_signal = self._structure_signal(paper)
        canonical_component = self._canonical_score_component(paper)

        total = (
            venue_score
            + citation_score
            + recency_score
            + survey_signal
            + structure_signal
            + canonical_component
        )

        return ScoredPaper(
            paper=paper,
            quality_score=round(min(total, 100.0), 2),
            venue_score=venue_score,
            citation_score=citation_score,
            recency_score=recency_score,
            survey_signal_score=survey_signal,
            structure_signal_score=structure_signal,
            canonical_score_component=canonical_component,
        )

    # ------------------------------------------------------------------
    # Component scorers
    # ------------------------------------------------------------------

    def _venue_score(self, paper: Paper) -> float:
        """Up to 20 pts. Exact then partial match against known venue names."""
        if not paper.venue:
            return 0.0

        venue_lower = paper.venue.lower()

        # Exact match first
        if venue_lower in self._venue_scores:
            return float(self._venue_scores[venue_lower])

        # Substring match (e.g. "NeurIPS 2023" → "neurips")
        for known, pts in self._venue_scores.items():
            if known in venue_lower or venue_lower in known:
                return float(pts)

        # Unknown venue: partial credit for non-arXiv sources
        return 5.0

    def _citation_score(self, paper: Paper) -> float:
        """
        Up to 20 pts: 8 from citations-per-year + 12 from influential-ratio.

        cpy component (8 pts max):   log-scale thresholds on citations/year.
        ratio component (12 pts max): influential_citation_count / citation_count.
        """
        year = paper.year or (_CURRENT_YEAR - 1)
        age = max(_CURRENT_YEAR - year + 1, 1)
        cpy = paper.citation_count / age

        if cpy >= 500:
            cpy_pts = 8.0
        elif cpy >= 200:
            cpy_pts = 6.4
        elif cpy >= 50:
            cpy_pts = 4.8
        elif cpy >= 20:
            cpy_pts = 3.2
        elif cpy >= 5:
            cpy_pts = 2.0
        else:
            cpy_pts = round(min(1.6, math.log1p(cpy) * 0.8), 2)

        total_citations = max(paper.citation_count, 1)
        ratio = paper.influential_citation_count / total_citations
        if ratio >= 0.5:
            ratio_pts = 12.0
        elif ratio >= 0.3:
            ratio_pts = 9.0
        elif ratio >= 0.15:
            ratio_pts = 6.0
        elif ratio >= 0.05:
            ratio_pts = 3.0
        else:
            ratio_pts = 0.0

        return round(cpy_pts + ratio_pts, 2)

    def _recency_score(self, paper: Paper) -> float:
        """
        Up to 10 pts.  Newer papers score higher, but papers older than 5
        years still get partial credit.

        Points by age (years since publication):
          0-1 → 10, 2 → 8, 3 → 6, 4 → 4, 5 → 2, >5 → 0
        """
        if paper.year is None:
            return 0.0
        age = _CURRENT_YEAR - paper.year
        if age <= 1:
            return 10.0
        if age == 2:
            return 8.0
        if age == 3:
            return 6.0
        if age == 4:
            return 4.0
        if age == 5:
            return 2.0
        return 0.0

    def _survey_signal(self, paper: Paper) -> float:
        """
        Up to 15 pts.  Checks title and abstract for survey-indicating terms.
        Title hits carry more weight than abstract hits.
        """
        text_title = (paper.title or "").lower()
        text_abstract = (paper.abstract or "").lower()

        title_hits = sum(1 for t in SURVEY_TERMS if t in text_title)
        abstract_hits = sum(1 for t in SURVEY_TERMS if t in text_abstract)

        score = min(title_hits * 10, 15)
        if score < 15:
            score = min(score + abstract_hits * 3, 15)

        return float(score)

    def _structure_signal(self, paper: Paper) -> float:
        """
        Up to 15 pts.  Survey papers typically mention benchmarks, datasets,
        evaluations, limitations, etc.  Each distinct term hit = 2 pts.
        """
        text = ((paper.title or "") + " " + (paper.abstract or "")).lower()
        hits = sum(1 for t in STRUCTURE_TERMS if t in text)
        return float(min(hits * 2, 15))

    def _canonical_score_component(self, paper: Paper) -> float:
        """Up to 20 pts from CanonicalSurveyDetector (written to paper.canonical_score)."""
        return round(min(20.0, paper.canonical_score * 20.0), 2)


def score_papers(papers: list[Paper], cfg: AppConfig) -> list[ScoredPaper]:
    """Score all papers and return them sorted by quality_score descending."""
    scorer = QualityScorer(cfg)
    scored = [scorer.score(p) for p in papers]
    scored.sort(key=lambda s: s.quality_score, reverse=True)
    return scored
