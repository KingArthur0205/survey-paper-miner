"""
Reading path generator.

Given the FieldMegaArchitecture and a set of LLM-judged papers for one topic,
generates a sequenced reading plan (ReadingPath) that guides a newcomer through
the field in a logical order: foundational → current-standard → emerging.

Usage:
    generator = ReadingPathGenerator(cfg)
    path = generator.generate(topic, mega, judge_triples, max_papers=10)
"""

from __future__ import annotations

import json
import logging

import anthropic

from .config import AppConfig
from .models import (
    FieldMegaArchitecture,
    JudgeResult,
    PaperSummary,
    ReadingPath,
    ReadingStep,
    ScoredPaper,
)

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_SCHEMA = """
{
  "target_audience": "e.g. 'graduate student new to the field'",
  "steps": [
    {
      "step": 1,
      "paper_title": "exact title from input",
      "rationale": "One sentence: why read this at this step.",
      "focus_sections": ["Introduction", "Background"],
      "prereq_concepts": ["concept A"],
      "estimated_reading_time": "30 min"
    }
  ]
}
""".strip()

_SYSTEM = (
    "You are an experienced research mentor. "
    "Given a list of survey papers (with authority tiers and judge assessments), "
    "produce a reading path for a newcomer to this field. "
    "Order: foundational papers first, then current-standard, then emerging. "
    "Within each tier, prefer must_read and worth_reading papers. "
    "Include only papers from the provided list. "
    "paper_title must match one of the provided titles exactly. "
    "Limit to the requested number of steps. "
    "Return ONLY valid JSON matching the schema. Be concise."
)


class ReadingPathGenerator:
    """Generates a sequenced newcomer reading path for one research field."""

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for reading path generation.")
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    def generate(
        self,
        topic: str,
        mega: FieldMegaArchitecture,
        judge_triples: list[tuple[ScoredPaper, PaperSummary, JudgeResult]],
        max_papers: int = 10,
    ) -> ReadingPath:
        """Generate a reading path for newcomers."""
        # Filter to actionable papers only
        usable = [
            (sp, summary, jr) for sp, summary, jr in judge_triples
            if not jr.judge_failed
            and jr.recommended_action in ("must_read", "worth_reading")
        ]
        if not usable:
            # Fall back to all non-failed papers
            usable = [(sp, s, jr) for sp, s, jr in judge_triples if not jr.judge_failed]

        if not usable:
            return ReadingPath(
                topic=topic,
                generation_failed=True,
                failure_reason="No judged papers available to build a reading path.",
            )

        prompt = _build_prompt(topic, mega, usable, max_papers)

        try:
            logger.info("[reading_path] Generating reading path for '%s' (%d papers)", topic, len(usable))
            with self._client.messages.stream(
                model=_MODEL,
                max_tokens=30000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                raw = _strip_fences(stream.get_final_text())
            data = json.loads(raw)
            return _build_reading_path(topic, data)
        except json.JSONDecodeError as exc:
            logger.error("[reading_path] JSON decode error for '%s': %s", topic, exc)
            return ReadingPath(
                topic=topic,
                generation_failed=True,
                failure_reason=f"JSON decode error: {exc}",
            )
        except Exception as exc:
            logger.error("[reading_path] LLM call failed for '%s': %s", topic, exc)
            return ReadingPath(
                topic=topic,
                generation_failed=True,
                failure_reason=str(exc),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(
    topic: str,
    mega: FieldMegaArchitecture,
    judge_triples: list[tuple[ScoredPaper, PaperSummary, JudgeResult]],
    max_papers: int,
) -> str:
    # Sort: foundational first, then current_standard, then emerging
    tier_order = {"foundational": 0, "current_standard": 1, "emerging": 2}
    action_order = {"must_read": 0, "worth_reading": 1, "optional": 2}

    sorted_triples = sorted(
        judge_triples,
        key=lambda t: (
            tier_order.get(t[0].paper.authority_tier or "", 3),
            action_order.get(t[2].recommended_action or "", 3),
        ),
    )

    papers_data = []
    for sp, _, jr in sorted_triples[:max_papers * 2]:   # send up to 2× pool
        papers_data.append({
            "title": sp.paper.title,
            "year": sp.paper.year,
            "authority_tier": sp.paper.authority_tier or "unknown",
            "recommended_action": jr.recommended_action,
            "authority_assessment": jr.authority_assessment,
            "scope_clarity": jr.scope_clarity,
            "strengths": jr.strengths[:2],
        })

    field_context = {
        "field_summary": mega.field_summary,
        "core_problems": [cp.get("problem", "") for cp in mega.core_problems[:4]],
        "method_families": list(mega.method_families.keys())[:6],
    }

    return (
        f"Topic: {topic}\n\n"
        f"Field context:\n{json.dumps(field_context, indent=2, ensure_ascii=False)}\n\n"
        f"Available papers (sorted by tier then importance):\n"
        f"{json.dumps(papers_data, indent=2, ensure_ascii=False)}\n\n"
        f"Generate a reading path with at most {max_papers} steps. "
        f"Return JSON matching:\n{_SCHEMA}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON → model
# ─────────────────────────────────────────────────────────────────────────────

def _build_reading_path(topic: str, data: dict) -> ReadingPath:
    steps: list[ReadingStep] = []
    for s in (data.get("steps") or []):
        if not isinstance(s, dict):
            continue
        steps.append(ReadingStep(
            step=int(s.get("step", len(steps) + 1)),
            paper_title=str(s.get("paper_title", "")),
            rationale=str(s.get("rationale", "")),
            focus_sections=[str(x) for x in (s.get("focus_sections") or [])],
            prereq_concepts=[str(x) for x in (s.get("prereq_concepts") or [])],
            estimated_reading_time=str(s.get("estimated_reading_time", "")),
        ))

    logger.info("[reading_path] Generated %d reading steps for '%s'", len(steps), topic)
    return ReadingPath(
        topic=topic,
        target_audience=str(data.get("target_audience", "")),
        steps=steps,
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
