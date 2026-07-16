# Operations and Learning

| API | Source | Role |
| --- | --- | --- |
| `ingest_jsonl` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/learner.py#L113) | Ingest append-only trade events. |
| `resolve_pending` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/learner.py#L183) | Resolve trades against final market outcomes. |
| `build_report` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/learner.py#L249) | Build the performance report consumed by gates. |
| `slice_summary` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/learner.py#L307) | Summarize strategy/interval/symbol slices. |
| `write_report_atomic` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/learner.py#L351) | Publish reports without partial files. |
| `run_once` | [`learner.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/learner.py#L392) | Execute one ingest/resolve/report cycle. |
| `summarize_jsonl` | [`report.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/report.py#L12) | Produce a compact operational summary. |
| `get_secret` | [`secrets.py`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/limitless_hl/secrets.py#L7) | Resolve secrets without committing them. |

PM2 process declarations live in
[`ecosystem.config.cjs`](https://github.com/samlogic-ship/limitless-hl-bot/blob/d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db/ecosystem.config.cjs).
The default posture keeps the funding-live process disabled until the learner
has positive live evidence.
