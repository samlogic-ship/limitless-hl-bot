"""Limitless flow recorder + wallet ("shark") scorer.

Limitless publishes every CLOB trade with the trader's identity on the public
``/markets/{slug}/events`` feed (verified live 2026-06-10: profile.account,
price, side, matchedSize, tokenId, txHash, createdAt). This daemon:

  1. tracks active crypto markets and their UP/DOWN token ids,
  2. incrementally records every trade event into SQLite,
  3. sweeps resolutions for expired markets,
  4. periodically scores wallets on REALIZED outcomes only and writes a
     leaderboard JSON consumed by the maker (adverse-selection caution) and,
     later, copy/fade lanes.

PnL accounting is CLOB-only: buys add signed shares at cost, sells remove
them; at resolution net winner-side shares pay $1. Mint/split flows are not
visible on this feed, so wallets that mint full sets (house-MM style) score
approximately — fine, the targets are taker sharks.

Read-only against the exchange. No order placement, no keys.
"""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests

from .clients import LimitlessClient

BASE_URL = "https://api.limitless.exchange"

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    slug TEXT PRIMARY KEY,
    symbol TEXT,
    interval TEXT,
    expiration_ms INTEGER,
    token_up TEXT,
    token_down TEXT,
    resolved INTEGER DEFAULT 0,
    winning_outcome TEXT,
    last_event_ms INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
    tx_hash TEXT,
    market_slug TEXT,
    account TEXT,
    profile_id INTEGER,
    username TEXT,
    rank_name TEXT,
    side INTEGER,            -- 0 = buy, 1 = sell (raw from feed)
    token_id TEXT,
    outcome TEXT,            -- UP / DOWN / NULL if token unmapped
    price REAL,
    shares REAL,             -- matchedSize / 1e6
    collateral REAL,         -- shares * price
    created_at_ms INTEGER,
    UNIQUE(tx_hash, account, side, token_id, price, shares, created_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_slug);
"""


def _parse_iso_ms(value: str | None) -> int:
    if not value:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


class FlowRecorder:
    def __init__(
        self,
        db_path: Path,
        out_path: Path,
        client: LimitlessClient | None = None,
        intervals: tuple[str, ...] = ("5m", "15m", "1h", "1d"),
        page_limit: int = 50,
        max_pages_per_market: int = 8,
        timeout: float = 10.0,
    ):
        self.client = client or LimitlessClient()
        self.intervals = intervals
        self.page_limit = page_limit
        self.max_pages_per_market = max_pages_per_market
        self.timeout = timeout
        self.out_path = out_path
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(SCHEMA)
        self.db.commit()

    # -- logging --------------------------------------------------------------

    def _log(self, payload: dict[str, Any]) -> None:
        payload.setdefault("ts_ms", int(time.time() * 1000))
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with self.out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    # -- market tracking --------------------------------------------------------

    def refresh_markets(self) -> int:
        """Upsert active crypto markets; fetch token map once per new slug."""
        added = 0
        try:
            markets = [m for m in self.client.active_crypto_markets() if m.interval in self.intervals]
        except Exception as exc:
            self._log({"event": "markets_error", "error": str(exc)[:200]})
            return 0
        known = {row[0] for row in self.db.execute("SELECT slug FROM markets")}
        for m in markets:
            if m.slug in known:
                continue
            token_up = token_down = None
            try:
                details = self.client.market_details(m.slug)
                tokens = details.get("tokens") or {}
                token_up = str(tokens.get("yes") or "") or None
                token_down = str(tokens.get("no") or "") or None
            except Exception:
                pass
            self.db.execute(
                "INSERT OR IGNORE INTO markets(slug, symbol, interval, expiration_ms, token_up, token_down)"
                " VALUES (?,?,?,?,?,?)",
                (m.slug, m.symbol, m.interval, m.expiration_ms, token_up, token_down),
            )
            added += 1
        self.db.commit()
        return added

    # -- trade events -------------------------------------------------------------

    def _fetch_events_page(self, slug: str, page: int) -> tuple[list[dict[str, Any]], int]:
        resp = self.session.get(
            f"{BASE_URL}/markets/{slug}/events",
            params={"page": page, "limit": self.page_limit},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return [], 0
        payload = resp.json()
        return payload.get("events") or [], int(payload.get("totalPages") or 0)

    def collect_market(self, slug: str, token_up: str | None, token_down: str | None, last_event_ms: int) -> int:
        """Pull pages newest-first until overlap with what we already stored."""
        inserted = 0
        newest_seen = last_event_ms
        for page in range(1, self.max_pages_per_market + 1):
            try:
                rows, total_pages = self._fetch_events_page(slug, page)
            except Exception as exc:
                self._log({"event": "events_error", "slug": slug, "error": str(exc)[:200]})
                break
            if not rows:
                break
            page_oldest = None
            for row in rows:
                created_ms = _parse_iso_ms(row.get("createdAt"))
                page_oldest = created_ms if page_oldest is None else min(page_oldest, created_ms)
                newest_seen = max(newest_seen, created_ms)
                profile = row.get("profile") or {}
                token_id = str(row.get("tokenId") or "")
                outcome = "UP" if token_id == token_up else "DOWN" if token_id == token_down else None
                try:
                    price = float(row.get("price") or 0)
                    shares = float(row.get("matchedSize") or 0) / 1_000_000
                except Exception:
                    continue
                if price <= 0 or shares <= 0:
                    continue
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO trades(tx_hash, market_slug, account, profile_id, username,"
                    " rank_name, side, token_id, outcome, price, shares, collateral, created_at_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(row.get("txHash") or ""), slug,
                        str(profile.get("account") or "").lower(),
                        profile.get("id"),
                        profile.get("username"),
                        profile.get("rankName"),
                        int(row.get("side") or 0),
                        token_id, outcome, price, shares, price * shares, created_ms,
                    ),
                )
                inserted += cur.rowcount if cur.rowcount > 0 else 0
            # stop paging once this page is fully older than what we had
            if last_event_ms and page_oldest is not None and page_oldest <= last_event_ms:
                break
            if page >= total_pages:
                break
        if newest_seen > last_event_ms:
            self.db.execute("UPDATE markets SET last_event_ms=? WHERE slug=?", (newest_seen, slug))
        self.db.commit()
        return inserted

    def collect_all(self, now_ms: int | None = None) -> int:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        rows = self.db.execute(
            "SELECT slug, token_up, token_down, last_event_ms, expiration_ms FROM markets"
            " WHERE resolved=0 AND expiration_ms > ? - 7200000",
            (now,),
        ).fetchall()
        total = 0
        for slug, token_up, token_down, last_ms, _exp in rows:
            total += self.collect_market(slug, token_up, token_down, last_ms or 0)
        return total

    # -- resolutions ---------------------------------------------------------------

    def sweep_resolutions(self, now_ms: int | None = None, max_checks: int = 25) -> int:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        rows = self.db.execute(
            "SELECT slug FROM markets WHERE resolved=0 AND expiration_ms < ? - 90000"
            " ORDER BY expiration_ms ASC LIMIT ?",
            (now, max_checks),
        ).fetchall()
        resolved = 0
        for (slug,) in rows:
            try:
                rm = self.client.resolved_market(slug)
            except Exception:
                continue
            idx = getattr(rm, "winning_outcome_index", None)
            if idx not in (0, 1):
                continue  # not resolved yet — retry next sweep
            outcome = "UP" if idx == 0 else "DOWN"
            self.db.execute(
                "UPDATE markets SET resolved=1, winning_outcome=? WHERE slug=?", (outcome, slug)
            )
            resolved += 1
        self.db.commit()
        return resolved

    # -- scoring --------------------------------------------------------------------

    def score(self, min_markets: int = 8, min_roi: float = 0.05) -> dict[str, Any]:
        """Realized per-wallet PnL over resolved markets -> leaderboard dict."""
        query = """
        SELECT t.account,
               t.market_slug,
               m.winning_outcome,
               t.outcome,
               SUM(CASE WHEN t.side = 0 THEN t.shares ELSE -t.shares END) AS net_shares,
               SUM(CASE WHEN t.side = 0 THEN t.collateral ELSE -t.collateral END) AS net_cost,
               SUM(CASE WHEN t.side = 0 THEN t.collateral ELSE 0 END) AS bought_volume,
               COUNT(*) AS n_trades,
               MAX(t.created_at_ms) AS last_ms
        FROM trades t
        JOIN markets m ON m.slug = t.market_slug
        WHERE m.resolved = 1 AND t.outcome IS NOT NULL AND t.account != ''
        GROUP BY t.account, t.market_slug, t.outcome
        """
        wallets: dict[str, dict[str, Any]] = {}
        per_market: dict[tuple[str, str], dict[str, float]] = {}
        for account, slug, winner, outcome, net_shares, net_cost, bought, n_trades, last_ms in self.db.execute(query):
            key = (account, slug)
            entry = per_market.setdefault(key, {"payout": 0.0, "cost": 0.0, "volume": 0.0, "trades": 0, "last_ms": 0})
            if outcome == winner:
                entry["payout"] += net_shares  # $1 per net winning share
            entry["cost"] += net_cost
            entry["volume"] += bought
            entry["trades"] += int(n_trades)
            entry["last_ms"] = max(entry["last_ms"], int(last_ms or 0))
        for (account, _slug), entry in per_market.items():
            w = wallets.setdefault(account, {
                "account": account, "n_markets": 0, "n_trades": 0,
                "volume_usdc": 0.0, "realized_pnl": 0.0, "wins": 0, "last_seen_ms": 0,
            })
            pnl = entry["payout"] - entry["cost"]
            w["n_markets"] += 1
            w["n_trades"] += entry["trades"]
            w["volume_usdc"] += entry["volume"]
            w["realized_pnl"] += pnl
            w["wins"] += 1 if pnl > 0 else 0
            w["last_seen_ms"] = max(w["last_seen_ms"], entry["last_ms"])
        scored = []
        for w in wallets.values():
            w["win_rate"] = w["wins"] / w["n_markets"] if w["n_markets"] else 0.0
            w["roi"] = w["realized_pnl"] / w["volume_usdc"] if w["volume_usdc"] > 0 else 0.0
            scored.append(w)
        scored.sort(key=lambda w: w["realized_pnl"], reverse=True)
        qualified = [w for w in scored if w["n_markets"] >= min_markets]
        top_n = max(1, len(qualified) // 10) if qualified else 0
        sharks = [w["account"] for w in qualified[:top_n] if w["realized_pnl"] > 0 and w["roi"] >= min_roi]
        fish = [w["account"] for w in sorted(qualified, key=lambda w: w["realized_pnl"])[:top_n]
                if w["realized_pnl"] < 0]
        return {
            "generated_at_ms": int(time.time() * 1000),
            "n_wallets": len(scored),
            "n_qualified": len(qualified),
            "min_markets": min_markets,
            "sharks": sharks,
            "fish": fish,
            "wallets": scored[:500],
        }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Limitless trade-flow recorder + wallet scorer")
    p.add_argument("--db", default="tmp/limitless_hl/flow.sqlite3")
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/flow_recorder.jsonl")
    p.add_argument("--scores-out", default="tmp/limitless_hl/shark_scores.json")
    p.add_argument("--intervals", default="5m,15m,1h,1d")
    p.add_argument("--loop-seconds", type=int, default=60)
    p.add_argument("--score-every-loops", type=int, default=15)
    p.add_argument("--min-markets", type=int, default=8)
    p.add_argument("--iterations", type=int, default=0)
    p.add_argument("--score-once", action="store_true", help="score the existing DB and exit")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    recorder = FlowRecorder(
        db_path=Path(args.db),
        out_path=Path(args.jsonl_out),
        intervals=tuple(s.strip() for s in args.intervals.split(",") if s.strip()),
    )
    scores_path = Path(args.scores_out)

    if args.score_once:
        board = recorder.score(min_markets=args.min_markets)
        scores_path.parent.mkdir(parents=True, exist_ok=True)
        scores_path.write_text(json.dumps(board, indent=1))
        print(json.dumps({k: board[k] for k in ("n_wallets", "n_qualified", "sharks", "fish")}, indent=1))
        return

    running = True

    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    recorder._log({"event": "startup", "intervals": args.intervals})
    iteration = 0
    while running:
        iteration += 1
        try:
            added = recorder.refresh_markets()
            inserted = recorder.collect_all()
            resolved = recorder.sweep_resolutions()
            recorder._log({"event": "loop", "iteration": iteration, "markets_added": added,
                           "trades_inserted": inserted, "markets_resolved": resolved})
            if iteration % max(args.score_every_loops, 1) == 0:
                board = recorder.score(min_markets=args.min_markets)
                scores_path.parent.mkdir(parents=True, exist_ok=True)
                scores_path.write_text(json.dumps(board, indent=1))
                recorder._log({"event": "scored", "n_wallets": board["n_wallets"],
                               "n_qualified": board["n_qualified"],
                               "n_sharks": len(board["sharks"]), "n_fish": len(board["fish"])})
        except Exception as exc:
            recorder._log({"event": "loop_error", "error": str(exc)[:300]})
        if args.iterations and iteration >= args.iterations:
            break
        time.sleep(max(args.loop_seconds, 5))
    recorder._log({"event": "shutdown"})


if __name__ == "__main__":
    main()
