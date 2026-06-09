from __future__ import annotations

from pathlib import Path

from limitless_hl.attribution import (
    PaperFill,
    ResolvedMarket,
    build_paper_fills,
    evaluate_fills,
    load_scan_candidates,
    resolve_candidate,
)
from limitless_hl.risk import RiskConfig, RiskLedger, RiskManager


def test_resolve_candidate_marks_up_and_down_winners() -> None:
    up_candidate = {"slug": "btc", "side": "UP", "limit_price": 0.40, "stake_usdc": 10.0}
    down_candidate = {"slug": "eth", "side": "DOWN", "limit_price": 0.30, "stake_usdc": 10.0}

    assert resolve_candidate(up_candidate, ResolvedMarket("btc", winning_outcome_index=0)).won is True
    assert resolve_candidate(down_candidate, ResolvedMarket("eth", winning_outcome_index=1)).won is True
    assert resolve_candidate(down_candidate, ResolvedMarket("eth", winning_outcome_index=0)).won is False


def test_build_paper_fills_deduplicates_slug_and_side(tmp_path: Path) -> None:
    log_path = tmp_path / "scan.jsonl"
    log_path.write_text(
        "\n".join(
            [
                '{"scanned_at_ms": 1000, "candidates": [{"slug": "btc", "side": "UP", "limit_price": 0.40, "stake_usdc": 10, "symbol": "BTC"}]}',
                '{"scanned_at_ms": 2000, "candidates": [{"slug": "btc", "side": "UP", "limit_price": 0.42, "stake_usdc": 10, "symbol": "BTC"}]}',
                '{"scanned_at_ms": 3000, "candidates": [{"slug": "btc", "side": "DOWN", "limit_price": 0.25, "stake_usdc": 10, "symbol": "BTC"}]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    candidates = load_scan_candidates(log_path)
    fills = build_paper_fills(candidates)

    assert len(fills) == 2
    assert fills[0].slug == "btc"
    assert fills[0].side == "UP"
    assert fills[0].price == 0.40
    assert fills[1].side == "DOWN"


def test_evaluate_fills_reports_realized_pnl() -> None:
    fills = [
        PaperFill("btc", "BTC", "UP", price=0.40, stake_usdc=10.0, scanned_at_ms=1000, raw={}),
        PaperFill("eth", "ETH", "DOWN", price=0.70, stake_usdc=10.0, scanned_at_ms=1000, raw={}),
        PaperFill("sol", "SOL", "UP", price=0.60, stake_usdc=10.0, scanned_at_ms=1000, raw={}),
    ]
    resolved = {
        "btc": ResolvedMarket("btc", winning_outcome_index=0),
        "eth": ResolvedMarket("eth", winning_outcome_index=0),
    }

    report = evaluate_fills(fills, resolved)

    assert report["resolved_count"] == 2
    assert report["unresolved_count"] == 1
    assert report["wins"] == 1
    assert report["losses"] == 1
    assert report["realized_pnl_usdc"] == 5.0


def test_risk_manager_blocks_daily_loss_and_duplicate_market() -> None:
    risk = RiskManager(RiskConfig(max_daily_loss_usdc=25.0, max_open_markets=2))
    ledger = RiskLedger(realized_pnl_usdc=-30.0, open_slugs={"btc"})

    assert risk.can_open({"slug": "eth", "stake_usdc": 10.0}, ledger).allowed is False
    assert risk.can_open({"slug": "btc", "stake_usdc": 10.0}, RiskLedger(open_slugs={"btc"})).reason == "duplicate_market"
