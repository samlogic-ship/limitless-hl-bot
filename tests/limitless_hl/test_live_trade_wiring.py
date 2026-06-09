from __future__ import annotations

from limitless_hl.live_trade import (
    HedgePlan,
    LimitlessCredentials,
    LimitlessOrderBuilder,
    LimitlessOrderIntent,
    PairTradeRunner,
    TradeState,
    candidate_to_limitless_intent,
    sign_hmac_headers,
)


def test_limitless_order_builder_builds_buy_fak_payload() -> None:
    builder = LimitlessOrderBuilder(
        maker="0x1111111111111111111111111111111111111111",
        owner_id=42,
        fee_rate_bps=0,
    )
    intent = LimitlessOrderIntent(
        market_slug="btc-up-or-down-15-min-1",
        token_id="123456789",
        side="BUY",
        price=0.40,
        size=25.0,
        order_type="FAK",
        verifying_contract="0x2222222222222222222222222222222222222222",
        client_order_id="test-order",
    )

    payload = builder.build_unsigned_payload(intent, salt=7, timestamp_ms=1000)

    assert payload["marketSlug"] == "btc-up-or-down-15-min-1"
    assert payload["ownerId"] == 42
    assert payload["orderType"] == "FAK"
    assert payload["clientOrderId"] == "test-order"
    assert payload["order"]["makerAmount"] == 10000000
    assert payload["order"]["takerAmount"] == 25000000
    assert payload["order"]["tokenId"] == "123456789"
    assert payload["order"]["nonce"] == 0
    assert payload["order"]["feeRateBps"] == 0
    assert payload["order"]["side"] == 0


def test_hmac_headers_are_deterministic_for_fixed_timestamp() -> None:
    creds = LimitlessCredentials(token_id="token", token_secret="c2VjcmV0")
    headers = sign_hmac_headers(
        creds,
        method="POST",
        path="/orders",
        body='{"a":1}',
        timestamp="2026-06-09T00:00:00+00:00",
    )

    assert headers["lmts-api-key"] == "token"
    assert headers["lmts-timestamp"] == "2026-06-09T00:00:00+00:00"
    assert headers["lmts-signature"] == "5zSDAV/i4VSq3Ssr1iOelejFJZo8bIcDMvb/5tAB528="


def test_pair_trade_runner_hedges_after_limitless_fill() -> None:
    class FakeLimitless:
        def submit(self, candidate):
            return {"submitted": True, "matched": True, "filled_usdc": 20.0, "raw": {"ok": True}}

    class FakeHedge:
        def hedge(self, plan):
            assert plan.symbol == "BTC"
            assert plan.side == "SHORT"
            return {"submitted": True, "raw": {"oid": 1}}

    runner = PairTradeRunner(limitless=FakeLimitless(), hedger=FakeHedge())
    state = runner.run(
        {
            "slug": "btc",
            "symbol": "BTC",
            "hyperliquid_hedge_side": "SHORT",
            "hyperliquid_mid": 100.0,
            "stake_usdc": 20.0,
        }
    )

    assert state.state == TradeState.HEDGED
    assert state.limitless_result["submitted"] is True
    assert state.hedge_result["submitted"] is True


def test_pair_trade_runner_uses_bounded_binary_delta_notional() -> None:
    class FakeLimitless:
        def submit(self, candidate):
            return {"submitted": True, "matched": True, "filled_usdc": 20.0, "raw": {"ok": True}}

    class FakeHedge:
        def hedge(self, plan: HedgePlan):
            assert plan.notional_usdc == 11.0
            return {"submitted": True, "raw": {"oid": 1}}

    state = PairTradeRunner(limitless=FakeLimitless(), hedger=FakeHedge()).run(
        {
            "slug": "btc",
            "symbol": "BTC",
            "side": "UP",
            "hyperliquid_hedge_side": "SHORT",
            "hyperliquid_mid": 100.0,
            "threshold_price": 120.0,
            "seconds_to_expiry": 300,
            "stake_usdc": 20.0,
            "limit_price": 0.9,
            "fair_probability": 0.01,
        }
    )

    assert state.state == TradeState.HEDGED


def test_pair_trade_runner_does_not_hedge_unfilled_limitless_order() -> None:
    class FakeLimitless:
        def submit(self, candidate):
            return {"submitted": True, "matched": False, "raw": {"ok": True}}

    class FakeHedge:
        def hedge(self, plan: HedgePlan):
            raise AssertionError("hedge should not be called")

    state = PairTradeRunner(limitless=FakeLimitless(), hedger=FakeHedge()).run(
        {"slug": "btc", "symbol": "BTC", "hyperliquid_hedge_side": "SHORT", "hyperliquid_mid": 100.0, "stake_usdc": 20.0}
    )

    assert state.state == TradeState.LIMITLESS_UNFILLED


def test_pair_trade_runner_can_record_unhedged_fill() -> None:
    class FakeLimitless:
        def submit(self, candidate):
            return {"submitted": True, "matched": True, "filled_usdc": 5.0, "raw": {"ok": True}}

    class FakeHedge:
        def hedge(self, plan: HedgePlan):
            raise AssertionError("hedge should not be called")

    state = PairTradeRunner(limitless=FakeLimitless(), hedger=FakeHedge(), require_hedge=False).run(
        {"slug": "btc", "symbol": "BTC", "hyperliquid_hedge_side": "SHORT", "hyperliquid_mid": 100.0, "stake_usdc": 5.0}
    )

    assert state.state == TradeState.LIMITLESS_FILLED_UNHEDGED
    assert state.limitless_result["filled_usdc"] == 5.0
    assert state.hedge_result is None


def test_pair_trade_runner_marks_blocked_hedge_as_failed() -> None:
    class FakeLimitless:
        def submit(self, candidate):
            return {"submitted": True, "matched": True, "filled_usdc": 9.0, "raw": {"ok": True}}

    class FakeHedge:
        def hedge(self, plan: HedgePlan):
            return {"submitted": False, "blocked": True, "reason": "below_min_notional"}

    state = PairTradeRunner(limitless=FakeLimitless(), hedger=FakeHedge()).run(
        {"slug": "btc", "symbol": "BTC", "hyperliquid_hedge_side": "SHORT", "hyperliquid_mid": 100.0, "stake_usdc": 9.0}
    )

    assert state.state == TradeState.HEDGE_FAILED
    assert state.hedge_result == {"submitted": False, "blocked": True, "reason": "below_min_notional"}
    assert state.error == "hedge_not_submitted: below_min_notional"


def test_candidate_to_limitless_intent_uses_up_down_token_and_venue() -> None:
    market_details = {
        "slug": "btc-up-or-down",
        "tokens": {"yes": "111", "no": "222"},
        "venue": {"exchange": "0x2222222222222222222222222222222222222222"},
    }

    intent = candidate_to_limitless_intent(
        {
            "slug": "btc-up-or-down",
            "side": "DOWN",
            "limit_price": 0.25,
            "stake_usdc": 10.0,
        },
        market_details,
        client_order_id="abc",
    )

    assert intent.market_slug == "btc-up-or-down"
    assert intent.token_id == "222"
    assert intent.price == 0.25
    assert intent.size == 40.0
    assert intent.order_type == "FAK"
    assert intent.verifying_contract == "0x2222222222222222222222222222222222222222"
