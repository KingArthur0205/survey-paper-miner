"""
AI Survey Paper Miner — main entry point.

Three operating modes:

  run     (default) — full pipeline end-to-end
  fetch             — steps 1–6b: retrieve, filter, deduplicate, score, stratify,
                      then save to DB + papers_scored.jsonl
  analyze           — steps 7+: load scored papers from papers_scored.jsonl,
                      then summarise, judge, architecture, concept-graph,
                      reading-path, field-guide, export

Usage:
    python main.py [run] --config config/topics.yaml
    python main.py fetch  --config config/topics.yaml
    python main.py analyze --config config/topics.yaml --papers-file data/processed/papers_scored.jsonl

Pipeline steps:
    1.  Load config
    2.  Build queries (LLM or cross-product)
    3.  Retrieve papers (arXiv + OpenAlex + CORE in parallel)
    4.  Topic relevance filter + survey-signal filter
    5.  Deduplicate
    5b. LLM relevance filter
    5c. Canonical Survey Detector  (writes paper.canonical_score)
    6.  Score and rank
    6b. Temporal Stratifier        (writes paper.authority_tier)
    --- fetch mode ends here; saves papers_scored.jsonl ---
    7.  LLM summarisation (top-N)
    7b. PDF full-text parser       (enriches arch prompt)
    8.  Architecture analysis + Mega-Architecture synthesis
    8b. LLM-as-Judge               (authority assessment)
    9.  Concept graph extraction   (per topic)
    9b. Reading path generation    (per topic)
    9c. Field guide generation     (per topic)
    10. Persist to SQLite
    11. Export XLSX / CSV / JSONL / Markdown / Architecture reports
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# Load .env before any module that reads environment variables.
# load_dotenv() defaults to override=False, so a variable already present in the
# shell environment wins over .env — even when it's an EMPTY string. That makes
# an exported `ANTHROPIC_API_KEY=` silently disable every LLM pass while CORE/S2
# (which aren't pre-set) still load. To be robust, let .env fill in any key the
# environment is missing OR has blank, without clobbering a real exported value.
load_dotenv()
for _k, _v in (dotenv_values() or {}).items():
    if _v and not os.environ.get(_k):
        os.environ[_k] = _v

from src.config import load_config, AppConfig
from src.query_builder import build_queries, SearchQuery
from src.retrievers import ArxivRetriever, OpenAlexRetriever, CoreRetriever
from src.filter import filter_all_topics, filter_survey_signal, filter_min_score
from src.llm_filter import llm_filter_papers
from src.cache import QueryCache
from src.dedup import deduplicate
from src.canonical import detect_canonical_surveys
from src.scorer import score_papers
from src.stratifier import stratify_papers
from src.summarizer import LLMSummarizer
from src.judge import LLMJudge
from src.architecture_analyzer import ArchitectureAnalyzer
from src.mega_architect import MegaArchitectSynthesizer
from src.database import Database
from src.export import Exporter, make_run_dir
from src.models import JudgeResult, Paper, ScoredPaper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI Survey Paper Miner — find and rank high-quality AI survey papers."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["run", "fetch", "analyze"],
        default="run",
        help=(
            "Pipeline mode: 'run' (default) = full pipeline; "
            "'fetch' = retrieve+score only; "
            "'analyze' = summarise+judge+arch+extras from saved papers_scored.jsonl"
        ),
    )
    parser.add_argument(
        "--config",
        default="config/topics.yaml",
        help="Path to topics.yaml (default: config/topics.yaml)",
    )
    parser.add_argument(
        "--venues-config",
        default=None,
        help="Path to venues.yaml (default: config/venues.yaml alongside topics.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for CSV/JSONL/Markdown (overrides config)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Max papers fetched per query per source (overrides config)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="Number of top papers to summarise with LLM (overrides config)",
    )
    parser.add_argument(
        "--year-from",
        type=int,
        default=None,
        help="Earliest publication year (overrides config)",
    )
    parser.add_argument(
        "--year-to",
        type=int,
        default=None,
        help="Latest publication year (overrides config)",
    )
    parser.add_argument(
        "--papers-file",
        default="data/processed/papers_scored.jsonl",
        help=(
            "Path to papers_scored.jsonl used by 'analyze' mode "
            "(default: data/processed/papers_scored.jsonl)"
        ),
    )
    parser.add_argument(
        "--no-llm-queries",
        action="store_true",
        help="Use mechanical topic×term cross-product instead of LLM-generated queries",
    )
    parser.add_argument(
        "--no-llm-filter",
        action="store_true",
        help="Skip LLM relevance filter (faster, but may include off-topic papers)",
    )
    parser.add_argument(
        "--no-summarize",
        action="store_true",
        help="Skip LLM summarisation (faster, no API cost)",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip LLM-as-Judge authority assessment pass",
    )
    parser.add_argument(
        "--no-architecture",
        action="store_true",
        help="Skip architecture analysis and mega-architecture synthesis",
    )
    parser.add_argument(
        "--no-pdf-parse",
        action="store_true",
        help="Skip PDF full-text parsing (no pdfplumber required)",
    )
    parser.add_argument(
        "--no-system-design",
        "--no-concept-graph",       # backwards-compatible alias
        dest="no_system_design",
        action="store_true",
        help="Skip the top-down system-design synthesis (Part 3)",
    )
    parser.add_argument(
        "--no-reading-path",
        action="store_true",
        help="Skip reading path generation",
    )
    parser.add_argument(
        "--no-landmarks",
        action="store_true",
        help="Skip landmark seminal primary-paper detection (ReAct/Self-RAG style)",
    )
    parser.add_argument(
        "--no-top-surveys",
        action="store_true",
        help="Skip injecting the highest-cited surveys per topic into the pool",
    )
    parser.add_argument(
        "--arxiv",
        action="store_true",
        help=(
            "Enable the arXiv retriever (disabled by default). "
            "OpenAlex already indexes all arXiv papers with richer metadata, "
            "so enabling arXiv adds minimal coverage at the cost of rate-limit "
            "errors (429s). Only useful if you need papers within the 1-2 day "
            "window before OpenAlex picks them up."
        ),
    )
    parser.add_argument(
        "--db",
        default="data/processed/papers.db",
        help="SQLite database path (default: data/processed/papers.db)",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Mode dispatchers
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(args: argparse.Namespace) -> None:
    """Full pipeline (fetch + analyze) — backward-compatible default."""
    cfg = _load_cfg(args)
    scored_papers = _run_fetch_steps(args, cfg)
    _run_analyze_steps(args, cfg, scored_papers)


def run_fetch(args: argparse.Namespace) -> None:
    """
    Fetch-only mode: retrieve → filter → deduplicate → score → stratify.
    Saves results to papers_scored.jsonl for later use by 'analyze' mode.
    """
    cfg = _load_cfg(args)
    scored_papers = _run_fetch_steps(args, cfg)

    # Save to DB
    db = Database(args.db)
    db.init_schema()
    db.upsert_papers(scored_papers)
    db.close()

    # Save to JSONL for 'analyze' mode
    papers_file = Path(args.papers_file)
    papers_file.parent.mkdir(parents=True, exist_ok=True)
    with papers_file.open("w", encoding="utf-8") as f:
        for sp in scored_papers:
            f.write(json.dumps(sp.model_dump(), ensure_ascii=False) + "\n")

    print("\n" + "─" * 60)
    print("✓ Fetch complete")
    print(f"  Papers ranked:  {len(scored_papers)}")
    print(f"\nSaved to: {papers_file}")
    print(f"  Run analysis with: python main.py analyze --papers-file {papers_file}")
    print("─" * 60 + "\n")


def run_analyze(args: argparse.Namespace) -> None:
    """
    Analyze-only mode: load scored papers from JSONL, then run all LLM passes.
    """
    papers_file = Path(args.papers_file)
    if not papers_file.exists():
        logger.error(
            "papers_scored.jsonl not found at '%s'. "
            "Run 'python main.py fetch' first, or specify --papers-file.",
            papers_file,
        )
        sys.exit(1)

    cfg = _load_cfg(args)

    logger.info("Loading scored papers from %s …", papers_file)
    scored_papers: list[ScoredPaper] = []
    with papers_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    scored_papers.append(ScoredPaper.model_validate(json.loads(line)))
                except Exception as exc:
                    logger.warning("Skipping malformed line in papers_scored.jsonl: %s", exc)

    logger.info("Loaded %d scored papers from %s", len(scored_papers), papers_file)
    _run_analyze_steps(args, cfg, scored_papers)


# ─────────────────────────────────────────────────────────────────────────────
# Shared step functions
# ─────────────────────────────────────────────────────────────────────────────

def _prioritise_top_surveys(
    scored_papers: list[ScoredPaper],
    top_n: int,
) -> list[ScoredPaper]:
    """
    Reorder so curated top-cited surveys come first, guaranteeing they fall
    within the top-N summarise window even if their citation score is low.

    Does not mutate the input list; the global `scored_papers` ranking (used
    for export) is left untouched.
    """
    injected = [sp for sp in scored_papers if sp.paper.from_top_survey]
    if not injected:
        return scored_papers
    others = [sp for sp in scored_papers if not sp.paper.from_top_survey]
    if len(injected) > top_n:
        logger.warning(
            "%d curated top-surveys exceed top_n=%d — some won't be analysed. "
            "Raise top_n_to_summarize to cover them all.",
            len(injected), top_n,
        )
    logger.info(
        "Prioritising %d curated top-survey(s) into the summarise window.",
        len(injected),
    )
    return injected + others


def _load_cfg(args: argparse.Namespace) -> AppConfig:
    overrides: dict = {}
    if args.output_dir:
        overrides["output_dir"] = Path(args.output_dir)
    if args.max_results:
        overrides["max_results_per_query"] = args.max_results
    if args.top_n:
        overrides["top_n_to_summarize"] = args.top_n
    if args.year_from:
        overrides["year_from"] = args.year_from
    if args.year_to:
        overrides["year_to"] = args.year_to

    cfg = load_config(args.config, venues_path=args.venues_config, overrides=overrides)
    logger.info(
        "Config loaded: %d topics, %d–%d, top-%d to summarise",
        len(cfg.topics), cfg.year_from, cfg.year_to, cfg.top_n_to_summarize,
    )
    return cfg


def _run_fetch_steps(
    args: argparse.Namespace,
    cfg: AppConfig,
) -> list[ScoredPaper]:
    """Steps 1–6b: retrieve, filter, dedup, score, stratify."""

    # ------------------------------------------------------------------ #
    # Volume sanity check                                                  #
    # ------------------------------------------------------------------ #
    _warn_low_volume(cfg)

    # ------------------------------------------------------------------ #
    # 2. Build queries                                                     #
    # ------------------------------------------------------------------ #
    use_llm_queries = not args.no_llm_queries
    queries = build_queries(cfg, use_llm=use_llm_queries)
    logger.info(
        "Generated %d queries via %s",
        len(queries),
        "LLM" if (use_llm_queries and cfg.anthropic_api_key) else "cross-product",
    )

    # ------------------------------------------------------------------ #
    # 3. Retrieve papers from all sources                                  #
    # ------------------------------------------------------------------ #
    cache = QueryCache(path="data/raw/query_cache.json", ttl_days=7)

    # arXiv is disabled by default: OpenAlex already indexes all arXiv papers
    # with richer metadata (citation counts, DOIs), so arXiv adds minimal
    # coverage. Pass --arxiv to opt-in if you specifically need it.
    retrievers = [OpenAlexRetriever()]
    if getattr(args, "arxiv", False):
        retrievers.insert(0, ArxivRetriever())
        logger.info(
            "arXiv retriever enabled (--arxiv flag). "
            "Note: arXiv rate-limits aggressive clients; 429s are expected."
        )
    else:
        logger.info(
            "arXiv retriever disabled (default). "
            "Pass --arxiv to enable it. OpenAlex covers all arXiv papers."
        )

    if cfg.core_api_key:
        retrievers.append(CoreRetriever(api_key=cfg.core_api_key))
    else:
        logger.info(
            "CORE retriever disabled — no CORE_API_KEY set. "
            "Add CORE_API_KEY to .env to enable it (free at core.ac.uk/services/api)."
        )
    for r in retrievers:
        r._cache = cache

    all_papers: list[Paper] = _retrieve_all(queries, retrievers, cfg)
    cache.save()

    logger.info(
        "Cache stats: %d hits, %d misses, %d total entries",
        cache.stats["hits"], cache.stats["misses"], cache.stats["total_entries"],
    )

    logger.info("Total papers retrieved (before filtering): %d", len(all_papers))

    # ------------------------------------------------------------------ #
    # 3b. Top-cited surveys per topic (coverage guarantee)                #
    #     Retrieved separately and merged in AFTER the crude keyword /     #
    #     binary filters: these are already verified topic surveys (matched #
    #     by OpenAlex title search + sorted by citations), so the literal   #
    #     keyword filter — which would drop e.g. "Graph RAG: A Survey" for  #
    #     an "Agentic RAG" topic because the acronym is spelled out — must  #
    #     not discard them. The judge still rates and filters them.         #
    # ------------------------------------------------------------------ #
    top_surveys: list[Paper] = []
    if cfg.top_surveys_enabled and not args.no_top_surveys:
        from src.top_survey_retriever import retrieve_top_surveys
        top_surveys = retrieve_top_surveys(
            cfg.topics, cfg.year_from, cfg.year_to,
            per_topic=cfg.top_surveys_per_topic,
        )

    # ------------------------------------------------------------------ #
    # 4. Three-layer relevance filter (on query-retrieved papers only)     #
    # ------------------------------------------------------------------ #
    all_papers = filter_all_topics(all_papers, min_fraction=0.5)
    all_papers = filter_survey_signal(all_papers)
    logger.info("Papers after relevance + survey-signal filter: %d", len(all_papers))

    # ------------------------------------------------------------------ #
    # 5. Deduplicate                                                        #
    # ------------------------------------------------------------------ #
    unique_papers = deduplicate(all_papers)
    logger.info("Unique papers after dedup: %d", len(unique_papers))

    # ------------------------------------------------------------------ #
    # 5b. LLM relevance filter                                             #
    # ------------------------------------------------------------------ #
    if not args.no_llm_filter:
        unique_papers = llm_filter_papers(unique_papers, cfg)
        logger.info("Papers after LLM relevance filter: %d", len(unique_papers))

    # Merge the curated top-cited surveys now (they bypass the crude keyword
    # and binary LLM filters), then re-dedup against the surviving pool.
    if top_surveys:
        before = len(unique_papers)
        unique_papers = deduplicate(unique_papers + top_surveys)
        logger.info(
            "Merged %d top-cited surveys → %d unique papers (was %d).",
            len(top_surveys), len(unique_papers), before,
        )

    # ------------------------------------------------------------------ #
    # 5c. Canonical Survey Detector                                        #
    # ------------------------------------------------------------------ #
    if cfg.canonical_detector_enabled:
        unique_papers = detect_canonical_surveys(unique_papers)

    # ------------------------------------------------------------------ #
    # 6. Score and rank                                                    #
    # ------------------------------------------------------------------ #
    scored_papers = score_papers(unique_papers, cfg)

    # Min-score gate — but curated top-cited surveys are exempt. They are
    # citation-ranked popular surveys that may be brand-new (few citations →
    # low score); cutting them here would defeat the injection. Let the judge
    # decide their fate instead.
    before_ms = len(scored_papers)
    kept = filter_min_score(scored_papers, min_score=cfg.min_quality_score)
    kept_titles = {sp.paper.title for sp in kept}
    rescued = [
        sp for sp in scored_papers
        if sp.paper.from_top_survey and sp.paper.title not in kept_titles
    ]
    if rescued:
        kept.extend(rescued)
        kept.sort(key=lambda sp: sp.quality_score, reverse=True)
        logger.info(
            "Min-score filter (>= %.0f): kept %d / %d (+%d curated top-surveys exempt)",
            cfg.min_quality_score, len(kept) - len(rescued), before_ms, len(rescued),
        )
    scored_papers = kept

    logger.info(
        "Top paper: '%s' (score=%.1f)",
        scored_papers[0].paper.title[:80] if scored_papers else "—",
        scored_papers[0].quality_score if scored_papers else 0,
    )

    # ------------------------------------------------------------------ #
    # 6b. Temporal Stratifier                                              #
    # ------------------------------------------------------------------ #
    scored_papers = stratify_papers(scored_papers)

    return scored_papers


def _run_analyze_steps(
    args: argparse.Namespace,
    cfg: AppConfig,
    scored_papers: list[ScoredPaper],
) -> None:
    """Steps 7–11: LLM passes, architecture, extras, export."""

    # ------------------------------------------------------------------ #
    # 7. Summarise top-N with LLM                                         #
    # ------------------------------------------------------------------ #
    summary_pairs = []
    if not args.no_summarize:
        if not cfg.anthropic_api_key:
            logger.warning(
                "ANTHROPIC_API_KEY not set — skipping LLM summarisation. "
                "Set it in .env or pass --no-summarize to suppress this warning."
            )
        else:
            # Dynamic sizing: top_n_to_summarize <= 0 means AUTO — summarise
            # every paper that passed the pre-judge filters, up to max_summarize.
            # So the analysed count scales with the fetch result rather than a
            # fixed cap; the judge then filters to the relevant subset.
            if cfg.top_n_to_summarize and cfg.top_n_to_summarize > 0:
                effective_n = cfg.top_n_to_summarize
                logger.info(
                    "Summarising up to %d papers (fixed cap) with %s …",
                    effective_n, "claude-sonnet-4-6",
                )
            else:
                effective_n = min(len(scored_papers), cfg.max_summarize)
                logger.info(
                    "Summarising %d papers (AUTO — all %d filtered, cap %d) with %s …",
                    effective_n, len(scored_papers), cfg.max_summarize, "claude-sonnet-4-6",
                )
            # Guarantee curated top-cited surveys a slot in the summarise/judge
            # window: place them first so they aren't pushed out by higher-scored
            # (often tangential, more-cited) papers. The judge re-ranks afterwards.
            summarize_input = _prioritise_top_surveys(scored_papers, effective_n)
            summarizer = LLMSummarizer(cfg)
            summary_pairs = summarizer.summarize_top_n(summarize_input, effective_n)
            failed = sum(1 for _, s in summary_pairs if s.summarization_failed)
            logger.info(
                "Summarisation complete: %d succeeded, %d failed",
                len(summary_pairs) - failed, failed,
            )

    # ------------------------------------------------------------------ #
    # 7b. PDF full-text parser                                             #
    # ------------------------------------------------------------------ #
    parsed_map: dict = {}
    run_pdf_parse = (
        not args.no_pdf_parse
        and bool(summary_pairs)
    )
    if run_pdf_parse:
        try:
            from src.pdf_parser import parse_papers
            cache_dir = Path("data/raw/parsed_pdfs")
            papers_to_parse = [sp for sp, _ in summary_pairs]
            logger.info("Parsing PDFs for %d papers …", len(papers_to_parse))
            parsed_map = parse_papers(papers_to_parse, cache_dir)
            success = sum(1 for p in parsed_map.values() if not p.parse_failed)
            logger.info(
                "PDF parsing complete: %d succeeded, %d failed",
                success, len(parsed_map) - success,
            )
        except ImportError:
            logger.warning(
                "pdfplumber not installed — skipping PDF parsing. "
                "Run: pip install pdfplumber"
            )
        except Exception as exc:
            logger.warning("PDF parsing failed: %s", exc)
    elif args.no_pdf_parse:
        logger.info("Skipping PDF parsing (--no-pdf-parse).")

    # ------------------------------------------------------------------ #
    # 8. LLM-as-Judge  (moved before architecture so we can skip papers   #
    #    the judge deems off-topic before spending architecture tokens)    #
    # ------------------------------------------------------------------ #
    judge_map: dict[str, JudgeResult] = {}
    run_judge = (
        not args.no_judge
        and bool(summary_pairs)
        and bool(cfg.anthropic_api_key)
    )
    if run_judge:
        logger.info("Running LLM judge on top %d papers …", cfg.judge_top_n)
        judge = LLMJudge(cfg)
        judge_triples = judge.judge_papers(summary_pairs)
        judge_map = {sp.paper.title: jr for sp, _, jr in judge_triples}
        failed_j = sum(1 for _, _, jr in judge_triples if jr.judge_failed)
        logger.info(
            "Judge complete: %d assessed, %d failed",
            len(judge_triples) - failed_j, failed_j,
        )

        # ── Re-rank scored_papers by judge-adjusted score ──────────────
        # The base quality_score rewards citations and venue but is blind
        # to topic specificity.  The judge now assigns topic_relevance (1-5)
        # and recommended_action, which we fold in here so the final ranking
        # reflects actual relevance to the configured research topics.
        #
        # Adjustment formula (centred at topic_relevance=3, action=optional):
        #   +40  must_read      +0  optional
        #   +20  worth_reading  -100 skip  (effectively removed from output)
        #   topic_relevance bonus/penalty: (relevance - 3) × 10
        #   → max uplift: +40 + 20 = +60   max penalty: -100 + -20 = -120
        _ACTION_DELTA = {
            "must_read": 40, "worth_reading": 20, "optional": 0, "skip": -100,
        }
        _RELEVANCE_WEIGHT = 10  # pts per step above/below the neutral 3

        for sp in scored_papers:
            jr = judge_map.get(sp.paper.title)
            if jr and not jr.judge_failed:
                action_delta     = _ACTION_DELTA.get(jr.recommended_action, 0)
                relevance_delta  = (jr.topic_relevance - 3) * _RELEVANCE_WEIGHT
                sp.judge_adjusted_score = sp.quality_score + action_delta + relevance_delta
            else:
                # No judge result → keep original score (no penalty)
                sp.judge_adjusted_score = sp.quality_score

        scored_papers.sort(key=lambda x: x.judge_adjusted_score, reverse=True)

        skip_count = sum(
            1 for sp in scored_papers
            if judge_map.get(sp.paper.title, JudgeResult(paper_title=sp.paper.title)).recommended_action == "skip"
        )
        logger.info(
            "Re-ranked %d papers by judge-adjusted score "
            "(%d marked 'skip' — moved to bottom of results).",
            len(scored_papers), skip_count,
        )

        # ── Hard relevance / tier / domain filters ─────────────────────
        # Runs after re-ranking so the order is already correct.  A paper is
        # dropped if it fails ANY of:
        #   - topic_relevance < cfg.min_topic_relevance
        #   - paper_tier below cfg.min_paper_tier   (core>useful>marginal>cut)
        #   - is_domain_specific and cfg.exclude_domain_specific
        _TIER_RANK = {"core": 3, "useful": 2, "marginal": 1, "cut": 0}
        min_rel  = cfg.min_topic_relevance
        min_tier = _TIER_RANK.get(cfg.min_paper_tier, 2)

        before_filt = len(scored_papers)
        removed: list[str] = []
        kept_papers: list = []
        for sp in scored_papers:
            jr = judge_map.get(sp.paper.title)
            if not jr or jr.judge_failed:
                kept_papers.append(sp)   # never drop on a failed assessment
                continue

            reasons: list[str] = []
            if jr.topic_relevance < min_rel:
                reasons.append(f"relevance={jr.topic_relevance}<{min_rel}")
            if _TIER_RANK.get(jr.paper_tier, 2) < min_tier:
                reasons.append(f"tier={jr.paper_tier}")
            if cfg.exclude_domain_specific and jr.is_domain_specific:
                reasons.append("domain-specific")

            if reasons:
                removed.append(f"'{sp.paper.title[:50]}' [{', '.join(reasons)}]")
            else:
                kept_papers.append(sp)

        scored_papers = kept_papers
        if removed:
            logger.info(
                "Relevance/tier filter (min_relevance>=%d, min_tier=%s%s): "
                "removed %d / %d papers.",
                min_rel, cfg.min_paper_tier,
                ", no-domain-specific" if cfg.exclude_domain_specific else "",
                len(removed), before_filt,
            )
            for entry in removed:
                logger.info("  ✗ %s", entry)
        else:
            logger.info(
                "Relevance/tier filter: all %d judged papers passed.", before_filt
            )

    elif args.no_judge:
        logger.info("Skipping LLM judge (--no-judge).")
    elif not summary_pairs:
        logger.info("Skipping LLM judge — no summaries available.")

    # ── Align summary_pairs with the tier/relevance-filtered set ──────────
    # Only papers that survived the relevance/tier filter (still present in
    # scored_papers) proceed to architecture analysis.  This keeps exports and
    # architecture analysis consistent and saves LLM tokens on dropped papers.
    if judge_map:
        kept_titles = {sp.paper.title for sp in scored_papers}
        before = len(summary_pairs)
        summary_pairs = [
            (sp, s) for sp, s in summary_pairs if sp.paper.title in kept_titles
        ]
        skipped = before - len(summary_pairs)
        if skipped:
            logger.info(
                "Excluded %d filtered-out papers from architecture analysis "
                "(%d remain).", skipped, len(summary_pairs),
            )

    # ------------------------------------------------------------------ #
    # 8b. Architecture analysis  (now runs on judge-filtered papers only) #
    # ------------------------------------------------------------------ #
    arch_triples_by_topic: dict[str, tuple] = {}

    run_architecture = (
        not args.no_architecture
        and cfg.architecture_enabled
        and bool(summary_pairs)
        and bool(cfg.anthropic_api_key)
    )

    if run_architecture:
        logger.info(
            "Running architecture analysis on %d papers …",
            len(summary_pairs),
        )
        arch_analyzer = ArchitectureAnalyzer(cfg)
        arch_triples = arch_analyzer.analyze(summary_pairs, parsed_map=parsed_map)
        comparisons = arch_analyzer.compare_by_topic(arch_triples)

        if cfg.mega_architecture_enabled:
            synth = MegaArchitectSynthesizer(cfg)
            by_topic: dict = defaultdict(list)
            for triple in arch_triples:
                sp = triple[0]
                topic_key = sp.paper.topic_queries[0] if sp.paper.topic_queries else "Uncategorised"
                by_topic[topic_key].append(triple)

            for topic_key, triples in by_topic.items():
                valid = [t for t in triples if not t[2].analysis_failed]
                if len(valid) < 2:
                    continue
                cmp = comparisons.get(topic_key)
                mega = synth.synthesize(topic_key, triples, cmp)
                arch_triples_by_topic[topic_key] = (triples, mega)
    else:
        if not summary_pairs:
            logger.info("Skipping architecture analysis — no summaries available.")
        elif args.no_architecture:
            logger.info("Skipping architecture analysis (--no-architecture).")

    # ------------------------------------------------------------------ #
    # 9. System design + reading path + landmarks (per topic)            #
    # ------------------------------------------------------------------ #
    system_designs: dict[str, object] = {}
    reading_paths: dict[str, object] = {}
    landmarks_by_topic: dict[str, list] = {}

    # Landmark detection works off the per-topic summary pairs
    summaries_by_topic: dict[str, list] = defaultdict(list)
    for sp, s in summary_pairs:
        tkey = sp.paper.topic_queries[0] if sp.paper.topic_queries else "Uncategorised"
        summaries_by_topic[tkey].append((sp, s))

    for topic_key, (triples, mega) in arch_triples_by_topic.items():
        # Build judge_triples for this topic (needed by reading path)
        topic_judge_triples = []
        if judge_map:
            for sp, summary, arch in triples:
                jr = judge_map.get(sp.paper.title)
                if jr:
                    topic_judge_triples.append((sp, summary, jr))

        # 9. System design (top-down architecture of the field)
        if not args.no_system_design and cfg.anthropic_api_key and not mega.synthesis_failed:
            try:
                from src.system_design import SystemDesignSynthesizer
                synth = SystemDesignSynthesizer(cfg)
                sd = synth.synthesize(topic_key, mega, triples)
                system_designs[topic_key] = sd
            except Exception as exc:
                logger.warning("[system_design] Failed for '%s': %s", topic_key, exc)

        # 9b. Reading path
        if not args.no_reading_path and cfg.anthropic_api_key and topic_judge_triples:
            try:
                from src.reading_path import ReadingPathGenerator
                rp_gen = ReadingPathGenerator(cfg)
                rp = rp_gen.generate(topic_key, mega, topic_judge_triples, max_papers=10)
                reading_paths[topic_key] = rp
            except Exception as exc:
                logger.warning("[reading_path] Failed for '%s': %s", topic_key, exc)

        # 9c. Landmark seminal primary papers (ReAct/Self-RAG style)
        if (
            not args.no_landmarks
            and cfg.landmarks_enabled
            and cfg.anthropic_api_key
        ):
            try:
                from src.landmark_detector import LandmarkDetector
                detector = LandmarkDetector(cfg)
                topic_summaries = summaries_by_topic.get(topic_key, [])
                lms = detector.detect(topic_key, topic_summaries)
                if lms:
                    landmarks_by_topic[topic_key] = lms
            except Exception as exc:
                logger.warning("[landmarks] Failed for '%s': %s", topic_key, exc)

    # ------------------------------------------------------------------ #
    # 10. Persist to SQLite                                                #
    # ------------------------------------------------------------------ #
    db = Database(args.db)
    db.init_schema()
    title_to_id = db.upsert_papers(scored_papers)
    if summary_pairs:
        db.upsert_summaries(summary_pairs, title_to_id)
    db.close()

    # ------------------------------------------------------------------ #
    # 11. Export                                                           #
    # ------------------------------------------------------------------ #
    run_dir = make_run_dir(cfg.topics, cfg.output_dir)
    exporter = Exporter(run_dir)
    # report.md / report.html sit at the run root beside papers_ranked.xlsx;
    # slug-prefix them only when several topics share this run.
    exporter.multi_topic = len(arch_triples_by_topic) > 1

    xlsx_path = exporter.export_xlsx(scored_papers, summary_pairs)

    arch_report_paths = []
    for topic_key, (triples, mega) in arch_triples_by_topic.items():
        rp = reading_paths.get(topic_key)
        sd = system_designs.get(topic_key)
        lms = landmarks_by_topic.get(topic_key)

        rpt = exporter.export_architecture_report(
            topic_key, triples, mega,
            judge_map=judge_map or None,
            reading_path=rp,
            system_design=sd,
            landmarks=lms,
            field_map_style=cfg.field_map_style,
        )
        arch_report_paths.append(rpt)

        # Interactive HTML version (Field Map outline/diagram toggle in-browser)
        try:
            html_rpt = exporter.export_html_report(
                topic_key, triples, mega,
                judge_map=judge_map or None,
                reading_path=rp,
                system_design=sd,
                landmarks=lms,
            )
            arch_report_paths.append(html_rpt)
        except Exception as exc:
            logger.warning("[export] HTML report failed for '%s': %s", topic_key, exc)

        json_path = exporter.export_mega_architecture_json(topic_key, mega)
        if json_path:
            arch_report_paths.append(json_path)

        mmd_path = exporter.export_mega_architecture_mmd(topic_key, mega)
        if mmd_path:
            arch_report_paths.append(mmd_path)
            png_path = mmd_path.with_suffix(".png")
            if png_path.exists():
                arch_report_paths.append(png_path)

        html_path = exporter.export_mindmap_html(topic_key, mega, arch_triples=triples)
        if html_path:
            arch_report_paths.append(html_path)

        if sd:
            sd_path = exporter.export_system_design_json(topic_key, sd)
            if sd_path:
                arch_report_paths.append(sd_path)

        if rp:
            rp_path = exporter.export_reading_path_json(topic_key, rp)
            if rp_path:
                arch_report_paths.append(rp_path)

    print("\n" + "─" * 60)
    print("✓ Pipeline complete")
    print(f"  Papers ranked:     {len(scored_papers)}")
    print(f"  Papers summarised: {len(summary_pairs)}")
    if judge_map:
        print(f"  Papers judged:     {len(judge_map)}")
    if arch_triples_by_topic:
        print(f"  Topics analysed:   {len(arch_triples_by_topic)}")
    if system_designs:
        print(f"  System designs:    {len(system_designs)}")
    if reading_paths:
        print(f"  Reading paths:     {len(reading_paths)}")
    if landmarks_by_topic:
        total_lms = sum(len(v) for v in landmarks_by_topic.values())
        print(f"  Landmark papers:   {total_lms}")
    print(f"\nOutputs saved to: {run_dir}")
    print(f"  XLSX   → {xlsx_path.name}")
    for p in arch_report_paths:
        print(f"  Arch   → {p.name}")
    print("─" * 60 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval helpers (shared)
# ─────────────────────────────────────────────────────────────────────────────

_ARXIV_MAX_QUERIES = 5   # arXiv is rate-limited and already covered by OpenAlex

# ── Volume sanity-check ───────────────────────────────────────────────────────

_LLM_QUERIES_PER_TOPIC = 10  # mirrors query_builder._LLM_QUERIES_PER_TOPIC

def _warn_low_volume(cfg: AppConfig) -> None:
    """
    Warn when config settings will almost certainly produce too few papers.

    The retrieval funnel is:
        (topics × queries_per_topic × sources × results_per_query)
        → dedup (expect ~40–60 % unique)
        → survey-signal filter (expect ~40–60 % pass)
        → LLM filter (expect ~70–90 % pass)
        → scoring

    Rule of thumb: you need at least ~200 raw papers before filtering to
    reliably end up with 10+ after all filters for a broad topic.
    """
    n_sources = 1  # OpenAlex always present; CORE optional
    if cfg.core_api_key:
        n_sources += 1

    raw_upper_bound = (
        len(cfg.topics)
        * _LLM_QUERIES_PER_TOPIC
        * n_sources
        * cfg.max_results_per_query
    )

    if cfg.max_results_per_query < 25:
        logger.warning(
            "max_results_per_query=%d is very low. "
            "Upper-bound raw paper count before filtering: ~%d. "
            "After dedup + survey-signal + LLM filters you may end up with 0–5 papers. "
            "Recommend max_results_per_query >= 50 for broad topics like 'Computer Vision'.",
            cfg.max_results_per_query, raw_upper_bound,
        )
    elif raw_upper_bound < 200:
        logger.warning(
            "Estimated raw paper pool is only ~%d papers before filtering "
            "(topics=%d × queries=%d × sources=%d × results=%d). "
            "Consider increasing max_results_per_query (currently %d) "
            "or adding more topics/subtopics.",
            raw_upper_bound,
            len(cfg.topics), _LLM_QUERIES_PER_TOPIC, n_sources,
            cfg.max_results_per_query, cfg.max_results_per_query,
        )


def _arxiv_query_subset(queries: list[SearchQuery]) -> set[str]:
    """
    Return at most _ARXIV_MAX_QUERIES query strings spread evenly across topics,
    so arXiv receives a representative but manageable load.
    OpenAlex + CORE receive all queries, so coverage is not reduced.
    """
    by_topic: dict[str, list[str]] = defaultdict(list)
    for q in queries:
        by_topic[q.topic].append(q.query_string)

    selected: list[str] = []
    topic_iters = {t: iter(qs) for t, qs in by_topic.items()}
    while len(selected) < _ARXIV_MAX_QUERIES and topic_iters:
        exhausted = []
        for topic, it in topic_iters.items():
            if len(selected) >= _ARXIV_MAX_QUERIES:
                break
            try:
                selected.append(next(it))
            except StopIteration:
                exhausted.append(topic)
        for t in exhausted:
            del topic_iters[t]

    return set(selected)


def _retrieve_all(
    queries: list[SearchQuery],
    retrievers,
    cfg: AppConfig,
) -> list[Paper]:
    """
    Fan out queries to all retrievers in parallel.

    arXiv is capped at _ARXIV_MAX_QUERIES total (spread across topics) because:
      - arXiv enforces a strict rate limit (1 req / 3 s) and 429s are common
        when many queries fire concurrently.
      - OpenAlex already indexes all arXiv papers with richer metadata, so
        arXiv mostly adds coverage at the margins.
    OpenAlex and CORE receive the full query set.
    """
    all_papers: list[Paper] = []
    arxiv_allowed = _arxiv_query_subset(queries)
    logger.info(
        "arXiv capped at %d / %d queries; OpenAlex + CORE get all %d",
        len(arxiv_allowed), len(queries), len(queries),
    )

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                retriever.search,
                q.query_string,
                q.topic,
                cfg.year_from,
                cfg.year_to,
                cfg.max_results_per_query,
            ): (retriever.source_name, q.query_string)
            for q in queries
            for retriever in retrievers
            if retriever.source_name != "arxiv" or q.query_string in arxiv_allowed
        }
        for future in as_completed(futures):
            source, query = futures[future]
            try:
                all_papers.extend(future.result())
            except Exception as exc:
                logger.warning("Retriever %s failed for query %r: %s", source, query, exc)

    return all_papers


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()
    try:
        if args.mode == "fetch":
            run_fetch(args)
        elif args.mode == "analyze":
            run_analyze(args)
        else:
            run_pipeline(args)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        sys.exit(1)
