"""
LLM-as-Judge authority assessment (section 5.2.9).

A lightweight, focused LLM pass that answers three questions about each paper:
  1. Is it actually a survey/review paper (vs. a primary research paper)?
  2. How authoritative is it (foundational / current_standard / emerging)?
  3. How strongly should a researcher prioritise reading it?

This is intentionally separate from the content summarizer (summarizer.py) so
each prompt is focused on a single task, costs less per call, and failures in
one pass do not affect the other.

Rules enforced in the prompt:
  - If the abstract is very short (< 100 words), confidence ≤ 0.5
  - If `is_survey` is False, `recommended_action` must be "skip"
  - Do not invent information not present in the input
  - If the LLM's authority_assessment contradicts the stratifier's
    authority_tier, the LLM assessment wins but the conflict is logged
"""

from __future__ import annotations

import json
import logging

import anthropic

from .config import AppConfig
from .llm_cache import LLMCache
from .models import JudgeResult, Paper, PaperSummary, ScoredPaper

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_CACHE_DIR = "data/cache/llm/judge"

_JUDGE_SCHEMA = """
{
  "is_survey": true,
  "authority_assessment": "foundational | current_standard | emerging | not_a_survey",
  "scope_clarity": "broad | narrow | unclear",
  "coverage_depth": "comprehensive | partial | shallow",
  "strengths": ["strength 1", "strength 2"],
  "weaknesses": ["weakness 1"],
  "recommended_action": "must_read | worth_reading | optional | skip",
  "confidence": 0.85
}
""".strip()

_SYSTEM = (
    "You are an expert academic reviewer assessing the authority and reading priority "
    "of survey/review papers in AI. "
    "Return ONLY valid JSON matching the schema given. "
    "If is_survey is false, set recommended_action to 'skip'. "
    "If the abstract is under 100 words, set confidence to at most 0.5. "
    "Do not invent information not present in the input."
)


class LLMJudge:
    """
    Runs the authority-assessment LLM pass on top-N summarised papers.

    Usage:
        judge = LLMJudge(cfg)
        judge_results = judge.judge_papers(summary_pairs)
        # judge_results: list[tuple[ScoredPaper, PaperSummary, JudgeResult]]
    """

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set — cannot run LLM judge.")
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._top_n = cfg.judge_top_n
        self._cache = LLMCache(_CACHE_DIR)

    def judge_papers(
        self,
        summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
    ) -> list[tuple[ScoredPaper, PaperSummary, JudgeResult]]:
        """
        Assess the top-N papers.  Returns triples in the same rank order.
        Failed assessments are included with `judge_failed=True`.
        """
        candidates = summary_pairs[: self._top_n]
        results = []
        for i, (sp, summary) in enumerate(candidates, 1):
            logger.info(
                "Judge %d/%d: %s", i, len(candidates), sp.paper.title[:70]
            )
            result = self._judge_one(sp, summary)

            # Log if LLM disagrees with stratifier
            tier = sp.paper.authority_tier
            if (
                tier
                and not result.judge_failed
                and result.authority_assessment
                and result.authority_assessment != tier
                and result.authority_assessment != "not_a_survey"
            ):
                logger.debug(
                    "Judge/stratifier conflict for '%s': stratifier=%s, judge=%s",
                    sp.paper.title[:60], tier, result.authority_assessment,
                )

            results.append((sp, summary, result))

        hits = self._cache.hits
        if hits:
            logger.info(
                "Judge cache: %d/%d hit (saved ~%d LLM call%s)",
                hits, len(candidates), hits, "s" if hits != 1 else "",
            )
        return results

    def _judge_one(self, sp: ScoredPaper, summary: PaperSummary) -> JudgeResult:
        # Cache key: title + abstract + research_scope (scope captures summary quality)
        cache_key = LLMCache.make_key(
            sp.paper.title,
            sp.paper.abstract or "",
            summary.research_scope or "",
            _MODEL,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.info("  ↩ cache hit — skipping judge for '%s'", sp.paper.title[:70])
            return _build_result(sp.paper.title, cached)

        prompt = _build_prompt(sp.paper, summary)
        for attempt in range(2):
            try:
                resp = self._client.messages.create(
                    model=_MODEL,
                    max_tokens=512,
                    system=_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = _strip_fences(resp.content[0].text)
                data = json.loads(raw)
                self._cache.set(cache_key, data, label=sp.paper.title[:70], model=_MODEL)
                return _build_result(sp.paper.title, data)
            except json.JSONDecodeError as e:
                if attempt == 0:
                    logger.warning(
                        "Judge JSON error for '%s', retrying: %s",
                        sp.paper.title[:50], e,
                    )
                    continue
                return JudgeResult(
                    paper_title=sp.paper.title,
                    judge_failed=True,
                    failure_reason=f"JSON decode error: {e}",
                )
            except Exception as e:
                logger.error(
                    "Judge LLM call failed for '%s': %s", sp.paper.title[:50], e
                )
                return JudgeResult(
                    paper_title=sp.paper.title,
                    judge_failed=True,
                    failure_reason=str(e),
                )
        return JudgeResult(
            paper_title=sp.paper.title,
            judge_failed=True,
            failure_reason="Unknown error",
        )


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(paper: Paper, summary: PaperSummary) -> str:
    abstract = paper.abstract or ""
    word_count = len(abstract.split())

    lines = [
        f"Title: {paper.title}",
        f"Year: {paper.year or 'Unknown'}",
        f"Venue: {paper.venue or 'Unknown'}",
        f"Citation count: {paper.citation_count}",
        f"Influential citation count: {paper.influential_citation_count}",
        f"Canonical score (0-1): {paper.canonical_score:.3f}",
        f"Authority tier (from temporal stratifier): {paper.authority_tier or 'none'}",
        f"Abstract word count: {word_count}",
    ]

    if abstract:
        lines.append(f"\nAbstract:\n{abstract[:1200]}")

    if not summary.summarization_failed:
        if summary.research_scope:
            lines.append(f"\nResearch scope: {summary.research_scope}")
        if summary.taxonomy:
            lines.append(f"Taxonomy: {', '.join(summary.taxonomy[:5])}")

    lines.append(
        f"\nAssess this paper's authority and reading priority. "
        f"Return JSON matching:\n{_JUDGE_SCHEMA}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _build_result(title: str, data: dict) -> JudgeResult:
    return JudgeResult(
        paper_title=title,
        is_survey=bool(data.get("is_survey", True)),
        authority_assessment=str(data.get("authority_assessment", "")),
        scope_clarity=str(data.get("scope_clarity", "")),
        coverage_depth=str(data.get("coverage_depth", "")),
        strengths=[str(s) for s in (data.get("strengths") or [])],
        weaknesses=[str(w) for w in (data.get("weaknesses") or [])],
        recommended_action=str(data.get("recommended_action", "")),
        confidence=float(data.get("confidence", 0.0)),
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
