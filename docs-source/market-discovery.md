# Market Discovery and Venue Clients

## Public surfaces

| API | Source | Role |
| --- | --- | --- |
| `LimitlessClient.active_markets` | [`clients.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/clients.py#L20) | Fetch and normalize active Limitless markets. |
| `LimitlessClient.orderbook` | [`clients.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/clients.py#L20) | Read venue liquidity for a market. |
| `HyperliquidClient.mid_prices` | [`clients.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/clients.py#L125) | Read reference mids from Hyperliquid. |
| `LimitlessHyperliquidScanner.scan` | [`scanner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/scanner.py#L35) | Join venue markets with reference prices. |
| `pm_slug` | [`polymarket_feed.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/polymarket_feed.py#L38) | Construct compatible Polymarket slugs. |
| `PolymarketFeed` | [`polymarket_feed.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/polymarket_feed.py#L56) | Supply cross-venue comparison data. |
| `FlowRecorder` | [`flow_recorder.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/flow_recorder.py#L81) | Persist market and wallet flow observations. |

## Data boundary

Discovery is read-only. It produces candidates and observations; order signing
is delegated to the execution layer. Cached HTTP reads live in
[`cached_get_json`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/http_cache.py#L41),
which keeps network failures from silently becoming trading approval.
