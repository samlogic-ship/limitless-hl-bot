from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from limitless_hl.attribution import ResolvedMarket
from limitless_hl.daemon import _load_slice_scores, _load_slice_stats
from limitless_hl.learner import connect, ingest_jsonl, run_once, trade_from_event


class FakeLimitlessClient:
    def __init__(self, outcomes: dict[str, int | None]):
        self.outcomes = outcomes

    def resolved_market(self, slug: str) -> ResolvedMarket:
        return ResolvedMarket(slug=slug, winning_outcome_index=self.outcomes.get(slug), raw={"slug": slug})


def _daemon_event() -> dict[str, Any]:
    return {
        "event": "trade",
        "ts_ms": 1000,
        "state": "limitless_filled_unhedged",
        "candidate": {
            "slug": "btc-up-or-down-15-min-1",
            "symbol": "BTC",
            "interval": "15m",
            "side": "UP",
            "limit_price": 0.40,
            "score": 2.4,
            "score_features": {"momentum_1m_bps": 8.0},
        },
        "limitless_result": {"matched": True, "filled_usdc": 2.0},
    }


def _funding_event() -> dict[str, Any]:
    return {
        "event": "trade",
        "state": "filled",
        "ts_ms": 2000,
        "slug": "bnb-up-or-down-15-min-1",
        "coin": "BNB",
        "direction": "DOWN",
        "entry_price": 0.50,
        "filled_usdc": 1.0,
        "kelly_stake_usdc": 1.0,
        "rate": 0.0000125,
    }


def test_trade_from_event_parses_daemon_and_funding_live_fills() -> None:
    daemon = trade_from_event(_daemon_event(), source="daemon", source_path="daemon.jsonl", line_no=1)
    funding = trade_from_event(_funding_event(), source="funding", source_path="funding.jsonl", line_no=1)

    assert daemon is not None
    assert daemon.symbol == "BTC"
    assert daemon.interval == "15m"
    assert daemon.side == "UP"
    assert daemon.price == 0.40
    assert daemon.stake_usdc == 2.0
    assert daemon.strategy == "scored_daemon"

    assert funding is not None
    assert funding.symbol == "BNB"
    assert funding.interval == "15m"
    assert funding.side == "DOWN"
    assert funding.strategy == "funding_kelly"


def test_trade_from_event_marks_dry_daemon_as_shadow() -> None:
    payload = _daemon_event() | {"mode": "dry_run"}
    trade = trade_from_event(payload, source="daemon", source_path="daemon_shadow.jsonl", line_no=1)

    assert trade is not None
    assert trade.strategy == "shadow_daemon"


def test_trade_from_event_marks_dry_funding_as_shadow() -> None:
    payload = _funding_event() | {"mode": "dry_run"}
    trade = trade_from_event(payload, source="funding", source_path="funding_dry.jsonl", line_no=1)

    assert trade is not None
    assert trade.strategy == "shadow_funding"


def test_ingest_jsonl_is_idempotent(tmp_path: Path) -> None:
    log = tmp_path / "daemon_trades.jsonl"
    log.write_text(json.dumps(_daemon_event()) + "\n", encoding="utf-8")

    with connect(tmp_path / "learner.sqlite3") as conn:
        assert ingest_jsonl(conn, [log], now_ms=3000) == 1
        assert ingest_jsonl(conn, [log], now_ms=4000) == 0
        count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    assert count == 1


def test_run_once_resolves_trades_and_writes_daemon_compatible_report(tmp_path: Path) -> None:
    daemon_log = tmp_path / "daemon_trades.jsonl"
    funding_log = tmp_path / "funding_trades.jsonl"
    daemon_log.write_text(json.dumps(_daemon_event()) + "\n", encoding="utf-8")
    funding_log.write_text(json.dumps(_funding_event()) + "\n", encoding="utf-8")

    report_path = tmp_path / "evaluation_report_live.json"
    report = run_once(
        db_path=tmp_path / "learner.sqlite3",
        logs=[daemon_log, funding_log],
        report_out=report_path,
        client=FakeLimitlessClient({
            "btc-up-or-down-15-min-1": 0,
            "bnb-up-or-down-15-min-1": 1,
        }),  # type: ignore[arg-type]
        now_ms=5000,
    )

    assert report["resolved_count"] == 2
    assert report["wins"] == 2
    assert report["realized_pnl_usdc"] == 4.0
    assert report_path.exists()

    scores = _load_slice_scores(report_path, min_n=1, min_roi=0.01, min_win_rate=0.5)
    stats = _load_slice_stats(report_path)

    assert ("15m", "BTC", "UP") in scores
    assert ("15m", "BNB", "DOWN") in scores
    assert stats[("15m", "BTC", "UP")].roi == 1.5


def test_seed_report_keeps_historical_slice_available(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps({
            "resolved": [
                {
                    "fill": {
                        "slug": "hype-seed",
                        "symbol": "HYPE",
                        "side": "UP",
                        "price": 0.5,
                        "stake_usdc": 1.0,
                        "scanned_at_ms": 1,
                        "raw": {"interval": "15m"},
                    },
                    "won": True,
                    "pnl_usdc": 1.0,
                    "resolved_market": {"slug": "hype-seed", "winning_outcome_index": 0},
                }
            ]
        }),
        encoding="utf-8",
    )
    report_path = tmp_path / "live.json"

    report = run_once(
        db_path=tmp_path / "learner.sqlite3",
        logs=[],
        report_out=report_path,
        client=FakeLimitlessClient({}),  # type: ignore[arg-type]
        seed_reports=[seed],
        now_ms=5000,
    )

    # Headline is live-only; the seeded row lives under seeded/combined.
    assert report["resolved_count"] == 0
    assert report["seeded"]["resolved_count"] == 1
    assert report["combined"]["resolved_count"] == 1
    assert _load_slice_scores(report_path, min_n=1, min_roi=0.01, min_win_rate=0.5) == {("15m", "HYPE", "UP")}
