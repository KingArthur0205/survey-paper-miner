"""
File-based cache for LLM call results.

Avoids re-calling the Anthropic API for papers that have already been processed
in a previous run.  Each cache entry is one JSON file named by the SHA-256
content hash of the inputs, stored under `data/cache/llm/<namespace>/`.

Design choices:
  - Flat directory per namespace — no nested structure to manage.
  - Content-addressed keys (hash of title + abstract + model): the same paper
    fetched from different sources still gets a cache hit.
  - Cache is intentionally NOT invalidated automatically.  Delete the namespace
    directory to force a full refresh:
        rm -rf data/cache/llm/summaries
  - Thread-safe for concurrent reads; writes are atomic via a temp-file rename.

Usage:
    cache = LLMCache("data/cache/llm/summaries")

    key = LLMCache.make_key(paper.title, paper.abstract or "", model)
    data = cache.get(key)
    if data is None:
        data = call_llm(...)
        cache.set(key, data, label=paper.title[:60], model=model)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class LLMCache:
    """Simple per-namespace file-based cache for LLM JSON responses."""

    def __init__(self, cache_dir: str | Path) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._hits = 0
        self._misses = 0

    # ── Key construction ──────────────────────────────────────────────────────

    @staticmethod
    def make_key(*parts: str) -> str:
        """
        Return a 16-char hex key derived from one or more string parts.

        All parts are joined with '|' before hashing, so the order matters.
        Example:
            key = LLMCache.make_key(paper.title, paper.abstract or "", model)
        """
        combined = "|".join(str(p) for p in parts)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    # ── Read / write ──────────────────────────────────────────────────────────

    def get(self, key: str) -> dict | None:
        """
        Return the cached payload dict, or None on a miss / read error.

        Logs at DEBUG so cache hits are visible with -v but silent by default.
        """
        path = self._dir / f"{key}.json"
        if not path.exists():
            self._misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._hits += 1
            logger.debug("[llm_cache] HIT  %s  (%s)", key, payload.get("label", ""))
            return payload.get("data")
        except Exception as exc:
            logger.debug("[llm_cache] Read error for %s: %s", key, exc)
            self._misses += 1
            return None

    def set(
        self,
        key: str,
        data: dict,
        *,
        label: str = "",
        model: str = "",
    ) -> None:
        """
        Write data to cache atomically.

        Uses a temp-file rename so a concurrent read never sees a partial write.
        Silently skips on any filesystem error (caching is best-effort).
        """
        path = self._dir / f"{key}.json"
        payload = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "label": label,
            "data": data,
        }
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)   # atomic on POSIX; near-atomic on Windows
            logger.debug("[llm_cache] SET  %s  (%s)", key, label)
        except Exception as exc:
            logger.debug("[llm_cache] Write error for %s: %s", key, exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Stats / maintenance ───────────────────────────────────────────────────

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def size(self) -> int:
        """Number of entries currently on disk."""
        return sum(1 for _ in self._dir.glob("*.json"))

    def clear(self) -> int:
        """Delete all entries and return how many were removed."""
        removed = 0
        for p in self._dir.glob("*.json"):
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
        return removed
