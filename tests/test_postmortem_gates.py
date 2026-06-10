"""Guards for the 2026-06-10 postmortem fixes.

These encode structural findings from the first live day:
- funding signals fired on HL's resting default rate (1.25e-05) — a constant,
  not a spike — and went 6/22 for -$34.91;
- sub-$0.20 entries went 0/10 (the 3% taker fee makes longshots unwinnable);
- claimed edge > 0.12 was adverse selection (29% WR vs 67% for small edges).
Do not delete these to make a strategy "tradeable" again.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from limitless_hl.daemon import _in_loss_cooldown, build_parser
from limitless_hl.funding_daemon import BASELINE_EPSILON, HL_BASELINE_FUNDING, SIGNALS


def _funding_triggered(sig, rate: float) -> bool:
    triggered = (
        (sig.compare == "gte" and rate >= sig.threshold)
        or (sig.compare == "lte" and rate <= sig.threshold)
    )
    if triggered and abs(abs(rate) - HL_BASELINE_FUNDING) < BASELINE_EPSILON:
        triggered = False
    return triggered


def test_funding_baseline_rate_never_triggers():
    for sig in SIGNALS:
        assert not _funding_triggered(sig, HL_BASELINE_FUNDING), sig.coin
        assert not _funding_triggered(sig, -HL_BASELINE_FUNDING), sig.coin


def test_funding_genuine_deviation_still_triggers():
    btc = next(s for s in SIGNALS if s.coin == "BTC")
    assert _funding_triggered(btc, 3.0e-05)
    sol = next(s for s in SIGNALS if s.coin == "SOL")
    assert _funding_triggered(sol, -5.0e-05)


def test_daemon_parser_has_postmortem_gates():
    args = build_parser().parse_args([])
    assert args.min_price == 0.0  # off by default, armed in ecosystem config
    assert args.max_edge == 1.0
    assert args.max_trades_per_hour == 0
    assert args.loss_cooldown_losses == 0


def _make_learner_db(path: Path, outcomes: list[int], resolved_at_ms: int) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE trades (trade_key TEXT PRIMARY KEY, source_path TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE resolutions (trade_key TEXT PRIMARY KEY, won INTEGER NOT NULL,"
        " resolved_at_ms INTEGER NOT NULL)"
    )
    for i, won in enumerate(outcomes):
        key = f"t{i}"
        con.execute(
            "INSERT INTO trades VALUES (?, ?)",
            (key, "tmp/limitless_hl/daemon_trades.jsonl"),
        )
        con.execute(
            "INSERT INTO resolutions VALUES (?, ?, ?)",
            (key, won, resolved_at_ms - (len(outcomes) - i) * 1000),
        )
    con.commit()
    con.close()


def test_loss_cooldown_active_after_streak(tmp_path):
    now_ms = int(time.time() * 1000)
    db = tmp_path / "learner.sqlite3"
    _make_learner_db(db, [1, 0, 0, 0], resolved_at_ms=now_ms - 60_000)
    assert _in_loss_cooldown(
        db,
        source_path="tmp/limitless_hl/daemon_trades.jsonl",
        losses=3,
        cooldown_ms=1_800_000,
        now_ms=now_ms,
    )


def test_loss_cooldown_expires(tmp_path):
    now_ms = int(time.time() * 1000)
    db = tmp_path / "learner.sqlite3"
    _make_learner_db(db, [0, 0, 0], resolved_at_ms=now_ms - 3_600_000)
    assert not _in_loss_cooldown(
        db,
        source_path="tmp/limitless_hl/daemon_trades.jsonl",
        losses=3,
        cooldown_ms=1_800_000,
        now_ms=now_ms,
    )


def test_loss_cooldown_clear_after_win(tmp_path):
    now_ms = int(time.time() * 1000)
    db = tmp_path / "learner.sqlite3"
    _make_learner_db(db, [0, 0, 1], resolved_at_ms=now_ms - 60_000)
    assert not _in_loss_cooldown(
        db,
        source_path="tmp/limitless_hl/daemon_trades.jsonl",
        losses=3,
        cooldown_ms=1_800_000,
        now_ms=now_ms,
    )


def test_hl_info_cache_roundtrip(tmp_path, monkeypatch):
    import limitless_hl.hl_info as hl_info

    monkeypatch.setattr(hl_info, "CACHE_DIR", tmp_path / "cache")
    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"BTC": "100000.0"}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        return FakeResp()

    monkeypatch.setattr(hl_info._SESSION, "post", fake_post)
    a = hl_info.post_info({"type": "allMids"})
    b = hl_info.post_info({"type": "allMids"})
    assert a == b == {"BTC": "100000.0"}
    assert calls["n"] == 1  # second call served from cache


def test_hl_info_serves_stale_on_upstream_failure(tmp_path, monkeypatch):
    import limitless_hl.hl_info as hl_info

    monkeypatch.setattr(hl_info, "CACHE_DIR", tmp_path / "cache")

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": 1}

    monkeypatch.setattr(
        hl_info._SESSION, "post", lambda url, json=None, timeout=None: FakeResp()
    )
    assert hl_info.post_info({"type": "metaAndAssetCtxs"}) == {"ok": 1}

    def raise_429(url, json=None, timeout=None):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(hl_info._SESSION, "post", raise_429)
    # TTL of 0.0 forces an upstream attempt, which fails -> stale cache path...
    # but ttl=0 also disables the cache, so use a tiny ttl and wait it out.
    time.sleep(0.05)
    assert hl_info.post_info({"type": "metaAndAssetCtxs"}, ttl_seconds=0.01) == {"ok": 1}
