"""limitless_hl/calibrator.py — session-cadence model recalibration.

Every run (default: each 6h trading session) refits two pricing parameters per
symbol against the last 3 days of Hyperliquid 1m candles:
- vol_scale: multiplier on the EWMA vol (corrects systematic bias)
- shade: mean-reversion shade after two same-direction 15m candles

Grid-fit by Brier score at minutes 3/7/11 inside each 15m window — the same
method as the original tmp/study/ calibration, automated. Results land in
tmp/limitless_hl/pricing_params.json, hot-reloaded by PricingProvider, so the
live model tightens every session instead of every few weeks.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import requests

from .clients import LimitlessClient
from .volatility import EWMA_LAMBDA, HL_INFO_URL

GRID_SCALES = (0.8, 0.9, 1.0, 1.1, 1.25)
GRID_SHADES = (0.0, 0.02, 0.04)
SAMPLE_MINUTES = (3, 7, 11)
LOOKBACK_DAYS = 3
MIN_WINDOWS = 120


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _candles_1m(session: requests.Session, coin: str, days: int) -> list[dict]:
    now_ms = int(time.time() * 1000)
    out: list[dict] = []
    end = now_ms
    target = now_ms - days * 86_400_000
    while end > target:
        resp = session.post(HL_INFO_URL, json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1m",
                    "startTime": max(target, end - 4900 * 60_000), "endTime": end},
        }, timeout=20)
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        out = batch + out
        new_end = int(batch[0]["t"]) - 1
        if new_end >= end:
            break
        end = new_end
        time.sleep(0.2)
    seen: set[int] = set()
    deduped = []
    for c in out:
        if c["t"] not in seen:
            seen.add(c["t"])
            deduped.append(c)
    deduped.sort(key=lambda c: c["t"])
    return deduped


def fit_symbol(session: requests.Session, symbol: str) -> dict | None:
    try:
        candles = _candles_1m(session, symbol, LOOKBACK_DAYS)
    except Exception:
        return None
    windows: dict[int, list[dict]] = {}
    for c in candles:
        windows.setdefault(int(c["t"]) // 900_000, []).append(c)
    full = {w: v for w, v in windows.items() if len(v) == 15}
    keys = sorted(full)
    if len(keys) < MIN_WINDOWS:
        return None

    # EWMA per-minute variance series
    ewma: dict[int, float] = {}
    variance: float | None = None
    closes = [(int(c["t"]), float(c["c"])) for c in candles if float(c.get("c") or 0) > 0]
    for (_, prev), (t, cur) in zip(closes, closes[1:]):
        r = math.log(cur / prev)
        variance = r * r if variance is None else EWMA_LAMBDA * variance + (1 - EWMA_LAMBDA) * r * r
        ewma[t] = math.sqrt(variance * 525_600)

    prevdir: dict[int, str] = {}
    for j in range(2, len(keys)):
        if keys[j - 1] == keys[j] - 1 and keys[j - 2] == keys[j] - 2:
            d1 = "U" if float(full[keys[j - 1]][-1]["c"]) > float(full[keys[j - 1]][0]["o"]) else "D"
            d2 = "U" if float(full[keys[j - 2]][-1]["c"]) > float(full[keys[j - 2]][0]["o"]) else "D"
            prevdir[keys[j]] = d2 + d1

    best: tuple[float, float, float] | None = None  # (brier, scale, shade)
    for scale in GRID_SCALES:
        for shade_amt in GRID_SHADES:
            total, n = 0.0, 0
            for w in keys:
                v = full[w]
                threshold = float(v[0]["o"])
                outcome = 1 if float(v[-1]["c"]) > threshold else 0
                direction = prevdir.get(w, "")
                shade = -shade_amt if direction == "UU" else (shade_amt if direction == "DD" else 0.0)
                for m in SAMPLE_MINUTES:
                    cur = float(v[m - 1]["c"])
                    vol = max(0.15, min(3.0, ewma.get(int(v[m - 1]["t"]), 0.75) * scale))
                    sigma = vol * math.sqrt((15 - m) * 60 / (365 * 24 * 3600))
                    z = math.log(cur / threshold) / sigma if threshold > 0 and cur > 0 else 0.0
                    p = min(0.999, max(0.001, _ncdf(z) + shade))
                    total += (p - outcome) ** 2
                    n += 1
            brier = total / n
            if best is None or brier < best[0]:
                best = (brier, scale, shade_amt)
    assert best is not None
    return {"vol_scale": best[1], "shade": best[2], "brier": round(best[0], 5),
            "windows": len(keys), "fitted_at_ms": int(time.time() * 1000)}


def run_once(out_path: Path, symbols: list[str] | None = None) -> dict:
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    if not symbols:
        markets = LimitlessClient().active_crypto_markets()
        symbols = sorted({m.symbol for m in markets})
    params: dict[str, dict] = {}
    if out_path.exists():
        try:
            params = json.loads(out_path.read_text()).get("symbols") or {}
        except Exception:
            params = {}
    for symbol in symbols:
        fitted = fit_symbol(session, symbol)
        if fitted is not None:
            params[symbol] = fitted
        time.sleep(0.5)
    payload = {"updated_at_ms": int(time.time() * 1000), "symbols": params}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=1))
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Session-cadence pricing recalibration")
    parser.add_argument("--params-out", default="tmp/limitless_hl/pricing_params.json")
    parser.add_argument("--interval-seconds", type=int, default=6 * 3600,
                        help="Refit cadence; one session = 6h (Asia/Europe/US opens)")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    out_path = Path(args.params_out)
    while True:
        started = time.time()
        try:
            payload = run_once(out_path)
            fitted = {s: (p["vol_scale"], p["shade"]) for s, p in payload["symbols"].items()}
            print(json.dumps({"event": "calibrated", "fitted": fitted}), flush=True)
        except Exception as exc:
            print(json.dumps({"event": "calibrate_error", "error": str(exc)}), flush=True)
        if args.once:
            break
        time.sleep(max(60, args.interval_seconds - (time.time() - started)))


if __name__ == "__main__":
    main()
