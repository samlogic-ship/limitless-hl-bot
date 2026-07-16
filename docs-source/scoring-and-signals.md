# Scoring and Signals

## Feature model

| API | Source | Role |
| --- | --- | --- |
| `MarketFeatures` | [`scorer.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/scorer.py#L22) | Normalized market feature packet. |
| `ScoringConfig` | [`scorer.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/scorer.py#L47) | Threshold and stake configuration. |
| `ScoreResult` | [`scorer.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/scorer.py#L62) | Auditable score, reason, and stake output. |
| `LiveFeatureProvider` | [`scorer.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/scorer.py#L71) | Fetch live mids, funding, momentum, and flow. |
| `score_candidate` | [`scorer.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/scorer.py#L95) | Convert features into an explainable trade score. |
| `load_hl_bot_context` | [`scorer.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/scorer.py#L188) | Load bounded, freshness-checked bot context. |
| `fit_symbol` | [`calibrator.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/calibrator.py#L68) | Fit symbol calibration from historical candles. |
| `signal_key` | [`funding_daemon.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/funding_daemon.py#L60) | Produce deterministic funding-signal identity. |
| `first_spike_decision` | [`funding_daemon.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/funding_daemon.py#L64) | Gate first-spike funding decisions. |
| `kelly_stake` | [`funding_daemon.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/funding_daemon.py#L128) | Bound stake from estimated edge and bankroll. |

Scores are evidence, not execution authority. The daemon applies strategy,
price-band, cooldown, and learner gates before any candidate reaches an order
builder.
