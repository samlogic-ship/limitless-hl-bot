from limitless_hl.maker import (
    MakerConfig,
    OpenOrder,
    QuotePlan,
    compute_quotes,
    diff_orders,
    locked_usdc,
    parse_open_orders,
)
from limitless_hl.model import LimitlessMarket, OrderBook


def _market(symbol: str = "BTC", interval: str = "1h") -> LimitlessMarket:
    return LimitlessMarket(
        slug=f"{symbol.lower()}-up-or-down-hourly-1",
        title=f"{symbol} Up or Down - Hourly",
        symbol=symbol,
        interval=interval,
        threshold_price=100.0,
        expiration_ms=10_000_000,
        open_price_captured_at="",
        volume_usdc=0.0,
        raw={},
    )


def _book(up_bid=0.40, up_ask=0.60, down_bid=0.35, down_ask=0.62) -> OrderBook:
    return OrderBook(
        up_bid=up_bid, up_ask=up_ask, down_bid=down_bid, down_ask=down_ask,
        up_ask_size=100.0, down_ask_size=100.0, raw={},
    )


def test_compute_quotes_bids_both_sides_below_fair():
    config = MakerConfig(margin=0.05)
    plans = compute_quotes(_market(), _book(), fair_up=0.50, inventory_usdc=0.0,
                           config=config, seconds_to_expiry=1800)
    assert {p.side for p in plans} == {"UP", "DOWN"}
    up = next(p for p in plans if p.side == "UP")
    down = next(p for p in plans if p.side == "DOWN")
    assert abs(up.price - 0.45) < 1e-9
    assert abs(down.price - 0.45) < 1e-9
    # structural no-dutch-loss invariant
    assert up.price + down.price < 1.0


def test_compute_quotes_never_crosses_ask():
    config = MakerConfig(margin=0.01)
    plans = compute_quotes(_market(), _book(up_ask=0.42), fair_up=0.50, inventory_usdc=0.0,
                           config=config, seconds_to_expiry=1800)
    up = next(p for p in plans if p.side == "UP")
    assert up.price <= 0.42 - 0.009


def test_compute_quotes_skews_against_inventory():
    config = MakerConfig(margin=0.05, inventory_skew=0.04, max_inventory_usdc_per_symbol=6.0)
    loaded = compute_quotes(_market(), _book(), fair_up=0.50, inventory_usdc=3.0,
                            config=config, seconds_to_expiry=1800)
    flat = compute_quotes(_market(), _book(), fair_up=0.50, inventory_usdc=0.0,
                          config=config, seconds_to_expiry=1800)
    loaded_up = next(p for p in loaded if p.side == "UP")
    flat_up = next(p for p in flat if p.side == "UP")
    assert loaded_up.price < flat_up.price  # long UP → bid UP lower


def test_compute_quotes_drops_loaded_side_at_cap():
    config = MakerConfig(max_inventory_usdc_per_symbol=6.0)
    plans = compute_quotes(_market(), _book(), fair_up=0.50, inventory_usdc=6.5,
                           config=config, seconds_to_expiry=1800)
    assert {p.side for p in plans} == {"DOWN"}


def test_compute_quotes_respects_expiry_gate():
    config = MakerConfig(min_seconds_to_expiry=600)
    assert compute_quotes(_market(), _book(), 0.5, 0.0, config, seconds_to_expiry=300) == []


def test_compute_quotes_price_bounds():
    config = MakerConfig(margin=0.05, min_price=0.07, max_price=0.88)
    plans = compute_quotes(_market(), _book(up_ask=0.99, down_ask=0.99), fair_up=0.97,
                           inventory_usdc=0.0, config=config, seconds_to_expiry=1800)
    # UP bid 0.92 > max_price → dropped; DOWN bid -0.02 < min_price → dropped
    assert plans == []


def test_diff_orders_keeps_close_prices_and_replaces_drifted():
    plan = QuotePlan(slug="s", symbol="BTC", interval="1h", side="UP", price=0.45, size_usdc=2.0)
    close = OpenOrder(order_id="1", slug="s", side="UP", price=0.44, size_usdc=2.0)
    cancels, posts = diff_orders([plan], [close], reprice_threshold=0.015)
    assert cancels == [] and posts == []

    drifted = OpenOrder(order_id="2", slug="s", side="UP", price=0.40, size_usdc=2.0)
    cancels, posts = diff_orders([plan], [drifted], reprice_threshold=0.015)
    assert [c.order_id for c in cancels] == ["2"]
    assert posts == [plan]


def test_diff_orders_cancels_undesired_and_posts_missing():
    plan = QuotePlan(slug="s", symbol="BTC", interval="1h", side="DOWN", price=0.45, size_usdc=2.0)
    stale = OpenOrder(order_id="9", slug="old", side="UP", price=0.30, size_usdc=2.0)
    cancels, posts = diff_orders([plan], [stale], reprice_threshold=0.015)
    assert [c.order_id for c in cancels] == ["9"]
    assert posts == [plan]


def test_locked_usdc_sums_open_and_pending():
    orders = [OpenOrder(order_id="1", slug="s", side="UP", price=0.4, size_usdc=2.0)]
    plans = [QuotePlan(slug="s", symbol="BTC", interval="1h", side="DOWN", price=0.45, size_usdc=2.0)]
    assert locked_usdc(orders, plans) == 4.0


def test_parse_open_orders_filters_and_maps_sides():
    token_sides = {"yes-token": "UP", "no-token": "DOWN"}
    rows = [
        {"id": "a", "tokenId": "yes-token", "price": 0.45, "makerAmount": 2_000_000, "status": "LIVE"},
        {"id": "b", "tokenId": "no-token", "price": 0.40, "makerAmount": 2_000_000, "status": "FILLED"},
        {"id": "c", "tokenId": "unknown", "price": 0.40, "makerAmount": 2_000_000},
    ]
    orders = parse_open_orders(rows, "slug", token_sides)
    assert [o.order_id for o in orders] == ["a"]
    assert orders[0].side == "UP"
    assert abs(orders[0].size_usdc - 2.0) < 1e-9
