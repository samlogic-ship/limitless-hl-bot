from __future__ import annotations

from limitless_hl.scorer import (
    HlBotContext,
    MarketFeatures,
    ScoringConfig,
    SliceStats,
    load_hl_bot_context,
    score_candidate,
)


def _candidate(**overrides):
    base = {
        "symbol": "BTC",
        "side": "UP",
        "interval": "15m",
        "threshold_price": 100.0,
        "hyperliquid_mid": 101.0,
        "limit_price": 0.45,
        "edge": 0.08,
        "stake_usdc": 1.0,
    }
    base.update(overrides)
    return base


def test_score_candidate_promotes_strong_own_evidence_and_momentum() -> None:
    result = score_candidate(
        _candidate(),
        slice_stats={("15m", "BTC", "UP"): SliceStats(n=12, win_rate=0.58, roi=0.22)},
        features=MarketFeatures(
            hl_mid=101.0,
            binance_mid=100.9,
            funding=0.00001,
            momentum_1m_bps=8.0,
            momentum_3m_bps=18.0,
            momentum_5m_bps=24.0,
            open_interest_change_bps=5.0,
        ),
        config=ScoringConfig(base_stake_usdc=1.0, max_stake_usdc=5.0, min_score=1.0),
    )

    assert result.allowed is True
    assert result.stake_usdc > 1.0
    assert "slice_positive" in result.reasons
    assert "oracle_basis_up" in result.reasons
    assert "momentum_up" in result.reasons


def test_score_candidate_blocks_degraded_slice() -> None:
    result = score_candidate(
        _candidate(symbol="ETH"),
        slice_stats={("15m", "ETH", "UP"): SliceStats(n=6, win_rate=0.0, roi=-1.0)},
        features=MarketFeatures(hl_mid=101.0, momentum_1m_bps=10.0, momentum_3m_bps=20.0, momentum_5m_bps=30.0),
        config=ScoringConfig(min_slice_n=3, min_slice_roi=0.02, min_slice_win_rate=0.25),
    )

    assert result.allowed is False
    assert result.reason == "slice_not_promoted"


def test_score_candidate_discovery_mode_allows_unknown_slice() -> None:
    result = score_candidate(
        _candidate(edge=0.02),
        slice_stats={},
        features=MarketFeatures(hl_mid=101.0, momentum_1m_bps=10.0, momentum_3m_bps=20.0, momentum_5m_bps=30.0),
        config=ScoringConfig(min_slice_n=0, min_score=0.0, base_stake_usdc=1.0, max_stake_usdc=1.0),
    )

    assert result.allowed is True
    assert result.reason == "allowed"
    assert result.stake_usdc == 1.0
    assert "slice_discovery" in result.reasons


def test_score_candidate_allows_scream_promoted_without_slice_stats() -> None:
    result = score_candidate(
        _candidate(symbol="XRP", side="DOWN", interval="5m", edge=0.09, threshold_price=100.0, hyperliquid_mid=99.0, scream_promoted=True),
        slice_stats={},
        features=MarketFeatures(hl_mid=99.0, momentum_1m_bps=-10.0, momentum_3m_bps=-20.0, momentum_5m_bps=-30.0),
        config=ScoringConfig(base_stake_usdc=1.0, max_stake_usdc=3.0, min_score=1.0),
    )

    assert result.allowed is True
    assert result.stake_usdc >= 1.0
    assert "scream_edge" in result.reasons
    assert "momentum_down" in result.reasons


def test_score_candidate_penalizes_crowded_late_up_trade() -> None:
    result = score_candidate(
        _candidate(),
        slice_stats={("15m", "BTC", "UP"): SliceStats(n=12, win_rate=0.58, roi=0.22)},
        features=MarketFeatures(
            hl_mid=101.0,
            funding=0.00008,
            momentum_1m_bps=35.0,
            momentum_3m_bps=70.0,
            momentum_5m_bps=120.0,
            open_interest_change_bps=90.0,
        ),
        config=ScoringConfig(min_score=1.0),
    )

    assert result.allowed is False
    assert result.reason == "score_below_min"
    assert "crowded_funding" in result.reasons
    assert "late_extension" in result.reasons


def test_score_candidate_uses_hl_bot_context_as_soft_feature() -> None:
    candidate = _candidate()
    stats = {("15m", "BTC", "UP"): SliceStats(n=12, win_rate=0.58, roi=0.22)}
    features = MarketFeatures(hl_mid=101.0, momentum_1m_bps=8.0, momentum_3m_bps=18.0, momentum_5m_bps=24.0)

    supportive = score_candidate(
        candidate,
        slice_stats=stats,
        features=features,
        hl_context=HlBotContext(fresh=True, regime="LONG", breadth_state="BROADLY_UP"),
        config=ScoringConfig(base_stake_usdc=1.0, max_stake_usdc=5.0, min_score=1.0),
    )
    opposing = score_candidate(
        candidate,
        slice_stats=stats,
        features=features,
        hl_context=HlBotContext(fresh=True, regime="SHORT", breadth_state="BROADLY_DOWN"),
        config=ScoringConfig(base_stake_usdc=1.0, max_stake_usdc=5.0, min_score=1.0),
    )

    assert supportive.score > opposing.score
    assert "hl_bot_supportive" in supportive.reasons
    assert "hl_bot_opposing" in opposing.reasons


def test_load_hl_bot_context_parses_nested_status(tmp_path) -> None:
    status = tmp_path / "status.json"
    status.write_text(
        '{"ts_ms": 1781025000000, "regime": "LONG", "market_breadth": {"state": "BROADLY_UP", "up": 12, "down": 3}}',
        encoding="utf-8",
    )

    context = load_hl_bot_context(status, now_ms=1781025030000, max_age_ms=120000)

    assert context.fresh is True
    assert context.regime == "LONG"
    assert context.breadth_state == "BROADLY_UP"
    assert context.breadth_up == 12
    assert context.breadth_down == 3
