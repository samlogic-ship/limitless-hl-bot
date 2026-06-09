from __future__ import annotations

from limitless_hl.funding_daemon import FundingProofConfig, FundingSignal, first_spike_decision, passes_live_funding_proof


def test_live_funding_proof_requires_edge_sample_and_stake_bounds() -> None:
    signal = FundingSignal(
        coin="BTC",
        direction="DOWN",
        threshold=1.25e-05,
        compare="gte",
        backtest_wr=0.578,
        backtest_n=15_366,
        max_entry_price=0.52,
    )
    config = FundingProofConfig(min_backtest_wr=0.56, min_backtest_n=1_000, min_ev_pct=0.10, max_live_stake_usdc=25.0)

    assert passes_live_funding_proof(signal, ev_pct=0.12, stake_usdc=25.0, config=config) == (True, "allowed")
    assert passes_live_funding_proof(signal, ev_pct=0.09, stake_usdc=25.0, config=config) == (False, "ev_below_min")
    assert passes_live_funding_proof(signal, ev_pct=0.12, stake_usdc=26.0, config=config) == (False, "stake_out_of_bounds")


def test_first_spike_filter_skips_startup_and_sustained_runs() -> None:
    signal = FundingSignal(
        coin="BTC",
        direction="DOWN",
        threshold=1.25e-05,
        compare="gte",
        backtest_wr=0.578,
        backtest_n=15_366,
        max_entry_price=0.52,
    )
    states: dict[str, bool] = {}

    assert first_spike_decision(states, signal, triggered=True) == (False, "startup_active_signal")
    assert first_spike_decision(states, signal, triggered=True) == (False, "sustained_signal")
    assert first_spike_decision(states, signal, triggered=False) == (False, "not_triggered")
    assert first_spike_decision(states, signal, triggered=True) == (True, "first_spike")
