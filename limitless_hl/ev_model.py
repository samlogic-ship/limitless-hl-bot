"""
limitless_hl/ev_model.py — self-recalibrating EV model (improvement loop).

Every --interval-seconds, fits a logistic regression (pure python, no sklearn)
on all resolved shadow/copy trades' recorded features -> win/loss, with a
time-ordered 75/25 train/test split, and writes tmp/limitless_hl/ev_model.json:

  { n, n_test, auc_test, brier_test, base_rate, weights, feature_names,
    active, fitted_at_ms }

`active` flips true only when n >= --min-n AND test AUC >= --min-auc — i.e.
the model has to beat coin-flip ranking out-of-sample on a real sample before
anything is allowed to consume it. Until then this loop just keeps measuring.
The daily report surfaces readiness; wiring `active` weights into the scorer
is a deliberate, separate step.
"""
from __future__ import annotations

import argparse
import json
import math
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

FEATURES = [
    "momentum_1m_bps",
    "momentum_3m_bps",
    "momentum_5m_bps",
    "funding",
    "binance_taker_imbalance_1m",
]
# engineered on top of raw features
DERIVED = ["entry_price", "basis_bps", "is_up"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Refit the EV model on resolved trades")
    p.add_argument("--learner-db", default="tmp/limitless_hl/learner.sqlite3")
    p.add_argument("--model-out", default="tmp/limitless_hl/ev_model.json")
    p.add_argument("--interval-seconds", type=int, default=21600)
    p.add_argument("--min-n", type=int, default=500)
    p.add_argument("--min-auc", type=float, default=0.55)
    p.add_argument("--strategies", default="shadow_daemon,copy_shadow,fade_shadow")
    p.add_argument("--iterations", type=int, default=0)
    return p


def extract_rows(db_path: str, strategies: list[str]) -> list[tuple[list[float], int]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        q = con.execute(
            "SELECT t.raw_json, t.price, t.side, r.won FROM trades t "
            "JOIN resolutions r ON t.trade_key = r.trade_key "
            f"WHERE t.strategy IN ({','.join('?' * len(strategies))}) "
            "ORDER BY t.ts_ms",
            strategies,
        ).fetchall()
    finally:
        con.close()
    rows: list[tuple[list[float], int]] = []
    for r in q:
        try:
            cand = (json.loads(r["raw_json"]).get("candidate")) or {}
        except (json.JSONDecodeError, TypeError):
            continue
        feats = cand.get("score_features") or {}
        vec: list[float] = []
        ok = True
        for name in FEATURES:
            v = feats.get(name)
            if v is None:
                v = 0.0  # absent feature (e.g. imbalance pre-wiring) -> neutral
            try:
                vec.append(float(v))
            except (TypeError, ValueError):
                ok = False
                break
        if not ok:
            continue
        hl_mid = feats.get("hl_mid") or 0.0
        thr = cand.get("threshold_price") or hl_mid
        basis = ((hl_mid - thr) / thr * 10_000.0) if thr else 0.0
        vec.extend([float(r["price"]), basis, 1.0 if r["side"] == "UP" else 0.0])
        rows.append((vec, int(r["won"])))
    return rows


def _standardize(x: list[list[float]]) -> tuple[list[list[float]], list[float], list[float]]:
    dims = len(x[0])
    mean = [sum(row[j] for row in x) / len(x) for j in range(dims)]
    std = [
        math.sqrt(sum((row[j] - mean[j]) ** 2 for row in x) / len(x)) or 1.0
        for j in range(dims)
    ]
    z = [[(row[j] - mean[j]) / std[j] for j in range(dims)] for row in x]
    return z, mean, std


def fit_logistic(
    x: list[list[float]], y: list[int], *, epochs: int = 300, lr: float = 0.1, l2: float = 0.01
) -> list[float]:
    """Plain gradient-descent logistic regression. Returns [bias, w1..wn]."""
    n, dims = len(x), len(x[0])
    w = [0.0] * (dims + 1)
    for _ in range(epochs):
        grad = [0.0] * (dims + 1)
        for xi, yi in zip(x, y):
            z = w[0] + sum(wj * xj for wj, xj in zip(w[1:], xi))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            err = p - yi
            grad[0] += err
            for j in range(dims):
                grad[j + 1] += err * xi[j]
        for j in range(dims + 1):
            reg = l2 * w[j] if j > 0 else 0.0
            w[j] -= lr * (grad[j] / n + reg)
    return w


def predict(w: list[float], xi: list[float]) -> float:
    z = w[0] + sum(wj * xj for wj, xj in zip(w[1:], xi))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def auc(scores: list[float], labels: list[int]) -> float:
    pairs = sorted(zip(scores, labels))
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return 0.5
    rank_sum = 0.0
    for rank, (_, label) in enumerate(pairs, start=1):
        if label:
            rank_sum += rank
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def refit(db_path: str, strategies: list[str], *, min_n: int, min_auc: float) -> dict[str, Any]:
    rows = extract_rows(db_path, strategies)
    n = len(rows)
    result: dict[str, Any] = {
        "n": n,
        "feature_names": FEATURES + DERIVED,
        "fitted_at_ms": int(time.time() * 1000),
        "active": False,
    }
    if n < 40:
        result["note"] = "too few resolved trades to fit"
        return result
    split = int(n * 0.75)
    x_all = [r[0] for r in rows]
    y_all = [r[1] for r in rows]
    z_all, mean, std = _standardize(x_all)
    w = fit_logistic(z_all[:split], y_all[:split])
    test_scores = [predict(w, xi) for xi in z_all[split:]]
    test_labels = y_all[split:]
    a = auc(test_scores, test_labels)
    brier = sum((p - y) ** 2 for p, y in zip(test_scores, test_labels)) / len(test_labels)
    result.update({
        "n_test": len(test_labels),
        "auc_test": round(a, 4),
        "brier_test": round(brier, 4),
        "base_rate": round(sum(y_all) / n, 4),
        "weights": [round(v, 6) for v in w],
        "feature_mean": [round(v, 6) for v in mean],
        "feature_std": [round(v, 6) for v in std],
        "active": n >= min_n and a >= min_auc,
    })
    return result


def main() -> None:
    args = build_parser().parse_args()
    out = Path(args.model_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    running = True

    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    iteration = 0
    while running:
        iteration += 1
        try:
            model = refit(args.learner_db, strategies,
                          min_n=args.min_n, min_auc=args.min_auc)
            tmp = out.with_suffix(".tmp")
            tmp.write_text(json.dumps(model, indent=1, sort_keys=True))
            tmp.replace(out)
            print(json.dumps({
                "event": "refit", "n": model.get("n"),
                "auc_test": model.get("auc_test"), "active": model.get("active"),
            }, sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"event": "refit_error", "error": str(exc)[:200]},
                             sort_keys=True), flush=True)
        if args.iterations and iteration >= args.iterations:
            break
        if running:
            time.sleep(max(args.interval_seconds, 60))


if __name__ == "__main__":
    main()
