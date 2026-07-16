# Limitless HL Bot

A small-account Limitless directional trading system with Hyperliquid-derived scoring, PM2 process management, auto-claiming, and a learner loop that resolves fills into a SQLite ledger.

## Documentation

The source-linked maintainer reference is published at
[alpha.samlogic.org](https://alpha.samlogic.org/).
It is generated with Sourcey from this repository at a pinned commit.

## Current Production Posture

- `limitless-hl-live`: live scored Limitless-only daemon.
- `limitless-hl-learner`: resolves filled trades and writes `tmp/limitless_hl/evaluation_report_live.json`.
- `limitless-hl-claimer`: claims resolved Limitless positions.
- `limitless-hl-funding-dry`: funding strategy stays dry-run until live evidence improves.
- `limitless-hl-funding-live`: intentionally not in the default PM2 config because live funding is negative in the current learner sample.

The daemon hot-reloads the learner report every loop. Seeded historical slices can be demoted when live ROI degrades.

## Local Setup

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .
pytest tests/limitless_hl -q
```

Create `.env` from `.env.example` and fill secrets locally. Do not commit `.env` or runtime files under `tmp/`.

## PM2

```bash
pm2 start ecosystem.config.cjs --only limitless-hl-learner
pm2 start ecosystem.config.cjs --only limitless-hl-live
pm2 start ecosystem.config.cjs --only limitless-hl-claimer
pm2 save
```

Funding live should stay stopped unless explicitly re-enabled after the learner shows positive live edge.
