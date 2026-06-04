"""Tests for export.py"""

import csv
import json
import tempfile
from pathlib import Path

import pytest

from src.export import Exporter
from src.models import Paper, PaperSummary, ScoredPaper


def _scored_paper(title="Survey on LLMs", year=2023, score=85.0) -> ScoredPaper:
    paper = Paper(
        title=title,
        year=year,
        venue="arXiv",
        abstract="A comprehensive survey.",
        citation_count=100,
        sources=["arxiv"],
        topic_queries=["large language models"],
        generated_queries=[],
        url=f"https://arxiv.org/abs/2303.{hash(title) % 90000 + 10000}",
    )
    return ScoredPaper(paper=paper, quality_score=score)


def _summary(title="Survey on LLMs") -> PaperSummary:
    return PaperSummary(
        paper_title=title,
        research_scope="LLM capabilities",
        core_problem="Understanding emergent abilities",
        taxonomy=["Pre-training", "Fine-tuning", "Alignment"],
        main_findings=["LLMs show emergent abilities", "Scale matters"],
        keywords={"tasks": ["classification"], "methods": ["RLHF"]},
    )


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def test_csv_creates_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        exporter.export_csv([_scored_paper()])
        assert (Path(tmpdir) / "papers_ranked.csv").exists()


def test_csv_columns():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        exporter.export_csv([_scored_paper()])
        with open(Path(tmpdir) / "papers_ranked.csv", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            row = next(reader)
        assert "title" in row
        assert "quality_score" in row
        assert "citation_count" in row


def test_csv_rank_column():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        papers = [_scored_paper("Paper A", score=90), _scored_paper("Paper B", score=70)]
        exporter.export_csv(papers)
        with open(Path(tmpdir) / "papers_ranked.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["rank"] == "1"
        assert rows[1]["rank"] == "2"


# ---------------------------------------------------------------------------
# JSONL export
# ---------------------------------------------------------------------------

def test_jsonl_creates_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        sp = _scored_paper()
        s = _summary()
        exporter.export_jsonl([(sp, s)])
        assert (Path(tmpdir) / "paper_summaries.jsonl").exists()


def test_jsonl_valid_json_per_line():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        sp = _scored_paper()
        s = _summary()
        exporter.export_jsonl([(sp, s)])
        with open(Path(tmpdir) / "paper_summaries.jsonl", encoding="utf-8") as f:
            for line in f:
                obj = json.loads(line)
                assert "title" in obj
                assert "summary" in obj


def test_jsonl_empty_summary_pairs():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        exporter.export_jsonl([])
        path = Path(tmpdir) / "paper_summaries.jsonl"
        assert path.exists()
        assert path.read_text().strip() == ""


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def test_markdown_creates_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        exporter.export_markdown([_scored_paper()], [])
        assert (Path(tmpdir) / "survey_report.md").exists()


def test_markdown_contains_title():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        sp = _scored_paper("My Test Survey")
        exporter.export_markdown([sp], [])
        content = (Path(tmpdir) / "survey_report.md").read_text()
        assert "My Test Survey" in content


def test_markdown_with_summary():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        sp = _scored_paper("Survey on LLMs")
        s = _summary("Survey on LLMs")
        exporter.export_markdown([sp], [(sp, s)])
        content = (Path(tmpdir) / "survey_report.md").read_text()
        assert "Pre-training" in content  # from taxonomy


def test_markdown_grouped_by_topic():
    with tempfile.TemporaryDirectory() as tmpdir:
        exporter = Exporter(tmpdir)
        p1 = _scored_paper("LLM Survey")
        p1.paper.topic_queries = ["large language models"]
        p2 = _scored_paper("RAG Survey")
        p2.paper.topic_queries = ["retrieval augmented generation"]
        exporter.export_markdown([p1, p2], [])
        content = (Path(tmpdir) / "survey_report.md").read_text()
        assert "Large Language Models" in content
        assert "Retrieval Augmented Generation" in content
