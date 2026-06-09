from limitless_hl.exiter import ExitConfig, decide_exit, taker_sell_fee_rate


def test_sell_fee_curve_peaks_at_half():
    assert abs(taker_sell_fee_rate(0.50) - 0.0150) < 1e-9
    assert abs(taker_sell_fee_rate(0.90) - 0.0078) < 1e-9
    assert taker_sell_fee_rate(0.50) > taker_sell_fee_rate(0.20) > taker_sell_fee_rate(0.05)
    # symmetric-ish tails
    assert abs(taker_sell_fee_rate(0.30) - taker_sell_fee_rate(0.70)) < 1e-9


def test_take_profit_when_bid_rich_vs_fair():
    # bought cheap, market now bids 0.85 but model says only 0.70 → sell into strength
    d = decide_exit(bid=0.85, fair_side=0.70, seconds_to_expiry=900,
                    position_value_usdc=3.0, config=ExitConfig())
    assert d.sell and d.reason == "take_profit"
    assert d.net_sell_value > d.hold_value


def test_recover_loss_when_thesis_dead():
    # bought at 0.45, model fair collapsed to 0.10, someone still bids 0.20 → recover
    d = decide_exit(bid=0.20, fair_side=0.10, seconds_to_expiry=900,
                    position_value_usdc=1.0, config=ExitConfig())
    assert d.sell and d.reason == "recover_loss"


def test_hold_when_market_pays_fair_or_less():
    d = decide_exit(bid=0.60, fair_side=0.62, seconds_to_expiry=900,
                    position_value_usdc=3.0, config=ExitConfig())
    assert not d.sell and d.reason == "hold_ev_better"
    # bid barely above fair but inside epsilon → still hold
    d = decide_exit(bid=0.63, fair_side=0.62, seconds_to_expiry=900,
                    position_value_usdc=3.0, config=ExitConfig(epsilon=0.015))
    assert not d.sell


def test_safety_gates():
    cfg = ExitConfig()
    assert decide_exit(0.85, 0.5, 30, 3.0, cfg).reason == "too_close_to_expiry"
    assert decide_exit(0.02, 0.5, 900, 3.0, cfg).reason == "junk_bid"
    assert decide_exit(0.85, 0.5, 900, 0.10, cfg).reason == "position_too_small"
