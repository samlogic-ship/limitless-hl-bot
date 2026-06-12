"""Cross-process file cache for slow-changing Limitless GET endpoints.

Mirrors hl_info.py. Several PM2 processes (daemon, shadow, maker, copy_shadow,
exiter) independently GET /markets/active (8 pages each) from the same IP and
collectively trip the per-IP rate limit (HTTP 429). Routing those GETs through
this on-disk TTL cache means identical (url, params) inside the TTL window costs
one upstream request total across every process.

On upstream failure (e.g. 429) a cached copy younger than STALE_MAX_FACTOR x TTL
is served stale instead of raising, so a transient rate-limit does not abort a
scan loop or emit a scan_error. A slightly old market list beats a missed scan.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

CACHE_DIR = Path(os.environ.get("LIMITLESS_HTTP_CACHE_DIR", "tmp/limitless_hl/http_cache"))
STALE_MAX_FACTOR = 8.0


def _cache_path(url: str, params: dict[str, Any] | None) -> Path:
    key = hashlib.sha1(
        (url + "?" + json.dumps(params or {}, sort_keys=True, separators=(",", ":"))).encode()
    ).hexdigest()
    return CACHE_DIR / f"{key}.json"


def _read(path: Path, max_age: float) -> Any:
    age = time.time() - path.stat().st_mtime
    if age >= max_age:
        raise OSError("cache expired")
    return json.loads(path.read_text(encoding="utf-8"))


def cached_get_json(session, url: str, params: dict[str, Any] | None = None,
                    *, ttl: float = 20.0, timeout: int = 15) -> Any:
    """GET url with a cross-process file cache. Returns parsed JSON.

    Serves fresh cache inside ttl; on upstream error serves stale cache up to
    STALE_MAX_FACTOR x ttl before re-raising.
    """
    path = _cache_path(url, params)
    if ttl > 0:
        try:
            return _read(path, ttl)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    try:
        resp = session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        if ttl > 0:
            try:
                return _read(path, ttl * STALE_MAX_FACTOR)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        raise
    if ttl > 0:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            pass
    return data
