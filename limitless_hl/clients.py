from __future__ import annotations

import re
from typing import Any

import requests

from .model import LimitlessMarket, OrderBook
from .attribution import ResolvedMarket

UP_DOWN_MARKET_RE = re.compile(r"^([A-Z0-9]{2,12}) Up or Down - (5 Min|15 Min|Hourly|Daily|Weekly)$")


class LimitlessClient:
    def __init__(self, base_url: str = "https://api.limitless.exchange", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "limitless-hl/0.1"})

    def active_crypto_markets(self, pages: int = 8, limit: int = 25) -> list[LimitlessMarket]:
        markets: list[LimitlessMarket] = []
        for page in range(1, pages + 1):
            payload = self.session.get(
                f"{self.base_url}/markets/active",
                params={"page": page, "limit": min(limit, 25), "tradeType": "clob"},
                timeout=self.timeout,
            )
            payload.raise_for_status()
            rows = self.parse_active_markets(payload.json())
            markets.extend(rows)
        return markets

    def orderbook(self, slug: str) -> OrderBook:
        payload = self.session.get(f"{self.base_url}/markets/{slug}/orderbook", timeout=self.timeout)
        payload.raise_for_status()
        return self.parse_orderbook(payload.json())

    def market_details(self, slug: str) -> dict[str, Any]:
        payload = self.session.get(f"{self.base_url}/markets/{slug}", timeout=self.timeout)
        payload.raise_for_status()
        return payload.json()

    def resolved_market(self, slug: str) -> ResolvedMarket:
        payload = self.market_details(slug)
        return self.parse_resolved_market(payload, fallback_slug=slug)

    @classmethod
    def parse_active_markets(cls, payload: dict[str, Any]) -> list[LimitlessMarket]:
        out: list[LimitlessMarket] = []
        for row in payload.get("data") or []:
            market = cls.parse_market(row)
            if market is not None:
                out.append(market)
        return out

    @staticmethod
    def parse_market(row: dict[str, Any]) -> LimitlessMarket | None:
        title = str(row.get("title") or "")
        match = UP_DOWN_MARKET_RE.match(title)
        if not match:
            return None
        if row.get("tradeType") != "clob":
            return None
        metadata = row.get("metadata") or {}
        open_price = _to_float(metadata.get("openPrice"))
        expiration_ms = _to_int(row.get("expirationTimestamp"))
        slug = str(row.get("slug") or "")
        if not slug or open_price <= 0 or expiration_ms <= 0:
            return None
        return LimitlessMarket(
            slug=slug,
            title=title,
            symbol=match.group(1),
            interval=_normalize_interval(match.group(2)),
            threshold_price=open_price,
            expiration_ms=expiration_ms,
            open_price_captured_at=str(metadata.get("openPriceCapturedAt") or ""),
            volume_usdc=_to_float(row.get("volumeFormatted")),
            raw=dict(row),
        )

    @staticmethod
    def parse_orderbook(payload: dict[str, Any]) -> OrderBook:
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        up_bid = _to_float((bids[0] if bids else {}).get("price"))
        up_ask = _to_float((asks[0] if asks else {}).get("price"))
        up_ask_size = _normalize_size((asks[0] if asks else {}).get("size"))
        # For binary single markets, the opposite outcome is the complement of the UP book.
        down_bid = round(max(0.0, 1.0 - up_ask), 6) if up_ask else 0.0
        down_ask = round(max(0.0, 1.0 - up_bid), 6) if up_bid else 0.0
        down_ask_size = _normalize_size((bids[0] if bids else {}).get("size"))
        return OrderBook(
            up_bid=up_bid,
            up_ask=up_ask,
            down_bid=down_bid,
            down_ask=down_ask,
            up_ask_size=up_ask_size,
            down_ask_size=down_ask_size,
            raw=dict(payload),
        )

    @staticmethod
    def parse_resolved_market(payload: dict[str, Any], fallback_slug: str = "") -> ResolvedMarket:
        value = payload.get("winningOutcomeIndex")
        try:
            winning = int(value) if value is not None else None
        except (TypeError, ValueError):
            winning = None
        return ResolvedMarket(
            slug=str(payload.get("slug") or fallback_slug),
            winning_outcome_index=winning if winning in {0, 1} else None,
            raw=dict(payload),
        )


class HyperliquidClient:
    def __init__(self, base_url: str = "https://api.hyperliquid.xyz", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "limitless-hl/0.1"})

    def all_mids(self) -> dict[str, float]:
        response = self.session.post(
            f"{self.base_url}/info",
            json={"type": "allMids"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self.parse_all_mids(response.json())

    @staticmethod
    def parse_all_mids(payload: dict[str, Any]) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, value in payload.items():
            parsed = _to_float(value)
            if parsed > 0:
                out[str(key)] = parsed
        return out


def _normalize_interval(label: str) -> str:
    return {"5 Min": "5m", "15 Min": "15m", "Hourly": "1h", "Daily": "1d", "Weekly": "1w"}[label]


def _normalize_size(value: Any) -> float:
    size = _to_float(value)
    if size > 100_000:
        return size / 1_000_000.0
    return size


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
