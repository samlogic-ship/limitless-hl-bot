# Risk, Quotes, and Exits

| API | Source | Role |
| --- | --- | --- |
| `MakerConfig` | [`maker.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/maker.py#L64) | Maker policy and quote bounds. |
| `compute_quotes` | [`maker.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/maker.py#L117) | Calculate bounded bid/ask plans. |
| `diff_orders` | [`maker.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/maker.py#L164) | Compare desired and existing orders. |
| `locked_usdc` | [`maker.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/maker.py#L200) | Account for collateral locked in open quotes. |
| `ExitConfig` | [`exiter.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/exiter.py#L63) | Profit, loss, and timing exit limits. |
| `decide_exit` | [`exiter.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/exiter.py#L79) | Return an explicit hold/exit decision. |
| `ExitEngine` | [`exiter.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/exiter.py#L100) | Inspect positions and execute approved exits. |
| `evaluate_gates` | [`gatekeeper.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/gatekeeper.py#L200) | Evaluate learner and live-PnL gates. |

Risk is layered: quote sizing, collateral accounting, live-performance gates,
loss cooldowns, and an exit engine each have independent evidence and tests.
