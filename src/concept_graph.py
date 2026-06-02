"""
Typed concept graph extractor.

Given the FieldMegaArchitecture for one topic, uses an LLM to extract a typed
concept graph: 8–20 nodes (named concepts) and 10–30 directed edges with one
of seven allowed relation types.

Edge types:
  is_subfield_of  — X is a sub-area of Y
  uses            — X relies on technique/method Y
  evaluated_by    — X is measured using metric/dataset Y
  applied_to      — X is applied in domain Y
  contrasts_with  — X is a competing/alternative approach to Y
  emerged_after   — X builds on or followed Y historically
  part_of         — X is a component of system/framework Y

Usage:
    extractor = ConceptGraphExtractor(cfg)
    graph = extractor.extract(topic, mega, arch_triples)
"""

from __future__ import annotations

import json
import logging

import anthropic

from .architecture_analyzer import _repair_truncated_json
from .config import AppConfig
from .models import (
    ConceptEdge,
    ConceptGraph,
    ConceptNode,
    FieldMegaArchitecture,
    PaperArchitecture,
    PaperSummary,
    ScoredPaper,
)

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_ALLOWED_EDGE_TYPES = {
    "is_subfield_of",
    "uses",
    "evaluated_by",
    "applied_to",
    "contrasts_with",
    "emerged_after",
    "part_of",
}

_SCHEMA = """
{
  "nodes": [
    {
      "node_id": "short_slug_no_spaces",
      "name": "Human-readable concept name",
      "definition": "One-sentence definition",
      "aliases": ["alternative name 1", "alternative name 2"],
      "representative_papers": ["Paper Title A"],
      "evidence_quotes": ["short quote from a survey"],
      "source_surveys": ["Survey Title X"]
    }
  ],
  "edges": [
    {
      "source_id": "node_id_a",
      "target_id": "node_id_b",
      "edge_type": "uses",
      "evidence": "One sentence justifying this relationship."
    }
  ]
}
""".strip()

_SYSTEM = (
    "You are an expert knowledge-graph builder for AI research. "
    "Given a field mega-architecture (concepts, methods, tasks, challenges), "
    "extract a typed concept graph. "
    "Produce 8–20 nodes and 10–30 edges. "
    "Every edge_type MUST be one of: "
    "is_subfield_of, uses, evaluated_by, applied_to, contrasts_with, emerged_after, part_of. "
    "Every source_id and target_id MUST match a node_id in the nodes array. "
    "node_id must be a lowercase alphanumeric slug (underscores allowed, no spaces). "
    "Return ONLY valid JSON matching the schema. Keep all strings concise."
)


class ConceptGraphExtractor:
    """Extracts a typed concept graph for one research field."""

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for concept graph extraction.")
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    def extract(
        self,
        topic: str,
        mega: FieldMegaArchitecture,
        arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    ) -> ConceptGraph:
        """Extract a concept graph from the mega-architecture."""
        if mega.synthesis_failed:
            return ConceptGraph(
                topic=topic,
                extraction_failed=True,
                failure_reason="Mega-architecture synthesis failed; no data to extract from.",
            )

        prompt = _build_prompt(topic, mega)

        try:
            logger.info("[concept_graph] Extracting concept graph for '%s'", topic)
            with self._client.messages.stream(
                model=_MODEL,
                max_tokens=30000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                raw = _strip_fences(stream.get_final_text())
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                repaired = _repair_truncated_json(raw)
                if repaired is not None:
                    logger.warning(
                        "[concept_graph] Truncated JSON repaired for '%s' (dropped incomplete fields)",
                        topic,
                    )
                    data = repaired
                else:
                    raise
            return _build_graph(topic, data)
        except json.JSONDecodeError as exc:
            logger.error("[concept_graph] JSON decode error for '%s': %s", topic, exc)
            return ConceptGraph(
                topic=topic,
                extraction_failed=True,
                failure_reason=f"JSON decode error: {exc}",
            )
        except Exception as exc:
            logger.error("[concept_graph] LLM call failed for '%s': %s", topic, exc)
            return ConceptGraph(
                topic=topic,
                extraction_failed=True,
                failure_reason=str(exc),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(topic: str, mega: FieldMegaArchitecture) -> str:
    # Summarise the mega-architecture into a compact JSON for the LLM
    field_data = {
        "topic": topic,
        "field_summary": mega.field_summary,
        "core_problems": [cp.get("problem", "") for cp in mega.core_problems[:6]],
        "major_tasks": list(mega.major_tasks.keys())[:8],
        "method_families": list(mega.method_families.keys())[:8],
        "applications": mega.applications[:6],
        "challenges": list(mega.challenges.keys())[:6],
        "future_directions": mega.future_research_directions[:5],
    }
    return (
        f"Field: {topic}\n\n"
        f"Mega-architecture summary:\n{json.dumps(field_data, indent=2, ensure_ascii=False)}\n\n"
        f"Extract a typed concept graph. Return JSON matching:\n{_SCHEMA}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON → model
# ─────────────────────────────────────────────────────────────────────────────

def _build_graph(topic: str, data: dict) -> ConceptGraph:
    nodes: list[ConceptNode] = []
    node_ids: set[str] = set()

    for n in (data.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        node_id = str(n.get("node_id", "")).strip()
        if not node_id:
            continue
        nodes.append(ConceptNode(
            node_id=node_id,
            name=str(n.get("name", node_id)),
            definition=str(n.get("definition", "")),
            aliases=[str(a) for a in (n.get("aliases") or [])],
            representative_papers=[str(p) for p in (n.get("representative_papers") or [])],
            evidence_quotes=[str(q) for q in (n.get("evidence_quotes") or [])],
            source_surveys=[str(s) for s in (n.get("source_surveys") or [])],
        ))
        node_ids.add(node_id)

    edges: list[ConceptEdge] = []
    for e in (data.get("edges") or []):
        if not isinstance(e, dict):
            continue
        src = str(e.get("source_id", "")).strip()
        tgt = str(e.get("target_id", "")).strip()
        etype = str(e.get("edge_type", "")).strip()
        # Validate: both endpoints must exist and edge type must be allowed
        if src not in node_ids or tgt not in node_ids:
            logger.debug(
                "[concept_graph] Dropping edge %s→%s: unknown node id(s)", src, tgt
            )
            continue
        if etype not in _ALLOWED_EDGE_TYPES:
            logger.debug("[concept_graph] Dropping edge %s→%s: invalid type '%s'", src, tgt, etype)
            continue
        edges.append(ConceptEdge(
            source_id=src,
            target_id=tgt,
            edge_type=etype,
            evidence=str(e.get("evidence", "")),
        ))

    logger.info(
        "[concept_graph] Extracted %d nodes and %d valid edges for '%s'",
        len(nodes), len(edges), topic,
    )
    return ConceptGraph(topic=topic, nodes=nodes, edges=edges)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
