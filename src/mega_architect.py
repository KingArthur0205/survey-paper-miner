"""
Field Mega-Architecture Synthesizer.

Takes all per-paper PaperArchitecture objects for one topic (produced by
ArchitectureAnalyzer) and synthesises them into a unified FieldMegaArchitecture
that covers:

  - A one-paragraph field summary
  - Core problems (with coverage counts and best-paper references)
  - Method families, major tasks, datasets, applications, challenges
  - Research gaps (frequency / future-convergence / conflict)
  - A Mermaid diagram rendered programmatically from the structured data
  - A suggested outline for writing a new survey in this field

The Mermaid diagram is generated from the structured JSON rather than by the
LLM, so the syntax is always valid.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict

import anthropic

from .config import AppConfig
from .models import (
    CrossSurveyComparison,
    FieldMegaArchitecture,
    PaperArchitecture,
    PaperSummary,
    ResearchGap,
    ScoredPaper,
)

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Mermaid mindmap prompt
# ---------------------------------------------------------------------------

_MINDMAP_SYSTEM = """\
You are a Mermaid diagram expert. Generate a Mermaid `mindmap` diagram that
looks like a 思维导图 (mind map) with the research field as the root growing
downward into branches.

STRICT RULES — violating any rule produces an invalid diagram:
1. First line must be exactly: mindmap
2. Root node (second line, 2-space indent): root((Field Name))
   — double parentheses create a circle shape for the root.
3. Category nodes (4-space indent): use plain text, NO shape syntax.
4. Leaf nodes (6-space indent): use plain text, NO shape syntax.
5. Maximum 3 levels (root → category → leaf). No deeper nesting.
6. Keep every label ≤ 35 characters. Truncate with … if needed.
7. Do NOT put parentheses, brackets, or braces INSIDE any label text.
   e.g. write  GPT-4 BERT  not  GPT-4 (BERT)
8. Do NOT use markdown fences, comments, or any text outside the diagram.
9. Suggested top-level categories (pick the most relevant 4-6):
   Research Areas · Methods · Benchmarks & Datasets · Challenges · Research Gaps · Applications
10. Under each category list the 3-5 most important items as leaves.
"""

# ---------------------------------------------------------------------------
# LLM schema for the synthesis pass
# ---------------------------------------------------------------------------

_SYNTH_SCHEMA = """
{
  "field_summary": "2-3 sentence plain-English overview of the field",
  "core_problems": [
    {"problem": "one sentence", "coverage_count": 5, "best_paper": "short title"}
  ],
  "major_tasks": {
    "<task name>": {
      "description": "one sentence",
      "subtasks": ["...", "..."],
      "methods": ["<method-family name this task uses>", "..."],
      "challenges": ["<challenge name this area still faces>", "..."],
      "coverage_count": 4
    }
  },
  "method_families": {
    "<family name>": {
      "description": "one sentence",
      "representative_methods": ["...", "..."],
      "coverage_count": 6
    }
  },
  "datasets_and_benchmarks": [
    {"name": "...", "task": "...", "coverage_count": 3}
  ],
  "evaluation_metrics": ["...", "..."],
  "applications": ["...", "..."],
  "challenges": {
    "<challenge name>": {
      "description": "one sentence",
      "severity": "high | medium | low",
      "coverage_count": 4
    }
  },
  "future_research_directions": ["...", "..."],
  "open_gaps": [
    {
      "gap": "one sentence",
      "gap_type": "frequency | future_convergence | conflict",
      "evidence": ["paper title 1", "paper title 2"],
      "opportunity_score": 0.85,
      "related_challenges": ["<challenge name this gap stems from, or [] for a blue-sky idea>"]
    }
  ],
  "suggested_title_template": "A Survey on [X]: [subtitle]",
  "suggested_abstract_template": "3-4 sentence abstract template",
  "suggested_sections": [
    {"section_number": 1, "title": "Introduction", "content_hints": ["hint 1", "hint 2"]}
  ]
}
""".strip()

_SYNTH_SYSTEM = (
    "You are an expert in academic meta-research. "
    "Given multiple per-paper architecture summaries for one research field, "
    "synthesise them into a single unified field architecture. "
    "coverage_count fields should reflect how many of the input papers cover each element. "
    "For each major_task, the 'methods' list must contain method-family names that "
    "EXACTLY match keys in method_families — these encode which methods each research "
    "area uses (a method may appear under several tasks). "
    "For each major_task, the 'challenges' list must contain challenge names that EXACTLY "
    "match keys in challenges — these encode which open problems each research area still "
    "faces (a challenge may appear under several areas; this is many-to-many). "
    "open_gaps should identify topics that are underrepresented or conceptually unresolved "
    "across the surveys. For each gap, 'related_challenges' must list the challenge names "
    "(EXACT keys in challenges) the gap stems from — many-to-many. A speculative, blue-sky "
    "gap that no current challenge motivates should have related_challenges = [] (it stays "
    "free-floating). "
    "datasets_and_benchmarks must contain ONLY benchmarks explicitly named in the input "
    "papers — never add benchmarks from outside knowledge, and do not invent one for a "
    "research area that the papers do not benchmark. "
    "Return ONLY valid JSON matching the schema. "
    "Keep every string field concise (1-3 sentences max). "
    "Limit: core_problems ≤ 6 items, method_families ≤ 8 keys, challenges ≤ 6 keys, "
    "open_gaps ≤ 5 items, suggested_sections ≤ 8 items. "
    "Be concrete and specific. Do not invent information not in the inputs."
)


class MegaArchitectSynthesizer:
    """
    Synthesises a FieldMegaArchitecture from per-paper architectures.

    Usage:
        synth = MegaArchitectSynthesizer(cfg)
        mega = synth.synthesize(topic, arch_triples, comparison)
    """

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set — cannot run mega-architecture synthesis."
            )
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    def synthesize(
        self,
        topic: str,
        arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
        comparison: CrossSurveyComparison | None = None,
    ) -> FieldMegaArchitecture:
        """
        Build the unified field architecture for one topic.

        arch_triples: output of ArchitectureAnalyzer.analyze(), filtered to this topic.
        comparison:   output of ArchitectureAnalyzer.compare_by_topic() for this topic.
        """
        valid = [(sp, s, a) for sp, s, a in arch_triples if not a.analysis_failed]
        if not valid:
            return FieldMegaArchitecture(
                topic=topic,
                synthesis_failed=True,
                failure_reason="No successfully-analysed papers to synthesise",
            )

        logger.info(
            "Synthesising mega-architecture for '%s' from %d papers", topic, len(valid)
        )

        # Token ladder: each JSONDecodeError (truncation) escalates to the next tier.
        _TOKEN_LADDER = [20_000, 32_000, 64_000]

        prompt = _build_synth_prompt(topic, valid, comparison)
        for attempt, max_tok in enumerate(_TOKEN_LADDER):
            # On retries, prepend a conciseness nudge so the model is less
            # verbose (reducing the chance we still hit the limit).
            effective_prompt = prompt
            if attempt == 1:
                effective_prompt = (
                    "IMPORTANT: Your previous response was too long and got cut off. "
                    "Be concise — keep descriptions to 1-2 sentences, "
                    "limit every array to 4 items maximum.\n\n" + prompt
                )
            elif attempt == 2:
                effective_prompt = (
                    "IMPORTANT: Your previous two responses were too long. "
                    "Be extremely brief — one short sentence per field, "
                    "max 3 items per array.\n\n" + prompt
                )
            try:
                logger.debug(
                    "Synthesis attempt %d/%d for '%s' (max_tokens=%d)",
                    attempt + 1, len(_TOKEN_LADDER), topic, max_tok,
                )
                resp = self._client.messages.create(
                    model=_MODEL,
                    max_tokens=max_tok,
                    system=_SYNTH_SYSTEM,
                    messages=[{"role": "user", "content": effective_prompt}],
                )
                raw = _strip_fences(resp.content[0].text)
                data = json.loads(raw)
                mega = _build_mega_architecture(topic, valid, comparison, data)
                # Build the Field Map PROGRAMMATICALLY from the structured
                # mega-architecture fields (major_tasks, method_families,
                # benchmarks, challenges, gaps, applications) — NOT via a free
                # LLM call — so every node in the diagram is exactly the data
                # shown in the report's tables (no rewording / invented nodes).
                mega.mermaid_diagram = _render_data_mindmap(mega)
                if attempt > 0:
                    logger.info(
                        "Synthesis succeeded for '%s' on attempt %d (max_tokens=%d)",
                        topic, attempt + 1, max_tok,
                    )
                return mega
            except json.JSONDecodeError as e:
                if attempt < len(_TOKEN_LADDER) - 1:
                    logger.warning(
                        "JSON truncation in synthesis for '%s' (max_tokens=%d), "
                        "retrying with max_tokens=%d: %s",
                        topic, max_tok, _TOKEN_LADDER[attempt + 1], e,
                    )
                    continue
                return FieldMegaArchitecture(
                    topic=topic,
                    synthesis_failed=True,
                    failure_reason=f"JSON decode error after {len(_TOKEN_LADDER)} attempts: {e}",
                )
            except Exception as e:
                logger.error("Synthesis LLM call failed for '%s': %s", topic, e)
                return FieldMegaArchitecture(
                    topic=topic,
                    synthesis_failed=True,
                    failure_reason=str(e),
                )
        return FieldMegaArchitecture(
            topic=topic, synthesis_failed=True, failure_reason="Unknown error"
        )


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_synth_prompt(
    topic: str,
    triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    comparison: CrossSurveyComparison | None,
) -> str:
    arch_list = []
    for sp, _, arch in triples:
        arch_list.append({
            "title": sp.paper.title,
            "year": sp.paper.year,
            "orientation": arch.orientation,
            "top_level_taxonomy": arch.top_level_taxonomy,
            "covered_tasks": arch.covered_tasks[:10],
            "covered_methods": arch.covered_methods[:10],
            "covered_datasets": arch.covered_datasets[:8],
            "covered_applications": arch.covered_applications[:6],
            "covered_challenges": arch.covered_challenges[:6],
            "covered_future_directions": arch.covered_future_directions[:6],
            "notable_omissions": arch.notable_omissions[:4],
        })

    lines = [
        f"Topic: {topic}",
        f"Number of surveys: {len(arch_list)}",
        "",
        "Per-paper architectures:",
        json.dumps(arch_list, indent=2, ensure_ascii=False),
    ]

    if comparison and not comparison.comparison_failed:
        lines += [
            "",
            "Cross-survey comparison (already computed):",
            f"  Shared taxonomy dimensions: {', '.join(comparison.shared_taxonomy_dimensions[:6])}",
            f"  Coverage gaps: {', '.join(comparison.coverage_gaps_across_all_surveys[:5])}",
        ]

    lines += [
        "",
        f"Synthesise a unified field architecture and return JSON matching:\n{_SYNTH_SCHEMA}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON → model
# ---------------------------------------------------------------------------

def _build_mega_architecture(
    topic: str,
    triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    comparison: CrossSurveyComparison | None,
    data: dict,
) -> FieldMegaArchitecture:
    gaps = []
    for g in data.get("open_gaps") or []:
        if isinstance(g, dict):
            gaps.append(ResearchGap(
                gap=str(g.get("gap", "")),
                gap_type=str(g.get("gap_type", "")),
                evidence=[str(e) for e in (g.get("evidence") or [])],
                opportunity_score=float(g.get("opportunity_score", 0.0)),
                related_challenges=[str(c) for c in (g.get("related_challenges") or [])],
            ))
    gaps.sort(key=lambda g: g.opportunity_score, reverse=True)

    sections = [
        d for d in (data.get("suggested_sections") or []) if isinstance(d, dict)
    ]

    return FieldMegaArchitecture(
        topic=topic,
        source_papers=[sp.paper.title for sp, _, _ in triples],
        field_summary=str(data.get("field_summary", "")),
        core_problems=[
            d for d in (data.get("core_problems") or []) if isinstance(d, dict)
        ],
        major_tasks={
            k: v for k, v in (data.get("major_tasks") or {}).items()
            if isinstance(v, dict)
        },
        method_families={
            k: v for k, v in (data.get("method_families") or {}).items()
            if isinstance(v, dict)
        },
        datasets_and_benchmarks=[
            d for d in (data.get("datasets_and_benchmarks") or []) if isinstance(d, dict)
        ],
        evaluation_metrics=[str(x) for x in (data.get("evaluation_metrics") or [])],
        applications=[str(x) for x in (data.get("applications") or [])],
        challenges={
            k: v for k, v in (data.get("challenges") or {}).items()
            if isinstance(v, dict)
        },
        future_research_directions=[
            str(x) for x in (data.get("future_research_directions") or [])
        ],
        open_gaps=gaps,
        suggested_title_template=str(data.get("suggested_title_template", "")),
        suggested_abstract_template=str(data.get("suggested_abstract_template", "")),
        suggested_sections=sections,
        cross_survey_comparison=comparison,
    )


# ---------------------------------------------------------------------------
# Mermaid mindmap generator (LLM-generated, programmatic fallback)
# ---------------------------------------------------------------------------

def _generate_mindmap(
    topic: str,
    mega: FieldMegaArchitecture,
    client: anthropic.Anthropic,
) -> str:
    """
    Ask the LLM to produce a Mermaid `mindmap` diagram for the field.

    The prompt includes a compact summary of the mega-architecture so the LLM
    can pick the most meaningful branches.  Falls back to the programmatic
    `_render_mermaid()` if the LLM call fails or returns an unparseable
    diagram.
    """
    field_summary = {
        "topic": topic,
        "major_tasks": list(mega.major_tasks.keys())[:6],
        "method_families": {
            k: v.get("representative_methods", [])[:4]
            for k, v in list(mega.method_families.items())[:6]
            if isinstance(v, dict)
        },
        "benchmarks": [d.get("name", "") for d in mega.datasets_and_benchmarks[:5]],
        "challenges": list(mega.challenges.keys())[:5],
        "research_gaps": [g.gap[:60] for g in mega.open_gaps[:4]],
        "applications": mega.applications[:4],
    }

    prompt = (
        f"Generate a Mermaid mindmap (思维导图) for this research field.\n\n"
        f"Field data:\n{json.dumps(field_summary, indent=2, ensure_ascii=False)}\n\n"
        f"Output only the raw Mermaid mindmap code — no fences, no explanation."
    )

    try:
        logger.info("[mindmap] Generating LLM mindmap for '%s'", topic)
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=1200,
            system=_MINDMAP_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()

        # Strip accidental fences (e.g. ```mermaid ... ```)
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("mermaid"):
                raw = raw[len("mermaid"):]
            raw = raw.strip()

        # Basic sanity check: first non-empty line must be 'mindmap'
        first = next((l.strip() for l in raw.splitlines() if l.strip()), "")
        if first != "mindmap":
            logger.warning(
                "[mindmap] LLM output doesn't start with 'mindmap' for '%s' "
                "(got %r) — falling back to programmatic diagram", topic, first
            )
            return _render_mermaid(mega)

        logger.info("[mindmap] LLM mindmap generated for '%s' (%d lines)", topic, raw.count("\n") + 1)
        return raw

    except Exception as exc:
        logger.warning(
            "[mindmap] LLM call failed for '%s': %s — falling back to programmatic diagram",
            topic, exc,
        )
        return _render_mermaid(mega)


def _render_data_mindmap(mega: FieldMegaArchitecture) -> str:
    """
    Build the Field Map `mindmap` directly from the structured mega-architecture
    fields, so every node is exactly the data shown in the report's tables.

    Branches (all sourced from the same data the report renders elsewhere):
      Research Areas      ← mega.major_tasks
      Methods             ← mega.method_families
      Benchmarks & Datasets ← mega.datasets_and_benchmarks
      Challenges        ← mega.challenges
      Research Gaps     ← mega.open_gaps
      Applications      ← mega.applications
    """
    def clean(text: object, max_len: int = 46) -> str:
        # mindmap node text breaks on (){}[]"|#;<> — strip them and trim length
        t = " ".join(str(text).split())
        t = re.sub(r'[()\[\]{}"|#;<>]', "", t)
        t = t.strip(" -—:")
        return (t[: max_len - 1] + "…") if len(t) > max_len else (t or "—")

    n = len(mega.source_papers)
    low = max(1, round(n * 0.3))

    lines = ["mindmap", f"  root(({clean(mega.topic, 32)}))"]

    def branch(title: str, items: list, counts: dict | None = None) -> None:
        items = [it for it in items if it]
        if not items:
            return
        lines.append(f"    {title}")
        for it in items[:6]:
            warn = ""
            if counts is not None:
                info = counts.get(it)
                cnt = info.get("coverage_count", 0) if isinstance(info, dict) else 0
                warn = " ⚠️" if isinstance(cnt, int) and cnt < low else ""
            lines.append(f"      {clean(it)}{warn}")

    branch("Research Areas", list(mega.major_tasks.keys()), mega.major_tasks)
    branch("Methods", list(mega.method_families.keys()), mega.method_families)
    branch("Benchmarks & Datasets", [d.get("name", "") for d in mega.datasets_and_benchmarks])
    branch("Challenges", list(mega.challenges.keys()), mega.challenges)
    branch("Research Gaps", [g.gap for g in mega.open_gaps])
    branch("Applications", list(mega.applications))

    return "\n".join(lines)


def _render_mermaid(mega: FieldMegaArchitecture) -> str:
    """
    Fallback: generate a simple Mermaid `graph TD` diagram programmatically.
    Used when the LLM mindmap call fails or returns invalid syntax.
    Node IDs are sanitised to be valid Mermaid identifiers.
    """
    n_papers = len(mega.source_papers)
    low_threshold = max(1, round(n_papers * 0.3))

    lines = ["graph TD"]

    topic_id = "Field"
    topic_label = _truncate(mega.topic, 35)
    lines.append(f'    {topic_id}["{topic_label}"]')

    # Top-level section nodes
    sections = [
        ("Tasks", "Research Areas", mega.major_tasks),
        ("Methods", "Methods", mega.method_families),
        ("Challenges", "Challenges", mega.challenges),
    ]

    for node_id, label, items in sections:
        lines.append(f'    {topic_id} --> {node_id}["{label}"]')
        for name, info in list(items.items())[:6]:
            safe = _node_id(name)
            count = info.get("coverage_count", 0) if isinstance(info, dict) else 0
            suffix = " ⚠️" if count < low_threshold else ""
            lines.append(f'    {node_id} --> {safe}["{_truncate(name, 28)}{suffix}"]')

    # Flat section nodes
    flat_sections = []
    if mega.core_problems:
        flat_sections.append(("CoreProblems", "Core Problems"))
    if mega.datasets_and_benchmarks:
        flat_sections.append(("Datasets", "Datasets & Benchmarks"))
    if mega.applications:
        flat_sections.append(("Apps", "Applications"))
    if mega.future_research_directions:
        flat_sections.append(("Future", "Future Directions"))

    for node_id, label in flat_sections:
        lines.append(f'    {topic_id} --> {node_id}["{label}"]')

    return "\n".join(lines)


def _node_id(name: str) -> str:
    """Convert an arbitrary string to a valid Mermaid node ID."""
    slug = re.sub(r"[^a-zA-Z0-9]", "_", name)[:30]
    if slug and slug[0].isdigit():
        slug = "N" + slug
    return slug or "Node"


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
