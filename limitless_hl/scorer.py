from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
import time

import requests


@dataclass(frozen=True, slots=True)
class SliceStats:
    n: int
    win_rate: float
    roi: float


@dataclass(frozen=True, slots=True)
class MarketFeatures:
    hl_mid: float = 0.0
    binance_mid: float | None = None
    funding: float | None = None
    momentum_1m_bps: float = 0.0
    momentum_3m_bps: float = 0.0
    momentum_5m_bps: float = 0.0
    open_interest_change_bps: float | None = None


@dataclass(frozen=True, slots=True)
class HlBotContext:
    fresh: bool = False
    regime: str = ""
    breadth_state: str = ""
    breadth_up: int = 0
    breadth_down: int = 0
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    base_stake_usdc: float = 1.0
    max_stake_usdc: float = 5.0
    min_score: float = 1.0
    min_slice_n: int = 3
    min_slice_roi: float = 0.02
    min_slice_win_rate: float = 0.25
    min_basis_bps: float = 8.0
    min_momentum_3m_bps: float = 5.0
    max_late_momentum_5m_bps: float = 100.0
    crowded_funding_abs: float = 0.00005
    crowded_oi_change_bps: float = 75.0


@dataclass(frozen=True, slots=True)
class ScoreResult:
    allowed: bool
    score: float
    stake_usdc: float
    reason: str
    reasons: list[str]
    features: dict[str, Any]


class LiveFeatureProvider:
    def __init__(self, timeout: int = 8, ttl_seconds: int = 15):
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds
        self.session = requests.Session()
        self._cache: dict[str, tuple[float, MarketFeatures]] = {}

    def features_for(self, candidate: dict[str, Any]) -> MarketFeatures:
        symbol = str(candidate.get("symbol") or "").upper()
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and now - cached[0] <= self.ttl_seconds:
            return cached[1]
        features = MarketFeatures(
            hl_mid=float(candidate.get("hyperliquid_mid") or 0.0),
            binance_mid=self._fetch_binance_mid(symbol),
            funding=self._fetch_funding(symbol),
            **self._fetch_momentum(symbol),
        )
        self._cache[symbol] = (now, features)
        return features


def score_candidate(
    candidate: dict[str, Any],
    *,
    slice_stats: dict[tuple[str, str, str], SliceStats],
    features: MarketFeatures,
    config: ScoringConfig,
    hl_context: HlBotContext | None = None,
) -> ScoreResult:
    interval = str(candidate.get("interval") or "").lower()
    symbol = str(candidate.get("symbol") or "").upper()
    side = str(candidate.get("side") or "").upper()
    stats = slice_stats.get((interval, symbol, side))
    reasons: list[str] = []
    score = float(candidate.get("edge") or 0.0) * 10.0

    if bool(candidate.get("scream_promoted")):
        reasons.append("scream_edge")
        score += 1.0
    elif config.min_slice_n > 0:
        if stats is None or stats.n < config.min_slice_n or stats.roi < config.min_slice_roi or stats.win_rate < config.min_slice_win_rate:
            return _result(False, score, 0.0, "slice_not_promoted", reasons, features)
        reasons.append("slice_positive")
        score += min(2.0, stats.roi * 2.0) + min(1.0, stats.win_rate)
    elif stats is None:
        reasons.append("slice_discovery")
    else:
        reasons.append("slice_observed")
        score += max(-1.0, min(1.0, stats.roi)) + min(0.5, stats.win_rate)

    threshold = float(candidate.get("threshold_price") or 0.0)
    hl_mid = features.hl_mid or float(candidate.get("hyperliquid_mid") or 0.0)
    if threshold > 0 and hl_mid > 0:
        basis_bps = (hl_mid - threshold) / threshold * 10_000.0
        if side == "UP" and basis_bps >= config.min_basis_bps:
            score += 0.8
            reasons.append("oracle_basis_up")
        elif side == "DOWN" and basis_bps <= -config.min_basis_bps:
            score += 0.8
            reasons.append("oracle_basis_down")
        else:
            score -= 0.4
            reasons.append("oracle_basis_weak")

    if features.binance_mid and hl_mid > 0:
        venue_basis_bps = (hl_mid - features.binance_mid) / features.binance_mid * 10_000.0
        if abs(venue_basis_bps) <= 20.0:
            score += 0.2
            reasons.append("venue_prices_aligned")
        else:
            score -= 0.2
            reasons.append("venue_basis_wide")

    if side == "UP" and features.momentum_3m_bps >= config.min_momentum_3m_bps:
        score += 0.8
        reasons.append("momentum_up")
    elif side == "DOWN" and features.momentum_3m_bps <= -config.min_momentum_3m_bps:
        score += 0.8
        reasons.append("momentum_down")
    else:
        score -= 0.3
        reasons.append("momentum_weak")

    if abs(features.momentum_5m_bps) > config.max_late_momentum_5m_bps:
        score -= 1.0
        reasons.append("late_extension")

    if features.funding is not None:
        crowded_up = side == "UP" and features.funding >= config.crowded_funding_abs
        crowded_down = side == "DOWN" and features.funding <= -config.crowded_funding_abs
        if crowded_up or crowded_down:
            score -= 1.0
            reasons.append("crowded_funding")
        elif side == "UP" and features.funding < 0:
            score += 0.3
            reasons.append("funding_tailwind")

    if features.open_interest_change_bps is not None and abs(features.open_interest_change_bps) >= config.crowded_oi_change_bps:
        score -= 0.7
        reasons.append("crowded_oi")

    if hl_context is not None and hl_context.fresh:
        delta, reason = _score_hl_bot_context(side, hl_context)
        score += delta
        reasons.append(reason)

    allowed = score >= config.min_score
    stake = _stake_for_score(score, config) if allowed else 0.0
    return _result(allowed, score, stake, "allowed" if allowed else "score_below_min", reasons, features)


def load_hl_bot_context(path: str | Path, *, now_ms: int | None = None, max_age_ms: int = 120_000) -> HlBotContext:
    status_path = Path(path)
    if not status_path.exists():
        return HlBotContext()
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return HlBotContext(raw={})
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    ts = _extract_status_ts(payload)
    fresh = ts > 0 and now - ts <= max_age_ms
    breadth = payload.get("market_breadth") or payload.get("breadth") or {}
    return HlBotContext(
        fresh=fresh,
        regime=str(payload.get("regime") or payload.get("btc_regime") or ""),
        breadth_state=str(breadth.get("state") or breadth.get("regime") or payload.get("market_breadth_state") or ""),
        breadth_up=int(breadth.get("up") or breadth.get("coins_up") or payload.get("breadth_coins_up") or 0),
        breadth_down=int(breadth.get("down") or breadth.get("coins_down") or payload.get("breadth_coins_down") or 0),
        raw=payload,
    )


def _extract_status_ts(payload: dict[str, Any]) -> int:
    for key in ("ts_ms", "timestamp_ms", "updated_at_ms", "last_tick_ms"):
        try:
            value = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _score_hl_bot_context(side: str, context: HlBotContext) -> tuple[float, str]:
    regime = context.regime.upper()
    breadth = context.breadth_state.upper()
    if side == "UP" and (regime in {"LONG", "RISK_ON"} or breadth in {"BROADLY_UP", "RISK_ON"}):
        return 0.5, "hl_bot_supportive"
    if side == "DOWN" and (regime in {"SHORT", "RISK_OFF", "BROADLY_DOWN"} or breadth == "BROADLY_DOWN"):
        return 0.5, "hl_bot_supportive"
    if side == "UP" and (regime in {"SHORT", "RISK_OFF", "BROADLY_DOWN"} or breadth == "BROADLY_DOWN"):
        return -0.7, "hl_bot_opposing"
    if side == "DOWN" and (regime in {"LONG", "RISK_ON"} or breadth in {"BROADLY_UP", "RISK_ON"}):
        return -0.7, "hl_bot_opposing"
    return 0.0, "hl_bot_neutral"


def _fetch_hl_post(session: requests.Session, payload: dict[str, Any], timeout: int) -> Any:
    response = session.post("https://api.hyperliquid.xyz/info", json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _momentum_bps(candles: list[dict[str, Any]], lookback: int) -> float:
    if len(candles) <= lookback:
        return 0.0
    start = float(candles[-lookback - 1].get("c") or candles[-lookback - 1].get("o") or 0)
    end = float(candles[-1].get("c") or 0)
    return ((end - start) / start * 10_000.0) if start > 0 else 0.0


def _binance_symbol(symbol: str) -> str:
    return f"{symbol.upper()}USDT"


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _completed_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    return [row for row in rows if int(row.get("T") or 0) <= now_ms - 5_000]


def _empty_momentum() -> dict[str, float]:
    return {"momentum_1m_bps": 0.0, "momentum_3m_bps": 0.0, "momentum_5m_bps": 0.0}


def _merge_momentum(rows: list[dict[str, Any]]) -> dict[str, float]:
    candles = _completed_candles(rows)
    return {
        "momentum_1m_bps": _momentum_bps(candles, 1),
        "momentum_3m_bps": _momentum_bps(candles, 3),
        "momentum_5m_bps": _momentum_bps(candles, 5),
    }


def _parse_funding_response(symbol: str, payload: Any) -> float | None:
    try:
        meta, ctxs = payload
        for asset, ctx in zip(meta.get("universe", []), ctxs):
            if asset.get("name") == symbol:
                return float(ctx.get("funding"))
    except Exception:
        return None
    return None


def _ticker_price(payload: Any) -> float | None:
    if isinstance(payload, dict):
        return _safe_float(payload.get("price"))
    return None


def _get_json(session: requests.Session, url: str, params: dict[str, Any], timeout: int) -> Any:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _post_json(session: requests.Session, payload: dict[str, Any], timeout: int) -> Any:
    return _fetch_hl_post(session, payload, timeout)


def _start_ms(minutes: int) -> int:
    return int(time.time() * 1000) - minutes * 60_000


def _end_ms() -> int:
    return int(time.time() * 1000)


def _hl_candle_payload(symbol: str) -> dict[str, Any]:
    return {
        "type": "candleSnapshot",
        "req": {"coin": symbol, "interval": "1m", "startTime": _start_ms(12), "endTime": _end_ms()},
    }


def _hl_funding_payload() -> dict[str, Any]:
    return {"type": "metaAndAssetCtxs"}


def _binance_ticker_url() -> str:
    return "https://api.binance.com/api/v3/ticker/price"


def _fetch_binance(session: requests.Session, symbol: str, timeout: int) -> float | None:
    try:
        return _ticker_price(_get_json(session, _binance_ticker_url(), {"symbol": _binance_symbol(symbol)}, timeout))
    except Exception:
        return None


def _fetch_funding(session: requests.Session, symbol: str, timeout: int) -> float | None:
    try:
        return _parse_funding_response(symbol, _post_json(session, _hl_funding_payload(), timeout))
    except Exception:
        return None


def _fetch_momentum(session: requests.Session, symbol: str, timeout: int) -> dict[str, float]:
    try:
        rows = _post_json(session, _hl_candle_payload(symbol), timeout)
        return _merge_momentum(rows if isinstance(rows, list) else [])
    except Exception:
        return _empty_momentum()


def _provider_fetch_binance(self: LiveFeatureProvider, symbol: str) -> float | None:
    return _fetch_binance(self.session, symbol, self.timeout)


def _provider_fetch_funding(self: LiveFeatureProvider, symbol: str) -> float | None:
    return _fetch_funding(self.session, symbol, self.timeout)


def _provider_fetch_momentum(self: LiveFeatureProvider, symbol: str) -> dict[str, float]:
    return _fetch_momentum(self.session, symbol, self.timeout)


LiveFeatureProvider._fetch_binance_mid = _provider_fetch_binance  # type: ignore[attr-defined]
LiveFeatureProvider._fetch_funding = _provider_fetch_funding  # type: ignore[attr-defined]
LiveFeatureProvider._fetch_momentum = _provider_fetch_momentum  # type: ignore[attr-defined]


def _stake_for_score(score: float, config: ScoringConfig) -> float:
    if score >= config.min_score + 2.0:
        return min(config.max_stake_usdc, config.base_stake_usdc * 3.0)
    if score >= config.min_score + 1.0:
        return min(config.max_stake_usdc, config.base_stake_usdc * 2.0)
    return config.base_stake_usdc


def _result(
    allowed: bool,
    score: float,
    stake_usdc: float,
    reason: str,
    reasons: list[str],
    features: MarketFeatures,
) -> ScoreResult:
    return ScoreResult(
        allowed=allowed,
        score=round(score, 6),
        stake_usdc=round(stake_usdc, 6),
        reason=reason,
        reasons=list(reasons),
        features={
            "hl_mid": features.hl_mid,
            "binance_mid": features.binance_mid,
            "funding": features.funding,
            "momentum_1m_bps": features.momentum_1m_bps,
            "momentum_3m_bps": features.momentum_3m_bps,
            "momentum_5m_bps": features.momentum_5m_bps,
            "open_interest_change_bps": features.open_interest_change_bps,
        },
    )
