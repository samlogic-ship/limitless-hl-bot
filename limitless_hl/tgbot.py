"""Telegram control bot for Limitless <> Hyperliquid arb daemon.

Patterns (modeled on memerunner tgbot.py):
- stdlib urllib only — zero third-party deps
- Long-polling getUpdates
- One authorized chat_id; all others silently ignored
- Anti-spam: per-event cooldowns + dedup
- Inline keyboard re-attached to every reply
- Background thread tails daemon_trades.jsonl for push notifications

Setup:
  cp creds/limitless-hl-telegram.env.example creds/limitless-hl-telegram.env
  # fill LIMITLESS_HL_TG_TOKEN and LIMITLESS_HL_TG_CHAT
  pm2 start limitless-hl-tg
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent   # .../New project
CREDS_FILE = ROOT / "creds" / "limitless-hl-telegram.env"
JSONL_PATH = ROOT / "tmp" / "limitless_hl" / "daemon_trades.jsonl"
PAUSE_FILE = ROOT / "tmp" / "limitless_hl" / "daemon.pause"

HEDGE_WALLET  = "0x8187478d66f3B18FE774FbD500F04c34B3015E3D"
EOA_ADDRESS   = "0xBA240843fdf7EF02fb89832D84325B1488e0646f"
USDC_BASE     = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # USDC on Base
HL_INFO_URL   = "https://api.hyperliquid.xyz/info"
BASE_RPC      = "https://mainnet.base.org"
DAEMON_NAME   = "limitless-hl-live"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _load_creds() -> tuple[str, str]:
    if CREDS_FILE.exists():
        for raw in CREDS_FILE.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    token = os.environ.get("LIMITLESS_HL_TG_TOKEN", "")
    chat  = os.environ.get("LIMITLESS_HL_TG_CHAT", "")
    if not token or not chat:
        raise RuntimeError(
            f"Missing LIMITLESS_HL_TG_TOKEN / LIMITLESS_HL_TG_CHAT\n"
            f"Set them in {CREDS_FILE}"
        )
    return token, chat


TOKEN, ALLOWED_CHAT = _load_creds()
TG_BASE = f"https://api.telegram.org/bot{TOKEN}"


# ---------------------------------------------------------------------------
# Anti-spam gateway
# ---------------------------------------------------------------------------

_last_sent: dict[str, float] = {}
_COOLDOWNS: dict[str, float] = {
    "trade":  5.0,
    "alert":  300.0,
    "cmd":    1.0,
    "startup": 30.0,
}


def _spam_ok(key: str, kind: str = "cmd") -> bool:
    now = time.time()
    gap = _COOLDOWNS.get(kind, 1.0)
    if now - _last_sent.get(key, 0.0) < gap:
        return False
    _last_sent[key] = now
    return True


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def _tg(method: str, **params: Any) -> dict[str, Any]:
    url  = f"{TG_BASE}/{method}"
    body = json.dumps(params).encode()
    req  = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read())
        except Exception:
            err = {"code": exc.code}
        return {"ok": False, "error": err}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# Keyboards
_MAIN_KB: dict[str, Any] = {
    "inline_keyboard": [
        [
            {"text": "📊 Status",  "callback_data": "/status"},
            {"text": "💹 Trades",  "callback_data": "/trades"},
        ],
        [
            {"text": "💰 Balance", "callback_data": "/balance"},
            {"text": "📈 P&L",     "callback_data": "/pnl"},
        ],
        [
            {"text": "⏸ Pause",   "callback_data": "/pause"},
            {"text": "▶️ Resume",  "callback_data": "/resume"},
        ],
        [{"text": "❓ Help", "callback_data": "/help"}],
    ]
}

_KILL_KB: dict[str, Any] = {
    "inline_keyboard": [[
        {"text": "✅ YES — stop now", "callback_data": "/kill_confirmed"},
        {"text": "❌ Cancel",          "callback_data": "/kill_cancel"},
    ]]
}


def _send(text: str, markup: dict[str, Any] | None = None) -> None:
    params: dict[str, Any] = {
        "chat_id": ALLOWED_CHAT,
        "text": text,
        "parse_mode": "HTML",
    }
    if markup is not None:
        params["reply_markup"] = markup
    _tg("sendMessage", **params)


def _answer_cb(cb_id: str, text: str = "") -> None:
    _tg("answerCallbackQuery", callback_query_id=cb_id, text=text)


# ---------------------------------------------------------------------------
# On-chain / HL data helpers
# ---------------------------------------------------------------------------

def _curl_post(url: str, payload: str) -> dict[str, Any]:
    out = subprocess.run(
        ["curl", "-s", "-X", "POST",
         "-H", "Content-Type: application/json",
         "-d", payload, url],
        capture_output=True, text=True, timeout=12,
    )
    return json.loads(out.stdout)


def _eoa_usdc() -> str:
    """USDC balance of EOA on Base via eth_call."""
    addr_hex = EOA_ADDRESS[2:].lower().zfill(64)
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_call",
        "params": [{"to": USDC_BASE, "data": "0x70a08231" + addr_hex}, "latest"],
    })
    try:
        result = _curl_post(BASE_RPC, payload).get("result", "0x0")
        return f"${int(result, 16) / 1_000_000:.2f}"
    except Exception as exc:
        return f"(err: {exc})"


def _hl_state(wallet: str) -> dict[str, Any]:
    payload = json.dumps({"type": "clearinghouseState", "user": wallet})
    try:
        return _curl_post(HL_INFO_URL, payload)
    except Exception:
        return {}


def _hedge_balance() -> str:
    data  = _hl_state(HEDGE_WALLET)
    acct  = float((data.get("marginSummary") or {}).get("accountValue") or 0)
    return f"${acct:.2f}"


def _hedge_positions() -> list[dict[str, Any]]:
    return _hl_state(HEDGE_WALLET).get("assetPositions") or []


# ---------------------------------------------------------------------------
# JSONL helpers
# ---------------------------------------------------------------------------

def _read_events(n: int = 500) -> list[dict[str, Any]]:
    if not JSONL_PATH.exists():
        return []
    lines = JSONL_PATH.read_text(encoding="utf-8").splitlines()
    out = []
    for raw in lines[-max(n * 4, 200):]:
        try:
            out.append(json.loads(raw))
        except Exception:
            pass
    return out


def _pnl_stats(events: list[dict[str, Any]]) -> tuple[float, float, int, int, int]:
    """Return (today_pnl, total_pnl, fills, hedged, hedge_failed)."""
    today_start = (int(time.time()) // 86400) * 86400 * 1000
    today_pnl = total_pnl = 0.0
    fills = hedged = hf = 0
    for e in events:
        if e.get("event") != "trade":
            continue
        state  = e.get("state", "")
        lim    = e.get("limitless_result") or {}
        cand   = e.get("candidate") or {}
        if lim.get("matched"):
            fills += 1
        if state == "hedged":
            hedged += 1
            filled = float(lim.get("filled_usdc") or 0)
            edge   = float(cand.get("edge") or 0)
            est    = filled * edge
            total_pnl += est
            if e.get("ts_ms", 0) >= today_start:
                today_pnl += est
        elif state == "hedge_failed":
            hf += 1
    return today_pnl, total_pnl, fills, hedged, hf


# ---------------------------------------------------------------------------
# Reply builders
# ---------------------------------------------------------------------------

def _daemon_pm2_status() -> str:
    try:
        out = subprocess.run(
            ["pm2", "jlist"], capture_output=True, text=True, timeout=5
        ).stdout
        lst = json.loads(out) if out.strip() else []
        entry = next((p for p in lst if p.get("name") == DAEMON_NAME), None)
        return (entry or {}).get("pm2_env", {}).get("status", "unknown")
    except Exception:
        return "unknown"


def _status_text() -> str:
    events = _read_events(300)
    startup = next((e for e in reversed(events) if e.get("event") == "startup"), None)
    mode = (startup or {}).get("mode", "unknown")

    pm2_status = _daemon_pm2_status()
    paused     = PAUSE_FILE.exists()
    icon       = "🟢" if pm2_status == "online" and not paused else "🔴"
    paused_tag = " <b>[PAUSED]</b>" if paused else ""

    today_pnl, total_pnl, fills, hedged, hf = _pnl_stats(events)
    trades_count = sum(1 for e in events if e.get("event") == "trade")
    eoa = _eoa_usdc()

    return "\n".join([
        f"{icon} <b>Limitless HL — {mode.upper()}{paused_tag}</b>",
        "",
        f"Daemon: <code>{pm2_status}</code>",
        f"EOA (Limitless): <code>{eoa}</code>",
        "",
        f"Session: {trades_count} scanned / {fills} filled / {hedged} hedged / {hf} hedge-failed",
        f"Today P&L (est): <code>${today_pnl:.2f}</code>",
        f"All-time P&L (est): <code>${total_pnl:.2f}</code>",
    ])


def _trades_text(n: int = 5) -> str:
    events = _read_events(300)
    trades = [e for e in events if e.get("event") == "trade"]
    recent = trades[-n:]
    if not recent:
        return "No trades logged yet."
    lines = [f"<b>Last {len(recent)} trades</b>\n"]
    for t in reversed(recent):
        ts_ms  = t.get("ts_ms", 0)
        ts_str = time.strftime("%H:%M:%S", time.gmtime(ts_ms / 1000)) if ts_ms else "?"
        cand   = t.get("candidate") or {}
        state  = t.get("state", "?")
        slug   = cand.get("slug", "?")
        side   = cand.get("side", "?")
        price  = cand.get("limit_price", 0)
        edge   = cand.get("edge", 0)
        filled = (t.get("limitless_result") or {}).get("filled_usdc", 0)
        icon = {"hedged": "✅", "limitless_unfilled": "⬜", "hedge_failed": "⚠️"}.get(state, "❓")
        lines.append(
            f"{icon} <code>{ts_str}</code> {slug}/{side} "
            f"@ <code>{price:.3f}</code>  edge={edge:.1%}  fill=${filled:.2f}"
        )
    return "\n".join(lines)


def _balance_text() -> str:
    eoa    = _eoa_usdc()
    hedge  = _hedge_balance()
    pos    = _hedge_positions()
    lines  = [
        "<b>💰 Balances</b>",
        "",
        f"EOA (Limitless/Base): <code>{eoa}</code>",
        f"HL hedge account:     <code>{hedge}</code>",
    ]
    if pos:
        lines += ["", "<b>Open HL positions:</b>"]
        for p in pos:
            position = p.get("position") or {}
            coin  = position.get("coin", "?")
            szi   = float(position.get("szi") or 0)
            entry = float(position.get("entryPx") or 0)
            upnl  = float(position.get("unrealizedPnl") or 0)
            icon  = "🟢" if szi > 0 else "🔴"
            lines.append(f"{icon} {coin}: {szi:+.4f} @ {entry:.2f}  uPnL=${upnl:.2f}")
    else:
        lines.append("No open HL hedge positions.")
    return "\n".join(lines)


def _pnl_text() -> str:
    events = _read_events(1000)
    today_pnl, total_pnl, fills, hedged, hf = _pnl_stats(events)
    return "\n".join([
        "<b>📈 P&L Summary</b>",
        "",
        f"Fills: {fills} total / {hedged} fully hedged / {hf} hedge-failed",
        f"Today P&L (est):    <code>${today_pnl:.2f}</code>",
        f"All-time P&L (est): <code>${total_pnl:.2f}</code>",
        "",
        "<i>Estimate = filled_usdc × edge (not accounting for binary resolution)</i>",
    ])


def _help_text() -> str:
    return (
        "<b>Limitless HL Arb Bot</b>\n\n"
        "/status  — daemon state, balance, session summary\n"
        "/trades  — last 5 fills\n"
        "/balance — EOA + HL hedge balances + open positions\n"
        "/pnl     — P&L summary\n"
        "/pause   — pause scanning (no new trades)\n"
        "/resume  — resume scanning\n"
        "/kill    — emergency stop (asks for confirmation)\n"
        "/help    — this message"
    )


# ---------------------------------------------------------------------------
# Control actions
# ---------------------------------------------------------------------------

_kill_pending: dict[str, bool] = {}


def _do_pause() -> str:
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text("paused\n")
    return "⏸ <b>Daemon paused.</b> No new trades until /resume."


def _do_resume() -> str:
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        return "▶️ <b>Daemon resumed.</b>"
    return "ℹ️ Daemon was not paused."


def _do_kill() -> str:
    try:
        r = subprocess.run(
            ["pm2", "stop", DAEMON_NAME],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return f"🛑 <code>{DAEMON_NAME}</code> stopped via pm2."
        return f"⚠️ pm2 stop failed (exit {r.returncode}): {r.stderr.strip()[:100]}"
    except Exception as exc:
        return f"⚠️ Could not stop: {exc}"


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def _handle(cmd: str, chat_id: str, cb_id: str | None = None) -> None:
    if chat_id != ALLOWED_CHAT:
        return
    if cb_id:
        _answer_cb(cb_id)

    # strip /prefix, @BotName suffix, args
    base = cmd.lstrip("/").split("@")[0].split()[0].lower()

    if base == "status":
        _send(_status_text(), _MAIN_KB)
    elif base == "trades":
        _send(_trades_text(5), _MAIN_KB)
    elif base == "balance":
        _send(_balance_text(), _MAIN_KB)
    elif base == "pnl":
        _send(_pnl_text(), _MAIN_KB)
    elif base == "pause":
        _send(_do_pause(), _MAIN_KB)
    elif base == "resume":
        _send(_do_resume(), _MAIN_KB)
    elif base == "kill":
        _kill_pending[chat_id] = True
        _send(
            "⚠️ <b>Stop the live daemon?</b>\n"
            "This halts all trading immediately. Limitless positions stay open until expiry.",
            _KILL_KB,
        )
    elif base == "kill_confirmed":
        if _kill_pending.pop(chat_id, False):
            _send(_do_kill(), _MAIN_KB)
        else:
            _send("No pending kill — use /kill first.", _MAIN_KB)
    elif base == "kill_cancel":
        _kill_pending.pop(chat_id, None)
        _send("❌ Kill cancelled.", _MAIN_KB)
    elif base in {"start", "help"}:
        _send(_help_text(), _MAIN_KB)
    else:
        _send(f"Unknown command: <code>/{base}</code>\n\n" + _help_text(), _MAIN_KB)


# ---------------------------------------------------------------------------
# JSONL tail — push notifications
# ---------------------------------------------------------------------------

def _tail_loop() -> None:
    """Background thread: tail daemon_trades.jsonl and push notable events."""
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Start at end of file so we don't replay history
    seen_pos = JSONL_PATH.stat().st_size if JSONL_PATH.exists() else 0

    while True:
        try:
            if not JSONL_PATH.exists():
                time.sleep(5)
                continue
            size = JSONL_PATH.stat().st_size
            if size < seen_pos:
                seen_pos = 0  # rotated
            if size == seen_pos:
                time.sleep(3)
                continue
            with JSONL_PATH.open("r", encoding="utf-8") as fh:
                fh.seek(seen_pos)
                chunk = fh.read(size - seen_pos)
                seen_pos = fh.tell()
            for raw in chunk.splitlines():
                try:
                    _push_event(json.loads(raw))
                except Exception:
                    pass
        except Exception:
            time.sleep(5)


def _push_event(e: dict[str, Any]) -> None:
    ev = e.get("event", "")

    if ev == "trade":
        state  = e.get("state", "")
        cand   = e.get("candidate") or {}
        lim    = e.get("limitless_result") or {}
        slug   = cand.get("slug", "?")
        side   = cand.get("side", "?")
        price  = cand.get("limit_price", 0)
        edge   = cand.get("edge", 0)
        filled = lim.get("filled_usdc", 0)

        if state == "hedged":
            if not _spam_ok(f"trade_{slug}", "trade"):
                return
            _send(
                f"✅ <b>HEDGED</b> — {slug}/{side}\n"
                f"Filled ${filled:.2f} @ {price:.3f}  edge={edge:.1%}"
            )
        elif state == "hedge_failed":
            if not _spam_ok(f"hf_{slug}", "trade"):
                return
            err = (e.get("error") or "unknown")[:120]
            _send(
                f"⚠️ <b>HEDGE FAILED</b> — {slug}/{side}\n"
                f"Filled ${filled:.2f} — hedge error: <code>{err}</code>"
            )

    elif ev == "startup":
        mode = e.get("mode", "?")
        if _spam_ok("startup", "startup"):
            _send(f"🟢 <b>Daemon started</b> — mode: <code>{mode}</code>")

    elif ev == "shutdown":
        if _spam_ok("shutdown", "startup"):
            _send("🔴 <b>Daemon shut down.</b>")

    elif ev in {"scan_error", "trade_error", "config_error", "circuit_breaker"}:
        err = (e.get("error") or e.get("reason") or "?")[:200]
        key = f"alert_{ev}_{err[:80]}"
        if not _spam_ok(key, "alert"):
            return
        _send(f"🚨 <b>{ev}</b>\n<code>{err}</code>")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"[limitless-hl-tg] starting — allowed chat: {ALLOWED_CHAT}", flush=True)

    tail = threading.Thread(target=_tail_loop, daemon=True)
    tail.start()

    _send("🟢 <b>Limitless HL bot online.</b>\nUse /status or tap a button.", _MAIN_KB)

    offset = 0
    while True:
        try:
            resp = _tg(
                "getUpdates",
                offset=offset,
                timeout=30,
                allowed_updates=["message", "callback_query"],
            )
            if not resp.get("ok"):
                err = resp.get("error") or {}
                code = err.get("code") if isinstance(err, dict) else 0
                if code == 409:
                    print("[limitless-hl-tg] 409 conflict — another instance running, backing off", flush=True)
                    time.sleep(15)
                elif code == 429:
                    time.sleep(10)
                else:
                    time.sleep(5)
                continue

            for update in resp.get("result") or []:
                offset = update["update_id"] + 1

                if "callback_query" in update:
                    cbq  = update["callback_query"]
                    cid  = str(cbq["from"]["id"])
                    data = cbq.get("data", "")
                    _handle(data, cid, cbq["id"])
                    continue

                msg  = update.get("message") or {}
                text = (msg.get("text") or "").strip()
                cid  = str((msg.get("from") or msg.get("chat") or {}).get("id", ""))
                if text.startswith("/") and cid:
                    _handle(text, cid)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[limitless-hl-tg] poll error: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
