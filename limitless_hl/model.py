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
    fee_buffer: float = 0.005  # spread/adverse-selection only; exchange fee is priced exactly
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


# Published taker BUY fee curve (docs.limitless.exchange/user-guide/fees, 2026-06-09).
# Flat 3.00% for any price <= 0.50, then declining. Fee is charged in outcome
# tokens, so expected payout per token is fair * (1 - fee).
_TAKER_BUY_FEE_POINTS = (
    (0.50, 0.0300), (0.55, 0.0252), (0.60, 0.0213), (0.65, 0.0180),
    (0.70, 0.0151), (0.75, 0.0126), (0.80, 0.0105), (0.85, 0.0085),
    (0.90, 0.0068), (0.95, 0.0053), (0.99, 0.0042), (1.00, 0.0040),
)


def taker_buy_fee_rate(price: float) -> float:
    if price <= 0.50:
        return 0.0300
    points = _TAKER_BUY_FEE_POINTS
    for (p0, f0), (p1, f1) in zip(points, points[1:]):
        if p0 <= price <= p1:
            span = p1 - p0
            return f0 + (f1 - f0) * ((price - p0) / span if span else 0.0)
    return points[-1][1]


def estimate_binary_probability(
    current_price: float,
    threshold_price: float,
    seconds_to_expiry: int,
    annualized_volatility: float,
    side: BinarySide,
    up_probability_shade: float = 0.0,
) -> float:
    if current_price <= 0 or threshold_price <= 0:
        return 0.5
    if seconds_to_expiry <= 0:
        up_probability = 1.0 if current_price > threshold_price else 0.0
    else:
        years = seconds_to_expiry / (365.0 * 24.0 * 60.0 * 60.0)
        sigma = max(annualized_volatility, 0.01) * math.sqrt(max(years, 1e-12))
        z = math.log(current_price / threshold_price) / sigma
        up_probability = _normal_cdf(z) + up_probability_shade
    if side == "UP":
        return min(max(up_probability, 0.0), 1.0)
    return min(max(1.0 - up_probability, 0.0), 1.0)


def choose_candidate(
    market: LimitlessMarket,
    book: OrderBook,
    hyperliquid_mid: float,
    now_ms: int,
    config: EdgeConfig,
    annualized_volatility: float | None = None,
    up_probability_shade: float = 0.0,
    reference_price: float | None = None,
) -> Candidate | None:
    volatility = annualized_volatility if annualized_volatility else config.annualized_volatility
    current_price = reference_price if reference_price else hyperliquid_mid
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
            current_price=current_price,
            threshold_price=market.threshold_price,
            seconds_to_expiry=seconds_to_expiry,
            annualized_volatility=volatility,
            side=side,  # type: ignore[arg-type]
            up_probability_shade=up_probability_shade,
        )
        # Fee-aware edge: buy fee is taken in outcome tokens, so the expected
        # payout per token is fair * (1 - fee). fee_buffer now covers only
        # spread/adverse-selection, not the exchange fee.
        edge = fair * (1.0 - taker_buy_fee_rate(ask)) - ask - config.fee_buffer
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
                annualized_volatility=volatility,
                reason=(
                    f"{market.symbol} {side} fair={fair:.3f} ask={ask:.3f} "
                    f"edge={edge:.3f} threshold={market.threshold_price:.8g} hl_mid={hyperliquid_mid:.8g} "
                    f"ref={current_price:.8g} vol={volatility:.3f} shade={up_probability_shade:+.3f}"
                ),
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.expected_value_usdc)
