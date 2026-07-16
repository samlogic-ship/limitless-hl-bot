# Limitless HL Bot Maintainer Reference

This reference maps the trading system back to commit
[`d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db`](https://github.com/samlogic-ship/limitless-hl-bot/tree/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db).
It is for maintainers auditing how markets are discovered, scored, executed,
exited, and learned from.

## Target

- Repository: [samlogic-ship/limitless-hl-bot](https://github.com/samlogic-ship/limitless-hl-bot)
- License: MIT
- Runtime: Python 3.12
- Package: [`limitless_hl`](https://github.com/samlogic-ship/limitless-hl-bot/tree/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl)

## Architecture

The system separates venue clients, market scanning, feature scoring, live
execution, exit policy, operations, and post-trade learning. The primary
maintainer entry points are:

- [`LimitlessClient`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/clients.py#L20)
- [`LimitlessHyperliquidScanner`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/scanner.py#L35)
- [`score_candidate`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/scorer.py#L95)
- [`PairTradeRunner`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/live_trade.py#L171)
- [`ExitEngine`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/exiter.py#L100)
- [`run_once`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/learner.py#L392)

The repository contains more than 30 implementation modules and a dedicated
test suite. The following pages cover more than 20 source-backed APIs and
operational concepts.
