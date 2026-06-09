from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .attribution import ResolvedMarket
from .clients import LimitlessClient


@dataclass(frozen=True, slots=True)
class LearnedTrade:
    trade_key: str
    source: str
    source_path: str
    line_no: int
    ts_ms: int
    slug: str
    symbol: str
    interval: str
    side: str
    price: float
    stake_usdc: float
    strategy: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LearnedResolution:
    trade: LearnedTrade
    market: ResolvedMarket
    won: bool
    pnl_usdc: float
    resolved_at_ms: int

    def to_report_row(self) -> dict[str, Any]:
        fill = {
            "slug": self.trade.slug,
            "symbol": self.trade.symbol,
            "side": self.trade.side,
            "price": self.trade.price,
            "stake_usdc": self.trade.stake_usdc,
            "scanned_at_ms": self.trade.ts_ms,
            "raw": {
                **self.trade.raw,
                "interval": self.trade.interval,
                "strategy": self.trade.strategy,
                "source": self.trade.source,
            },
        }
        return {
            "fill": fill,
            "resolved_market": asdict(self.market),
            "won": self.won,
            "pnl_usdc": self.pnl_usdc,
            "resolved_at_ms": self.resolved_at_ms,
            "trade_key": self.trade.trade_key,
        }


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS trades (
    trade_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_path TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    slug TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('UP', 'DOWN')),
    price REAL NOT NULL CHECK(price > 0 AND price < 1),
    stake_usdc REAL NOT NULL CHECK(stake_usdc > 0),
    strategy TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    inserted_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_source_line ON trades(source_path, line_no);
CREATE INDEX IF NOT EXISTS idx_trades_slug ON trades(slug);
CREATE INDEX IF NOT EXISTS idx_trades_slice ON trades(interval, symbol, side);

CREATE TABLE IF NOT EXISTS resolutions (
    trade_key TEXT PRIMARY KEY REFERENCES trades(trade_key) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    winning_outcome_index INTEGER NOT NULL CHECK(winning_outcome_index IN (0, 1)),
    won INTEGER NOT NULL CHECK(won IN (0, 1)),
    pnl_usdc REAL NOT NULL,
    resolved_at_ms INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resolutions_slug ON resolutions(slug);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def ingest_jsonl(conn: sqlite3.Connection, paths: Iterable[str | Path], *, now_ms: int | None = None) -> int:
    inserted = 0
    stamp = int(now_ms if now_ms is not None else time.time() * 1000)
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        source = _source_name(path)
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                trade = trade_from_event(payload, source=source, source_path=str(path), line_no=line_no)
                if trade is None:
                    continue
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO trades (
                        trade_key, source, source_path, line_no, ts_ms, slug, symbol, interval,
                        side, price, stake_usdc, strategy, raw_json, inserted_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade.trade_key,
                        trade.source,
                        trade.source_path,
                        trade.line_no,
                        trade.ts_ms,
                        trade.slug,
                        trade.symbol,
                        trade.interval,
                        trade.side,
                        trade.price,
                        trade.stake_usdc,
                        trade.strategy,
                        json.dumps(trade.raw, sort_keys=True),
                        stamp,
                    ),
                )
                inserted += int(cur.rowcount > 0)
    conn.commit()
    return inserted


def trade_from_event(payload: dict[str, Any], *, source: str, source_path: str, line_no: int) -> LearnedTrade | None:
    if payload.get("event") != "trade":
        return None
    if source == "daemon":
        return _daemon_trade(payload, source=source, source_path=source_path, line_no=line_no)
    if source == "funding":
        return _funding_trade(payload, source=source, source_path=source_path, line_no=line_no)
    return _generic_trade(payload, source=source, source_path=source_path, line_no=line_no)


def unresolved_trades(conn: sqlite3.Connection) -> list[LearnedTrade]:
    rows = conn.execute(
        """
        SELECT t.* FROM trades t
        LEFT JOIN resolutions r ON r.trade_key = t.trade_key
        WHERE r.trade_key IS NULL
        ORDER BY t.ts_ms ASC, t.trade_key ASC
        """
    ).fetchall()
    return [_trade_from_row(row) for row in rows]


def resolve_pending(
    conn: sqlite3.Connection,
    client: LimitlessClient,
    *,
    now_ms: int | None = None,
    max_markets: int = 200,
) -> int:
    resolved = 0
    stamp = int(now_ms if now_ms is not None else time.time() * 1000)
    cache: dict[str, ResolvedMarket] = {}
    for trade in unresolved_trades(conn)[:max_markets]:
        market = cache.get(trade.slug)
        if market is None:
            try:
                market = client.resolved_market(trade.slug)
            except Exception:
                continue
            cache[trade.slug] = market
        if not market.resolved:
            continue
        row = resolve_trade(trade, market, resolved_at_ms=stamp)
        conn.execute(
            """
            INSERT OR REPLACE INTO resolutions (
                trade_key, slug, winning_outcome_index, won, pnl_usdc, resolved_at_ms, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.trade.trade_key,
                row.trade.slug,
                int(row.market.winning_outcome_index),
                1 if row.won else 0,
                row.pnl_usdc,
                row.resolved_at_ms,
                json.dumps(row.market.raw or {}, sort_keys=True),
            ),
        )
        resolved += 1
    conn.commit()
    return resolved


def resolve_trade(trade: LearnedTrade, market: ResolvedMarket, *, resolved_at_ms: int) -> LearnedResolution:
    winning_side = "UP" if market.winning_outcome_index == 0 else "DOWN"
    won = trade.side == winning_side
    payout = trade.stake_usdc / trade.price if won else 0.0
    pnl = round(payout - trade.stake_usdc, 8)
    return LearnedResolution(trade=trade, market=market, won=won, pnl_usdc=pnl, resolved_at_ms=resolved_at_ms)


def build_report(conn: sqlite3.Connection, *, seed_reports: Iterable[str | Path] = ()) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            t.*,
            r.winning_outcome_index,
            r.won,
            r.pnl_usdc,
            r.resolved_at_ms,
            r.raw_json AS market_raw_json
        FROM trades t
        JOIN resolutions r ON r.trade_key = t.trade_key
        ORDER BY t.ts_ms ASC, t.trade_key ASC
        """
    ).fetchall()
    resolved_rows = _seed_resolved_rows(seed_reports) + [_resolution_from_row(row).to_report_row() for row in rows]
    unresolved_count = int(conn.execute(
        """
        SELECT COUNT(*) FROM trades t
        LEFT JOIN resolutions r ON r.trade_key = t.trade_key
        WHERE r.trade_key IS NULL
        """
    ).fetchone()[0])
    wins = sum(1 for row in resolved_rows if row["won"])
    losses = len(resolved_rows) - wins
    realized = round(sum(float(row["pnl_usdc"] or 0) for row in resolved_rows), 8)
    staked = round(sum(float(row["fill"]["stake_usdc"] or 0) for row in resolved_rows), 8)
    return {
        "fill_count": len(resolved_rows) + unresolved_count,
        "resolved_count": len(resolved_rows),
        "unresolved_count": unresolved_count,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(resolved_rows) if resolved_rows else None,
        "realized_pnl_usdc": realized,
        "resolved_stake_usdc": staked,
        "roi": realized / staked if staked else None,
        "slices": slice_summary(conn),
        "resolved": resolved_rows,
    }


def slice_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            t.interval,
            t.symbol,
            t.side,
            t.strategy,
            COUNT(*) AS n,
            SUM(r.won) AS wins,
            SUM(t.stake_usdc) AS stake_usdc,
            SUM(r.pnl_usdc) AS pnl_usdc,
            AVG(t.price) AS avg_price
        FROM trades t
        JOIN resolutions r ON r.trade_key = t.trade_key
        GROUP BY t.interval, t.symbol, t.side, t.strategy
        ORDER BY pnl_usdc DESC, n DESC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        n = int(row["n"] or 0)
        wins = int(row["wins"] or 0)
        stake = float(row["stake_usdc"] or 0)
        pnl = float(row["pnl_usdc"] or 0)
        out.append(
            {
                "interval": row["interval"],
                "symbol": row["symbol"],
                "side": row["side"],
                "strategy": row["strategy"],
                "n": n,
                "wins": wins,
                "losses": n - wins,
                "win_rate": wins / n if n else None,
                "stake_usdc": round(stake, 8),
                "pnl_usdc": round(pnl, 8),
                "roi": pnl / stake if stake else None,
                "avg_price": float(row["avg_price"] or 0),
            }
        )
    return out


def write_report_atomic(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _seed_resolved_rows(seed_reports: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in seed_reports:
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in payload.get("resolved") or []:
            fill = row.get("fill") or {}
            slug = str(fill.get("slug") or "")
            side = str(fill.get("side") or "")
            scanned = str(fill.get("scanned_at_ms") or "")
            key = str(row.get("trade_key") or f"seed:{slug}:{side}:{scanned}")
            if not slug or key in seen:
                continue
            seeded = dict(row)
            seeded["trade_key"] = key
            seeded["seed_report"] = str(path)
            rows.append(seeded)
            seen.add(key)
    return rows


def run_once(
    *,
    db_path: str | Path,
    logs: Iterable[str | Path],
    report_out: str | Path,
    client: LimitlessClient,
    seed_reports: Iterable[str | Path] = (),
    now_ms: int | None = None,
    max_markets: int = 200,
) -> dict[str, Any]:
    with connect(db_path) as conn:
        inserted = ingest_jsonl(conn, logs, now_ms=now_ms)
        resolved = resolve_pending(conn, client, now_ms=now_ms, max_markets=max_markets)
        report = build_report(conn, seed_reports=seed_reports)
        report["learner"] = {
            "inserted": inserted,
            "resolved_now": resolved,
            "updated_at_ms": int(now_ms if now_ms is not None else time.time() * 1000),
            "db_path": str(db_path),
            "logs": [str(path) for path in logs],
            "seed_reports": [str(path) for path in seed_reports],
        }
        write_report_atomic(report, report_out)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Learn from live Limitless fills and update slice score evidence")
    parser.add_argument("--db", default="tmp/limitless_hl/learner.sqlite3")
    parser.add_argument("--log", action="append", default=[], help="Trade JSONL path; can be passed more than once")
    parser.add_argument("--report-out", default="tmp/limitless_hl/evaluation_report_live.json")
    parser.add_argument("--seed-report", action="append", default=[], help="Existing evaluation report to merge into output")
    parser.add_argument("--loop-seconds", type=int, default=60)
    parser.add_argument("--iterations", type=int, default=0)
    parser.add_argument("--max-markets", type=int, default=200)
    args = parser.parse_args()

    logs = args.log or [
        "tmp/limitless_hl/daemon_trades.jsonl",
        "tmp/limitless_hl/funding_trades.jsonl",
    ]

    running = True

    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    client = LimitlessClient()
    iteration = 0
    while running:
        iteration += 1
        try:
            report = run_once(
                db_path=args.db,
                logs=logs,
                report_out=args.report_out,
                client=client,
                seed_reports=args.seed_report,
                max_markets=args.max_markets,
            )
            print(json.dumps({
                "event": "learner_update",
                "fill_count": report["fill_count"],
                "resolved_count": report["resolved_count"],
                "unresolved_count": report["unresolved_count"],
                "roi": report["roi"],
            }, sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"event": "learner_error", "error": str(exc)}, sort_keys=True), flush=True)

        if args.iterations and iteration >= args.iterations:
            break
        if running:
            time.sleep(max(args.loop_seconds, 1))


def _daemon_trade(payload: dict[str, Any], *, source: str, source_path: str, line_no: int) -> LearnedTrade | None:
    state = str(payload.get("state") or "")
    result = payload.get("limitless_result") or {}
    filled = _to_float(result.get("filled_usdc"))
    matched = bool(result.get("matched")) or filled > 0
    if not matched or filled <= 0 or state not in {"limitless_filled_unhedged", "hedged", "hedge_failed"}:
        return None
    candidate = payload.get("candidate") or {}
    slug = str(candidate.get("slug") or payload.get("slug") or "")
    side = str(candidate.get("side") or "").upper()
    price = _to_float(candidate.get("limit_price") or candidate.get("price"))
    strategy = "shadow_daemon" if payload.get("mode") == "dry_run" else "scored_daemon"
    return _make_trade(
        payload,
        source=source,
        source_path=source_path,
        line_no=line_no,
        slug=slug,
        symbol=str(candidate.get("symbol") or ""),
        interval=str(candidate.get("interval") or ""),
        side=side,
        price=price,
        stake=filled,
        strategy=strategy,
    )


def _funding_trade(payload: dict[str, Any], *, source: str, source_path: str, line_no: int) -> LearnedTrade | None:
    filled = _to_float(payload.get("filled_usdc"))
    matched = str(payload.get("state") or "") == "filled" or filled > 0
    if not matched or filled <= 0:
        return None
    strategy = "shadow_funding" if payload.get("mode") == "dry_run" else "funding_kelly"
    return _make_trade(
        payload,
        source=source,
        source_path=source_path,
        line_no=line_no,
        slug=str(payload.get("slug") or ""),
        symbol=str(payload.get("coin") or ""),
        interval="15m",
        side=str(payload.get("direction") or "").upper(),
        price=_to_float(payload.get("entry_price")),
        stake=filled,
        strategy=strategy,
    )


def _generic_trade(payload: dict[str, Any], *, source: str, source_path: str, line_no: int) -> LearnedTrade | None:
    filled = _to_float(payload.get("filled_usdc"))
    if filled <= 0:
        return None
    return _make_trade(
        payload,
        source=source,
        source_path=source_path,
        line_no=line_no,
        slug=str(payload.get("slug") or ""),
        symbol=str(payload.get("symbol") or payload.get("coin") or ""),
        interval=str(payload.get("interval") or ""),
        side=str(payload.get("side") or payload.get("direction") or "").upper(),
        price=_to_float(payload.get("price") or payload.get("entry_price") or payload.get("limit_price")),
        stake=filled,
        strategy=source,
    )


def _make_trade(
    payload: dict[str, Any],
    *,
    source: str,
    source_path: str,
    line_no: int,
    slug: str,
    symbol: str,
    interval: str,
    side: str,
    price: float,
    stake: float,
    strategy: str,
) -> LearnedTrade | None:
    symbol = symbol.upper()
    interval = interval.lower()
    if not slug or not symbol or not interval or side not in {"UP", "DOWN"}:
        return None
    if price <= 0 or price >= 1 or stake <= 0:
        return None
    ts_ms = int(_to_float(payload.get("ts_ms")) or 0)
    if ts_ms <= 0:
        return None
    key = f"{source}:{slug}:{side}:{ts_ms}:{line_no}"
    return LearnedTrade(
        trade_key=key,
        source=source,
        source_path=source_path,
        line_no=line_no,
        ts_ms=ts_ms,
        slug=slug,
        symbol=symbol,
        interval=interval,
        side=side,
        price=price,
        stake_usdc=stake,
        strategy=strategy,
        raw=dict(payload),
    )


def _trade_from_row(row: sqlite3.Row) -> LearnedTrade:
    return LearnedTrade(
        trade_key=str(row["trade_key"]),
        source=str(row["source"]),
        source_path=str(row["source_path"]),
        line_no=int(row["line_no"]),
        ts_ms=int(row["ts_ms"]),
        slug=str(row["slug"]),
        symbol=str(row["symbol"]),
        interval=str(row["interval"]),
        side=str(row["side"]),
        price=float(row["price"]),
        stake_usdc=float(row["stake_usdc"]),
        strategy=str(row["strategy"]),
        raw=json.loads(row["raw_json"]),
    )


def _resolution_from_row(row: sqlite3.Row) -> LearnedResolution:
    trade = _trade_from_row(row)
    market = ResolvedMarket(
        slug=str(row["slug"]),
        winning_outcome_index=int(row["winning_outcome_index"]),
        raw=json.loads(row["market_raw_json"] or "{}"),
    )
    return LearnedResolution(
        trade=trade,
        market=market,
        won=bool(row["won"]),
        pnl_usdc=float(row["pnl_usdc"]),
        resolved_at_ms=int(row["resolved_at_ms"]),
    )


def _source_name(path: Path) -> str:
    name = path.name
    if name == "daemon_trades.jsonl":
        return "daemon"
    if name in {"funding_trades.jsonl", "funding_dry.jsonl"}:
        return "funding"
    if name == "daemon_shadow.jsonl":
        return "daemon"
    return path.stem.replace("_trades", "")


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    main()
