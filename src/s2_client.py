"""
Shared Semantic Scholar client with a single global rate limiter.

Semantic Scholar allows **1 request per second, cumulative across all
endpoints** (whether or not you use an API key). Every S2 call in this project
— landmark resolution (search endpoint) and PDF resolution (paper endpoint) —
must go through `get()` here so that one process-wide gate enforces the limit
across all of them.

Set SEMANTIC_SCHOLAR_API_KEY (or S2_API_KEY) in the environment to authenticate;
without it the same 1 req/s limit applies but from a heavily-contended shared
pool, so 429s are common.
"""

from __future__ import annotations

import os
import threading
import time

import requests

API_KEY = (
    os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    or os.environ.get("S2_API_KEY")
    or ""
)

# S2's documented limit is 1 req/sec across all endpoints. Use a hair over 1s
# so we never exceed it even with clock jitter.
_MIN_INTERVAL = 1.1
_LOCK = threading.Lock()
_last_call: float = 0.0
_NO_PROXY = {"http": None, "https": None}


def get(
    url: str,
    params: dict | None = None,
    timeout: int = 20,
    retries: int | None = None,
) -> requests.Response | None:
    """
    Rate-limited GET to Semantic Scholar.

    Serialises all callers through a global lock and guarantees at least
    `_MIN_INTERVAL` seconds between consecutive requests to ANY S2 endpoint.
    Retries on HTTP 429 with linear back-off. Returns the final Response
    (which may still be a 429), or None on a network error.

    `retries` defaults to 4 when an API key is set, else 1 (the keyless pool
    429s constantly, so callers should fail fast to their fallback).
    """
    global _last_call
    headers = {"x-api-key": API_KEY} if API_KEY else {}
    n = retries if retries is not None else (4 if API_KEY else 1)

    resp: requests.Response | None = None
    for attempt in range(n):
        with _LOCK:
            gap = _MIN_INTERVAL - (time.monotonic() - _last_call)
            if gap > 0:
                time.sleep(gap)
            try:
                resp = requests.get(
                    url, params=params, headers=headers,
                    timeout=timeout, proxies=_NO_PROXY,
                )
            except Exception:
                _last_call = time.monotonic()
                return None
            finally:
                _last_call = time.monotonic()
        if resp.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        return resp
    return resp
