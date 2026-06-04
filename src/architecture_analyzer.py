"""
Survey Architecture Analyzer.

Two-pass LLM analysis:

  Pass 1 — Per-paper architecture extraction
    For each top-N paper, reverse-engineers how the survey organises its field:
    orientation type, organisational logic, taxonomy tree, what tasks/methods/
    datasets/challenges/future-directions it covers, and structural strengths /
    weaknesses / omissions.

  Pass 2 — Cross-survey comparison (per topic)
    Aggregates all per-paper architectures for one topic and asks the LLM to
    identify shared taxonomy dimensions, conflicting classifications, and which
    paper best covers each aspect.

Both passes reuse the existing PaperSummary fields as context so we do not
repeat the basic content extraction that the summarizer already did.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

import anthropic

from .config import AppConfig
from .llm_cache import LLMCache
from .models import (
    CrossSurveyComparison,
    Paper,
    PaperArchitecture,
    PaperSummary,
    ScoredPaper,
)

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_CACHE_DIR_ARCH = "data/cache/llm/architecture"
_CACHE_DIR_CMP  = "data/cache/llm/comparison"

# ---------------------------------------------------------------------------
# Pass 1 — per-paper schema
# ---------------------------------------------------------------------------

_ARCH_SCHEMA = """
{
  "orientation": "one of: task | method | application | timeline | challenge | hybrid",
  "core_research_questions": ["2-4 specific questions this survey tries to answer"],
  "organizational_logic": "one paragraph: how does this survey structure its content?",
  "top_level_taxonomy": ["3-7 top-level categories the survey uses"],
  "second_level_taxonomy": {
    "<top-level category>": ["sub-category 1", "sub-category 2"]
  },
  "covered_tasks": ["NLP/ML/CV tasks explicitly covered"],
  "covered_methods": ["algorithms or model families explicitly covered"],
  "covered_datasets": ["benchmark or dataset names explicitly mentioned"],
  "covered_applications": ["application domains covered"],
  "covered_challenges": ["open problems or limitations discussed"],
  "covered_future_directions": ["future directions explicitly stated"],
  "notable_omissions": ["important topics NOT covered that a reader might expect"],
  "structural_strengths": ["1-3 things this survey does better than most"],
  "structural_weaknesses": ["1-3 structural or coverage weaknesses"]
}
""".strip()

_ARCH_SYSTEM = (
    "You are an expert in academic literature analysis. "
    "Your task is to reverse-engineer the organisational structure of a survey paper — "
    "not to summarise what it says, but to describe HOW it organises its field. "
    "Return ONLY valid JSON matching the schema given. "
    "Be concrete and specific. Do not invent information not present in the input. "
    "Use empty lists [] for fields where information is unavailable."
)

# ---------------------------------------------------------------------------
# Pass 2 — cross-survey comparison schema
# ---------------------------------------------------------------------------

_COMPARISON_SCHEMA = """
{
  "orientation_distribution": {
    "task": 0, "method": 0, "application": 0,
    "timeline": 0, "challenge": 0, "hybrid": 0
  },
  "shared_taxonomy_dimensions": [
    "dimension that appears in most surveys"
  ],
  "conflicting_classifications": [
    {
      "dimension": "the concept being classified",
      "paper_a": "short title",
      "paper_a_view": "how paper A classifies it",
      "paper_b": "short title",
      "paper_b_view": "how paper B classifies it"
    }
  ],
  "complementary_coverage": [
    {
      "aspect": "what aspect",
      "best_covered_by": "short paper title"
    }
  ],
  "best_overall_structure": "short title of the paper with best overall structure",
  "best_overall_structure_reason": "one sentence explaining why",
  "coverage_gaps_across_all_surveys": [
    "topic that NO survey covers well"
  ]
}
""".strip()

_COMPARISON_SYSTEM = (
    "You are an expert meta-reviewer of academic survey papers. "
    "Given multiple per-paper architecture summaries for the same topic, "
    "produce a cross-survey comparison. "
    "Return ONLY valid JSON matching the schema given. "
    "Be concrete. Reference papers by short title only."
)


class ArchitectureAnalyzer:
    """
    Runs two LLM passes to extract and compare survey architectures.

    Usage:
        analyzer = ArchitectureAnalyzer(cfg)
        results = analyzer.analyze(summary_pairs)
        # results: list[(ScoredPaper, PaperSummary, PaperArchitecture)]

        comparisons = analyzer.compare_by_topic(results)
        # comparisons: dict[topic_str, CrossSurveyComparison]
    """

    def __init__(self, cfg: AppConfig):
        if not cfg.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set — cannot run architecture analysis."
            )
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._top_n = cfg.analyze_top_n
        self._arch_cache = LLMCache(_CACHE_DIR_ARCH)
        self._cmp_cache  = LLMCache(_CACHE_DIR_CMP)

    # ------------------------------------------------------------------
    # Pass 1
    # ------------------------------------------------------------------

    def analyze(
        self,
        summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
        parsed_map: dict | None = None,
    ) -> list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]]:
        """
        Extract per-paper architecture for the top-N pairs.

        parsed_map (optional): dict[paper_title, ParsedPaper] from pdf_parser.
          When provided, conclusion_text and future_work_text are injected into
          the architecture prompt, improving extraction quality.
        """
        candidates = summary_pairs[: self._top_n]
        parsed_map = parsed_map or {}
        results = []
        for i, (sp, summary) in enumerate(candidates, 1):
            if summary.summarization_failed:
                arch = PaperArchitecture(
                    paper_title=sp.paper.title,
                    analysis_failed=True,
                    failure_reason="Skipped — summarization failed",
                )
            else:
                logger.info(
                    "Architecture analysis %d/%d: %s",
                    i, len(candidates), sp.paper.title[:70],
                )
                parsed = parsed_map.get(sp.paper.title)
                arch = self._analyze_one(sp.paper, summary, parsed)
            results.append((sp, summary, arch))

        hits = self._arch_cache.hits
        if hits:
            logger.info(
                "Architecture cache: %d/%d hit (saved ~%d LLM call%s)",
                hits, len(candidates), hits, "s" if hits != 1 else "",
            )
        return results

    def _analyze_one(self, paper: Paper, summary: PaperSummary, parsed=None) -> PaperArchitecture:
        # Cache key: title + abstract + taxonomy (taxonomy changes if summary is re-run)
        taxonomy_sig = "|".join(sorted(summary.taxonomy[:8]))
        cache_key = LLMCache.make_key(paper.title, paper.abstract or "", taxonomy_sig, _MODEL)
        cached = self._arch_cache.get(cache_key)
        if cached is not None:
            logger.info("  ↩ cache hit — skipping arch analysis for '%s'", paper.title[:70])
            return _build_architecture(paper.title, cached)

        prompt = _build_arch_prompt(paper, summary, parsed)
        for attempt in range(2):
            try:
                with self._client.messages.stream(
                    model=_MODEL,
                    max_tokens=30000,
                    system=_ARCH_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    raw = _strip_fences(stream.get_final_text())

                # Try direct parse; fall back to truncation repair on first attempt
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as parse_err:
                    if attempt == 0:
                        repaired = _repair_truncated_json(raw)
                        if repaired is not None:
                            logger.warning(
                                "Truncated JSON repaired for '%s' (dropped incomplete fields)",
                                paper.title[:50],
                            )
                            data = repaired
                        else:
                            raise  # triggers the outer retry
                    else:
                        raise

                self._arch_cache.set(cache_key, data, label=paper.title[:70], model=_MODEL)
                return _build_architecture(paper.title, data)

            except json.JSONDecodeError as e:
                if attempt == 0:
                    logger.warning("JSON error for '%s', retrying: %s", paper.title[:50], e)
                    continue
                logger.error("Architecture parse failed for '%s': %s", paper.title[:50], e)
                return PaperArchitecture(
                    paper_title=paper.title,
                    analysis_failed=True,
                    failure_reason=f"JSON decode error: {e}",
                )
            except Exception as e:
                logger.error("Architecture LLM call failed for '%s': %s", paper.title[:50], e)
                return PaperArchitecture(
                    paper_title=paper.title,
                    analysis_failed=True,
                    failure_reason=str(e),
                )
        return PaperArchitecture(
            paper_title=paper.title,
            analysis_failed=True,
            failure_reason="Unknown error",
        )

    # ------------------------------------------------------------------
    # Pass 2
    # ------------------------------------------------------------------

    def compare_by_topic(
        self,
        arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    ) -> dict[str, CrossSurveyComparison]:
        """
        Group triples by topic and run a cross-survey comparison per topic.
        Returns a dict mapping topic string → CrossSurveyComparison.
        Topics with fewer than 2 successful papers are skipped.
        """
        by_topic: dict[str, list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]]] = (
            defaultdict(list)
        )
        for triple in arch_triples:
            sp, _, arch = triple
            topic = sp.paper.topic_queries[0] if sp.paper.topic_queries else "Uncategorised"
            by_topic[topic].append(triple)

        comparisons: dict[str, CrossSurveyComparison] = {}
        for topic, triples in by_topic.items():
            valid = [(sp, s, a) for sp, s, a in triples if not a.analysis_failed]
            if len(valid) < 2:
                logger.info(
                    "Skipping cross-survey comparison for '%s' (%d valid papers)", topic, len(valid)
                )
                continue
            logger.info(
                "Cross-survey comparison for '%s' (%d papers)", topic, len(valid)
            )
            comparisons[topic] = self._compare_topic(topic, valid)

        return comparisons

    def _compare_topic(
        self,
        topic: str,
        triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    ) -> CrossSurveyComparison:
        # Cache key: topic + sorted paper titles (stable regardless of order)
        titles_sig = "|".join(sorted(sp.paper.title for sp, _, _ in triples))
        cache_key = LLMCache.make_key(topic, titles_sig, _MODEL)
        cached = self._cmp_cache.get(cache_key)
        if cached is not None:
            logger.info("  ↩ cache hit — skipping comparison for '%s'", topic[:70])
            return _build_comparison(topic, cached)

        prompt = _build_comparison_prompt(topic, triples)
        for attempt in range(2):
            try:
                with self._client.messages.stream(
                    model=_MODEL,
                    max_tokens=30000,
                    system=_COMPARISON_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                ) as stream:
                    raw = _strip_fences(stream.get_final_text())
                data = json.loads(raw)
                self._cmp_cache.set(cache_key, data, label=topic[:70], model=_MODEL)
                return _build_comparison(topic, data)
            except json.JSONDecodeError as e:
                if attempt == 0:
                    logger.warning("JSON error in comparison for '%s', retrying: %s", topic, e)
                    continue
                return CrossSurveyComparison(
                    topic=topic,
                    comparison_failed=True,
                    failure_reason=f"JSON decode error: {e}",
                )
            except Exception as e:
                logger.error("Comparison LLM call failed for '%s': %s", topic, e)
                return CrossSurveyComparison(
                    topic=topic,
                    comparison_failed=True,
                    failure_reason=str(e),
                )
        return CrossSurveyComparison(
            topic=topic, comparison_failed=True, failure_reason="Unknown error"
        )


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_arch_prompt(paper: Paper, summary: PaperSummary, parsed=None) -> str:
    lines = [
        f"Title: {paper.title}",
        f"Year: {paper.year or 'Unknown'}",
        f"Venue: {paper.venue or 'Unknown'}",
    ]
    if paper.abstract:
        lines.append(f"\nAbstract:\n{paper.abstract[:4000]}")

    # Feed in the existing summary so the model doesn't start from scratch
    if summary.taxonomy:
        lines.append(f"\nExisting taxonomy labels: {', '.join(summary.taxonomy[:10])}")
    if summary.main_methods:
        lines.append(f"Existing methods list: {', '.join(summary.main_methods[:10])}")
    if summary.datasets_and_benchmarks:
        lines.append(f"Existing datasets list: {', '.join(summary.datasets_and_benchmarks[:10])}")
    if summary.limitations:
        lines.append(f"Existing limitations: {', '.join(summary.limitations[:5])}")
    if summary.future_directions:
        lines.append(f"Existing future directions: {', '.join(summary.future_directions[:5])}")

    # Inject PDF-parsed full-text sections when available
    if parsed and not parsed.parse_failed:
        if parsed.sections:
            lines.append(f"\nSection headings: {'; '.join(parsed.sections[:20])}")
        if parsed.conclusion_text:
            lines.append(f"\nConclusion (extracted from PDF):\n{parsed.conclusion_text[:1000]}")
        if parsed.future_work_text:
            lines.append(f"\nFuture work (extracted from PDF):\n{parsed.future_work_text[:800]}")

    lines.append(
        f"\nAnalyse how this survey ORGANISES its field and return JSON matching:\n{_ARCH_SCHEMA}"
    )
    return "\n".join(lines)


def _build_comparison_prompt(
    topic: str,
    triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
) -> str:
    summaries = []
    for sp, _, arch in triples:
        entry = {
            "title": sp.paper.title,
            "year": sp.paper.year,
            "orientation": arch.orientation,
            "top_level_taxonomy": arch.top_level_taxonomy,
            "covered_tasks": arch.covered_tasks[:8],
            "covered_methods": arch.covered_methods[:8],
            "covered_challenges": arch.covered_challenges[:5],
            "structural_strengths": arch.structural_strengths,
            "notable_omissions": arch.notable_omissions[:5],
        }
        summaries.append(entry)

    prompt = (
        f"Topic: {topic}\n"
        f"Number of surveys: {len(summaries)}\n\n"
        f"Per-paper architecture summaries:\n"
        f"{json.dumps(summaries, indent=2, ensure_ascii=False)}\n\n"
        f"Return a cross-survey comparison JSON matching:\n{_COMPARISON_SCHEMA}"
    )
    return prompt


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _build_architecture(title: str, data: dict) -> PaperArchitecture:
    return PaperArchitecture(
        paper_title=title,
        orientation=str(data.get("orientation", "")),
        core_research_questions=_as_list(data.get("core_research_questions")),
        organizational_logic=str(data.get("organizational_logic", "")),
        top_level_taxonomy=_as_list(data.get("top_level_taxonomy")),
        second_level_taxonomy=_as_str_dict(data.get("second_level_taxonomy")),
        covered_tasks=_as_list(data.get("covered_tasks")),
        covered_methods=_as_list(data.get("covered_methods")),
        covered_datasets=_as_list(data.get("covered_datasets")),
        covered_applications=_as_list(data.get("covered_applications")),
        covered_challenges=_as_list(data.get("covered_challenges")),
        covered_future_directions=_as_list(data.get("covered_future_directions")),
        notable_omissions=_as_list(data.get("notable_omissions")),
        structural_strengths=_as_list(data.get("structural_strengths")),
        structural_weaknesses=_as_list(data.get("structural_weaknesses")),
    )


def _build_comparison(topic: str, data: dict) -> CrossSurveyComparison:
    return CrossSurveyComparison(
        topic=topic,
        orientation_distribution={
            k: int(v) for k, v in (data.get("orientation_distribution") or {}).items()
        },
        shared_taxonomy_dimensions=_as_list(data.get("shared_taxonomy_dimensions")),
        conflicting_classifications=[
            d for d in (data.get("conflicting_classifications") or []) if isinstance(d, dict)
        ],
        complementary_coverage=[
            d for d in (data.get("complementary_coverage") or []) if isinstance(d, dict)
        ],
        best_overall_structure=str(data.get("best_overall_structure", "")),
        best_overall_structure_reason=str(data.get("best_overall_structure_reason", "")),
        coverage_gaps_across_all_surveys=_as_list(
            data.get("coverage_gaps_across_all_surveys")
        ),
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _repair_truncated_json(text: str) -> dict | None:
    """
    Recover a JSON object that was cut off mid-string by the LLM token limit.

    Strategy:
      1. Walk the raw text tracking bracket/brace nesting and string state.
      2. Record the position AND stack snapshot of the last comma at depth 1
         (directly inside the root ``{}``).  Everything before that comma is a
         complete, parseable set of key-value pairs.
      3. Truncate there and close only the structures that were open *at that
         snapshot* — NOT the final stack, which may have grown deeper since.

    Example — given (truncated at ``…``):
        {"a": [1, 2], "b": ["ok"], "c": "truncated str
    Repairs to:
        {"a": [1, 2], "b": ["ok"]}

    Returns the repaired dict, or None if repair is not possible.
    """
    stack: list[str] = []          # '{' or '[' for each open structure
    in_string = False
    escaped = False
    last_comma_depth1 = -1                  # text index of last top-level ','
    stack_at_last_comma: list[str] = []     # stack snapshot at that comma

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
        # Structural characters (outside strings)
        if ch in ("{", "["):
            stack.append(ch)
        elif ch in ("}", "]"):
            if stack:
                stack.pop()
        elif ch == "," and len(stack) == 1:
            last_comma_depth1 = i
            stack_at_last_comma = stack.copy()   # snapshot: always ['{'] at depth 1

    if last_comma_depth1 == -1:
        return None

    # Trim to the last complete top-level field
    trimmed = text[: last_comma_depth1].rstrip()

    # Close only the structures that were open at the snapshot point
    closers = {"{": "}", "[": "]"}
    suffix = "".join(closers[s] for s in reversed(stack_at_last_comma))

    try:
        return json.loads(trimmed + suffix)
    except Exception:
        return None


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _as_list(val) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val]
    return [str(val)]


def _as_str_dict(val) -> dict[str, list[str]]:
    if not isinstance(val, dict):
        return {}
    return {
        str(k): [str(x) for x in v] if isinstance(v, list) else [str(v)]
        for k, v in val.items()
    }
