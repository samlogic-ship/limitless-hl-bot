from __future__ import annotations

import pytest

from limitless_hl.execution import ExecutionConfig, ExecutionRouter, LiveTradingBlocked


def test_execution_router_records_dry_run_order() -> None:
    router = ExecutionRouter(ExecutionConfig(mode="dry_run"))
    result = router.submit({"slug": "btc", "side": "UP", "stake_usdc": 10})

    assert result["submitted"] is False
    assert result["mode"] == "dry_run"
    assert result["candidate"]["slug"] == "btc"


def test_execution_router_refuses_live_without_arm_flag() -> None:
    router = ExecutionRouter(ExecutionConfig(mode="live", live_armed=False))

    with pytest.raises(LiveTradingBlocked):
        router.submit({"slug": "btc", "side": "UP", "stake_usdc": 10})
