from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


class LiveTradingBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    mode: Literal["dry_run", "live"] = "dry_run"
    live_armed: bool = False


class ExecutionRouter:
    def __init__(self, config: ExecutionConfig):
        self.config = config

    def submit(self, candidate: dict[str, Any]) -> dict[str, Any]:
        if self.config.mode == "dry_run":
            return {
                "submitted": False,
                "mode": "dry_run",
                "candidate": dict(candidate),
                "reason": "dry_run_only",
            }
        if not self.config.live_armed:
            raise LiveTradingBlocked("live trading requested but live_armed is false")
        raise LiveTradingBlocked("live Limitless order signing and Hyperliquid hedging are not implemented")
