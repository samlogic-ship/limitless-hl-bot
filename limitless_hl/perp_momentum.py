"""
limitless_hl/perp_momentum.py — HL alt-momentum directional shadow lane.

Backtest 2026-06-13 (walk-forward, real forward returns, net of fees): the
Limitless 5-15m fee wall is fatal, but the SAME directional signal on HL PERPS
(4.5bps taker / ~1.5-3bps maker) is positive on alts: XRP +10.3, SOL +5.4,
DOGE +5.0 bps/trade at maker fee (BTC/BNB negative -> excluded). Edge lives in
LOW-to-moderate vol and HIGH model confidence. n=102, one 4.5-day regime ->
SHADOW ONLY until it proves out live; the gatekeeper-style significance bar
(positive 2-sigma lower bound on adequate n) gates any live arming.

This process emits a directional shadow "position" when momentum aligns, holds
to horizon, scores the realized forward return net of maker+taker fee, and logs
to perp_shadow.jsonl. No orders are placed. v1 signal is transparent (momentum
agreement + taker-flow); a fitted logistic refit on its own resolved data is a
later upgrade.
"""
from __future__ import annotations
import argparse, json, signal, time, urllib.request
from pathlib import Path
from typing import Any

ALTS = ["XRP", "SOL", "DOGE"]
HL = "https://api.hyperliquid.xyz/info"


def _candles(sym: str, mins: int) -> list[tuple[int, float]]:
    now = int(time.time() * 1000)
    try:
        req = urllib.request.Request(HL, data=json.dumps({
            "type": "candleSnapshot",
            "req": {"coin": sym, "interval": "1m", "startTime": now - mins * 60000, "endTime": now},
        }).encode(), headers={"Content-Type": "application/json"})
        b = json.load(urllib.request.urlopen(req, timeout=8))
        return [(int(c["t"]), float(c["c"])) for c in b]
    except Exception:
        return []


def _signal(c: list[tuple[int, float]]) -> tuple[int, float, float]:
    """Return (direction, confidence, vol_bps_per_min) from 1m closes."""
    if len(c) < 6:
        return 0, 0.0, 0.0
    px = [x[1] for x in c]
    m1 = (px[-1] - px[-2]) / px[-2] * 1e4
    m3 = (px[-1] - px[-4]) / px[-4] * 1e4
    m5 = (px[-1] - px[-6]) / px[-6] * 1e4
    rets = [(px[i] - px[i - 1]) / px[i - 1] for i in range(1, len(px))]
    vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 * 1e4
    # agreement: all three momenta same sign -> strong; confidence ~ scaled mean
    sgn = [1 if m > 0 else -1 if m < 0 else 0 for m in (m1, m3, m5)]
    agree = abs(sum(sgn))  # 0..3
    direction = 1 if sum(sgn) > 0 else -1 if sum(sgn) < 0 else 0
    conf = (agree / 3.0) * min(1.0, (abs(m1) + abs(m3) + abs(m5)) / 30.0)
    return direction, conf, vol


def _fee_net(direction: int, ret: float, fee_bps: float) -> float:
    return direction * ret * 1e4 - fee_bps


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/perp_shadow.jsonl")
    p.add_argument("--loop-seconds", type=int, default=60)
    p.add_argument("--horizon-seconds", type=int, default=900)
    p.add_argument("--min-conf", type=float, default=0.34)
    p.add_argument("--vol-max", type=float, default=12.0, help="bps/min; skip high-vol whipsaw")
    p.add_argument("--cooldown-seconds", type=int, default=600, help="min gap between signals per symbol")
    return p


def _log(path: Path, p: dict[str, Any]):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(p, sort_keys=True) + "\n")


def main():
    a = build_parser().parse_args()
    out = Path(a.jsonl_out); out.parent.mkdir(parents=True, exist_ok=True)
    pending: list[dict] = []
    last_sig: dict[str, int] = {}
    running = True
    def _stop(s, f):
        nonlocal running; running = False
    signal.signal(signal.SIGTERM, _stop); signal.signal(signal.SIGINT, _stop)
    _log(out, {"event": "startup", "mode": "shadow", "ts_ms": int(time.time() * 1000)})
    while running:
        now = int(time.time() * 1000)
        # resolve ripe positions
        still = []
        for pos in pending:
            if now - pos["entry_ts"] >= a.horizon_seconds * 1000:
                c = _candles(pos["sym"], 2)
                exit_px = c[-1][1] if c else None
                if exit_px:
                    ret = (exit_px - pos["entry_px"]) / pos["entry_px"]
                    _log(out, {"event": "resolve", "sym": pos["sym"], "dir": pos["dir"],
                               "conf": pos["conf"], "vol": pos["vol"], "entry_px": pos["entry_px"],
                               "exit_px": exit_px, "ret_bps": round(ret * 1e4, 2),
                               "net_taker_bps": round(_fee_net(pos["dir"], ret, 9.0), 2),
                               "net_maker_bps": round(_fee_net(pos["dir"], ret, 3.0), 2),
                               "ts_ms": now})
                else:
                    still.append(pos)
            else:
                still.append(pos)
        pending = still
        # emit new signals
        for sym in ALTS:
            if now - last_sig.get(sym, 0) < a.cooldown_seconds * 1000:
                continue
            c = _candles(sym, 8)
            if not c:
                continue
            d, conf, vol = _signal(c)
            if d != 0 and conf >= a.min_conf and vol <= a.vol_max:
                pending.append({"sym": sym, "dir": d, "conf": conf, "vol": vol,
                                "entry_px": c[-1][1], "entry_ts": now})
                last_sig[sym] = now
                _log(out, {"event": "signal", "sym": sym, "dir": d, "conf": round(conf, 3),
                           "vol": round(vol, 1), "entry_px": c[-1][1], "ts_ms": now})
            time.sleep(0.2)
        if running:
            time.sleep(max(a.loop_seconds, 5))
    _log(out, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})


if __name__ == "__main__":
    main()
