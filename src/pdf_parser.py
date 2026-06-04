"""
PDF full-text parser.

Downloads a paper's PDF (when a pdf_url is available), then uses pdfplumber
to extract the document structure: section headings, sub-headings, conclusion
text, future-work text, and table titles.

Heading detection uses font-size percentiles:
  - ≥ 90th percentile → top-level heading
  - ≥ 85th percentile (and < 90th) → sub-heading

Results are cached in data/raw/parsed_pdfs/<slug>.json so the network and
pdfplumber are only hit once per paper.

Usage:
    from src.pdf_parser import parse_papers
    parsed_map = parse_papers(scored_papers, cache_dir=Path("data/raw/parsed_pdfs"))
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

from .models import ParsedPaper, ScoredPaper

logger = logging.getLogger(__name__)

_ARXIV_DELAY = 3.0         # seconds between arXiv PDF downloads
_HTTP_TIMEOUT_ARXIV = 30   # arXiv is reliable — give it full time
_HTTP_TIMEOUT_OTHER = 15   # non-arXiv sources get a shorter leash
_HTTP_TIMEOUT_META  = 8    # Unpaywall / S2 metadata lookups — should be fast
_last_arxiv_download: float = 0.0

# Email sent with Unpaywall requests (required by their polite-pool policy).
_UNPAYWALL_EMAIL = "survey-miner@example.com"

# Suppress pdfminer rendering noise (invalid float colours, missing FontBBox,
# and other quirks of malformed PDFs). These warnings are harmless — pdfplumber
# still extracts text correctly. Silence the whole pdfminer tree plus the
# specific sub-loggers that emit at WARNING.
for _name in (
    "pdfminer",
    "pdfminer.pdfinterp",
    "pdfminer.pdfdocument",
    "pdfminer.pdfpage",
    "pdfminer.pdffont",
    "pdfminer.cmapdb",
    "pdfminer.layout",
):
    logging.getLogger(_name).setLevel(logging.ERROR)

# Domain prefixes that almost always return 403/401 for automated access.
# Papers from these sources are skipped early to avoid slow timeout waits.
_BLOCKED_DOMAINS = (
    "ieeexplore.ieee.org",
    "dl.acm.org",
    "onlinelibrary.wiley.com",
    "link.springer.com",
    "sciencedirect.com",
    "tandfonline.com",
    "journals.sagepub.com",
)

# Section title patterns that signal conclusions / future work
_CONCLUSION_RE = re.compile(
    r"^(conclusion|conclusions|summary and conclusion|concluding remarks?)\b",
    re.IGNORECASE,
)
_FUTURE_RE = re.compile(
    r"^(future work|future directions?|limitations and future|open problems?)\b",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_papers(
    scored_papers: list[ScoredPaper],
    cache_dir: Path,
) -> dict[str, ParsedPaper]:
    """
    Parse PDFs for a list of papers.

    Returns a mapping {paper_title: ParsedPaper}.  Papers without a pdf_url
    (and without an arXiv ID) are silently skipped (not included in the map).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, ParsedPaper] = {}
    no_url: list[str] = []

    for sp in scored_papers:
        p = sp.paper
        pdf_url = _resolve_pdf_url(p)
        if not pdf_url:
            no_url.append(p.title)
            result[p.title] = ParsedPaper(
                paper_title=p.title,
                parse_failed=True,
                failure_reason="No open-access PDF found (no arXiv ID, no OA link from OpenAlex/Unpaywall/S2)",
            )
            continue
        try:
            parsed = parse_paper(p.title, pdf_url, cache_dir, is_arxiv=bool(p.arxiv_id))
            result[p.title] = parsed
        except Exception as exc:
            _log_download_failure(p.title, pdf_url, exc)
            result[p.title] = ParsedPaper(
                paper_title=p.title,
                parse_failed=True,
                failure_reason=str(exc),
            )

    if no_url:
        logger.info(
            "[pdf_parser] %d paper(s) have no open-access PDF — skipping full-text parse: %s",
            len(no_url),
            "; ".join(t[:50] for t in no_url[:5]) + ("…" if len(no_url) > 5 else ""),
        )
    return result


def parse_paper(
    paper_title: str,
    pdf_url: str,
    cache_dir: Path,
    is_arxiv: bool = False,
) -> ParsedPaper:
    """
    Parse one PDF.  Returns a ParsedPaper (from cache if available).
    """
    slug = _paper_slug(paper_title)
    cache_path = cache_dir / f"{slug}.json"

    # Cache hit
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return ParsedPaper.model_validate(data)
        except Exception as exc:
            logger.debug("[pdf_parser] Cache load failed for %s: %s", slug, exc)

    # Download
    logger.info("[pdf_parser] Downloading PDF for '%s'", paper_title[:60])
    pdf_bytes = _download_pdf(pdf_url, is_arxiv=is_arxiv)

    # Parse
    structure = _extract_structure(pdf_bytes)
    parsed = ParsedPaper(
        paper_title=paper_title,
        sections=structure["sections"],
        subsections=structure["subsections"],
        conclusion_text=structure["conclusion_text"],
        future_work_text=structure["future_work_text"],
        table_titles=structure["table_titles"],
        parse_source="pdf",
    )

    # Write cache
    try:
        cache_path.write_text(
            json.dumps(parsed.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("[pdf_parser] Cache write failed for %s: %s", slug, exc)

    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_pdf_url(paper) -> str | None:
    """
    Return the best PDF URL for a paper, trying multiple sources in order.

    Priority:
      1. arXiv PDF          — always open-access, bot-friendly, reliable.
      2. Stored pdf_url     — from OpenAlex best_oa_location.
      3. Unpaywall API      — resolves open-access PDFs by DOI; covers
                             many papers that OpenAlex hasn't indexed as OA.
      4. Semantic Scholar   — has its own OA PDF index; useful when neither
                             OpenAlex nor Unpaywall has a link.

    Blocked domains are rejected at every step to avoid wasted attempts.
    """
    def _not_blocked(url: str) -> bool:
        return not any(d in url for d in _BLOCKED_DOMAINS)

    # 1. arXiv
    if paper.arxiv_id:
        return f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf"

    # 2. OpenAlex best_oa_location
    if paper.pdf_url and _not_blocked(paper.pdf_url):
        return paper.pdf_url

    if paper.pdf_url:
        logger.debug(
            "[pdf_parser] Skipping paywalled URL for '%s': %s",
            paper.title[:60], paper.pdf_url[:80],
        )

    # 3. Unpaywall (needs a DOI)
    if paper.doi:
        url = _unpaywall_pdf_url(paper.doi)
        if url and _not_blocked(url):
            logger.debug("[pdf_parser] Unpaywall hit for '%s'", paper.title[:60])
            return url

    # 4. Semantic Scholar open-access index
    url = _semantic_scholar_pdf_url(paper.doi, getattr(paper, "arxiv_id", None))
    if url and _not_blocked(url):
        logger.debug("[pdf_parser] Semantic Scholar hit for '%s'", paper.title[:60])
        return url

    return None


def _unpaywall_pdf_url(doi: str) -> str | None:
    """
    Query the Unpaywall API for an open-access PDF link.

    Unpaywall is free and requires only a valid email in the query string.
    Returns the `url_for_pdf` from the best OA location, or None.
    API docs: https://unpaywall.org/products/api
    """
    _NO_PROXY = {"http": None, "https": None}
    url = f"https://api.unpaywall.org/v2/{doi}?email={_UNPAYWALL_EMAIL}"
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT_META, proxies=_NO_PROXY)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()

        # Prefer best_oa_location; fall back to scanning all oa_locations
        for loc in [data.get("best_oa_location")] + (data.get("oa_locations") or []):
            if not loc:
                continue
            pdf = loc.get("url_for_pdf")
            if pdf:
                return pdf
    except Exception as exc:
        logger.debug("[pdf_parser] Unpaywall lookup failed for DOI %s: %s", doi, exc)
    return None


def _semantic_scholar_pdf_url(doi: str | None, arxiv_id: str | None) -> str | None:
    """
    Query the Semantic Scholar Graph API for an open-access PDF link.

    Prefers DOI lookup; falls back to arXiv ID.  Goes through the shared S2
    client so it shares the global 1 req/sec rate limit with landmark
    resolution (S2 limits requests cumulatively across all endpoints).
    """
    from . import s2_client

    _S2_BASE = "https://api.semanticscholar.org/graph/v1/paper"

    paper_id: str | None = None
    if doi:
        paper_id = f"DOI:{doi}"
    elif arxiv_id:
        paper_id = f"ArXiv:{arxiv_id}"

    if not paper_id:
        return None

    resp = s2_client.get(
        f"{_S2_BASE}/{paper_id}",
        params={"fields": "openAccessPdf"},
        timeout=_HTTP_TIMEOUT_META,
    )
    if resp is None or resp.status_code != 200:
        return None
    try:
        oa = resp.json().get("openAccessPdf") or {}
        return oa.get("url") or None
    except Exception as exc:
        logger.debug("[pdf_parser] S2 lookup failed for %s: %s", paper_id, exc)
    return None


def _paper_slug(title: str) -> str:
    """Convert a paper title to a filesystem-safe slug."""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:80] or "untitled"


def _download_pdf(url: str, is_arxiv: bool = False) -> bytes:
    """
    Download a PDF with rate-limiting for arXiv URLs.

    arXiv enforces a 1 req/3s guideline for PDFs; we respect it globally
    using a module-level timestamp.  Non-arXiv sources receive a shorter
    timeout and browser-like headers to improve open-access success rates.
    """
    global _last_arxiv_download

    is_arxiv_url = is_arxiv or "arxiv.org" in url

    if is_arxiv_url:
        now = time.monotonic()
        gap = _ARXIV_DELAY - (now - _last_arxiv_download)
        if gap > 0:
            time.sleep(gap)

    # Use realistic browser headers — improves success with MDPI, CORE, etc.
    headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }

    # CORE file-server needs a Referer from the CORE domain
    if "core.ac.uk" in url or "fileserver-az" in url:
        headers["Referer"] = "https://core.ac.uk/"

    timeout = _HTTP_TIMEOUT_ARXIV if is_arxiv_url else _HTTP_TIMEOUT_OTHER

    # Bypass any system/env proxy for PDF downloads.
    # Academic PDF hosts are public; routing them through a corporate proxy
    # often causes 503 / tunnel failures, and the content is not sensitive.
    _NO_PROXY = {"http": None, "https": None}

    resp = requests.get(
        url, headers=headers, timeout=timeout, allow_redirects=True, proxies=_NO_PROXY
    )
    resp.raise_for_status()

    if is_arxiv_url:
        _last_arxiv_download = time.monotonic()

    # Verify we actually got a PDF
    content_type = resp.headers.get("content-type", "")
    if "pdf" not in content_type and resp.content[:4] != b"%PDF":
        raise ValueError(f"URL did not return a PDF (content-type: {content_type})")

    return resp.content


def _log_download_failure(title: str, url: str, exc: Exception) -> None:
    """
    Log a PDF download/parse failure at an appropriate level.

    Expected / environment failures (paywalled, proxy issues, not-a-PDF) are
    logged at DEBUG so the console stays clean.  Genuinely unexpected errors
    (timeouts, unknown exceptions) remain at WARNING.
    """
    http_err = None
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        http_err = exc.response.status_code

    if http_err in (400, 403, 404):
        logger.debug(
            "[pdf_parser] HTTP %d (paywalled/unavailable) — skipping '%s'",
            http_err, title[:60],
        )
    elif "did not return a PDF" in str(exc):
        logger.debug("[pdf_parser] Non-PDF response — skipping '%s'", title[:60])
    elif isinstance(exc, requests.exceptions.ProxyError):
        logger.debug(
            "[pdf_parser] Proxy error — skipping '%s' (%s)", title[:60], url[:80]
        )
    elif isinstance(exc, requests.exceptions.ConnectionError):
        logger.debug(
            "[pdf_parser] Connection error — skipping '%s' (%s)", title[:60], url[:80]
        )
    elif isinstance(exc, requests.exceptions.Timeout):
        logger.warning("[pdf_parser] Timeout downloading '%s' (%s)", title[:60], url[:80])
    else:
        logger.warning("[pdf_parser] Failed to parse '%s': %s", title[:60], exc)


def _extract_structure(pdf_bytes: bytes) -> dict[str, Any]:
    """
    Use pdfplumber to extract headings, subheadings, conclusion text,
    future-work text, and table titles from a PDF.

    Heading detection strategy:
      1. Collect the font size of every character on every page.
      2. Compute the 85th and 90th percentile of the size distribution.
      3. Lines whose largest character is ≥ 90th percentile → top-level heading.
      4. Lines whose largest character is ≥ 85th percentile (but < 90th) → sub-heading.

    This avoids hard-coding pixel thresholds that vary between papers.
    """
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber is not installed. Run: pip install pdfplumber"
        )

    sections: list[str] = []
    subsections: dict[str, list[str]] = {}
    conclusion_text_parts: list[str] = []
    future_work_text_parts: list[str] = []
    table_titles: list[str] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # ── Step 1: collect all character sizes ──────────────────────────
        all_sizes: list[float] = []
        for page in pdf.pages[:40]:   # cap at 40 pages for performance
            for char in (page.chars or []):
                sz = char.get("size")
                if sz and isinstance(sz, (int, float)) and sz > 4:
                    all_sizes.append(float(sz))

        if not all_sizes:
            return _empty_structure()

        all_sizes_sorted = sorted(all_sizes)
        n = len(all_sizes_sorted)
        p85 = all_sizes_sorted[int(n * 0.85)]
        p90 = all_sizes_sorted[int(n * 0.90)]

        # ── Step 2: walk pages, group chars into lines, classify ─────────
        current_section: str | None = None
        in_conclusion = False
        in_future = False

        for page in pdf.pages[:40]:
            # Extract table titles (first line of each table bounding box)
            for table in (page.find_tables() or []):
                try:
                    rows = table.extract()
                    if rows and rows[0]:
                        caption = " ".join(str(c) for c in rows[0] if c)
                        if caption.strip():
                            table_titles.append(caption.strip()[:120])
                except Exception:
                    pass

            # Group characters into text lines
            lines = _chars_to_lines(page.chars or [])

            for line_text, max_size in lines:
                stripped = line_text.strip()
                if not stripped or len(stripped) < 2:
                    continue

                # Classify as heading / sub-heading / body
                if max_size >= p90 and len(stripped) < 120:
                    # Top-level heading
                    sections.append(stripped)
                    subsections.setdefault(stripped, [])
                    current_section = stripped
                    in_conclusion = bool(_CONCLUSION_RE.match(stripped))
                    in_future = bool(_FUTURE_RE.match(stripped))

                elif max_size >= p85 and len(stripped) < 120:
                    # Sub-heading
                    if current_section:
                        subsections.setdefault(current_section, []).append(stripped)
                    else:
                        sections.append(stripped)
                        subsections.setdefault(stripped, [])
                        current_section = stripped
                    # Check if this sub-heading signals future-work
                    if _FUTURE_RE.match(stripped):
                        in_future = True
                        in_conclusion = False

                else:
                    # Body text
                    if in_conclusion and len(conclusion_text_parts) < 20:
                        conclusion_text_parts.append(stripped)
                    if in_future and len(future_work_text_parts) < 20:
                        future_work_text_parts.append(stripped)

    return {
        "sections": sections,
        "subsections": subsections,
        "conclusion_text": " ".join(conclusion_text_parts)[:3000],
        "future_work_text": " ".join(future_work_text_parts)[:3000],
        "table_titles": table_titles[:20],
    }


def _chars_to_lines(chars: list[dict]) -> list[tuple[str, float]]:
    """
    Group pdfplumber character dicts into (line_text, max_font_size) pairs.

    Characters are sorted by (top, x0) — top is the y-position from the top
    of the page.  Characters with the same `top` value belong to the same line.
    We use a 2-point tolerance to handle slight vertical misalignment.
    """
    if not chars:
        return []

    # Sort by vertical position then horizontal
    sorted_chars = sorted(
        chars, key=lambda c: (round(c.get("top", 0) / 2) * 2, c.get("x0", 0))
    )

    lines: list[tuple[str, float]] = []
    current_text: list[str] = []
    current_sizes: list[float] = []
    current_top: float | None = None

    for char in sorted_chars:
        top = round(char.get("top", 0) / 2) * 2
        text = char.get("text", "")
        size = char.get("size", 0) or 0

        if current_top is None:
            current_top = top

        if abs(top - current_top) <= 4:
            current_text.append(text)
            current_sizes.append(size)
        else:
            if current_text:
                line = "".join(current_text)
                max_size = max(current_sizes) if current_sizes else 0
                lines.append((line, max_size))
            current_text = [text]
            current_sizes = [size]
            current_top = top

    if current_text:
        line = "".join(current_text)
        max_size = max(current_sizes) if current_sizes else 0
        lines.append((line, max_size))

    return lines


def _empty_structure() -> dict[str, Any]:
    return {
        "sections": [],
        "subsections": {},
        "conclusion_text": "",
        "future_work_text": "",
        "table_titles": [],
    }
