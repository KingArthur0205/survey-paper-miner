"""Tests for query_builder.py"""

import pytest
from src.config import AppConfig
from src.query_builder import build_queries


def _cfg(**kwargs) -> AppConfig:
    defaults = dict(
        topics=["large language models", "AI safety"],
        survey_terms=["survey", "review"],
        year_from=2021,
        year_to=2026,
    )
    defaults.update(kwargs)
    return AppConfig(**defaults)


def test_build_queries_count():
    cfg = _cfg(topics=["llm", "rag"], survey_terms=["survey", "review"])
    queries = build_queries(cfg)
    # 2 topics × 2 terms = 4 queries
    assert len(queries) == 4


def test_build_queries_no_duplicates():
    cfg = _cfg(topics=["llm"], survey_terms=["survey", "survey"])
    queries = build_queries(cfg)
    # Duplicate term should be collapsed
    assert len(queries) == 1


def test_query_string_format():
    cfg = _cfg(topics=["large language models"], survey_terms=["survey"])
    queries = build_queries(cfg)
    assert queries[0].query_string == '"large language models" "survey"'


def test_topic_preserved():
    cfg = _cfg(topics=["AI safety"], survey_terms=["taxonomy"])
    queries = build_queries(cfg)
    assert queries[0].topic == "AI safety"


def test_empty_topics():
    cfg = _cfg(topics=[], survey_terms=["survey"])
    queries = build_queries(cfg)
    assert queries == []
