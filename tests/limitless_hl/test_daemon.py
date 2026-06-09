"""Tests for limitless_hl.daemon — risk gating and loop wiring."""
from __future__ import annotations

from typing import Any

from limitless_hl.clients import LimitlessClient
from limitless_hl.daemon import (
    _build_runner,
    _filter_candidates,
    _is_insufficient_collateral_error,
    _is_rate_limited_error,
    _load_recent_open_slugs,
    _load_slice_scores,
    _score_candidates,
)
from limitless_hl.scorer import MarketFeatures, ScoringConfig, SliceStats
from limitless_hl.risk import RiskConfig, RiskLedger, RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_candidate(slug: str = "btc-up-or-down-hourly-1", seconds: int = 600) -> dict[str, Any]:
    return {
        "slug": slug,
        "symbol": "BTC",
        "side": "UP",
        "limit_price": 0.07,
        "stake_usdc": 25.0,
        "hyperliquid_mid": 62000.0,
        "hyperliquid_hedge_side": "SHORT",
        "seconds_to_expiry": seconds,
        "edge": 0.12,
        "expected_value_usdc": 3.0,
        "fair_probability": 0.20,
        "threshold_price": 63000.0,
        "interval": "1h",
        "title": "BTC Up or Down - Hourly",
        "reason": "test",
    }


# ---------------------------------------------------------------------------
# Risk manager unit tests
# ---------------------------------------------------------------------------

def test_risk_manager_blocks_duplicate_slug() -> None:
    risk = RiskManager(RiskConfig())
    candidate = _fake_candidate()
    ledger = RiskLedger(open_slugs={"btc-up-or-down-hourly-1"})

    decision = risk.can_open(candidate, ledger)

    assert not decision.allowed
    assert decision.reason == "duplicate_market"


def test_risk_manager_blocks_when_daily_loss_hit() -> None:
    risk = RiskManager(RiskConfig(max_daily_loss_usdc=50.0))
    candidate = _fake_candidate()
    ledger = RiskLedger(realized_pnl_usdc=-50.0)

    decision = risk.can_open(candidate, ledger)

    assert not decision.allowed
    assert decision.reason == "daily_loss_limit"


def test_risk_manager_blocks_when_open_market_cap_hit() -> None:
    risk = RiskManager(RiskConfig(max_open_markets=2))
    candidate = _fake_candidate(slug="new-market")
    ledger = RiskLedger(open_slugs={"a", "b"})

    decision = risk.can_open(candidate, ledger)

    assert not decision.allowed
    assert decision.reason == "open_market_limit"


def test_risk_manager_allows_clean_candidate() -> None:
    risk = RiskManager(RiskConfig())
    candidate = _fake_candidate()
    ledger = RiskLedger()

    decision = risk.can_open(candidate, ledger)

    assert decision.allowed
    assert decision.reason == "allowed"


# ---------------------------------------------------------------------------
# _build_runner returns preview legs when not live-armed
# ---------------------------------------------------------------------------

def test_build_runner_returns_preview_legs_in_dry_run(tmp_path: Any) -> None:
    import argparse

    args = argparse.Namespace(
        live_armed=False,
        hedge_live=False,
        max_hedge_notional_usdc=5.0,
        hyperliquid_env_file=".env.hyperliquid",
        client_order_prefix="test",
    )
    candidate = _fake_candidate()
    details = {
        "slug": candidate["slug"],
        "tokens": {"yes": "1234", "no": "5678"},
        "venue": {"exchange": "0xabcdef1234567890abcdef1234567890abcdef12"},
    }

    runner = _build_runner(args, LimitlessClient(), candidate, details)
    result = runner.run(candidate)

    # Dry-run: preview leg simulates a paper fill so the learner can resolve it later.
    assert result.state.value == "hedged"
    assert result.limitless_result is not None
    assert result.limitless_result["matched"] is True
    assert result.limitless_result["filled_usdc"] == candidate["stake_usdc"]
    assert result.limitless_result["raw"]["mode"] == "preview"
    assert result.hedge_result is not None
    assert result.hedge_result["submitted"] is True


def test_filter_candidates_limits_live_unhedged_to_vetted_slice() -> None:
    candidates = [
        _fake_candidate(slug="btc-15", seconds=600) | {"symbol": "BTC", "interval": "15m", "side": "UP"},
        _fake_candidate(slug="eth-15", seconds=600) | {"symbol": "ETH", "interval": "15m", "side": "UP"},
        _fake_candidate(slug="btc-5", seconds=600) | {"symbol": "BTC", "interval": "5m", "side": "UP"},
        _fake_candidate(slug="btc-down", seconds=600) | {"symbol": "BTC", "interval": "15m", "side": "DOWN"},
    ]

    filtered = _filter_candidates(candidates, symbols={"BTC", "HYPE"}, intervals={"15m"}, sides={"UP"})

    assert [row["slug"] for row in filtered] == ["btc-15"]


def test_filter_candidates_uses_slice_scores_when_available(tmp_path: Any) -> None:
    report = tmp_path / "eval.json"
    report.write_text(
        """
        {
          "resolved": [
            {"won": true, "pnl_usdc": 1.0, "fill": {"symbol": "BTC", "side": "UP", "stake_usdc": 1, "raw": {"interval": "15m"}}},
            {"won": true, "pnl_usdc": 1.0, "fill": {"symbol": "BTC", "side": "UP", "stake_usdc": 1, "raw": {"interval": "15m"}}},
            {"won": false, "pnl_usdc": -1.0, "fill": {"symbol": "ETH", "side": "UP", "stake_usdc": 1, "raw": {"interval": "15m"}}}
          ]
        }
        """,
        encoding="utf-8",
    )
    scores = _load_slice_scores(report, min_n=2, min_roi=0.1, min_win_rate=0.5)
    candidates = [
        _fake_candidate(slug="btc-15", seconds=600) | {"symbol": "BTC", "interval": "15m", "side": "UP"},
        _fake_candidate(slug="eth-15", seconds=600) | {"symbol": "ETH", "interval": "15m", "side": "UP"},
    ]

    filtered = _filter_candidates(candidates, symbols=set(), intervals=set(), sides=set(), slice_scores=scores)

    assert [row["slug"] for row in filtered] == ["btc-15"]


def test_filter_candidates_allows_screaming_short_market_without_slice_score() -> None:
    candidates = [
        _fake_candidate(slug="xrp-5", seconds=600)
        | {"symbol": "XRP", "interval": "5m", "side": "DOWN", "edge": 0.09},
        _fake_candidate(slug="ada-1d", seconds=600)
        | {"symbol": "ADA", "interval": "1d", "side": "UP", "edge": 0.20},
        _fake_candidate(slug="eth-5", seconds=600)
        | {"symbol": "ETH", "interval": "5m", "side": "UP", "edge": 0.03},
    ]

    filtered = _filter_candidates(
        candidates,
        symbols=set(),
        intervals=set(),
        sides=set(),
        slice_scores=set(),
        scream_promote=True,
        scream_min_edge=0.08,
        scream_intervals={"5M", "15M"},
    )

    assert [row["slug"] for row in filtered] == ["xrp-5"]
    assert filtered[0]["scream_promoted"] is True


def test_load_recent_open_slugs_persists_duplicate_guard_after_restart(tmp_path: Any) -> None:
    log = tmp_path / "daemon_trades.jsonl"
    log.write_text(
        '{"event":"trade","ts_ms":1000,"candidate":{"slug":"btc-15","seconds_to_expiry":120}}\n'
        '{"event":"trade","ts_ms":1000,"candidate":{"slug":"old","seconds_to_expiry":5}}\n',
        encoding="utf-8",
    )

    open_slugs, expiries = _load_recent_open_slugs(log, now_ms=10_000)

    assert open_slugs == {"btc-15"}
    assert expiries["btc-15"] == 121_000


def test_error_classifiers_identify_rate_limits_and_collateral() -> None:
    assert _is_rate_limited_error(Exception("429 Client Error: Too Many Requests"))
    assert _is_rate_limited_error(Exception("rate limit exceeded"))
    assert not _is_rate_limited_error(Exception("orderbook unavailable"))

    assert _is_insufficient_collateral_error(Exception("Insufficient collateral balance for this order."))
    assert not _is_insufficient_collateral_error(Exception("insufficient liquidity"))


def test_slice_scores_demote_seeded_slice_when_live_roi_degrades(tmp_path: Any) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"resolved":[{"fill":{"slug":"hype-seed","symbol":"HYPE","side":"UP",'
        '"price":0.5,"stake_usdc":1,"raw":{"interval":"15m"}},'
        '"won":true,"pnl_usdc":1.0}],"slices":[{"interval":"15m",'
        '"symbol":"HYPE","side":"UP","n":4,"stake_usdc":4,"pnl_usdc":-1.0}]}',
        encoding="utf-8",
    )

    scores = _load_slice_scores(
        report,
        min_n=1,
        min_roi=0.0,
        min_win_rate=0.0,
        live_min_n=4,
        live_min_roi=0.0,
    )

    assert ("15m", "HYPE", "UP") not in scores


def test_slice_scores_ignore_other_live_strategies(tmp_path: Any) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"resolved":[{"fill":{"slug":"doge-live","symbol":"DOGE","side":"DOWN",'
        '"price":0.5,"stake_usdc":1,"raw":{"interval":"15m","strategy":"funding_kelly"}},'
        '"won":true,"pnl_usdc":1.0}],"slices":[{"interval":"15m",'
        '"symbol":"DOGE","side":"DOWN","strategy":"funding_kelly","n":4,"stake_usdc":4,"pnl_usdc":2.0}]}',
        encoding="utf-8",
    )

    scores = _load_slice_scores(
        report,
        min_n=1,
        min_roi=0.0,
        min_win_rate=0.0,
        allowed_strategies={"scored_daemon", "seed"},
    )

    assert scores == set()


def test_shadow_slice_can_graduate_with_strict_thresholds(tmp_path: Any) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"resolved":['
        '{"fill":{"slug":"eth-1","symbol":"ETH","side":"UP","price":0.5,"stake_usdc":1,'
        '"raw":{"interval":"5m","strategy":"shadow_daemon"}},"won":true,"pnl_usdc":1.0},'
        '{"fill":{"slug":"eth-2","symbol":"ETH","side":"UP","price":0.5,"stake_usdc":1,'
        '"raw":{"interval":"5m","strategy":"shadow_daemon"}},"won":true,"pnl_usdc":1.0},'
        '{"fill":{"slug":"eth-3","symbol":"ETH","side":"UP","price":0.5,"stake_usdc":1,'
        '"raw":{"interval":"5m","strategy":"shadow_daemon"}},"won":false,"pnl_usdc":-1.0}'
        ']}',
        encoding="utf-8",
    )

    scores = _load_slice_scores(
        report,
        min_n=3,
        min_roi=0.20,
        min_win_rate=0.60,
        allowed_strategies={"shadow_daemon"},
    )

    assert scores == {("5m", "ETH", "UP")}


def test_shadow_slice_does_not_graduate_with_small_sample(tmp_path: Any) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        '{"resolved":[{"fill":{"slug":"hype-1","symbol":"HYPE","side":"DOWN",'
        '"price":0.5,"stake_usdc":1,"raw":{"interval":"15m","strategy":"shadow_daemon"}},'
        '"won":true,"pnl_usdc":1.0}]}',
        encoding="utf-8",
    )

    scores = _load_slice_scores(
        report,
        min_n=20,
        min_roi=0.10,
        min_win_rate=0.52,
        allowed_strategies={"shadow_daemon"},
    )

    assert scores == set()


def test_score_candidates_updates_stake_and_explains_blocks() -> None:
    candidates = [
        _fake_candidate(slug="btc-15", seconds=600)
        | {"symbol": "BTC", "interval": "15m", "side": "UP", "threshold_price": 100.0, "hyperliquid_mid": 101.0},
        _fake_candidate(slug="eth-15", seconds=600)
        | {"symbol": "ETH", "interval": "15m", "side": "UP", "threshold_price": 100.0, "hyperliquid_mid": 101.0},
    ]

    class FakeProvider:
        def features_for(self, candidate):
            return MarketFeatures(hl_mid=101.0, momentum_1m_bps=8, momentum_3m_bps=20, momentum_5m_bps=30)

    scored, rejected = _score_candidates(
        candidates,
        provider=FakeProvider(),
        slice_stats={("15m", "BTC", "UP"): SliceStats(n=5, win_rate=0.6, roi=0.2)},
        config=ScoringConfig(base_stake_usdc=1, max_stake_usdc=3, min_score=1),
    )

    assert [row["slug"] for row in scored] == ["btc-15"]
    assert scored[0]["stake_usdc"] > 1
    assert scored[0]["score"] >= 1
    assert rejected[0]["slug"] == "eth-15"
    assert rejected[0]["reason"] == "slice_not_promoted"
