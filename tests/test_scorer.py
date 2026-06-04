"""Tests for scorer.py"""

import pytest
from src.config import AppConfig
from src.scorer import QualityScorer, score_papers
from src.models import Paper


def _cfg(**kwargs) -> AppConfig:
    defaults = dict(
        year_from=2021,
        year_to=2026,
        venue_scores={
            "ACM Computing Surveys": 20,
            "NeurIPS": 15,
            "arXiv": 8,
        },
    )
    defaults.update(kwargs)
    return AppConfig(**defaults)


def _paper(**kwargs) -> Paper:
    defaults = dict(
        title="A Survey on Large Language Models",
        year=2023,
        abstract=(
            "This survey provides a comprehensive overview and taxonomy of "
            "large language models. We review benchmarks, datasets, and evaluation "
            "metrics. We discuss limitations and future directions."
        ),
        citation_count=0,
        influential_citation_count=0,
        sources=["arxiv"],
        topic_queries=["large language models"],
        generated_queries=[],
    )
    defaults.update(kwargs)
    return Paper(**defaults)


# ---------------------------------------------------------------------------
# Venue score
# ---------------------------------------------------------------------------

def test_venue_score_known_exact():
    scorer = QualityScorer(_cfg())
    p = _paper(venue="ACM Computing Surveys")
    sp = scorer.score(p)
    assert sp.venue_score == 20.0


def test_venue_score_known_substring():
    scorer = QualityScorer(_cfg())
    p = _paper(venue="NeurIPS 2023")
    sp = scorer.score(p)
    assert sp.venue_score == 15.0


def test_venue_score_unknown_nonzero():
    scorer = QualityScorer(_cfg())
    p = _paper(venue="Some Unknown Workshop")
    sp = scorer.score(p)
    assert sp.venue_score > 0  # partial credit for having a venue


def test_venue_score_none():
    scorer = QualityScorer(_cfg())
    p = _paper(venue=None)
    sp = scorer.score(p)
    assert sp.venue_score == 0.0


# ---------------------------------------------------------------------------
# Citation score
# ---------------------------------------------------------------------------

def test_citation_score_high_cpy():
    scorer = QualityScorer(_cfg())
    p = _paper(year=2021, citation_count=3000)  # 3000/6yr = 500cpy → max cpy_pts=8
    sp = scorer.score(p)
    assert sp.citation_score == 8.0  # max cpy (8) + no influential (0)


def test_citation_score_full():
    scorer = QualityScorer(_cfg())
    # max cpy: 500+/yr; max ratio: ≥50% influential
    p = _paper(year=2021, citation_count=3000, influential_citation_count=1600)
    sp = scorer.score(p)
    assert sp.citation_score == 20.0  # 8 (cpy) + 12 (ratio ≥ 0.5)


def test_citation_score_zero():
    scorer = QualityScorer(_cfg())
    p = _paper(year=2024, citation_count=0)
    sp = scorer.score(p)
    assert sp.citation_score == 0.0


# ---------------------------------------------------------------------------
# Recency score
# ---------------------------------------------------------------------------

def test_recency_score_recent(monkeypatch):
    import src.scorer as scorer_mod
    monkeypatch.setattr(scorer_mod, "_CURRENT_YEAR", 2026)
    scorer = QualityScorer(_cfg())
    p = _paper(year=2025)
    sp = scorer.score(p)
    assert sp.recency_score == 10.0


def test_recency_score_old(monkeypatch):
    import src.scorer as scorer_mod
    monkeypatch.setattr(scorer_mod, "_CURRENT_YEAR", 2026)
    scorer = QualityScorer(_cfg())
    p = _paper(year=2019)
    sp = scorer.score(p)
    assert sp.recency_score == 0.0


# ---------------------------------------------------------------------------
# Survey signal
# ---------------------------------------------------------------------------

def test_survey_signal_title_hit():
    scorer = QualityScorer(_cfg())
    p = _paper(title="A Survey on Transformers", abstract="")
    sp = scorer.score(p)
    assert sp.survey_signal_score >= 10.0


def test_survey_signal_no_terms():
    scorer = QualityScorer(_cfg())
    p = _paper(title="Attention Is All You Need", abstract="We propose a new model.")
    sp = scorer.score(p)
    assert sp.survey_signal_score == 0.0


# ---------------------------------------------------------------------------
# Total score capped at 100
# ---------------------------------------------------------------------------

def test_total_score_capped():
    scorer = QualityScorer(_cfg())
    p = _paper(
        title="A Comprehensive Survey and Taxonomy",
        year=2025,
        venue="ACM Computing Surveys",
        citation_count=10000,
        influential_citation_count=500,
        abstract=(
            "survey taxonomy benchmark dataset evaluation future directions "
            "limitations comparison classification open challenges"
        ),
    )
    sp = scorer.score(p)
    assert sp.quality_score <= 100.0


# ---------------------------------------------------------------------------
# Canonical score component
# ---------------------------------------------------------------------------

def test_canonical_score_component():
    scorer = QualityScorer(_cfg())
    p = _paper(canonical_score=1.0)
    sp = scorer.score(p)
    assert sp.canonical_score_component == 20.0


def test_canonical_score_component_zero():
    scorer = QualityScorer(_cfg())
    p = _paper(canonical_score=0.0)
    sp = scorer.score(p)
    assert sp.canonical_score_component == 0.0


def test_canonical_score_component_partial():
    scorer = QualityScorer(_cfg())
    p = _paper(canonical_score=0.5)
    sp = scorer.score(p)
    assert sp.canonical_score_component == 10.0


# ---------------------------------------------------------------------------
# score_papers returns sorted list
# ---------------------------------------------------------------------------

def test_score_papers_sorted():
    cfg = _cfg()
    papers = [
        _paper(title="Not a survey", citation_count=0),
        _paper(title="A Survey on X", citation_count=500, year=2024, venue="ACM Computing Surveys"),
    ]
    scored = score_papers(papers, cfg)
    assert scored[0].quality_score >= scored[1].quality_score
