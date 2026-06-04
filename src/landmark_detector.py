"""
Landmark (seminal primary-paper) detector.

The survey-mining pipeline deliberately keeps only survey/review papers, so a
newcomer never sees the actual landmark *primary* works the surveys are built
on — ReAct, Self-RAG, FLARE, DPR, Toolformer, and the like.

This module closes that gap.  Given the analysed surveys for one topic, it:

  1. Asks an LLM to extract the seminal primary papers/methods the surveys
     most repeatedly reference, grounded in the surveys' own scopes, methods,
     and taxonomies, with an estimate of how many surveys cite each.
  2. Keeps only candidates referenced by >= landmark_min_mentions surveys.
  3. Resolves each candidate against OpenAlex to get the real title, year,
     citation count, and URL.
  4. Keeps only genuinely high-impact works (>= landmark_min_citations).
  5. Returns the top landmark_max_count, most-referenced first.

Landmark papers are intentionally NOT year-filtered — foundational works
(e.g. DPR 2020) often predate the survey window.
"""

from __future__ import annotations

import json
import logging

import anthropic
import requests

from . import s2_client
from .config import AppConfig
from .llm_cache import LLMCache
from .models import LandmarkPaper, PaperSummary, ScoredPaper

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_CACHE_DIR = "data/cache/llm/landmarks"
_OPENALEX_URL = "https://api.openalex.org/works"
_S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_API_KEY = s2_client.API_KEY   # whether an S2 key is configured
_MAILTO = "survey-miner@example.com"
_NO_PROXY = {"http": None, "https": None}

_SCHEMA = """
{
  "landmarks": [
    {
      "name": "Self-RAG",
      "full_title": "Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection",
      "why_seminal": "Introduced self-reflective retrieval that agentic RAG systems build on.",
      "mentioned_by": 3
    }
  ]
}
""".strip()

_SYSTEM = (
    "You are an expert who identifies the SEMINAL PRIMARY papers that a set of "
    "survey papers collectively build upon. "
    "A primary paper introduces a concrete method, model, or system (e.g. ReAct, "
    "Self-RAG, FLARE, DPR, Toolformer) — NOT another survey. "
    "Only list works that clearly fall within the surveyed topic AND that multiple "
    "of the provided surveys would cite as foundational. "
    "Estimate 'mentioned_by' as the number of the provided surveys that build on or "
    "reference each work, based on their scopes, methods, and taxonomies. "
    "Prefer well-known, highly-cited works. Do not invent obscure papers. "
    "Return ONLY valid JSON matching the schema."
)


class LandmarkDetector:
    """Detects seminal primary papers the surveys are built on."""

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for landmark detection.")
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._cfg = cfg
        self._cache = LLMCache(_CACHE_DIR)

    def detect(
        self,
        topic: str,
        summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
    ) -> list[LandmarkPaper]:
        """Return high-impact landmark primary papers for `topic`."""
        if not summary_pairs:
            return []

        candidates = self._extract_candidates(topic, summary_pairs)
        if not candidates:
            return []

        # Filter by how many surveys reference each candidate
        min_mentions = self._cfg.landmark_min_mentions
        candidates = [c for c in candidates if int(c.get("mentioned_by", 0)) >= min_mentions]
        logger.info(
            "[landmarks] %d candidate(s) referenced by >= %d surveys for '%s'",
            len(candidates), min_mentions, topic,
        )

        # Citation floor: Semantic Scholar gives accurate counts, but the
        # keyless pool is rate-limited so we fall back to OpenAlex, which
        # severely undercounts seminal papers. Without an S2 key, cap the floor
        # low so genuine landmarks (resolved via OpenAlex) aren't filtered out.
        floor = self._cfg.landmark_min_citations
        if not _S2_API_KEY:
            floor = min(floor, 15)
            logger.info(
                "[landmarks] no SEMANTIC_SCHOLAR_API_KEY — using OpenAlex counts; "
                "citation floor capped at %d. Set the key for accurate counts.",
                floor,
            )

        # Resolve each (Semantic Scholar → OpenAlex) and keep high-impact ones
        landmarks: list[LandmarkPaper] = []
        seen_titles: set[str] = set()
        for c in candidates:
            name = str(c.get("name", "")).strip()
            query = str(c.get("full_title") or name).strip()
            if not query:
                continue
            meta = _resolve_paper(query)
            if not meta:
                logger.debug("[landmarks] no match for %r", query)
                continue
            if meta["citation_count"] < floor:
                logger.debug(
                    "[landmarks] dropping %r — only %d citations (< %d)",
                    meta["title"][:60], meta["citation_count"], floor,
                )
                continue
            tkey = meta["title"].lower()
            if tkey in seen_titles:
                continue
            seen_titles.add(tkey)
            landmarks.append(LandmarkPaper(
                name=name or meta["title"],
                title=meta["title"],
                year=meta["year"],
                citation_count=meta["citation_count"],
                url=meta["url"],
                mentioned_by=int(c.get("mentioned_by", 0)),
                why_seminal=str(c.get("why_seminal", "")),
            ))

        # Most-referenced first, then most-cited; cap the count
        landmarks.sort(key=lambda lm: (lm.mentioned_by, lm.citation_count), reverse=True)
        landmarks = landmarks[: self._cfg.landmark_max_count]
        logger.info(
            "[landmarks] %d landmark paper(s) kept for '%s' (>= %d citations)",
            len(landmarks), topic, self._cfg.landmark_min_citations,
        )
        return landmarks

    # ────────────────────────────────────────────────────────────────────
    def _extract_candidates(
        self,
        topic: str,
        summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
    ) -> list[dict]:
        prompt = _build_prompt(topic, summary_pairs)
        cache_key = LLMCache.make_key(topic, prompt, _MODEL)
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.info("[landmarks] cache hit for '%s'", topic)
            return cached.get("landmarks", [])

        try:
            with self._client.messages.stream(
                model=_MODEL,
                max_tokens=2000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                raw = _strip_fences(stream.get_final_text())
            data = json.loads(raw)
            self._cache.set(cache_key, data, label=topic[:60], model=_MODEL)
            return data.get("landmarks", [])
        except Exception as exc:
            logger.warning("[landmarks] extraction failed for '%s': %s", topic, exc)
            return []


def _build_prompt(
    topic: str,
    summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
) -> str:
    survey_blocks: list[str] = []
    for sp, s in summary_pairs:
        parts = [f"- Survey: {sp.paper.title}"]
        if not s.summarization_failed:
            if s.research_scope:
                parts.append(f"  Scope: {s.research_scope}")
            if s.main_methods:
                parts.append(f"  Methods: {', '.join(s.main_methods[:8])}")
            if s.taxonomy:
                parts.append(f"  Taxonomy: {', '.join(s.taxonomy[:8])}")
            if s.representative_papers_or_models:
                parts.append(
                    f"  Named works/models: {', '.join(s.representative_papers_or_models[:8])}"
                )
        survey_blocks.append("\n".join(parts))

    return (
        f"Research topic: {topic}\n\n"
        f"The following {len(summary_pairs)} survey papers were analysed:\n\n"
        + "\n\n".join(survey_blocks)
        + "\n\nIdentify the seminal PRIMARY papers (concrete methods/models/systems, "
        "not surveys) that these surveys most repeatedly build upon. "
        f"Return JSON matching:\n{_SCHEMA}"
    )


def _resolve_paper(query: str) -> dict | None:
    """
    Resolve a paper name/title to metadata, trying Semantic Scholar first
    (best coverage of ML preprints AND accurate citation counts) and falling
    back to OpenAlex.  Returns {title, year, citation_count, url} or None.
    """
    return _semantic_scholar_lookup(query) or _openalex_lookup(query)


def _pick_best(query: str, candidates: list[dict]) -> dict | None:
    """
    Choose the candidate whose title best matches `query`.

    Each candidate is {title, cites, year, url}.  We require the title to
    contain the query's words (token_set_ratio >= 85), then among the closest
    full-title matches (within 5 pts of the best token_sort_ratio) pick the
    MOST-CITED — selecting the canonical record over duplicates while rejecting
    superset titles (e.g. "RocketQA … Dense Passage Retrieval" vs the real DPR).
    """
    from rapidfuzz import fuzz

    q_norm = _norm(query)
    scored = []
    for c in candidates:
        title = (c.get("title") or "").strip()
        if not title:
            continue
        scored.append({
            "set": fuzz.token_set_ratio(q_norm, _norm(title)),
            "sort": fuzz.token_sort_ratio(q_norm, _norm(title)),
            "cites": c.get("cites", 0) or 0,
            "item": c,
        })

    matches = [s for s in scored if s["set"] >= 85]
    if not matches:
        return None
    top_sort = max(s["sort"] for s in matches)
    if top_sort < 70:
        return None
    near = [s for s in matches if s["sort"] >= top_sort - 5]
    return max(near, key=lambda s: s["cites"])["item"]


def _semantic_scholar_lookup(query: str) -> dict | None:
    """Resolve via the Semantic Scholar search API (shared global rate limiter)."""
    params = {
        "query": query,
        "limit": 6,
        "fields": "title,year,citationCount,externalIds,url",
    }
    resp = s2_client.get(_S2_SEARCH_URL, params=params)
    if resp is None or resp.status_code != 200:
        return None
    try:
        data = resp.json().get("data", [])
    except Exception as exc:
        logger.debug("[landmarks] S2 lookup failed for %r: %s", query, exc)
        return None
    if not data:
        return None

    candidates = []
    for d in data:
        ext = d.get("externalIds") or {}
        doi = ext.get("DOI")
        arxiv = ext.get("ArXiv")
        url = (
            f"https://doi.org/{doi}" if doi
            else f"https://arxiv.org/abs/{arxiv}" if arxiv
            else d.get("url") or ""
        )
        candidates.append({
            "title": d.get("title") or "",
            "cites": d.get("citationCount") or 0,
            "year": d.get("year"),
            "url": url,
        })

    best = _pick_best(query, candidates)
    if not best:
        return None
    return {
        "title": best["title"].strip(),
        "year": best.get("year"),
        "citation_count": best.get("cites", 0),
        "url": best.get("url", ""),
    }


def _openalex_lookup(query: str) -> dict | None:
    """Resolve via OpenAlex title search (fallback when S2 misses)."""
    params = {
        "filter": f"title.search:{query}",
        "per-page": 25,
        "select": "id,title,publication_year,cited_by_count,doi",
        "mailto": _MAILTO,
    }
    try:
        resp = requests.get(_OPENALEX_URL, params=params, timeout=10, proxies=_NO_PROXY)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:
        logger.debug("[landmarks] OpenAlex lookup failed for %r: %s", query, exc)
        return None

    candidates = []
    for r in results:
        oa_id = r.get("id") or ""
        doi = (r.get("doi") or "").strip()
        candidates.append({
            "title": r.get("title") or "",
            "cites": r.get("cited_by_count", 0) or 0,
            "year": r.get("publication_year"),
            "url": doi or oa_id,
        })

    best = _pick_best(query, candidates)
    if not best:
        return None
    return {
        "title": best["title"].strip(),
        "year": best.get("year"),
        "citation_count": best.get("cites", 0),
        "url": best.get("url", ""),
    }


def _norm(text: str) -> str:
    import re
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
