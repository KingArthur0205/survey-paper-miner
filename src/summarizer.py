"""
LLM Summarizer.

Generates a structured JSON summary of each paper using the Anthropic API
(claude-sonnet-4-6 by default).

Design principles:
- Summarises from the abstract only (MVP; full-text in Phase 2).
- Uses a strict JSON schema prompt to minimise hallucination.
- The model is explicitly instructed to use empty lists rather than inventing
  datasets, benchmarks, or model names that are not mentioned in the abstract.
- Fields marked [INFERRED] indicate information extrapolated beyond the
  abstract text — the caller should treat these with lower confidence.
- Retries once on malformed JSON before marking the paper as failed.
- All summaries include a `summarization_source: "abstract"` flag so
  downstream users know the provenance.
"""

from __future__ import annotations

import json
import logging

import anthropic

from .config import AppConfig
from .llm_cache import LLMCache
from .models import Paper, PaperSummary, ScoredPaper

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_CACHE_DIR = "data/cache/llm/summaries"

# JSON schema description passed to the model so it always returns parseable output
_SCHEMA_DESCRIPTION = """
{
  "research_scope": "string — what domain / subfield this survey covers",
  "core_problem": "string — the central research problem or gap the survey addresses",
  "taxonomy": ["list of taxonomy categories or themes identified"],
  "main_methods": ["list of key methods, approaches, or techniques surveyed"],
  "representative_papers_or_models": ["ONLY mention if explicitly named in the abstract; otherwise []"],
  "datasets_and_benchmarks": ["ONLY mention if explicitly named in the abstract; otherwise []"],
  "evaluation_metrics": ["metrics mentioned in the abstract; otherwise []"],
  "main_findings": ["key conclusions or insights (2–4 bullet points)"],
  "limitations": ["limitations acknowledged in the abstract; otherwise []"],
  "future_directions": ["future directions mentioned; otherwise []"],
  "keywords": {
    "tasks": ["NLP/CV/ML tasks"],
    "methods": ["algorithmic methods"],
    "models": ["named models or architectures"],
    "datasets": ["dataset names"],
    "evaluation": ["evaluation protocol terms"],
    "risks": ["safety/bias/fairness terms if mentioned"]
  },
  "citation_use_cases": ["2–3 reasons a researcher might cite this survey"]
}
""".strip()

_SYSTEM_PROMPT = (
    "You are a precise academic assistant that extracts structured information "
    "from academic paper abstracts. "
    "You must return ONLY valid JSON matching the schema given. "
    "Do not invent information not present in the abstract. "
    "For any field where information is unavailable, return an empty string or empty list. "
    "Never hallucinate dataset names, model names, or benchmark names."
)


class LLMSummarizer:
    """
    Wraps the Anthropic API to produce structured PaperSummary objects.
    Processes papers one at a time to give clear per-paper error handling.
    """

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file or set the environment variable."
            )
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._cache = LLMCache(_CACHE_DIR)

    def summarize_top_n(
        self,
        scored_papers: list[ScoredPaper],
        top_n: int,
    ) -> list[tuple[ScoredPaper, PaperSummary]]:
        """
        Summarise the top-N papers by quality score.

        Returns a list of (ScoredPaper, PaperSummary) pairs in rank order.
        Papers that fail summarisation are included with `summarization_failed=True`.
        """
        candidates = scored_papers[:top_n]
        results = []

        for rank, sp in enumerate(candidates, start=1):
            logger.info(
                "Summarising paper %d/%d: %s", rank, len(candidates), sp.paper.title[:80]
            )
            summary = self._summarize_one(sp.paper)
            results.append((sp, summary))

        hits = self._cache.hits
        total = len(candidates)
        if hits:
            logger.info(
                "Summarisation cache: %d/%d hit (saved ~%d LLM call%s)",
                hits, total, hits, "s" if hits != 1 else "",
            )
        return results

    def _summarize_one(self, paper: Paper) -> PaperSummary:
        """Summarise a single paper; checks the cache first, retries once on bad JSON."""
        cache_key = LLMCache.make_key(paper.title, paper.abstract or "", _MODEL)
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.info("  ↩ cache hit — skipping LLM call for '%s'", paper.title[:70])
            return _build_summary(paper.title, cached)

        prompt = _build_prompt(paper)

        for attempt in range(2):
            try:
                # Streaming is required for max_tokens values that may exceed 10 k.
                # We use it here unconditionally for consistency and to avoid the
                # "Streaming required" error if the schema grows.
                with self._client.messages.stream(
                    model=_MODEL,
                    max_tokens=4096,
                    system=_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    raw_text = _strip_fences(stream.get_final_text())

                data = json.loads(raw_text)
                self._cache.set(cache_key, data, label=paper.title[:70], model=_MODEL)
                return _build_summary(paper.title, data)

            except json.JSONDecodeError as e:
                if attempt == 0:
                    # Try to salvage a truncated response before retrying
                    repaired = _repair_truncated_json(raw_text)
                    if repaired is not None:
                        logger.warning(
                            "Repaired truncated JSON for '%s' (attempt 1)",
                            paper.title[:60],
                        )
                        self._cache.set(cache_key, repaired, label=paper.title[:70], model=_MODEL)
                        return _build_summary(paper.title, repaired)
                    logger.warning(
                        "JSON decode error for '%s' (attempt 1), retrying. Error: %s",
                        paper.title[:60], e,
                    )
                    continue
                logger.error("Failed to parse LLM output for '%s': %s", paper.title[:60], e)
                return PaperSummary(
                    paper_title=paper.title,
                    summarization_failed=True,
                    failure_reason=f"JSON decode error: {e}",
                )
            except Exception as e:
                logger.error("LLM summarization failed for '%s': %s", paper.title[:60], e)
                return PaperSummary(
                    paper_title=paper.title,
                    summarization_failed=True,
                    failure_reason=str(e),
                )

        return PaperSummary(
            paper_title=paper.title,
            summarization_failed=True,
            failure_reason="Unknown error after retries",
        )


def _build_prompt(paper: Paper) -> str:
    """Construct the user prompt for one paper."""
    lines = [
        f"Title: {paper.title}",
        f"Year: {paper.year or 'Unknown'}",
        f"Venue: {paper.venue or 'Unknown'}",
    ]
    if paper.authors:
        lines.append(f"Authors: {', '.join(paper.authors[:5])}")
    if paper.abstract:
        lines.append(f"\nAbstract:\n{paper.abstract}")
    else:
        lines.append("\nAbstract: [Not available — base your response only on the title and venue]")

    lines.append(
        f"\nReturn a JSON object matching this schema exactly:\n{_SCHEMA_DESCRIPTION}"
    )
    return "\n".join(lines)


def _build_summary(title: str, data: dict) -> PaperSummary:
    """Construct a PaperSummary from a parsed JSON dict."""
    return PaperSummary(
        paper_title=title,
        research_scope=data.get("research_scope", ""),
        core_problem=data.get("core_problem", ""),
        taxonomy=_as_list(data.get("taxonomy")),
        main_methods=_as_list(data.get("main_methods")),
        representative_papers_or_models=_as_list(data.get("representative_papers_or_models")),
        datasets_and_benchmarks=_as_list(data.get("datasets_and_benchmarks")),
        evaluation_metrics=_as_list(data.get("evaluation_metrics")),
        main_findings=_as_list(data.get("main_findings")),
        limitations=_as_list(data.get("limitations")),
        future_directions=_as_list(data.get("future_directions")),
        keywords=data.get("keywords") if isinstance(data.get("keywords"), dict) else {},
        citation_use_cases=_as_list(data.get("citation_use_cases")),
        summarization_source="abstract",
        summarization_failed=False,
    )


def _as_list(val) -> list[str]:
    """Coerce a value to a list[str], returning [] for None/non-list."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val]
    return [str(val)]


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that the model sometimes wraps JSON in."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _repair_truncated_json(text: str) -> dict | None:
    """
    Attempt to close a JSON object that was cut off mid-stream.

    Walks the text character-by-character tracking bracket depth and string
    state, then appends the minimum closing brackets to make it valid JSON.
    Returns the parsed dict on success, or None if it cannot be repaired.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    last_comma_depth1 = -1
    stack_at_last_comma: list[str] = []

    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append(ch)
        elif ch in ("}", "]"):
            if stack:
                stack.pop()
        elif ch == "," and len(stack) == 1:
            last_comma_depth1 = i
            stack_at_last_comma = stack.copy()

    if last_comma_depth1 == -1:
        return None

    trimmed = text[:last_comma_depth1].rstrip()
    closers = {"{": "}", "[": "]"}
    suffix = "".join(closers[s] for s in reversed(stack_at_last_comma))
    try:
        return json.loads(trimmed + suffix)
    except Exception:
        return None
