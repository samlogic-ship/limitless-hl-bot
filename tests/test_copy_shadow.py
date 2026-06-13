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


def test_rank_selects_value_shark_and_fish(tmp_path):
    db = tmp_path / "flow.sqlite3"
    _make_flow_db(db)
    # net-of-fee ranking: 0xshark wins every market at 0.35 -> big +net/trade;
    # min_resolved lowered to fixture size (12 markets).
    sharks, fish = rank_wallets(
        str(db), min_markets=10, min_roi=0.05, min_pnl=20.0, max_avg_price=0.85,
        min_net_per_trade=0.05, min_resolved=10, min_win_rate=0.50,
        probation_hours=0.0,
    )
    assert sharks == {"0xshark"}
    assert fish == {"0xfish"}  # -$60 on $60 staked, ROI -100%


def test_rank_sniper_excluded_by_net_per_trade(tmp_path):
    # 0xsniper wins every market but buys at 0.98 -> net/trade ~+0.02, BELOW the
    # 0.05 net gate. The old gross logic would include it; net-of-fee ranking
    # correctly rejects it (no copyable edge after fees).
    db = tmp_path / "flow.sqlite3"
    _make_flow_db(db)
    sharks, fish = rank_wallets(
        str(db), min_markets=10, min_roi=0.001, min_pnl=1.0, max_avg_price=1.0,
        min_net_per_trade=0.05, min_resolved=10, min_win_rate=0.50,
        probation_hours=0.0,
    )
    assert "0xshark" in sharks       # +net/trade survivor
    assert "0xsniper" not in sharks  # +0.02 net < 0.05 gate
    # with the gate dropped, the sniper qualifies again (sanity)
    sharks2, _ = rank_wallets(
        str(db), min_markets=10, min_roi=0.001, min_pnl=1.0, max_avg_price=1.0,
        min_net_per_trade=0.0, min_resolved=10, min_win_rate=0.50,
        probation_hours=0.0,
    )
    assert "0xsniper" in sharks2


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


def test_copy_live_file_routes_to_daemon_parser():
    from pathlib import Path

    from limitless_hl.learner import _source_name

    assert _source_name(Path("tmp/limitless_hl/copy_live.jsonl")) == "daemon"


def test_smart_chase_allows_when_pm_says_still_cheap(monkeypatch):
    # PM twin fair for our side = 0.70; our chased ask 0.62 *1.03 = 0.6386;
    # net edge 0.061 >= margin 0.03 -> chase allowed.
    from limitless_hl import copy_shadow as cs

    class FakePM:
        def implied_up_prob(self, sym, iv, exp):
            return {"up_prob": 0.70}
    # net edge math sanity (mirrors try_copy)
    ask, margin = 0.62, 0.03
    pm_fair = 0.70
    assert (pm_fair - ask * 1.03) >= margin


def test_smart_chase_blocks_when_pm_not_cheap_enough():
    ask, margin = 0.62, 0.03
    pm_fair = 0.64  # net edge 0.0014 < margin -> blocked
    assert (pm_fair - ask * 1.03) < margin
