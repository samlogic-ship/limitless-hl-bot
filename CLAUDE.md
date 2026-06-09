# Limitless HL Bot — Codex/Claude Instructions

## Deploy workflow

Live bot runs on **VPS** at `/opt/limitless-hl-bot`. The Mac copy is a dev workspace only.

### Push a change to VPS

```bash
# 1. Get the samlogic-ship token from Mac keychain
SHIP_TOKEN=$(git credential-osxkeychain get <<< $'protocol=https\nhost=github.com\n' 2>/dev/null | grep password | cut -d= -f2)
# Fallback if above is empty:
# SHIP_TOKEN=$(security dump-keychain 2>/dev/null | grep -A4 '"srvr".*github.com' | grep password | head -1 | grep -o '".*"' | tr -d '"')

# 2. Push to GitHub
git -c user.email=samuelakpanzoe@gmail.com push \
    "https://samlogic-ship:${SHIP_TOKEN}@github.com/samlogic-ship/limitless-hl-bot.git" main

# 3. Pull and reload on VPS
ssh vps "cd /opt/limitless-hl-bot && \
    git pull https://samlogic-ship:${SHIP_TOKEN}@github.com/samlogic-ship/limitless-hl-bot.git main && \
    pm2 reload ecosystem.config.cjs"
```

### VPS at a glance
- SSH: `ssh vps` (Tailnet, key `~/.ssh/hetzner_vps`)
- Repo: `/opt/limitless-hl-bot`
- Secrets: `/opt/limitless-hl-bot/.env` — never committed, loaded by ecosystem at launch
- Logs: `ssh vps "pm2 logs limitless-hl-funding-live --lines 30 --nostream"`
- Status: `ssh vps "pm2 list"`

### Commit hygiene (public repo)
- Author email must be `samuelakpanzoe@gmail.com` — no Mac hostname leaks
- No `Co-Authored-By:` trailers in commit messages
- Never commit `.env`, `tmp/`, `*.sqlite3`, `*.jsonl`

---

## Architecture

```
funding_daemon.py   — Strategy 3: HL funding rate → Limitless 15-min DOWN/UP bets
daemon.py           — Strategy 1: scored slice bets (learner-driven)
learner.py          — resolves fills from SQLite, writes evaluation_report_live.json
claimer.py          — redeems resolved Limitless positions on Base
cli.py              — overnight dry-run scanner
tgbot.py            — Telegram control interface
secrets.py          — reads env vars first, falls back to macOS keychain
```

Runtime data lives in `tmp/limitless_hl/` (gitignored):
- `learner.sqlite3` — resolved trade ledger
- `evaluation_report_live.json` — learner-promoted slice scores
- `funding_trades.jsonl` — funding strategy live fills
- `daemon_trades.jsonl` — scored daemon live fills

---

## Hard rules — do not violate these

### Risk caps stay in, period
`FundingProofConfig.max_live_stake_usdc`, `RiskConfig.max_daily_loss_usdc`,
`RiskConfig.max_stake_usdc`, and the daemon's `--max-daily-loss-usdc` /
`--stake-usdc` args are LOAD-BEARING. Never remove or raise them because a
strategy "looks profitable" — see tmp/study/: the previous +$170 headline was
seeded backtest data; real live PnL at the time was negative. Kelly sizing is
always applied UNDER these caps, never instead of them.

### funding-live stays OFF until proven
`limitless-hl-funding-live` may only be started after the learner shows
positive LIVE-ONLY ROI (report key `realized_pnl_usdc`, not `combined`) on
100+ resolved funding fills. As of 2026-06-09 live funding is 6/22 wins,
-$34.91. "Losses are normal variance" is not an argument — evidence is.

### No pause file for the funding daemon
Never create `tmp/limitless_hl/funding_daemon.pause`; it silently no-ops the
process. Use `pm2 stop` so the state is visible.

### Headline PnL is live-only
`evaluation_report_live.json` headline keys exclude seeded rows; seeds appear
only under `seeded`/`combined`. Never "fix" the report to merge them back.

### Fees are modeled exactly
Taker buy fee is 3.00% flat at any price <= $0.50 (declining above) and is
priced inside `model.taker_buy_fee_rate`. Never replace it with a flat buffer
again, and never assume the old 1.5% number.

## Secrets map

| Secret | How loaded |
|---|---|
| Maker private key | `LIMITLESS_PRIVATE_KEY` env var (from `.env`) |
| Limitless token ID | `LIMITLESS_TOKEN_ID` env var |
| Limitless token secret | `LIMITLESS_TOKEN_SECRET` env var |
| TG bot token | `LIMITLESS_HL_TG_TOKEN` env var |
| TG chat ID | `LIMITLESS_HL_TG_CHAT` env var |
| Owner ID | `LIMITLESS_OWNER_ID=1379275` |
| Maker address | `LIMITLESS_MAKER_ADDRESS=0xBA240843fdf7EF02fb89832D84325B1488e0646f` |

On Mac dev: secrets fall back to macOS keychain via `secrets.py` if env vars are absent.
On VPS: `.env` provides all of the above.

---

## PM2 cheatsheet (run on VPS)

```bash
pm2 list                                    # all process status
pm2 logs limitless-hl-funding-live --lines 30 --nostream
pm2 reload ecosystem.config.cjs             # reload all after code change
pm2 restart limitless-hl-funding-live       # restart one process
pm2 save                                    # persist process list across reboots
```
