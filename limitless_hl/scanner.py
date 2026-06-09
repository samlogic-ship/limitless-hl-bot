from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any
from typing import Protocol

from .model import Candidate, EdgeConfig, LimitlessMarket, OrderBook, choose_candidate


class PricingLike(Protocol):
    def vol_for(self, symbol: str) -> float:
        ...

    def up_shade_for(self, symbol: str) -> float:
        ...

    def ref_price(self, symbol: str, hyperliquid_mid: float) -> float:
        ...


class LimitlessLike(Protocol):
    def active_crypto_markets(self) -> list[LimitlessMarket]:
        ...

    def orderbook(self, slug: str) -> OrderBook:
        ...


class HyperliquidLike(Protocol):
    def all_mids(self) -> dict[str, float]:
        ...


class LimitlessHyperliquidScanner:
    def __init__(
        self,
        limitless: LimitlessLike,
        hyperliquid: HyperliquidLike,
        config: EdgeConfig,
        pricing: PricingLike | None = None,
    ):
        self.limitless = limitless
        self.hyperliquid = hyperliquid
        self.config = config
        self.pricing = pricing

    def scan(self, now_ms: int | None = None) -> list[Candidate]:
        return [Candidate(**row) for row in self.scan_report(now_ms=now_ms)["candidates"]]

    def scan_report(self, now_ms: int | None = None) -> dict[str, Any]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        mids = self.hyperliquid.all_mids()
        candidates: list[Candidate] = []
        rejections: list[dict[str, Any]] = []
        books: list[dict[str, Any]] = []
        markets = [market for market in self.limitless.active_crypto_markets() if market is not None]
        for market in markets:
            mid = mids.get(market.symbol)
            if mid is None:
                rejections.append({"slug": market.slug, "symbol": market.symbol, "reason": "missing_hyperliquid_mid"})
                continue
            try:
                book = self.limitless.orderbook(market.slug)
            except Exception as exc:
                rejections.append({"slug": market.slug, "symbol": market.symbol, "reason": "orderbook_error", "error": str(exc)})
                continue
            vol = shade = ref = None
            if self.pricing is not None:
                try:
                    vol = self.pricing.vol_for(market.symbol)
                    shade = self.pricing.up_shade_for(market.symbol)
                    ref = self.pricing.ref_price(market.symbol, mid)
                except Exception:
                    vol = shade = ref = None
            books.append({
                "ts_ms": now,
                "slug": market.slug,
                "symbol": market.symbol,
                "interval": market.interval,
                "seconds_to_expiry": int((market.expiration_ms - now) / 1000),
                "threshold": market.threshold_price,
                "up_bid": book.up_bid, "up_ask": book.up_ask,
                "down_bid": book.down_bid, "down_ask": book.down_ask,
                "up_ask_size": book.up_ask_size, "down_ask_size": book.down_ask_size,
                "hl_mid": mid, "ref_price": ref, "vol": vol, "shade": shade,
            })
            candidate = choose_candidate(
                market=market,
                book=book,
                hyperliquid_mid=mid,
                now_ms=now,
                config=self.config,
                annualized_volatility=vol,
                up_probability_shade=shade or 0.0,
                reference_price=ref,
            )
            if candidate is not None:
                candidates.append(candidate)
            else:
                rejections.append({"slug": market.slug, "symbol": market.symbol, "reason": "no_positive_edge"})
        candidates.sort(key=lambda item: item.expected_value_usdc, reverse=True)
        return {
            "scanned_at_ms": now,
            "market_count": len(markets),
            "candidate_count": len(candidates),
            "rejected_count": len(rejections),
            "candidates": [candidate.to_dict() for candidate in candidates],
            "rejections": rejections[:50],
            "books": books,
            "config": asdict(self.config),
        }
