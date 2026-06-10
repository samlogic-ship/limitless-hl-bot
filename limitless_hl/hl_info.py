"""
limitless_hl/hl_info.py — shared, file-cached Hyperliquid /info fetcher.

Several PM2 processes (daemon, shadow, maker, funding, tgbot, scorer features)
poll api.hyperliquid.xyz/info independently from the same IP and collectively
trip the per-IP rate limit (HTTP 429; 60+ hits on 2026-06-09). This module
routes /info POSTs through a small on-disk TTL cache shared by every process,
so identical payloads inside the TTL window cost one upstream request total.

Behavior on upstream failure: if a cached copy exists and is younger than
STALE_MAX_FACTOR x TTL, serve it stale instead of raising. A slightly old mid
beats a missed scan cycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
CACHE_DIR = Path(os.environ.get("LIMITLESS_HL_CACHE_DIR", "tmp/limitless_hl/hl_info_cache"))

# TTL per payload type, seconds. Anything unknown gets DEFAULT_TTL.
TTLS: dict[str, float] = {
    "allMids": 3.0,
    "metaAndAssetCtxs": 10.0,
    "candleSnapshot": 20.0,
}
DEFAULT_TTL = 5.0
STALE_MAX_FACTOR = 10.0

_SESSION = requests.Session()
_SESSION.headers.update({"Content-Type": "application/json"})


def _cache_path(payload: dict[str, Any]) -> Path:
    key = hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CACHE_DIR / f"{key}.json"


def _read_cache(path: Path, max_age: float) -> Any:
    age = time.time() - path.stat().st_mtime
    if age >= max_age:
        raise OSError("cache expired")
    return json.loads(path.read_text(encoding="utf-8"))


def post_info(
    payload: dict[str, Any],
    *,
    ttl_seconds: float | None = None,
    timeout: int = 10,
) -> Any:
    """POST to HL /info with a cross-process file cache. Returns parsed JSON."""
    ttl = TTLS.get(str(payload.get("type")), DEFAULT_TTL) if ttl_seconds is None else ttl_seconds
    path = _cache_path(payload)

    if ttl > 0:
        try:
            return _read_cache(path, ttl)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    try:
        resp = _SESSION.post(HL_INFO_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # Upstream down or rate-limited: serve stale cache if tolerably fresh.
        if ttl > 0:
            try:
                return _read_cache(path, ttl * STALE_MAX_FACTOR)
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
