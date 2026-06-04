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

from . import s2_client
from .models import Paper
from .retrievers.openalex import _parse_item

logger = logging.getLogger(__name__)

_OPENALEX_URL = "https://api.openalex.org/works"
_S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
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
        # Specific (agentic) query forms come first so they aren't crowded out
        # by higher-cited broad surveys; each query merges OpenAlex + Semantic
        # Scholar results, survey-titled and citation-sorted.
        for query in _query_variants(topic):
            if kept_for_topic >= per_topic:
                break
            for paper in _gather(query, topic, year_from, year_to):
                key = _norm(paper.title)
                if key in seen:
                    continue
                paper.from_top_survey = True
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
    Build search queries from a topic, in priority order.

    Generates BOTH the spelled-out form and the ACRONYM form, plus a broadened
    variant — so for "Agentic RAG Systems" we search:
      1. "agentic retrieval augmented generation survey"  (spelled-out specific)
      2. "agentic rag survey"                              (acronym specific)
      3. "retrieval augmented generation survey"           (broad foundational)
    The acronym form is essential: many real surveys title themselves with
    "Agentic RAG" / "RAG-Reasoning" rather than the spelled-out phrase, and a
    spelled-out title query would never match them.
    """
    expanded = _significant_words(topic, expand=True)
    acronym = _significant_words(topic, expand=False)
    variants: list[str] = []
    if expanded:
        variants.append(f"{' '.join(expanded)} survey")
    if acronym and acronym != expanded:
        variants.append(f"{' '.join(acronym)} survey")
    # Broaden by dropping the leading qualifier word (e.g. "agentic")
    if len(expanded) > 3:
        variants.append(f"{' '.join(expanded[1:])} survey")
    # de-dupe while preserving order
    seen: set[str] = set()
    return [v for v in variants if not (v in seen or seen.add(v))]


def _significant_words(topic: str, expand: bool = True) -> list[str]:
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]+", topic.lower()):
        if expand:
            out.extend(_ACRONYMS.get(w, w).split())
        else:
            out.append(w)
    return [w for w in out if w not in _FILLER and len(w) > 1]


def _is_survey_title(title: str) -> bool:
    t = title.lower()
    return any(term in t for term in _SURVEY_TITLE_TERMS)


def _gather(query: str, topic: str, year_from: int, year_to: int) -> list[Paper]:
    """
    Collect survey-titled candidate papers for one query from BOTH OpenAlex
    (title search) and Semantic Scholar (broader preprint coverage), merged and
    sorted by citation count.
    """
    papers: list[Paper] = []
    for item in _openalex_title_search(query, year_from, year_to):
        p = _parse_item(item, topic, f"top-cited-survey(oa): {query}")
        if p:
            papers.append(p)
    papers.extend(_s2_survey_search(query, topic, year_from, year_to))

    papers = [p for p in papers if _is_survey_title(p.title)]
    papers.sort(key=lambda p: p.citation_count, reverse=True)
    return papers


def _openalex_title_search(query: str, year_from: int, year_to: int) -> list[dict]:
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


def _s2_survey_search(query: str, topic: str, year_from: int, year_to: int) -> list[Paper]:
    """Query Semantic Scholar (catches preprints OpenAlex misses, e.g. ACL/arXiv)."""
    params = {
        "query": query,
        "limit": 20,
        "year": f"{year_from}-{year_to}",
        "fields": "title,year,citationCount,externalIds,abstract,authors",
    }
    resp = s2_client.get(_S2_SEARCH_URL, params=params)
    if resp is None or resp.status_code != 200:
        return []
    try:
        data = resp.json().get("data", []) or []
    except Exception:
        return []

    papers: list[Paper] = []
    for d in data:
        title = (d.get("title") or "").strip()
        if not title:
            continue
        ext = d.get("externalIds") or {}
        doi = ext.get("DOI")
        arxiv = ext.get("ArXiv")
        url = (
            f"https://doi.org/{doi}" if doi
            else f"https://arxiv.org/abs/{arxiv}" if arxiv
            else (d.get("url") or None)
        )
        authors = [a.get("name", "").strip() for a in (d.get("authors") or []) if a.get("name")]
        papers.append(Paper(
            title=title,
            year=d.get("year"),
            authors=authors,
            abstract=d.get("abstract"),
            doi=doi,
            arxiv_id=arxiv,
            url=url,
            citation_count=d.get("citationCount") or 0,
            sources=["semanticscholar"],
            topic_queries=[topic],
            generated_queries=[f"top-cited-survey(s2): {query}"],
        ))
    return papers


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))
