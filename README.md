# AI Survey Paper Miner

Automatically discovers, scores, and deeply analyses AI survey papers from OpenAlex and CORE. Given a list of research topics it retrieves papers, filters out non-surveys, ranks them by quality, and runs a suite of LLM passes to produce a structured literature review with architecture analysis, concept graphs, and a guided reading path.

---

## How to use

### 1 — Install

```bash
pip install -r requirements.txt
```

### 2 — Add your API key

```bash
cp .env.example .env
# Open .env and fill in ANTHROPIC_API_KEY (required for all LLM features)
# CORE_API_KEY is optional but raises CORE's rate limit from 10 → 300 req/min
```

### 3 — Set your topics

Edit `config/topics.yaml`:

```yaml
topics:
  - Computer Vision
  - Large Language Models
  - Reinforcement Learning

year_from: 2022
year_to: 2026
max_results_per_query: 50   # raise this for broader coverage
top_n_to_summarize: 20
min_quality_score: 15
```

### 4 — Run

**Option A — Web UI** (recommended)

```bash
streamlit run ui.py
```

Open `http://localhost:8501`, configure everything in the sidebar, and click **Run Pipeline**.

**Option B — Command line**

```bash
# Full pipeline (retrieve + analyse)
python3 main.py run --config config/topics.yaml

# Fetch only (retrieve & score, save to JSONL for later analysis)
python3 main.py fetch --config config/topics.yaml

# Analyse only (load a saved JSONL and run all LLM passes)
python3 main.py analyze --papers-file data/processed/papers_scored.jsonl
```

Outputs are written to `data/exports/<topics>_<date>/`.

---

## Outputs

```
data/exports/computer-vision_2026-05-28/
├── papers_ranked.xlsx          ← formatted workbook (ranked papers + summaries)
├── papers_ranked.csv
├── paper_summaries.jsonl
├── survey_report.md            ← full Markdown report
└── computer-vision/            ← per-topic sub-folder
    ├── report.md               ← architecture report + reading guide
    ├── mindmap.html            ← interactive concept mindmap
    ├── architecture.json       ← machine-readable mega-architecture
    ├── concept_graph.json      ← typed concept graph
    └── reading_path.json       ← sequenced newcomer reading list
```

---

## Pipeline

```
topics.yaml
    │
    ├─ LLM query generation  (10 diverse queries per topic, cached 30 days)
    │
    ├─ Retrieval  ──  OpenAlex · CORE  (arXiv optional via --arxiv)
    │                 Results cached 7 days — re-runs are instant
    │
    ├─ Filters  ──  topic keyword overlap → survey-signal → LLM relevance filter
    │
    ├─ Deduplication  (DOI · arXiv ID · normalised title · fuzzy title)
    │
    ├─ Quality scoring  (venue · citations · survey signal · recency)
    │
    ├─ LLM summarisation  (structured scope / methods / findings per paper)
    │
    ├─ PDF parsing  (downloads open-access PDFs to enrich architecture prompts)
    │
    ├─ Architecture analysis  (LLM reverse-engineers each survey's structure)
    │
    ├─ LLM-as-Judge  (authority & quality assessment per paper)
    │
    ├─ Mega-architecture synthesis  (cross-survey field map per topic)
    │
    ├─ Concept graph  (typed graph linking key ideas across all surveys)
    │
    └─ Reading path  (sequenced newcomer reading list)
```

Skip any step with the matching flag: `--no-summarize`, `--no-architecture`, `--no-judge`, `--no-concept-graph`, `--no-reading-path`, `--no-pdf-parse`, `--no-llm-filter`, `--no-llm-queries`.

---

## Data sources

| Source | Coverage | Default |
|---|---|---|
| **OpenAlex** | 250 M+ papers, best citation data | ✅ always on |
| **CORE** | 200 M+ open-access papers, fills institutional-repo gaps | ✅ when `CORE_API_KEY` set |
| **arXiv** | CS / ML preprints | ⚪ opt-in via `--arxiv` |

> **Why is arXiv opt-in?** OpenAlex already indexes every arXiv paper with richer metadata (citation counts, DOIs). arXiv is disabled by default to avoid rate-limit errors; enable it only if you need papers published in the last day or two before OpenAlex picks them up.

---

## LLM cost estimate

A typical full run (3 topics, 50 results/query) costs roughly:

| Pass | Model | Approx. cost |
|---|---|---|
| Query generation | Haiku | ~$0.001 (cached after first run) |
| Relevance filter | Haiku | ~$0.02–0.05 |
| Summarisation | Sonnet | ~$0.10–0.30 |
| Architecture analysis | Sonnet | ~$0.20–0.60 |
| Concept graph + Reading path | Sonnet | ~$0.05–0.15 |

LLM results (summaries, judge, architecture) are cached locally so re-running the analysis pass on the same papers costs nothing.

---

## Caches

| Cache | Path | TTL |
|---|---|---|
| API query results | `data/raw/query_cache.json` | 7 days |
| LLM query strings | `data/raw/llm_query_cache.json` | 30 days |
| LLM summaries | `data/cache/llm/summaries/` | permanent |
| LLM judge results | `data/cache/llm/judge/` | permanent |
| Architecture results | `data/cache/llm/architecture/` | permanent |

Delete any cache file/folder to force a fresh run for that step.
