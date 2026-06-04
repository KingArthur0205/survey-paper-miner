"""
Shared data models for the AI Survey Paper Miner.

All retrievers convert their source-specific responses into `Paper` objects.
`PaperSummary` holds the structured LLM output for a single paper.
`ScoredPaper` wraps a Paper with its computed quality score.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class Paper(BaseModel):
    """Normalised metadata for one academic paper."""

    title: str
    year: Optional[int] = None
    authors: list[str] = Field(default_factory=list)
    venue: Optional[str] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    citation_count: int = 0
    influential_citation_count: int = 0
    # Fraction of citations where citing paper uses this as a background reference.
    # Populated by SemanticScholarRetriever when citation-context fetching is enabled.
    background_citation_count: int = 0

    # Set by CanonicalSurveyDetector (0.0–1.0); used in quality scoring.
    canonical_score: float = 0.0
    # Set by TemporalStratifier: "foundational" | "current_standard" | "emerging" | None
    authority_tier: Optional[str] = None

    # True when injected by the top-cited-survey retriever. These curated,
    # citation-ranked surveys bypass the score-based filters (keyword / min-score)
    # and are guaranteed a summarise+judge slot, so popular surveys aren't cut by
    # raw citation score before the judge can assess their relevance.
    from_top_survey: bool = False

    # Provenance: which retriever(s) returned this paper
    sources: list[str] = Field(default_factory=list)
    # Which user-facing topic generated the query that found this paper
    topic_queries: list[str] = Field(default_factory=list)
    # The exact search query strings sent to the APIs
    generated_queries: list[str] = Field(default_factory=list)

    def normalized_title(self) -> str:
        """Lower-case, whitespace-collapsed title used for deduplication."""
        return " ".join(self.title.lower().split())


class ScoredPaper(BaseModel):
    """A Paper with its computed quality score and per-component breakdown."""

    paper: Paper
    quality_score: float = 0.0

    # Component scores for transparency (max pts: 20+20+10+15+15+20 = 100)
    venue_score: float = 0.0
    citation_score: float = 0.0        # influential-ratio-weighted, 20 pts
    recency_score: float = 0.0
    survey_signal_score: float = 0.0
    structure_signal_score: float = 0.0
    canonical_score_component: float = 0.0  # from CanonicalSurveyDetector, 20 pts

    # Set after LLM-as-Judge; combines quality_score with judge bonuses/penalties.
    # Papers the judge marks as off-topic or non-survey score much lower here.
    # Used for final ranking in all exports.  0.0 = not yet judged.
    judge_adjusted_score: float = 0.0


class PaperSummary(BaseModel):
    """Structured summary produced by the LLM for one paper."""

    paper_title: str

    research_scope: str = ""
    core_problem: str = ""
    taxonomy: list[str] = Field(default_factory=list)
    main_methods: list[str] = Field(default_factory=list)
    representative_papers_or_models: list[str] = Field(default_factory=list)
    datasets_and_benchmarks: list[str] = Field(default_factory=list)
    evaluation_metrics: list[str] = Field(default_factory=list)
    main_findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    future_directions: list[str] = Field(default_factory=list)

    keywords: dict[str, list[str]] = Field(default_factory=dict)
    # e.g. {"tasks": [...], "methods": [...], "models": [...], ...}

    citation_use_cases: list[str] = Field(default_factory=list)

    # Metadata about the summarization itself
    summarization_source: str = "abstract"  # "abstract" or "full_text"
    summarization_failed: bool = False
    failure_reason: str = ""

    def to_flat_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable flat dict suitable for JSONL export."""
        d = self.model_dump()
        return d


class JudgeResult(BaseModel):
    """
    Authority assessment produced by LLMJudge for one paper.

    A separate, lightweight LLM pass that answers whether this paper is
    actually a survey and how authoritative it is — distinct from the
    content summarization in PaperSummary.
    """

    paper_title: str

    is_survey: bool = True
    # "foundational" | "current_standard" | "emerging" | "not_a_survey"
    authority_assessment: str = ""
    # "broad" | "narrow" | "unclear"
    scope_clarity: str = ""
    # "comprehensive" | "partial" | "shallow"
    coverage_depth: str = ""

    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)

    # How specifically this paper addresses the configured research topics.
    # 1 = completely off-topic  2 = tangential  3 = related but not specific
    # 4 = directly relevant     5 = exactly this topic
    topic_relevance: int = 3

    # True when the paper surveys the topic *within one narrow application
    # vertical* (e.g. clinical/medical, finance, agriculture, materials,
    # software-engineering) rather than the general architecture/field.
    # Such papers can be on-topic (high topic_relevance) yet poor as a
    # general reference — this flag lets the pipeline filter them separately.
    is_domain_specific: bool = False

    # Final inclusion tier, mirroring a manual relevance audit:
    #   "core"     = directly and specifically surveys the topic — primary source
    #   "useful"   = strong related survey providing important context
    #   "marginal" = tangential, domain-specific application, or non-survey
    #   "cut"      = off-topic, non-English, or clearly not a survey
    paper_tier: str = "useful"

    # "must_read" | "worth_reading" | "optional" | "skip"
    recommended_action: str = ""
    confidence: float = 0.0

    judge_failed: bool = False
    failure_reason: str = ""

    def to_flat_dict(self) -> dict[str, Any]:
        return self.model_dump()


class PaperArchitecture(BaseModel):
    """
    Reverse-engineered organisational structure of one survey paper.

    Produced by ArchitectureAnalyzer.  Describes *how* the paper organises
    its field rather than *what* it says.
    """

    paper_title: str

    # How the survey is organised — one of the five canonical orientations
    orientation: str = ""  # "task"|"method"|"application"|"timeline"|"challenge"|"hybrid"
    core_research_questions: list[str] = Field(default_factory=list)
    organizational_logic: str = ""  # one-paragraph description of the structure

    top_level_taxonomy: list[str] = Field(default_factory=list)
    second_level_taxonomy: dict[str, list[str]] = Field(default_factory=dict)

    covered_tasks: list[str] = Field(default_factory=list)
    covered_methods: list[str] = Field(default_factory=list)
    covered_datasets: list[str] = Field(default_factory=list)
    covered_applications: list[str] = Field(default_factory=list)
    covered_challenges: list[str] = Field(default_factory=list)
    covered_future_directions: list[str] = Field(default_factory=list)

    notable_omissions: list[str] = Field(default_factory=list)
    structural_strengths: list[str] = Field(default_factory=list)
    structural_weaknesses: list[str] = Field(default_factory=list)

    analysis_failed: bool = False
    failure_reason: str = ""


class CrossSurveyComparison(BaseModel):
    """
    Comparison across all per-paper architectures for one topic.
    Produced by ArchitectureAnalyzer after all per-paper passes are done.
    """

    topic: str

    orientation_distribution: dict[str, int] = Field(default_factory=dict)
    shared_taxonomy_dimensions: list[str] = Field(default_factory=list)

    # Each entry: {dimension, paper_a, paper_a_view, paper_b, paper_b_view}
    conflicting_classifications: list[dict[str, str]] = Field(default_factory=list)

    # Each entry: {aspect, best_covered_by}
    complementary_coverage: list[dict[str, str]] = Field(default_factory=list)

    best_overall_structure: str = ""
    best_overall_structure_reason: str = ""
    coverage_gaps_across_all_surveys: list[str] = Field(default_factory=list)

    comparison_failed: bool = False
    failure_reason: str = ""


class ResearchGap(BaseModel):
    gap: str
    gap_type: str = ""   # "frequency" | "future_convergence" | "conflict"
    evidence: list[str] = Field(default_factory=list)  # paper titles that signal this gap
    opportunity_score: float = 0.0


class ParsedPaper(BaseModel):
    """Full-text structure extracted from a paper's PDF."""

    paper_title: str

    # Top-level section headings (in order)
    sections: list[str] = Field(default_factory=list)
    # {section_heading: [subsection_headings]}
    subsections: dict[str, list[str]] = Field(default_factory=dict)

    # Extracted text from conclusion / conclusion & future work sections
    conclusion_text: str = ""
    # Extracted text from future work sections (may overlap with conclusion_text)
    future_work_text: str = ""

    # Captions / titles of tables (useful for spotting dataset tables)
    table_titles: list[str] = Field(default_factory=list)

    # "pdf" or "abstract_fallback"
    parse_source: str = "pdf"
    parse_failed: bool = False
    failure_reason: str = ""


class ConceptNode(BaseModel):
    """A named concept in the field concept graph."""

    node_id: str          # short slug, e.g. "transformer_architecture"
    name: str
    definition: str = ""
    aliases: list[str] = Field(default_factory=list)
    representative_papers: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list)
    source_surveys: list[str] = Field(default_factory=list)


class ConceptEdge(BaseModel):
    """A typed directed edge in the concept graph."""

    source_id: str
    target_id: str
    # Allowed values: "is_subfield_of" | "uses" | "evaluated_by" | "applied_to" |
    #                 "contrasts_with" | "emerged_after" | "part_of"
    edge_type: str
    evidence: str = ""    # one-sentence justification


class ConceptGraph(BaseModel):
    """Typed concept graph for one research field."""

    topic: str
    nodes: list[ConceptNode] = Field(default_factory=list)
    edges: list[ConceptEdge] = Field(default_factory=list)

    extraction_failed: bool = False
    failure_reason: str = ""


class LandmarkPaper(BaseModel):
    """
    A seminal *primary* paper (not a survey) that the analysed surveys
    repeatedly build on — e.g. ReAct or Self-RAG for Agentic RAG.

    Detected from the survey material, then resolved against OpenAlex and
    kept only if it is genuinely high-impact.  Surfaced so a newcomer can
    read the actual landmark techniques the surveys describe.
    """

    name: str                       # short name, e.g. "Self-RAG"
    title: str = ""                 # resolved full paper title
    year: Optional[int] = None
    citation_count: int = 0
    url: str = ""
    mentioned_by: int = 0           # how many analysed surveys reference it
    why_seminal: str = ""           # one sentence on its foundational role


class ReadingStep(BaseModel):
    """One paper in a curated reading path."""

    step: int
    paper_title: str
    rationale: str = ""                   # why to read this paper at this step
    focus_sections: list[str] = Field(default_factory=list)   # e.g. ["Introduction", "Method"]
    prereq_concepts: list[str] = Field(default_factory=list)  # concepts to know first
    estimated_reading_time: str = ""      # e.g. "45 min"


class ReadingPath(BaseModel):
    """Sequenced reading plan for newcomers to a field."""

    topic: str
    target_audience: str = ""   # e.g. "graduate student new to NLP"
    steps: list[ReadingStep] = Field(default_factory=list)

    generation_failed: bool = False
    failure_reason: str = ""


class FieldGuide(BaseModel):
    """
    Narrative beginner's guide to a research field.
    Generated by FieldGuideGenerator from the mega-architecture.
    """

    topic: str

    what_is_this_field: str = ""
    why_it_matters: str = ""
    core_problems: list[str] = Field(default_factory=list)
    main_approaches: list[str] = Field(default_factory=list)
    key_terms: dict[str, str] = Field(default_factory=dict)  # {term: plain-English definition}
    historical_milestones: list[str] = Field(default_factory=list)
    common_misconceptions: list[str] = Field(default_factory=list)
    how_to_get_started: list[str] = Field(default_factory=list)

    generation_failed: bool = False
    failure_reason: str = ""


class FieldMegaArchitecture(BaseModel):
    """
    Unified field architecture synthesised from all per-paper architectures.
    Produced by MegaArchitectSynthesizer.
    """

    topic: str
    source_papers: list[str] = Field(default_factory=list)  # paper titles used as input

    # One-paragraph narrative introducing the field
    field_summary: str = ""

    # Each entry: {problem, coverage_count, best_paper}
    core_problems: list[dict] = Field(default_factory=list)

    # {family_name: {description, representative_methods, coverage_count}}
    major_tasks: dict[str, dict] = Field(default_factory=dict)
    method_families: dict[str, dict] = Field(default_factory=dict)

    # [{name, task, coverage_count}]
    datasets_and_benchmarks: list[dict] = Field(default_factory=list)
    evaluation_metrics: list[str] = Field(default_factory=list)
    applications: list[str] = Field(default_factory=list)

    # {challenge: {description, severity, coverage_count}}
    challenges: dict[str, dict] = Field(default_factory=dict)
    future_research_directions: list[str] = Field(default_factory=list)

    open_gaps: list[ResearchGap] = Field(default_factory=list)
    mermaid_diagram: str = ""

    # Suggested outline for writing a new survey
    suggested_title_template: str = ""
    suggested_abstract_template: str = ""
    # [{section_number, title, content_hints}]
    suggested_sections: list[dict] = Field(default_factory=list)

    cross_survey_comparison: Optional[CrossSurveyComparison] = None

    synthesis_failed: bool = False
    failure_reason: str = ""
