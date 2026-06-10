"""
limitless_hl/copy_shadow.py — paper-trade copies of profitable Limitless wallets.

The flow recorder attributes ~half of all taker prints to real wallets and
tracks market resolutions. This daemon ranks those wallets by realized PnL
(net-position accounting) and, when a currently-profitable wallet opens a new
position, records a dry-run copy at OUR currently fillable ask price (not
their fill). The learner ingests the jsonl and scores the lane as
strategy=copy_shadow. No orders are ever sent; mode is always dry_run.

Leaderboard criteria (refreshed every --rank-seconds from flow.sqlite3):
  >= --min-markets resolved markets, ROI >= --min-roi, PnL >= --min-pnl,
  average entry price <= --max-avg-price (excludes last-second snipers whose
  edge is latency we cannot copy).
Copy rules: fresh buys only, >= --min-seconds-to-expiry left, our ask within
  --max-chase of the shark's fill, ask inside [--min-price, --max-price],
  one copy per (slug, side).
"""
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .clients import LimitlessClient


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Shadow-copy profitable Limitless wallets")
    p.add_argument("--flow-db", default="tmp/limitless_hl/flow.sqlite3")
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/copy_shadow.jsonl")
    p.add_argument("--loop-seconds", type=int, default=30)
    p.add_argument("--rank-seconds", type=int, default=900)
    p.add_argument("--min-markets", type=int, default=10)
    p.add_argument("--min-roi", type=float, default=0.05)
    p.add_argument("--min-pnl", type=float, default=20.0)
    p.add_argument("--max-avg-price", type=float, default=0.85)
    p.add_argument("--min-seconds-to-expiry", type=int, default=120)
    p.add_argument("--max-chase", type=float, default=0.06)
    p.add_argument("--min-price", type=float, default=0.05)
    p.add_argument("--max-price", type=float, default=0.92)
    p.add_argument("--stake-usdc", type=float, default=1.0)
    p.add_argument("--iterations", type=int, default=0)
    return p


def rank_wallets(
    db_path: str,
    *,
    min_markets: int,
    min_roi: float,
    min_pnl: float,
    max_avg_price: float,
) -> set[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        res = {
            m["slug"]: m["winning_outcome"]
            for m in con.execute(
                "SELECT slug, winning_outcome FROM markets "
                "WHERE resolved=1 AND winning_outcome IN ('UP','DOWN')"
            )
        }
        pos: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        px: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        for t in con.execute(
            "SELECT account, market_slug, side, outcome, price, shares, collateral "
            "FROM trades WHERE outcome IN ('UP','DOWN') AND account != ''"
        ):
            if t["market_slug"] not in res:
                continue
            k = (t["account"], t["market_slug"], t["outcome"])
            sign = 1.0 if t["side"] == 0 else -1.0
            pos[k][0] += sign * t["shares"]
            pos[k][1] += sign * t["collateral"]
            px[t["account"]][0] += t["price"]
            px[t["account"]][1] += 1
    finally:
        con.close()

    pnl: dict[str, float] = defaultdict(float)
    staked: dict[str, float] = defaultdict(float)
    mkts: dict[str, set[str]] = defaultdict(set)
    for (acct, slug, outcome), (sh, cost) in pos.items():
        pnl[acct] += (sh if res[slug] == outcome else 0.0) - cost
        staked[acct] += max(cost, 0.0)
        mkts[acct].add(slug)

    sharks = set()
    for acct, p in pnl.items():
        st = staked[acct]
        n_px = px[acct][1]
        if (
            len(mkts[acct]) >= min_markets
            and st > 0
            and p >= min_pnl
            and p / st >= min_roi
            and n_px > 0
            and px[acct][0] / n_px <= max_avg_price
        ):
            sharks.add(acct)
    return sharks


def _load_copied(path: Path) -> set[tuple[str, str]]:
    copied: set[tuple[str, str]] = set()
    if not path.exists():
        return copied
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-2000:]
    except Exception:
        return copied
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "trade":
            c = r.get("candidate") or {}
            if c.get("slug") and c.get("side"):
                copied.add((c["slug"], c["side"]))
    return copied


def _log(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def main() -> None:
    args = build_parser().parse_args()
    out_path = Path(args.jsonl_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    client = LimitlessClient()

    copied = _load_copied(out_path)
    sharks: set[str] = set()
    ranked_at = 0.0
    last_seen_ms = int(time.time() * 1000)

    running = True

    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _log(out_path, {"event": "startup", "mode": "dry_run", "ts_ms": last_seen_ms})

    iteration = 0
    while running:
        iteration += 1
        now_ms = int(time.time() * 1000)

        if time.time() - ranked_at >= args.rank_seconds:
            try:
                sharks = rank_wallets(
                    args.flow_db,
                    min_markets=args.min_markets,
                    min_roi=args.min_roi,
                    min_pnl=args.min_pnl,
                    max_avg_price=args.max_avg_price,
                )
                ranked_at = time.time()
                _log(out_path, {"event": "leaderboard", "sharks": len(sharks), "ts_ms": now_ms})
            except Exception as exc:
                _log(out_path, {"event": "rank_error", "error": str(exc), "ts_ms": now_ms})

        try:
            con = sqlite3.connect(f"file:{args.flow_db}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                fresh = con.execute(
                    "SELECT t.account, t.market_slug, t.outcome, t.price, t.created_at_ms,"
                    "       m.symbol, m.interval, m.expiration_ms "
                    "FROM trades t JOIN markets m ON m.slug = t.market_slug "
                    "WHERE t.created_at_ms > ? AND t.side = 0 "
                    "AND t.outcome IN ('UP','DOWN') AND t.account != '' "
                    "ORDER BY t.created_at_ms",
                    (last_seen_ms,),
                ).fetchall()
            finally:
                con.close()
        except Exception as exc:
            _log(out_path, {"event": "poll_error", "error": str(exc), "ts_ms": now_ms})
            time.sleep(max(args.loop_seconds, 1))
            continue

        for t in fresh:
            last_seen_ms = max(last_seen_ms, t["created_at_ms"])
            if t["account"] not in sharks:
                continue
            key = (t["market_slug"], t["outcome"])
            if key in copied:
                continue
            secs_left = (t["expiration_ms"] - now_ms) / 1000
            if secs_left < args.min_seconds_to_expiry:
                _log(out_path, {"event": "copy_skip", "reason": "too_late",
                                "slug": t["market_slug"], "ts_ms": now_ms})
                continue
            try:
                book = client.orderbook(t["market_slug"])
            except Exception as exc:
                _log(out_path, {"event": "copy_skip", "reason": "book_error",
                                "slug": t["market_slug"], "error": str(exc), "ts_ms": now_ms})
                continue
            ask = book.up_ask if t["outcome"] == "UP" else book.down_ask
            if not ask or not (args.min_price <= ask <= args.max_price):
                _log(out_path, {"event": "copy_skip", "reason": "price_out_of_band",
                                "slug": t["market_slug"], "ask": ask, "ts_ms": now_ms})
                continue
            if ask > t["price"] + args.max_chase:
                _log(out_path, {"event": "copy_skip", "reason": "chased_too_far",
                                "slug": t["market_slug"], "shark_px": t["price"],
                                "ask": ask, "ts_ms": now_ms})
                continue
            copied.add(key)
            _log(out_path, {
                "event": "trade",
                "mode": "dry_run",
                "state": "hedged",
                "strategy": "copy_shadow",
                "candidate": {
                    "slug": t["market_slug"],
                    "symbol": t["symbol"],
                    "interval": t["interval"],
                    "side": t["outcome"],
                    "limit_price": ask,
                    "stake_usdc": args.stake_usdc,
                    "seconds_to_expiry": int(secs_left),
                    "reason": (
                        f"copy {t['account'][:10]} {t['outcome']} "
                        f"shark_px={t['price']:.3f} our_ask={ask:.3f}"
                    ),
                    "shark": t["account"],
                    "shark_price": t["price"],
                },
                "limitless_result": {"matched": True, "filled_usdc": args.stake_usdc,
                                     "raw": {"mode": "preview"}},
                "hedge_result": None,
                "ts_ms": now_ms,
            })

        if args.iterations and iteration >= args.iterations:
            break
        if running:
            time.sleep(max(args.loop_seconds, 1))

    _log(out_path, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})


if __name__ == "__main__":
    main()
