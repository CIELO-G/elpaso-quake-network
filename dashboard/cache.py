"""TTL + file-mtime invalidated response cache with ETag helpers.

Two layers: an in-process dict caches the response data (cheap dependency-
free TTL), and the ETag header lets browsers skip re-downloading bodies
they already have. ``watch_file`` invalidates the cache when an underlying
file changes, so saving the catalog doesn't serve stale rows for ttl seconds.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from dashboard.deps import file_mtime


class _CacheEntry:
    __slots__ = ("data", "etag", "created_at", "file_mtime")

    def __init__(self, data, etag: str, file_mtime_: Optional[float]) -> None:
        self.data = data
        self.etag = etag
        self.created_at = time.monotonic()
        self.file_mtime = file_mtime_


_cache: dict[str, _CacheEntry] = {}


def get_cached(
    key: str, ttl: float, watch_file: Optional[Path] = None
) -> Optional[_CacheEntry]:
    """Return a cache entry if it's fresh (within TTL and file mtime matches)."""
    entry = _cache.get(key)
    if entry is None:
        return None
    if (time.monotonic() - entry.created_at) > ttl:
        return None
    if watch_file is not None:
        current_mtime = file_mtime(watch_file)
        if current_mtime != entry.file_mtime:
            return None
    return entry


def set_cached(key: str, data, watch_file: Optional[Path] = None) -> str:
    """Store ``data`` under ``key`` and return its ETag (md5 of JSON body)."""
    mtime = file_mtime(watch_file) if watch_file else None
    etag_raw = json.dumps(data, sort_keys=True, default=str)
    etag = hashlib.md5(etag_raw.encode()).hexdigest()
    _cache[key] = _CacheEntry(data, etag, mtime)
    return etag


def etag_response(data, etag: str, request: Request) -> Response:
    """Return 304 if the client's If-None-Match matches, else a 200 with body."""
    if_none_match = request.headers.get("if-none-match", "")
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=data, headers={"ETag": etag})


def clear_all() -> None:
    """Drop every cached entry. Called after a write (catalog save / review)."""
    _cache.clear()
