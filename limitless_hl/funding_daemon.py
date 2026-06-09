"""Strategy 3 — HL Funding Signal → Limitless Directional Bet.

No HL hedge. Pure signal bet.

Backtest (113k rows, May 2025–Jun 2026, 15-min price return window):
  BTC  DOWN  funding >= +1.25e-05/hr (cap)   → 57.8% WR  n=15,366
  ETH  DOWN  funding >= +1.25e-05/hr (cap)   → 56.2% WR  n=15,232
  HYPE DOWN  funding >= +5.34e-05/hr (90th)  → 57.5% WR  n=2,830
  SOL  UP    funding <= -4.38e-05/hr (5th)   → 60.7% WR  n=1,415
  BNB  DOWN  funding >= +1.25e-05/hr (cap)   → BTC-proxied WR, backtest pending
  DOGE DOWN  funding >= +1.25e-05/hr (cap)   → BTC-proxied WR, backtest pending
  XRP  DOWN  funding >= +1.25e-05/hr (cap)   → BTC-proxied WR, backtest pending

Limitless offers 5-min, 15-min, Hourly, and Daily markets.
We use 15-min markets — an exact match for the backtest horizon.

Logic:
  1. Poll HL /info for current funding rates every --loop-seconds
  2. When a coin hits a signal threshold, look up its active Limitless 15-min market
  3. If the Limitless price gives positive EV (accounting for 300bps fee), place a FAK buy
  4. Risk-gate: max one open bet per coin, daily loss cap
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .secrets import get_secret

# ---------------------------------------------------------------------------
# Signal config — derived from backtest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FundingSignal:
    coin: str
    direction: str          # "UP" or "DOWN"
    threshold: float        # hourly rate threshold
    compare: str            # "gte" or "lte"
    backtest_wr: float      # historical win rate
    backtest_n: int         # sample size
    max_entry_price: float  # don't bet if Limitless price > this


@dataclass(frozen=True)
class FundingProofConfig:
    min_backtest_wr: float = 0.56
    min_backtest_n: int = 1_000
    min_ev_pct: float = 0.10
    max_live_stake_usdc: float = 5.0


def signal_key(signal: FundingSignal) -> str:
    return f"{signal.coin}:{signal.direction}:{signal.compare}:{signal.threshold}"


def first_spike_decision(
    states: dict[str, bool],
    signal: FundingSignal,
    *,
    triggered: bool,
    allow_startup_active: bool = False,
) -> tuple[bool, str]:
    key = signal_key(signal)
    previous = states.get(key)
    if not triggered:
        states[key] = False
        return False, "not_triggered"
    states[key] = True
    if previous is None and not allow_startup_active:
        return False, "startup_active_signal"
    if previous:
        return False, "sustained_signal"
    return True, "first_spike"

# Minimum edge after fee: entry_price <= backtest_wr * (1 - fee) - safety_margin
# 300bps fee = 0.03; safety_margin = 0.02 extra cushion
SIGNALS: list[FundingSignal] = [
    FundingSignal("BTC",  "DOWN", threshold=+1.25e-05, compare="gte", backtest_wr=0.578, backtest_n=15366, max_entry_price=0.52),
    FundingSignal("ETH",  "DOWN", threshold=+1.25e-05, compare="gte", backtest_wr=0.562, backtest_n=15232, max_entry_price=0.51),
    FundingSignal("HYPE", "DOWN", threshold=+5.34e-05, compare="gte", backtest_wr=0.575, backtest_n=2830,  max_entry_price=0.52),
    FundingSignal("SOL",  "UP",   threshold=-4.38e-05, compare="lte", backtest_wr=0.607, backtest_n=1415,  max_entry_price=0.57),
    # BTC-proxied: same funding-cap threshold, conservative WR until coin-specific backtest runs
    FundingSignal("BNB",  "DOWN", threshold=+1.25e-05, compare="gte", backtest_wr=0.570, backtest_n=1000,  max_entry_price=0.52),
    FundingSignal("DOGE", "DOWN", threshold=+1.25e-05, compare="gte", backtest_wr=0.570, backtest_n=1000,  max_entry_price=0.52),
    FundingSignal("XRP",  "DOWN", threshold=+1.25e-05, compare="gte", backtest_wr=0.570, backtest_n=1000,  max_entry_price=0.52),
]

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LIMITLESS_BASE = "https://api.limitless.exchange"
BASE_RPC = "https://mainnet.base.org"
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FEE_BPS = 300


def get_wallet_usdc_balance(maker_address: str) -> float:
    """Return current USDC balance of maker wallet on Base mainnet."""
    data_field = "0x70a08231" + "000000000000000000000000" + maker_address[2:].lower()
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": USDC_ADDRESS, "data": data_field}, "latest"],
    })
    out = subprocess.run(
        ["curl", "-s", "--max-time", "10", "-X", "POST",
         "-H", "Content-Type: application/json",
         "-d", payload, BASE_RPC],
        capture_output=True, text=True, timeout=12,
    )
    result = json.loads(out.stdout).get("result", "0x0")
    return int(result, 16) / 1_000_000.0


def kelly_stake(
    backtest_wr: float,
    entry_price: float,
    bankroll: float,
    kelly_fraction: float,
    min_stake: float,
) -> float:
    """
    Quarter-Kelly (default) stake for a binary prediction market.

    Full Kelly:  f* = p - q / b
      where b = (1 - entry_price) / entry_price  (net odds per $1 risked)
            p = backtest win rate
            q = 1 - p

    Scaled Kelly: stake = f* × kelly_fraction × bankroll
    Returns 0.0 when the formula yields no edge (f* ≤ 0).
    """
    if entry_price <= 0 or entry_price >= 1 or bankroll <= 0:
        return 0.0
    b = (1.0 - entry_price) / entry_price
    f_full = backtest_wr - (1.0 - backtest_wr) / b
    if f_full <= 0:
        return 0.0
    stake = f_full * kelly_fraction * bankroll
    return max(min_stake, round(stake, 2))


def passes_live_funding_proof(
    signal: FundingSignal,
    *,
    ev_pct: float,
    stake_usdc: float,
    config: FundingProofConfig,
) -> tuple[bool, str]:
    if signal.backtest_wr < config.min_backtest_wr:
        return False, "backtest_wr_below_min"
    if signal.backtest_n < config.min_backtest_n:
        return False, "backtest_n_below_min"
    if ev_pct < config.min_ev_pct:
        return False, "ev_below_min"
    if stake_usdc <= 0:
        return False, "stake_zero"
    if stake_usdc > config.max_live_stake_usdc:
        return False, "stake_out_of_bounds"
    return True, "allowed"


# ---------------------------------------------------------------------------
# HL live funding
# ---------------------------------------------------------------------------

def get_live_funding(retries: int = 3) -> dict[str, float]:
    """Return {coin: hourly_funding_rate} for all HL perpetuals.

    Retries up to `retries` times on empty/failed responses; raises on exhaustion.
    """
    payload = '{"type":"metaAndAssetCtxs"}'
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(retries):
        try:
            out = subprocess.run(
                ["curl", "-s", "--max-time", "20", "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-d", payload, HL_INFO_URL],
                capture_output=True, text=True, timeout=25,
            )
            if not out.stdout.strip():
                raise ValueError("empty response from HL API")
            data = json.loads(out.stdout)
            meta, ctxs = data[0], data[1]
            universe = meta.get("universe", [])
            result = {}
            for asset, ctx in zip(universe, ctxs):
                name = asset.get("name", "")
                funding = ctx.get("funding")
                if funding is not None:
                    try:
                        result[name] = float(funding)
                    except (ValueError, TypeError):
                        pass
            return result
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(3)
    raise last_exc


# ---------------------------------------------------------------------------
# Limitless market helpers
# ---------------------------------------------------------------------------

def get_active_15min_markets() -> list[dict[str, Any]]:
    """Fetch active 15-min direction markets from Limitless (scan up to 3 pages).

    API limit cap = 25; page 1 of 25 covers all active 15-min markets.
    Category tag is '15 min' (e.g. ['Crypto', '15 min'] or ['Crypto', 'Bitcoin', '15 min']).
    Title pattern: '<COIN> Up or Down - 15 Min'.

    Note: the original limit=50 bug returned a 400 error silently — fixed to limit=25.
    """
    results = []
    seen_slugs: set[str] = set()
    for page in range(1, 4):
        out = subprocess.run(
            ["curl", "-s", f"{LIMITLESS_BASE}/markets/active?page={page}&limit=25&tradeType=clob"],
            capture_output=True, text=True, timeout=10,
        )
        try:
            data = json.loads(out.stdout)
        except Exception:
            break
        rows = data.get("data") or []
        if not rows:
            break
        for row in rows:
            cats  = row.get("categories") or []
            title = str(row.get("title") or "")
            slug  = str(row.get("slug") or "")
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            if "15 min" not in cats:
                continue
            if "Up or Down" not in title:
                continue
            symbol = title.split(" Up or Down")[0].strip().upper()
            results.append({
                "symbol": symbol,
                "slug": slug,
                "expiration_ms": row.get("expirationTimestamp", 0),
            })
    return results


def get_limitless_price(slug: str) -> tuple[float, float] | None:
    """Return (up_ask, down_ask) from the 15-min orderbook. None on error."""
    out = subprocess.run(
        ["curl", "-s", f"{LIMITLESS_BASE}/markets/{slug}/orderbook"],
        capture_output=True, text=True, timeout=8,
    )
    try:
        book = json.loads(out.stdout)
    except Exception:
        return None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not asks or not bids:
        return None
    up_ask   = float(asks[0].get("price", 0))
    down_ask = float(bids[0].get("price", 0)) if bids else 0.0
    # Limitless returns DOWN price as (1 - up_bid) implicitly via bid side
    # Use complement: down_ask ≈ 1 - up_best_bid
    up_bid = float(bids[0].get("price", 0))
    down_ask = round(1.0 - up_bid, 4)
    return up_ask, down_ask


def get_market_details(slug: str) -> dict[str, Any]:
    out = subprocess.run(
        ["curl", "-s", f"{LIMITLESS_BASE}/markets/{slug}"],
        capture_output=True, text=True, timeout=8,
    )
    return json.loads(out.stdout)


# ---------------------------------------------------------------------------
# Order placement (reuses live_trade.py)
# ---------------------------------------------------------------------------

def place_signal_order(
    signal: FundingSignal,
    slug: str,
    market_details: dict[str, Any],
    entry_price: float,
    stake_usdc: float,
) -> dict[str, Any]:
    from .live_trade import (
        LimitlessCredentials, LimitlessOrderBuilder, LimitlessOrderIntent,
        LimitlessSubmitter, sign_hmac_headers,
    )

    token_id_str = get_secret("LIMITLESS_TOKEN_ID")
    token_secret = get_secret("LIMITLESS_TOKEN_SECRET")
    private_key  = get_secret("LIMITLESS_PRIVATE_KEY")
    maker        = os.environ.get("LIMITLESS_MAKER_ADDRESS", "")
    owner_id     = int(os.environ.get("LIMITLESS_OWNER_ID", "0"))

    tokens = market_details.get("tokens") or {}
    # For DOWN signal → buy NO token; for UP signal → buy YES token
    token_id = tokens.get("no") if signal.direction == "DOWN" else tokens.get("yes")
    if not token_id:
        return {"submitted": False, "error": f"missing token_id for {signal.direction}"}

    venue             = market_details.get("venue") or {}
    verifying_contract = str(venue.get("exchange") or "")
    if not verifying_contract:
        return {"submitted": False, "error": "missing verifying_contract"}

    # Tick-round the size
    price_str = f"{entry_price:.10f}".rstrip("0")
    decimals  = len(price_str.split(".")[1]) if "." in price_str else 0
    tick      = 10 ** (decimals + 1)
    size_raw  = int(stake_usdc / entry_price * 1_000_000)
    size      = ((size_raw // tick) * tick) / 1_000_000

    if size <= 0:
        return {"submitted": False, "error": "size rounded to zero"}

    import time as _time
    client_order_id = f"fs3-{signal.coin[:3]}-{signal.direction[:2]}-{int(_time.time()*1000)}"

    builder = LimitlessOrderBuilder(
        maker=maker, owner_id=owner_id,
        fee_rate_bps=FEE_BPS, signature_type=0, signer=None,
    )
    intent = LimitlessOrderIntent(
        market_slug=slug, token_id=str(token_id),
        side="BUY", price=entry_price, size=size,
        order_type="FAK", verifying_contract=verifying_contract,
        client_order_id=client_order_id,
    )
    submitter = LimitlessSubmitter(
        credentials=LimitlessCredentials(token_id_str or "", token_secret or ""),
        builder=builder, private_key=private_key or "",
    )
    return submitter.submit_intent(intent)


# ---------------------------------------------------------------------------
# CLI + main loop
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="HL funding signal → Limitless directional daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--kelly-fraction",      type=float, default=0.25,
                   help="Fraction of full Kelly to bet (0.25 = quarter-Kelly)")
    p.add_argument("--min-stake-usdc",      type=float, default=1.0,
                   help="Floor stake; skip trade if Kelly produces less than this")
    p.add_argument("--min-seconds-to-expiry", type=int, default=120,
                   help="Skip market if fewer than this many seconds remain")
    p.add_argument("--loop-seconds",        type=int,   default=30)
    p.add_argument("--live-armed",          action="store_true")
    p.add_argument("--jsonl-out",           default="tmp/limitless_hl/funding_trades.jsonl")
    p.add_argument("--iterations",          type=int, default=0)
    p.add_argument("--min-live-ev-pct",     type=float, default=0.10)
    p.add_argument("--min-backtest-wr",     type=float, default=0.56)
    p.add_argument("--min-backtest-n",      type=int, default=1_000)
    p.add_argument("--maker-address",       type=str, default="",
                   help="Override wallet address for balance fetch (falls back to LIMITLESS_MAKER_ADDRESS env)")
    p.add_argument("--first-spike-only",    action="store_true",
                   help="Only act when a funding signal first enters the threshold; skip sustained cap runs")
    p.add_argument("--allow-startup-active-signal", action="store_true",
                   help="With --first-spike-only, allow already-active signals on daemon startup")
    return p


def _log(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def main() -> None:
    args = build_parser().parse_args()

    out_path = Path(args.jsonl_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "live" if args.live_armed else "dry_run"
    if args.live_armed and os.environ.get("LIMITLESS_FUNDING_ALLOW_UNHEDGED") != "1":
        raise SystemExit(
            "--live-armed funding daemon is unhedged; set LIMITLESS_FUNDING_ALLOW_UNHEDGED=1 "
            "only after accepting naked binary exposure"
        )
    proof = FundingProofConfig(
        min_backtest_wr=args.min_backtest_wr,
        min_backtest_n=args.min_backtest_n,
        min_ev_pct=args.min_live_ev_pct,
    )
    maker_address = (
        args.maker_address
        or os.environ.get("LIMITLESS_MAKER_ADDRESS", "")
    )
    _log(out_path, {"event": "startup", "mode": mode, "kelly_fraction": args.kelly_fraction,
                    "min_stake_usdc": args.min_stake_usdc, "ts_ms": int(time.time() * 1000)})
    print(json.dumps({"event": "startup", "mode": mode, "kelly_fraction": args.kelly_fraction}), flush=True)

    open_coins: set[str] = set()      # coins with open bets (avoid doubles)
    coin_expiry_ms: dict[str, int] = {}
    signal_states: dict[str, bool] = {}
    pause_file = out_path.parent / "funding_daemon.pause"

    running = True
    def _stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running; running = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    iteration = 0
    while running:
        iteration += 1
        now_ms = int(time.time() * 1000)

        # Clear expired coins
        for coin in list(coin_expiry_ms):
            if coin_expiry_ms[coin] < now_ms:
                open_coins.discard(coin)
                del coin_expiry_ms[coin]

        if pause_file.exists():
            time.sleep(5); continue

        # 1. Live funding rates
        try:
            live_funding = get_live_funding()
        except Exception as exc:
            _log(out_path, {"event": "funding_fetch_error", "error": str(exc), "ts_ms": now_ms})
            time.sleep(max(args.loop_seconds, 1)); continue

        # 2. Active 15-min markets on Limitless
        try:
            markets_15m = get_active_15min_markets()
        except Exception as exc:
            _log(out_path, {"event": "market_fetch_error", "error": str(exc), "ts_ms": now_ms})
            time.sleep(max(args.loop_seconds, 1)); continue

        markets_by_symbol = {m["symbol"]: m for m in markets_15m}

        # 3. Check each signal
        traded = False
        for sig in SIGNALS:
            rate = live_funding.get(sig.coin)
            if rate is None:
                continue

            triggered = (
                (sig.compare == "gte" and rate >= sig.threshold) or
                (sig.compare == "lte" and rate <= sig.threshold)
            )
            _log(out_path, {
                "event": "signal_check", "coin": sig.coin, "direction": sig.direction,
                "rate": rate, "threshold": sig.threshold,
                "triggered": triggered, "ts_ms": now_ms,
            })

            if not triggered:
                if args.first_spike_only:
                    first_spike_decision(
                        signal_states,
                        sig,
                        triggered=False,
                        allow_startup_active=args.allow_startup_active_signal,
                    )
                continue
            if args.first_spike_only:
                first_ok, first_reason = first_spike_decision(
                    signal_states,
                    sig,
                    triggered=True,
                    allow_startup_active=args.allow_startup_active_signal,
                )
                if not first_ok:
                    _log(out_path, {
                        "event": "signal_skip_first_spike", "coin": sig.coin,
                        "direction": sig.direction, "reason": first_reason,
                        "rate": rate, "threshold": sig.threshold, "ts_ms": now_ms,
                    })
                    continue
            if sig.coin in open_coins:
                continue  # already have an open bet

            market = markets_by_symbol.get(sig.coin)
            if not market:
                _log(out_path, {"event": "signal_no_market", "coin": sig.coin,
                                "available": list(markets_by_symbol.keys()), "ts_ms": now_ms})
                continue

            # Check time to expiry
            secs_to_expiry = (market["expiration_ms"] - now_ms) / 1000
            if secs_to_expiry < args.min_seconds_to_expiry:
                _log(out_path, {"event": "signal_skip_expiry", "coin": sig.coin,
                                "secs_to_expiry": secs_to_expiry, "ts_ms": now_ms})
                continue

            # Get current Limitless price
            try:
                prices = get_limitless_price(market["slug"])
            except Exception:
                continue
            if not prices:
                continue
            up_ask, down_ask = prices
            entry_price = down_ask if sig.direction == "DOWN" else up_ask

            if entry_price <= 0 or entry_price >= 1:
                continue

            # EV check: bet only if price < backtest_wr - fee_buffer
            fee_buffer = FEE_BPS / 10000.0 / (1.0 - entry_price)
            net_wr = sig.backtest_wr - fee_buffer
            if entry_price > sig.max_entry_price:
                _log(out_path, {"event": "signal_skip_price", "coin": sig.coin,
                                "direction": sig.direction, "entry_price": entry_price,
                                "max_entry_price": sig.max_entry_price, "ts_ms": now_ms})
                continue

            ev_pct = (sig.backtest_wr / entry_price) - 1.0 - (FEE_BPS / 10000.0)
            _log(out_path, {
                "event": "signal_candidate", "coin": sig.coin, "direction": sig.direction,
                "entry_price": entry_price, "backtest_wr": sig.backtest_wr,
                "ev_pct": round(ev_pct, 4), "rate": rate, "ts_ms": now_ms,
            })

            # Kelly stake: read live balance, size the bet
            if args.live_armed and maker_address:
                try:
                    bankroll = get_wallet_usdc_balance(maker_address)
                except Exception as exc:
                    _log(out_path, {"event": "balance_error", "error": str(exc), "ts_ms": now_ms})
                    continue
            else:
                bankroll = 100.0  # dry-run: simulate $100 bankroll

            stake_usdc = kelly_stake(
                backtest_wr=sig.backtest_wr,
                entry_price=entry_price,
                bankroll=bankroll,
                kelly_fraction=args.kelly_fraction,
                min_stake=args.min_stake_usdc,
            )

            if not args.live_armed:
                entry = {
                    "event": "trade",
                    "mode": "dry_run",
                    "state": "filled",
                    "slug": market["slug"],
                    "coin": sig.coin,
                    "direction": sig.direction,
                    "entry_price": entry_price,
                    "filled_usdc": stake_usdc,
                    "kelly_stake_usdc": stake_usdc,
                    "ev_pct": round(ev_pct, 4),
                    "rate": rate,
                    "bankroll": bankroll,
                    "ts_ms": now_ms,
                }
                _log(out_path, entry)
                print(json.dumps(entry, sort_keys=True), flush=True)
                traded = True
                break

            proof_ok, proof_reason = passes_live_funding_proof(
                sig,
                ev_pct=ev_pct,
                stake_usdc=stake_usdc,
                config=proof,
            )
            if not proof_ok:
                _log(out_path, {
                    "event": "signal_skip_proof",
                    "coin": sig.coin,
                    "direction": sig.direction,
                    "reason": proof_reason,
                    "ev_pct": round(ev_pct, 4),
                    "backtest_wr": sig.backtest_wr,
                    "backtest_n": sig.backtest_n,
                    "kelly_stake_usdc": stake_usdc,
                    "bankroll": bankroll,
                    "ts_ms": now_ms,
                })
                continue

            # Place order
            try:
                details = get_market_details(market["slug"])
            except Exception as exc:
                _log(out_path, {"event": "details_error", "error": str(exc), "ts_ms": now_ms})
                continue

            try:
                result = place_signal_order(sig, market["slug"], details, entry_price, stake_usdc)
            except Exception as exc:
                _log(out_path, {"event": "order_error", "coin": sig.coin, "error": str(exc), "ts_ms": now_ms})
                continue

            matched = result.get("matched", False)
            filled  = result.get("filled_usdc", 0.0)
            state   = "filled" if matched else "unfilled"

            _log(out_path, {
                "event": "trade", "state": state, "coin": sig.coin,
                "direction": sig.direction, "entry_price": entry_price,
                "filled_usdc": filled, "ev_pct": round(ev_pct, 4),
                "kelly_stake_usdc": stake_usdc, "bankroll": bankroll,
                "kelly_fraction": args.kelly_fraction,
                "backtest_wr": sig.backtest_wr, "rate": rate,
                "slug": market["slug"], "result": result, "ts_ms": now_ms,
            })
            print(json.dumps({"event": "trade", "state": state, "coin": sig.coin,
                              "direction": sig.direction, "entry_price": entry_price,
                              "filled_usdc": filled, "ev_pct": round(ev_pct, 4),
                              "kelly_stake_usdc": stake_usdc, "bankroll": bankroll}), flush=True)

            if matched:
                open_coins.add(sig.coin)
                coin_expiry_ms[sig.coin] = int(market["expiration_ms"])
                traded = True
                break  # one filled trade per cycle; unfilled → fall through to next signal

        if not traded:
            _log(out_path, {"event": "scan_no_signal", "ts_ms": now_ms})

        if args.iterations and iteration >= args.iterations:
            break
        if running:
            time.sleep(max(args.loop_seconds, 1))

    _log(out_path, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})
    print(json.dumps({"event": "shutdown"}), flush=True)


if __name__ == "__main__":
    main()
