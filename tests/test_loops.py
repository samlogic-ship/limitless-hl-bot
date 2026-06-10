"""Guards for the automatic loops: gatekeeper gates and EV-model refit."""
from __future__ import annotations

import json
import random
import sqlite3

from limitless_hl.ev_model import auc, fit_logistic, predict, refit
from limitless_hl.gatekeeper import evaluate_gates


def _learner_db(path, rows):
    """rows: list of (strategy, interval, price, raw_json, won, pnl, ts_ms)"""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE trades (trade_key TEXT PRIMARY KEY, strategy TEXT, interval TEXT,"
        " price REAL, side TEXT DEFAULT 'UP', raw_json TEXT, ts_ms INTEGER)"
    )
    con.execute(
        "CREATE TABLE resolutions (trade_key TEXT PRIMARY KEY, won INTEGER,"
        " pnl_usdc REAL, resolved_at_ms INTEGER)"
    )
    for i, (strat, iv, price, raw, won, pnl, ts) in enumerate(rows):
        k = f"t{i}"
        con.execute(
            "INSERT INTO trades (trade_key, strategy, interval, price, raw_json, ts_ms)"
            " VALUES (?,?,?,?,?,?)", (k, strat, iv, price, raw, ts))
        con.execute(
            "INSERT INTO resolutions VALUES (?,?,?,?)", (k, won, pnl, ts + 1000))
    con.commit()
    con.close()


def _slice_raw(score=2.5, edge=0.05):
    return json.dumps({"candidate": {"score": score, "edge": edge}})


def _conviction_raw(stake=80.0):
    return json.dumps({"candidate": {"shark_stake_usdc": stake}})


def test_copy_gate_opens_on_positive_ev(tmp_path):
    db = tmp_path / "l.sqlite3"
    rows = []
    # 120 conviction copies (shark stake >= $50), 40% WR, winners pay 2.0
    for i in range(120):
        won = 1 if i % 5 < 2 else 0
        rows.append(("copy_shadow", "15m", 0.4, _conviction_raw(), won,
                     2.0 if won else -1.0, 1781085600001 + i))
    _learner_db(db, rows)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    gates = evaluate_gates(con, min_n=100, since_ms=1781085600000)
    assert gates["copy"]["passed"] is True
    assert gates["fade"]["passed"] is False  # no fade trades
    assert gates["model"]["passed"] is False


def test_copy_gate_stays_closed_when_unprofitable(tmp_path):
    db = tmp_path / "l.sqlite3"
    rows = []
    # 120 trades, 60% WR but wins pay 0.3 vs -1 losses -> negative EV
    for i in range(120):
        won = 1 if i % 5 < 3 else 0
        rows.append(("copy_shadow", "15m", 0.75, _conviction_raw(), won,
                     0.3 if won else -1.0, 1781085600001 + i))
    _learner_db(db, rows)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    gates = evaluate_gates(con, min_n=100, since_ms=1781085600000)
    assert gates["copy"]["passed"] is False  # high WR is not enough


def test_model_gate_uses_slice_filters(tmp_path):
    db = tmp_path / "l.sqlite3"
    rows = []
    # 110 in-slice winners-enough trades
    for i in range(110):
        won = 1 if i % 10 < 6 else 0
        rows.append(("shadow_daemon", "15m", 0.45, _slice_raw(), won,
                     1.1 if won else -1.0, 1781085600001 + i))
    # 200 out-of-slice losers must NOT count (low score)
    for i in range(200):
        rows.append(("shadow_daemon", "15m", 0.45, _slice_raw(score=0.5), 0,
                     -1.0, 1781085600001 + i))
    _learner_db(db, rows)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    gates = evaluate_gates(con, min_n=100, since_ms=1781085600000)
    st = gates["model"]["stats"]
    assert st["n"] == 110
    assert gates["model"]["passed"] is True


def test_logistic_learns_separable_signal():
    random.seed(7)
    x, y = [], []
    for _ in range(400):
        sig = random.uniform(-1, 1)
        noise = random.uniform(-1, 1)
        x.append([sig, noise])
        y.append(1 if sig + random.gauss(0, 0.3) > 0 else 0)
    w = fit_logistic(x, y)
    scores = [predict(w, xi) for xi in x]
    assert auc(scores, y) > 0.8
    assert abs(w[1]) > abs(w[2])  # signal weight dominates noise weight


def test_refit_inactive_on_small_sample(tmp_path):
    db = tmp_path / "l.sqlite3"
    raw = json.dumps({"candidate": {"score_features": {
        "momentum_1m_bps": 1.0, "momentum_3m_bps": 2.0, "momentum_5m_bps": 3.0,
        "funding": 0.0, "binance_taker_imbalance_1m": 0.1, "hl_mid": 100.0},
        "threshold_price": 100.0}})
    rows = [("shadow_daemon", "15m", 0.5, raw, i % 2, 0.0, 1781085600001 + i)
            for i in range(60)]
    _learner_db(db, rows)
    model = refit(str(db), ["shadow_daemon"], min_n=500, min_auc=0.55)
    assert model["n"] == 60
    assert model["active"] is False  # never active below min_n
    assert "auc_test" in model


def test_copy_gate_ignores_small_stake_copies(tmp_path):
    db = tmp_path / "l.sqlite3"
    rows = []
    # profitable but ALL small-stake (noise) copies: gate must not open
    for i in range(120):
        won = 1 if i % 5 < 2 else 0
        rows.append(("copy_shadow", "15m", 0.4, _conviction_raw(stake=5.0), won,
                     2.0 if won else -1.0, 1781085600001 + i))
    _learner_db(db, rows)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    gates = evaluate_gates(con, min_n=100, since_ms=1781085600000)
    assert gates["copy"]["stats"]["n"] == 0
    assert gates["copy"]["passed"] is False
