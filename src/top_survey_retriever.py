"""
Top-cited survey retriever.

The LLM-generated queries are all phrased around the exact topic, so they can
miss popular *adjacent* surveys that don't use the same wording (e.g. a
"Graph Retrieval-Augmented Generation: A Survey" for an "Agentic RAG" topic).

This module guarantees the most popular surveys are in the candidate pool by
asking OpenAlex directly for the highest-cited survey papers whose TITLE
matches the topic — independent of how the LLM phrased its queries. Results
are merged into the retrieved pool before filtering/dedup, so the normal
relevance/judge filters still decide what reaches the final report.

OpenAlex `title.search` (not full-text `search`) is used so results are real
topic surveys rather than globally high-cited noise, sorted by citation count.
"""

from __future__ import annotations

import logging
import re

import requests

from .models import Paper
from .retrievers.openalex import _parse_item

logger = logging.getLogger(__name__)

_OPENALEX_URL = "https://api.openalex.org/works"
_MAILTO = "survey-miner@example.com"
_NO_PROXY = {"http": None, "https": None}
_SELECT = (
    "id,title,publication_year,authorships,primary_location,"
    "abstract_inverted_index,doi,ids,cited_by_count,open_access,best_oa_location"
)

_SURVEY_TITLE_TERMS = ("survey", "review", "overview", "taxonomy")

# Generic words removed from a topic before building the title query — they
# rarely appear in survey titles and over-constrain the match.
_FILLER = {
    "systems", "system", "methods", "method", "approaches", "approach",
    "based", "using", "modern", "comprehensive", "a", "an", "the", "for",
    "of", "on", "in", "and", "or", "to", "with",
}

# Acronym expansion so a topic acronym matches spelled-out survey titles.
_ACRONYMS = {
    "rag": "retrieval augmented generation",
    "llm": "large language model",
    "llms": "large language models",
    "mllm": "multimodal large language model",
    "nlp": "natural language processing",
    "kg": "knowledge graph",
    "qa": "question answering",
    "cv": "computer vision",
    "rl": "reinforcement learning",
}


def retrieve_top_surveys(
    topics: list[str],
    year_from: int,
    year_to: int,
    per_topic: int = 12,
) -> list[Paper]:
    """Return the most-cited survey papers matching each topic (deduped)."""
    out: list[Paper] = []
    seen: set[str] = set()

    for topic in topics:
        kept_for_topic = 0
        for query in _query_variants(topic):
            if kept_for_topic >= per_topic:
                break
            for item in _title_search(query, year_from, year_to):
                title = (item.get("title") or "").strip()
                if not title or not _is_survey_title(title):
                    continue
                key = _norm(title)
                if key in seen:
                    continue
                paper = _parse_item(item, topic, f"top-cited-survey: {query}")
                if not paper:
                    continue
                seen.add(key)
                out.append(paper)
                kept_for_topic += 1
                if kept_for_topic >= per_topic:
                    break

    if out:
        logger.info(
            "Top-survey retriever: injected %d high-cited surveys across %d topic(s)",
            len(out), len(topics),
        )
        for p in sorted(out, key=lambda x: x.citation_count, reverse=True)[:8]:
            logger.info("  ★ %d cites · %s", p.citation_count, p.title[:70])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _query_variants(topic: str) -> list[str]:
    """
    Build title.search queries from a topic.

    Produces a SPECIFIC query (full expanded topic) and a BROADER one (leading
    qualifier dropped), each with 'survey' — so for "Agentic RAG Systems" we
    search both "agentic retrieval augmented generation survey" (finds the
    agentic surveys) and "retrieval augmented generation survey" (finds the
    foundational/popular RAG surveys).
    """
    words = _significant_words(topic)
    if not words:
        return []
    variants: list[str] = []
    full = " ".join(words)
    variants.append(f"{full} survey")
    # Broaden by dropping the leading qualifier word (e.g. "agentic")
    if len(words) > 3:
        variants.append(f"{' '.join(words[1:])} survey")
    return variants


def _significant_words(topic: str) -> list[str]:
    expanded: list[str] = []
    for w in re.findall(r"[a-z0-9]+", topic.lower()):
        expanded.extend(_ACRONYMS.get(w, w).split())
    return [w for w in expanded if w not in _FILLER and len(w) > 1]


def _is_survey_title(title: str) -> bool:
    t = title.lower()
    return any(term in t for term in _SURVEY_TITLE_TERMS)


def _title_search(query: str, year_from: int, year_to: int) -> list[dict]:
    params = {
        "filter": f"title.search:{query},publication_year:{year_from}-{year_to}",
        "sort": "cited_by_count:desc",
        "per-page": 25,
        "select": _SELECT,
        "mailto": _MAILTO,
    }
    try:
        resp = requests.get(_OPENALEX_URL, params=params, timeout=15, proxies=_NO_PROXY)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as exc:
        logger.debug("[top-survey] OpenAlex query failed for %r: %s", query, exc)
        return []


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))
