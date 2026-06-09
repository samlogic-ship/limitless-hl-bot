from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .attribution import build_paper_fills, load_scan_candidates


def summarize_jsonl(path: str | Path) -> dict[str, Any]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    candidates = load_scan_candidates(path)
    fills = build_paper_fills(candidates)
    symbols = Counter(str(row.get("symbol") or "") for row in candidates)
    intervals = Counter(str(row.get("interval") or "") for row in candidates)
    sides = Counter(str(row.get("side") or "") for row in candidates)
    top = sorted(candidates, key=lambda row: float(row.get("edge") or 0), reverse=True)[:10]
    return {
        "scan_rows": len(rows),
        "candidate_rows": len(candidates),
        "unique_paper_fills": len(fills),
        "symbols": dict(symbols),
        "intervals": dict(intervals),
        "sides": dict(sides),
        "top_edges": top,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Limitless <> Hyperliquid dry-run JSONL")
    parser.add_argument("--jsonl", default="tmp/limitless_hl/overnight_dry_run.jsonl")
    args = parser.parse_args()
    print(json.dumps(summarize_jsonl(args.jsonl), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
