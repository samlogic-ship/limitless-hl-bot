"""EWMA realized-volatility estimator and pricing context for fair-prob inputs.

Calibration study 2026-06-09 (tmp/study/ reports, 4 symbols, ~1000 samples each):
- Flat 0.75 vol was badly miscalibrated per symbol (BTC best-fit 0.50, HYPE 1.25)
  and across regimes (BTC 52-day vol 0.41 vs 0.60 in the last 4 days).
- Per-symbol EWMA on 1m log returns (lambda 0.99, annualized, scaled 0.9) beat
  flat 0.75 on Brier for BTC/ETH/HYPE and tied SOL.
- 15m candles mean-revert on all 4 symbols: after two same-direction candles the
  next candle continues only 43-48% of the time. A +/-0.02 shade on P(up)
  improved Brier further.
- Markets resolve on Chainlink Data Streams (CEX spot composite), so Binance
  spot mid is the closest reference price; HL perp mid carries funding basis.
"""
from __future__ import annotations

import math
import time

import requests

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
BINANCE_BOOK_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
BINANCE_SYMBOL_OVERRIDES = {"HYPE": None}  # no Binance spot listing

# 52-day realized vol fallbacks (long_report.json, 2026-06-09)
BASELINE_ANNUAL_VOL = {"BTC": 0.41, "ETH": 0.52, "SOL": 0.58, "HYPE": 0.98}
DEFAULT_ANNUAL_VOL = 0.75

EWMA_LAMBDA = 0.99
VOL_SCALE = 0.9
VOL_FLOOR = 0.15
VOL_CAP = 3.0
REVERSAL_SHADE = 0.02
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0


class PricingProvider:
    """Per-symbol dynamic vol, reversal shade, and spot reference price.

    Every method degrades to the static defaults on network failure so the
    scanner never blocks on this provider.
    """

    def __init__(
        self,
        *,
        timeout: int = 8,
        vol_ttl_seconds: int = 60,
        shade_ttl_seconds: int = 120,
        spot_ttl_seconds: int = 10,
        bootstrap_minutes: int = 360,
    ):
        self.timeout = timeout
        self.vol_ttl_seconds = vol_ttl_seconds
        self.shade_ttl_seconds = shade_ttl_seconds
        self.spot_ttl_seconds = spot_ttl_seconds
        self.bootstrap_minutes = bootstrap_minutes
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        # symbol -> (refreshed_at, ewma_1m_variance, last_candle_open_ms, last_close)
        self._ewma: dict[str, tuple[float, float, int, float]] = {}
        self._shade: dict[str, tuple[float, float]] = {}
        self._spot: dict[str, tuple[float, float | None]] = {}

    # -- volatility -----------------------------------------------------------

    def vol_for(self, symbol: str) -> float:
        symbol = symbol.upper()
        now = time.time()
        cached = self._ewma.get(symbol)
        if cached and now - cached[0] <= self.vol_ttl_seconds:
            return self._annualize(cached[1])
        try:
            candles = self._fetch_candles(symbol, "1m", self.bootstrap_minutes)
            variance = self._ewma_variance(symbol, candles)
            if variance is None:
                raise ValueError("no candles")
            self._ewma[symbol] = (now, variance, int(candles[-1]["t"]), float(candles[-1]["c"]))
            return self._annualize(variance)
        except Exception:
            if cached:
                return self._annualize(cached[1])
            return BASELINE_ANNUAL_VOL.get(symbol, DEFAULT_ANNUAL_VOL)

    def _ewma_variance(self, symbol: str, candles: list[dict]) -> float | None:
        closes = [float(c["c"]) for c in candles if float(c.get("c") or 0) > 0]
        if len(closes) < 30:
            return None
        variance: float | None = None
        for prev, cur in zip(closes, closes[1:]):
            r = math.log(cur / prev)
            variance = r * r if variance is None else EWMA_LAMBDA * variance + (1 - EWMA_LAMBDA) * r * r
        return variance

    @staticmethod
    def _annualize(variance_1m: float) -> float:
        vol = math.sqrt(max(variance_1m, 0.0) * MINUTES_PER_YEAR) * VOL_SCALE
        return min(VOL_CAP, max(VOL_FLOOR, vol))

    # -- reversal shade --------------------------------------------------------

    def up_shade_for(self, symbol: str) -> float:
        """Shade applied to P(up): negative after two up candles, positive after two down."""
        symbol = symbol.upper()
        now = time.time()
        cached = self._shade.get(symbol)
        if cached and now - cached[0] <= self.shade_ttl_seconds:
            return cached[1]
        try:
            candles = self._fetch_candles(symbol, "15m", 75)
            completed = [c for c in candles if int(c["T"]) <= int(time.time() * 1000)]
            shade = 0.0
            if len(completed) >= 2:
                d1 = _direction(completed[-1])
                d2 = _direction(completed[-2])
                if d1 == d2 == "U":
                    shade = -REVERSAL_SHADE
                elif d1 == d2 == "D":
                    shade = REVERSAL_SHADE
            self._shade[symbol] = (now, shade)
            return shade
        except Exception:
            return cached[1] if cached else 0.0

    # -- spot reference price --------------------------------------------------

    def ref_price(self, symbol: str, hyperliquid_mid: float) -> float:
        """Binance spot mid when listed (closest to the Chainlink resolution feed),
        otherwise the HL perp mid the caller already has."""
        symbol = symbol.upper()
        if BINANCE_SYMBOL_OVERRIDES.get(symbol, symbol) is None:
            return hyperliquid_mid
        now = time.time()
        cached = self._spot.get(symbol)
        if cached and now - cached[0] <= self.spot_ttl_seconds:
            return cached[1] or hyperliquid_mid
        spot: float | None = None
        try:
            resp = self.session.get(
                BINANCE_BOOK_URL, params={"symbol": f"{symbol}USDT"}, timeout=self.timeout
            )
            resp.raise_for_status()
            payload = resp.json()
            bid, ask = float(payload["bidPrice"]), float(payload["askPrice"])
            if bid > 0 and ask > 0:
                spot = (bid + ask) / 2
        except Exception:
            spot = cached[1] if cached else None
        self._spot[symbol] = (now, spot)
        return spot or hyperliquid_mid

    # -- shared ------------------------------------------------------------------

    def _fetch_candles(self, symbol: str, interval: str, minutes: int) -> list[dict]:
        now_ms = int(time.time() * 1000)
        resp = self.session.post(
            HL_INFO_URL,
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": interval,
                    "startTime": now_ms - minutes * 60_000,
                    "endTime": now_ms,
                },
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        candles = resp.json()
        if not isinstance(candles, list) or not candles:
            raise ValueError(f"no candles for {symbol}")
        return candles


def _direction(candle: dict) -> str:
    o, c = float(candle["o"]), float(candle["c"])
    if c > o:
        return "U"
    if c < o:
        return "D"
    return "F"
