# Execution and Orders

## Intent construction

| API | Source | Role |
| --- | --- | --- |
| `LimitlessCredentials` | [`live_trade.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/live_trade.py#L24) | Typed credential container. |
| `LimitlessOrderIntent` | [`live_trade.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/live_trade.py#L30) | Explicit outcome, side, price, and size intent. |
| `LimitlessOrderBuilder` | [`live_trade.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/live_trade.py#L43) | Build venue order payloads. |
| `LimitlessSubmitter` | [`live_trade.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/live_trade.py#L106) | Submit signed intents to Limitless. |
| `PairTradeRunner` | [`live_trade.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/live_trade.py#L171) | Coordinate the Limitless leg and optional hedge. |
| `candidate_to_limitless_intent` | [`live_trade.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/live_trade.py#L219) | Translate a scored candidate into a typed intent. |
| `sign_hmac_headers` | [`live_trade.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/live_trade.py#L265) | Create request authentication headers. |
| `ExecutionRouter` | [`execution.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/execution.py#L17) | Enforce paper/live routing policy. |

The order layer keeps construction separate from submission so tests can prove
payload shape without broadcasting. Live routing raises
[`LiveTradingBlocked`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/execution.py#L7)
when the configured boundary does not authorize a live path.
