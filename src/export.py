"""
Exporter.

Writes output files for each pipeline run into a dedicated folder:

  <output_dir>/<topic-slug>_<date>/
      papers_ranked.xlsx   — rich Excel workbook (two sheets)
      papers_ranked.csv    — plain CSV for scripting / pandas
      paper_summaries.jsonl — one JSON object per summarised paper
      survey_report.md     — Markdown report grouped by topic

The run folder name is derived from the configured topics + today's date so
each run is stored separately and old results are never overwritten.

Excel workbook sheets:
  "Ranked Papers"  — all scored papers, one row each, sortable columns
  "Summaries"      — top-N papers that received LLM summaries, with full
                     findings, scope, taxonomy etc. in readable columns
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import subprocess
from collections import defaultdict
from datetime import date
from pathlib import Path

from .models import (
    ConceptGraph,
    FieldGuide,
    FieldMegaArchitecture,
    JudgeResult,
    LandmarkPaper,
    Paper,
    PaperArchitecture,
    PaperSummary,
    ReadingPath,
    ScoredPaper,
)

logger = logging.getLogger(__name__)

# Columns written to both the CSV and the "Ranked Papers" XLSX sheet
_PAPER_COLUMNS = [
    "rank",
    "title",
    "year",
    "venue",
    "authors",
    "topic_queries",
    "citation_count",
    "influential_citation_count",
    "background_citation_count",
    "influential_ratio",
    "canonical_score",
    "authority_tier",
    "llm_authority_assessment",
    "recommended_action",
    "orientation",
    "quality_score",
    "doi",
    "arxiv_id",
    "url",
    "pdf_url",
    "abstract",
    "sources",
]

# Columns written to the "Summaries" XLSX sheet
_SUMMARY_COLUMNS = [
    "rank",
    "title",
    "year",
    "venue",
    "quality_score",
    "research_scope",
    "core_problem",
    "taxonomy",
    "main_methods",
    "main_findings",
    "limitations",
    "future_directions",
    "datasets_and_benchmarks",
    "url",
    "doi",
]


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_run_dir(topics: list[str], base_dir: Path) -> Path:
    """
    Return (and create) a uniquely-named subdirectory for this run.

    Name format: <topic-slug>[_<topic-slug>...]_YYYY-MM-DD[_N]
    e.g. "ai-computer-science-education_machine-learning_2026-05-20"

    If the directory already exists a counter suffix is appended so old runs
    are never overwritten.
    """
    slugs = [_topic_slug(t) for t in topics[:3]]   # max 3 topics in name
    if len(topics) > 3:
        slugs.append(f"and-{len(topics) - 3}-more")
    date_str = date.today().isoformat()
    stem = "_".join(slugs) + "_" + date_str

    folder = base_dir / stem
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # Append incrementing counter to avoid clobbering an earlier run today
    counter = 2
    while True:
        candidate = base_dir / f"{stem}_{counter}"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        counter += 1


def _topic_slug(topic: str) -> str:
    """'AI for computer science education' → 'ai-computer-science-education'"""
    _STOPWORDS = {"for", "in", "of", "the", "a", "an", "and", "or", "with", "on"}
    words = re.sub(r"[^\w\s]", "", topic.lower()).split()
    significant = [w for w in words if w not in _STOPWORDS][:4]
    return "-".join(significant) or "topic"


# ─────────────────────────────────────────────────────────────────────────────
# Exporter
# ─────────────────────────────────────────────────────────────────────────────

class Exporter:
    def __init__(self, output_dir: str | Path):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _topic_dir(self, topic: str) -> Path:
        """Return (and create) a per-topic sub-folder inside the run directory."""
        d = self._output_dir / _topic_slug(topic)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def export_xlsx(
        self,
        scored_papers: list[ScoredPaper],
        summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
    ) -> Path:
        """
        Write a two-sheet Excel workbook.

        Sheet 1 "Ranked Papers" — every scored paper, all columns.
        Sheet 2 "Summaries"     — only papers that have LLM summaries.
        """
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            logger.warning(
                "openpyxl not installed — skipping XLSX export. "
                "Run: pip install openpyxl"
            )
            return self._output_dir / "papers_ranked.xlsx"

        path = self._output_dir / "papers_ranked.xlsx"
        wb = openpyxl.Workbook()

        # ── Sheet 1: Ranked Papers ──────────────────────────────────────
        ws1 = wb.active
        ws1.title = "Ranked Papers"
        _write_sheet(
            ws1,
            headers=_PAPER_COLUMNS,
            rows=[_to_paper_row(rank, sp) for rank, sp in enumerate(scored_papers, 1)],
            col_widths={
                "rank": 6, "title": 60, "year": 8, "venue": 28,
                "authors": 36, "topic_queries": 28,
                "citation_count": 12, "influential_citation_count": 14,
                "background_citation_count": 14, "influential_ratio": 12,
                "canonical_score": 12, "authority_tier": 16,
                "llm_authority_assessment": 18, "recommended_action": 16,
                "orientation": 16,
                "quality_score": 12, "doi": 22, "arxiv_id": 20,
                "url": 32, "pdf_url": 32, "abstract": 70, "sources": 14,
            },
            url_cols={"url", "pdf_url"},
        )

        # ── Sheet 2: Summaries ─────────────────────────────────────────
        if summary_pairs:
            ws2 = wb.create_sheet("Summaries")
            summary_map = {sp.paper.title: (sp, summary) for sp, summary in summary_pairs}
            summary_rows = []
            rank = 0
            for sp in scored_papers:
                if sp.paper.title in summary_map:
                    rank += 1
                    _, summary = summary_map[sp.paper.title]
                    summary_rows.append(_to_summary_row(rank, sp, summary))
            _write_sheet(
                ws2,
                headers=_SUMMARY_COLUMNS,
                rows=summary_rows,
                col_widths={
                    "rank": 6, "title": 55, "year": 8, "venue": 24,
                    "quality_score": 12, "research_scope": 40,
                    "core_problem": 40, "taxonomy": 40,
                    "main_methods": 40, "main_findings": 60,
                    "limitations": 40, "future_directions": 40,
                    "datasets_and_benchmarks": 36, "url": 32, "doi": 22,
                },
                url_cols={"url"},
            )

        wb.save(path)
        logger.info("XLSX exported: %s (%d papers, %d summaries)",
                    path, len(scored_papers), len(summary_pairs))
        return path

    def export_csv(
        self,
        scored_papers: list[ScoredPaper],
        judge_map: dict[str, JudgeResult] | None = None,
        arch_map: dict[str, PaperArchitecture] | None = None,
    ) -> Path:
        """Write all ranked papers to CSV."""
        path = self._output_dir / "papers_ranked.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_PAPER_COLUMNS)
            writer.writeheader()
            for rank, sp in enumerate(scored_papers, start=1):
                jr = (judge_map or {}).get(sp.paper.title)
                arch = (arch_map or {}).get(sp.paper.title)
                writer.writerow(_to_paper_row(rank, sp, jr, arch))
        logger.info("CSV exported: %s (%d papers)", path, len(scored_papers))
        return path

    def export_jsonl(
        self,
        summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
        judge_map: dict[str, JudgeResult] | None = None,
    ) -> Path:
        """Write one JSON object per summarised paper to JSONL."""
        path = self._output_dir / "paper_summaries.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for sp, summary in summary_pairs:
                p = sp.paper
                total = max(p.citation_count, 1)
                jr = (judge_map or {}).get(p.title)
                record = {
                    "title": p.title,
                    "year": p.year,
                    "venue": p.venue,
                    "quality_score": sp.quality_score,
                    "doi": p.doi,
                    "arxiv_id": p.arxiv_id,
                    "url": p.url,
                    "citation_count": p.citation_count,
                    "influential_citation_count": p.influential_citation_count,
                    "background_citation_count": p.background_citation_count,
                    "influential_ratio": round(p.influential_citation_count / total, 4),
                    "canonical_score": p.canonical_score,
                    "authority_tier": p.authority_tier,
                    "topic_queries": p.topic_queries,
                    "sources": p.sources,
                    "judge": jr.to_flat_dict() if jr else None,
                    "summary": summary.to_flat_dict(),
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("JSONL exported: %s (%d papers)", path, len(summary_pairs))
        return path

    def export_architecture_report(
        self,
        topic: str,
        arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
        mega: FieldMegaArchitecture,
        judge_map: dict[str, JudgeResult] | None = None,
        reading_path: "ReadingPath | None" = None,
        concept_graph: "ConceptGraph | None" = None,
        landmarks: "list[LandmarkPaper] | None" = None,
    ) -> Path:
        """
        Write the full architecture report for one topic to its sub-folder.

        Part 1 — Field Architecture (mega-arch, Mermaid, gaps)
        Part 2 — Survey Navigator   (orientation map, coverage matrix, reading path)
        Part 3 — Concept Graph      (node/edge listing; only when concept_graph is provided)
        Part 4 — Paper Cards        (one card per paper, with anchor IDs)

        All cross-references inside the file use Markdown anchor links so the
        user can click between sections in any Markdown viewer.
        """
        path = self._topic_dir(topic) / "report.md"

        show_concept_graph = bool(concept_graph and not concept_graph.extraction_failed)
        lines: list[str] = []
        lines += _render_part1_field_architecture(
            topic, mega, arch_triples,
            has_landmarks=bool(landmarks),
            has_concept_graph=show_concept_graph,
        )
        lines += _render_part2_survey_navigator(topic, arch_triples, mega, reading_path)
        if landmarks:
            lines += _render_landmark_papers(landmarks)
        if show_concept_graph:
            lines += _render_part3_concept_graph(concept_graph)
        lines += _render_part4_paper_cards(arch_triples, judge_map)

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Architecture report exported: %s (%d papers)", path, len(arch_triples))
        return path

    def export_concept_graph_json(
        self,
        topic: str,
        graph: "ConceptGraph",
    ) -> "Path | None":
        """Write ConceptGraph as JSON.  Returns None if extraction failed."""
        if graph.extraction_failed:
            logger.warning(
                "Skipping concept graph JSON for '%s' — extraction failed: %s",
                topic, graph.failure_reason,
            )
            return None
        path = self._topic_dir(topic) / "concept_graph.json"
        path.write_text(
            json.dumps(graph.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Concept graph JSON exported: %s (%d nodes, %d edges)", path, len(graph.nodes), len(graph.edges))
        return path

    def export_reading_path_json(
        self,
        topic: str,
        reading_path: "ReadingPath",
    ) -> "Path | None":
        """Write ReadingPath as JSON.  Returns None if generation failed."""
        if reading_path.generation_failed:
            logger.warning(
                "Skipping reading path JSON for '%s' — generation failed: %s",
                topic, reading_path.failure_reason,
            )
            return None
        path = self._topic_dir(topic) / "reading_path.json"
        path.write_text(
            json.dumps(reading_path.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Reading path JSON exported: %s (%d steps)", path, len(reading_path.steps))
        return path

    def export_mindmap_html(
        self,
        topic: str,
        mega: FieldMegaArchitecture,
        arch_triples: "list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]] | None" = None,
    ) -> "Path | None":
        """
        Write a self-contained interactive HTML mind map using markmap.

        Opens in any browser — no installation required.  Each node is enriched
        with bold labels, `coverage` badges, italic definitions, technique lists,
        and clickable links to the original papers.  Click any node to expand /
        collapse its children; use the toolbar to reset view or expand all.

        Returns None if synthesis failed.
        """
        if mega.synthesis_failed:
            return None
        path = self._topic_dir(topic) / "mindmap.html"
        markdown = _build_mindmap_markdown(topic, mega, arch_triples or [])
        html = _MINDMAP_HTML_TEMPLATE.format(topic=topic, markdown=markdown)
        path.write_text(html, encoding="utf-8")
        logger.info("Interactive mind map exported: %s", path)
        return path

    def export_mega_architecture_json(
        self,
        topic: str,
        mega: FieldMegaArchitecture,
    ) -> Path | None:
        """Write FieldMegaArchitecture as JSON for programmatic consumption.
        Returns None and skips the file if synthesis failed."""
        if mega.synthesis_failed:
            logger.warning(
                "Skipping JSON export for '%s' — synthesis failed: %s",
                topic, mega.failure_reason,
            )
            return None
        path = self._topic_dir(topic) / "architecture.json"
        path.write_text(
            json.dumps(mega.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Architecture JSON exported: %s", path)
        return path

    def export_mega_architecture_mmd(
        self,
        topic: str,
        mega: FieldMegaArchitecture,
    ) -> Path | None:
        """Write the Mermaid diagram as a standalone .mmd file.
        Returns None and skips the file if synthesis failed or diagram is empty."""
        if mega.synthesis_failed or not mega.mermaid_diagram:
            logger.warning(
                "Skipping MMD export for '%s' — synthesis failed or no diagram.",
                topic,
            )
            return None
        topic_dir = self._topic_dir(topic)
        mmd_path = topic_dir / "architecture.mmd"
        mmd_path.write_text(mega.mermaid_diagram, encoding="utf-8")
        logger.info("Mermaid diagram exported: %s", mmd_path)

        png_path = topic_dir / "architecture.png"
        _render_mermaid_png(mmd_path, png_path)
        return mmd_path

    def export_markdown(
        self,
        scored_papers: list[ScoredPaper],
        summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
    ) -> Path:
        """Write a Markdown report grouped by topic."""
        path = self._output_dir / "survey_report.md"

        summary_map: dict[str, PaperSummary] = {
            sp.paper.title: summary for sp, summary in summary_pairs
        }

        by_topic: dict[str, list[ScoredPaper]] = defaultdict(list)
        for sp in scored_papers:
            topic = sp.paper.topic_queries[0] if sp.paper.topic_queries else "Uncategorised"
            by_topic[topic].append(sp)

        lines: list[str] = [
            "# AI Survey Paper Report",
            "",
            f"*Generated by AI Survey Paper Miner — {len(scored_papers)} papers across "
            f"{len(by_topic)} topics*",
            "",
            "---",
            "",
        ]

        for topic, papers in sorted(by_topic.items()):
            lines.append(f"## {topic.title()}")
            lines.append("")
            lines.append(f"*{len(papers)} papers — ranked by quality score*")
            lines.append("")
            for rank, sp in enumerate(papers, start=1):
                lines += _paper_section(rank, sp, summary_map.get(sp.paper.title))

        with path.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info("Markdown report exported: %s", path)
        return path


# ─────────────────────────────────────────────────────────────────────────────
# XLSX helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write_sheet(ws, headers, rows, col_widths, url_cols=None):
    """Write headers + rows to a worksheet with formatting."""
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    url_cols = url_cols or set()
    HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
    HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
    WRAP = Alignment(wrap_text=True, vertical="top")
    TOP = Alignment(vertical="top")

    # Write headers
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header.replace("_", " ").title())
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = TOP

    ws.freeze_panes = "A2"

    # Write data rows
    for row_idx, row_dict in enumerate(rows, 2):
        for col_idx, header in enumerate(headers, 1):
            value = row_dict.get(header, "")
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            # Wrap text for long fields; top-align everything
            if header in {"abstract", "main_findings", "taxonomy",
                          "main_methods", "limitations", "future_directions"}:
                cell.alignment = WRAP
            else:
                cell.alignment = TOP
            # Add hyperlink for URL columns
            if header in url_cols and value and str(value).startswith("http"):
                cell.hyperlink = str(value)
                cell.font = Font(color="0563C1", underline="single")

    # Set column widths
    for col_idx, header in enumerate(headers, 1):
        width = col_widths.get(header, 20)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Taller default row height for wrapped rows
    for row_idx in range(2, len(rows) + 2):
        ws.row_dimensions[row_idx].height = 60


# ─────────────────────────────────────────────────────────────────────────────
# Row builders
# ─────────────────────────────────────────────────────────────────────────────

def _to_paper_row(
    rank: int,
    sp: ScoredPaper,
    jr: "JudgeResult | None" = None,
    arch: "PaperArchitecture | None" = None,
) -> dict:
    p = sp.paper
    total = max(p.citation_count, 1)
    return {
        "rank": rank,
        "title": p.title,
        "year": p.year or "",
        "venue": p.venue or "",
        "authors": "; ".join(p.authors[:5]),
        "topic_queries": "; ".join(p.topic_queries),
        "citation_count": p.citation_count,
        "influential_citation_count": p.influential_citation_count,
        "background_citation_count": p.background_citation_count,
        "influential_ratio": round(p.influential_citation_count / total, 4),
        "canonical_score": round(p.canonical_score, 4),
        "authority_tier": p.authority_tier or "",
        "llm_authority_assessment": jr.authority_assessment if jr and not jr.judge_failed else "",
        "recommended_action": jr.recommended_action if jr and not jr.judge_failed else "",
        "orientation": arch.orientation if arch and not arch.analysis_failed else "",
        "quality_score": round(sp.quality_score, 1),
        "doi": p.doi or "",
        "arxiv_id": p.arxiv_id or "",
        "url": p.url or "",
        "pdf_url": p.pdf_url or "",
        "abstract": (p.abstract or "").replace("\n", " "),
        "sources": "; ".join(p.sources),
    }


def _to_summary_row(rank: int, sp: ScoredPaper, summary: PaperSummary) -> dict:
    p = sp.paper
    return {
        "rank": rank,
        "title": p.title,
        "year": p.year or "",
        "venue": p.venue or "",
        "quality_score": round(sp.quality_score, 1),
        "research_scope": summary.research_scope or "",
        "core_problem": summary.core_problem or "",
        "taxonomy": "\n".join(f"• {t}" for t in summary.taxonomy),
        "main_methods": "\n".join(f"• {m}" for m in summary.main_methods),
        "main_findings": "\n".join(f"• {f}" for f in summary.main_findings),
        "limitations": "\n".join(f"• {l}" for l in summary.limitations),
        "future_directions": "\n".join(f"• {d}" for d in summary.future_directions),
        "datasets_and_benchmarks": "; ".join(summary.datasets_and_benchmarks),
        "url": p.url or "",
        "doi": p.doi or "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Markdown helpers
# ─────────────────────────────────────────────────────────────────────────────

def _paper_section(rank: int, sp: ScoredPaper, summary: PaperSummary | None) -> list[str]:
    p = sp.paper
    lines: list[str] = []

    lines.append(f"### {rank}. {p.title}")
    lines.append("")
    lines.append(f"- **Year:** {p.year or 'N/A'}")
    lines.append(f"- **Venue:** {p.venue or 'N/A'}")
    lines.append(f"- **Quality score:** {sp.quality_score}")
    lines.append(f"- **Citations:** {p.citation_count}")

    if p.url:
        lines.append(f"- **URL:** {p.url}")
    if p.arxiv_id:
        lines.append(f"- **arXiv:** https://arxiv.org/abs/{p.arxiv_id}")

    if summary and not summary.summarization_failed:
        if summary.research_scope:
            lines.append(f"- **Scope:** {summary.research_scope}")
        if summary.core_problem:
            lines.append(f"- **Core problem:** {summary.core_problem}")
        if summary.taxonomy:
            lines.append(f"- **Taxonomy:** {', '.join(summary.taxonomy[:5])}")
        if summary.main_findings:
            lines.append("- **Key findings:**")
            for finding in summary.main_findings[:3]:
                lines.append(f"  - {finding}")
        if summary.datasets_and_benchmarks:
            lines.append(f"- **Benchmarks:** {', '.join(summary.datasets_and_benchmarks[:5])}")
        all_keywords = []
        for kw_list in summary.keywords.values():
            all_keywords.extend(kw_list[:3])
        if all_keywords:
            lines.append(f"- **Keywords:** {', '.join(all_keywords[:10])}")
    elif summary and summary.summarization_failed:
        lines.append(f"- *Summary unavailable: {summary.failure_reason}*")

    lines.append("")
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Architecture report helpers  (three-part per-topic Markdown)
# ─────────────────────────────────────────────────────────────────────────────

def _anchor(text: str) -> str:
    """Convert text to a GitHub-Flavoured-Markdown anchor fragment."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s]+", "-", slug).strip("-")
    return slug


def _paper_anchor(sp: ScoredPaper) -> str:
    """Stable anchor id for a paper card (first 6 words of title + year)."""
    words = re.sub(r"[^\w\s]", "", sp.paper.title.lower()).split()[:6]
    slug = "-".join(words)
    year = str(sp.paper.year or "nd")
    return f"{slug}-{year}"


def _render_field_outline(mega: FieldMegaArchitecture) -> list[str]:
    """
    Field Map as a directory-style nested outline (instead of a Mermaid
    mind-map). Reads the same structured mega-architecture fields, so it stays
    consistent with the report's tables, but renders as plain nested bullets
    that are readable in ANY viewer without diagram support.
    """
    n = len(mega.source_papers)
    low = max(1, round(n * 0.3))
    out: list[str] = []

    def cov(info: object) -> str:
        if isinstance(info, dict) and isinstance(info.get("coverage_count"), int):
            c = info["coverage_count"]
            return f"  ·  {c}/{n} surveys" + (" ⚠️" if c < low else "")
        return ""

    if mega.major_tasks:
        out.append("- **Major Tasks**")
        for name, info in list(mega.major_tasks.items())[:8]:
            out.append(f"  - {name}{cov(info)}")

    if mega.method_families:
        out.append("- **Method Families**")
        for name, info in list(mega.method_families.items())[:8]:
            reps = ""
            if isinstance(info, dict):
                rm = [str(x) for x in (info.get("representative_methods") or [])][:4]
                if rm:
                    reps = f" — {', '.join(rm)}"
            out.append(f"  - {name}{reps}{cov(info)}")

    benches = [d.get("name", "") for d in mega.datasets_and_benchmarks if d.get("name")]
    if benches:
        out.append("- **Benchmarks & Datasets**")
        for b in benches[:8]:
            out.append(f"  - {b}")

    if mega.challenges:
        out.append("- **Challenges**")
        for name, info in list(mega.challenges.items())[:8]:
            sev = ""
            if isinstance(info, dict) and str(info.get("severity", "")).strip():
                sev = f" `{str(info['severity']).strip()}`"
            out.append(f"  - {name}{sev}{cov(info)}")

    if mega.open_gaps:
        out.append("- **Research Gaps**")
        for g in mega.open_gaps[:6]:
            out.append(f"  - {g.gap}")

    if mega.applications:
        out.append("- **Applications**")
        for a in mega.applications[:8]:
            out.append(f"  - {a}")

    return out


def _render_part1_field_architecture(
    topic: str,
    mega: FieldMegaArchitecture,
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    has_landmarks: bool = False,
    has_concept_graph: bool = False,
) -> list[str]:
    n = len(mega.source_papers)
    n_gaps = len(mega.open_gaps)
    today = date.today().isoformat()

    lines: list[str] = [
        f"# {topic.title()} — Survey Report",
        f"*Generated {today} · {n} surveys analysed · {n_gaps} research gaps identified*",
        "",
        "---",
        "",
        "## Contents",
        "",
        "- [Part 1 — Field Architecture](#part-1--field-architecture)",
        "  - [Field at a Glance](#field-at-a-glance)",
        "  - [Field Map](#field-map)",
        "  - [Core Problems](#core-problems)",
        "  - [Research Landscape](#research-landscape)",
        "  - [Research Gaps](#research-gaps)",
        "- [Part 2 — Survey Navigator](#part-2--survey-navigator)",
        "  - [Reading Guide](#reading-guide-where-to-start)",
        *(["- [Landmark Papers](#landmark-papers)"] if has_landmarks else []),
        *([
            "- [Part 3 — Concept Graph](#part-3--concept-graph)",
            "  - [Concepts](#concepts)",
            "  - [Concept Map](#concept-map)",
            "  - [How Concepts Relate](#how-concepts-relate)",
        ] if has_concept_graph else []),
        "- [Part 4 — Paper Cards](#part-4--paper-cards)",
        "",
        "---",
        "",
        "## Part 1 — Field Architecture",
        "",
        "### Field at a Glance",
        "",
    ]

    if mega.field_summary:
        lines.append(mega.field_summary)
        lines.append("")

    # Deepest / weakest coverage bullets derived from coverage_count
    all_methods = list(mega.method_families.items())
    if all_methods:
        best = sorted(all_methods, key=lambda kv: kv[1].get("coverage_count", 0), reverse=True)
        worst = [kv for kv in best if kv[1].get("coverage_count", 0) < max(1, round(n * 0.3))]
        if best:
            lines.append(f"**Deepest coverage:** {' · '.join(k for k, _ in best[:3])}")
        if worst:
            lines.append(f"**Weakest coverage:** {' · '.join(k for k, _ in worst[:3])}")

    cmp = mega.cross_survey_comparison
    if cmp and not cmp.comparison_failed and cmp.conflicting_classifications:
        conflict = cmp.conflicting_classifications[0]
        lines.append(
            f"**Most contested concept:** {conflict.get('dimension', '')} "
            f"({conflict.get('paper_a', '')} vs {conflict.get('paper_b', '')})"
        )
    lines.append("")

    # Field Map — directory-style outline (readable in any viewer, no Mermaid)
    lines += ["---", "", "### Field Map", ""]
    lines += _render_field_outline(mega)
    lines += [
        "",
        "> ⚠️ marks items covered by fewer than 30% of analysed surveys — likely research gaps.",
        "",
        "---",
        "",
        "### Core Problems",
        "",
    ]

    if mega.core_problems:
        lines.append("| Problem | Surveys covering it | Best paper |")
        lines.append("|---|---|---|")
        for cp in mega.core_problems:
            prob = cp.get("problem", "")
            cnt = cp.get("coverage_count", "—")
            best_p = cp.get("best_paper", "—")
            best_link = _paper_ref(best_p, arch_triples)
            coverage = f"{cnt} / {n}" if isinstance(cnt, int) else str(cnt)
            lines.append(f"| {prob} | {coverage} | {best_link} |")
    else:
        lines.append("*No core problems extracted.*")
    lines.append("")

    # Merged Research Landscape (mainstream areas + methods + datasets + challenges)
    lines += _render_research_landscape(mega, arch_triples)

    # Research gaps
    lines += ["---", "", "### Research Gaps", ""]
    if mega.open_gaps:
        for i, gap in enumerate(mega.open_gaps, 1):
            score_str = f"{gap.opportunity_score:.2f}" if gap.opportunity_score else "—"
            lines.append(
                f"**Gap {i} — {gap.gap}** *(opportunity score: {score_str})*"
            )
            lines.append("")
            if gap.gap_type:
                lines.append(f"**Type:** {gap.gap_type}")
            if gap.evidence:
                evidence_links = [_paper_ref(title, arch_triples) for title in gap.evidence]
                lines.append(f"**Evidence:** {' · '.join(evidence_links)}")
            lines.append("")
            lines.append("---")
            lines.append("")
    else:
        lines.append("*No research gaps identified.*")
        lines.append("")

    return lines


# Generic words ignored when matching a research-area name to papers.
_AREA_STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "for", "in", "on", "to", "with",
    "based", "using", "systems", "system", "general", "other", "via",
    "approaches", "approach", "methods", "method",
}


def _area_tokens(name: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", name.lower())
        if w not in _AREA_STOPWORDS and len(w) > 2
    }


def _papers_for_area(
    area_name: str,
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    max_papers: int = 6,
) -> list[str]:
    """
    Return Markdown links to the papers most relevant to a research area.

    Robustly matches by word overlap between the area name and the UNION of
    each paper's covered tasks/applications/challenges, its taxonomy, and its
    summary taxonomy — so an area gets its key papers even when the exact
    phrasing differs (the old strict substring match left many areas empty).
    Ranked by overlap strength, then citation count.
    """
    tokens = _area_tokens(area_name)
    if not tokens:
        return []

    scored: list[tuple[int, int, ScoredPaper]] = []
    for sp, summary, arch in arch_triples:
        if arch.analysis_failed:
            continue
        haystack_parts = (
            list(arch.covered_tasks)
            + list(arch.covered_applications)
            + list(arch.covered_challenges)
            + list(arch.top_level_taxonomy)
        )
        if summary and not summary.summarization_failed:
            haystack_parts += list(summary.taxonomy)
        hay_tokens = set(re.findall(r"[a-z0-9]+", " ".join(haystack_parts).lower()))
        overlap = len(tokens & hay_tokens)
        if overlap >= 1:
            scored.append((overlap, sp.paper.citation_count, sp))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    links: list[str] = []
    for _, _, sp in scored[:max_papers]:
        a = _paper_anchor(sp)
        cit = f" ({sp.paper.citation_count:,}✱)" if sp.paper.citation_count else ""
        links.append(f"[{sp.paper.title}](#{a}){cit}")
    return links


def _render_research_landscape(
    mega: FieldMegaArchitecture,
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
) -> list[str]:
    """
    Merged section: Mainstream Research Areas + Methods + Datasets + Challenges.

    For each method family, links to the actual papers that use it (with
    citation counts and paper-card anchors) so readers can navigate directly
    to the relevant literature.
    """
    n = len(mega.source_papers)
    low_threshold = max(1, round(n * 0.3))

    lines: list[str] = [
        "---",
        "",
        "### Research Landscape",
        "",
        "*Mainstream research areas, methods in use, relevant benchmarks, and open challenges — "
        "all cross-referenced to the surveyed papers below.*",
        "",
    ]

    # ── 1. Mainstream research areas ────────────────────────────────────
    if mega.major_tasks:
        lines += ["#### Mainstream Research Areas", ""]
        lines.append("| Research Area | What it studies | Surveys | Key Papers |")
        lines.append("|---|---|---|---|")
        for task_name, info in mega.major_tasks.items():
            if not isinstance(info, dict):
                continue
            desc = str(info.get("description", "—"))
            cnt = info.get("coverage_count", "—")
            coverage = f"{cnt} / {n}" if isinstance(cnt, int) else str(cnt)
            warn = " ⚠️" if isinstance(cnt, int) and cnt < low_threshold else ""

            # Papers covering this area — robust word-overlap match across all
            # of each paper's covered fields + taxonomy (not just covered_tasks),
            # so areas no longer come up empty just because of phrasing.
            paper_links = _papers_for_area(task_name, arch_triples, max_papers=6)
            # One paper per line inside the cell (<br> renders as a line break)
            papers_str = "<br>".join(paper_links) if paper_links else "—"
            safe_desc = desc.replace("|", "\\|")
            lines.append(f"| **{task_name}{warn}** | {safe_desc} | {coverage} | {papers_str} |")
        lines.append("")

    # ── 2. Mainstream methods ────────────────────────────────────────────
    if mega.method_families:
        lines += ["#### Mainstream Methods", ""]
        lines.append(
            "*For each method: what it is, which benchmarks evaluate it, "
            "and which papers use it (citation count in parentheses).*"
        )
        lines.append("")

        for fam_name, info in mega.method_families.items():
            if not isinstance(info, dict):
                continue
            desc = str(info.get("description", ""))
            rep_methods: list[str] = [str(x) for x in (info.get("representative_methods") or [])]
            cnt = info.get("coverage_count", "—")
            coverage = f"{cnt} / {n}" if isinstance(cnt, int) else str(cnt)
            warn = " ⚠️" if isinstance(cnt, int) and cnt < low_threshold else ""

            fam_lower = fam_name.lower()
            rep_lower = {r.lower() for r in rep_methods[:5]}

            # Papers that use this method family (match against arch.covered_methods)
            papers_using: list[str] = []
            for sp, _, arch in arch_triples:
                if arch.analysis_failed:
                    continue
                covered_str = " ".join(arch.covered_methods).lower()
                if fam_lower in covered_str or any(r in covered_str for r in rep_lower):
                    a = _paper_anchor(sp)
                    p = sp.paper
                    year_str = str(p.year) if p.year else "n.d."
                    cit_str = f"{p.citation_count:,} citations" if p.citation_count else "no citation data"
                    label = f"{p.title} ({year_str}, {cit_str})"
                    papers_using.append(f"[{label}](#{a})")

            # Render as a compact definition-style block
            lines.append(f"**{fam_name}{warn}** · *{coverage} surveys*  ")
            if desc:
                lines.append(f"> {desc}  ")
            if rep_methods:
                lines.append(f"Representative techniques: {', '.join(rep_methods[:6])}  ")
            if papers_using:
                lines.append("Papers using this approach:")
                for ref in papers_using[:6]:
                    lines.append(f"- {ref}")
            lines.append("")

    # ── 3. Key benchmarks & datasets ────────────────────────────────────
    if mega.datasets_and_benchmarks:
        lines += ["#### Key Benchmarks & Datasets", ""]
        lines.append("| Benchmark / Dataset | Research area | Surveys citing it |")
        lines.append("|---|---|---|")
        for ds in mega.datasets_and_benchmarks:
            name = ds.get("name", "")
            task = ds.get("task", "—")
            cnt = ds.get("coverage_count", "—")
            coverage = f"{cnt} / {n}" if isinstance(cnt, int) else str(cnt)
            warn = " ⚠️" if isinstance(cnt, int) and cnt < low_threshold else ""
            lines.append(f"| **{name}**{warn} | {task} | {coverage} |")
        lines.append("")

    # ── 4. Open challenges ───────────────────────────────────────────────
    if mega.challenges:
        lines += ["#### Open Challenges", ""]
        lines.append("| Challenge | Severity | Surveys | Description |")
        lines.append("|---|---|---|---|")
        for name, info in mega.challenges.items():
            if not isinstance(info, dict):
                continue
            sev = str(info.get("severity", "—"))
            cnt = info.get("coverage_count", "—")
            desc = str(info.get("description", "—"))
            coverage = f"{cnt} / {n}" if isinstance(cnt, int) else str(cnt)
            safe_desc = desc.replace("|", "\\|")
            lines.append(f"| **{name}** | {sev} | {coverage} | {safe_desc} |")
        lines.append("")

    return lines


def _render_part2_survey_navigator(
    topic: str,
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    mega: FieldMegaArchitecture,
    reading_path: "ReadingPath | None" = None,
) -> list[str]:
    cmp = mega.cross_survey_comparison
    lines: list[str] = [
        "---",
        "",
        "## Part 2 — Survey Navigator",
        "",
    ]

    # Complementary coverage from comparison
    if cmp and not cmp.comparison_failed and cmp.complementary_coverage:
        lines += ["---", "", "### Reading Guide: Where to Start", ""]
        lines.append("| Goal | Best paper |")
        lines.append("|---|---|")
        for item in cmp.complementary_coverage[:8]:
            aspect = item.get("aspect", "")
            best = item.get("best_covered_by", "")
            lines.append(f"| {aspect} | {_paper_ref(best, arch_triples)} |")
        if cmp.best_overall_structure:
            lines.append(
                f"| Best overall structure | "
                f"{_paper_ref(cmp.best_overall_structure, arch_triples)} |"
            )
        lines.append("")

    # Reading path (LLM-generated sequenced reading plan)
    if reading_path and not reading_path.generation_failed and reading_path.steps:
        lines += ["---", "", "### Sequenced Reading Path", ""]
        if reading_path.target_audience:
            lines.append(f"*For: {reading_path.target_audience}*")
            lines.append("")
        lines.append("| Step | Paper | Why | Focus | Est. time |")
        lines.append("|---|---|---|---|---|")
        for step in reading_path.steps:
            # Full real title + link to the paper card (falls back to the
            # step's own title text, un-truncated, if no confident match)
            title_link = _paper_ref(step.paper_title, arch_triples)
            focus = ", ".join(step.focus_sections[:3]) if step.focus_sections else "—"
            time_str = step.estimated_reading_time or "—"
            rationale = step.rationale.replace("|", "\\|")
            lines.append(f"| {step.step} | {title_link} | {rationale} | {focus} | {time_str} |")
        lines.append("")

    return lines


def _render_part4_paper_cards(
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
    judge_map: "dict[str, JudgeResult] | None" = None,
) -> list[str]:
    lines: list[str] = [
        "---",
        "",
        "## Part 4 — Paper Cards",
        "",
    ]

    for sp, summary, arch in arch_triples:
        p = sp.paper
        anchor = _paper_anchor(sp)
        jr = (judge_map or {}).get(p.title)

        lines += ["---", ""]
        lines.append(f'<a id="{anchor}"></a>')
        lines.append("")

        # Title line with metadata badges
        lines.append(f"### {p.title}")
        meta_parts = []
        if p.year:
            meta_parts.append(str(p.year))
        if p.venue:
            meta_parts.append(p.venue)
        meta_parts.append(f"Quality {sp.quality_score:.0f} / 100")
        if p.authority_tier:
            tier_badge = {"foundational": "🏛", "current_standard": "📌", "emerging": "🌱"}.get(
                p.authority_tier, ""
            )
            meta_parts.append(f"{tier_badge} {p.authority_tier}")
        if arch.orientation and not arch.analysis_failed:
            meta_parts.append(f"*{arch.orientation}-oriented*")
        lines.append(f"**{'  ·  '.join(meta_parts)}**")
        lines.append("")

        # URL and back-link
        url = p.url or (f"https://arxiv.org/abs/{p.arxiv_id}" if p.arxiv_id else "")
        if url:
            lines.append(f"[Paper URL]({url})  ·  [Back to Contents](#contents)")
        else:
            lines.append("[Back to Contents](#contents)")
        lines.append("")

        # One-line takeaway from summary
        if summary and not summary.summarization_failed and summary.core_problem:
            lines.append(f"> **One-line takeaway:** {summary.core_problem}")
            lines.append("")

        # Stats table
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        # OpenAlex doesn't expose an "influential citation" count, so it is
        # always 0 — only show it when a source actually provides one.
        if p.influential_citation_count:
            lines.append(
                f"| Citations | {p.citation_count} "
                f"(influential: {p.influential_citation_count}) |"
            )
        else:
            lines.append(f"| Citations | {p.citation_count} |")
        if jr and not jr.judge_failed:
            if jr.recommended_action:
                lines.append(f"| Recommended action | **{jr.recommended_action}** |")
            if jr.scope_clarity:
                lines.append(f"| Scope | {jr.scope_clarity} |")
            if jr.coverage_depth:
                lines.append(f"| Coverage depth | {jr.coverage_depth} |")
        if summary and not summary.summarization_failed and summary.taxonomy:
            lines.append(f"| Covers | {' · '.join(summary.taxonomy[:5])} |")
        lines.append("")

        # Taxonomy tree — rendered as a Mermaid figure (not text-art)
        if arch.top_level_taxonomy:
            lines.append("**How this survey organises the field:**")
            lines.append("")
            lines += _taxonomy_mermaid(
                root_label=_short_title(p.title),
                top_level=arch.top_level_taxonomy,
                second_level=arch.second_level_taxonomy,
                anchor=anchor,
            )
            lines.append("")

        # Organizational logic — formatted as scannable bullets, not a wall of text
        if arch.organizational_logic:
            lines.append("**How it organises the field:**")
            lines.append("")
            lines += _format_prose_as_bullets(arch.organizational_logic)
            lines.append("")

        # Structural strengths and weaknesses (from architecture analysis)
        if arch.structural_strengths and not arch.analysis_failed:
            lines.append(
                f"**Read this if:** {arch.structural_strengths[0]}"
            )
        if arch.notable_omissions and not arch.analysis_failed:
            lines.append(
                f"**Notable omissions:** {', '.join(arch.notable_omissions[:3])}"
            )

        # LLM-Judge strengths and weaknesses
        if jr and not jr.judge_failed:
            if jr.strengths:
                lines.append(f"**Strengths:** {' · '.join(jr.strengths[:3])}")
            if jr.weaknesses:
                lines.append(f"**Weaknesses:** {' · '.join(jr.weaknesses[:2])}")
        lines.append("")

    return lines


def _render_part0_field_guide(topic: str, guide: "FieldGuide") -> list[str]:
    """Render Part 0 — Beginner Field Guide."""
    from .field_guide import render_field_guide_markdown
    lines: list[str] = [
        f"# {topic.title()} — Survey Report",
        "",
        "---",
        "",
        "## Part 0 — Field Guide *(Start Here)*",
        "",
        "> This section is a plain-English introduction for newcomers to the field.",
        "",
        render_field_guide_markdown(guide),
        "---",
        "",
    ]
    return lines


# Plain-English metadata for each relationship type.
# (display order, readable verb for the diagram, sentence template, what it means)
_EDGE_META: dict[str, dict] = {
    "is_subfield_of": {
        "label": "is a subfield of",
        "heading": "Hierarchy — subfields",
        "explains": "The first concept is a more specialised area within the second.",
    },
    "part_of": {
        "label": "is part of",
        "heading": "Composition — components",
        "explains": "The first concept is a component or building block of the second.",
    },
    "uses": {
        "label": "uses",
        "heading": "Dependencies — what builds on what",
        "explains": "The first concept relies on or is built using the second.",
    },
    "applied_to": {
        "label": "is applied to",
        "heading": "Applications — method → task",
        "explains": "The first concept (a method) is used to tackle the second (a task or domain).",
    },
    "evaluated_by": {
        "label": "is evaluated by",
        "heading": "Evaluation — benchmarks & metrics",
        "explains": "The first concept is measured using the second (a benchmark or metric).",
    },
    "contrasts_with": {
        "label": "contrasts with",
        "heading": "Contrasts — competing approaches",
        "explains": "The two concepts are alternatives or stand in tension with each other.",
    },
    "emerged_after": {
        "label": "emerged after",
        "heading": "Timeline — what came later",
        "explains": "The first concept developed later than the second.",
    },
}
# Order relationship groups are presented in.
_EDGE_ORDER = [
    "is_subfield_of", "part_of", "uses", "applied_to",
    "evaluated_by", "contrasts_with", "emerged_after",
]


def _render_landmark_papers(landmarks: list["LandmarkPaper"]) -> list[str]:
    """
    Render the Landmark Papers section: the seminal *primary* works (not
    surveys) that the analysed surveys repeatedly build on.
    """
    lines: list[str] = [
        "---",
        "",
        "## Landmark Papers",
        "",
        "*Seminal primary papers (not surveys) that the analysed surveys most often "
        "build upon — read these for the original techniques.*",
        "",
        "| Paper | Year | Citations | Referenced by | Why it matters |",
        "|---|---|---:|---:|---|",
    ]
    for lm in landmarks:
        name = (lm.name or "").strip()
        short = _short_title(lm.title, 9) if lm.title else name
        # Avoid "Self-RAG — Self-RAG: ..." redundancy when the name is in the title
        if name and lm.title and name.lower() not in lm.title.lower():
            title_disp = f"{name} — {short}"
        else:
            title_disp = short or name
        link = f"[{title_disp}]({lm.url})" if lm.url else title_disp
        year = lm.year or "—"
        cites = f"{lm.citation_count:,}" if lm.citation_count else "—"
        ref = f"{lm.mentioned_by} surveys" if lm.mentioned_by else "—"
        why = (lm.why_seminal or "").replace("|", "\\|")
        lines.append(f"| {link} | {year} | {cites} | {ref} | {why} |")
    lines.append("")
    return lines


def _render_part3_concept_graph(graph: "ConceptGraph") -> list[str]:
    """Render Part 3 — Concept Graph: concepts table + map + readable relationships."""
    lines: list[str] = [
        "---",
        "",
        "## Part 3 — Concept Graph",
        "",
        f"*{len(graph.nodes)} concepts · {len(graph.edges)} typed relationships*",
        "",
    ]

    node_name_map = {n.node_id: n.name for n in graph.nodes}

    # ── Concepts table (Name + Definition only) ──────────────────────────
    if graph.nodes:
        lines += ["### Concepts", ""]
        lines.append("| Name | Definition |")
        lines.append("|---|---|")
        for node in graph.nodes:
            safe_def = (node.definition or "").replace("|", "\\|")
            lines.append(f"| **{node.name}** | {safe_def} |")
        lines.append("")

    # ── Concept map (visual overview) ─────────────────────────────────────
    if graph.edges:
        lines += ["### Concept Map", ""]
        lines.append(
            "Arrows are labelled with the relationship type. "
            "See *How concepts relate* below for the full list with evidence."
        )
        lines.append("")
        lines += _concept_graph_mermaid(graph, node_name_map)
        lines.append("")

    # ── Readable, grouped relationship list ───────────────────────────────
    if graph.edges:
        lines += ["### How concepts relate", ""]

        from collections import defaultdict as _dd
        by_type: dict[str, list] = _dd(list)
        for edge in graph.edges:
            by_type[edge.edge_type].append(edge)

        # Known types first (in defined order), then any unexpected types
        ordered_types = [t for t in _EDGE_ORDER if t in by_type]
        ordered_types += [t for t in sorted(by_type) if t not in _EDGE_META]

        for etype in ordered_types:
            edges = by_type[etype]
            meta = _EDGE_META.get(etype, {
                "label": etype.replace("_", " "),
                "heading": etype.replace("_", " ").title(),
                "explains": "",
            })
            lines.append(f"#### {meta['heading']}")
            if meta["explains"]:
                lines.append(f"*{meta['explains']}*")
            lines.append("")
            for e in edges:
                src = node_name_map.get(e.source_id, e.source_id)
                tgt = node_name_map.get(e.target_id, e.target_id)
                stmt = f"- **{src}** {meta['label']} **{tgt}**"
                if e.evidence:
                    ev = e.evidence.strip().rstrip(".")
                    stmt += f" — {ev}."
                lines.append(stmt)
            lines.append("")

    return lines


def _concept_graph_mermaid(
    graph: "ConceptGraph",
    node_name_map: dict[str, str],
    max_edges: int = 24,
) -> list[str]:
    """
    Render the concept graph as a Mermaid directed diagram with labelled edges.

    Only nodes that participate in a shown edge are drawn, so isolated
    concepts don't clutter the figure.  Capped at `max_edges` edges to keep
    the diagram legible; a note is added if edges were omitted.
    """
    edges = graph.edges[:max_edges]
    omitted = len(graph.edges) - len(edges)

    def nid(node_id: str) -> str:
        return "cg" + re.sub(r"[^a-zA-Z0-9]", "_", node_id)[:40]

    out: list[str] = ["```mermaid", "graph LR"]

    # Declare only the nodes that appear in a shown edge
    used_ids: list[str] = []
    seen: set[str] = set()
    for e in edges:
        for node_id in (e.source_id, e.target_id):
            if node_id not in seen:
                seen.add(node_id)
                used_ids.append(node_id)
    for node_id in used_ids:
        name = _mermaid_label(node_name_map.get(node_id, node_id))
        out.append(f'    {nid(node_id)}["{name}"]')

    # Edges with relationship labels
    for e in edges:
        meta = _EDGE_META.get(e.edge_type, {"label": e.edge_type.replace("_", " ")})
        label = _mermaid_label(meta["label"], max_len=22).replace('"', "")
        out.append(f"    {nid(e.source_id)} -->|{label}| {nid(e.target_id)}")

    out.append("```")
    if omitted > 0:
        out.append("")
        out.append(f"> Showing {len(edges)} of {len(graph.edges)} relationships "
                   f"({omitted} omitted for legibility — see the full list below).")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Interactive HTML mind map (markmap)
# ─────────────────────────────────────────────────────────────────────────────

# Self-contained HTML template.  markmap-autoloader from CDN does all the
# rendering — no build step, no local install.
# Only {topic} and {markdown} are format slots; all other braces are doubled.
_MINDMAP_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{topic} — Mind Map</title>
  <style>
    html, body {{
      margin: 0; padding: 0;
      width: 100%; height: 100%;
      overflow: hidden;
      background: #f0f4f8;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .mm-header {{
      position: fixed; top: 14px; left: 50%;
      transform: translateX(-50%);
      background: white;
      border-radius: 24px;
      padding: 7px 20px;
      box-shadow: 0 2px 14px rgba(0,0,0,0.12);
      display: flex; align-items: center; gap: 10px;
      z-index: 100;
    }}
    .mm-header h1 {{
      font-size: 0.88rem; font-weight: 700;
      color: #1a202c; white-space: nowrap; margin: 0;
    }}
    .mm-header .sep {{ color: #cbd5e0; }}
    .mm-header button {{
      font-size: 0.72rem; padding: 3px 11px;
      border: 1px solid #e2e8f0; border-radius: 10px;
      background: white; color: #4a5568; cursor: pointer;
      transition: all 0.15s;
    }}
    .mm-header button:hover {{ background: #ebf4ff; border-color: #90cdf4; color: #2b6cb0; }}
    .markmap {{ width: 100vw; height: 100vh; }}
    svg.markmap {{ width: 100%; height: 100%; }}
    /* Make paper-link nodes visually distinct */
    .markmap-node text a {{ text-decoration: underline; }}
  </style>
  <script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@0.16"></script>
</head>
<body>
  <div class="mm-header">
    <h1>{topic}</h1>
    <span class="sep">|</span>
    <button id="btn-fit">⤢ Fit</button>
    <button id="btn-expand">＋ Expand all</button>
    <button id="btn-collapse">－ Collapse all</button>
  </div>

  <div class="markmap">
    <script type="text/template">
---
markmap:
  colorFreezeLevel: 2
  initialExpandLevel: 2
  maxWidth: 360
  zoom: true
  pan: true
---
{markdown}
    </script>
  </div>

  <script>
    // Poll for the markmap SVG instance (autoloader is async)
    let mm = null;
    const poll = setInterval(() => {{
      const svg = document.querySelector("svg.markmap");
      if (svg && svg.__markmap) {{ mm = svg.__markmap; clearInterval(poll); }}
    }}, 80);

    document.getElementById("btn-fit").onclick = () => mm && mm.fit();

    function walkNodes(node, fn) {{
      fn(node);
      (node.children || []).forEach(c => walkNodes(c, fn));
    }}
    document.getElementById("btn-expand").onclick = () => {{
      if (!mm) return;
      walkNodes(mm.state.data, n => {{ n.payload = n.payload || {{}}; n.payload.fold = 0; }});
      mm.renderData(mm.state.data);
      mm.fit();
    }};
    document.getElementById("btn-collapse").onclick = () => {{
      if (!mm) return;
      // Collapse all but the root's direct children
      (mm.state.data.children || []).forEach(child => {{
        walkNodes(child, (n, depth) => {{
          if (n !== child) {{ n.payload = n.payload || {{}}; n.payload.fold = 1; }}
        }});
      }});
      mm.renderData(mm.state.data);
      mm.fit();
    }};

    // Open all links in a new tab
    document.addEventListener("click", e => {{
      const a = e.target.closest("a[href]");
      if (a && !a.href.startsWith("#")) {{
        e.preventDefault();
        window.open(a.href, "_blank", "noopener");
      }}
    }});
  </script>
</body>
</html>
"""


def _build_mindmap_markdown(
    topic: str,
    mega: FieldMegaArchitecture,
    arch_triples: "list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]]",
) -> str:
    """
    Build rich Markdown for markmap from the structured FieldMegaArchitecture.

    Node anatomy:
      - **Concept name** `N/M surveys`          ← bold label + coverage badge
        - *One-sentence definition*             ← italic description child
        - Techniques: `GPT-4` `BERT` `LLaMA`   ← code-span technique badges
        - [Paper title (year)](url) `120✱`      ← clickable citation children

    Nodes with coverage below 30% get a ⚠️ suffix.
    Paper links open in a new tab (handled by the HTML script).
    """
    n = len(mega.source_papers)
    low = max(1, round(n * 0.3))

    def cov(cnt) -> str:
        return f"`{cnt}/{n}`" if isinstance(cnt, int) else f"`{cnt}`"

    def warn(cnt) -> str:
        return " ⚠️" if isinstance(cnt, int) and cnt < low else ""

    def paper_link(sp: ScoredPaper) -> str:
        """One-line citation: linked title + year + citation badge."""
        p = sp.paper
        url = p.url or (f"https://arxiv.org/abs/{p.arxiv_id}" if p.arxiv_id else "")
        year = str(p.year) if p.year else "n.d."
        cit = f" `{p.citation_count:,}✱`" if p.citation_count else ""
        label = f"**{_short_title(p.title, 7)}** ({year})"
        return f"[{label}]({url}){cit}" if url else f"{label}{cit}"

    lines: list[str] = [f"# {topic}", ""]

    # ── Core Problems ────────────────────────────────────────────────────
    if mega.core_problems:
        lines += ["## ❓ Core Problems", ""]
        for cp in mega.core_problems:
            prob = str(cp.get("problem", "")).strip()
            cnt  = cp.get("coverage_count", "—")
            best = str(cp.get("best_paper", "")).strip()
            if not prob:
                continue
            lines.append(f"- **{_truncate_str(prob, 55)}** {cov(cnt)}")
            if best:
                a = _find_paper_anchor(best, arch_triples)
                lines.append(f"  - Best covered by: [{best}](#{a})" if a else f"  - Best covered by: {best}")
        lines.append("")

    # ── Research Areas (major tasks) ─────────────────────────────────────
    if mega.major_tasks:
        lines += ["## 📋 Research Areas", ""]
        for task, info in mega.major_tasks.items():
            if not isinstance(info, dict):
                continue
            cnt  = info.get("coverage_count", "—")
            desc = str(info.get("description", "")).strip()
            lines.append(f"- **{task}**{warn(cnt)} {cov(cnt)}")
            if desc:
                lines.append(f"  - *{desc}*")
            # Papers covering this task
            t_lower = task.lower()
            for sp, _, arch in arch_triples:
                if arch.analysis_failed:
                    continue
                if any(t_lower in t.lower() or t.lower() in t_lower
                       for t in arch.covered_tasks):
                    lines.append(f"  - {paper_link(sp)}")
        lines.append("")

    # ── Methods ──────────────────────────────────────────────────────────
    if mega.method_families:
        lines += ["## 🔧 Methods", ""]
        for fam, info in mega.method_families.items():
            if not isinstance(info, dict):
                continue
            cnt  = info.get("coverage_count", "—")
            desc = str(info.get("description", "")).strip()
            reps: list[str] = [str(x) for x in (info.get("representative_methods") or [])]
            lines.append(f"- **{fam}**{warn(cnt)} {cov(cnt)}")
            if desc:
                lines.append(f"  - *{desc}*")
            if reps:
                badges = " ".join(f"`{r}`" for r in reps[:6])
                lines.append(f"  - Techniques: {badges}")
            # Papers using this method
            f_lower = fam.lower()
            rep_lower = {r.lower() for r in reps[:5]}
            for sp, _, arch in arch_triples:
                if arch.analysis_failed:
                    continue
                covered = " ".join(arch.covered_methods).lower()
                if f_lower in covered or any(r in covered for r in rep_lower):
                    lines.append(f"  - {paper_link(sp)}")
        lines.append("")

    # ── Benchmarks & Datasets ────────────────────────────────────────────
    if mega.datasets_and_benchmarks:
        lines += ["## 📊 Benchmarks", ""]
        for ds in mega.datasets_and_benchmarks:
            name = str(ds.get("name", "")).strip()
            task = str(ds.get("task", "")).strip()
            cnt  = ds.get("coverage_count", "—")
            if not name:
                continue
            lines.append(f"- **{name}**{warn(cnt)} {cov(cnt)}")
            if task:
                lines.append(f"  - *{task}*")
        lines.append("")

    # ── Challenges ────────────────────────────────────────────────────────
    if mega.challenges:
        lines += ["## ⚡ Challenges", ""]
        for name, info in mega.challenges.items():
            if not isinstance(info, dict):
                continue
            cnt  = info.get("coverage_count", "—")
            sev  = str(info.get("severity", "")).strip()
            desc = str(info.get("description", "")).strip()
            sev_badge = f" `{sev}`" if sev else ""
            lines.append(f"- **{name}**{sev_badge} {cov(cnt)}")
            if desc:
                lines.append(f"  - *{desc}*")
        lines.append("")

    # ── Research Gaps ─────────────────────────────────────────────────────
    if mega.open_gaps:
        lines += ["## 🔭 Research Gaps", ""]
        for gap in mega.open_gaps:
            score = f" `score {gap.opportunity_score:.2f}`" if gap.opportunity_score else ""
            gtype = f" `{gap.gap_type}`" if gap.gap_type else ""
            lines.append(f"- **{_truncate_str(gap.gap, 60)}**{gtype}{score}")
            for ev_title in gap.evidence[:2]:
                a = _find_paper_anchor(ev_title, arch_triples)
                ev_link = f"[{_short_title(ev_title)}](#{a})" if a else _short_title(ev_title)
                lines.append(f"  - Evidence: {ev_link}")
        lines.append("")

    # ── Applications ─────────────────────────────────────────────────────
    if mega.applications:
        lines += ["## 🌐 Applications", ""]
        for app in mega.applications:
            lines.append(f"- {app}")
        lines.append("")

    return "\n".join(lines)


def _truncate_str(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _render_mermaid_png(mmd_path: Path, png_path: Path) -> None:
    """
    Render a .mmd file to PNG using the Mermaid CLI.

    Tries `mmdc` first (global install), then `npx @mermaid-js/mermaid-cli`
    (no global install required).  Logs a one-line hint and returns silently
    if neither is available — the pipeline is never blocked by this.
    """
    # Candidate mmdc locations: PATH first, then common nvm/homebrew locations
    _MMDC_CANDIDATES = [
        "mmdc",
        "/Users/guanzhongpan/.nvm/versions/node/v20.18.0/bin/mmdc",
        "/opt/homebrew/bin/mmdc",
    ]
    cmd: list[str] | None = None
    for candidate in _MMDC_CANDIDATES:
        if shutil.which(candidate) or (
            candidate != "mmdc" and __import__("os").path.exists(candidate)
        ):
            cmd = [candidate, "-i", str(mmd_path), "-o", str(png_path), "-b", "white"]
            break
    if cmd is None and shutil.which("npx"):
        cmd = [
            "npx", "--yes", "@mermaid-js/mermaid-cli",
            "-i", str(mmd_path), "-o", str(png_path), "-b", "white",
        ]

    if cmd is None:
        logger.info(
            "Mermaid CLI not found — skipping PNG render for %s. "
            "Install with: npm install -g @mermaid-js/mermaid-cli",
            mmd_path.name,
        )
        return

    # Tell Puppeteer to use system Chrome if present (avoids download errors)
    env = {**__import__("os").environ}
    system_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if __import__("os").path.exists(system_chrome):
        env["PUPPETEER_EXECUTABLE_PATH"] = system_chrome
        env["PUPPETEER_SKIP_DOWNLOAD"] = "true"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if result.returncode == 0:
            logger.info("Mermaid PNG rendered: %s", png_path.name)
        else:
            logger.warning(
                "Mermaid render failed for %s (exit %d): %s",
                mmd_path.name, result.returncode,
                (result.stderr or result.stdout).strip()[:200],
            )
    except subprocess.TimeoutExpired:
        logger.warning("Mermaid render timed out for %s", mmd_path.name)
    except Exception as exc:
        logger.warning("Mermaid render error for %s: %s", mmd_path.name, exc)


# Generic words that appear in almost every survey title and therefore carry
# no disambiguating signal.  They are stripped before fuzzy title matching so a
# fragment like "RAG for AIGC: A Survey" doesn't match "RAG for LLMs: A Survey"
# purely on the shared words "for", "a", "survey".
_TITLE_STOPWORDS = {
    "a", "an", "the", "for", "of", "on", "in", "and", "or", "to", "with", "from",
    "survey", "review", "systematic", "literature", "comprehensive", "overview",
    "study", "towards", "via", "using", "based", "approach", "approaches",
    "generation", "retrieval", "augmented", "retrievalaugmented",
}


# Domain acronyms expanded before matching, so a fragment that uses the acronym
# ("RAG for AIGC") matches a title that spells it out ("…for AI-Generated Content").
# RAG/LLM expansions land on stopwords and drop out, leaving the distinguishing
# words (e.g. "ai generated content" vs "large language models").
_TITLE_ACRONYMS = {
    "aigc": "ai generated content",
    "llms": "large language models",
    "llm": "large language model",
    "mllms": "multimodal large language models",
    "mllm": "multimodal large language model",
    "cais": "compound ai systems",
    "rag": "retrieval augmented generation",
    "qa": "question answering",
    "kg": "knowledge graph",
    "nlp": "natural language processing",
    "mcp": "model context protocol",
}


def _title_signature(text: str) -> str:
    """
    Lowercase significant words of a title for fuzzy matching:
    expand domain acronyms, then drop generic stopwords.
    """
    expanded = " ".join(
        _TITLE_ACRONYMS.get(w, w) for w in re.findall(r"[a-z0-9]+", text.lower())
    )
    words = re.findall(r"[a-z0-9]+", expanded)
    return " ".join(w for w in words if w not in _TITLE_STOPWORDS and len(w) > 1)


def _find_paper(
    title_fragment: str,
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
) -> "ScoredPaper | None":
    """
    Return the ScoredPaper whose title best matches `title_fragment`, or None.

    Matching is done on the *significant* words of the title (generic survey
    words removed) using rapidfuzz, with a disambiguation margin so an ambiguous
    fragment matches NOTHING rather than a plausible-but-wrong paper.
    """
    if not title_fragment:
        return None

    frag_lower = title_fragment.lower().strip()

    # 1. Exact / containment match on the full title (strongest signal)
    for sp, _, _ in arch_triples:
        t = sp.paper.title.lower()
        if frag_lower == t or (len(frag_lower) > 15 and frag_lower in t) \
           or (len(t) > 15 and t in frag_lower):
            return sp

    # 2. Fuzzy match on significant words, with a disambiguation margin
    from rapidfuzz import fuzz
    frag_sig = _title_signature(title_fragment)
    if not frag_sig:
        return None

    scored: list[tuple[float, ScoredPaper]] = []
    for sp, _, _ in arch_triples:
        score = fuzz.token_set_ratio(frag_sig, _title_signature(sp.paper.title))
        scored.append((score, sp))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_sp = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0

    if best_score >= 70 and (best_score - second_score) >= 15:
        return best_sp
    return None


def _find_paper_anchor(
    title_fragment: str,
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
) -> str:
    """Anchor of the best-matching paper card, or "" if no confident match."""
    sp = _find_paper(title_fragment, arch_triples)
    return _paper_anchor(sp) if sp else ""


def _paper_ref(
    title_query: str,
    arch_triples: list[tuple[ScoredPaper, PaperSummary, PaperArchitecture]],
) -> str:
    """
    A Markdown link to the matching paper card, using the paper's FULL real
    title (so titles are never confusingly truncated or paraphrased). Falls
    back to the given text (no link) when there is no confident match.
    """
    sp = _find_paper(title_query, arch_triples)
    if sp:
        return f"[{sp.paper.title}](#{_paper_anchor(sp)})"
    return title_query or ""


def _short_title(title: str, max_words: int = 5) -> str:
    words = title.split()
    if len(words) <= max_words:
        return title
    return " ".join(words[:max_words]) + "…"


# Abbreviations whose trailing dot must NOT be treated as a sentence boundary.
_SENTENCE_ABBREV = (
    "e.g.", "i.e.", "etc.", "vs.", "cf.", "et al.", "al.", "Fig.", "Eq.",
    "Sec.", "approx.", "Ref.", "No.", "Dr.", "Mr.", "Ms.", "Prof.",
)


def _split_sentences(text: str) -> list[str]:
    """
    Split prose into sentences, protecting common abbreviations so their
    trailing period is not mistaken for a sentence boundary.
    """
    protected = text
    for ab in _SENTENCE_ABBREV:
        protected = protected.replace(ab, ab.replace(".", "\x00"))
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", protected)
    return [p.replace("\x00", ".").strip() for p in parts if p.strip()]


def _format_prose_as_bullets(text: str, max_bullets: int = 6) -> list[str]:
    """
    Turn a free-text paragraph into scannable Markdown.

    - A single sentence is rendered as a block-quote line.
    - Multiple sentences become a bullet list (one bullet per sentence),
      capped at `max_bullets`.
    """
    text = " ".join(str(text).split())   # collapse whitespace
    if not text:
        return []
    sentences = _split_sentences(text)
    if len(sentences) <= 1:
        return [f"> {text}"]
    bullets = [f"- {s}" for s in sentences[:max_bullets]]
    if len(sentences) > max_bullets:
        # fold any overflow into the last bullet so no content is lost
        remainder = " ".join(sentences[max_bullets:])
        bullets[-1] = bullets[-1] + " " + remainder
    return bullets


def _mermaid_label(text: str, max_len: int = 48) -> str:
    """
    Sanitise a string for use inside a Mermaid node label `id["..."]`.

    Mermaid breaks on raw double-quotes and a few other characters even
    inside quoted labels, so we replace/strip the problematic ones and
    truncate long labels to keep the figure readable.
    """
    t = " ".join(str(text).split())          # collapse whitespace/newlines
    t = t.replace('"', "'").replace("`", "'")
    t = t.replace("[", "(").replace("]", ")")
    t = t.replace("{", "(").replace("}", ")")
    t = t.replace("|", "/").replace("#", "no.")
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t or "—"


def _taxonomy_mermaid(
    root_label: str,
    top_level: list[str],
    second_level: dict[str, list[str]],
    anchor: str,
    max_top: int = 6,
    max_sub: int = 4,
) -> list[str]:
    """
    Render a survey's taxonomy as a Mermaid flowchart (left-to-right tree).

    Produces an inline ```mermaid code block that renders as a real figure
    on GitHub, VS Code, Obsidian, and most Markdown viewers — replacing the
    old ASCII text-art tree.  `anchor` makes node IDs unique per paper card
    so multiple diagrams on one page never collide.
    """
    # Node-id prefix unique to this card (strip non-alphanumerics from anchor)
    pid = "t" + re.sub(r"[^a-zA-Z0-9]", "", anchor)[:12]

    out: list[str] = ["```mermaid", "graph LR"]
    root_id = f"{pid}root"
    out.append(f'    {root_id}["{_mermaid_label(root_label)}"]')

    for i, cat in enumerate(top_level[:max_top]):
        cat_id = f"{pid}c{i}"
        out.append(f'    {cat_id}["{_mermaid_label(cat)}"]')
        out.append(f"    {root_id} --> {cat_id}")
        for j, sub in enumerate(second_level.get(cat, [])[:max_sub]):
            sub_id = f"{pid}c{i}s{j}"
            out.append(f'    {sub_id}["{_mermaid_label(sub)}"]')
            out.append(f"    {cat_id} --> {sub_id}")

    # Style: highlight the root node so the survey title stands out
    out.append(f"    style {root_id} fill:#dbeafe,stroke:#2563eb,stroke-width:2px")
    out.append("```")
    return out
