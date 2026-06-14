"""
limitless_hl/perp_momentum.py — HL perp rich-feature forward-return DATA ENGINE
+ directional shadow signal.

Why v2: the 17-day backtest (2026-06-13) showed COARSE 5m momentum alone has NO
significant edge (lower bound -1.35bps). The 4.5-day promise came from a RICHER
feature set (1m momentum + funding + Binance taker-flow imbalance) that cannot
be backtested long (1m history caps at ~3.6 days, taker-flow not stored). So we
validate it FORWARD: this lane logs the full rich feature vector on every sample
alongside the realized 15-min forward return (net of maker/taker fee), with no
lookahead. Offline we fit a walk-forward model on the accumulated data and find
empirically whether ANY signal combination predicts perp direction profitably.
No orders are placed. Build a live executor only if the fitted forward edge
clears a 2-sigma significance bar on adequate n.
"""
from __future__ import annotations
import argparse, json, signal, time, urllib.request
from pathlib import Path

ALTS = ["XRP", "SOL", "DOGE", "AVAX", "LINK", "LTC", "ADA"]
HL = "https://api.hyperliquid.xyz/info"
BINANCE = {"XRP": "XRPUSDT", "SOL": "SOLUSDT", "DOGE": "DOGEUSDT", "AVAX": "AVAXUSDT",
           "LINK": "LINKUSDT", "LTC": "LTCUSDT", "ADA": "ADAUSDT"}


def _hl(payload):
    req = urllib.request.Request(HL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=8))


def _candles(sym, mins):
    now = int(time.time() * 1000)
    try:
        b = _hl({"type": "candleSnapshot",
                 "req": {"coin": sym, "interval": "1m", "startTime": now - mins * 60000, "endTime": now}})
        return [(int(c["t"]), float(c["c"])) for c in b]
    except Exception:
        return []


def _funding_all():
    try:
        meta, ctx = _hl({"type": "metaAndAssetCtxs"})
        out = {}
        for i, a in enumerate(meta.get("universe", [])):
            out[a.get("name")] = float(ctx[i].get("funding", 0) or 0)
        return out
    except Exception:
        return {}


def _taker_imbalance(binsym):
    try:
        end = int(time.time() * 1000)
        rows = json.load(urllib.request.urlopen(
            f"https://api.binance.com/api/v3/aggTrades?symbol={binsym}&startTime={end-60000}&endTime={end}&limit=1000", timeout=6))
        buy = sell = 0.0
        for r in rows:
            n = float(r["p"]) * float(r["q"])
            if r.get("m"):
                sell += n
            else:
                buy += n
        tot = buy + sell
        return (buy - sell) / tot if tot > 0 else 0.0
    except Exception:
        return 0.0


def _feat(c):
    px = [x[1] for x in c]
    if len(px) < 16:
        return None
    def m(k):
        return (px[-1] - px[-1 - k]) / px[-1 - k] * 1e4
    rets = [(px[i] - px[i - 1]) / px[i - 1] for i in range(1, len(px))]
    vol = (sum(r * r for r in rets) / len(rets)) ** 0.5 * 1e4
    return {"m1": m(1), "m3": m(3), "m5": m(5), "m15": m(15), "vol": vol}


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/perp_shadow.jsonl")
    p.add_argument("--loop-seconds", type=int, default=60)
    p.add_argument("--horizon-seconds", type=int, default=900)
    p.add_argument("--sample-cooldown", type=int, default=300, help="per-symbol gap between samples")
    return p


def _log(path, p):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(p, sort_keys=True) + "\n")


def main():
    a = build_parser().parse_args()
    out = Path(a.jsonl_out); out.parent.mkdir(parents=True, exist_ok=True)
    pending: list[dict] = []
    last: dict[str, int] = {}
    running = True
    def _stop(s, f):
        nonlocal running; running = False
    signal.signal(signal.SIGTERM, _stop); signal.signal(signal.SIGINT, _stop)
    _log(out, {"event": "startup", "mode": "shadow_richfeatures", "ts_ms": int(time.time() * 1000)})
    while running:
        now = int(time.time() * 1000)
        # resolve ripe samples with forward return net of fee
        keep = []
        for s in pending:
            if now - s["ts_ms"] >= a.horizon_seconds * 1000:
                c = _candles(s["sym"], 2)
                ex = c[-1][1] if c else None
                if ex:
                    ret = (ex - s["entry_px"]) / s["entry_px"]
                    d = 1 if s["mom_dir"] > 0 else -1
                    _log(out, {"event": "resolve", "sym": s["sym"], "feat": s["feat"],
                               "mom_dir": s["mom_dir"], "funding": s["funding"], "taker_imb": s["taker_imb"],
                               "entry_px": s["entry_px"], "exit_px": ex, "ret_bps": round(ret * 1e4, 2),
                               "dir_win": 1 if (ret > 0) == (s["mom_dir"] > 0) else 0,
                               "net_maker_bps": round(d * ret * 1e4 - 3.0, 2),
                               "net_taker_bps": round(d * ret * 1e4 - 9.0, 2),
                               "ts_ms": now})
                else:
                    keep.append(s)
            else:
                keep.append(s)
        pending = keep
        funding = _funding_all()
        for sym in ALTS:
            if now - last.get(sym, 0) < a.sample_cooldown * 1000:
                continue
            c = _candles(sym, 17)
            f = _feat(c)
            if not f:
                continue
            ti = _taker_imbalance(BINANCE[sym])
            mom_dir = 1 if (f["m1"] + f["m3"] + f["m5"]) > 0 else -1
            pending.append({"sym": sym, "feat": f, "funding": funding.get(sym, 0.0),
                            "taker_imb": ti, "mom_dir": mom_dir, "entry_px": c[-1][1], "ts_ms": now})
            last[sym] = now
            _log(out, {"event": "sample", "sym": sym, "feat": f, "funding": funding.get(sym, 0.0),
                       "taker_imb": round(ti, 4), "mom_dir": mom_dir, "entry_px": c[-1][1], "ts_ms": now})
            time.sleep(0.2)
        if running:
            time.sleep(max(a.loop_seconds, 5))
    _log(out, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})


if __name__ == "__main__":
    main()
