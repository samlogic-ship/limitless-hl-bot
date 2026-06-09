const ROOT = __dirname;
const PYTHON = `${ROOT}/.venv/bin/python`;
const LOAD_ENV = "set -a; [ -f .env ] && . ./.env; set +a";
const LIMITLESS_ENV = [
  "LIMITLESS_OWNER_ID=\${LIMITLESS_OWNER_ID}",
  "LIMITLESS_MAKER_ADDRESS=\${LIMITLESS_MAKER_ADDRESS}",
  "LIMITLESS_FEE_RATE_BPS=\${LIMITLESS_FEE_RATE_BPS:-300}",
  "LIMITLESS_SIGNATURE_TYPE=\${LIMITLESS_SIGNATURE_TYPE:-0}",
  "LIMITLESS_FUNDING_ALLOW_UNHEDGED=\${LIMITLESS_FUNDING_ALLOW_UNHEDGED:-1}",
].join(" ");

module.exports = {
  apps: [
    {
      name: "limitless-hl-live",
      cwd: ROOT,
      script: "bash",
      args: [
        "--noprofile", "--norc", "-c",
        `${LOAD_ENV}; ${LIMITLESS_ENV} "${PYTHON}" -m limitless_hl.daemon --live-armed --allow-unhedged-live --intervals 1m,5m,15m,1h,1d,1w --sides UP,DOWN --slice-score-file tmp/limitless_hl/evaluation_report_live.json --slice-min-n 3 --slice-min-roi 0.02 --slice-min-win-rate 0.25 --slice-live-min-n 4 --slice-live-min-roi 0.0 --shadow-graduate --shadow-min-n 20 --shadow-min-roi 0.10 --shadow-min-win-rate 0.52 --scream-promote --scream-min-edge 0.08 --scream-intervals 5m,15m --scoring-live --score-min 1.0 --score-base-stake-usdc 1 --score-max-stake-usdc 3 --hl-bot-status-file /opt/hyperliquid-bot/hl_bot_status.json --min-edge 0.03 --max-price 0.88 --min-seconds-to-expiry 60 --stake-usdc 1 --max-daily-loss-usdc 10 --loop-seconds 15 --jsonl-out tmp/limitless_hl/daemon_trades.jsonl`,
      ],
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 20,
      time: true,
      vizion: false,
    },
    {
      name: "limitless-hl-learner",
      cwd: ROOT,
      script: "bash",
      args: [
        "--noprofile", "--norc", "-c",
        `"${PYTHON}" -m limitless_hl.learner --db tmp/limitless_hl/learner.sqlite3 --log tmp/limitless_hl/daemon_trades.jsonl --log tmp/limitless_hl/daemon_shadow.jsonl --log tmp/limitless_hl/funding_trades.jsonl --log tmp/limitless_hl/funding_dry.jsonl --seed-report data/evaluation_report_strict.json --report-out tmp/limitless_hl/evaluation_report_live.json --loop-seconds 45`,
      ],
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 20,
      time: true,
      vizion: false,
    },
    {
      name: "limitless-hl-claimer",
      cwd: ROOT,
      script: "bash",
      args: [
        "--noprofile", "--norc", "-c",
        `${LOAD_ENV}; LIMITLESS_MAKER_ADDRESS=\${LIMITLESS_MAKER_ADDRESS} "${PYTHON}" -m limitless_hl.claimer --live --log-dir tmp/limitless_hl --loop-seconds 60 --jsonl-out tmp/limitless_hl/claims.jsonl`,
      ],
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 20,
      time: true,
      vizion: false,
    },
    {
      name: "limitless-hl-funding-dry",
      cwd: ROOT,
      script: "bash",
      args: [
        "--noprofile", "--norc", "-c",
        `${LOAD_ENV}; ${LIMITLESS_ENV} "${PYTHON}" -m limitless_hl.funding_daemon --min-stake-usdc 1 --first-spike-only --loop-seconds 20 --min-seconds-to-expiry 120 --jsonl-out tmp/limitless_hl/funding_dry.jsonl`,
      ],
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 20,
      time: true,
      vizion: false,
    },
    {
      name: "limitless-hl-shadow",
      cwd: ROOT,
      script: "bash",
      args: [
        "--noprofile", "--norc", "-c",
        `${LOAD_ENV}; ${LIMITLESS_ENV} "${PYTHON}" -m limitless_hl.daemon --intervals 1m,5m,15m,1h,1d,1w --sides UP,DOWN --scoring-live --slice-score-file tmp/limitless_hl/evaluation_report_live.json --slice-min-n 0 --slice-min-roi -1.0 --slice-min-win-rate 0.0 --score-min 0.0 --score-base-stake-usdc 1 --score-max-stake-usdc 1 --hl-bot-status-file /opt/hyperliquid-bot/hl_bot_status.json --min-edge 0.0 --max-price 0.95 --min-seconds-to-expiry 45 --stake-usdc 1 --max-daily-loss-usdc 999 --max-open-markets 100 --loop-seconds 15 --jsonl-out tmp/limitless_hl/daemon_shadow.jsonl`,
      ],
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 50,
      time: true,
      vizion: false,
    },
    {
      name: "limitless-hl-tg",
      cwd: ROOT,
      script: "bash",
      args: [
        "--noprofile", "--norc", "-c",
        `${LOAD_ENV}; "${PYTHON}" -m limitless_hl.tgbot`,
      ],
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 20,
      time: true,
      vizion: false,
    },
  ],
};
