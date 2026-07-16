# Operations and Learning

| API | Source | Role |
| --- | --- | --- |
| `ingest_jsonl` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/learner.py#L113) | Ingest append-only trade events. |
| `resolve_pending` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/learner.py#L183) | Resolve trades against final market outcomes. |
| `build_report` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/learner.py#L249) | Build the performance report consumed by gates. |
| `slice_summary` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/learner.py#L307) | Summarize strategy/interval/symbol slices. |
| `write_report_atomic` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/learner.py#L351) | Publish reports without partial files. |
| `run_once` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/learner.py#L392) | Execute one ingest/resolve/report cycle. |
| `summarize_jsonl` | [`report.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/report.py#L12) | Produce a compact operational summary. |
| `get_secret` | [`secrets.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/limitless_hl/secrets.py#L7) | Resolve secrets without committing them. |

PM2 process declarations live in
[`ecosystem.config.cjs`](https://github.com/samlogic-ship/limitless-hl-bot/blob/5c569c592082a823f22850722ec3f88d0fb2dc3a/ecosystem.config.cjs).
The default posture keeps the funding-live process disabled until the learner
has positive live evidence.
