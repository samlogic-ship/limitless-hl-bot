from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .clients import HyperliquidClient, LimitlessClient
from .model import EdgeConfig
from .scanner import LimitlessHyperliquidScanner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Limitless crypto binary scanner hedged against Hyperliquid mids")
    parser.add_argument("--limitless-url", default="https://api.limitless.exchange")
    parser.add_argument("--hyperliquid-url", default="https://api.hyperliquid.xyz")
    parser.add_argument("--min-edge", type=float, default=0.05)
    parser.add_argument("--vol", type=float, default=0.75, help="Annualized volatility assumption for binary fair value")
    parser.add_argument("--stake-usdc", type=float, default=25.0)
    parser.add_argument("--min-size-usdc", type=float, default=25.0)
    parser.add_argument("--fee-buffer", type=float, default=0.015)
    parser.add_argument("--max-price", type=float, default=0.97)
    parser.add_argument("--min-seconds-to-expiry", type=int, default=45)
    parser.add_argument("--max-seconds-to-expiry", type=int, default=24 * 60 * 60)
    parser.add_argument("--interval-seconds", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=1, help="0 means run forever")
    parser.add_argument("--jsonl-out", default="limitless_hl_dry_run.jsonl")
    parser.add_argument("--submit", action="store_true", help="Reserved for live trading; currently refuses")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.submit:
        raise SystemExit("live submission is not implemented; dry-run scanner only")

    config = EdgeConfig(
        min_edge=args.min_edge,
        annualized_volatility=args.vol,
        min_size_usdc=args.min_size_usdc,
        max_price=args.max_price,
        min_seconds_to_expiry=args.min_seconds_to_expiry,
        max_seconds_to_expiry=args.max_seconds_to_expiry,
        fee_buffer=args.fee_buffer,
        stake_usdc=args.stake_usdc,
    )
    scanner = LimitlessHyperliquidScanner(
        limitless=LimitlessClient(args.limitless_url),
        hyperliquid=HyperliquidClient(args.hyperliquid_url),
        config=config,
    )
    out_path = Path(args.jsonl_out)
    iteration = 0
    while True:
        iteration += 1
        scanned_at_ms = int(time.time() * 1000)
        try:
            payload = scanner.scan_report(now_ms=scanned_at_ms)
            payload["mode"] = "dry_run"
        except Exception as exc:
            payload = {
                "scanned_at_ms": scanned_at_ms,
                "mode": "dry_run",
                "error": str(exc),
                "candidate_count": 0,
                "candidates": [],
            }
        line = json.dumps(payload, sort_keys=True)
        print(line, flush=True)
        with out_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if args.iterations and iteration >= args.iterations:
            break
        time.sleep(max(args.interval_seconds, 1))


if __name__ == "__main__":
    main()
