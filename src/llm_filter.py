"""
LLM-based relevance classifier.

Uses claude-haiku (cheapest/fastest model) to binary-classify each paper as
relevant or irrelevant to the configured research topics.

Run after the keyword filters to catch papers that slipped through token-overlap
checks but are clearly off-topic when read in context — e.g. a "computer vision"
activity-recognition paper surfacing in an "AI in education" search because it
shares the word "computer".

Design:
- Batches 20 papers per API call to keep cost low (~$0.01–0.05 per full run).
- Fails open: if an API call errors, the whole batch is kept (no silent data loss).
- Skips gracefully if ANTHROPIC_API_KEY is not configured.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from .config import AppConfig
from .models import Paper

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_BATCH_SIZE = 20

_SYSTEM_PROMPT = (
    "You are a strict topical relevance filter for an academic paper search pipeline. "
    "Your job is to remove papers that do not specifically survey the given research topics. "
    "Be conservative: when uncertain, filter out (false). It is better to miss a borderline "
    "paper than to keep noise.\n\n"

    "Mark TRUE only when ALL of the following hold:\n"
    "1. The paper IS a survey, review, overview, taxonomy, or systematic literature review "
    "(not a primary research paper introducing a new method or model).\n"
    "2. The paper's CORE SUBJECT is one of the given research topics — not merely a "
    "domain that applies the technology, or a tool that uses it as a component.\n\n"

    "Always mark FALSE for:\n"
    "- Papers NOT written in English (e.g. a Portuguese, Spanish, or Chinese title).\n"
    "- Tool / framework papers that USE the technology for a different purpose.\n"
    "  Example: 'LatteReview: A Multi-Agent Framework for Conducting Systematic Reviews' "
    "  uses agents as a mechanism to run reviews — it does NOT survey agentic AI or RAG.\n"
    "- Primary research / system papers that introduce ONE new framework, model, or "
    "  implementation (even if titled 'comprehensive' and including a related-work section).\n"
    "- Domain-specific application papers when the topic is general.\n"
    "  Example: 'RAG for Biomedical Question Answering', 'Agentic AI in Remote Sensing', "
    "  'Multi-Agent RAG for Clinical Decision Support', '... in Finance', '... in Agriculture' "
    "  are NOT relevant to a general 'Agentic RAG' topic.\n"
    "- Over-broad overviews where the topic is only a minor subsection "
    "  (e.g. an 'AGI and GenAI trends' review when the topic is 'Agentic RAG').\n"
    "- Papers that only mention the topic in passing or use it as a baseline.\n"
    "- Papers about a clearly different topic that shares surface-level keywords.\n\n"

    "Return ONLY a JSON array of booleans, one per paper, in the same order as the input. "
    "No explanation, no prose, no markdown."
)


def llm_filter_papers(
    papers: list[Paper],
    cfg: AppConfig,
) -> list[Paper]:
    """
    Filter `papers` using LLM binary relevance classification.

    Runs only when ANTHROPIC_API_KEY is configured.  Without a key, logs a
    warning and returns all papers unchanged so the rest of the pipeline still
    works.

    Returns the subset of papers that the LLM judged relevant to at least one
    of the topics in `cfg.topics`.
    """
    if not cfg.anthropic_api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set — skipping LLM relevance filter. "
            "Off-topic papers may remain. Set the key in .env to enable this filter."
        )
        return papers

    if not papers:
        return papers

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    topics_text = "\n".join(f"- {t}" for t in cfg.topics)

    # Split into batches and classify all in parallel
    batches = [
        papers[i : i + _BATCH_SIZE]
        for i in range(0, len(papers), _BATCH_SIZE)
    ]

    # Each future carries its batch index so we can reconstruct order
    batch_results: dict[int, list[bool]] = {}
    with ThreadPoolExecutor(max_workers=min(len(batches), 8)) as executor:
        future_to_idx = {
            executor.submit(_classify_batch, client, batch, topics_text): idx
            for idx, batch in enumerate(batches)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                batch_results[idx] = future.result()
            except Exception as exc:
                logger.warning(
                    "LLM filter batch %d failed: %s — keeping all", idx, exc
                )
                batch_results[idx] = [True] * len(batches[idx])

    kept: list[Paper] = []
    removed_titles: list[str] = []

    for idx, batch in enumerate(batches):
        mask = batch_results.get(idx, [True] * len(batch))
        for paper, is_relevant in zip(batch, mask):
            if is_relevant:
                kept.append(paper)
            else:
                removed_titles.append(paper.title)
                logger.debug("LLM filter removed: '%s'", paper.title[:100])

    if removed_titles:
        logger.info(
            "LLM relevance filter: kept %d / %d papers (%d removed). "
            "Removed examples: %s",
            len(kept), len(papers), len(removed_titles),
            "; ".join(t[:60] for t in removed_titles[:3]),
        )
    else:
        logger.info(
            "LLM relevance filter: kept all %d papers (none removed)", len(papers)
        )

    return kept


# ── Internal helpers ──────────────────────────────────────────────────────────

def _classify_batch(
    client: anthropic.Anthropic,
    papers: list[Paper],
    topics_text: str,
) -> list[bool]:
    """
    Ask the LLM to classify one batch.  Returns a parallel list of booleans.
    On any failure, returns all True (keep all) to avoid silent data loss.
    """
    paper_blocks = []
    for i, p in enumerate(papers, start=1):
        snippet = (p.abstract or "")[:500].replace("\n", " ")
        paper_blocks.append(
            f"{i}. Title: {p.title}\n"
            f"   Abstract: {snippet or '[No abstract available]'}"
        )

    prompt = (
        f"Research topics being surveyed:\n{topics_text}\n\n"
        f"For each paper below, mark TRUE only if it is an English-language SURVEY/REVIEW "
        f"whose PRIMARY subject is one of the topics above, covering the general field "
        f"(not one narrow application vertical). Mark FALSE if it is non-English, a "
        f"tool/system/primary-research paper, a domain-specific application, an over-broad "
        f"overview, or off-topic.\n\n"
        + "\n\n".join(paper_blocks)
        + f"\n\nReturn a JSON array of exactly {len(papers)} booleans in input order."
    )

    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if the model wraps the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        if not isinstance(result, list):
            logger.warning(
                "LLM returned non-list for batch of %d — keeping all", len(papers)
            )
            return [True] * len(papers)

        if len(result) != len(papers):
            logger.warning(
                "LLM returned %d classifications for %d papers — keeping all",
                len(result), len(papers),
            )
            return [True] * len(papers)

        return [bool(x) for x in result]

    except Exception as exc:
        logger.warning(
            "LLM relevance classification failed for batch of %d: %s — keeping all",
            len(papers), exc,
        )
        return [True] * len(papers)
