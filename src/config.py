"""
Configuration loader.

Reads topics.yaml and venues.yaml and exposes a single `AppConfig` dataclass
that the rest of the pipeline consumes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AppConfig:
    # Search parameters
    topics: list[str] = field(default_factory=list)
    survey_terms: list[str] = field(default_factory=list)
    year_from: int = 2021
    year_to: int = 2026
    max_results_per_query: int = 50   # per query per source; 50 is a sensible floor
    top_n_to_summarize: int = 30
    # Papers below this score are dropped after quality scoring (0 = keep all)
    min_quality_score: float = 20.0

    # Venue quality scores loaded from venues.yaml
    venue_scores: dict[str, int] = field(default_factory=dict)

    # Output directory
    output_dir: Path = Path("data/exports")

    # Anthropic API key (read from environment)
    anthropic_api_key: str = ""

    # CORE API key — optional, raises rate limit. Get one free at:
    # https://core.ac.uk/services/api
    core_api_key: str = ""

    # Architecture analysis settings
    architecture_enabled: bool = True
    analyze_top_n: int = 20         # per-paper architecture extraction limit
    mega_architecture_enabled: bool = True

    # Canonical Survey Detector
    canonical_detector_enabled: bool = True

    # LLM-as-Judge settings
    judge_top_n: int = 50            # how many summarised papers to judge

    # Research-gap detection thresholds
    gap_min_surveys: int = 3         # min surveys that must mention a gap
    gap_frequency_threshold: float = 0.3  # fraction of surveys for "frequency" gap


def load_config(
    topics_path: str | Path,
    venues_path: str | Path | None = None,
    llm_path: str | Path | None = None,
    overrides: dict | None = None,
) -> AppConfig:
    """
    Load AppConfig from YAML files and optional CLI overrides.

    Args:
        topics_path: Path to topics.yaml.
        venues_path: Path to venues.yaml. Defaults to config/venues.yaml
                     in the same directory as topics_path.
        llm_path: Path to llm.yaml (LLM / judge / gap settings).
                  Defaults to config/llm.yaml alongside topics_path.
        overrides: Dict of field name → value to override after loading.
    """
    topics_path = Path(topics_path)
    with topics_path.open() as f:
        raw = yaml.safe_load(f)

    if venues_path is None:
        venues_path = topics_path.parent / "venues.yaml"
    venues_path = Path(venues_path)

    venue_scores: dict[str, int] = {}
    if venues_path.exists():
        with venues_path.open() as f:
            venues_raw = yaml.safe_load(f) or {}
        venue_scores = venues_raw.get("venues", {})

    if llm_path is None:
        llm_path = topics_path.parent / "llm.yaml"
    llm_path = Path(llm_path)

    llm_raw: dict = {}
    if llm_path.exists():
        with llm_path.open() as f:
            llm_raw = yaml.safe_load(f) or {}

    cfg = AppConfig(
        topics=raw.get("topics", []),
        survey_terms=raw.get("survey_terms", ["survey", "review", "taxonomy", "overview"]),
        year_from=raw.get("year_from", 2021),
        year_to=raw.get("year_to", 2026),
        max_results_per_query=raw.get("max_results_per_query", 20),
        top_n_to_summarize=raw.get("top_n_to_summarize", 30),
        min_quality_score=raw.get("min_quality_score", 20.0),
        venue_scores=venue_scores,
        output_dir=Path(raw.get("output_dir", "data/exports")),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        core_api_key=os.environ.get("CORE_API_KEY", ""),
        architecture_enabled=raw.get("architecture_enabled", True),
        analyze_top_n=raw.get("analyze_top_n", 20),
        mega_architecture_enabled=raw.get("mega_architecture_enabled", True),
        canonical_detector_enabled=llm_raw.get("canonical_detector_enabled", True),
        judge_top_n=llm_raw.get("judge_top_n", 50),
        gap_min_surveys=llm_raw.get("gap_min_surveys", 3),
        gap_frequency_threshold=llm_raw.get("gap_frequency_threshold", 0.3),
    )

    if overrides:
        for key, value in overrides.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    return cfg
