from __future__ import annotations

import argparse
import json
import os
import time

from .clients import HyperliquidClient, LimitlessClient
from .live_trade import (
    LimitlessCredentials,
    LimitlessOrderBuilder,
    LimitlessSubmitter,
    PairTradeRunner,
    candidate_to_limitless_intent,
)
from .model import EdgeConfig
from .scanner import LimitlessHyperliquidScanner
from .secrets import get_secret


class PreviewLeg:
    def __init__(self, intent):
        self.intent = intent

    def submit(self, candidate):
        return {
            "submitted": False,
            "matched": False,
            "filled_usdc": 0.0,
            "intent": self.intent.__dict__,
            "raw": {"mode": "preview"},
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or run a Limitless <> Hyperliquid pair trade")
    parser.add_argument("--live-armed", action="store_true")
    parser.add_argument("--min-edge", type=float, default=0.08)
    parser.add_argument("--max-price", type=float, default=0.85)
    parser.add_argument("--min-seconds-to-expiry", type=int, default=180)
    parser.add_argument("--stake-usdc", type=float, default=25.0)
    parser.add_argument("--client-order-prefix", default="limitless-hl")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    limitless = LimitlessClient()
    scanner = LimitlessHyperliquidScanner(
        limitless=limitless,
        hyperliquid=HyperliquidClient(),
        config=EdgeConfig(
            min_edge=args.min_edge,
            max_price=args.max_price,
            min_seconds_to_expiry=args.min_seconds_to_expiry,
            stake_usdc=args.stake_usdc,
        ),
    )
    report = scanner.scan_report()
    candidates = report.get("candidates") or []
    if not candidates:
        print(json.dumps({"submitted": False, "reason": "no_candidate", "scan_report": report}, indent=2, sort_keys=True))
        return
    candidate = candidates[0]
    details = limitless.market_details(candidate["slug"])
    client_order_id = f"{args.client_order_prefix}-{candidate['slug']}-{candidate['side']}-{int(time.time() * 1000)}"
    intent = candidate_to_limitless_intent(candidate, details, client_order_id=client_order_id)
    if not args.live_armed:
        runner = PairTradeRunner(limitless=PreviewLeg(intent))
        print(json.dumps(runner.run(candidate).to_dict(), indent=2, sort_keys=True))
        return

    token_id = get_secret("LIMITLESS_TOKEN_ID")
    token_secret = get_secret("LIMITLESS_TOKEN_SECRET")
    private_key = get_secret("LIMITLESS_PRIVATE_KEY")
    owner_id = os.environ.get("LIMITLESS_OWNER_ID")
    maker_address = os.environ.get("LIMITLESS_MAKER_ADDRESS")
    required_values = {
        "LIMITLESS_TOKEN_ID": token_id,
        "LIMITLESS_TOKEN_SECRET": token_secret,
        "LIMITLESS_PRIVATE_KEY": private_key,
        "LIMITLESS_OWNER_ID": owner_id,
        "LIMITLESS_MAKER_ADDRESS": maker_address,
    }
    missing = [name for name, value in required_values.items() if not value]
    if missing:
        raise SystemExit(f"live armed but missing required env vars: {', '.join(missing)}")
    submitter = LimitlessSubmitter(
        credentials=LimitlessCredentials(token_id or "", token_secret or ""),
        builder=LimitlessOrderBuilder(
            maker=maker_address or "",
            owner_id=int(owner_id or "0"),
            fee_rate_bps=int(os.environ.get("LIMITLESS_FEE_RATE_BPS", "0")),
        ),
        private_key=private_key or "",
    )
    runner = PairTradeRunner(
        limitless=type(
            "LiveLimitlessLeg",
            (),
            {"submit": lambda _self, candidate: submitter.submit_intent(intent)},
        )(),
    )
    print(json.dumps(runner.run(candidate).to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
