from __future__ import annotations

from limitless_hl.hyperliquid_hedge import HyperliquidHedgerConfig, HyperliquidMarketHedger, _is_ok
from limitless_hl.live_trade import HedgePlan


def test_hyperliquid_hedger_dry_run_returns_order_plan() -> None:
    hedger = HyperliquidMarketHedger(
        config=HyperliquidHedgerConfig(live=False, max_notional_usdc=25.0),
        _exchange=None,
    )

    result = hedger.hedge(HedgePlan(symbol="BTC", side="SHORT", notional_usdc=12.0, reference_price=100.0))

    assert result["submitted"] is False
    assert result["mode"] == "dry_run"
    assert result["coin"] == "BTC"
    assert result["is_buy"] is False
    assert result["sz"] == 0.12


def test_hyperliquid_hedger_rejects_notional_above_cap() -> None:
    hedger = HyperliquidMarketHedger(
        config=HyperliquidHedgerConfig(live=False, max_notional_usdc=10.0),
        _exchange=None,
    )

    result = hedger.hedge(HedgePlan(symbol="ETH", side="LONG", notional_usdc=25.0, reference_price=100.0))

    assert result["submitted"] is False
    assert result["blocked"] is True
    assert result["reason"] == "notional_cap"


def test_hyperliquid_hedger_rejects_below_exchange_minimum() -> None:
    hedger = HyperliquidMarketHedger(
        config=HyperliquidHedgerConfig(live=False, max_notional_usdc=25.0),
        _exchange=None,
    )

    result = hedger.hedge(HedgePlan(symbol="BTC", side="SHORT", notional_usdc=10.99, reference_price=100.0))

    assert result["submitted"] is False
    assert result["blocked"] is True
    assert result["reason"] == "below_min_notional"


def test_hyperliquid_hedger_maps_long_to_buy() -> None:
    hedger = HyperliquidMarketHedger(
        config=HyperliquidHedgerConfig(live=False, max_notional_usdc=25.0),
        _exchange=None,
    )

    result = hedger.hedge(HedgePlan(symbol="ETH", side="LONG", notional_usdc=11.0, reference_price=100.0))

    assert result["is_buy"] is True


def test_hyperliquid_order_error_status_is_not_ok() -> None:
    response = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"error": "Order must have minimum value of $10. asset=1"}]},
        },
    }

    assert _is_ok(response) is False
