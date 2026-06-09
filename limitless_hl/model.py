from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal

BinarySide = Literal["UP", "DOWN"]


@dataclass(frozen=True, slots=True)
class EdgeConfig:
    min_edge: float = 0.05
    annualized_volatility: float = 0.75
    min_size_usdc: float = 25.0
    max_price: float = 0.97
    min_seconds_to_expiry: int = 45
    max_seconds_to_expiry: int = 24 * 60 * 60
    fee_buffer: float = 0.015
    stake_usdc: float = 25.0


@dataclass(frozen=True, slots=True)
class LimitlessMarket:
    slug: str
    title: str
    symbol: str
    interval: str
    threshold_price: float
    expiration_ms: int
    open_price_captured_at: str
    volume_usdc: float
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OrderBook:
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    up_ask_size: float
    down_ask_size: float
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Candidate:
    slug: str
    title: str
    symbol: str
    interval: str
    side: BinarySide
    fair_probability: float
    limit_price: float
    edge: float
    hyperliquid_mid: float
    threshold_price: float
    seconds_to_expiry: int
    stake_usdc: float
    expected_value_usdc: float
    hyperliquid_hedge_side: Literal["LONG", "SHORT"]
    annualized_volatility: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def estimate_binary_probability(
    current_price: float,
    threshold_price: float,
    seconds_to_expiry: int,
    annualized_volatility: float,
    side: BinarySide,
) -> float:
    if current_price <= 0 or threshold_price <= 0:
        return 0.5
    if seconds_to_expiry <= 0:
        up_probability = 1.0 if current_price > threshold_price else 0.0
    else:
        years = seconds_to_expiry / (365.0 * 24.0 * 60.0 * 60.0)
        sigma = max(annualized_volatility, 0.01) * math.sqrt(max(years, 1e-12))
        z = math.log(current_price / threshold_price) / sigma
        up_probability = _normal_cdf(z)
    if side == "UP":
        return min(max(up_probability, 0.0), 1.0)
    return min(max(1.0 - up_probability, 0.0), 1.0)


def choose_candidate(
    market: LimitlessMarket,
    book: OrderBook,
    hyperliquid_mid: float,
    now_ms: int,
    config: EdgeConfig,
) -> Candidate | None:
    seconds_to_expiry = int((market.expiration_ms - now_ms) / 1000)
    if seconds_to_expiry < config.min_seconds_to_expiry:
        return None
    if seconds_to_expiry > config.max_seconds_to_expiry:
        return None

    candidates: list[Candidate] = []
    for side, ask, ask_size, hedge_side in (
        ("UP", book.up_ask, book.up_ask_size, "SHORT"),
        ("DOWN", book.down_ask, book.down_ask_size, "LONG"),
    ):
        if ask <= 0 or ask > config.max_price:
            continue
        if ask_size * ask < config.min_size_usdc:
            continue
        fair = estimate_binary_probability(
            current_price=hyperliquid_mid,
            threshold_price=market.threshold_price,
            seconds_to_expiry=seconds_to_expiry,
            annualized_volatility=config.annualized_volatility,
            side=side,  # type: ignore[arg-type]
        )
        edge = fair - ask - config.fee_buffer
        if edge < config.min_edge:
            continue
        stake = min(config.stake_usdc, ask_size * ask)
        candidates.append(
            Candidate(
                slug=market.slug,
                title=market.title,
                symbol=market.symbol,
                interval=market.interval,
                side=side,  # type: ignore[arg-type]
                fair_probability=fair,
                limit_price=ask,
                edge=edge,
                hyperliquid_mid=hyperliquid_mid,
                threshold_price=market.threshold_price,
                seconds_to_expiry=seconds_to_expiry,
                stake_usdc=stake,
                expected_value_usdc=stake * edge,
                hyperliquid_hedge_side=hedge_side,  # type: ignore[arg-type]
                annualized_volatility=config.annualized_volatility,
                reason=(
                    f"{market.symbol} {side} fair={fair:.3f} ask={ask:.3f} "
                    f"edge={edge:.3f} threshold={market.threshold_price:.8g} hl_mid={hyperliquid_mid:.8g}"
                ),
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.expected_value_usdc)
