"""Continuous Limitless <> Hyperliquid trade daemon.

Scans every --loop-seconds, gates each candidate through RiskManager,
executes via PairTradeRunner, and logs every event to JSONL.

Dry-run by default (preview legs, no real orders).
Pass --live-armed + --hedge-live for real execution.
Required env for live: LIMITLESS_OWNER_ID, LIMITLESS_MAKER_ADDRESS, LIMITLESS_FEE_RATE_BPS.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .clients import HyperliquidClient, LimitlessClient
from .hyperliquid_hedge import MIN_HEDGE_NOTIONAL_USD, HyperliquidHedgerConfig, HyperliquidMarketHedger
from .live_trade import (
    LimitlessCredentials,
    LimitlessOrderBuilder,
    LimitlessSubmitter,
    PairTradeRunner,
    candidate_to_limitless_intent,
)
from .model import EdgeConfig
from .risk import RiskConfig, RiskLedger, RiskManager
from .polymarket_feed import PolymarketFeed
from .scanner import LimitlessHyperliquidScanner
from .volatility import PricingProvider
from .scorer import LiveFeatureProvider, ScoringConfig, SliceStats, load_hl_bot_context, score_candidate
from .secrets import get_secret


# ---------------------------------------------------------------------------
# Preview legs (dry-run only — no network calls)
# ---------------------------------------------------------------------------

class _PreviewLeg:
    def __init__(self, intent: Any) -> None:
        self._intent = intent

    def submit(self, candidate: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return {
            "submitted": False,
            "matched": True,
            "filled_usdc": float(candidate.get("stake_usdc") or 0.0),
            "intent": asdict(self._intent),
            "raw": {"mode": "preview"},
        }


class _PreviewHedger:
    def hedge(self, plan: Any) -> dict[str, Any]:
        return {"submitted": True, "plan": asdict(plan), "raw": {"mode": "preview"}}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Limitless <> Hyperliquid continuous trade daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Edge gates
    p.add_argument("--min-edge", type=float, default=0.08)
    p.add_argument("--max-price", type=float, default=0.85)
    p.add_argument("--min-price", type=float, default=0.0,
                   help="Reject candidates priced below this; sub-0.20 longshots lose to the 3% taker fee")
    p.add_argument("--max-edge", type=float, default=1.0,
                   help="Reject candidates whose claimed edge exceeds this; very large model-vs-market gaps are adverse selection, not alpha")
    p.add_argument("--min-seconds-to-expiry", type=int, default=180)
    p.add_argument("--stake-usdc", type=float, default=25.0)
    # Risk gates
    p.add_argument("--max-daily-loss-usdc", type=float, default=50.0)
    p.add_argument("--max-open-markets", type=int, default=10)
    p.add_argument("--max-trades-per-hour", type=int, default=0,
                   help="Cap live entries per rolling hour (0 = unlimited)")
    p.add_argument("--loss-cooldown-losses", type=int, default=0,
                   help="Pause entries after this many consecutive resolved losses (0 = off)")
    p.add_argument("--loss-cooldown-seconds", type=int, default=1800,
                   help="How long to pause after the loss streak threshold is hit")
    p.add_argument("--learner-db", default="tmp/limitless_hl/learner.sqlite3",
                   help="Learner DB consulted for the loss-cooldown gate")
    # Execution mode
    p.add_argument("--live-armed", action="store_true", help="Enable real Limitless order submission")
    p.add_argument("--hedge-live", action="store_true", help="Enable real Hyperliquid hedge; requires --live-armed")
    p.add_argument("--allow-unhedged-live", action="store_true", help="Allow Limitless-only live fills without HL hedge")
    p.add_argument("--max-hedge-notional-usdc", type=float, default=25.0)
    p.add_argument("--client-order-prefix", default="limitless-hl")
    p.add_argument("--symbols", default="", help="Comma-separated candidate symbols to allow")
    p.add_argument("--intervals", default="", help="Comma-separated candidate intervals to allow")
    p.add_argument("--sides", default="", help="Comma-separated candidate sides to allow")
    p.add_argument("--slice-score-file", default="", help="Optional evaluation report used to gate live slices")
    p.add_argument("--slice-min-n", type=int, default=3)
    p.add_argument("--slice-min-roi", type=float, default=0.0)
    p.add_argument("--slice-min-win-rate", type=float, default=0.0)
    p.add_argument("--slice-live-min-n", type=int, default=4, help="Demote a seeded slice when live resolved sample is large enough and weak")
    p.add_argument("--slice-live-min-roi", type=float, default=0.0, help="Minimum live-only ROI required once live sample reaches --slice-live-min-n")
    p.add_argument("--slice-strategies", default="scored_daemon,seed", help="Comma-separated learner strategies allowed to promote scored live daemon slices")
    p.add_argument("--shadow-graduate", action="store_true", help="Allow strong shadow_daemon slices to graduate into live eligibility")
    p.add_argument("--shadow-min-n", type=int, default=20, help="Minimum resolved shadow trades before a slice can graduate")
    p.add_argument("--shadow-min-roi", type=float, default=0.10, help="Minimum shadow ROI required before live graduation")
    p.add_argument("--shadow-min-win-rate", type=float, default=0.52, help="Minimum shadow win rate required before live graduation")
    p.add_argument("--scream-promote", action="store_true", help="Allow very high current-edge short markets to bypass slice-history gating")
    p.add_argument("--scream-min-edge", type=float, default=0.08, help="Minimum current scan edge required for scream promotion")
    p.add_argument("--scream-intervals", default="5m,15m", help="Comma-separated intervals eligible for scream promotion")
    p.add_argument("--scoring-live", action="store_true", help="Score live candidates with momentum/funding/basis features")
    p.add_argument("--score-min", type=float, default=1.0)
    p.add_argument("--score-base-stake-usdc", type=float, default=1.0)
    p.add_argument("--score-max-stake-usdc", type=float, default=3.0)
    p.add_argument("--hl-bot-status-file", default="", help="Optional hl_bot_status.json used as a soft scoring feature")
    p.add_argument("--hl-bot-status-max-age-ms", type=int, default=120_000)
    # Loop
    p.add_argument("--loop-seconds", type=int, default=20)
    p.add_argument("--scan-error-backoff-seconds", type=int, default=120, help="Sleep this long after rate-limit scan errors")
    # Pricing
    p.add_argument("--flat-pricing", action="store_true",
                   help="Disable dynamic EWMA vol / reversal shade / spot ref; use flat config vol")
    p.add_argument("--book-log", default="",
                   help="Optional jsonl path: append per-scan orderbook snapshots for calibration studies")
    p.add_argument("--stop-on-insufficient-collateral", action="store_true", help="Stop live daemon after Limitless insufficient collateral")
    p.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    # Output
    p.add_argument("--jsonl-out", default="tmp/limitless_hl/daemon_trades.jsonl")
    p.add_argument(
        "--polymarket-gate-threshold", type=float, default=0.0,
        help="Block a candidate when our model's side-probability exceeds the "
             "Polymarket twin's implied probability by more than this "
             "(0 disables the gate; the signal is always recorded in books).",
    )
    return p


# ---------------------------------------------------------------------------
# Runner factory
# ---------------------------------------------------------------------------

def _build_runner(
    args: argparse.Namespace,
    limitless_client: LimitlessClient,
    candidate: dict[str, Any],
    details: dict[str, Any],
) -> PairTradeRunner:
    client_order_id = (
        f"{args.client_order_prefix}-{candidate['slug']}-{candidate['side']}"
        f"-{int(time.time() * 1000)}"
    )
    intent = candidate_to_limitless_intent(candidate, details, client_order_id=client_order_id)

    if not args.live_armed:
        return PairTradeRunner(limitless=_PreviewLeg(intent), hedger=_PreviewHedger())

    # Live path — resolve credentials
    token_id = get_secret("LIMITLESS_TOKEN_ID")
    token_secret = get_secret("LIMITLESS_TOKEN_SECRET")
    private_key = get_secret("LIMITLESS_PRIVATE_KEY")
    owner_id = os.environ.get("LIMITLESS_OWNER_ID")
    maker_address = os.environ.get("LIMITLESS_MAKER_ADDRESS")

    missing = [
        name
        for name, value in {
            "LIMITLESS_TOKEN_ID": token_id,
            "LIMITLESS_TOKEN_SECRET": token_secret,
            "LIMITLESS_PRIVATE_KEY": private_key,
            "LIMITLESS_OWNER_ID": owner_id,
            "LIMITLESS_MAKER_ADDRESS": maker_address,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"--live-armed but missing credentials: {', '.join(missing)}")

    if not args.hedge_live and not args.allow_unhedged_live:
        raise RuntimeError("--live-armed requires --hedge-live; refusing one-sided Limitless order")

    # Smart wallet address — maker in all orders; EOA (maker_address) is the signer.
    smart_wallet = os.environ.get("LIMITLESS_SMART_WALLET") or maker_address or ""
    eoa_address  = maker_address or ""
    sig_type     = int(os.environ.get("LIMITLESS_SIGNATURE_TYPE", "1"))

    submitter = LimitlessSubmitter(
        credentials=LimitlessCredentials(token_id or "", token_secret or ""),
        builder=LimitlessOrderBuilder(
            maker=smart_wallet,
            owner_id=int(owner_id or "0"),
            fee_rate_bps=int(os.environ.get("LIMITLESS_FEE_RATE_BPS", "0")),
            signature_type=sig_type,   # 1 = proxy/smart-wallet; 0 = plain EOA
            signer=eoa_address if sig_type == 1 else None,
        ),
        private_key=private_key or "",
    )
    hedger = HyperliquidMarketHedger(
        HyperliquidHedgerConfig(
            live=True,
            max_notional_usdc=args.max_hedge_notional_usdc,
        )
    )

    class _LiveLeg:
        def submit(self, candidate: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
            return submitter.submit_intent(intent)

    return PairTradeRunner(limitless=_LiveLeg(), hedger=hedger, require_hedge=args.hedge_live)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _ensure_eoa_mode() -> None:
    """Force tradeWalletOption=eoa on startup — the Limitless UI resets it to smartWallet."""
    try:
        token_id = get_secret("LIMITLESS_TOKEN_ID")
        token_secret = get_secret("LIMITLESS_TOKEN_SECRET")
        if not token_id or not token_secret:
            return
        from .live_trade import sign_hmac_headers
        import requests as _req
        body = '{"tradeWalletOption":"eoa"}'
        headers = sign_hmac_headers(
            __import__("limitless_hl.live_trade", fromlist=["LimitlessCredentials"]).LimitlessCredentials(token_id, token_secret),
            "PUT", "/profiles", body,
        )
        headers["Content-Type"] = "application/json"
        resp = _req.put("https://api.limitless.exchange/profiles", data=body, headers=headers, timeout=10)
        client = (resp.json() or {}).get("client", "?")
        print(json.dumps({"event": "profile_eoa_set", "client": client}), flush=True)
    except Exception as exc:
        print(json.dumps({"event": "profile_eoa_warn", "error": str(exc)}), flush=True)


def main() -> None:
    args = build_parser().parse_args()

    if args.live_armed:
        if args.hedge_live and args.stake_usdc < MIN_HEDGE_NOTIONAL_USD:
            raise SystemExit(
                f"--live-armed requires --stake-usdc >= {MIN_HEDGE_NOTIONAL_USD:.2f} "
                "so the Hyperliquid hedge can meet minimum order notional"
            )
        if args.hedge_live and args.max_hedge_notional_usdc < args.stake_usdc:
            raise SystemExit("--live-armed requires --max-hedge-notional-usdc >= --stake-usdc")
        if args.allow_unhedged_live and args.hedge_live:
            raise SystemExit("choose either --hedge-live or --allow-unhedged-live, not both")
        if args.allow_unhedged_live and args.stake_usdc > 5:
            raise SystemExit("--allow-unhedged-live is capped at --stake-usdc 5 for the pilot")
        _ensure_eoa_mode()

    pricing = None if args.flat_pricing else PricingProvider()
    scanner = LimitlessHyperliquidScanner(
        limitless=LimitlessClient(),
        hyperliquid=HyperliquidClient(),
        config=EdgeConfig(
            min_edge=args.min_edge,
            max_price=args.max_price,
            min_seconds_to_expiry=args.min_seconds_to_expiry,
            stake_usdc=args.stake_usdc,
            min_size_usdc=args.stake_usdc,  # only require liquidity >= our stake
        ),
        pricing=pricing,
        polymarket=PolymarketFeed(),
    )
    risk = RiskManager(RiskConfig(
        max_daily_loss_usdc=args.max_daily_loss_usdc,
        max_open_markets=args.max_open_markets,
        max_stake_usdc=args.score_max_stake_usdc if args.scoring_live else args.stake_usdc,
    ))
    limitless_client = LimitlessClient()  # separate instance for market_details calls
    slice_score_path = Path(args.slice_score_file) if args.slice_score_file else None
    slice_scores: set[tuple[str, str, str]] | None = None
    slice_stats: dict[tuple[str, str, str], SliceStats] = {}
    feature_provider = LiveFeatureProvider() if args.scoring_live else None
    scoring_config = ScoringConfig(
        base_stake_usdc=args.score_base_stake_usdc,
        max_stake_usdc=args.score_max_stake_usdc,
        min_score=args.score_min,
        min_slice_n=args.slice_min_n,
        min_slice_roi=args.slice_min_roi,
        min_slice_win_rate=args.slice_min_win_rate,
    )

    out_path = Path(args.jsonl_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Session-scoped mutable state
    open_slugs, slug_expiry_ms = _load_recent_open_slugs(out_path, now_ms=int(time.time() * 1000))
    realized_pnl: float = 0.0
    trade_times_ms: list[int] = []
    fill_stats = {"filled": 0, "unfilled": 0}

    running = True

    def _handle_stop(sig: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    mode_label = "live" if args.live_armed else "dry_run"
    _log(out_path, {"event": "startup", "mode": mode_label, "ts_ms": int(time.time() * 1000)})

    pause_file = out_path.parent / "daemon.pause"

    iteration = 0
    while running:
        iteration += 1
        now_ms = int(time.time() * 1000)

        # Pause flag — written by TG bot /pause command
        if pause_file.exists():
            _log(out_path, {"event": "paused", "ts_ms": now_ms})
            time.sleep(5)
            continue

        # Remove expired markets from the open set
        expired = {slug for slug, exp in slug_expiry_ms.items() if exp < now_ms}
        if expired:
            open_slugs -= expired
            for slug in expired:
                slug_expiry_ms.pop(slug, None)

        if slice_score_path is not None:
            slice_scores = _load_slice_scores(
                slice_score_path,
                min_n=args.slice_min_n,
                min_roi=args.slice_min_roi,
                min_win_rate=args.slice_min_win_rate,
                live_min_n=args.slice_live_min_n,
                live_min_roi=args.slice_live_min_roi,
                allowed_strategies=_parse_strategy_filter(args.slice_strategies),
            )
            if args.shadow_graduate:
                shadow_scores = _load_slice_scores(
                    slice_score_path,
                    min_n=args.shadow_min_n,
                    min_roi=args.shadow_min_roi,
                    min_win_rate=args.shadow_min_win_rate,
                    live_min_n=args.slice_live_min_n,
                    live_min_roi=args.slice_live_min_roi,
                    allowed_strategies={"shadow_daemon"},
                )
                slice_scores |= shadow_scores
            stats_strategies = _parse_strategy_filter(args.slice_strategies)
            if args.shadow_graduate:
                stats_strategies |= {"shadow_daemon"}
            slice_stats = _load_slice_stats(slice_score_path, allowed_strategies=stats_strategies)

        # Scan
        try:
            report = scanner.scan_report(now_ms=now_ms)
            if args.book_log:
                try:
                    book_path = Path(args.book_log)
                    book_path.parent.mkdir(parents=True, exist_ok=True)
                    with book_path.open("a", encoding="utf-8") as handle:
                        for row in report.get("books") or []:
                            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                except Exception:
                    pass
            candidates: list[dict[str, Any]] = _filter_candidates(
                report.get("candidates") or [],
                symbols=_parse_filter(args.symbols),
                intervals=_parse_filter(args.intervals),
                sides=_parse_filter(args.sides),
                slice_scores=slice_scores,
                scream_promote=args.scream_promote,
                scream_min_edge=args.scream_min_edge,
                scream_intervals=_parse_filter(args.scream_intervals),
            )
            gated: list[dict[str, Any]] = []
            for cand in candidates:
                price = float(cand.get("limit_price") or 0.0)
                edge = float(cand.get("edge") or 0.0)
                if args.min_price > 0 and price < args.min_price:
                    _log(out_path, {
                        "event": "price_blocked", "slug": cand.get("slug"), "side": cand.get("side"),
                        "price": price, "min_price": args.min_price, "ts_ms": now_ms,
                    })
                    continue
                if edge > args.max_edge:
                    _log(out_path, {
                        "event": "edge_blocked", "slug": cand.get("slug"), "side": cand.get("side"),
                        "edge": edge, "max_edge": args.max_edge, "ts_ms": now_ms,
                    })
                    continue
                gated.append(cand)
            candidates = gated
            if feature_provider is not None:
                candidates, score_rejections = _score_candidates(
                    candidates,
                    provider=feature_provider,
                    slice_stats=slice_stats,
                    config=scoring_config,
                    hl_context=load_hl_bot_context(
                        args.hl_bot_status_file,
                        now_ms=now_ms,
                        max_age_ms=args.hl_bot_status_max_age_ms,
                    ) if args.hl_bot_status_file else None,
                )
                for rejection in score_rejections:
                    _log(out_path, {"event": "score_blocked", **rejection, "ts_ms": now_ms})
            candidates, pm_blocked = _polymarket_gate(
                candidates, report.get("books") or [], args.polymarket_gate_threshold
            )
            for cand in pm_blocked:
                _log(out_path, {
                    "event": "polymarket_blocked",
                    "slug": cand.get("slug"),
                    "side": cand.get("side"),
                    "model_prob": cand.get("fair_probability"),
                    "pm_side_prob": cand.get("pm_side_prob"),
                    "threshold": args.polymarket_gate_threshold,
                    "ts_ms": now_ms,
                })
        except Exception as exc:
            _log(out_path, {"event": "scan_error", "error": str(exc), "ts_ms": now_ms})
            print(json.dumps({"event": "scan_error", "error": str(exc)}, sort_keys=True), flush=True)
            if args.iterations and iteration >= args.iterations:
                break
            delay = args.scan_error_backoff_seconds if _is_rate_limited_error(exc) else args.loop_seconds
            time.sleep(max(delay, 1))
            continue

        if not candidates:
            _log(out_path, {"event": "scan_empty", "market_count": report.get("market_count", 0), "ts_ms": now_ms})
            if args.iterations and iteration >= args.iterations:
                break
            time.sleep(max(args.loop_seconds, 1))
            continue

        # Entry throttle: rolling-hour trade cap
        if args.max_trades_per_hour > 0:
            cutoff = now_ms - 3_600_000
            trade_times_ms[:] = [t for t in trade_times_ms if t >= cutoff]
            if len(trade_times_ms) >= args.max_trades_per_hour:
                _log(out_path, {
                    "event": "throttled", "reason": "max_trades_per_hour",
                    "count": len(trade_times_ms), "ts_ms": now_ms,
                })
                if args.iterations and iteration >= args.iterations:
                    break
                time.sleep(max(args.loop_seconds, 1))
                continue

        # Loss cooldown: pause after a streak of resolved losses
        if args.loss_cooldown_losses > 0 and _in_loss_cooldown(
            Path(args.learner_db),
            source_path=str(args.jsonl_out),
            losses=args.loss_cooldown_losses,
            cooldown_ms=args.loss_cooldown_seconds * 1000,
            now_ms=now_ms,
        ):
            _log(out_path, {"event": "throttled", "reason": "loss_cooldown", "ts_ms": now_ms})
            if args.iterations and iteration >= args.iterations:
                break
            time.sleep(max(args.loop_seconds, 1))
            continue

        # One trade per cycle — take the top EV candidate that passes risk gates
        traded = False
        for candidate in candidates:
            ledger = RiskLedger(realized_pnl_usdc=realized_pnl, open_slugs=set(open_slugs))
            decision = risk.can_open(candidate, ledger)
            if not decision.allowed:
                _log(out_path, {
                    "event": "risk_blocked",
                    "reason": decision.reason,
                    "slug": candidate["slug"],
                    "ts_ms": now_ms,
                })
                continue

            # Fetch token IDs and verifying contract for this market
            try:
                details = limitless_client.market_details(candidate["slug"])
            except Exception as exc:
                _log(out_path, {
                    "event": "details_error",
                    "slug": candidate["slug"],
                    "error": str(exc),
                    "ts_ms": now_ms,
                })
                continue

            try:
                runner = _build_runner(args, limitless_client, candidate, details)
            except RuntimeError as exc:
                _log(out_path, {"event": "config_error", "error": str(exc), "ts_ms": now_ms})
                print(json.dumps({"event": "config_error", "error": str(exc)}, sort_keys=True), flush=True)
                running = False
                break

            try:
                result = runner.run(candidate)
            except Exception as exc:
                fatal_error = args.live_armed and args.stop_on_insufficient_collateral and _is_insufficient_collateral_error(exc)
                entry = {
                    "event": "trade_error",
                    "ts_ms": now_ms,
                    "slug": candidate["slug"],
                    "error": str(exc),
                    "fatal": fatal_error,
                }
                _log(out_path, entry)
                print(json.dumps(entry, sort_keys=True), flush=True)
                # Mark slug as open anyway to avoid hammering the same market
                open_slugs.add(candidate["slug"])
                slug_expiry_ms[candidate["slug"]] = now_ms + int(candidate.get("seconds_to_expiry", 3600)) * 1000
                traded = True
                if fatal_error:
                    _log(out_path, {
                        "event": "circuit_breaker",
                        "reason": "insufficient_collateral",
                        "ts_ms": now_ms,
                    })
                    running = False
                break

            entry = {
                "event": "trade",
                "ts_ms": now_ms,
                "mode": mode_label,
                **result.to_dict(),
            }
            _log(out_path, entry)
            print(json.dumps(entry, sort_keys=True), flush=True)

            trade_times_ms.append(now_ms)
            if entry.get("state") == "limitless_unfilled":
                fill_stats["unfilled"] += 1
            else:
                fill_stats["filled"] += 1
            attempts = fill_stats["filled"] + fill_stats["unfilled"]
            if attempts % 10 == 0:
                _log(out_path, {
                    "event": "fill_stats", **fill_stats,
                    "fill_rate": round(fill_stats["filled"] / attempts, 3),
                    "ts_ms": now_ms,
                })

            # Mark this slug as open for the session (prevents duplicate entries)
            expiry_ms = now_ms + int(candidate.get("seconds_to_expiry", 3600)) * 1000
            open_slugs.add(candidate["slug"])
            slug_expiry_ms[candidate["slug"]] = expiry_ms

            traded = True
            break  # one trade per scan cycle

        if not traded:
            _log(out_path, {
                "event": "scan_no_trade",
                "candidate_count": len(candidates),
                "ts_ms": now_ms,
            })

        if args.iterations and iteration >= args.iterations:
            break
        if running:
            time.sleep(max(args.loop_seconds, 1))

    _log(out_path, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})
    print(json.dumps({"event": "shutdown"}, sort_keys=True), flush=True)


def _log(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _in_loss_cooldown(
    db_path: Path,
    *,
    source_path: str,
    losses: int,
    cooldown_ms: int,
    now_ms: int,
) -> bool:
    """True when the last `losses` resolved trades for this jsonl source are all
    losses and the most recent one resolved inside the cooldown window."""
    if losses <= 0 or not db_path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT r.won, r.resolved_at_ms FROM resolutions r "
                "JOIN trades t ON t.trade_key = r.trade_key "
                "WHERE t.source_path = ? ORDER BY r.resolved_at_ms DESC LIMIT ?",
                (source_path, losses),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return False
    if len(rows) < losses or any(row[0] for row in rows):
        return False
    last_loss_ms = max(int(row[1]) for row in rows)
    return now_ms - last_loss_ms < cooldown_ms


def _is_rate_limited_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


def _is_insufficient_collateral_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "insufficient collateral" in text or "insufficient collateral balance" in text


def _load_recent_open_slugs(path: Path, *, now_ms: int) -> tuple[set[str], dict[str, int]]:
    open_slugs: set[str] = set()
    slug_expiry_ms: dict[str, int] = {}
    if not path.exists():
        return open_slugs, slug_expiry_ms
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-500:]
    except Exception:
        return open_slugs, slug_expiry_ms
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("event") != "trade":
            continue
        candidate = payload.get("candidate") or {}
        slug = str(candidate.get("slug") or payload.get("slug") or "")
        if not slug:
            continue
        ts_ms = int(payload.get("ts_ms") or 0)
        seconds = int(candidate.get("seconds_to_expiry") or 0)
        expiry_ms = ts_ms + seconds * 1000
        if ts_ms > 0 and seconds > 0 and expiry_ms > now_ms:
            open_slugs.add(slug)
            slug_expiry_ms[slug] = max(slug_expiry_ms.get(slug, 0), expiry_ms)
    return open_slugs, slug_expiry_ms


def _parse_filter(raw: str) -> set[str]:
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def _parse_strategy_filter(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def _filter_candidates(
    candidates: list[dict[str, Any]],
    *,
    symbols: set[str],
    intervals: set[str],
    sides: set[str],
    slice_scores: set[tuple[str, str, str]] | None = None,
    scream_promote: bool = False,
    scream_min_edge: float = 0.0,
    scream_intervals: set[str] | None = None,
) -> list[dict[str, Any]]:
    out = []
    interval_filter = {item.lower() for item in intervals}
    scream_interval_filter = {item.lower() for item in (scream_intervals or set())}
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").upper()
        interval = str(candidate.get("interval") or "").lower()
        side = str(candidate.get("side") or "").upper()
        if symbols and symbol not in symbols:
            continue
        if interval_filter and interval not in interval_filter:
            continue
        if sides and side not in sides:
            continue
        scream_allowed = (
            scream_promote
            and interval in scream_interval_filter
            and float(candidate.get("edge") or 0.0) >= scream_min_edge
        )
        if slice_scores is not None and (interval, symbol, side) not in slice_scores:
            if not scream_allowed:
                continue
            candidate = dict(candidate)
            candidate["scream_promoted"] = True
        out.append(candidate)
    return out


def _polymarket_gate(
    candidates: list[dict[str, Any]],
    books: list[dict[str, Any]],
    threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop candidates whose model probability is more optimistic than the
    Polymarket twin by more than `threshold` on the candidate's side.

    Polymarket only ever VETOES (model too hot vs the bigger venue); when it is
    more optimistic than us, or has no twin/usable book, the candidate passes.
    Blocked rows come back annotated with pm_side_prob for logging.
    """
    if threshold <= 0:
        return candidates, []
    pm_by_slug = {
        row.get("slug"): row.get("pm_up_prob")
        for row in books
        if row.get("pm_up_prob") is not None
    }
    kept: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for cand in candidates:
        pm_up = pm_by_slug.get(cand.get("slug"))
        if pm_up is None:
            kept.append(cand)
            continue
        side_prob = pm_up if cand.get("side") == "UP" else 1.0 - pm_up
        model_prob = float(cand.get("fair_probability") or 0.0)
        if model_prob - side_prob > threshold:
            blocked.append({**cand, "pm_side_prob": side_prob})
        else:
            kept.append(cand)
    return kept, blocked


def _load_slice_scores(
    path: Path,
    *,
    min_n: int,
    min_roi: float,
    min_win_rate: float,
    live_min_n: int = 0,
    live_min_roi: float = -1.0,
    allowed_strategies: set[str] | None = None,
) -> set[tuple[str, str, str]]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    aggregates: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in payload.get("resolved") or []:
        fill = row.get("fill") or {}
        raw = fill.get("raw") or {}
        if not _strategy_allowed(row, raw, allowed_strategies):
            continue
        interval = str(raw.get("interval") or fill.get("interval") or "").lower()
        symbol = str(fill.get("symbol") or "").upper()
        side = str(fill.get("side") or "").upper()
        if not interval or not symbol or side not in {"UP", "DOWN"}:
            continue
        key = (interval, symbol, side)
        agg = aggregates.setdefault(key, {"n": 0.0, "wins": 0.0, "pnl": 0.0, "stake": 0.0})
        agg["n"] += 1
        agg["wins"] += 1 if row.get("won") else 0
        agg["pnl"] += float(row.get("pnl_usdc") or 0)
        agg["stake"] += float(fill.get("stake_usdc") or 0)
    allowed = set()
    for key, agg in aggregates.items():
        n = int(agg["n"])
        roi = agg["pnl"] / agg["stake"] if agg["stake"] else -1.0
        win_rate = agg["wins"] / agg["n"] if agg["n"] else 0.0
        if n >= min_n and roi >= min_roi and win_rate >= min_win_rate:
            allowed.add(key)
    if live_min_n > 0:
        allowed -= _live_degraded_slices(payload, min_n=live_min_n, min_roi=live_min_roi, allowed_strategies=allowed_strategies)
    return allowed


def _live_degraded_slices(
    payload: dict[str, Any],
    *,
    min_n: int,
    min_roi: float,
    allowed_strategies: set[str] | None = None,
) -> set[tuple[str, str, str]]:
    aggregates: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in payload.get("slices") or []:
        strategy = str(row.get("strategy") or "")
        if allowed_strategies is not None and strategy not in allowed_strategies:
            continue
        interval = str(row.get("interval") or "").lower()
        symbol = str(row.get("symbol") or "").upper()
        side = str(row.get("side") or "").upper()
        if not interval or not symbol or side not in {"UP", "DOWN"}:
            continue
        key = (interval, symbol, side)
        agg = aggregates.setdefault(key, {"n": 0.0, "pnl": 0.0, "stake": 0.0})
        agg["n"] += float(row.get("n") or 0)
        agg["pnl"] += float(row.get("pnl_usdc") or 0)
        agg["stake"] += float(row.get("stake_usdc") or 0)
    degraded = set()
    for key, agg in aggregates.items():
        roi = agg["pnl"] / agg["stake"] if agg["stake"] else -1.0
        if agg["n"] >= min_n and roi < min_roi:
            degraded.add(key)
    return degraded


def _load_slice_stats(path: Path, *, allowed_strategies: set[str] | None = None) -> dict[tuple[str, str, str], SliceStats]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    aggregates: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in payload.get("resolved") or []:
        fill = row.get("fill") or {}
        raw = fill.get("raw") or {}
        if not _strategy_allowed(row, raw, allowed_strategies):
            continue
        interval = str(raw.get("interval") or fill.get("interval") or "").lower()
        symbol = str(fill.get("symbol") or "").upper()
        side = str(fill.get("side") or "").upper()
        if not interval or not symbol or side not in {"UP", "DOWN"}:
            continue
        key = (interval, symbol, side)
        agg = aggregates.setdefault(key, {"n": 0.0, "wins": 0.0, "pnl": 0.0, "stake": 0.0})
        agg["n"] += 1
        agg["wins"] += 1 if row.get("won") else 0
        agg["pnl"] += float(row.get("pnl_usdc") or 0)
        agg["stake"] += float(fill.get("stake_usdc") or 0)
    return {
        key: SliceStats(
            n=int(agg["n"]),
            win_rate=agg["wins"] / agg["n"] if agg["n"] else 0.0,
            roi=agg["pnl"] / agg["stake"] if agg["stake"] else 0.0,
        )
        for key, agg in aggregates.items()
    }


def _strategy_allowed(row: dict[str, Any], raw: dict[str, Any], allowed_strategies: set[str] | None) -> bool:
    if allowed_strategies is None:
        return True
    strategy = str(raw.get("strategy") or "")
    if strategy:
        return strategy in allowed_strategies
    return "seed" in allowed_strategies


def _score_candidates(
    candidates: list[dict[str, Any]],
    *,
    provider: Any,
    slice_stats: dict[tuple[str, str, str], SliceStats],
    config: ScoringConfig,
    hl_context: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        result = score_candidate(
            candidate,
            slice_stats=slice_stats,
            features=provider.features_for(candidate),
            config=config,
            hl_context=hl_context,
        )
        row = dict(candidate)
        row["score"] = result.score
        row["score_reasons"] = result.reasons
        row["score_features"] = result.features
        if result.allowed:
            row["stake_usdc"] = result.stake_usdc
            row["expected_value_usdc"] = float(row.get("edge") or 0.0) * float(row.get("stake_usdc") or 0.0)
            accepted.append(row)
        else:
            rejected.append({
                "slug": row.get("slug"),
                "symbol": row.get("symbol"),
                "interval": row.get("interval"),
                "side": row.get("side"),
                "reason": result.reason,
                "score": result.score,
                "score_reasons": result.reasons,
                "score_features": result.features,
            })
    accepted.sort(key=lambda item: (float(item.get("score") or 0), float(item.get("expected_value_usdc") or 0)), reverse=True)
    return accepted, rejected




if __name__ == "__main__":
    main()
