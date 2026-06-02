"""
SQLite persistence layer.

Tables:
  papers    — one row per unique paper (after deduplication)
  summaries — one row per LLM-generated summary (FK → papers)

JSON columns store lists/dicts as JSON strings because SQLite has no native
array type.  The `load_*` helpers deserialise them back on read.

Usage:
    db = Database("data/processed/papers.db")
    db.init_schema()
    db.upsert_papers(scored_papers)
    db.upsert_summaries(summary_pairs)
    papers = db.load_papers()
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .models import Paper, PaperSummary, ScoredPaper

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str | Path = "data/processed/papers.db"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        """Create tables if they don't already exist."""
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS papers (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                title                    TEXT NOT NULL,
                normalized_title         TEXT,
                year                     INTEGER,
                authors                  TEXT,
                venue                    TEXT,
                abstract                 TEXT,
                doi                      TEXT,
                arxiv_id                 TEXT,
                url                      TEXT,
                pdf_url                  TEXT,
                citation_count           INTEGER DEFAULT 0,
                influential_citation_count INTEGER DEFAULT 0,
                quality_score            REAL,
                sources                  TEXT,
                topic_queries            TEXT,
                generated_queries        TEXT,
                created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi
                ON papers(doi) WHERE doi IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_arxiv_id
                ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_papers_norm_title
                ON papers(normalized_title);

            CREATE TABLE IF NOT EXISTS summaries (
                id                             INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id                       INTEGER,
                research_scope                 TEXT,
                core_problem                   TEXT,
                taxonomy                       TEXT,
                main_methods                   TEXT,
                representative_papers_or_models TEXT,
                datasets_and_benchmarks        TEXT,
                evaluation_metrics             TEXT,
                main_findings                  TEXT,
                limitations                    TEXT,
                future_directions              TEXT,
                keywords                       TEXT,
                citation_use_cases             TEXT,
                summarization_source           TEXT,
                summarization_failed           INTEGER DEFAULT 0,
                failure_reason                 TEXT,
                created_at                     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(paper_id) REFERENCES papers(id)
            );
        """)
        self._conn.commit()
        logger.info("Database schema initialised at %s", self._path)

    def upsert_papers(self, scored_papers: list[ScoredPaper]) -> dict[str, int]:
        """
        Insert or update papers.  Returns a mapping of normalised_title → row id
        so summaries can reference the correct paper_id.

        We avoid ON CONFLICT(doi) because doi has a *partial* unique index
        (WHERE doi IS NOT NULL), which SQLite's INSERT … ON CONFLICT clause
        does not support.  Instead we do an explicit SELECT-then-INSERT-or-UPDATE.
        """
        cur = self._conn.cursor()
        title_to_id: dict[str, int] = {}

        for sp in scored_papers:
            p = sp.paper
            norm = p.normalized_title()
            row = _paper_to_row(p, sp.quality_score)

            # Check for an existing row by normalised title (post-dedup, this is unique)
            cur.execute("SELECT id FROM papers WHERE normalized_title = ?", (norm,))
            existing = cur.fetchone()

            if existing:
                row_id = existing["id"]
                cur.execute("""
                    UPDATE papers SET
                        citation_count           = MAX(citation_count, :citation_count),
                        quality_score            = :quality_score,
                        abstract                 = COALESCE(abstract, :abstract),
                        pdf_url                  = COALESCE(pdf_url, :pdf_url),
                        doi                      = COALESCE(doi, :doi),
                        arxiv_id                 = COALESCE(arxiv_id, :arxiv_id)
                    WHERE id = :row_id
                """, {**row, "row_id": row_id})
            else:
                cur.execute("""
                    INSERT INTO papers
                        (title, normalized_title, year, authors, venue, abstract,
                         doi, arxiv_id, url, pdf_url, citation_count,
                         influential_citation_count, quality_score,
                         sources, topic_queries, generated_queries)
                    VALUES
                        (:title, :normalized_title, :year, :authors, :venue, :abstract,
                         :doi, :arxiv_id, :url, :pdf_url, :citation_count,
                         :influential_citation_count, :quality_score,
                         :sources, :topic_queries, :generated_queries)
                """, row)
                row_id = cur.lastrowid

            title_to_id[norm] = row_id

        self._conn.commit()
        logger.info("Upserted %d papers into database", len(scored_papers))
        return title_to_id

    def upsert_summaries(
        self,
        summary_pairs: list[tuple[ScoredPaper, PaperSummary]],
        title_to_id: dict[str, int],
    ) -> None:
        """Insert summaries, linking each to its paper row."""
        cur = self._conn.cursor()
        for sp, summary in summary_pairs:
            paper_id = title_to_id.get(sp.paper.normalized_title())
            if paper_id is None:
                logger.warning("No paper_id found for '%s' — skipping summary", sp.paper.title[:60])
                continue

            cur.execute("""
                INSERT INTO summaries
                    (paper_id, research_scope, core_problem, taxonomy,
                     main_methods, representative_papers_or_models,
                     datasets_and_benchmarks, evaluation_metrics,
                     main_findings, limitations, future_directions,
                     keywords, citation_use_cases,
                     summarization_source, summarization_failed, failure_reason)
                VALUES
                    (:paper_id, :research_scope, :core_problem, :taxonomy,
                     :main_methods, :representative_papers_or_models,
                     :datasets_and_benchmarks, :evaluation_metrics,
                     :main_findings, :limitations, :future_directions,
                     :keywords, :citation_use_cases,
                     :summarization_source, :summarization_failed, :failure_reason)
            """, _summary_to_row(paper_id, summary))

        self._conn.commit()
        logger.info("Inserted %d summaries into database", len(summary_pairs))

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _paper_to_row(p: Paper, quality_score: float) -> dict:
    return {
        "title": p.title,
        "normalized_title": p.normalized_title(),
        "year": p.year,
        "authors": json.dumps(p.authors),
        "venue": p.venue,
        "abstract": p.abstract,
        "doi": p.doi,
        "arxiv_id": p.arxiv_id,
        "url": p.url,
        "pdf_url": p.pdf_url,
        "citation_count": p.citation_count,
        "influential_citation_count": p.influential_citation_count,
        "quality_score": quality_score,
        "sources": json.dumps(p.sources),
        "topic_queries": json.dumps(p.topic_queries),
        "generated_queries": json.dumps(p.generated_queries),
    }


def _summary_to_row(paper_id: int, s: PaperSummary) -> dict:
    return {
        "paper_id": paper_id,
        "research_scope": s.research_scope,
        "core_problem": s.core_problem,
        "taxonomy": json.dumps(s.taxonomy),
        "main_methods": json.dumps(s.main_methods),
        "representative_papers_or_models": json.dumps(s.representative_papers_or_models),
        "datasets_and_benchmarks": json.dumps(s.datasets_and_benchmarks),
        "evaluation_metrics": json.dumps(s.evaluation_metrics),
        "main_findings": json.dumps(s.main_findings),
        "limitations": json.dumps(s.limitations),
        "future_directions": json.dumps(s.future_directions),
        "keywords": json.dumps(s.keywords),
        "citation_use_cases": json.dumps(s.citation_use_cases),
        "summarization_source": s.summarization_source,
        "summarization_failed": int(s.summarization_failed),
        "failure_reason": s.failure_reason,
    }
