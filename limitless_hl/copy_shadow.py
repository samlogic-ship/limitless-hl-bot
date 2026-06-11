"""
limitless_hl/copy_shadow.py — paper-trade copies of profitable Limitless wallets.

The flow recorder attributes ~half of all taker prints to real wallets and
tracks market resolutions. This daemon ranks those wallets by realized PnL
(net-position accounting) and, when a currently-profitable wallet opens a new
position, records a dry-run copy at OUR currently fillable ask price (not
their fill). The learner ingests the jsonl and scores the lane as
strategy=copy_shadow. No orders are ever sent; mode is always dry_run.

Detection has two paths:
- FAST: 5m/15m/1h markets get their /events feed polled directly every loop
  (~8s detection latency; there is no public websocket, but the endpoint
  answers in ~30ms and tolerates this rate).
- SLOW: everything else (dailies/weeklies, where minutes of latency are
  irrelevant) comes from flow.sqlite3, which the recorder refreshes every 60s.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import os
import requests

from .clients import LimitlessClient
from .polymarket_feed import PolymarketFeed
from .live_trade import (
    LimitlessCredentials,
    LimitlessOrderBuilder,
    LimitlessSubmitter,
    candidate_to_limitless_intent,
)
from .secrets import get_secret

BASE_URL = "https://api.limitless.exchange"
FAST_INTERVALS = {"5m", "15m", "1h"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Shadow-copy profitable Limitless wallets")
    p.add_argument("--flow-db", default="tmp/limitless_hl/flow.sqlite3")
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/copy_shadow.jsonl")
    p.add_argument("--loop-seconds", type=int, default=8)
    p.add_argument("--rank-seconds", type=int, default=900)
    p.add_argument("--markets-refresh-seconds", type=int, default=60)
    p.add_argument("--min-markets", type=int, default=10)
    p.add_argument("--min-roi", type=float, default=0.05)
    p.add_argument("--min-pnl", type=float, default=20.0)
    p.add_argument("--max-avg-price", type=float, default=0.85)
    p.add_argument("--fade-max-pnl", type=float, default=-50.0,
                   help="Wallets at or below this realized PnL are fade candidates")
    p.add_argument("--fade-max-roi", type=float, default=-0.30)
    p.add_argument("--max-sharks", type=int, default=12,
                   help="Cap the copy leaderboard to the top N wallets by PnL; "
                        "the 2026-06-10 expansion to 38 sharks diluted edge")
    p.add_argument("--min-seconds-to-expiry", type=int, default=120)
    p.add_argument("--max-chase", type=float, default=0.06)
    p.add_argument("--smart-chase", action="store_true", default=True,
                   help="When price ran past the shark, still copy if the "
                        "Polymarket twin says our new price is below fair")
    p.add_argument("--smart-chase-margin", type=float, default=0.03,
                   help="Required PM-fair minus our fee-adjusted ask to chase")
    p.add_argument("--smart-chase-max", type=float, default=0.15,
                   help="Never chase further than this beyond the shark price")
    p.add_argument("--min-price", type=float, default=0.05)
    p.add_argument("--max-price", type=float, default=0.92)
    p.add_argument("--stake-usdc", type=float, default=1.0)
    p.add_argument("--iterations", type=int, default=0)
    # Live execution: only fires when BOTH --live-allowed is set AND the
    # gatekeeper has written the per-strategy arm flag (gate passed). The
    # flag is the automatic switch; this arg is the standing authorization.
    p.add_argument("--live-allowed", action="store_true")
    p.add_argument("--live-stake-usdc", type=float, default=1.0)
    p.add_argument("--live-min-shark-stake", type=float, default=50.0,
                   help="Live-copy only conviction bets: shark stake >= this. "
                        "Net-of-fee data 2026-06-10: >=$50 copies +0.21/trade, "
                        "<$50 copies negative.")
    p.add_argument("--live-max-per-day", type=int, default=30)
    p.add_argument("--explore-max-per-day", type=int, default=8,
                   help="Daily live cap while the lane is in the explore tier")
    p.add_argument("--live-daily-loss-stop", type=float, default=3.0)
    p.add_argument("--learner-db", default="tmp/limitless_hl/learner.sqlite3")
    p.add_argument("--live-jsonl-out", default="tmp/limitless_hl/copy_live.jsonl")
    p.add_argument("--arm-flag-dir", default="tmp/limitless_hl")
    return p


def rank_wallets(
    db_path: str,
    *,
    min_markets: int,
    min_roi: float,
    min_pnl: float,
    max_avg_price: float,
    fade_max_pnl: float = -50.0,
    fade_max_roi: float = -0.30,
    max_sharks: int = 12,
) -> tuple[set[str], set[str]]:
    """Returns (sharks, fish): wallets to copy and wallets to bet against."""
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

    shark_rank: list[tuple[float, str]] = []
    fish: set[str] = set()
    for acct, p in pnl.items():
        st = staked[acct]
        n_px = px[acct][1]
        if len(mkts[acct]) < min_markets or st <= 0 or n_px <= 0:
            continue
        avg_px = px[acct][0] / n_px
        if p >= min_pnl and p / st >= min_roi and avg_px <= max_avg_price:
            shark_rank.append((p, acct))
        elif p <= fade_max_pnl and p / st <= fade_max_roi and avg_px <= max_avg_price:
            fish.add(acct)
    shark_rank.sort(reverse=True)
    sharks = {acct for _, acct in shark_rank[:max_sharks]}
    return sharks, fish


def _parse_iso_ms(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _load_copied(path: Path) -> set[tuple[str, str, str]]:
    # Full scan: a tail slice forgot old entries after restart and allowed a
    # second real order into the same market+side (audit 2026-06-11).
    copied: set[tuple[str, str, str]] = set()
    if not path.exists():
        return copied
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
                copied.add((c["slug"], c["side"], r.get("strategy") or "copy_shadow"))
    return copied


def _log(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _live_strategy(strategy: str) -> str:
    return strategy.replace("_shadow", "_live")


class LiveExecutor:
    """Submits real Limitless FAK buys for gate-passed lanes. Hard caps:
    stake clamped to <= $2, per-day trade cap, daily-loss stop from the
    learner DB. Lazily builds the submitter; any setup failure disables it."""

    def __init__(self, args: argparse.Namespace, out_path: Path):
        self.args = args
        self.out_path = Path(args.live_jsonl_out)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = LimitlessClient()
        self.submitter: LimitlessSubmitter | None = None
        self.disabled_reason: str | None = None
        self.disabled_at: float = 0.0
        self.day_start = int(time.time() // 86400)
        self.sent_today = self._count_sent_today()

    def _count_sent_today(self) -> int:
        if not self.out_path.exists():
            return 0
        day_start = int(time.time() // 86400 * 86400 * 1000)
        n = 0
        try:
            for line in self.out_path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("event") == "live_trade" and r.get("ts_ms", 0) >= day_start:
                    n += 1
        except OSError:
            pass
        return n

    def _ensure_submitter(self) -> bool:
        if self.submitter is not None:
            return True
        # A setup failure (missing env, transient error) retries after 10min
        # instead of silently disabling live trading forever.
        if self.disabled_reason and time.time() - self.disabled_at < 600:
            return False
        self.disabled_reason = None
        try:
            token_id = get_secret("LIMITLESS_TOKEN_ID")
            token_secret = get_secret("LIMITLESS_TOKEN_SECRET")
            private_key = get_secret("LIMITLESS_PRIVATE_KEY")
            owner_id = os.environ.get("LIMITLESS_OWNER_ID")
            maker_address = os.environ.get("LIMITLESS_MAKER_ADDRESS")
            if not all([token_id, token_secret, private_key, owner_id, maker_address]):
                raise RuntimeError("missing credentials")
            smart_wallet = os.environ.get("LIMITLESS_SMART_WALLET") or maker_address or ""
            sig_type = int(os.environ.get("LIMITLESS_SIGNATURE_TYPE", "0"))
            self.submitter = LimitlessSubmitter(
                credentials=LimitlessCredentials(token_id or "", token_secret or ""),
                builder=LimitlessOrderBuilder(
                    maker=smart_wallet,
                    owner_id=int(owner_id or "0"),
                    fee_rate_bps=int(os.environ.get("LIMITLESS_FEE_RATE_BPS", "300")),
                    signature_type=sig_type,
                    signer=(maker_address if sig_type == 1 else None),
                ),
                private_key=private_key or "",
            )
            return True
        except Exception as exc:
            self.disabled_reason = str(exc)
            self.disabled_at = time.time()
            _log(self.out_path, {"event": "live_disabled", "error": str(exc),
                                 "ts_ms": int(time.time() * 1000)})
            return False

    def _daily_loss_hit(self, strategies: tuple[str, ...]) -> bool:
        db = Path(self.args.learner_db)
        if not db.exists():
            return False
        day_start = int(time.time() // 86400 * 86400 * 1000)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute(
                    "SELECT COALESCE(SUM(r.pnl_usdc), 0) FROM trades t "
                    "JOIN resolutions r ON t.trade_key = r.trade_key "
                    f"WHERE t.strategy IN ({','.join('?' * len(strategies))}) "
                    "AND r.resolved_at_ms >= ?",
                    (*strategies, day_start),
                ).fetchone()
            finally:
                con.close()
        except Exception:
            return False
        return float(row[0] or 0.0) <= -abs(self.args.live_daily_loss_stop)

    def maybe_execute(self, strategy: str, candidate: dict[str, Any], now_ms: int) -> None:
        a = self.args
        flag = Path(a.arm_flag_dir) / f"gate_{_live_strategy(strategy)}.flag"
        if not a.live_allowed or not flag.exists():
            return
        tier = "full"
        try:
            tier = json.loads(flag.read_text()).get("tier", "full")
        except (json.JSONDecodeError, OSError):
            pass
        day_cap = a.live_max_per_day if tier == "full" else a.explore_max_per_day
        cur_day = int(time.time() // 86400)
        if cur_day != self.day_start:
            self.day_start = cur_day
            self.sent_today = self._count_sent_today()
        if self.sent_today >= day_cap:
            _log(self.out_path, {"event": "live_skip", "reason": "max_per_day",
                                 "ts_ms": now_ms})
            return
        live_strats = ("copy_live", "fade_live")
        if self._daily_loss_hit(live_strats):
            _log(self.out_path, {"event": "live_skip", "reason": "daily_loss_stop",
                                 "ts_ms": now_ms})
            return
        if not self._ensure_submitter():
            return
        stake = min(a.live_stake_usdc, 2.0)
        live_cand = dict(candidate)
        live_cand["stake_usdc"] = stake
        try:
            details = self.client.market_details(candidate["slug"])
            client_order_id = (
                f"limitless-copy-{candidate['slug']}-{candidate['side']}-{now_ms}"
            )
            intent = candidate_to_limitless_intent(
                live_cand, details, client_order_id=client_order_id
            )
            assert self.submitter is not None
            if not flag.exists():  # gate may have been revoked while we built the intent
                _log(self.out_path, {"event": "live_skip", "reason": "gate_revoked",
                                     "ts_ms": now_ms})
                return
            result = self.submitter.submit_intent(intent)
            if not result.get("matched"):
                # FAK missed the book: no position, no daily slot burned.
                _log(self.out_path, {"event": "live_unmatched", "slug": candidate.get("slug"),
                                     "limitless_result": result, "ts_ms": now_ms})
                return
            self.sent_today += 1
            _log(self.out_path, {
                "event": "live_trade",
                "mode": "live",
                "state": "limitless_filled_unhedged",
                "strategy": _live_strategy(strategy),
                "candidate": live_cand,
                "limitless_result": result,
                "hedge_result": None,
                "ts_ms": now_ms,
            })
        except Exception as exc:
            _log(self.out_path, {"event": "live_error", "slug": candidate.get("slug"),
                                 "error": str(exc)[:300], "ts_ms": now_ms})


class CopyShadow:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out_path = Path(args.jsonl_out)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = LimitlessClient()
        self.polymarket = PolymarketFeed()
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.copied = _load_copied(self.out_path)
        self.executor = LiveExecutor(args, self.out_path)
        self.sharks: set[str] = set()
        self.fish: set[str] = set()
        self.fast_markets: list[dict[str, Any]] = []
        self.watermark: dict[str, int] = {}
        # Seed the slow cursor from the flow DB so a restart does not drop
        # shark signals recorded during the downtime window.
        self.slow_seen_ms = int(time.time() * 1000)
        try:
            con = sqlite3.connect(f"file:{args.flow_db}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("SELECT MAX(created_at_ms) FROM trades").fetchone()
            finally:
                con.close()
            if row and row[0]:
                self.slow_seen_ms = min(self.slow_seen_ms, int(row[0]))
        except Exception:
            pass

    def refresh_markets(self, now_ms: int) -> None:
        con = sqlite3.connect(f"file:{self.args.flow_db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT slug, symbol, interval, expiration_ms, token_up, token_down "
                "FROM markets WHERE resolved=0 AND expiration_ms > ?",
                (now_ms + self.args.min_seconds_to_expiry * 1000,),
            ).fetchall()
        finally:
            con.close()
        self.fast_markets = [dict(r) for r in rows if r["interval"] in FAST_INTERVALS]
        for m in self.fast_markets:
            self.watermark.setdefault(m["slug"], now_ms)

    def poll_fast(self, now_ms: int) -> list[dict[str, Any]]:
        """Poll /events directly for short markets; return fresh shark buys."""
        hits: list[dict[str, Any]] = []
        for m in self.fast_markets:
            if m["expiration_ms"] - now_ms < self.args.min_seconds_to_expiry * 1000:
                continue
            try:
                resp = self.session.get(
                    f"{BASE_URL}/markets/{m['slug']}/events",
                    params={"page": 1, "limit": 25},
                    timeout=6,
                )
                if resp.status_code != 200:
                    continue
                events = resp.json().get("events") or []
            except Exception:
                continue
            wm = self.watermark.get(m["slug"], now_ms)
            newest = wm
            for row in events:
                created_ms = _parse_iso_ms(row.get("createdAt"))
                newest = max(newest, created_ms)
                if created_ms <= wm:
                    continue
                if int(row.get("side") or 0) != 0:  # buys only
                    continue
                acct = str((row.get("profile") or {}).get("account") or "").lower()
                if acct not in self.sharks and acct not in self.fish:
                    continue
                token_id = str(row.get("tokenId") or "")
                outcome = (
                    "UP" if token_id == m["token_up"]
                    else "DOWN" if token_id == m["token_down"]
                    else None
                )
                if not outcome:
                    continue
                try:
                    price = float(row.get("price") or 0)
                    stake = float(row.get("matchedSize") or 0) / 1_000_000 * price
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                hits.append({
                    "account": acct, "market_slug": m["slug"], "outcome": outcome,
                    "price": price, "stake": stake, "created_at_ms": created_ms,
                    "symbol": m["symbol"], "interval": m["interval"],
                    "expiration_ms": m["expiration_ms"], "via": "fast",
                })
            self.watermark[m["slug"]] = newest
            time.sleep(0.05)
        return hits

    def poll_slow(self, now_ms: int) -> list[dict[str, Any]]:
        """flow.sqlite3 backstop for long intervals (recorder refreshes ~60s)."""
        try:
            con = sqlite3.connect(f"file:{self.args.flow_db}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT t.account, t.market_slug, t.outcome, t.price, t.collateral, t.created_at_ms,"
                    "       m.symbol, m.interval, m.expiration_ms "
                    "FROM trades t JOIN markets m ON m.slug = t.market_slug "
                    "WHERE t.created_at_ms > ? AND t.side = 0 "
                    "AND t.outcome IN ('UP','DOWN') AND t.account != ''",
                    (self.slow_seen_ms,),
                ).fetchall()
            finally:
                con.close()
        except Exception as exc:
            _log(self.out_path, {"event": "poll_error", "error": str(exc), "ts_ms": now_ms})
            return []
        hits = []
        for t in rows:
            self.slow_seen_ms = max(self.slow_seen_ms, t["created_at_ms"])
            if t["interval"] in FAST_INTERVALS:  # fast path owns these
                continue
            if t["account"] in self.sharks or t["account"] in self.fish:
                hits.append({**dict(t), "stake": t["collateral"], "via": "slow"})
        return hits

    def try_copy(self, hit: dict[str, Any], now_ms: int) -> None:
        a = self.args
        is_fade = hit["account"] in self.fish
        strategy = "fade_shadow" if is_fade else "copy_shadow"
        our_side = (
            ("DOWN" if hit["outcome"] == "UP" else "UP") if is_fade else hit["outcome"]
        )
        key = (hit["market_slug"], our_side, strategy)
        if key in self.copied:
            return
        secs_left = (hit["expiration_ms"] - now_ms) / 1000
        if secs_left < a.min_seconds_to_expiry:
            _log(self.out_path, {"event": "copy_skip", "reason": "too_late",
                                 "slug": hit["market_slug"], "ts_ms": now_ms})
            return
        try:
            book = self.client.orderbook(hit["market_slug"])
        except Exception as exc:
            _log(self.out_path, {"event": "copy_skip", "reason": "book_error",
                                 "slug": hit["market_slug"], "error": str(exc), "ts_ms": now_ms})
            return
        ask = book.up_ask if our_side == "UP" else book.down_ask
        # reference for the chase guard: the signal price on OUR side
        ref_px = (1 - hit["price"]) if is_fade else hit["price"]
        if not ask or not (a.min_price <= ask <= a.max_price):
            _log(self.out_path, {"event": "copy_skip", "reason": "price_out_of_band",
                                 "slug": hit["market_slug"], "ask": ask, "ts_ms": now_ms})
            return
        chased = ask > ref_px + a.max_chase
        chase_ok = False
        pm_fair = None
        if chased and a.smart_chase and ask <= ref_px + a.smart_chase_max:
            # Consult the Polymarket twin: is our higher price STILL below fair?
            pm = self.polymarket.implied_up_prob(
                hit["symbol"], hit["interval"], hit["expiration_ms"])
            if pm is not None:
                up_prob = float(pm.get("up_prob") or 0.0)
                pm_fair = up_prob if our_side == "UP" else 1.0 - up_prob
                # fee-adjusted: we redeem shares net of the 3% taker fee
                net_edge = pm_fair - ask * 1.03
                chase_ok = net_edge >= a.smart_chase_margin
        if chased and not chase_ok:
            _log(self.out_path, {"event": "copy_skip", "reason": "chased_too_far",
                                 "slug": hit["market_slug"], "shark_px": hit["price"],
                                 "ref_px": ref_px, "ask": ask,
                                 "pm_fair": pm_fair, "ts_ms": now_ms})
            return
        if chased and chase_ok:
            _log(self.out_path, {"event": "smart_chase", "slug": hit["market_slug"],
                                 "shark_px": hit["price"], "ask": ask,
                                 "pm_fair": pm_fair, "ts_ms": now_ms})
        self.copied.add(key)
        detect_lag_s = max(0.0, (now_ms - hit["created_at_ms"]) / 1000)
        _log(self.out_path, {
            "event": "trade",
            "mode": "dry_run",
            "state": "hedged",
            "strategy": strategy,
            "candidate": {
                "slug": hit["market_slug"],
                "symbol": hit["symbol"],
                "interval": hit["interval"],
                "side": our_side,
                "limit_price": ask,
                "stake_usdc": a.stake_usdc,
                "seconds_to_expiry": int(secs_left),
                "reason": (
                    f"{strategy.split('_')[0]} {hit['account'][:10]} signal={hit['outcome']} "
                    f"via={hit['via']} lag={detect_lag_s:.1f}s "
                    f"signal_px={hit['price']:.3f} our_ask={ask:.3f}"
                ),
                "shark": hit["account"],
                "shark_price": hit["price"],
                "shark_stake_usdc": round(float(hit.get("stake") or 0.0), 2),
                "detect_lag_s": round(detect_lag_s, 1),
                "via": hit["via"],
            },
            "limitless_result": {"matched": True, "filled_usdc": a.stake_usdc,
                                 "raw": {"mode": "preview"}},
            "hedge_result": None,
            "ts_ms": now_ms,
        })
        if float(hit.get("stake") or 0.0) >= a.live_min_shark_stake:
            self.executor.maybe_execute(strategy, {
                "slug": hit["market_slug"], "symbol": hit["symbol"],
                "interval": hit["interval"], "side": our_side, "limit_price": ask,
                "stake_usdc": a.stake_usdc, "seconds_to_expiry": int(secs_left),
            }, now_ms)


def main() -> None:
    args = build_parser().parse_args()
    cs = CopyShadow(args)
    ranked_at = 0.0
    markets_at = 0.0

    running = True

    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _log(cs.out_path, {"event": "startup", "mode": "dry_run", "ts_ms": int(time.time() * 1000)})

    iteration = 0
    while running:
        iteration += 1
        now_ms = int(time.time() * 1000)

        if time.time() - ranked_at >= args.rank_seconds:
            try:
                cs.sharks, cs.fish = rank_wallets(
                    args.flow_db,
                    min_markets=args.min_markets,
                    min_roi=args.min_roi,
                    min_pnl=args.min_pnl,
                    max_avg_price=args.max_avg_price,
                    fade_max_pnl=args.fade_max_pnl,
                    fade_max_roi=args.fade_max_roi,
                    max_sharks=args.max_sharks,
                )
                ranked_at = time.time()
                _log(cs.out_path, {"event": "leaderboard", "sharks": len(cs.sharks),
                                   "fish": len(cs.fish), "ts_ms": now_ms})
            except Exception as exc:
                _log(cs.out_path, {"event": "rank_error", "error": str(exc), "ts_ms": now_ms})

        if time.time() - markets_at >= args.markets_refresh_seconds:
            try:
                cs.refresh_markets(now_ms)
                markets_at = time.time()
            except Exception as exc:
                _log(cs.out_path, {"event": "markets_error", "error": str(exc), "ts_ms": now_ms})

        for hit in cs.poll_fast(now_ms) + cs.poll_slow(now_ms):
            cs.try_copy(hit, int(time.time() * 1000))

        if args.iterations and iteration >= args.iterations:
            break
        if running:
            time.sleep(max(args.loop_seconds, 1))

    _log(cs.out_path, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})


if __name__ == "__main__":
    main()
