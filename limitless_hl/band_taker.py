"""
limitless_hl/band_taker.py — mid-favorite late-entry band strategy (shadow first).

Evidence (2026-06-13 backtest, net of 3% taker fee, OUR realistic asks):
  - Buying the side priced in a mid-favorite band is NEGATIVE at early entry
    (the market-wide +EV at 0.6-0.7 is a late-fill mirage we cannot capture
    when we enter with minutes to spare).
  - BUT entering the band [0.58,0.74] with 120-300s to expiry showed +0.126/
    trade (n=27, promising-not-proven). This lane isolates that window at scale
    so the gatekeeper can prove or kill it with a real confidence interval.

Records a dry-run "trade" on the band side at our fillable ask. Learner scores
it as strategy=band_shadow (live variant band_live, flag-gated like copy).
No orders are sent unless --live-allowed AND the gate flag exists.
"""
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

from .clients import LimitlessClient

FAST_INTERVALS = {"5m", "15m", "1h"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Mid-favorite late-entry band taker")
    p.add_argument("--flow-db", default="tmp/limitless_hl/flow.sqlite3")
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/band_shadow.jsonl")
    p.add_argument("--loop-seconds", type=int, default=10)
    p.add_argument("--band-lo", type=float, default=0.58)
    p.add_argument("--band-hi", type=float, default=0.74)
    p.add_argument("--ste-lo", type=int, default=120, help="min seconds to expiry")
    p.add_argument("--ste-hi", type=int, default=300, help="max seconds to expiry")
    p.add_argument("--stake-usdc", type=float, default=1.0)
    p.add_argument("--intervals", default="5m,15m")
    p.add_argument("--iterations", type=int, default=0)
    return p


def _log(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _load_done(path: Path) -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    if not path.exists():
        return done
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") == "trade":
                c = r.get("candidate") or {}
                if c.get("slug") and c.get("side"):
                    done.add((c["slug"], c["side"]))
    except OSError:
        pass
    return done


def main() -> None:
    args = build_parser().parse_args()
    out = Path(args.jsonl_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    client = LimitlessClient()
    done = _load_done(out)
    intervals = {s.strip() for s in args.intervals.split(",") if s.strip()}

    running = True

    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _log(out, {"event": "startup", "mode": "dry_run", "ts_ms": int(time.time() * 1000)})

    it = 0
    while running:
        it += 1
        now_ms = int(time.time() * 1000)
        try:
            con = sqlite3.connect(f"file:{args.flow_db}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                markets = con.execute(
                    "SELECT slug, symbol, interval, expiration_ms FROM markets "
                    "WHERE resolved=0 AND expiration_ms > ? AND expiration_ms < ?",
                    (now_ms + args.ste_lo * 1000, now_ms + args.ste_hi * 1000),
                ).fetchall()
            finally:
                con.close()
        except Exception as exc:
            _log(out, {"event": "scan_error", "error": str(exc)[:200], "ts_ms": now_ms})
            time.sleep(max(args.loop_seconds, 1))
            continue

        for m in markets:
            if m["interval"] not in intervals:
                continue
            ste = (m["expiration_ms"] - now_ms) / 1000
            if not (args.ste_lo <= ste <= args.ste_hi):
                continue
            try:
                book = client.orderbook(m["slug"])
            except Exception:
                continue
            for side, ask in (("UP", book.up_ask), ("DOWN", book.down_ask)):
                if not ask or not (args.band_lo <= ask <= args.band_hi):
                    continue
                key = (m["slug"], side)
                if key in done:
                    continue
                done.add(key)
                _log(out, {
                    "event": "trade", "mode": "dry_run", "state": "hedged",
                    "strategy": "band_shadow",
                    "candidate": {
                        "slug": m["slug"], "symbol": m["symbol"], "interval": m["interval"],
                        "side": side, "limit_price": ask, "stake_usdc": args.stake_usdc,
                        "seconds_to_expiry": int(ste),
                        "reason": f"band {args.band_lo}-{args.band_hi} late-entry ste={int(ste)}s ask={ask:.3f}",
                    },
                    "limitless_result": {"matched": True, "filled_usdc": args.stake_usdc,
                                         "raw": {"mode": "preview"}},
                    "hedge_result": None, "ts_ms": now_ms,
                })
            time.sleep(0.05)

        if args.iterations and it >= args.iterations:
            break
        if running:
            time.sleep(max(args.loop_seconds, 1))
    _log(out, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})


if __name__ == "__main__":
    main()
