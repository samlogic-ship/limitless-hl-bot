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

from .hl_info import post_info

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
BINANCE_BOOK_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
BINANCE_SYMBOL_OVERRIDES = {"HYPE": None}  # no Binance spot listing
HERMES_URL = "https://hermes.pyth.network/v2/updates/price/latest"

# Pyth price feed ids (verified via Hermes /v2/price_feeds, 2026-06-09).
# Hourly/daily/weekly markets resolve on Pyth 1-minute candle opens, so the
# reference price for those intervals must come from Pyth itself.
PYTH_FEED_IDS = {
    "BTC": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ETH": "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "HYPE": "4279e31cc369bbcc2faf022b382b080e32a8e689ff20fbc530d2a603eb6cd98b",
    "BNB": "2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f",
    "DOGE": "dcef50dd0a4cd2dcc17e45df1676dcb336a11a61c69df7a0299b0150c672d25c",
    "XRP": "ec5d399846a9209f3fe5881d70aae9268c94339ff9817e8d18ff19fa05eea1c8",
}

# 52-day realized vol fallbacks (long_report.json, 2026-06-09)
BASELINE_ANNUAL_VOL = {"BTC": 0.41, "ETH": 0.52, "SOL": 0.58, "HYPE": 0.98}
DEFAULT_ANNUAL_VOL = 0.75

EWMA_LAMBDA = 0.99
VOL_SCALE = 0.9
VOL_FLOOR = 0.15
VOL_CAP = 3.0
REVERSAL_SHADE = 0.02
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0

# Written by calibrator.py every session; hot-reloaded here.
PRICING_PARAMS_PATH = "tmp/limitless_hl/pricing_params.json"


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
        self._params: dict[str, dict] = {}
        self._params_mtime: float = 0.0
        self._params_checked: float = 0.0
        self._feed_ids: dict[str, str | None] = dict(PYTH_FEED_IDS)

    # -- session-calibrated params (hot-reloaded) -------------------------------

    def _param(self, symbol: str, key: str, default: float) -> float:
        import os
        now = time.time()
        if now - self._params_checked > 60:
            self._params_checked = now
            try:
                mtime = os.path.getmtime(PRICING_PARAMS_PATH)
                if mtime != self._params_mtime:
                    import json as _json
                    with open(PRICING_PARAMS_PATH, encoding="utf-8") as handle:
                        self._params = _json.load(handle).get("symbols") or {}
                    self._params_mtime = mtime
            except OSError:
                pass
        row = self._params.get(symbol)
        if row and key in row:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                return default
        return default

    # -- volatility -----------------------------------------------------------

    def vol_for(self, symbol: str) -> float:
        symbol = symbol.upper()
        now = time.time()
        scale = self._param(symbol, "vol_scale", VOL_SCALE)
        cached = self._ewma.get(symbol)
        if cached and now - cached[0] <= self.vol_ttl_seconds:
            return self._annualize(cached[1], scale)
        try:
            candles = self._fetch_candles(symbol, "1m", self.bootstrap_minutes)
            variance = self._ewma_variance(symbol, candles)
            if variance is None:
                raise ValueError("no candles")
            self._ewma[symbol] = (now, variance, int(candles[-1]["t"]), float(candles[-1]["c"]))
            return self._annualize(variance, scale)
        except Exception:
            if cached:
                return self._annualize(cached[1], scale)
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
    def _annualize(variance_1m: float, scale: float = VOL_SCALE) -> float:
        vol = math.sqrt(max(variance_1m, 0.0) * MINUTES_PER_YEAR) * scale
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
            shade_amount = self._param(symbol, "shade", REVERSAL_SHADE)
            shade = 0.0
            if len(completed) >= 2:
                d1 = _direction(completed[-1])
                d2 = _direction(completed[-2])
                if d1 == d2 == "U":
                    shade = -shade_amount
                elif d1 == d2 == "D":
                    shade = shade_amount
            self._shade[symbol] = (now, shade)
            return shade
        except Exception:
            return cached[1] if cached else 0.0

    # -- spot reference price --------------------------------------------------

    def ref_price(self, symbol: str, hyperliquid_mid: float, resolution: str = "chainlink") -> float:
        """Reference price matched to the market's resolution feed.

        5m/15m resolve on Chainlink CEX-composite streams → Binance spot mid is
        the closest free proxy. 1h/1d/1w resolve on Pyth 1-minute candle opens →
        use Pyth Hermes directly (feed-consistent with both the threshold capture
        and the resolution print). Falls back down the chain on failure.
        """
        symbol = symbol.upper()
        if resolution == "pyth":
            pyth = self._pyth_price(symbol)
            if pyth:
                return pyth
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

    def _pyth_feed_id(self, symbol: str) -> str | None:
        if symbol in self._feed_ids:
            return self._feed_ids[symbol]
        feed_id: str | None = None
        try:
            resp = self.session.get(
                "https://hermes.pyth.network/v2/price_feeds",
                params={"query": symbol, "asset_type": "crypto"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            for feed in resp.json():
                attrs = feed.get("attributes") or {}
                if attrs.get("base") == symbol and attrs.get("quote_currency") == "USD":
                    feed_id = feed["id"]
                    break
        except Exception:
            return None  # transient: retry next call, don't negative-cache
        self._feed_ids[symbol] = feed_id
        return feed_id

    def _pyth_price(self, symbol: str) -> float | None:
        feed_id = self._pyth_feed_id(symbol)
        if not feed_id:
            return None
        now = time.time()
        cached = getattr(self, "_pyth_cache", None)
        if cached is None:
            cached = self._pyth_cache = {}
        hit = cached.get(symbol)
        if hit and now - hit[0] <= 5:
            return hit[1]
        try:
            resp = self.session.get(HERMES_URL, params=[("ids[]", feed_id)], timeout=self.timeout)
            resp.raise_for_status()
            parsed = (resp.json().get("parsed") or [])
            price_obj = parsed[0]["price"]
            value = float(price_obj["price"]) * (10 ** int(price_obj["expo"]))
            cached[symbol] = (now, value)
            return value
        except Exception:
            return hit[1] if hit else None

    # -- shared ------------------------------------------------------------------

    def _fetch_candles(self, symbol: str, interval: str, minutes: int) -> list[dict]:
        # Bucket endTime to 15s so identical (symbol,interval,minutes) requests
        # share one cached upstream call across processes (429 reduction).
        now_ms = int(time.time() * 1000)
        end_ms = now_ms - (now_ms % 15_000)
        data = post_info({
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": interval,
                "startTime": end_ms - minutes * 60_000,
                "endTime": end_ms,
            },
        }, timeout=self.timeout)

        class _R:
            def __init__(self, d): self._d = d
            def raise_for_status(self): pass
            def json(self): return self._d
        resp = _R(data)
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
