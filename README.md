# AI Survey Paper Miner

Automatically discovers, scores, and deeply analyses AI survey papers from OpenAlex and CORE. Given a list of research topics it retrieves papers, filters out non-surveys, ranks them by quality, and runs a suite of LLM passes to produce a structured literature review with architecture analysis, a top-down system design, and a guided reading path.

> 📐 **Architecture & pipeline diagrams + why each module exists:** see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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
├── papers_ranked.xlsx          ← ranked papers + summaries (open in Excel)
├── report.html                 ← THE MAIN REPORT, interactive (double-click → browser)
├── report.md                   ← same report as Markdown (for GitHub/VS Code/Obsidian)
└── computer-vision/            ← per-topic sub-folder (supporting artefacts)
    ├── mindmap.html            ← interactive mindmap (double-click → browser)
    ├── architecture.json       ← machine-readable mega-architecture
    ├── architecture.mmd        ← Mermaid source of the field diagram
    ├── system_design.json      ← top-down system design (layers + components)
    └── reading_path.json       ← sequenced newcomer reading list

> With several topics in one run, the reports are slug-prefixed
> (`agentic-rag-systems-report.html`, …) so they don't collide.
```

The report comes in **two formats with the same content** — pick whichever you prefer:
- **`report.html`** — just double-click; opens in your browser, no tools needed. The Field Map is a **collapsible tree** (the topic and its categories show first; click a category to expand its items, or use Expand/Collapse all), and the **Field Tree** (problem-solving chain: Core Problems → Research Areas → Methods → Techniques) and **Problem Tree** (problem-exposing chain: Research Areas → Challenges → Research Gaps) both let you click any item to highlight what it links to.
- **`report.md`** — the Markdown version, best if you read in GitHub / VS Code / Obsidian.

> **Two trees, two viewpoints.** The **Field Tree** answers *"how is the field solving its problems?"* (drivers → topic → areas → methods → techniques). The **Problem Tree** answers *"where is the field still stuck?"* (areas → open challenges → research gaps), with speculative *blue-sky* gaps — ideas no current challenge motivates — listed separately. Benchmarks are reported **only when the surveyed papers name them** — none are added from outside knowledge.

---

## How to open the report (no Markdown experience needed)

Each run produces a few things you'll actually look at. Here they are, **easiest first**:

| File | How to open | What it is |
|---|---|---|
| **`report.html`** ⭐ | **Double-click → opens in your browser** | The full report, interactive. Zero setup. The Field Map is a collapsible tree you expand category-by-category. **Start here.** |
| **`papers_ranked.xlsx`** | Double-click → **Excel / Numbers / Google Sheets** | The ranked list of papers with summaries. |
| **`mindmap.html`** | Double-click → browser | An interactive mind-map of the field. |
| **`report.md`** | See below ↓ | The same report as Markdown — for reading in GitHub / VS Code / Obsidian. |

> 💡 **If you're not technical, just double-click `report.html`.** Everything renders in your browser with no setup. The Markdown (`report.md`) section below is only for people who prefer GitHub / VS Code / Obsidian.

### Why can't I just double-click `report.md`?

`.md` (Markdown) is a **plain-text** format. If you double-click it, it opens in a text editor and you'll see raw symbols like `#`, `[link](...)`, and the diagrams will show up as code instead of pictures. You need a **Markdown viewer** to see it rendered (with diagrams and clickable links). Pick **any one** of these — all free, all show the diagrams and the click-to-jump links correctly:

**Option A — VS Code (recommended, easiest)**
1. Install [VS Code](https://code.visualstudio.com/) (free).
2. Open `report.md` in it.
3. Press **`Cmd+Shift+V`** (Mac) or **`Ctrl+Shift+V`** (Windows) to see the rendered preview.
4. For the diagrams to appear, install the extension **"Markdown Preview Mermaid Support"** once: click the Extensions icon on the left, search that name, click *Install*. Done — reopen the preview.

**Option B — Obsidian (best reading experience)**
1. Install [Obsidian](https://obsidian.md/) (free).
2. "Open folder as vault" → choose the report's folder.
3. Click `report.md`. Diagrams **and** the internal jump-links (e.g. clicking a paper title jumps to its card) work out of the box.

**Option C — GitHub (nothing to install)**
Upload the report folder to any GitHub repository and open `report.md` on the website — GitHub renders the Mermaid diagrams automatically.

### Want to share it with someone non-technical?

Turn it into a **PDF**: in VS Code, install the **"Markdown PDF"** extension, then right-click the report → *"Markdown PDF: Export (pdf)"*. You get a normal PDF anyone can open.

> 💡 In short: **just double-click `report.html`** — full report in your browser, nothing to install. Use **`report.md`** (in VS Code / Obsidian / GitHub) only if you specifically want the Markdown version.

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
    ├─ System design  (top-down layered architecture of the field)
    │
    └─ Reading path  (sequenced newcomer reading list)
```

Skip any step with the matching flag: `--no-summarize`, `--no-architecture`, `--no-judge`, `--no-system-design`, `--no-reading-path`, `--no-pdf-parse`, `--no-llm-filter`, `--no-llm-queries`.

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
| System design + Reading path | Sonnet | ~$0.05–0.15 |

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
