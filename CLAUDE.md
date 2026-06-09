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

### FundingProofConfig must NOT have `max_live_stake_usdc`
Kelly sizing is uncapped by design. This field has been removed on purpose and must never be re-added:

```python
# CORRECT
@dataclass(frozen=True)
class FundingProofConfig:
    min_backtest_wr: float = 0.56
    min_backtest_n: int = 1_000
    min_ev_pct: float = 0.10

# WRONG — do not add this back
#   max_live_stake_usdc: float = 5.0
```

The `passes_live_funding_proof` function must not check `config.max_live_stake_usdc`. Kelly fraction (`--kelly-fraction 0.25`) already limits risk.

### No pause file
Never create `tmp/limitless_hl/funding_daemon.pause`. It silently kills the live process by looping without trading. If you need to disable trading, use `pm2 stop`.

### ecosystem.config.cjs — keep `limitless-hl-funding-live`
This entry must always be present. Do not remove it because of observed losses — losses are normal variance in a positive-EV strategy.

### Do not add `--stake-usdc` or `--max-daily-loss-usdc` to funding_daemon args
The funding daemon uses Kelly sizing (`--kelly-fraction`, `--min-stake-usdc`). Fixed stake args were removed intentionally.

---

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
