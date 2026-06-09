from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ResolvedMarket:
    slug: str
    winning_outcome_index: int | None
    raw: dict[str, Any] | None = None

    @property
    def resolved(self) -> bool:
        return self.winning_outcome_index in {0, 1}


@dataclass(frozen=True, slots=True)
class PaperFill:
    slug: str
    symbol: str
    side: str
    price: float
    stake_usdc: float
    scanned_at_ms: int
    raw: dict[str, Any]

    @property
    def shares(self) -> float:
        return self.stake_usdc / self.price if self.price > 0 else 0.0


@dataclass(frozen=True, slots=True)
class FillResolution:
    fill: PaperFill
    resolved_market: ResolvedMarket
    won: bool
    pnl_usdc: float

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["fill"] = asdict(self.fill)
        payload["resolved_market"] = asdict(self.resolved_market)
        return payload


def load_scan_candidates(path: str | Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        scanned_at_ms = int(payload.get("scanned_at_ms") or 0)
        for candidate in payload.get("candidates") or []:
            row = dict(candidate)
            row["scanned_at_ms"] = scanned_at_ms
            candidates.append(row)
    return candidates


def build_paper_fills(candidates: list[dict[str, Any]]) -> list[PaperFill]:
    fills: list[PaperFill] = []
    seen: set[tuple[str, str]] = set()
    for candidate in sorted(candidates, key=lambda row: int(row.get("scanned_at_ms") or 0)):
        slug = str(candidate.get("slug") or "")
        side = str(candidate.get("side") or "")
        key = (slug, side)
        if not slug or side not in {"UP", "DOWN"} or key in seen:
            continue
        price = float(candidate.get("limit_price") or 0)
        stake = float(candidate.get("stake_usdc") or 0)
        if price <= 0 or stake <= 0:
            continue
        seen.add(key)
        fills.append(
            PaperFill(
                slug=slug,
                symbol=str(candidate.get("symbol") or ""),
                side=side,
                price=price,
                stake_usdc=stake,
                scanned_at_ms=int(candidate.get("scanned_at_ms") or 0),
                raw=dict(candidate),
            )
        )
    return fills


def resolve_candidate(candidate: dict[str, Any], resolved_market: ResolvedMarket) -> FillResolution:
    fill = PaperFill(
        slug=str(candidate.get("slug") or resolved_market.slug),
        symbol=str(candidate.get("symbol") or ""),
        side=str(candidate.get("side") or ""),
        price=float(candidate.get("limit_price") or candidate.get("price") or 0),
        stake_usdc=float(candidate.get("stake_usdc") or 0),
        scanned_at_ms=int(candidate.get("scanned_at_ms") or 0),
        raw=dict(candidate),
    )
    return resolve_fill(fill, resolved_market)


def resolve_fill(fill: PaperFill, resolved_market: ResolvedMarket) -> FillResolution:
    winning_side = "UP" if resolved_market.winning_outcome_index == 0 else "DOWN"
    won = fill.side == winning_side
    payout = fill.shares if won else 0.0
    pnl = round(payout - fill.stake_usdc, 8)
    return FillResolution(fill=fill, resolved_market=resolved_market, won=won, pnl_usdc=pnl)


def evaluate_fills(fills: list[PaperFill], resolved: dict[str, ResolvedMarket]) -> dict[str, Any]:
    resolved_rows: list[FillResolution] = []
    unresolved: list[PaperFill] = []
    for fill in fills:
        market = resolved.get(fill.slug)
        if market is None or not market.resolved:
            unresolved.append(fill)
            continue
        resolved_rows.append(resolve_fill(fill, market))
    wins = sum(1 for row in resolved_rows if row.won)
    losses = sum(1 for row in resolved_rows if not row.won)
    realized = round(sum(row.pnl_usdc for row in resolved_rows), 8)
    staked = round(sum(row.fill.stake_usdc for row in resolved_rows), 8)
    return {
        "fill_count": len(fills),
        "resolved_count": len(resolved_rows),
        "unresolved_count": len(unresolved),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(resolved_rows) if resolved_rows else None,
        "realized_pnl_usdc": realized,
        "resolved_stake_usdc": staked,
        "roi": realized / staked if staked else None,
        "resolved": [row.to_dict() for row in resolved_rows],
        "unresolved": [asdict(fill) for fill in unresolved],
    }
