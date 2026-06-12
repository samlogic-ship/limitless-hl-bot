from __future__ import annotations

from limitless_hl.model import EdgeConfig, LimitlessMarket, OrderBook, choose_candidate, estimate_binary_probability


def test_estimate_binary_probability_prices_in_the_money_up_higher() -> None:
    probability = estimate_binary_probability(
        current_price=101.0,
        threshold_price=100.0,
        seconds_to_expiry=15 * 60,
        annualized_volatility=0.80,
        side="UP",
    )

    assert probability > 0.70
    assert estimate_binary_probability(101.0, 100.0, 15 * 60, 0.80, "DOWN") == 1 - probability


def test_choose_candidate_requires_positive_edge_and_liquidity() -> None:
    market = LimitlessMarket(
        slug="btc-up-or-down-15-min-1",
        title="BTC Up or Down - 15 Min",
        symbol="BTC",
        interval="15m",
        threshold_price=100.0,
        expiration_ms=1_000_900_000,
        open_price_captured_at="2026-06-09T01:15:00.000Z",
        volume_usdc=1200.0,
        raw={},
    )
    book = OrderBook(
        up_bid=0.65,
        up_ask=0.70,
        down_bid=0.25,
        down_ask=0.30,
        up_ask_size=250.0,
        down_ask_size=250.0,
        raw={},
    )

    candidate = choose_candidate(
        market=market,
        book=book,
        hyperliquid_mid=101.0,
        now_ms=1_000_000_000,
        config=EdgeConfig(min_edge=0.03, annualized_volatility=0.80, min_size_usdc=10.0),
    )

    assert candidate is not None
    assert candidate.side == "UP"
    assert candidate.limit_price == 0.70
    assert candidate.edge > 0.03


def test_choose_candidate_rejects_stale_expired_and_thin_books() -> None:
    market = LimitlessMarket(
        slug="sol-up-or-down-15-min-1",
        title="SOL Up or Down - 15 Min",
        symbol="SOL",
        interval="15m",
        threshold_price=100.0,
        expiration_ms=1_000_001_000,
        open_price_captured_at="2026-06-09T01:15:00.000Z",
        volume_usdc=1200.0,
        raw={},
    )
    book = OrderBook(
        up_bid=0.10,
        up_ask=0.20,
        down_bid=0.70,
        down_ask=0.80,
        up_ask_size=1.0,
        down_ask_size=1.0,
        raw={},
    )

    candidate = choose_candidate(
        market=market,
        book=book,
        hyperliquid_mid=101.0,
        now_ms=1_000_000_000,
        config=EdgeConfig(min_edge=0.01, annualized_volatility=0.80, min_size_usdc=10.0, min_seconds_to_expiry=30),
    )

    assert candidate is None


def test_taker_buy_fee_curve():
    from limitless_hl.model import taker_buy_fee_rate

    assert taker_buy_fee_rate(0.15) == 0.03
    assert taker_buy_fee_rate(0.50) == 0.03
    assert abs(taker_buy_fee_rate(0.65) - 0.018) < 1e-9
    assert abs(taker_buy_fee_rate(0.95) - 0.0053) < 1e-9
    # interpolation between published points
    assert 0.0252 < taker_buy_fee_rate(0.52) < 0.03


def test_probability_shade_applies_to_up_side():
    from limitless_hl.model import estimate_binary_probability

    base = estimate_binary_probability(100.0, 100.0, 600, 0.75, 'UP')
    shaded = estimate_binary_probability(100.0, 100.0, 600, 0.75, 'UP', up_probability_shade=0.02)
    assert abs((shaded - base) - 0.02) < 1e-9
    down = estimate_binary_probability(100.0, 100.0, 600, 0.75, 'DOWN', up_probability_shade=0.02)
    assert abs((1 - shaded) - down) < 1e-9
