"""
Beginner field guide generator.

Produces a narrative FieldGuide for a research field — a plain-English
explanation suitable for a newcomer with no prior background.  The guide
is generated from the FieldMegaArchitecture and a sample of paper summaries.

Usage:
    generator = FieldGuideGenerator(cfg)
    guide = generator.generate(topic, mega, summaries)
    md = render_field_guide_markdown(guide)
"""

from __future__ import annotations

import json
import logging

import anthropic

from .config import AppConfig
from .models import FieldGuide, FieldMegaArchitecture, PaperSummary, ScoredPaper

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_SCHEMA = """
{
  "what_is_this_field": "2-3 sentence plain-English description",
  "why_it_matters": "1-2 sentences on real-world impact",
  "core_problems": ["problem 1", "problem 2", "problem 3"],
  "main_approaches": ["approach 1", "approach 2"],
  "key_terms": {
    "term 1": "plain-English definition in one sentence",
    "term 2": "plain-English definition in one sentence"
  },
  "historical_milestones": [
    "Year X: first notable achievement",
    "Year Y: key breakthrough"
  ],
  "common_misconceptions": [
    "Misconception: <wrong belief>. Reality: <correct view>."
  ],
  "how_to_get_started": [
    "Step 1: read ...",
    "Step 2: implement ..."
  ]
}
""".strip()

_SYSTEM = (
    "You are a science communicator writing for a smart newcomer (e.g. a final-year "
    "undergraduate or someone pivoting from a different field). "
    "Write in plain English — no unexplained jargon. "
    "Be accurate but accessible. "
    "key_terms should have 5–10 entries covering essential vocabulary. "
    "historical_milestones should be 3–6 entries with approximate years. "
    "common_misconceptions should list 2–4 items. "
    "how_to_get_started should list 3–5 actionable steps. "
    "Return ONLY valid JSON matching the schema."
)


class FieldGuideGenerator:
    """Generates a beginner-friendly narrative guide for one research field."""

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for field guide generation.")
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    def generate(
        self,
        topic: str,
        mega: FieldMegaArchitecture,
        summaries: list[tuple[ScoredPaper, PaperSummary]],
    ) -> FieldGuide:
        """Generate a beginner field guide."""
        prompt = _build_prompt(topic, mega, summaries)

        try:
            logger.info("[field_guide] Generating field guide for '%s'", topic)
            with self._client.messages.stream(
                model=_MODEL,
                max_tokens=30000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                raw = _strip_fences(stream.get_final_text())
            data = json.loads(raw)
            return _build_guide(topic, data)
        except json.JSONDecodeError as exc:
            logger.error("[field_guide] JSON decode error for '%s': %s", topic, exc)
            return FieldGuide(
                topic=topic,
                generation_failed=True,
                failure_reason=f"JSON decode error: {exc}",
            )
        except Exception as exc:
            logger.error("[field_guide] LLM call failed for '%s': %s", topic, exc)
            return FieldGuide(
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
    summaries: list[tuple[ScoredPaper, PaperSummary]],
) -> str:
    field_data: dict = {
        "topic": topic,
        "field_summary": mega.field_summary,
        "core_problems": [cp.get("problem", "") for cp in mega.core_problems[:5]],
        "method_families": list(mega.method_families.keys())[:6],
        "applications": mega.applications[:6],
        "challenges": list(mega.challenges.keys())[:5],
        "future_directions": mega.future_research_directions[:4],
    }

    # Add a sample of paper abstracts / scopes to give the LLM vocabulary
    sample_abstracts = []
    for sp, s in summaries[:8]:
        if not s.summarization_failed and s.research_scope:
            sample_abstracts.append({
                "title": sp.paper.title,
                "year": sp.paper.year,
                "scope": s.research_scope,
            })

    return (
        f"Topic: {topic}\n\n"
        f"Field mega-architecture:\n{json.dumps(field_data, indent=2, ensure_ascii=False)}\n\n"
        f"Sample paper scopes:\n{json.dumps(sample_abstracts, indent=2, ensure_ascii=False)}\n\n"
        f"Generate a beginner field guide. Return JSON matching:\n{_SCHEMA}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON → model
# ─────────────────────────────────────────────────────────────────────────────

def _build_guide(topic: str, data: dict) -> FieldGuide:
    key_terms_raw = data.get("key_terms") or {}
    if isinstance(key_terms_raw, list):
        # Sometimes LLMs return a list of {term, definition} objects
        key_terms = {}
        for item in key_terms_raw:
            if isinstance(item, dict):
                term = str(item.get("term", ""))
                defn = str(item.get("definition", ""))
                if term:
                    key_terms[term] = defn
    elif isinstance(key_terms_raw, dict):
        key_terms = {str(k): str(v) for k, v in key_terms_raw.items()}
    else:
        key_terms = {}

    guide = FieldGuide(
        topic=topic,
        what_is_this_field=str(data.get("what_is_this_field", "")),
        why_it_matters=str(data.get("why_it_matters", "")),
        core_problems=[str(x) for x in (data.get("core_problems") or [])],
        main_approaches=[str(x) for x in (data.get("main_approaches") or [])],
        key_terms=key_terms,
        historical_milestones=[str(x) for x in (data.get("historical_milestones") or [])],
        common_misconceptions=[str(x) for x in (data.get("common_misconceptions") or [])],
        how_to_get_started=[str(x) for x in (data.get("how_to_get_started") or [])],
    )
    logger.info(
        "[field_guide] Guide generated for '%s': %d key terms, %d steps to get started",
        topic, len(guide.key_terms), len(guide.how_to_get_started),
    )
    return guide


# ─────────────────────────────────────────────────────────────────────────────
# Markdown renderer
# ─────────────────────────────────────────────────────────────────────────────

def render_field_guide_markdown(guide: FieldGuide) -> str:
    """
    Render a FieldGuide to a standalone Markdown string.

    Suitable both as a standalone .md file and as Part 0 prepended to the
    architecture report.
    """
    if guide.generation_failed:
        return f"> *Field guide unavailable: {guide.failure_reason}*\n"

    lines: list[str] = [
        f"## What is {guide.topic}?",
        "",
    ]

    if guide.what_is_this_field:
        lines.append(guide.what_is_this_field)
        lines.append("")

    if guide.why_it_matters:
        lines += ["**Why it matters:** " + guide.why_it_matters, ""]

    if guide.core_problems:
        lines += ["### Core Problems", ""]
        for p in guide.core_problems:
            lines.append(f"- {p}")
        lines.append("")

    if guide.main_approaches:
        lines += ["### Main Approaches", ""]
        for a in guide.main_approaches:
            lines.append(f"- {a}")
        lines.append("")

    if guide.key_terms:
        lines += ["### Key Terms", ""]
        lines.append("| Term | Definition |")
        lines.append("|---|---|")
        for term, defn in list(guide.key_terms.items())[:12]:
            # Escape pipe characters so Markdown tables don't break
            safe_defn = defn.replace("|", "\\|")
            lines.append(f"| **{term}** | {safe_defn} |")
        lines.append("")

    if guide.historical_milestones:
        lines += ["### Historical Milestones", ""]
        for m in guide.historical_milestones:
            lines.append(f"- {m}")
        lines.append("")

    if guide.common_misconceptions:
        lines += ["### Common Misconceptions", ""]
        for m in guide.common_misconceptions:
            lines.append(f"- {m}")
        lines.append("")

    if guide.how_to_get_started:
        lines += ["### How to Get Started", ""]
        for i, step in enumerate(guide.how_to_get_started, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
