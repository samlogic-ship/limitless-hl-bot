"""Hyperliquid hedge leg for Limitless binary arb.

Uses the native hyperliquid-python-sdk against a dedicated isolated hedge
account (0x8187478d66f3B18FE774FbD500F04c34B3015E3D), completely separate
from the main hl-bot trading account.

To fund the hedge account:
  app.hyperliquid.xyz → connect wallet 0x8187478d... → deposit USDC
  OR: main HL account → Transfer → Sub-account transfer to 0x8187478d...
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .live_trade import HedgePlan
from .secrets import get_secret

HEDGE_WALLET_ADDRESS = "0x8187478d66f3B18FE774FbD500F04c34B3015E3D"
HL_BASE_URL = "https://api.hyperliquid.xyz"
MIN_HEDGE_NOTIONAL_USD = 11.0


@dataclass(frozen=True, slots=True)
class HyperliquidHedgerConfig:
    live: bool = False
    max_notional_usdc: float = 10.0
    slippage: float = 0.01


class HyperliquidMarketHedger:
    """Places a market order on HL to offset delta from a Limitless binary fill."""

    def __init__(self, config: HyperliquidHedgerConfig, _exchange: Any | None = None):
        self.config = config
        self._exchange = _exchange  # injectable for tests

    def hedge(self, plan: HedgePlan) -> dict[str, Any]:
        if plan.notional_usdc <= 0 or plan.reference_price <= 0:
            return {"submitted": False, "blocked": True, "reason": "invalid_plan"}

        if plan.notional_usdc > self.config.max_notional_usdc:
            return {
                "submitted": False,
                "blocked": True,
                "reason": "notional_cap",
                "notional_usdc": plan.notional_usdc,
                "max_notional_usdc": self.config.max_notional_usdc,
            }
        if plan.notional_usdc < MIN_HEDGE_NOTIONAL_USD:
            return {"submitted": False, "blocked": True, "reason": "below_min_notional",
                    "notional_usdc": plan.notional_usdc, "min_notional_usdc": MIN_HEDGE_NOTIONAL_USD}

        # BUY UP binary → we're long the underlying's up move → hedge with SHORT perp
        # BUY DOWN binary → we're long the underlying's down move → hedge with LONG perp
        is_buy = (plan.side == "LONG")

        sz = _round_size(plan.notional_usdc / plan.reference_price, plan.symbol)
        if sz <= 0:
            return {"submitted": False, "blocked": True, "reason": "zero_size_after_rounding"}

        if not self.config.live:
            return {
                "submitted": False, "mode": "dry_run",
                "coin": plan.symbol, "is_buy": is_buy,
                "sz": sz, "reference_price": plan.reference_price,
                "notional_usdc": plan.notional_usdc,
            }

        exchange = self._exchange or _build_exchange()
        resp = exchange.market_open(
            name=plan.symbol,
            is_buy=is_buy,
            sz=sz,
            slippage=self.config.slippage,
        )
        ok = _is_ok(resp)
        return {
            "submitted": ok,
            "mode": "live",
            "coin": plan.symbol,
            "is_buy": is_buy,
            "sz": sz,
            "reference_price": plan.reference_price,
            "notional_usdc": plan.notional_usdc,
            "raw": resp,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_exchange() -> Any:
    from eth_account import Account
    from hyperliquid.exchange import Exchange

    secret = get_secret("limitless-hl-hedge-key", service="samsbots", account="limitless-hl-hedge-key")
    if not secret:
        raise RuntimeError(
            f"Missing hedge key in Keychain (service=samsbots account=limitless-hl-hedge-key). "
            f"Fund {HEDGE_WALLET_ADDRESS} on app.hyperliquid.xyz first."
        )
    wallet = Account.from_key(secret)
    return Exchange(wallet, HL_BASE_URL)


# Coin → sz_decimals, fetched once from HL meta and cached
_SZ_DECIMALS: dict[str, int] = {}


def _get_sz_decimals(coin: str) -> int:
    if not _SZ_DECIMALS:
        try:
            from hyperliquid.info import Info
            meta = Info(HL_BASE_URL, skip_ws=True).meta()
            for asset in meta.get("universe", []):
                _SZ_DECIMALS[asset["name"]] = int(asset.get("szDecimals", 5))
        except Exception:
            pass
    return _SZ_DECIMALS.get(coin, 5)


def _round_size(raw_sz: float, coin: str) -> float:
    decimals = _get_sz_decimals(coin)
    factor = 10 ** decimals
    return math.floor(raw_sz * factor) / factor


def _is_ok(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return resp is not None
    # Check HL's nested statuses
    statuses = (resp.get("response") or {}).get("data", {}).get("statuses") or []
    if not statuses:
        info = resp.get("info") or {}
        statuses = info.get("statuses") or []
    if statuses:
        s = str(statuses[0])
        return ("filled" in s or "resting" in s) and "error" not in s.lower()
    return resp.get("status") == "ok"
