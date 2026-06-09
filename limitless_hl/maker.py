"""limitless_hl/maker.py — passive maker engine for Limitless Up/Down markets.

Edge thesis (study 2026-06-09, tmp/study/ + book_snapshots.jsonl):
- Makers pay ZERO fees and receive 100% of taker fees as rebates on
  15m/hourly/daily markets; takers pay 3.00% at <=$0.50. We switch sides
  of that fee flow.
- Hourly books carry 7-30c spreads (BNB 30c, HYPE 21c, DOGE 12c) around a
  house MM quoting ~2.5c margin per side on the tight books and much wider
  on the rest. Quoting fair +/- margin from our calibrated v2 model sits
  inside that band, top-of-book, on the markets with real taker volume
  (~$32K/market hourly vs $3-450 on 5-min).
- Structural safety: we only BID both outcomes. bid_up + bid_down =
  1 - 2*margin < 1, so a double fill locks in 2*margin minus model error.

Inventory is capped per symbol and skews quotes against the loaded side.
On shutdown all resting orders are cancelled. No pause files — pm2 stop.
"""
from __future__ import annotations

import argparse
import json
import os
import signal as _signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from .clients import LimitlessClient
from .live_trade import (
    LimitlessCredentials,
    LimitlessOrderBuilder,
    LimitlessOrderIntent,
    LimitlessSubmitter,
    sign_hmac_headers,
)
from .model import LimitlessMarket, OrderBook, estimate_binary_probability
from .secrets import get_secret
from .volatility import PricingProvider

BASE_URL = "https://api.limitless.exchange"
PRICE_TICK = 0.01


# ---------------------------------------------------------------------------
# Config and plans
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MakerConfig:
    intervals: tuple[str, ...] = ("1h",)
    symbols: tuple[str, ...] = ("BTC", "ETH", "SOL", "HYPE", "BNB", "DOGE", "XRP")
    margin: float = 0.05
    quote_size_usdc: float = 2.0
    reprice_threshold: float = 0.015
    min_price: float = 0.07
    max_price: float = 0.88
    min_seconds_to_expiry: int = 600
    max_seconds_to_expiry: int = 4 * 60 * 60
    max_inventory_usdc_per_symbol: float = 6.0
    max_total_locked_usdc: float = 14.0
    inventory_skew: float = 0.04
    max_markets: int = 5


@dataclass(frozen=True, slots=True)
class QuotePlan:
    slug: str
    symbol: str
    interval: str
    side: str  # UP | DOWN — which outcome token we BID on
    price: float
    size_usdc: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.slug, self.side)


@dataclass(slots=True)
class OpenOrder:
    order_id: str
    slug: str
    side: str
    price: float
    size_usdc: float
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure quote logic (unit tested)
# ---------------------------------------------------------------------------

def compute_quotes(
    market: LimitlessMarket,
    book: OrderBook,
    fair_up: float,
    inventory_usdc: float,
    config: MakerConfig,
    seconds_to_expiry: int,
) -> list[QuotePlan]:
    """Bid both outcomes at (skew-adjusted fair) - margin, never crossing the book."""
    if seconds_to_expiry < config.min_seconds_to_expiry:
        return []
    if seconds_to_expiry > config.max_seconds_to_expiry:
        return []

    # Inventory skew: positive inventory_usdc = long UP exposure → mark UP down.
    load = max(-1.0, min(1.0, inventory_usdc / max(config.max_inventory_usdc_per_symbol, 1e-9)))
    fair_adj = fair_up - config.inventory_skew * load

    plans: list[QuotePlan] = []
    for side, fair_side, ask in (
        ("UP", fair_adj, book.up_ask),
        ("DOWN", 1.0 - fair_adj, book.down_ask),
    ):
        # Don't add to a maxed-out side.
        if side == "UP" and load >= 1.0:
            continue
        if side == "DOWN" and load <= -1.0:
            continue
        bid = fair_side - config.margin
        if ask and ask > 0:
            bid = min(bid, ask - PRICE_TICK)  # post-only safety: stay below the ask
        bid = round(round(bid / PRICE_TICK) * PRICE_TICK, 2)
        if bid < config.min_price or bid > config.max_price:
            continue
        plans.append(
            QuotePlan(
                slug=market.slug,
                symbol=market.symbol,
                interval=market.interval,
                side=side,
                price=bid,
                size_usdc=config.quote_size_usdc,
            )
        )
    return plans


def diff_orders(
    desired: list[QuotePlan],
    open_orders: list[OpenOrder],
    reprice_threshold: float,
) -> tuple[list[OpenOrder], list[QuotePlan]]:
    """Return (orders to cancel, plans to post)."""
    desired_by_key = {plan.key: plan for plan in desired}
    open_by_key: dict[tuple[str, str], list[OpenOrder]] = {}
    for order in open_orders:
        open_by_key.setdefault((order.slug, order.side), []).append(order)

    cancels: list[OpenOrder] = []
    posts: list[QuotePlan] = []

    for key, orders in open_by_key.items():
        plan = desired_by_key.get(key)
        keep_one = False
        for order in orders:
            if (
                plan is not None
                and not keep_one
                and abs(order.price - plan.price) < reprice_threshold
                and order.size_usdc >= plan.size_usdc * 0.5
            ):
                keep_one = True
                continue
            cancels.append(order)
        if plan is not None and not keep_one:
            posts.append(plan)

    for key, plan in desired_by_key.items():
        if key not in open_by_key:
            posts.append(plan)
    return cancels, posts


def locked_usdc(open_orders: list[OpenOrder], pending_posts: list[QuotePlan]) -> float:
    return sum(o.size_usdc for o in open_orders) + sum(p.size_usdc for p in pending_posts)


# ---------------------------------------------------------------------------
# Authed REST client (orders / positions)
# ---------------------------------------------------------------------------

class LimitlessPrivateClient:
    def __init__(self, credentials: LimitlessCredentials, timeout: int = 15):
        self.credentials = credentials
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _request(self, method: str, path: str, body: str = "") -> Any:
        headers = sign_hmac_headers(self.credentials, method, path, body)
        if body:
            headers["Content-Type"] = "application/json"
        resp = self.session.request(
            method, f"{BASE_URL}{path}", data=body or None, headers=headers, timeout=self.timeout
        )
        resp.raise_for_status()
        if not resp.text:
            return {}
        return resp.json()

    def user_orders(self, slug: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/markets/{slug}/user-orders")
        if isinstance(payload, list):
            return payload
        return payload.get("data") or payload.get("orders") or []

    def cancel_order(self, order_id: str) -> Any:
        return self._request("DELETE", f"/orders/{order_id}")

    def cancel_all(self, slug: str) -> Any:
        return self._request("DELETE", f"/orders/all/{slug}")

    def positions(self) -> Any:
        return self._request("GET", "/portfolio/positions")


def parse_open_orders(
    rows: list[dict[str, Any]],
    slug: str,
    token_sides: dict[str, str],
) -> list[OpenOrder]:
    """Tolerant parse of /markets/{slug}/user-orders rows into OpenOrder."""
    out: list[OpenOrder] = []
    for row in rows:
        status = str(row.get("status") or "").upper()
        if status and status not in {"LIVE", "OPEN", "NEW", "PARTIALLY_FILLED"}:
            continue
        token = str(row.get("tokenId") or row.get("token") or "")
        side = token_sides.get(token, "")
        price = float(row.get("price") or 0)
        maker_amount = float(row.get("makerAmount") or 0) / 1_000_000
        remaining = row.get("remainingSize")
        if remaining is not None and price > 0:
            size_usdc = float(remaining) / 1_000_000 * price
        else:
            size_usdc = maker_amount
        order_id = str(row.get("id") or row.get("orderId") or "")
        if not order_id or not side or price <= 0:
            continue
        out.append(OpenOrder(order_id=order_id, slug=slug, side=side, price=price, size_usdc=size_usdc, raw=row))
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class MakerEngine:
    def __init__(
        self,
        config: MakerConfig,
        limitless: LimitlessClient,
        private_client: LimitlessPrivateClient,
        submitter: LimitlessSubmitter | None,
        pricing: PricingProvider,
        out_path: Path,
        live: bool = False,
    ):
        self.config = config
        self.limitless = limitless
        self.private_client = private_client
        self.submitter = submitter
        self.pricing = pricing
        self.out_path = out_path
        self.live = live
        self._details_cache: dict[str, dict[str, Any]] = {}
        self._mids_session = requests.Session()

    # -- data ----------------------------------------------------------------

    def _hl_mids(self) -> dict[str, float]:
        resp = self._mids_session.post(
            "https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=10
        )
        resp.raise_for_status()
        return {k: float(v) for k, v in resp.json().items() if not k.startswith("@")}

    def _market_details(self, slug: str) -> dict[str, Any]:
        if slug not in self._details_cache:
            self._details_cache[slug] = self.limitless.market_details(slug)
        return self._details_cache[slug]

    def _token_sides(self, slug: str) -> dict[str, str]:
        details = self._market_details(slug)
        tokens = details.get("tokens") or {}
        out: dict[str, str] = {}
        if tokens.get("yes"):
            out[str(tokens["yes"])] = "UP"
        if tokens.get("no"):
            out[str(tokens["no"])] = "DOWN"
        return out

    def _inventory_by_symbol(self, slug_symbols: dict[str, str]) -> dict[str, float] | None:
        """Net UP-equivalent exposure in USDC per symbol. None = unknown (fail safe)."""
        try:
            payload = self.private_client.positions()
        except Exception:
            return None
        rows = payload if isinstance(payload, list) else (payload.get("data") or payload.get("positions") or [])
        inventory: dict[str, float] = {}
        try:
            for row in rows:
                market = row.get("market") or {}
                slug = str(market.get("slug") or row.get("marketSlug") or "")
                symbol = slug_symbols.get(slug)
                if symbol is None:
                    continue
                outcome = row.get("outcomeIndex")
                if outcome is None:
                    outcome = row.get("outcome")
                shares = float(
                    row.get("contractsFormatted")
                    or row.get("contracts")
                    or row.get("size")
                    or 0
                )
                if abs(shares) > 100_000:  # raw 1e-6 units
                    shares /= 1_000_000
                signed = shares if str(outcome) in {"0", "UP", "YES", "yes"} else -shares
                inventory[symbol] = inventory.get(symbol, 0.0) + signed * 0.5  # ~USDC at mid
        except Exception:
            return None
        return inventory

    # -- actions ---------------------------------------------------------------

    def _post(self, plan: QuotePlan) -> dict[str, Any]:
        details = self._market_details(plan.slug)
        tokens = details.get("tokens") or {}
        token_id = tokens.get("yes") if plan.side == "UP" else tokens.get("no")
        venue = details.get("venue") or {}
        contract = str(venue.get("exchange") or "")
        if not token_id or not contract:
            return {"submitted": False, "reason": "missing_token_or_venue"}
        # Contract tick rule mirrors candidate_to_limitless_intent.
        price_str = f"{plan.price:.10f}".rstrip("0")
        decimals = len(price_str.split(".")[1]) if "." in price_str else 0
        contract_tick = 10 ** (decimals + 1)
        size_raw = int(plan.size_usdc / plan.price * 1_000_000)
        size = ((size_raw // contract_tick) * contract_tick) / 1_000_000
        if size <= 0:
            return {"submitted": False, "reason": "zero_size"}
        intent = LimitlessOrderIntent(
            market_slug=plan.slug,
            token_id=str(token_id),
            side="BUY",
            price=plan.price,
            size=size,
            order_type="GTC",
            verifying_contract=contract,
            client_order_id=f"mkr-{plan.slug}-{plan.side}-{int(time.time() * 1000)}",
            post_only=True,
        )
        if not self.live or self.submitter is None:
            return {"submitted": False, "mode": "dry_run", "intent": asdict(intent)}
        return self.submitter.submit_intent(intent)

    def cancel_all_open(self, slugs: list[str]) -> None:
        for slug in slugs:
            try:
                if self.live:
                    self.private_client.cancel_all(slug)
                self._log({"event": "cancel_all", "slug": slug, "live": self.live})
            except Exception as exc:
                self._log({"event": "cancel_all_error", "slug": slug, "error": str(exc)})

    def _log(self, payload: dict[str, Any]) -> None:
        payload.setdefault("ts_ms", int(time.time() * 1000))
        with self.out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    # -- one loop ---------------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        markets = [
            m
            for m in self.limitless.active_crypto_markets()
            if m.interval in self.config.intervals and m.symbol in self.config.symbols
        ]
        markets.sort(key=lambda m: m.expiration_ms)
        markets = markets[: self.config.max_markets]
        slug_symbols = {m.slug: m.symbol for m in markets}

        mids = self._hl_mids()
        inventory = self._inventory_by_symbol(slug_symbols)
        if inventory is None:
            self._log({"event": "inventory_unknown_pull_quotes"})
            self.cancel_all_open(list(slug_symbols))
            return {"quoted": 0, "reason": "inventory_unknown"}

        desired: list[QuotePlan] = []
        open_orders: list[OpenOrder] = []
        for market in markets:
            mid = mids.get(market.symbol)
            if not mid:
                continue
            try:
                book = self.limitless.orderbook(market.slug)
                vol = self.pricing.vol_for(market.symbol)
                shade = self.pricing.up_shade_for(market.symbol)
                resolution = "pyth" if market.interval in ("1h", "1d", "1w") else "chainlink"
                ref = self.pricing.ref_price(market.symbol, mid, resolution)
            except Exception as exc:
                self._log({"event": "market_error", "slug": market.slug, "error": str(exc)})
                continue
            seconds = int((market.expiration_ms - now_ms) / 1000)
            fair_up = estimate_binary_probability(
                current_price=ref,
                threshold_price=market.threshold_price,
                seconds_to_expiry=seconds,
                annualized_volatility=vol,
                side="UP",
                up_probability_shade=shade,
            )
            desired.extend(
                compute_quotes(
                    market, book, fair_up, inventory.get(market.symbol, 0.0), self.config, seconds
                )
            )
            try:
                rows = self.private_client.user_orders(market.slug)
                open_orders.extend(parse_open_orders(rows, market.slug, self._token_sides(market.slug)))
            except Exception as exc:
                self._log({"event": "user_orders_error", "slug": market.slug, "error": str(exc)})
                # Unknown open state on this market — do not quote it this loop.
                desired = [p for p in desired if p.slug != market.slug]

        cancels, posts = diff_orders(desired, open_orders, self.config.reprice_threshold)

        kept = [o for o in open_orders if o not in cancels]
        while posts and locked_usdc(kept, posts) > self.config.max_total_locked_usdc:
            dropped = posts.pop()
            self._log({"event": "post_dropped_capital_cap", "slug": dropped.slug, "side": dropped.side})

        for order in cancels:
            try:
                if self.live:
                    self.private_client.cancel_order(order.order_id)
                self._log({"event": "cancel", "order_id": order.order_id, "slug": order.slug,
                           "side": order.side, "price": order.price, "live": self.live})
            except Exception as exc:
                self._log({"event": "cancel_error", "order_id": order.order_id, "error": str(exc)})

        for plan in posts:
            result = self._post(plan)
            self._log({"event": "post", "plan": asdict(plan), "result": _compact(result), "live": self.live})

        self._log({
            "event": "loop",
            "markets": len(markets),
            "desired": len(desired),
            "open": len(open_orders),
            "cancels": len(cancels),
            "posts": len(posts),
            "inventory": inventory,
        })
        return {"quoted": len(desired), "cancels": len(cancels), "posts": len(posts)}


def _compact(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    raw = out.get("raw")
    if isinstance(raw, dict):
        out["raw"] = {k: raw.get(k) for k in ("id", "status", "errorCode", "message") if k in raw}
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Limitless passive maker daemon")
    p.add_argument("--live-armed", action="store_true")
    p.add_argument("--intervals", default="1h")
    p.add_argument("--symbols", default="BTC,ETH,SOL,HYPE,BNB,DOGE,XRP")
    p.add_argument("--margin", type=float, default=0.05)
    p.add_argument("--quote-size-usdc", type=float, default=2.0)
    p.add_argument("--max-total-locked-usdc", type=float, default=14.0)
    p.add_argument("--max-inventory-usdc", type=float, default=6.0)
    p.add_argument("--max-markets", type=int, default=5)
    p.add_argument("--min-seconds-to-expiry", type=int, default=600)
    p.add_argument("--loop-seconds", type=int, default=10)
    p.add_argument("--iterations", type=int, default=0)
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/maker_trades.jsonl")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = MakerConfig(
        intervals=tuple(s.strip() for s in args.intervals.split(",") if s.strip()),
        symbols=tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()),
        margin=args.margin,
        quote_size_usdc=args.quote_size_usdc,
        max_total_locked_usdc=args.max_total_locked_usdc,
        max_inventory_usdc_per_symbol=args.max_inventory_usdc,
        max_markets=args.max_markets,
        min_seconds_to_expiry=args.min_seconds_to_expiry,
    )
    token_id = get_secret("LIMITLESS_TOKEN_ID") or ""
    token_secret = get_secret("LIMITLESS_TOKEN_SECRET") or ""
    credentials = LimitlessCredentials(token_id, token_secret)
    submitter = None
    if args.live_armed:
        private_key = get_secret("LIMITLESS_PRIVATE_KEY") or ""
        owner_id = int(os.environ.get("LIMITLESS_OWNER_ID") or "0")
        maker_address = os.environ.get("LIMITLESS_MAKER_ADDRESS") or ""
        if not (token_id and token_secret and private_key and owner_id and maker_address):
            raise RuntimeError("--live-armed but missing Limitless credentials")
        submitter = LimitlessSubmitter(
            credentials=credentials,
            builder=LimitlessOrderBuilder(
                maker=maker_address,
                owner_id=owner_id,
                fee_rate_bps=int(os.environ.get("LIMITLESS_FEE_RATE_BPS", "0")),
                signature_type=int(os.environ.get("LIMITLESS_SIGNATURE_TYPE", "0")),
            ),
            private_key=private_key,
        )

    out_path = Path(args.jsonl_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    engine = MakerEngine(
        config=config,
        limitless=LimitlessClient(),
        private_client=LimitlessPrivateClient(credentials),
        submitter=submitter,
        pricing=PricingProvider(),
        out_path=out_path,
        live=args.live_armed,
    )

    running = True

    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    _signal.signal(_signal.SIGTERM, _stop)
    _signal.signal(_signal.SIGINT, _stop)

    engine._log({"event": "startup", "mode": "live" if args.live_armed else "dry_run",
                 "config": asdict(config)})
    iteration = 0
    try:
        while running:
            iteration += 1
            try:
                engine.run_once()
            except Exception as exc:
                engine._log({"event": "loop_error", "error": str(exc)})
            if args.iterations and iteration >= args.iterations:
                break
            time.sleep(args.loop_seconds)
    finally:
        # Never leave resting orders behind.
        try:
            active = [
                m.slug
                for m in engine.limitless.active_crypto_markets()
                if m.interval in config.intervals and m.symbol in config.symbols
            ]
            engine.cancel_all_open(active)
        except Exception as exc:
            engine._log({"event": "shutdown_cancel_error", "error": str(exc)})
        engine._log({"event": "shutdown"})


if __name__ == "__main__":
    main()
