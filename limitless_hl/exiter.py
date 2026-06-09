"""limitless_hl/exiter.py — smart position exits for Limitless Up/Down holdings.

One rule covers both profit-taking and loss recovery:

    SELL when  best_bid * (1 - sell_fee)  >=  model_fair + epsilon

If the market bids more than our model says the position is worth, selling
beats holding — whether that's because the position ran deep into profit
(bid rich vs fair) or because our thesis died and the bid still recovers
more than the expected resolution value. Both cases are the same inequality.

Safety gates: never exit in the final seconds (no liquidity, gamma noise),
never sell into junk bids, minimum position value, every decision logged.
Fees use the published taker SELL curve (peaks 1.5% at $0.50).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clients import LimitlessClient
from .live_trade import (
    LimitlessCredentials,
    LimitlessOrderBuilder,
    LimitlessOrderIntent,
    LimitlessSubmitter,
)
from .maker import LimitlessPrivateClient
from .model import estimate_binary_probability
from .secrets import get_secret
from .volatility import PricingProvider

# Published taker SELL fee curve (docs.limitless.exchange/user-guide/fees).
_SELL_FEE_POINTS = (
    (0.01, 0.0042), (0.05, 0.0060), (0.10, 0.0078), (0.20, 0.0111),
    (0.30, 0.0132), (0.40, 0.0144), (0.50, 0.0150), (0.60, 0.0144),
    (0.70, 0.0132), (0.80, 0.0111), (0.90, 0.0078), (0.95, 0.0060),
    (0.999, 0.0042),
)

INTERVAL_FROM_SLUG = re.compile(r"-(5-min|15-min|hourly|daily|weekly)-")
_INTERVAL_NORM = {"5-min": "5m", "15-min": "15m", "hourly": "1h", "daily": "1d", "weekly": "1w"}


def taker_sell_fee_rate(price: float) -> float:
    points = _SELL_FEE_POINTS
    if price <= points[0][0]:
        return points[0][1]
    for (p0, f0), (p1, f1) in zip(points, points[1:]):
        if p0 <= price <= p1:
            span = p1 - p0
            return f0 + (f1 - f0) * ((price - p0) / span if span else 0.0)
    return points[-1][1]


@dataclass(frozen=True, slots=True)
class ExitConfig:
    epsilon: float = 0.015          # required net advantage of selling over holding
    min_position_usdc: float = 0.50
    min_bid: float = 0.03
    min_seconds_to_expiry: int = 60
    max_shares_per_order: float = 200.0


@dataclass(frozen=True, slots=True)
class ExitDecision:
    sell: bool
    reason: str
    net_sell_value: float
    hold_value: float


def decide_exit(
    bid: float,
    fair_side: float,
    seconds_to_expiry: int,
    position_value_usdc: float,
    config: ExitConfig,
) -> ExitDecision:
    """Pure decision: sell iff net-of-fee bid beats model hold value by epsilon."""
    if seconds_to_expiry < config.min_seconds_to_expiry:
        return ExitDecision(False, "too_close_to_expiry", 0.0, fair_side)
    if bid < config.min_bid:
        return ExitDecision(False, "junk_bid", 0.0, fair_side)
    if position_value_usdc < config.min_position_usdc:
        return ExitDecision(False, "position_too_small", 0.0, fair_side)
    net = bid * (1.0 - taker_sell_fee_rate(bid))
    if net >= fair_side + config.epsilon:
        kind = "take_profit" if fair_side >= 0.5 else "recover_loss"
        return ExitDecision(True, kind, net, fair_side)
    return ExitDecision(False, "hold_ev_better", net, fair_side)


class ExitEngine:
    def __init__(
        self,
        config: ExitConfig,
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
        self._details: dict[str, dict[str, Any]] = {}

    def _log(self, payload: dict[str, Any]) -> None:
        payload.setdefault("ts_ms", int(time.time() * 1000))
        with self.out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def _market_details(self, slug: str) -> dict[str, Any]:
        if slug not in self._details:
            self._details[slug] = self.limitless.market_details(slug)
        return self._details[slug]

    def _sell(self, slug: str, side: str, shares: float, price: float) -> dict[str, Any]:
        details = self._market_details(slug)
        tokens = details.get("tokens") or {}
        token_id = tokens.get("yes") if side == "UP" else tokens.get("no")
        venue = details.get("venue") or {}
        contract = str(venue.get("exchange") or "")
        if not token_id or not contract:
            return {"submitted": False, "reason": "missing_token_or_venue"}
        intent = LimitlessOrderIntent(
            market_slug=slug,
            token_id=str(token_id),
            side="SELL",
            price=round(price, 2),
            size=min(shares, self.config.max_shares_per_order),
            order_type="FAK",
            verifying_contract=contract,
            client_order_id=f"exit-{slug}-{side}-{int(time.time() * 1000)}",
        )
        if not self.live or self.submitter is None:
            return {"submitted": False, "mode": "dry_run"}
        return self.submitter.submit_intent(intent)

    def run_once(self) -> int:
        now_ms = int(time.time() * 1000)
        try:
            payload = self.private_client.positions()
        except Exception as exc:
            self._log({"event": "positions_error", "error": str(exc)})
            return 0
        rows = payload if isinstance(payload, list) else (payload.get("clob") or payload.get("data") or [])
        exits = 0
        for row in rows:
            market = row.get("market") or {}
            slug = str(market.get("slug") or "")
            if not slug or str(market.get("status") or "").upper() not in {"FUNDED", "ACTIVE", "OPEN"}:
                continue
            balances = row.get("tokensBalance") or {}
            holdings = []
            yes = float(balances.get("yes") or 0) / 1_000_000
            no = float(balances.get("no") or 0) / 1_000_000
            if yes > 0:
                holdings.append(("UP", yes))
            if no > 0:
                holdings.append(("DOWN", no))
            if not holdings:
                continue
            interval_match = INTERVAL_FROM_SLUG.search(slug)
            interval = _INTERVAL_NORM.get(interval_match.group(1)) if interval_match else None
            try:
                details = self._market_details(slug)
                metadata = details.get("metadata") or {}
                threshold = float(metadata.get("openPrice") or 0)
                expiration_ms = int(details.get("expirationTimestamp") or 0)
                book = self.limitless.orderbook(slug)
            except Exception as exc:
                self._log({"event": "market_error", "slug": slug, "error": str(exc)})
                continue
            if threshold <= 0 or expiration_ms <= 0:
                continue
            seconds = int((expiration_ms - now_ms) / 1000)
            symbol = slug.split("-")[0].upper()
            resolution = "pyth" if interval in ("1h", "1d", "1w") else "chainlink"
            try:
                vol = self.pricing.vol_for(symbol)
                shade = self.pricing.up_shade_for(symbol)
                ref = self.pricing.ref_price(symbol, 0.0, resolution)
            except Exception as exc:
                self._log({"event": "pricing_error", "slug": slug, "error": str(exc)})
                continue
            if not ref:
                continue
            for side, shares in holdings:
                bid = book.up_bid if side == "UP" else book.down_bid
                fair = estimate_binary_probability(
                    current_price=ref,
                    threshold_price=threshold,
                    seconds_to_expiry=seconds,
                    annualized_volatility=vol,
                    side=side,  # type: ignore[arg-type]
                    up_probability_shade=shade,
                )
                decision = decide_exit(bid or 0.0, fair, seconds, shares * (bid or 0.0), self.config)
                if not decision.sell:
                    continue
                try:
                    result = self._sell(slug, side, shares, bid)
                except Exception as exc:
                    result = {"submitted": False, "reason": str(exc)[:160]}
                exits += 1
                self._log({
                    "event": "exit",
                    "slug": slug, "symbol": symbol, "side": side,
                    "shares": round(shares, 4), "bid": bid,
                    "fair": round(fair, 4),
                    "net_sell_value": round(decision.net_sell_value, 4),
                    "reason": decision.reason,
                    "live": self.live,
                    "result": {k: result.get(k) for k in ("submitted", "mode", "reason", "matched", "filled_usdc") if k in result},
                })
        return exits


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Limitless smart exit daemon")
    parser.add_argument("--live-armed", action="store_true")
    parser.add_argument("--epsilon", type=float, default=0.015)
    parser.add_argument("--loop-seconds", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=0)
    parser.add_argument("--jsonl-out", default="tmp/limitless_hl/exiter_trades.jsonl")
    args = parser.parse_args(argv)

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
    engine = ExitEngine(
        config=ExitConfig(epsilon=args.epsilon),
        limitless=LimitlessClient(),
        private_client=LimitlessPrivateClient(credentials),
        submitter=submitter,
        pricing=PricingProvider(),
        out_path=out_path,
        live=args.live_armed,
    )
    engine._log({"event": "startup", "mode": "live" if args.live_armed else "dry_run",
                 "epsilon": args.epsilon})
    iteration = 0
    while True:
        iteration += 1
        try:
            engine.run_once()
        except Exception as exc:
            engine._log({"event": "loop_error", "error": str(exc)})
        if args.iterations and iteration >= args.iterations:
            break
        time.sleep(args.loop_seconds)


if __name__ == "__main__":
    main()
