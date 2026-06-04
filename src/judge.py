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
# Bump when the judge schema or prompt changes so stale cached assessments
# (made with an older rubric) are not reused.
_JUDGE_SCHEMA_VERSION = "v3-fullabstract"

_JUDGE_SCHEMA = """
{
  "is_survey": true,
  "is_domain_specific": false,
  "authority_assessment": "foundational | current_standard | emerging | not_a_survey",
  "scope_clarity": "broad | narrow | unclear",
  "coverage_depth": "comprehensive | partial | shallow",
  "topic_relevance": 4,
  "paper_tier": "core | useful | marginal | cut",
  "strengths": ["strength 1", "strength 2"],
  "weaknesses": ["weakness 1"],
  "recommended_action": "must_read | worth_reading | optional | skip",
  "confidence": 0.85
}
""".strip()

_SYSTEM = (
    "You are an expert academic reviewer deciding which papers belong in a focused "
    "literature review on the user's EXACT research topic. Your job is to separate "
    "primary survey sources from noise. Be strict.\n\n"

    "Assess FOUR independent signals, then assign a final tier.\n\n"

    "1. is_survey (bool): TRUE only if the paper REVIEWS/SURVEYS existing literature. "
    "FALSE if it is a primary research paper, a system/framework/tool paper that "
    "introduces ONE new method or implementation, a benchmark paper, or a position "
    "paper. A title containing 'survey'/'review' is NOT sufficient — a paper that "
    "proposes a novel framework and merely includes a related-work section is NOT a "
    "survey (is_survey=false).\n\n"

    "2. is_domain_specific (bool): TRUE if the paper surveys the topic only WITHIN one "
    "narrow application vertical — e.g. healthcare/clinical/medical, finance/business, "
    "agriculture, materials science, software engineering, law, education, remote "
    "sensing. A general architecture survey is is_domain_specific=false; "
    "'<Topic> for Clinical Decision Support' or '<Topic> in Finance' is "
    "is_domain_specific=true.\n\n"

    "3. topic_relevance (1-5): how specifically the paper addresses the EXACT topic. "
    "1=off-topic (different field), 2=tangential (shared keywords, different focus), "
    "3=related but broader/adjacent (the general area, not the exact topic), "
    "4=directly relevant, 5=exactly this topic.\n\n"

    "4. paper_tier — combine the above into a final inclusion decision:\n"
    "   'core'     = is_survey=true AND topic_relevance>=4 AND is_domain_specific=false. "
    "A primary, general survey of the exact topic.\n"
    "   'useful'   = a genuine survey with topic_relevance 3-4 that provides important "
    "context (foundational background, a key sub-area, or an adjacent survey), "
    "is_domain_specific=false.\n"
    "   'marginal' = ANY of: is_domain_specific=true, OR is_survey=false (system/tool/"
    "primary paper), OR topic_relevance=2. Keep only if domain coverage is wanted.\n"
    "   'cut'      = topic_relevance<=1, OR not in English, OR not a survey AND not "
    "specifically about the topic. Off-topic noise.\n\n"

    "Consistency rules:\n"
    "- If paper_tier is 'marginal' or 'cut', set recommended_action to 'skip'.\n"
    "- If is_survey is false, paper_tier cannot be 'core' or 'useful'.\n"
    "- If is_domain_specific is true, paper_tier cannot be 'core'.\n"
    "- If the abstract is under 100 words, set confidence to at most 0.5.\n"
    "Return ONLY valid JSON. Do not invent information not in the input."
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
        self._topics = cfg.topics          # used in every judge prompt
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
        # Topics are included in the cache key so that judging the same paper
        # for a different research topic always triggers a fresh assessment.
        cache_key = LLMCache.make_key(
            sp.paper.title,
            sp.paper.abstract or "",
            summary.research_scope or "",
            "|".join(sorted(self._topics)),
            _MODEL,
            _JUDGE_SCHEMA_VERSION,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.info("  ↩ cache hit — skipping judge for '%s'", sp.paper.title[:70])
            return _build_result(sp.paper.title, cached)

        prompt = _build_prompt(sp.paper, summary, self._topics)
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

def _build_prompt(paper: Paper, summary: PaperSummary, topics: list[str]) -> str:
    abstract = paper.abstract or ""
    word_count = len(abstract.split())

    lines: list[str] = []

    # Lead with the target topics so the model weights relevance first
    if topics:
        lines.append("Research topics being investigated:")
        for t in topics:
            lines.append(f"  - {t}")
        lines.append("")

    lines += [
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
        # Send the whole abstract (capped generously). The previous 1200-char
        # cut left the model with a mid-sentence fragment, so it kept reporting
        # "abstract is truncated" as a weakness even when the source abstract
        # was complete.
        lines.append(f"\nAbstract:\n{abstract[:4000]}")

    if not summary.summarization_failed:
        if summary.research_scope:
            lines.append(f"\nResearch scope: {summary.research_scope}")
        if summary.taxonomy:
            lines.append(f"Taxonomy: {', '.join(summary.taxonomy[:5])}")

    lines.append(
        f"\nAssess how relevant this paper is to the research topics above, "
        f"and its overall authority. "
        f"Return JSON matching:\n{_JUDGE_SCHEMA}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_VALID_TIERS = ("core", "useful", "marginal", "cut")


def _build_result(title: str, data: dict) -> JudgeResult:
    # Clamp topic_relevance to the 1-5 scale in case the model drifts
    raw_relevance = data.get("topic_relevance", 3)
    try:
        topic_relevance = max(1, min(5, int(raw_relevance)))
    except (TypeError, ValueError):
        topic_relevance = 3

    is_survey = bool(data.get("is_survey", True))
    is_domain_specific = bool(data.get("is_domain_specific", False))

    tier = str(data.get("paper_tier", "")).strip().lower()
    if tier not in _VALID_TIERS:
        tier = _derive_tier(is_survey, is_domain_specific, topic_relevance)

    # Deterministically enforce the consistency rules so a single sloppy LLM
    # response can't promote a domain-specific or non-survey paper to core/useful.
    tier = _enforce_tier_rules(tier, is_survey, is_domain_specific, topic_relevance)

    action = str(data.get("recommended_action", ""))
    if tier in ("marginal", "cut"):
        action = "skip"

    return JudgeResult(
        paper_title=title,
        is_survey=is_survey,
        is_domain_specific=is_domain_specific,
        authority_assessment=str(data.get("authority_assessment", "")),
        scope_clarity=str(data.get("scope_clarity", "")),
        coverage_depth=str(data.get("coverage_depth", "")),
        topic_relevance=topic_relevance,
        paper_tier=tier,
        strengths=[str(s) for s in (data.get("strengths") or [])],
        weaknesses=[str(w) for w in (data.get("weaknesses") or [])],
        recommended_action=action,
        confidence=float(data.get("confidence", 0.0)),
    )


def _derive_tier(is_survey: bool, is_domain_specific: bool, relevance: int) -> str:
    """Derive a tier from the component signals when the LLM omits one."""
    if relevance <= 1 or not is_survey:
        return "cut" if relevance <= 1 else "marginal"
    if relevance == 2 or is_domain_specific:
        return "marginal"
    if relevance >= 4:
        return "core"
    return "useful"   # relevance == 3


def _enforce_tier_rules(
    tier: str, is_survey: bool, is_domain_specific: bool, relevance: int
) -> str:
    """
    Clamp the LLM's tier down to what the component signals allow.  Never
    promotes; only demotes.  This guarantees the documented invariants:
      - non-survey  → at most 'marginal'
      - domain-specific → at most 'useful' (never 'core')
      - relevance <= 1 → 'cut'
      - relevance == 2 → at most 'marginal'
    """
    if relevance <= 1:
        return "cut"
    if not is_survey and tier in ("core", "useful"):
        tier = "marginal"
    if is_domain_specific and tier == "core":
        tier = "useful"
    if relevance == 2 and tier in ("core", "useful"):
        tier = "marginal"
    return tier


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
