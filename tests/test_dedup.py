"""Tests for dedup.py"""

import pytest
from src.dedup import deduplicate, _merge, _longer
from src.models import Paper


def _paper(**kwargs) -> Paper:
    defaults = dict(
        title="A Survey on Large Language Models",
        year=2023,
        authors=["Alice", "Bob"],
        citation_count=100,
        sources=["semantic_scholar"],
        topic_queries=["large language models"],
        generated_queries=['"large language models" "survey"'],
    )
    defaults.update(kwargs)
    return Paper(**defaults)


# ---------------------------------------------------------------------------
# DOI deduplication
# ---------------------------------------------------------------------------

def test_dedup_by_doi():
    p1 = _paper(doi="10.1234/abc", sources=["semantic_scholar"])
    p2 = _paper(doi="10.1234/abc", sources=["openalex"], title="A Survey on Large Language Models")
    result = deduplicate([p1, p2])
    assert len(result) == 1
    assert "openalex" in result[0].sources
    assert "semantic_scholar" in result[0].sources


# ---------------------------------------------------------------------------
# arXiv ID deduplication
# ---------------------------------------------------------------------------

def test_dedup_by_arxiv_id():
    p1 = _paper(arxiv_id="2303.12345", sources=["arxiv"])
    p2 = _paper(arxiv_id="2303.12345", sources=["semantic_scholar"], citation_count=200)
    result = deduplicate([p1, p2])
    assert len(result) == 1
    # Merge takes higher citation count
    assert result[0].citation_count == 200


# ---------------------------------------------------------------------------
# Normalised title deduplication
# ---------------------------------------------------------------------------

def test_dedup_by_norm_title():
    p1 = _paper(title="A Survey on Large Language Models")
    p2 = _paper(title="A Survey on Large Language Models")
    result = deduplicate([p1, p2])
    assert len(result) == 1


def test_dedup_case_insensitive():
    p1 = _paper(title="Large Language Models: A Survey")
    p2 = _paper(title="large language models: a survey")
    result = deduplicate([p1, p2])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Fuzzy title deduplication
# ---------------------------------------------------------------------------

def test_dedup_fuzzy_similar_titles():
    # Titles differ by one word — should be caught at threshold 95
    p1 = _paper(title="A Comprehensive Survey on Large Language Models")
    p2 = _paper(title="A Comprehensive  Survey on Large Language Models")  # double space
    result = deduplicate([p1, p2])
    assert len(result) == 1


def test_dedup_different_papers_kept():
    p1 = _paper(title="A Survey on Large Language Models")
    p2 = _paper(title="A Survey on Retrieval Augmented Generation")
    result = deduplicate([p1, p2])
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def test_merge_prefers_nonempty_doi():
    base = _paper(doi=None)
    dup = _paper(doi="10.1234/xyz")
    merged = _merge(base, dup)
    assert merged.doi == "10.1234/xyz"


def test_merge_takes_higher_citations():
    base = _paper(citation_count=50)
    dup = _paper(citation_count=200)
    merged = _merge(base, dup)
    assert merged.citation_count == 200


def test_merge_unions_sources():
    base = _paper(sources=["semantic_scholar"])
    dup = _paper(sources=["arxiv"])
    merged = _merge(base, dup)
    assert set(merged.sources) == {"semantic_scholar", "arxiv"}


def test_merge_prefers_longer_abstract():
    base = _paper(abstract="Short.")
    dup = _paper(abstract="A much longer abstract that has more information.")
    merged = _merge(base, dup)
    assert merged.abstract == "A much longer abstract that has more information."


def test_longer_helper_none_handling():
    assert _longer(None, "hello") == "hello"
    assert _longer("hello", None) == "hello"
    assert _longer(None, None) is None
    assert _longer("ab", "abc") == "abc"
