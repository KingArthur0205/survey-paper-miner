"""
Query result cache.

Persists raw API results to disk so that re-runs skip queries that were
already answered.  This is the single biggest reduction in API calls during
development, config tuning, and topic expansion.

Storage:
  data/raw/query_cache.json  — one JSON object, keys are cache keys

Cache key:
  SHA-256 of "<source>|<query_string>|<year_from>|<year_to>|<limit>"

TTL:
  Configurable; default 7 days.  Stale entries are evicted on load.
  Set ttl_days=0 to disable expiry (keep results indefinitely).

Usage:
    cache = QueryCache()                        # load existing cache
    hit = cache.get("semantic_scholar", q, ...) # None on miss
    if hit is None:
        papers = retriever._fetch(...)
        cache.set("semantic_scholar", q, ..., papers)
    cache.save()                                # flush to disk
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Paper

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("data/raw/query_cache.json")
_DEFAULT_TTL_DAYS = 7


class QueryCache:
    def __init__(
        self,
        path: str | Path = _DEFAULT_PATH,
        ttl_days: int = _DEFAULT_TTL_DAYS,
    ):
        self._path = Path(path)
        self._ttl = timedelta(days=ttl_days) if ttl_days > 0 else None
        self._store: dict[str, dict] = {}  # key → {papers: [...], cached_at: ISO str}
        self._hits = 0
        self._misses = 0
        self._load()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get(
        self,
        source: str,
        query: str,
        year_from: int,
        year_to: int,
        limit: int,
    ) -> list[Paper] | None:
        """Return cached papers, or None on cache miss / expired entry."""
        key = _make_key(source, query, year_from, year_to, limit)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None

        if self._is_expired(entry):
            del self._store[key]
            self._misses += 1
            logger.debug("Cache expired: source=%s query=%r", source, query)
            return None

        self._hits += 1
        papers = [Paper(**p) for p in entry["papers"]]
        logger.debug(
            "Cache hit: source=%s query=%r → %d papers", source, query, len(papers)
        )
        return papers

    def set(
        self,
        source: str,
        query: str,
        year_from: int,
        year_to: int,
        limit: int,
        papers: list[Paper],
    ) -> None:
        """Store papers for a query."""
        key = _make_key(source, query, year_from, year_to, limit)
        self._store[key] = {
            "papers": [p.model_dump() for p in papers],
            "cached_at": datetime.utcnow().isoformat(),
            "source": source,
            "query": query,
        }

    def save(self) -> None:
        """Flush the in-memory store to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(self._store, f, indent=2, ensure_ascii=False)
        logger.info(
            "Cache saved: %d entries, %d hits / %d misses this run",
            len(self._store), self._hits, self._misses,
        )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "total_entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
        }

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self._path.exists():
            logger.debug("No cache file found at %s — starting fresh", self._path)
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                raw = json.load(f)
            # Evict expired entries on load to keep the file small
            now = datetime.utcnow()
            kept = {}
            evicted = 0
            for key, entry in raw.items():
                if self._is_expired(entry):
                    evicted += 1
                else:
                    kept[key] = entry
            self._store = kept
            logger.info(
                "Cache loaded: %d entries (%d expired entries evicted)",
                len(self._store), evicted,
            )
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Cache file corrupt — starting fresh. Error: %s", exc)
            self._store = {}

    def _is_expired(self, entry: dict) -> bool:
        if self._ttl is None:
            return False
        try:
            cached_at = datetime.fromisoformat(entry["cached_at"])
            return datetime.utcnow() - cached_at > self._ttl
        except (KeyError, ValueError):
            return True


def _make_key(
    source: str, query: str, year_from: int, year_to: int, limit: int
) -> str:
    raw = f"{source}|{query}|{year_from}|{year_to}|{limit}"
    return hashlib.sha256(raw.encode()).hexdigest()
