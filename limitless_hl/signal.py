"""
limitless_hl/signal.py — 15-min directional signal for Limitless direction markets.

Reads HL API for candles + funding.  No VPS dependency.

Signals:
  A: momentum-N  — last N × 15m candles all same direction
  B: momentum-N + funding aligned (positive funding = longs pay = slight down pressure)
  C: momentum-N + last candle moved > min_move_pct
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal, Optional, cast

import requests

from .hl_info import post_info

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
_SESSION = requests.Session()
_SESSION.headers.update({"Content-Type": "application/json"})


# ── Candle ────────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    open_ms: int   # UTC ms at candle open
    close_ms: int  # UTC ms at candle close
    open: float
    close: float
    high: float
    low: float
    volume: float

    @property
    def direction(self) -> Literal["UP", "DN", "FLAT"]:
        if self.close > self.open:
            return "UP"
        if self.close < self.open:
            return "DN"
        return "FLAT"

    @property
    def change_pct(self) -> float:
        return ((self.close - self.open) / self.open * 100) if self.open else 0.0


def fetch_candles(coin: str, n: int = 6, interval: str = "15m") -> list[Candle]:
    """
    Fetch the last N completed 15m candles for `coin` from Hyperliquid.
    Drops the current (incomplete) candle.
    """
    now_ms = int(time.time() * 1000)
    interval_ms = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}.get(interval, 900_000)
    start_ms = now_ms - (n + 3) * interval_ms

    data = post_info({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": now_ms},
    }, timeout=10)

    candles = []
    for c in data:
        candles.append(Candle(
            open_ms=int(c["t"]), close_ms=int(c["T"]),
            open=float(c["o"]), close=float(c["c"]),
            high=float(c["h"]), low=float(c["l"]),
            volume=float(c["v"]),
        ))

    # Drop incomplete last candle (close_ms > now - 30s grace)
    if candles and candles[-1].close_ms > now_ms - 30_000:
        candles = candles[:-1]

    return candles[-n:] if len(candles) >= n else candles


def fetch_funding(coin: str) -> Optional[float]:
    """
    Fetch the current 1-hour funding rate for `coin` from HL.
    Positive = longs pay (bearish pressure), Negative = shorts pay (bullish pressure).
    Returns None on error.
    """
    try:
        meta_list, ctx_list = post_info({"type": "metaAndAssetCtxs"}, timeout=10)
        universe = meta_list.get("universe", [])
        for i, asset in enumerate(universe):
            if asset.get("name") == coin:
                return float(ctx_list[i].get("funding", 0))
    except Exception:
        pass
    return None


# ── Signal ────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    coin: str
    direction: Optional[Literal["UP", "DN"]]  # None = no signal (SKIP)
    confidence: str        # "A", "B", "C", or "NONE"
    reason: str
    candles_used: int
    funding: Optional[float] = None
    last_move_pct: float = 0.0


def compute_signal(
    coin: str,
    *,
    lookback: int = 3,
    min_move_pct: float = 0.0,
    use_funding: bool = True,
    anti: bool = False,
) -> Signal:
    """
    Compute directional signal at the current 15m window open.

    Signal A: last `lookback` candles all same direction (no FLATs).
    Signal B: Signal A + funding rate aligned with bet direction.
    Signal C: Signal A + last candle |change| > min_move_pct.

    anti=True: bet AGAINST momentum (mean-reversion). Backtest shows BTC/ETH
    anti-momentum at lb=2-3 is consistent +2-5% edge over 30d / 5 weeks.
    This edge assumes Limitless asks ≈0.50 regardless of prior momentum state —
    validate actual ask prices in paper mode before real capital.
    """
    candles = fetch_candles(coin, n=lookback + 1)
    funding = fetch_funding(coin) if use_funding else None

    if len(candles) < lookback:
        return Signal(
            coin=coin, direction=None, confidence="NONE",
            reason=f"insufficient candles ({len(candles)}/{lookback})",
            candles_used=len(candles), funding=funding,
        )

    recent = candles[-lookback:]
    directions = [c.direction for c in recent]

    if len(set(directions)) != 1 or directions[0] == "FLAT":
        return Signal(
            coin=coin, direction=None, confidence="NONE",
            reason=f"mixed candles: {directions}",
            candles_used=lookback, funding=funding,
        )

    candle_direction = cast(Literal["UP", "DN"], directions[0])
    last_move = abs(recent[-1].change_pct)

    # Bet direction: reverse if anti-momentum mode
    direction = cast(Literal["UP", "DN"], ("DN" if candle_direction == "UP" else "UP") if anti else candle_direction)
    mode_tag = "ANTI" if anti else "MOM"

    # Base: Signal A
    confidence = "A"
    reason = f"{mode_tag}-{lookback}x{candle_direction}→{direction}"

    # Upgrade to C if strong last candle (relevant for anti: a strong move that should revert)
    if min_move_pct > 0 and last_move >= min_move_pct:
        confidence = "C"
        reason += f" + strong({last_move:.2f}%)"

    # Upgrade to B if funding aligned with the bet direction
    if confidence == "A" and funding is not None:
        aligned = (direction == "DN" and funding > 0) or (direction == "UP" and funding < 0)
        if aligned:
            confidence = "B"
            reason += f" + funding={'neg' if funding < 0 else 'pos'}({funding:.5f})"

    return Signal(
        coin=coin, direction=direction, confidence=confidence,
        reason=reason, candles_used=lookback, funding=funding, last_move_pct=last_move,
    )


# ── Window timing helpers ─────────────────────────────────────────────────────

WINDOW_SECS = 900  # 15 minutes


def window_offset_secs() -> int:
    """Seconds elapsed since the current 15-min UTC window opened."""
    return int(time.time()) % WINDOW_SECS


def window_open_ms() -> int:
    """Unix ms at the start of the current 15-min window."""
    return (int(time.time()) // WINDOW_SECS) * WINDOW_SECS * 1000


def next_window_open_ms() -> int:
    """Unix ms at the start of the next 15-min window."""
    return window_open_ms() + WINDOW_SECS * 1000
