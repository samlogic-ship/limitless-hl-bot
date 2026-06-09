from __future__ import annotations

import argparse
import json
from pathlib import Path

from .attribution import build_paper_fills, evaluate_fills, load_scan_candidates
from .clients import LimitlessClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate Limitless <> Hyperliquid dry-run candidates")
    parser.add_argument("--jsonl", default="tmp/limitless_hl/overnight_dry_run.jsonl")
    parser.add_argument("--limitless-url", default="https://api.limitless.exchange")
    parser.add_argument("--out", default="tmp/limitless_hl/evaluation_report.json")
    parser.add_argument("--max-markets", type=int, default=200)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    candidates = load_scan_candidates(args.jsonl)
    fills = build_paper_fills(candidates)
    client = LimitlessClient(args.limitless_url)
    resolved = {}
    for fill in fills[: args.max_markets]:
        try:
            resolved[fill.slug] = client.resolved_market(fill.slug)
        except Exception as exc:
            resolved[fill.slug] = None
    report = evaluate_fills(fills, {key: value for key, value in resolved.items() if value is not None})
    report["source_jsonl"] = args.jsonl
    report["unique_paper_fills"] = len(fills)
    report["resolution_probe_count"] = len(resolved)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
