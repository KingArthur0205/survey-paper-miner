"""
Abstract base class that every retriever must implement.

Two cross-cutting concerns are handled here so subclasses stay simple:

  - Cache layer  : `search()` checks the QueryCache before calling `_fetch()`.
                   On a cache hit the network is never touched.  Results are
                   written back to the cache after a successful fetch.

  - Retry logic  : `_get()` retries on network errors, 429, 502, 503, 504
                   using tenacity with exponential back-off (2–15 s).
                   500 errors are not retried (query syntax error — no point).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from ..models import Paper

logger = logging.getLogger(__name__)

# Shared session with a descriptive User-Agent
_session = requests.Session()
_session.headers.update({
    "User-Agent": "AI-Survey-Paper-Miner/0.1 (research tool; contact via GitHub)"
})


class BaseRetriever(ABC):
    source_name: str = "unknown"

    def __init__(self):
        # Injected by main.py after construction; None = caching disabled
        self._cache = None

    def search(
        self,
        query: str,
        topic: str,
        year_from: int,
        year_to: int,
        limit: int,
    ) -> list[Paper]:
        """
        Public entry point.

        1. Check cache — return immediately on hit.
        2. Call _fetch() — the real network request.
        3. Write result to cache.
        4. On any exception, log and return [] so one failure doesn't abort
           the whole pipeline.
        """
        # ── Cache check ──────────────────────────────────────────────────
        if self._cache is not None:
            cached = self._cache.get(
                self.source_name, query, year_from, year_to, limit
            )
            if cached is not None:
                return cached

        # ── Live fetch ───────────────────────────────────────────────────
        try:
            papers = self._fetch(query, topic, year_from, year_to, limit)
        except Exception as exc:
            # HTTP 500 almost always means the query syntax was rejected by the
            # remote API (e.g. CORE's query parser) — it's not a transient error
            # and happens for every query to that source, so log at DEBUG to
            # avoid flooding the output.
            if self._is_http_500(exc):
                logger.debug(
                    "[%s] Query skipped (500 — server rejected query). query=%r",
                    self.source_name, query,
                )
            else:
                logger.warning(
                    "[%s] Query failed — skipping. query=%r error=%s",
                    self.source_name, query, exc,
                )
            return []

        # ── Write-through cache ──────────────────────────────────────────
        if self._cache is not None:
            self._cache.set(
                self.source_name, query, year_from, year_to, limit, papers
            )

        return papers

    @abstractmethod
    def _fetch(
        self,
        query: str,
        topic: str,
        year_from: int,
        year_to: int,
        limit: int,
    ) -> list[Paper]:
        """Subclasses implement this; may raise freely."""
        ...

    @staticmethod
    def _is_http_500(exc: BaseException) -> bool:
        return (
            isinstance(exc, requests.HTTPError)
            and exc.response is not None
            and exc.response.status_code == 500
        )

    @staticmethod
    def _get(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        """HTTP GET with retry. Returns parsed JSON. Raises on persistent failure."""
        return _retried_get(url, params=params, headers=headers)

    @staticmethod
    def _post(url: str, json: dict | None = None, headers: dict | None = None) -> dict:
        """HTTP POST with retry. Returns parsed JSON. Raises on persistent failure."""
        return _retried_post(url, json=json, headers=headers)

    @staticmethod
    def _get_text(url: str, params: dict | None = None, headers: dict | None = None) -> str:
        """HTTP GET with retry. Returns raw response text (for XML APIs). Raises on persistent failure."""
        return _retried_get_text(url, params=params, headers=headers)


# ─────────────────────────────────────────────────────────────────────────────
# Retry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_retryable(exc: BaseException) -> bool:
    """
    Retry on network errors, timeouts, and genuinely transient HTTP errors.

    429 — rate limited (always worth retrying after back-off)
    502 — bad gateway / reverse-proxy hiccup
    503 — service temporarily unavailable
    504 — gateway timeout

    500 is intentionally excluded: it almost always means the request itself
    is bad (e.g. a query-parser error on CORE), not a transient server blip.
    Retrying a 500 just burns time without any chance of success.
    """
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (429, 502, 503, 504)
    return False


def _handle_rate_limit(resp: requests.Response) -> None:
    """Log 429 rate-limit responses.

    We deliberately do NOT sleep here.  The old pattern of sleeping for the
    full Retry-After duration (typically 15 s) and *then* re-raising caused
    tenacity to add its own exponential back-off on top, so a single failing
    query could block the thread for 60 s or more with three retry attempts.

    tenacity's wait_exponential (min=2 s, max=15 s) already provides a
    reasonable delay between attempts without the double-penalty.
    """
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            logger.warning(
                "Rate-limited by %s (Retry-After: %ss) — tenacity will back off",
                resp.url, retry_after,
            )
        else:
            logger.warning("Rate-limited by %s — tenacity will back off", resp.url)


_RETRY_KWARGS = dict(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


@retry(**_RETRY_KWARGS)
def _retried_get(url: str, params: dict | None, headers: dict | None) -> dict:
    resp = _session.get(url, params=params, headers=headers, timeout=30)
    _handle_rate_limit(resp)
    resp.raise_for_status()
    return resp.json()


@retry(**_RETRY_KWARGS)
def _retried_get_text(url: str, params: dict | None, headers: dict | None) -> str:
    """Like _retried_get but returns raw text instead of JSON (for XML APIs)."""
    resp = _session.get(url, params=params, headers=headers, timeout=30)
    _handle_rate_limit(resp)
    resp.raise_for_status()
    return resp.text


@retry(**_RETRY_KWARGS)
def _retried_post(url: str, json: dict | None, headers: dict | None) -> dict:
    resp = _session.post(url, json=json, headers=headers, timeout=30)
    _handle_rate_limit(resp)
    resp.raise_for_status()
    return resp.json()
