"""
limitless_hl/funding_monitor.py — dormant funding-regime watcher.

Funding carry is a real edge ONLY when funding is extreme. 2026-06-14 backtest:
at the current calm regime (4-10%/yr) the hedge cost eats the funding (net
+1bp/wk). So this watcher stays asleep and alerts via Telegram only when an
asset's funding is sustained-elevated enough that delta-neutral carry would
clear the ~2-leg hedge cost — i.e. a regime worth deploying into. One efficient
metaAndAssetCtxs call per poll (all coins at once). No trading; alert only.
"""
from __future__ import annotations
import argparse, json, os, time, urllib.parse, urllib.request
from pathlib import Path

HL = "https://api.hyperliquid.xyz/info"


def _ctx():
    req = urllib.request.Request(HL, data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
                                 headers={"Content-Type": "application/json"})
    meta, ctx = json.load(urllib.request.urlopen(req, timeout=10))
    out = {}
    for i, a in enumerate(meta.get("universe", [])):
        try:
            out[a["name"]] = float(ctx[i].get("funding", 0) or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    return out


def _tg(msg):
    tok = os.environ.get("LIMITLESS_HL_TG_TOKEN"); chat = os.environ.get("LIMITLESS_HL_TG_CHAT")
    if not tok or not chat:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()), timeout=8)
    except Exception:
        pass


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/funding_monitor.jsonl")
    p.add_argument("--loop-seconds", type=int, default=1800)
    p.add_argument("--alert-annual-pct", type=float, default=40.0,
                   help="alert when sustained |funding| annualizes above this %/yr (carry-worthy regime)")
    p.add_argument("--sustain", type=int, default=4, help="consecutive elevated polls before alerting")
    p.add_argument("--realert-hours", type=float, default=12.0)
    return p


def main():
    import urllib.parse  # noqa
    a = build_parser().parse_args()
    out = Path(a.jsonl_out); out.parent.mkdir(parents=True, exist_ok=True)
    streak: dict[str, int] = {}
    last_alert: dict[str, float] = {}
    thr_hr = a.alert_annual_pct / 100.0 / (24 * 365)  # hourly funding fraction
    out.open("a").write(json.dumps({"event": "startup", "alert_annual_pct": a.alert_annual_pct,
                                    "ts_ms": int(time.time() * 1000)}) + "\n")
    while True:
        now = time.time()
        try:
            f = _ctx()
        except Exception as e:
            f = {}
        elevated = []
        for sym, rate in f.items():
            ann = abs(rate) * 24 * 365 * 100
            if abs(rate) >= thr_hr:
                streak[sym] = streak.get(sym, 0) + 1
                if streak[sym] >= a.sustain and now - last_alert.get(sym, 0) > a.realert_hours * 3600:
                    side = "SHORT perp + long spot" if rate > 0 else "LONG perp + short spot"
                    _tg(f"FUNDING SPIKE: {sym} funding {rate*1e4:+.3f}bps/hr (~{ann:.0f}%/yr), "
                        f"sustained {streak[sym]} polls. Carry-worthy: {side}. Backtest carry before sizing.")
                    last_alert[sym] = now
                    elevated.append({"sym": sym, "rate_bps_hr": round(rate * 1e4, 3), "ann_pct": round(ann)})
            else:
                streak[sym] = 0
        with out.open("a") as fh:
            fh.write(json.dumps({"event": "poll", "n_assets": len(f),
                                 "max_ann_pct": round(max((abs(r) * 24 * 365 * 100 for r in f.values()), default=0)),
                                 "alerts": elevated, "ts_ms": int(now * 1000)}) + "\n")
        time.sleep(max(a.loop_seconds, 60))


if __name__ == "__main__":
    main()
