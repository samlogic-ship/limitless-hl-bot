from pathlib import Path

from limitless_hl.flow_recorder import FlowRecorder, _parse_iso_ms


class _StubClient:
    def active_crypto_markets(self):
        return []

    def market_details(self, slug):
        return {}

    def resolved_market(self, slug):
        raise RuntimeError("not used in these tests")


def _recorder(tmp_path: Path) -> FlowRecorder:
    return FlowRecorder(
        db_path=tmp_path / "flow.sqlite3",
        out_path=tmp_path / "flow.jsonl",
        client=_StubClient(),
    )


def _seed_market(rec: FlowRecorder, slug="btc-up-or-down-15-min-1", winner="UP"):
    rec.db.execute(
        "INSERT INTO markets(slug, symbol, interval, expiration_ms, token_up, token_down,"
        " resolved, winning_outcome) VALUES (?,?,?,?,?,?,1,?)",
        (slug, "BTC", "15m", 1_000, "tokU", "tokD", winner),
    )
    rec.db.commit()


def _insert_trade(rec, slug, account, side, token, price, shares, ts=500):
    outcome = "UP" if token == "tokU" else "DOWN"
    rec.db.execute(
        "INSERT OR IGNORE INTO trades(tx_hash, market_slug, account, profile_id, username,"
        " rank_name, side, token_id, outcome, price, shares, collateral, created_at_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"0x{account}{side}{token}{price}", slug, account, 1, None, "Silver",
         side, token, outcome, price, shares, price * shares, ts),
    )
    rec.db.commit()


def test_score_buy_winner_pays_out(tmp_path):
    rec = _recorder(tmp_path)
    _seed_market(rec, winner="UP")
    # buys 10 UP shares at 0.50 ($5 cost) -> payout $10, pnl +$5
    _insert_trade(rec, "btc-up-or-down-15-min-1", "0xshark", 0, "tokU", 0.50, 10.0)
    board = rec.score(min_markets=1)
    w = board["wallets"][0]
    assert w["account"] == "0xshark"
    assert abs(w["realized_pnl"] - 5.0) < 1e-9
    assert w["win_rate"] == 1.0
    assert "0xshark" in board["sharks"]


def test_score_buy_loser_loses_cost(tmp_path):
    rec = _recorder(tmp_path)
    _seed_market(rec, winner="DOWN")
    _insert_trade(rec, "btc-up-or-down-15-min-1", "0xfish", 0, "tokU", 0.40, 10.0)
    board = rec.score(min_markets=1)
    w = board["wallets"][0]
    assert abs(w["realized_pnl"] + 4.0) < 1e-9
    assert "0xfish" in board["fish"]


def test_score_sell_reduces_exposure(tmp_path):
    rec = _recorder(tmp_path)
    _seed_market(rec, winner="UP")
    # buy 10 UP @0.50, sell 4 UP @0.60 -> net 6 shares, net cost 5-2.4=2.6,
    # payout 6 -> pnl +3.4
    _insert_trade(rec, "btc-up-or-down-15-min-1", "0xtrader", 0, "tokU", 0.50, 10.0)
    _insert_trade(rec, "btc-up-or-down-15-min-1", "0xtrader", 1, "tokU", 0.60, 4.0, ts=600)
    board = rec.score(min_markets=1)
    w = board["wallets"][0]
    assert abs(w["realized_pnl"] - 3.4) < 1e-9


def test_score_both_sides_nets_like_maker_doge(tmp_path):
    # The maker's real first night: bought both sides, NO won.
    rec = _recorder(tmp_path)
    _seed_market(rec, winner="DOWN")
    _insert_trade(rec, "btc-up-or-down-15-min-1", "0xmaker", 0, "tokD", 0.5197, 15.392)
    _insert_trade(rec, "btc-up-or-down-15-min-1", "0xmaker", 0, "tokU", 0.4298, 9.306, ts=600)
    board = rec.score(min_markets=1)
    w = board["wallets"][0]
    expected = 15.392 * 1.0 - (0.5197 * 15.392 + 0.4298 * 9.306)
    assert abs(w["realized_pnl"] - expected) < 1e-6
    assert expected > 3.0  # the night's actual ~+$3.4


def test_duplicate_events_deduped(tmp_path):
    rec = _recorder(tmp_path)
    _seed_market(rec, winner="UP")
    for _ in range(3):
        _insert_trade(rec, "btc-up-or-down-15-min-1", "0xdup", 0, "tokU", 0.50, 10.0)
    n = rec.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert n == 1


def test_unqualified_wallets_are_not_sharks(tmp_path):
    rec = _recorder(tmp_path)
    _seed_market(rec, winner="UP")
    _insert_trade(rec, "btc-up-or-down-15-min-1", "0xlucky", 0, "tokU", 0.50, 10.0)
    board = rec.score(min_markets=8)  # one market only -> not qualified
    assert board["sharks"] == []
    assert board["n_qualified"] == 0


def test_parse_iso_ms():
    assert _parse_iso_ms("2026-06-09T21:56:26.825Z") == 1781042186825
    assert _parse_iso_ms(None) == 0
    assert _parse_iso_ms("garbage") == 0
