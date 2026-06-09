from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_daily_loss_usdc: float = 50.0
    max_open_markets: int = 10
    max_stake_usdc: float = 25.0


@dataclass(frozen=True, slots=True)
class RiskLedger:
    realized_pnl_usdc: float = 0.0
    open_slugs: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config

    def can_open(self, candidate: dict[str, Any], ledger: RiskLedger) -> RiskDecision:
        slug = str(candidate.get("slug") or "")
        stake = float(candidate.get("stake_usdc") or 0)
        if slug in ledger.open_slugs:
            return RiskDecision(False, "duplicate_market")
        if ledger.realized_pnl_usdc <= -abs(self.config.max_daily_loss_usdc):
            return RiskDecision(False, "daily_loss_limit")
        if len(ledger.open_slugs) >= self.config.max_open_markets:
            return RiskDecision(False, "open_market_limit")
        if stake <= 0:
            return RiskDecision(False, "invalid_stake")
        if stake > self.config.max_stake_usdc:
            return RiskDecision(False, "stake_limit")
        return RiskDecision(True, "allowed")
