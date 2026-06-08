"""
System-design synthesiser.

Given the FieldMegaArchitecture for one topic (plus the per-survey
architectures), uses an LLM to draw the field as a TOP-DOWN system: an ordered
stack of subsystems/layers, the key components inside each, the cross-cutting
concerns (memory, evaluation, …) and how data flows through it. The goal is a
"system design" a newcomer can read top-to-bottom to understand the whole field
without reading every survey.

Usage:
    synth = SystemDesignSynthesizer(cfg)
    design = synth.synthesize(topic, mega, arch_triples)
"""

from __future__ import annotations

import json
import logging

import anthropic

from .architecture_analyzer import _repair_truncated_json
from .config import AppConfig
from .models import (
    FieldMegaArchitecture,
    PaperArchitecture,
    PaperSummary,
    ScoredPaper,
    SystemComponent,
    SystemDesign,
    SystemLayer,
)

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_SCHEMA = """
{
  "overview": "1-2 sentences: describe the field as a single end-to-end system",
  "layers": [
    {
      "name": "<subsystem / layer name>",
      "role": "one sentence: what this layer is responsible for",
      "components": [
        {"name": "<component>", "description": "1-2 sentence plain-language explanation of what it is / does"}
      ]
    }
  ],
  "cross_cutting": [
    {
      "name": "<cross-cutting concern, e.g. Memory or Evaluation>",
      "role": "one sentence",
      "components": [
        {"name": "<component>", "description": "1-2 sentences"}
      ]
    }
  ],
  "data_flow": "1-2 sentences: how information flows through the layers, top to bottom"
}
""".strip()

_SYSTEM = (
    "You are a systems architect explaining a research field to a newcomer. "
    "Given a field's mega-architecture (tasks, methods, datasets, challenges) and "
    "how its surveys organise it, design the field as ONE top-down system. "
    "Produce 4-6 ordered layers from the entry point down to the foundations "
    "(for a pipeline-like field, order them input → output). Each layer has 2-5 "
    "concrete components drawn from the actual methods/tasks in the data. Put "
    "concerns that span all layers (e.g. memory, evaluation, safety) in "
    "cross_cutting (0-3 of them). Every component needs a short, clear "
    "description a newcomer understands. Ground everything in the provided data; "
    "do not invent components the field does not use. "
    "Return ONLY valid JSON matching the schema. Keep strings concise."
)


class SystemDesignSynthesizer:
    """Synthesises a top-down SystemDesign for one research field."""

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for system-design synthesis.")
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    def synthesize(
        self,
        topic: str,
        mega: FieldMegaArchitecture,
        arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    ) -> SystemDesign:
        if mega.synthesis_failed:
            return SystemDesign(
                topic=topic, extraction_failed=True,
                failure_reason="Mega-architecture synthesis failed; no data to design from.",
            )

        prompt = _build_prompt(topic, mega, arch_triples)
        try:
            logger.info("[system_design] Synthesising system design for '%s'", topic)
            with self._client.messages.stream(
                model=_MODEL,
                max_tokens=20000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                raw = _strip_fences(stream.get_final_text())
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                repaired = _repair_truncated_json(raw)
                if repaired is None:
                    raise
                logger.warning("[system_design] Truncated JSON repaired for '%s'", topic)
                data = repaired
            return _build_design(topic, data)
        except Exception as exc:  # noqa: BLE001
            logger.error("[system_design] Failed for '%s': %s", topic, exc)
            return SystemDesign(topic=topic, extraction_failed=True, failure_reason=str(exc))


def _build_prompt(
    topic: str,
    mega: FieldMegaArchitecture,
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
) -> str:
    # Method families with their representative techniques give the components.
    method_families = {}
    for name, info in list(mega.method_families.items())[:8]:
        reps = info.get("representative_methods", []) if isinstance(info, dict) else []
        method_families[name] = [str(r) for r in reps][:5]

    # A few surveys' top-level taxonomies hint at how the field is structured.
    survey_structures = []
    for sp, _summary, arch in arch_triples[:6]:
        if arch.analysis_failed or not arch.top_level_taxonomy:
            continue
        survey_structures.append({
            "survey": sp.paper.title[:80],
            "organises_by": arch.top_level_taxonomy[:7],
        })

    field_data = {
        "topic": topic,
        "field_summary": mega.field_summary,
        "core_problems": [cp.get("problem", "") for cp in mega.core_problems[:6]],
        "research_areas": list(mega.major_tasks.keys())[:8],
        "methods": method_families,
        "benchmarks": [d.get("name", "") for d in mega.datasets_and_benchmarks[:8] if d.get("name")],
        "challenges": list(mega.challenges.keys())[:6],
        "applications": mega.applications[:6],
        "how_surveys_organise_the_field": survey_structures,
    }
    return (
        f"Field: {topic}\n\n"
        f"Field data:\n{json.dumps(field_data, indent=2, ensure_ascii=False)}\n\n"
        f"Design this field as a top-down system. Return JSON matching:\n{_SCHEMA}"
    )


def _build_layer(d: dict) -> SystemLayer:
    comps = []
    for c in d.get("components") or []:
        if isinstance(c, dict) and str(c.get("name", "")).strip():
            comps.append(SystemComponent(
                name=str(c["name"]).strip(),
                description=str(c.get("description", "")).strip(),
            ))
        elif isinstance(c, str) and c.strip():
            comps.append(SystemComponent(name=c.strip()))
    return SystemLayer(
        name=str(d.get("name", "")).strip(),
        role=str(d.get("role", "")).strip(),
        components=comps,
    )


def _build_design(topic: str, data: dict) -> SystemDesign:
    layers = [_build_layer(l) for l in (data.get("layers") or []) if isinstance(l, dict) and l.get("name")]
    cross = [_build_layer(l) for l in (data.get("cross_cutting") or []) if isinstance(l, dict) and l.get("name")]
    return SystemDesign(
        topic=topic,
        overview=str(data.get("overview", "")).strip(),
        layers=layers,
        cross_cutting=cross,
        data_flow=str(data.get("data_flow", "")).strip(),
        extraction_failed=not layers,
        failure_reason="" if layers else "No layers produced.",
    )


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()
