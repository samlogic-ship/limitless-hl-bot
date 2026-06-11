"""
limitless_hl/gatekeeper.py — automatic gate opening, divergence pause, kill switch.

Sam's standing instruction (2026-06-10): "if the report passes, the gates
should open automatically." This loop evaluates each lane's go-live gate from
the learner DB every --loop-seconds and:

- OPENS a gate by writing tmp/limitless_hl/gate_<lane>_live.flag (the copy/fade
  executor only trades while its flag exists) and announcing on Telegram.
- For the own-model lane it starts the PM2 process `limitless-hl-live`.
- PAUSES a live lane (removes flag / pm2 stop) when live results diverge from
  the paper twin by more than --divergence-usdc per trade over the last
  --divergence-window resolved trades.
- KILL SWITCH: combined live PnL today <= -$--kill-daily-loss closes every
  gate and stops the live daemon.

Gates (all require resolved n >= --min-n):
  copy:  strategy copy_shadow  — total PnL > 0 AND PnL/trade >= +0.05
  fade:  strategy fade_shadow  — same
  model: shadow_daemon slice (15m, price 0.25-0.88, score>=2, edge 0.03-0.12)
         — win rate >= 0.52 AND total PnL > 0
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import requests


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Automatic go-live gatekeeper")
    p.add_argument("--learner-db", default="tmp/limitless_hl/learner.sqlite3")
    p.add_argument("--flag-dir", default="tmp/limitless_hl")
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/gatekeeper.jsonl")
    p.add_argument("--loop-seconds", type=int, default=600)
    p.add_argument("--min-n", type=int, default=100)
    p.add_argument("--explore-min-n", type=int, default=20,
                   help="Exploration tier: best lane trades small even before "
                        "full significance; a silent engine learns nothing")
    p.add_argument("--explore-min-per-trade", type=float, default=0.05)
    p.add_argument("--conviction-since-floor", type=int, default=1781134560000,
                   help="The $50 conviction threshold was chosen at this "
                        "timestamp (2026-06-10 23:36Z); gates judge it only on "
                        "data collected AFTER selection, never on the window "
                        "that suggested it (multiple-comparisons guard)")
    p.add_argument("--since-ms", type=int, default=1781085600000)  # 2026-06-10 10:00Z restart
    p.add_argument("--divergence-window", type=int, default=30)
    p.add_argument("--divergence-usdc", type=float, default=0.15)
    p.add_argument("--kill-daily-loss", type=float, default=5.0)
    p.add_argument("--manage-model-pm2", action="store_true",
                   help="Also start/stop the limitless-hl-live PM2 process on its gate")
    p.add_argument("--iterations", type=int, default=0)
    return p


def _q(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return con.execute(sql, params).fetchall()


def _finalize_stats(pnls: list[float], wins: int, total_entered: int) -> dict[str, float]:
    """Mean, win rate, and a 2-sigma significance bound plus resolution coverage.
    The audit (2026-06-11) showed +0.05/trade at n=100 is ~0.5 SE of noise and
    that resolved-only INNER JOINs hide a pending tail (survivorship)."""
    n = len(pnls)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "per_trade": 0.0,
                "se": 0.0, "lower_bound": 0.0, "coverage": 0.0}
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / max(n - 1, 1)
    se = (var ** 0.5) / (n ** 0.5)
    return {
        "n": n, "wr": wins / n, "pnl": sum(pnls), "per_trade": mean,
        "se": se, "lower_bound": mean - 2 * se,
        "coverage": n / total_entered if total_entered else 0.0,
    }


def lane_stats(con: sqlite3.Connection, strategy: str, since_ms: int) -> dict[str, float]:
    rows = _q(con,
        "SELECT r.pnl_usdc, r.won FROM trades t "
        "JOIN resolutions r ON t.trade_key=r.trade_key "
        "WHERE t.strategy=? AND t.ts_ms > ?", (strategy, since_ms))
    total = _q(con, "SELECT COUNT(*) c FROM trades WHERE strategy=? AND ts_ms > ?",
               (strategy, since_ms))[0]["c"]
    return _finalize_stats([r["pnl_usdc"] for r in rows],
                           sum(r["won"] for r in rows), int(total))


def model_slice_stats(con: sqlite3.Connection, since_ms: int) -> dict[str, float]:
    rows = _q(con,
        "SELECT t.raw_json, t.interval, t.price, r.won, r.pnl_usdc "
        "FROM trades t JOIN resolutions r ON t.trade_key=r.trade_key "
        "WHERE t.strategy='shadow_daemon' AND t.ts_ms > ?", (since_ms,))
    pnls: list[float] = []
    wins = 0
    for r in rows:
        try:
            c = (json.loads(r["raw_json"]).get("candidate")) or {}
        except (json.JSONDecodeError, TypeError):
            continue
        s, e = c.get("score"), c.get("edge")
        if (
            r["interval"] == "15m" and 0.25 <= r["price"] <= 0.88
            and s is not None and s >= 2.0 and e is not None and 0.03 <= e <= 0.12
        ):
            pnls.append(r["pnl_usdc"])
            wins += r["won"]
    total = _q(con, "SELECT COUNT(*) c FROM trades WHERE strategy='shadow_daemon' AND ts_ms > ?",
               (since_ms,))[0]["c"]
    return _finalize_stats(pnls, wins, int(total))


def recent_per_trade(con: sqlite3.Connection, strategy: str, window: int) -> tuple[int, float]:
    rows = _q(con,
        "SELECT r.pnl_usdc FROM trades t JOIN resolutions r ON t.trade_key=r.trade_key "
        "WHERE t.strategy=? ORDER BY r.resolved_at_ms DESC LIMIT ?", (strategy, window))
    if not rows:
        return 0, 0.0
    return len(rows), sum(r["pnl_usdc"] for r in rows) / len(rows)


def live_pnl_today(con: sqlite3.Connection) -> float:
    day_start = int(time.time() // 86400 * 86400 * 1000)
    row = _q(con,
        "SELECT COALESCE(SUM(r.pnl_usdc),0) FROM trades t "
        "JOIN resolutions r ON t.trade_key=r.trade_key "
        "WHERE t.strategy IN ('copy_live','fade_live','scored_daemon') "
        "AND t.ts_ms >= ?", (day_start,))[0]
    return float(row[0] or 0.0)


def tg_send(text: str) -> None:
    token = os.environ.get("LIMITLESS_HL_TG_TOKEN")
    chat = os.environ.get("LIMITLESS_HL_TG_CHAT")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text}, timeout=8,
        )
    except Exception:
        pass


def _log(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def conviction_stats(
    con: sqlite3.Connection, strategy: str, since_ms: int, min_stake: float = 50.0
) -> dict[str, float]:
    """Stats for the conviction subset (shark stake >= min_stake) — the trades
    the live executor would actually take."""
    rows = _q(con,
        "SELECT t.raw_json, r.won, r.pnl_usdc "
        "FROM trades t JOIN resolutions r ON t.trade_key=r.trade_key "
        "WHERE t.strategy=? AND t.ts_ms > ?", (strategy, since_ms))
    pnls: list[float] = []
    wins = 0
    for r in rows:
        try:
            cand = (json.loads(r["raw_json"]).get("candidate")) or {}
        except (json.JSONDecodeError, TypeError):
            continue
        stake = cand.get("shark_stake_usdc")
        if stake is None or float(stake) < min_stake:
            continue
        pnls.append(r["pnl_usdc"])
        wins += r["won"]
    total = _q(con, "SELECT COUNT(*) c FROM trades WHERE strategy=? AND ts_ms > ?",
               (strategy, since_ms))[0]["c"]
    return _finalize_stats(pnls, wins, int(total))


def decide_tier(stats: dict[str, float], passed: bool, *, explore_min_n: int,
                explore_min_per_trade: float) -> str | None:
    """'full' = significance proven; 'explore' = positive point estimate on a
    real sample, trade at reduced caps to buy information; None = stay paper."""
    if passed:
        return "full"
    if (
        stats["n"] >= explore_min_n
        and stats["pnl"] > 0
        and stats["per_trade"] >= explore_min_per_trade
    ):
        return "explore"
    return None


def evaluate_gates(con: sqlite3.Connection, *, min_n: int, since_ms: int) -> dict[str, dict]:
    """Returns {lane: {passed, stats}} for copy, fade, model. Copy/fade gates
    judge the conviction subset because that is what live execution takes."""
    out: dict[str, dict] = {}
    for lane, strat in (("copy", "copy_shadow"), ("fade", "fade_shadow")):
        st = conviction_stats(con, strat, since_ms)
        out[lane] = {
            "stats": st,
            # Open only when the 2-sigma LOWER bound of per-trade PnL clears
            # zero (audit: a point estimate of +0.05 at n=100 passes pure
            # luck ~30% of the time) and >=70% of entered trades resolved.
            "passed": (
                st["n"] >= min_n and st["pnl"] > 0
                and st["lower_bound"] > 0.0
                and st["coverage"] >= 0.70
            ),
        }
    st = model_slice_stats(con, since_ms)
    out["model"] = {
        "stats": st,
        "passed": (
            st["n"] >= min_n and st["wr"] >= 0.52 and st["pnl"] > 0
            and st["lower_bound"] > 0.0
        ),
    }
    return out


def main() -> None:
    args = build_parser().parse_args()
    out_path = Path(args.jsonl_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    flag_dir = Path(args.flag_dir)

    running = True

    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    flags = {
        "copy": flag_dir / "gate_copy_live.flag",
        "fade": flag_dir / "gate_fade_live.flag",
        "model": flag_dir / "gate_model_live.flag",
    }
    live_strat = {"copy": "copy_live", "fade": "fade_live", "model": "scored_daemon"}
    fail_streak: dict[str, int] = {"copy": 0, "fade": 0, "model": 0}
    paper_strat = {"copy": "copy_shadow", "fade": "fade_shadow", "model": "shadow_daemon"}

    _log(out_path, {"event": "startup", "ts_ms": int(time.time() * 1000)})

    iteration = 0
    while running:
        iteration += 1
        now_ms = int(time.time() * 1000)
        try:
            con = sqlite3.connect(f"file:{args.learner_db}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                # Two windows: the EXPLORE tier (reduced caps) may act on the
                # rolling window that includes the data which suggested the
                # conviction filter — it IS the bounded forward experiment.
                # FULL caps require the gate to pass on post-selection data
                # only (multiple-comparisons guard from the 2026-06-11 audit).
                window_since = max(args.since_ms, now_ms - 48 * 3600 * 1000)
                forward_since = max(window_since, args.conviction_since_floor)
                gates = evaluate_gates(con, min_n=args.min_n, since_ms=window_since)
                gates_forward = evaluate_gates(con, min_n=args.min_n, since_ms=forward_since)
                for lane in gates:
                    gates[lane]["passed"] = gates_forward[lane]["passed"]
                    gates[lane]["forward_stats"] = gates_forward[lane]["stats"]

                # Kill switch first
                today = live_pnl_today(con)
                if today <= -abs(args.kill_daily_loss):
                    for lane, flag in flags.items():
                        if flag.exists():
                            flag.unlink()
                    if args.manage_model_pm2:
                        subprocess.run(["pm2", "stop", "limitless-hl-live"],
                                       capture_output=True, timeout=30)
                    _log(out_path, {"event": "kill_switch", "live_pnl_today": today,
                                    "ts_ms": now_ms})
                    tg_send(f"KILL SWITCH: live PnL today {today:+.2f} USDC. "
                            "All gates closed, live trading stopped.")
                else:
                    for lane, info in gates.items():
                        flag = flags[lane]
                        st = info["stats"]
                        target = decide_tier(
                            st, info["passed"],
                            explore_min_n=args.explore_min_n,
                            explore_min_per_trade=args.explore_min_per_trade,
                        )
                        current = None
                        if flag.exists():
                            try:
                                current = json.loads(flag.read_text()).get("tier", "full")
                            except (json.JSONDecodeError, OSError):
                                current = "full"

                        if target and target != current:
                            flag.write_text(json.dumps(
                                {"tier": target, "opened_ms": now_ms, **st}))
                            fail_streak[lane] = 0
                            if lane == "model" and args.manage_model_pm2:
                                subprocess.run(
                                    ["pm2", "start", "ecosystem.config.cjs",
                                     "--only", "limitless-hl-live"],
                                    capture_output=True, timeout=60,
                                    cwd=str(Path.cwd()),
                                )
                            _log(out_path, {"event": "gate_opened", "lane": lane,
                                            "tier": target, **st, "ts_ms": now_ms})
                            tg_send(
                                f"GATE {target.upper()}: {lane} lane live at "
                                f"{'full' if target == 'full' else 'reduced explore'} caps. "
                                f"n={st['n']} wr={st['wr']:.0%} pnl={st['pnl']:+.2f} "
                                f"(per-trade {st['per_trade']:+.3f}, "
                                f"2-sigma lower {st['lower_bound']:+.3f})."
                            )

                        # Close when no tier is justified (2-eval hysteresis).
                        if flag.exists() and target is None:
                            fail_streak[lane] += 1
                            if fail_streak[lane] >= 2:
                                flag.unlink()
                                if lane == "model" and args.manage_model_pm2:
                                    subprocess.run(["pm2", "stop", "limitless-hl-live"],
                                                   capture_output=True, timeout=30)
                                _log(out_path, {"event": "gate_closed", "lane": lane,
                                                **st, "ts_ms": now_ms})
                                tg_send(
                                    f"GATE CLOSED: {lane} lane lost its tier "
                                    f"(n={st['n']} per-trade {st['per_trade']:+.3f}). "
                                    "Back to paper."
                                )
                                fail_streak[lane] = 0
                        elif not flag.exists():
                            fail_streak[lane] = 0

                        # Divergence pause for open gates. Two triggers:
                        # (a) steady state: live trails paper over the window;
                        # (b) ramp-up: absolute live floor at small n, because
                        #     waiting for 30 resolved live trades leaves the
                        #     breaker unreachable for weeks (audit F8).
                        if flag.exists():
                            ln, live_pt = recent_per_trade(
                                con, live_strat[lane], args.divergence_window)
                            pn, paper_pt = recent_per_trade(
                                con, paper_strat[lane], args.divergence_window)
                            if (
                                (ln >= 8 and live_pt < -0.10)
                                or (
                                    ln >= args.divergence_window
                                    and pn >= args.divergence_window
                                    and live_pt < paper_pt - args.divergence_usdc
                                )
                            ):
                                flag.unlink()
                                if lane == "model" and args.manage_model_pm2:
                                    subprocess.run(["pm2", "stop", "limitless-hl-live"],
                                                   capture_output=True, timeout=30)
                                _log(out_path, {"event": "divergence_pause", "lane": lane,
                                                "live_per_trade": live_pt,
                                                "paper_per_trade": paper_pt,
                                                "ts_ms": now_ms})
                                tg_send(
                                    f"DIVERGENCE PAUSE: {lane} live "
                                    f"({live_pt:+.3f}/trade) trails paper "
                                    f"({paper_pt:+.3f}/trade) beyond tolerance. "
                                    "Gate closed; paper continues."
                                )

                if iteration % 6 == 1:
                    _log(out_path, {"event": "heartbeat",
                                    "gates": {k: v["stats"] for k, v in gates.items()},
                                    "open": [k for k, f in flags.items() if f.exists()],
                                    "live_pnl_today": today, "ts_ms": now_ms})
            finally:
                con.close()
        except Exception as exc:
            _log(out_path, {"event": "gatekeeper_error", "error": str(exc)[:200],
                            "ts_ms": now_ms})

        if args.iterations and iteration >= args.iterations:
            break
        if running:
            time.sleep(max(args.loop_seconds, 10))

    _log(out_path, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})


if __name__ == "__main__":
    main()
