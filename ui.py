"""
Survey Paper Miner — Streamlit Web UI  (v2)

Minimal, full-canvas layout.  No sidebar.
Launch:  streamlit run ui.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Page config  (must be first Streamlit call)
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Survey Paper Miner",
    page_icon="🔬",
    layout="centered",
    initial_sidebar_state="collapsed",
)

PROJECT_ROOT = Path(__file__).parent


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* hide the sidebar toggle arrow */
[data-testid="collapsedControl"] { display: none !important; }

/* generous vertical breathing room */
.block-container { padding-top: 2.8rem; padding-bottom: 5rem; }

/* section labels — small-caps, muted */
.sec {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #9ca3af;
    margin-top: 2.6rem;
    margin-bottom: 0.35rem;
}

/* one-line hint below a control */
.hint {
    font-size: 0.80rem;
    color: #6b7280;
    margin-top: -0.1rem;
    margin-bottom: 0.5rem;
    line-height: 1.5;
}

/* coloured left-bar info box used for mode description */
.mode-box {
    background: #f8f9ff;
    border-left: 3px solid #818cf8;
    padding: 0.55rem 0.9rem;
    border-radius: 0 6px 6px 0;
    font-size: 0.84rem;
    color: #374151;
    margin-top: 0.3rem;
    margin-bottom: 0.6rem;
}

/* status pill */
.pill {
    display: inline-block;
    padding: 0.15rem 0.55rem;
    border-radius: 999px;
    font-size: 0.76rem;
    font-weight: 500;
    margin-right: 0.35rem;
}
.ok   { background: #d1fae5; color: #065f46; }
.warn { background: #fef3c7; color: #92400e; }
.off  { background: #f3f4f6; color: #9ca3af; }

/* run button — full-width, tall */
div[data-testid="stButton"] button[kind="primary"] {
    width: 100%;
    height: 3.2rem;
    font-size: 1.0rem;
    font-weight: 600;
    border-radius: 8px;
    margin-top: 0.4rem;
}

/* output file list */
.outfile { font-family: monospace; font-size: 0.83rem; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_defaults() -> dict:
    path = PROJECT_ROOT / "config" / "topics.yaml"
    d: dict = dict(
        topics="",
        survey_terms="survey\nreview\ntaxonomy\noverview\nsystematic review",
        year_from=2022,
        year_to=2026,
        max_results=50,
        top_n=20,
        min_score=15.0,
        output_dir="data/exports",
    )
    if path.exists():
        with path.open() as f:
            raw = yaml.safe_load(f) or {}
        d["topics"]      = "\n".join(raw.get("topics", []))
        d["survey_terms"]= "\n".join(raw.get("survey_terms", ["survey","review","taxonomy","overview"]))
        d["year_from"]   = raw.get("year_from",             d["year_from"])
        d["year_to"]     = raw.get("year_to",               d["year_to"])
        d["max_results"] = raw.get("max_results_per_query",  d["max_results"])
        d["top_n"]       = raw.get("top_n_to_summarize",    d["top_n"])
        d["min_score"]   = raw.get("min_quality_score",     d["min_score"])
        d["output_dir"]  = raw.get("output_dir",            d["output_dir"])
    return d


def _load_env() -> dict[str, str | bool]:
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except ImportError:
        pass
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    core_key      = os.environ.get("CORE_API_KEY", "")
    return {"anthropic": anthropic_key, "core": core_key}


def _write_config_yaml(run_cfg: dict, tmp_dir: str) -> str:
    topics = [t.strip() for t in run_cfg["topics_text"].splitlines() if t.strip()]
    terms  = [t.strip() for t in run_cfg["survey_terms_text"].splitlines() if t.strip()]
    doc = {
        "topics":                    topics,
        "survey_terms":              terms,
        "year_from":                 run_cfg["year_from"],
        "year_to":                   run_cfg["year_to"],
        "max_results_per_query":     run_cfg["max_results"],
        "top_n_to_summarize":        run_cfg["top_n"],
        "min_quality_score":         run_cfg["min_score"],
        "min_topic_relevance":       run_cfg["min_topic_relevance"],
        "output_dir":                run_cfg["output_dir"],
        "architecture_enabled":      run_cfg["use_architecture"],
        "mega_architecture_enabled": run_cfg["use_architecture"],
    }
    path = os.path.join(tmp_dir, "topics_ui.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f, default_flow_style=False, allow_unicode=True)
    return path


def _pick_directory(initial: str) -> str | None:
    """
    Open a native OS directory-picker dialog and return the chosen path,
    or None if the user cancelled.

    Uses tkinter's filedialog — available on macOS, Windows, and Linux
    (install python3-tk on Linux if missing).  The dialog is raised to the
    front with wm_attributes so it doesn't hide behind the browser window.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()                         # hide the empty Tk root window
        root.wm_attributes("-topmost", True)    # raise dialog above other windows
        chosen = filedialog.askdirectory(
            title="Select output directory",
            initialdir=initial if Path(initial).is_dir() else ".",
        )
        root.destroy()
        return chosen or None
    except Exception:
        return None


def _build_cmd(run_cfg: dict, config_path: str) -> list[str]:
    cmd = [sys.executable, str(PROJECT_ROOT / "main.py"), run_cfg["mode"]]
    cmd += ["--config",      config_path]
    cmd += ["--output-dir",  run_cfg["output_dir"]]
    cmd += ["--db",          run_cfg["db_path"]]
    cmd += ["--papers-file", run_cfg["papers_file"]]
    flags = {
        "--no-llm-queries":   not run_cfg["use_llm_queries"],
        "--no-llm-filter":    not run_cfg["use_llm_filter"],
        "--no-summarize":     not run_cfg["use_summarize"],
        "--no-judge":         not run_cfg["use_judge"],
        "--no-architecture":  not run_cfg["use_architecture"],
        "--no-pdf-parse":     not run_cfg["use_pdf_parse"],
        "--no-concept-graph": not run_cfg["use_concept_graph"],
        "--no-reading-path":  not run_cfg["use_reading_path"],
    }
    for flag, active in flags.items():
        if active:
            cmd.append(flag)
    if run_cfg.get("use_arxiv"):
        cmd.append("--arxiv")
    return cmd


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────
_STATE_DEFAULTS: dict = {
    "running": False, "finished": False,
    "log_lines": [], "returncode": None,
    "run_cfg": None, "tmp_dir": None,
    "output_dir": None,   # set from DEFAULTS after they load
}
for _k, _v in _STATE_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

DEFAULTS = _load_defaults()
env_keys  = _load_env()
has_anthropic = bool(env_keys["anthropic"])
has_core      = bool(env_keys["core"])

# Seed output_dir from config on first load (None means not yet initialised)
if st.session_state["output_dir"] is None:
    st.session_state["output_dir"] = DEFAULTS["output_dir"]


# ─────────────────────────────────────────────────────────────────────────────
# ── HEADER ───────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
left, right = st.columns([3, 1.4])
with left:
    st.markdown("# 🔬 Survey Paper Miner")
    st.caption("Discover, score, and deeply analyse AI survey papers from OpenAlex and CORE.")
with right:
    st.write("")
    st.write("")
    a_cls = "pill ok" if has_anthropic else "pill warn"
    a_txt = "✓ Anthropic" if has_anthropic else "⚠ No API key"
    c_cls = "pill ok" if has_core else "pill off"
    c_txt = "✓ CORE" if has_core else "○ No CORE key"
    st.markdown(
        f'<span class="{a_cls}">{a_txt}</span>'
        f'<span class="{c_cls}">{c_txt}</span>',
        unsafe_allow_html=True,
    )

# Inline API key entry when key is missing
api_key_override = ""
if not has_anthropic:
    st.warning(
        "No Anthropic API key found in `.env`. LLM features will be skipped. "
        "Enter one below or add it to `.env`.",
        icon="⚠️",
    )
    api_key_override = st.text_input(
        "Anthropic API Key",
        type="password",
        placeholder="sk-ant-…",
        help="Used only for this run. Not saved to disk. "
             "To persist it, add ANTHROPIC_API_KEY=… to your .env file.",
    )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# ── SECTION 1 — RESEARCH TOPICS ──────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="sec">Research Topics</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="hint">One topic per line. '
    'Claude generates 10 diverse search queries per topic (synonyms, subtopics, '
    'domain terminology) to maximise recall. Near-duplicate topics are automatically merged.</p>',
    unsafe_allow_html=True,
)
topics_text = st.text_area(
    "topics",
    value=DEFAULTS["topics"],
    height=148,
    placeholder="Computer Vision\nLarge Language Models\nReinforcement Learning from Human Feedback",
    label_visibility="collapsed",
)

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# ── SECTION 2 — PIPELINE MODE ────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="sec">Pipeline Mode</p>', unsafe_allow_html=True)

_MODE_LABELS = {
    "run":     "▶  Full Pipeline",
    "fetch":   "⬇  Fetch Only",
    "analyze": "🔍  Analyse Only",
}
_MODE_DESC = {
    "run": (
        "Complete end-to-end run: retrieve papers from OpenAlex & CORE, "
        "filter and score them, then run all selected LLM analysis passes "
        "(summarisation, architecture, judge, concept graph, reading path) and export."
    ),
    "fetch": (
        "Retrieval + scoring only — no LLM calls. "
        "Papers are saved to a JSONL file so you can inspect, tweak settings, "
        "then run Analyse Only without re-fetching from the APIs."
    ),
    "analyze": (
        "LLM analysis only — loads a previously saved papers JSONL and runs "
        "all enabled LLM passes. Useful when you already have fetched papers "
        "and want to re-run or extend the analysis without hitting the retrieval APIs again."
    ),
}

mode = st.radio(
    "mode",
    options=["run", "fetch", "analyze"],
    format_func=_MODE_LABELS.__getitem__,
    horizontal=True,
    label_visibility="collapsed",
)
st.markdown(f'<div class="mode-box">{_MODE_DESC[mode]}</div>', unsafe_allow_html=True)

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# ── SECTION 3 — SEARCH PARAMETERS ────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="sec">Search Parameters</p>', unsafe_allow_html=True)

c1, c2, c3, c4 = st.columns(4)
with c1:
    year_from = st.number_input(
        "Year from",
        min_value=2000, max_value=2030,
        value=int(DEFAULTS["year_from"]), step=1,
        help="Only papers published in this year or later are retrieved. "
             "OpenAlex filters this server-side, so it doesn't waste your results quota.",
    )
with c2:
    year_to = st.number_input(
        "Year to",
        min_value=2000, max_value=2030,
        value=int(DEFAULTS["year_to"]), step=1,
        help="Only papers published up to (and including) this year are retrieved.",
    )
with c3:
    max_results = st.number_input(
        "Results per query",
        min_value=10, max_value=500,
        value=max(int(DEFAULTS["max_results"]), 50), step=10,
        help="How many papers each source returns per search query. "
             "With 10 queries per topic and 2 sources this caps the raw pool at "
             "10 × 2 × N papers before filtering. Setting this below 50 often "
             "produces fewer than 5 papers after all filters for broad topics.",
    )
with c4:
    top_n = st.number_input(
        "Top N to analyse",
        min_value=5, max_value=100,
        value=int(DEFAULTS["top_n"]), step=5,
        help="The highest-scoring N papers are sent through the LLM passes "
             "(summarisation, architecture analysis, judge). "
             "Lower = cheaper & faster; higher = more comprehensive.",
    )

c5, c6, c7 = st.columns([1, 1, 2])
with c5:
    min_score = st.number_input(
        "Min quality score",
        min_value=0.0, max_value=100.0,
        value=float(DEFAULTS["min_score"]), step=1.0,
        help="Papers scoring below this threshold are dropped before the LLM passes. "
             "The score is out of 100 and combines venue quality, citation impact, "
             "survey signal strength, structure, and recency. "
             "15–20 is a good default; lower it if you are getting too few results.",
    )
with c6:
    min_topic_relevance = st.selectbox(
        "Min topic relevance",
        options=[1, 2, 3, 4, 5],
        index=2,   # default = 3
        format_func={
            1: "1 — off-topic (no filter)",
            2: "2 — tangential",
            3: "3 — related (default)",
            4: "4 — directly relevant",
            5: "5 — exact topic only",
        }.__getitem__,
        help="After judging, papers below this topic-relevance score are removed. "
             "The judge rates 1–5 how specifically a paper addresses your configured topics. "
             "3 keeps related background papers (e.g. a conceptual agent taxonomy when "
             "searching for Agentic RAG). "
             "4 requires papers to be directly about the topic. "
             "5 keeps only papers that are precisely about your topic.",
    )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# ── SECTION 4 — PIPELINE STEPS ───────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="sec">Pipeline Steps</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="hint">All steps are on by default. '
    'Uncheck any step to skip it and reduce API cost or runtime. '
    'Hover the <strong>ⓘ</strong> icon for details on each step.</p>',
    unsafe_allow_html=True,
)

_STEPS: list[tuple[str, str, str, str]] = [
    # (state_key, label, caption, help_text)
    (
        "use_llm_queries",
        "LLM Query Generation",
        "claude-haiku · cached 30 days",
        "Claude writes 10 diverse search queries per topic using synonyms, subtopics, "
        "and domain-specific terminology — far better recall than a simple "
        "keyword cross-product. Results are cached locally so subsequent runs with "
        "the same topics cost nothing.",
    ),
    (
        "use_llm_filter",
        "LLM Relevance Filter",
        "claude-haiku · batches of 20",
        "After keyword and survey-signal filters, a Claude pass binary-classifies "
        "each remaining paper as relevant or not. Catches papers that matched "
        "keywords but are about a different domain (e.g. a medical imaging paper "
        "surfacing in an 'AI in education' search). Very low cost (~$0.01–0.05/run).",
    ),
    (
        "use_summarize",
        "LLM Summarisation",
        "claude-sonnet · top-N papers",
        "For the top-N highest-scoring papers, Claude extracts a structured summary: "
        "research scope, core problem, methods taxonomy, key findings, limitations, "
        "and future directions. Results are cached per paper so re-running the "
        "analysis step costs nothing for previously summarised papers.",
    ),
    (
        "use_judge",
        "LLM-as-Judge",
        "claude-sonnet · authority assessment",
        "An independent Claude pass rates each paper on: authority tier "
        "(foundational / current-standard / emerging), scope clarity, methodological "
        "rigour, and gives a recommendation: must-read / worth-reading / optional / skip. "
        "Results are cached per paper.",
    ),
    (
        "use_architecture",
        "Architecture Analysis",
        "claude-sonnet · per paper + mega-synthesis",
        "Claude reverse-engineers how each survey organises its field: problem taxonomy, "
        "method families, evaluation benchmarks, open challenges, and future directions. "
        "A second pass synthesises all per-paper analyses into one unified field-level "
        "mega-architecture per topic.",
    ),
    (
        "use_pdf_parse",
        "PDF Full-text Parsing",
        "pdfplumber · open-access PDFs only",
        "Downloads freely available PDFs (arXiv, open-access journals) and extracts "
        "the introduction and related-work sections. This enriches the architecture "
        "prompt with concrete details that abstract text alone cannot provide. "
        "Silently skipped for paywalled papers. Requires pdfplumber.",
    ),
    (
        "use_concept_graph",
        "Concept Graph",
        "claude-sonnet · per topic",
        "Identifies the key concepts, methods, datasets, and metrics mentioned across "
        "all survey papers for a topic, and maps typed relationships between them "
        "(e.g. 'method X addresses problem Y', 'dataset A evaluates method B'). "
        "Exported as JSON and as an interactive HTML mindmap.",
    ),
    (
        "use_reading_path",
        "Reading Path",
        "claude-sonnet · per topic",
        "Produces an ordered reading plan for a newcomer to the field: foundational "
        "papers first, then current-standard, then emerging. Each step includes a "
        "rationale, the sections to focus on, prerequisite concepts, and an estimated "
        "reading time.",
    ),
]

step_vals: dict[str, bool] = {}
cols_a, cols_b = st.columns(2)
for i, (key, label, caption, help_text) in enumerate(_STEPS):
    col = cols_a if i % 2 == 0 else cols_b
    with col:
        step_vals[key] = st.checkbox(label, value=True, help=help_text)
        st.markdown(f'<p class="hint">{caption}</p>', unsafe_allow_html=True)

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# ── SECTION 5 — OUTPUT DIRECTORY ─────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<p class="sec">Output Directory</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="hint">Results are saved here. Each run creates a new dated sub-folder '
    '(e.g. <code>computer-vision_2026-05-28/</code>) so previous outputs are never overwritten.</p>',
    unsafe_allow_html=True,
)

dir_col, btn_col = st.columns([5, 1])
with dir_col:
    output_dir = st.text_input(
        "output_dir_field",
        value=st.session_state["output_dir"],
        placeholder="data/exports",
        label_visibility="collapsed",
    )
    # Keep state in sync when the user edits the path manually
    st.session_state["output_dir"] = output_dir
with btn_col:
    st.write("")   # nudge button down to align with the text input
    if st.button("📂 Browse", use_container_width=True, help="Open a folder picker"):
        picked = _pick_directory(output_dir)
        if picked:
            st.session_state["output_dir"] = picked
            st.rerun()

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# ── SECTION 6 — ADVANCED (collapsed by default) ──────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
papers_file_default = "data/processed/papers_scored.jsonl"

with st.expander("⚙️  Advanced Settings", expanded=False):

    st.markdown("**Survey signal terms**")
    st.caption(
        "A paper must contain at least one of these words/phrases in its title or abstract "
        "to pass the survey-signal filter. Primary research papers that pass keyword "
        "filters are removed here. Separate with newlines."
    )
    survey_terms_text = st.text_area(
        "survey_terms",
        value=DEFAULTS["survey_terms"],
        height=100,
        label_visibility="collapsed",
    )

    st.write("")
    st.markdown("**Data sources**")
    use_arxiv = st.checkbox(
        "Include arXiv",
        value=False,
        help="OpenAlex already indexes every arXiv paper with richer metadata "
             "(citation counts, DOIs, normalised venues). arXiv is disabled by default "
             "to avoid 429 rate-limit errors. Enable only if you need papers published "
             "in the last 1–2 days before OpenAlex picks them up.",
    )
    if use_arxiv:
        st.caption("⚠️ arXiv aggressively rate-limits automated clients. Expect 429 warnings.")

    st.write("")
    st.markdown("**Storage paths**")
    col_p1, col_p2 = st.columns(2)
    with col_p1:
        db_path = st.text_input(
            "SQLite database",
            value="data/processed/papers.db",
            help="All retrieved papers and LLM summaries are persisted here across runs. "
                 "The database enables incremental updates: papers already in the DB "
                 "are updated rather than duplicated.",
        )
    with col_p2:
        papers_file = st.text_input(
            "Papers file (Analyse mode)",
            value=papers_file_default,
            help="Path to the JSONL produced by a Fetch run. "
                 "Only required when running in Analyse Only mode.",
        )
        if mode == "analyze":
            pf = PROJECT_ROOT / papers_file
            if pf.exists():
                st.caption(f"✓ Found  ({pf.stat().st_size // 1024} KB)")
            else:
                st.error("File not found — run Fetch mode first.")


# ─────────────────────────────────────────────────────────────────────────────
# ── VALIDATION ────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# papers_file is now defined (from the expander widget above, which always runs)
topics_list      = [t.strip() for t in topics_text.splitlines() if t.strip()]
_analyze_file_ok = (mode != "analyze") or (PROJECT_ROOT / papers_file).exists()
_can_run         = bool(topics_list or mode == "analyze") and _analyze_file_ok

if not topics_list and mode != "analyze":
    st.warning("Add at least one topic above before running.", icon="⚠️")
if mode == "analyze" and not _analyze_file_ok:
    st.error(
        "**Analyse Only** mode needs a saved papers file. "
        "Run **Fetch Only** or **Full Pipeline** first to generate it.",
        icon="🚫",
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── RUN BUTTON ────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
st.write("")
_btn_label    = "⏳  Running…" if st.session_state.running else "▶  Run Pipeline"
_btn_disabled = st.session_state.running or not _can_run

run_btn = st.button(
    _btn_label,
    type="primary",
    disabled=_btn_disabled,
    use_container_width=True,
)

if st.session_state.running:
    st.info("Pipeline is running — live output below.", icon="⏳")
elif st.session_state.finished:
    rc = st.session_state.returncode
    if rc == 0:
        st.success("Pipeline completed successfully!", icon="✅")
    else:
        st.error(f"Pipeline failed (exit code {rc}). See the log below.", icon="❌")


# ─────────────────────────────────────────────────────────────────────────────
# ── CAPTURE CONFIG AT CLICK TIME ─────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
if run_btn and not st.session_state.running:
    st.session_state.run_cfg = dict(
        mode              = mode,
        topics_text       = topics_text,
        survey_terms_text = survey_terms_text,
        year_from         = int(year_from),
        year_to           = int(year_to),
        max_results       = int(max_results),
        top_n             = int(top_n),
        min_score             = float(min_score),
        min_topic_relevance   = int(min_topic_relevance),
        output_dir        = output_dir,
        db_path           = db_path,
        papers_file       = papers_file,
        api_key_override  = api_key_override,
        use_arxiv         = use_arxiv,
        **{k: step_vals[k] for k, *_ in _STEPS},
    )
    st.session_state.running    = True
    st.session_state.finished   = False
    st.session_state.log_lines  = []
    st.session_state.returncode = None
    st.session_state.tmp_dir    = tempfile.mkdtemp()
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# ── LIVE EXECUTION ────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
if st.session_state.running and not st.session_state.finished:
    run_cfg     = st.session_state.run_cfg
    config_path = _write_config_yaml(run_cfg, st.session_state.tmp_dir)
    cmd         = _build_cmd(run_cfg, config_path)

    with st.expander("Command", expanded=False):
        st.code(" ".join(cmd), language="bash")

    st.markdown('<p class="sec">Live Output</p>', unsafe_allow_html=True)
    log_placeholder = st.empty()

    env = {**os.environ}
    if run_cfg.get("api_key_override"):
        env["ANTHROPIC_API_KEY"] = run_cfg["api_key_override"]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    collected: list[str] = []
    for raw in proc.stdout:
        collected.append(raw.rstrip())
        log_placeholder.code("\n".join(collected[-80:]), language="")

    proc.wait()
    st.session_state.log_lines  = collected
    st.session_state.running    = False
    st.session_state.finished   = True
    st.session_state.returncode = proc.returncode
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# ── POST-RUN VIEW ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
if st.session_state.finished:

    with st.expander(
        "📜 Full Run Log",
        expanded=(st.session_state.returncode != 0),
    ):
        st.code("\n".join(st.session_state.log_lines), language="")

    # Output files
    run_cfg_saved = st.session_state.run_cfg or {}
    out_path = PROJECT_ROOT / run_cfg_saved.get("output_dir", DEFAULTS["output_dir"])
    db_file  = PROJECT_ROOT / run_cfg_saved.get("db_path", "data/processed/papers.db")

    if out_path.exists():
        recent = sorted(
            (f for f in out_path.rglob("*") if f.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:40]

        if recent:
            st.markdown('<p class="sec">Output Files</p>', unsafe_allow_html=True)
            _ICONS = {
                ".html": "🌐", ".md": "📝", ".xlsx": "📊",
                ".csv":  "📄", ".json": "🗃", ".jsonl": "🗃",
                ".png":  "🖼", ".mmd": "🔷",
            }
            groups: dict[str, list[Path]] = {}
            for f in recent:
                groups.setdefault(f.suffix.lower() or "other", []).append(f)

            for ext, files in sorted(groups.items()):
                icon = _ICONS.get(ext, "📎")
                with st.expander(
                    f"{icon} {ext.lstrip('.').upper() or 'Other'}  ({len(files)})",
                    expanded=True,
                ):
                    for f in files:
                        sz = f.stat().st_size
                        sz_str = f"{sz/1e6:.1f} MB" if sz >= 1_000_000 else f"{sz/1024:.1f} KB"
                        try:
                            rel = f.relative_to(PROJECT_ROOT)
                        except ValueError:
                            rel = f
                        st.markdown(
                            f"<span class='outfile'>`{rel}`</span>"
                            f"&emsp;<span style='color:#9ca3af'>{sz_str}</span>",
                            unsafe_allow_html=True,
                        )

    if db_file.exists():
        st.caption(
            f"🗄 Database: `{db_file.relative_to(PROJECT_ROOT)}`  "
            f"({db_file.stat().st_size // 1024} KB)"
        )

    st.divider()
    if st.button("🔄  Start a new run", use_container_width=False):
        for k, v in _STATE_DEFAULTS.items():
            st.session_state[k] = v if not isinstance(v, list) else []
        st.session_state["output_dir"] = DEFAULTS["output_dir"]   # re-seed from config
        st.rerun()
