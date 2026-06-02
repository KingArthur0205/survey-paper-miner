"""
Survey Paper Miner — Streamlit Web UI

Launch with:
    streamlit run ui.py
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
# Page config  (must be the very first Streamlit call)
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Survey Paper Miner",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

PROJECT_ROOT = Path(__file__).parent


# ─────────────────────────────────────────────────────────────────────────────
# Custom CSS
# ─────────────────────────────────────────────────────────────────────────────

st.markdown(
    """
    <style>
    /* Tighten sidebar spacing */
    section[data-testid="stSidebar"] .block-container { padding-top: 1rem; }
    /* Make the run button larger */
    div[data-testid="stButton"] > button[kind="primary"] {
        font-size: 1.05rem;
        padding: 0.55rem 1.25rem;
    }
    /* Dim disabled steps in the pipeline checklist */
    div[data-testid="stCheckbox"] label { cursor: pointer; }
    /* File list monospace */
    .output-file { font-family: monospace; font-size: 0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_defaults() -> dict:
    """Read defaults from config/topics.yaml (cached for the session)."""
    path = PROJECT_ROOT / "config" / "topics.yaml"
    d: dict = dict(
        topics="",
        survey_terms="survey\nreview\ntaxonomy\noverview\nsystematic review",
        year_from=2022,
        year_to=2026,
        max_results=25,
        top_n=40,
        min_score=15.0,
        output_dir="data/exports",
    )
    if path.exists():
        with path.open() as f:
            raw = yaml.safe_load(f) or {}
        d["topics"]       = "\n".join(raw.get("topics", []))
        d["survey_terms"] = "\n".join(
            raw.get("survey_terms", ["survey", "review", "taxonomy", "overview"])
        )
        d["year_from"]    = raw.get("year_from",            d["year_from"])
        d["year_to"]      = raw.get("year_to",              d["year_to"])
        d["max_results"]  = raw.get("max_results_per_query", d["max_results"])
        d["top_n"]        = raw.get("top_n_to_summarize",   d["top_n"])
        d["min_score"]    = raw.get("min_quality_score",    d["min_score"])
        d["output_dir"]   = raw.get("output_dir",           d["output_dir"])
    return d


def _api_key_status() -> tuple[bool, str]:
    """Return (has_key, display_message) for the Anthropic API key."""
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except ImportError:
        pass
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        masked = key[:8] + "…" + key[-4:] if len(key) > 12 else key[:4] + "…"
        return True, f"Found  `{masked}`"
    return False, "Not set — LLM steps will be skipped"


def _write_config_yaml(run_cfg: dict, tmp_dir: str) -> str:
    """Serialise UI settings to a temporary topics.yaml and return its path."""
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
        "output_dir":                run_cfg["output_dir"],
        "architecture_enabled":      run_cfg["use_architecture"],
        "mega_architecture_enabled": run_cfg["use_architecture"],
    }
    path = os.path.join(tmp_dir, "topics_ui.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f, default_flow_style=False, allow_unicode=True)
    return path


def _build_cmd(run_cfg: dict, config_path: str) -> list[str]:
    """Build the CLI command list from the frozen run_cfg dict."""
    cmd = [sys.executable, str(PROJECT_ROOT / "main.py"), run_cfg["mode"]]
    cmd += ["--config",      config_path]
    cmd += ["--output-dir",  run_cfg["output_dir"]]
    cmd += ["--db",          run_cfg["db_path"]]
    cmd += ["--papers-file", run_cfg["papers_file"]]

    flag_map = {
        "--no-llm-queries":   not run_cfg["use_llm_queries"],
        "--no-llm-filter":    not run_cfg["use_llm_filter"],
        "--no-summarize":     not run_cfg["use_summarize"],
        "--no-judge":         not run_cfg["use_judge"],
        "--no-architecture":  not run_cfg["use_architecture"],
        "--no-pdf-parse":     not run_cfg["use_pdf_parse"],
        "--no-concept-graph": not run_cfg["use_concept_graph"],
        "--no-reading-path":  not run_cfg["use_reading_path"],
    }
    for flag, active in flag_map.items():
        if active:
            cmd.append(flag)
    # arXiv is opt-in (disabled by default)
    if run_cfg.get("use_arxiv"):
        cmd.append("--arxiv")
    return cmd


# ─────────────────────────────────────────────────────────────────────────────
# Session state bootstrap
# ─────────────────────────────────────────────────────────────────────────────

_STATE_DEFAULTS: dict = {
    "running":    False,
    "finished":   False,
    "log_lines":  [],
    "returncode": None,
    "run_cfg":    None,
    "tmp_dir":    None,
}
for _k, _v in _STATE_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────

DEFAULTS = _load_defaults()
has_key, key_msg = _api_key_status()

with st.sidebar:

    # ── Branding ─────────────────────────────────────────────────────────────
    st.markdown("## 🔬 Survey Paper Miner")
    st.caption("Configure your pipeline, then click **Run**.")

    # ── API key status ────────────────────────────────────────────────────────
    if has_key:
        st.success(f"🔑 API key: {key_msg}")
        api_key_override = ""
    else:
        st.warning(f"⚠️ API key: {key_msg}", icon="⚠️")
        api_key_override = st.text_input(
            "Enter Anthropic API Key",
            type="password",
            placeholder="sk-ant-…",
            help="Overrides ANTHROPIC_API_KEY for this run. Not saved to disk.",
        )

    st.divider()

    # ── Run mode ──────────────────────────────────────────────────────────────
    st.subheader("🚀 Run Mode")
    mode = st.radio(
        "Pipeline mode",
        options=["run", "fetch", "analyze"],
        format_func={
            "run":     "▶  Full Pipeline",
            "fetch":   "⬇  Fetch Only",
            "analyze": "🔍  Analyze Only",
        }.__getitem__,
        captions=[
            "Retrieve papers AND run all LLM analysis",
            "Retrieve & score papers → save to file",
            "Load saved papers → run LLM analysis",
        ],
        label_visibility="collapsed",
    )

    st.divider()

    # ── Topics ────────────────────────────────────────────────────────────────
    st.subheader("📚 Topics")
    topics_text = st.text_area(
        "One topic per line",
        value=DEFAULTS["topics"],
        height=190,
        placeholder=(
            "Large Language Models in education\n"
            "Generative AI for automated assessment\n"
            "LLM-based intelligent tutoring systems"
        ),
        help=(
            "Each line is a separate research topic. "
            "Papers are retrieved from OpenAlex and CORE for each topic "
            "(arXiv can be enabled in Search Settings)."
        ),
    )

    st.subheader("📋 Survey Terms")
    survey_terms_text = st.text_area(
        "One term per line",
        value=DEFAULTS["survey_terms"],
        height=110,
        help=(
            "Keywords that indicate a paper is a survey or review. "
            "Papers must contain at least one of these terms to pass the survey-signal filter."
        ),
    )

    st.divider()

    # ── Date range & search ───────────────────────────────────────────────────
    st.subheader("📅 Date Range")
    col_y1, col_y2 = st.columns(2)
    with col_y1:
        year_from = st.number_input("From", min_value=2000, max_value=2030,
                                    value=int(DEFAULTS["year_from"]), step=1)
    with col_y2:
        year_to = st.number_input("To", min_value=2000, max_value=2030,
                                  value=int(DEFAULTS["year_to"]), step=1)

    st.subheader("🔍 Search Settings")
    _default_max = max(int(DEFAULTS["max_results"]), 50)
    max_results = st.slider(
        "Max results per query", 10, 200, _default_max, 10,
        help=(
            "How many papers each source returns per search query. "
            "With 10 queries per topic and 1–2 sources, setting this below 50 "
            "usually produces fewer than 5 papers after all filters for broad topics."
        ),
    )

    use_arxiv = st.checkbox(
        "Include arXiv source",
        value=False,
        help=(
            "OpenAlex already indexes every arXiv paper with richer metadata "
            "(citation counts, DOIs). arXiv is disabled by default to avoid "
            "rate-limit errors (429s). Enable only if you need papers published "
            "in the past 1–2 days before OpenAlex picks them up."
        ),
    )
    if use_arxiv:
        st.caption("⚠️ arXiv may return 429 rate-limit errors if queried frequently.")
    top_n = st.slider(
        "Top N to summarise", 5, 100, int(DEFAULTS["top_n"]), 5,
        help="The highest-scoring N papers receive full LLM summarisation & analysis.",
    )
    min_score = st.slider(
        "Min quality score", 0.0, 100.0, float(DEFAULTS["min_score"]), 1.0,
        help="Papers ranked below this score are dropped before the LLM passes.",
    )

    st.divider()

    # ── Pipeline steps ────────────────────────────────────────────────────────
    st.subheader("⚙️ Pipeline Steps")
    st.caption("Uncheck to skip a step and reduce API cost / runtime.")

    use_llm_queries   = st.checkbox("LLM query generation",  value=True,
        help="Ask Claude to write diverse search queries. Off = simple keyword cross-product.")
    use_llm_filter    = st.checkbox("LLM relevance filter",  value=True,
        help="Pass 5b: LLM re-scores each candidate for topic relevance.")
    use_summarize     = st.checkbox("LLM summarisation",     value=True,
        help="Pass 7: extract structured summaries (scope, methods, findings…) for top-N papers.")
    use_judge         = st.checkbox("LLM-as-Judge",          value=True,
        help="Pass 8b: independent authority & quality assessment for each paper.")
    use_architecture  = st.checkbox("Architecture analysis", value=True,
        help="Pass 8: reverse-engineer how each survey organises its field; build mega-architecture.")
    use_pdf_parse     = st.checkbox("PDF full-text parsing", value=True,
        help="Pass 7b: download & parse PDFs to enrich architecture prompts (needs pdfplumber).")
    use_concept_graph = st.checkbox("Concept graph",         value=True,
        help="Pass 9: extract a typed concept graph linking key ideas across all surveys.")
    use_reading_path  = st.checkbox("Reading path",          value=True,
        help="Pass 9b: generate a sequenced newcomer reading list.")

    st.divider()

    # ── Storage paths ─────────────────────────────────────────────────────────
    st.subheader("📁 Storage Paths")
    output_dir  = st.text_input("Output directory",   value=DEFAULTS["output_dir"],
        help="Folder where CSV, XLSX, Markdown, and architecture reports are written.")
    db_path     = st.text_input("SQLite database",    value="data/processed/papers.db",
        help="Path to the SQLite file used to persist papers and summaries.")
    papers_file = st.text_input(
        "Papers file  *(Analyze mode)*",
        value="data/processed/papers_scored.jsonl",
        help="JSONL produced by a previous Fetch run. Only needed in Analyze Only mode.",
    )

    # Inline status for analyze mode
    if mode == "analyze":
        pf = PROJECT_ROOT / papers_file
        if pf.exists():
            kb = pf.stat().st_size // 1024
            st.caption(f"✓ File found  ({kb} KB)")
        else:
            st.error("⚠ File not found — run Fetch mode first.")


# ─────────────────────────────────────────────────────────────────────────────
# Main area
# ─────────────────────────────────────────────────────────────────────────────

st.title("🔬 Survey Paper Miner")
st.caption(
    "Automatically discover, rank, and deeply analyse AI survey papers "
    "using OpenAlex, CORE, and Claude."
)

# ── Validation ────────────────────────────────────────────────────────────────
topics_list = [t.strip() for t in topics_text.splitlines() if t.strip()]
_can_run = True

if not topics_list and mode != "analyze":
    st.warning("Enter at least one topic in the sidebar before running.", icon="⚠️")
    _can_run = False

if mode == "analyze" and not (PROJECT_ROOT / papers_file).exists():
    st.error(
        f"**Analyze Only** mode requires `{papers_file}`.  "
        "Run the pipeline in **Fetch** or **Full Pipeline** mode first.",
        icon="🚫",
    )
    _can_run = False

# ── Config summary ────────────────────────────────────────────────────────────
with st.expander(
    "📋 Configuration Summary",
    expanded=(not st.session_state.running and not st.session_state.finished),
):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Mode",            mode.upper())
    c2.metric("Topics",          len(topics_list))
    c3.metric("Year range",      f"{year_from} – {year_to}")
    c4.metric("Top N",           top_n)

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Max results/q",   max_results)
    c6.metric("Min quality",     min_score)
    active_steps = sum([
        use_llm_queries, use_llm_filter, use_summarize, use_judge,
        use_architecture, use_pdf_parse, use_concept_graph,
        use_reading_path,
    ])
    c7.metric("Active steps",    f"{active_steps} / 8")
    c8.metric("Output dir",      output_dir)

    if topics_list:
        st.markdown("**Topics:**")
        for t in topics_list:
            st.markdown(f"- `{t}`")

st.divider()

# ── Run / status row ──────────────────────────────────────────────────────────
run_col, status_col = st.columns([1, 3])

with run_col:
    _btn_label  = "⏳  Running…" if st.session_state.running else "▶  Run Pipeline"
    _btn_disabled = st.session_state.running or not _can_run
    run_btn = st.button(
        _btn_label,
        type="primary",
        disabled=_btn_disabled,
        use_container_width=True,
    )

with status_col:
    if st.session_state.running:
        st.info("Pipeline is running — live output below.", icon="⏳")
    elif st.session_state.finished:
        if st.session_state.returncode == 0:
            st.success("Pipeline completed successfully!", icon="✅")
        else:
            st.error(
                f"Pipeline failed (exit code {st.session_state.returncode}). "
                "See the log below for details.",
                icon="❌",
            )
    elif not _can_run:
        st.empty()
    else:
        st.info(
            {
                "run":     "**Full Pipeline** — fetch papers, then run all LLM passes.",
                "fetch":   "**Fetch Only** — retrieve & score papers, save to JSONL.",
                "analyze": "**Analyze Only** — load saved papers, run LLM analysis.",
            }[mode],
            icon="ℹ️",
        )

# ── Capture config at click time ──────────────────────────────────────────────
if run_btn and not st.session_state.running:
    st.session_state.run_cfg = dict(
        mode             = mode,
        topics_text      = topics_text,
        survey_terms_text= survey_terms_text,
        year_from        = int(year_from),
        year_to          = int(year_to),
        max_results      = int(max_results),
        top_n            = int(top_n),
        min_score        = float(min_score),
        output_dir       = output_dir,
        db_path          = db_path,
        papers_file      = papers_file,
        use_llm_queries  = use_llm_queries,
        use_llm_filter   = use_llm_filter,
        use_summarize    = use_summarize,
        use_judge        = use_judge,
        use_architecture = use_architecture,
        use_pdf_parse    = use_pdf_parse,
        use_concept_graph= use_concept_graph,
        use_reading_path = use_reading_path,
        use_arxiv        = use_arxiv,
        api_key_override = api_key_override,
    )
    st.session_state.running    = True
    st.session_state.finished   = False
    st.session_state.log_lines  = []
    st.session_state.returncode = None
    st.session_state.tmp_dir    = tempfile.mkdtemp()
    st.rerun()

# ── Live pipeline execution ───────────────────────────────────────────────────
if st.session_state.running and not st.session_state.finished:
    run_cfg    = st.session_state.run_cfg
    tmp_dir    = st.session_state.tmp_dir
    config_path = _write_config_yaml(run_cfg, tmp_dir)
    cmd        = _build_cmd(run_cfg, config_path)

    with st.expander("💻 Command", expanded=False):
        st.code(" ".join(cmd), language="bash")

    st.subheader("📡 Live Output")
    log_placeholder = st.empty()

    # Build process environment
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
    for raw in proc.stdout:          # blocks until each line arrives
        collected.append(raw.rstrip())
        # Show the most recent 80 lines so the box doesn't grow unbounded
        log_placeholder.code("\n".join(collected[-80:]), language="")

    proc.wait()

    st.session_state.log_lines  = collected
    st.session_state.running    = False
    st.session_state.finished   = True
    st.session_state.returncode = proc.returncode
    st.rerun()

# ── Post-run view ─────────────────────────────────────────────────────────────
if st.session_state.finished:

    # Full log (collapsed on success, expanded on failure)
    with st.expander(
        "📜 Full Run Log",
        expanded=(st.session_state.returncode != 0),
    ):
        st.code("\n".join(st.session_state.log_lines), language="")

    # Output files
    run_cfg_saved = st.session_state.run_cfg or {}
    out_path = PROJECT_ROOT / run_cfg_saved.get("output_dir", output_dir)
    db_file  = PROJECT_ROOT / run_cfg_saved.get("db_path", db_path)

    if out_path.exists():
        recent_files = sorted(
            (f for f in out_path.rglob("*") if f.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:40]

        if recent_files:
            st.subheader("📁 Output Files")

            # Group by extension for a cleaner display
            groups: dict[str, list[Path]] = {}
            for f in recent_files:
                groups.setdefault(f.suffix.lower() or "other", []).append(f)

            _ext_icons = {
                ".html": "🌐", ".md": "📝", ".xlsx": "📊",
                ".csv": "📄",  ".json": "🗃", ".jsonl": "🗃",
                ".png": "🖼",  ".mmd": "🔷",
            }

            for ext, files in sorted(groups.items()):
                icon = _ext_icons.get(ext, "📎")
                with st.expander(f"{icon} {ext.lstrip('.').upper() or 'Other'}  ({len(files)})",
                                 expanded=True):
                    for f in files:
                        size = f.stat().st_size
                        size_str = (
                            f"{size/1e6:.1f} MB" if size >= 1_000_000
                            else f"{size/1024:.1f} KB"
                        )
                        try:
                            rel = f.relative_to(PROJECT_ROOT)
                        except ValueError:
                            rel = f
                        st.markdown(
                            f"<span class='output-file'>`{rel}`</span>"
                            f"&emsp;<span style='color:#888'>{size_str}</span>",
                            unsafe_allow_html=True,
                        )

    if db_file.exists():
        kb = db_file.stat().st_size // 1024
        st.caption(f"🗄 Database: `{db_file.relative_to(PROJECT_ROOT)}`  ({kb} KB)")

    st.divider()

    # Reset button
    if st.button("🔄  Reset & Configure Again", use_container_width=False):
        for k, v in _STATE_DEFAULTS.items():
            st.session_state[k] = v if not isinstance(v, list) else []
        st.rerun()
