"""Polymarket public-market feed for Limitless up/down twins.

Why this exists (verified live 2026-06-10):
  * Limitless flags its entire 15m lane ``metadata.isPolyArbitrage: true`` — the
    house market maker prices that lane against Polymarket. Reading Polymarket
    directly shows part of the counterparty's own reference.
  * Polymarket runs the same up/down windows on a deterministic slug grid:
    ``{sym}-updown-5m-{window_start_epoch}``  (btc/eth/sol/xrp)
    ``{sym}-updown-15m-{window_start_epoch}`` (all seven Limitless symbols)
    Window starts sit on the same UTC 5m/15m grid Limitless expiries use, so a
    Limitless market maps 1:1 to its Polymarket twin: same start, same end,
    both resolve "close vs window open" (feeds differ slightly — Limitless 5/15m
    uses the Chainlink CEX composite — so treat this as a signal, not free arb).

Fail-safe contract: every public method returns ``None`` on any problem
(timeout, missing twin, empty/wide book). Callers must treat ``None`` as
"no opinion", never as 0.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# Limitless symbol -> Polymarket slug prefix. 5m twins exist for a subset only.
PM_SYMBOLS = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp",
              "DOGE": "doge", "BNB": "bnb", "HYPE": "hype"}
PM_5M_SYMBOLS = {"BTC", "ETH", "SOL", "XRP"}
PM_INTERVAL_SECONDS = {"5m": 300, "15m": 900}


def pm_slug(symbol: str, interval: str, expiration_ms: int) -> str | None:
    """Deterministic Polymarket twin slug for a Limitless market, or None.

    The slug epoch is the WINDOW START (expiry minus duration) and must sit on
    the interval grid; off-grid expiries have no twin.
    """
    duration = PM_INTERVAL_SECONDS.get(interval)
    sym = PM_SYMBOLS.get((symbol or "").upper())
    if not duration or not sym:
        return None
    if interval == "5m" and symbol.upper() not in PM_5M_SYMBOLS:
        return None
    start = int(expiration_ms) // 1000 - duration
    if start % duration != 0:
        return None
    return f"{sym}-updown-{interval}-{start}"


class PolymarketFeed:
    """Implied up-probability from the Polymarket CLOB book, cached briefly."""

    def __init__(
        self,
        timeout: float = 4.0,
        max_spread: float = 0.12,
        cache_ttl_seconds: float = 2.0,
    ):
        self.timeout = timeout
        self.max_spread = max_spread
        self.cache_ttl_seconds = cache_ttl_seconds
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._token_cache: dict[str, tuple[str, str] | None] = {}
        self._prob_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    # -- internals ----------------------------------------------------------

    def _get(self, url: str) -> Any | None:
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200 or not resp.text.strip():
                return None
            return resp.json()
        except Exception:
            return None

    def _up_down_tokens(self, slug: str) -> tuple[str, str] | None:
        """(up_token_id, down_token_id) for a twin slug. Cached forever (immutable)."""
        if slug in self._token_cache:
            return self._token_cache[slug]
        result: tuple[str, str] | None = None
        events = self._get(f"{GAMMA_URL}/events?slug={slug}")
        if isinstance(events, list) and events:
            markets = events[0].get("markets") or []
            if markets:
                m = markets[0]
                try:
                    token_ids = json.loads(m.get("clobTokenIds") or "[]")
                    raw_outcomes = m.get("outcomes")
                    outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else (raw_outcomes or [])
                    up_idx = 0
                    for i, name in enumerate(outcomes):
                        if str(name).strip().lower() == "up":
                            up_idx = i
                            break
                    if len(token_ids) >= 2:
                        result = (str(token_ids[up_idx]), str(token_ids[1 - up_idx]))
                except Exception:
                    result = None
        self._token_cache[slug] = result
        return result

    def _book_mid(self, token_id: str) -> dict[str, float] | None:
        """Mid/spread from the live CLOB book; None on empty or wide book."""
        book = self._get(f"{CLOB_URL}/book?token_id={token_id}")
        if not isinstance(book, dict):
            return None
        try:
            bids = [float(b["price"]) for b in (book.get("bids") or [])]
            asks = [float(a["price"]) for a in (book.get("asks") or [])]
        except Exception:
            return None
        if not bids or not asks:
            return None
        best_bid, best_ask = max(bids), min(asks)
        spread = best_ask - best_bid
        if spread < 0 or spread > self.max_spread:
            return None
        return {"bid": best_bid, "ask": best_ask, "mid": (best_bid + best_ask) / 2.0, "spread": spread}

    # -- public -------------------------------------------------------------

    def implied_up_prob(self, symbol: str, interval: str, expiration_ms: int) -> dict[str, Any] | None:
        """Polymarket's implied P(up) for the twin of a Limitless market.

        Returns {"up_prob", "bid", "ask", "spread", "slug", "ts_ms"} or None.
        """
        slug = pm_slug(symbol, interval, expiration_ms)
        if slug is None:
            return None
        now = time.time()
        cached = self._prob_cache.get(slug)
        if cached is not None and now - cached[0] < self.cache_ttl_seconds:
            return cached[1]
        result: dict[str, Any] | None = None
        tokens = self._up_down_tokens(slug)
        if tokens is not None:
            mid = self._book_mid(tokens[0])
            if mid is not None:
                result = {
                    "up_prob": mid["mid"],
                    "bid": mid["bid"],
                    "ask": mid["ask"],
                    "spread": mid["spread"],
                    "slug": slug,
                    "ts_ms": int(now * 1000),
                }
        self._prob_cache[slug] = (now, result)
        return result
