"""Guards for the copy_shadow lane (2026-06-10)."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from limitless_hl.copy_shadow import rank_wallets


def _make_flow_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE markets (slug TEXT PRIMARY KEY, symbol TEXT, interval TEXT,"
        " expiration_ms INTEGER, token_up TEXT, token_down TEXT,"
        " resolved INTEGER DEFAULT 0, winning_outcome TEXT, last_event_ms INTEGER DEFAULT 0)"
    )
    con.execute(
        "CREATE TABLE trades (tx_hash TEXT, market_slug TEXT, account TEXT,"
        " profile_id INTEGER, username TEXT, rank_name TEXT, side INTEGER,"
        " token_id TEXT, outcome TEXT, price REAL, shares REAL, collateral REAL,"
        " created_at_ms INTEGER)"
    )
    for i in range(12):
        slug = f"m{i}"
        con.execute(
            "INSERT INTO markets (slug, symbol, interval, expiration_ms, resolved,"
            " winning_outcome) VALUES (?, 'BTC', '15m', ?, 1, 'UP')",
            (slug, 1000 + i),
        )
        # value shark: buys UP cheap, wins every market
        con.execute(
            "INSERT INTO trades (market_slug, account, side, outcome, price, shares,"
            " collateral, created_at_ms) VALUES (?, '0xshark', 0, 'UP', 0.35, 10, 3.5, ?)",
            (slug, 100 + i),
        )
        # sniper: buys UP at 0.98 (excluded by max_avg_price)
        con.execute(
            "INSERT INTO trades (market_slug, account, side, outcome, price, shares,"
            " collateral, created_at_ms) VALUES (?, '0xsniper', 0, 'UP', 0.98, 10, 9.8, ?)",
            (slug, 100 + i),
        )
        # fish: buys DOWN, loses every market
        con.execute(
            "INSERT INTO trades (market_slug, account, side, outcome, price, shares,"
            " collateral, created_at_ms) VALUES (?, '0xfish', 0, 'DOWN', 0.5, 10, 5.0, ?)",
            (slug, 100 + i),
        )
    con.commit()
    con.close()


def test_rank_selects_value_shark_only(tmp_path):
    db = tmp_path / "flow.sqlite3"
    _make_flow_db(db)
    sharks = rank_wallets(
        str(db), min_markets=10, min_roi=0.05, min_pnl=20.0, max_avg_price=0.85
    )
    assert sharks == {"0xshark"}


def test_rank_sniper_included_when_price_cap_lifted(tmp_path):
    db = tmp_path / "flow.sqlite3"
    _make_flow_db(db)
    sharks = rank_wallets(
        str(db), min_markets=10, min_roi=0.001, min_pnl=1.0, max_avg_price=1.0
    )
    assert "0xsniper" in sharks and "0xshark" in sharks
    assert "0xfish" not in sharks


def test_copy_record_is_learner_compatible():
    # the learner's _daemon_trade accepts: state in {hedged,...}, matched fill,
    # candidate slug/side/limit_price; strategy override must survive.
    from limitless_hl.learner import _daemon_trade

    payload = {
        "event": "trade",
        "mode": "dry_run",
        "state": "hedged",
        "strategy": "copy_shadow",
        "candidate": {
            "slug": "btc-up-or-down-15-min-1",
            "symbol": "BTC",
            "interval": "15m",
            "side": "UP",
            "limit_price": 0.41,
            "stake_usdc": 1.0,
        },
        "limitless_result": {"matched": True, "filled_usdc": 1.0},
        "ts_ms": 1781100000000,
    }
    trade = _daemon_trade(payload, source="daemon", source_path="x.jsonl", line_no=1)
    assert trade is not None
    assert trade.strategy == "copy_shadow"
    assert trade.price == 0.41
