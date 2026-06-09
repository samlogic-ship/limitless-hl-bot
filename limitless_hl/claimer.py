"""
claimer.py — Auto-claim resolved Limitless positions on Base.

Scans all JSONL trade logs for filled orders, checks each resolved market via the
Limitless API, then calls redeemPositions on the Gnosis CTF contract on Base to
collect USDC for winning tokens (and drain worthless losing tokens to 0).

Contracts (Base mainnet):
  CTF:   0xC9c98965297Bc527861c898329Ee280632B76e18  (Gnosis Conditional Token Framework)
  USDC:  0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

Usage (dry-run, no on-chain txs):
  python -m limitless_hl.claimer --log-dir tmp/limitless_hl

Usage (live):
  python -m limitless_hl.claimer --live --log-dir tmp/limitless_hl
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from .secrets import get_secret

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------
LIMITLESS_API  = "https://api.limitless.exchange"
BASE_RPC       = "https://mainnet.base.org"
CTF_ADDRESS    = "0xC9c98965297Bc527861c898329Ee280632B76e18"
USDC_ADDRESS   = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ZERO_BYTES32   = b"\x00" * 32

# Gnosis CTF indexSets for binary markets:
#   YES/UP  = outcome 0 → bit 0 → indexSet 1
#   NO/DOWN = outcome 1 → bit 1 → indexSet 2
INDEX_YES = 1
INDEX_NO  = 2

CTF_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _log(path: Path | None, record: dict[str, Any]) -> None:
    line = json.dumps(record, separators=(",", ":"))
    print(line, flush=True)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _market_info(slug: str, session: requests.Session) -> dict[str, Any]:
    r = session.get(f"{LIMITLESS_API}/markets/{slug}", timeout=10)
    r.raise_for_status()
    return r.json()


def _collect_filled_trades(log_dir: Path) -> dict[str, dict[str, Any]]:
    """Return {slug: trade_record} for all filled trades across all JSONL logs.

    Handles two event formats:
      - funding_daemon: event=trade, result.matched=true, slug at top level
      - daemon (S1):    event=trade, limitless_result.matched=true, slug in candidate
    """
    filled: dict[str, dict[str, Any]] = {}
    for jsonl_path in log_dir.glob("*.jsonl"):
        try:
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") != "trade":
                    continue

                # Format A: funding_daemon — result.matched
                result_a = rec.get("result") or {}
                matched_a = bool(result_a.get("matched"))

                # Format B: daemon (S1) — limitless_result.matched
                result_b = rec.get("limitless_result") or {}
                matched_b = bool(result_b.get("matched")) or float(result_b.get("filled_usdc") or 0) > 0

                if not (matched_a or matched_b):
                    continue

                # Slug: top-level first, fallback to candidate
                slug = str(rec.get("slug") or (rec.get("candidate") or {}).get("slug") or "")
                if not slug:
                    continue

                # Keep earliest fill per slug (don't overwrite)
                if slug not in filled:
                    filled[slug] = rec
        except Exception:
            continue
    return filled


def _load_claimed(claims_path: Path) -> set[str]:
    claimed: set[str] = set()
    if not claims_path.exists():
        return claimed
    for line in claims_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            slug = str(rec.get("slug") or "")
            event = rec.get("event")
            live_claimed = event == "claimed" and not rec.get("dry_run") and int(rec.get("status") or 0) == 1
            empty = event == "nothing_to_claim"
            if (live_claimed or empty) and slug:
                claimed.add(slug)
        except Exception:
            continue
    return claimed


# ------------------------------------------------------------------
# Core claim logic
# ------------------------------------------------------------------
def try_claim(
    slug: str,
    trade: dict[str, Any],
    w3,  # web3.Web3
    ctf,  # contract instance
    account,  # eth_account.Account
    maker_address: str,
    live: bool,
    session: requests.Session,
    out_path: Path | None,
    now_ms: int,
) -> str:
    """
    Returns: 'claimed', 'nothing_to_claim', 'not_resolved', 'error'
    """
    try:
        info = _market_info(slug, session)
    except Exception as exc:
        _log(out_path, {"event": "claim_error", "slug": slug, "error": str(exc), "ts_ms": now_ms})
        return "error"

    status = str(info.get("status") or "")
    winning_idx = info.get("winningOutcomeIndex")

    if status != "RESOLVED" or winning_idx not in (0, 1):
        return "not_resolved"

    # Check balance of both position tokens
    condition_id_hex = str(info.get("conditionId") or "")
    if not condition_id_hex:
        _log(out_path, {"event": "claim_error", "slug": slug, "error": "no conditionId", "ts_ms": now_ms})
        return "error"

    tokens = info.get("tokens") or {}
    yes_id = tokens.get("yes")
    no_id  = tokens.get("no")

    try:
        yes_balance = ctf.functions.balanceOf(maker_address, int(yes_id)).call() if yes_id else 0
        no_balance  = ctf.functions.balanceOf(maker_address, int(no_id)).call()  if no_id  else 0
    except Exception as exc:
        _log(out_path, {"event": "claim_error", "slug": slug, "error": f"balanceOf: {exc}", "ts_ms": now_ms})
        return "error"

    total_balance = yes_balance + no_balance
    if total_balance == 0:
        _log(out_path, {
            "event": "nothing_to_claim", "slug": slug,
            "winning_outcome": winning_idx, "yes_balance": yes_balance, "no_balance": no_balance,
            "ts_ms": now_ms,
        })
        return "nothing_to_claim"

    # Determine which indexSets to redeem
    index_sets = []
    if yes_balance > 0:
        index_sets.append(INDEX_YES)
    if no_balance > 0:
        index_sets.append(INDEX_NO)

    # Winning amount (in 1e-6 USDC)
    winning_balance = yes_balance if winning_idx == 0 else no_balance
    payout_usdc = winning_balance / 1_000_000

    _log(out_path, {
        "event": "claim_attempt", "slug": slug,
        "winning_outcome": winning_idx,
        "yes_balance": yes_balance, "no_balance": no_balance,
        "expected_payout_usdc": round(payout_usdc, 6),
        "index_sets": index_sets,
        "live": live,
        "ts_ms": now_ms,
    })

    if not live:
        _log(out_path, {
            "event": "claimed", "slug": slug, "dry_run": True,
            "expected_payout_usdc": round(payout_usdc, 6),
            "ts_ms": now_ms,
        })
        return "claimed"

    # On-chain redeem
    try:
        from web3 import Web3

        condition_id_bytes = bytes.fromhex(condition_id_hex.removeprefix("0x"))
        collateral = Web3.to_checksum_address(USDC_ADDRESS)

        tx = ctf.functions.redeemPositions(
            collateral,
            ZERO_BYTES32,
            condition_id_bytes,
            index_sets,
        ).build_transaction({
            "from": maker_address,
            "nonce": w3.eth.get_transaction_count(maker_address),
            "gas": 200_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei("0.01", "gwei"),
            "chainId": 8453,
        })

        private_key = get_secret("LIMITLESS_PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("LIMITLESS_PRIVATE_KEY not found in keychain/env")

        signed = account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if int(receipt.status) != 1:
            _log(out_path, {
                "event": "claim_error", "slug": slug,
                "tx_hash": tx_hash.hex(),
                "gas_used": receipt.gasUsed,
                "status": receipt.status,
                "error": "receipt_status_not_success",
                "ts_ms": now_ms,
            })
            return "error"

        _log(out_path, {
            "event": "claimed", "slug": slug,
            "tx_hash": tx_hash.hex(),
            "gas_used": receipt.gasUsed,
            "expected_payout_usdc": round(payout_usdc, 6),
            "status": receipt.status,
            "ts_ms": now_ms,
        })
        return "claimed"

    except Exception as exc:
        _log(out_path, {"event": "claim_error", "slug": slug, "error": str(exc), "ts_ms": now_ms})
        return "error"


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Auto-claim resolved Limitless positions")
    ap.add_argument("--live", action="store_true", help="Submit real on-chain redeem transactions")
    ap.add_argument("--log-dir", default="tmp/limitless_hl", help="Directory containing JSONL trade logs")
    ap.add_argument("--loop-seconds", type=int, default=60, help="Scan interval in seconds")
    ap.add_argument("--jsonl-out", default=None, help="Path for claims log (defaults to log-dir/claims.jsonl)")
    args = ap.parse_args()

    log_dir = Path(args.log_dir)
    out_path = Path(args.jsonl_out) if args.jsonl_out else log_dir / "claims.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    now_ms = int(time.time() * 1000)
    _log(out_path, {"event": "startup", "live": args.live, "ts_ms": now_ms})

    # Lazy-import web3 only when needed
    w3 = None
    ctf = None
    account = None
    maker_address = None

    if args.live:
        try:
            from web3 import Web3
            from eth_account import Account

            w3 = Web3(Web3.HTTPProvider(BASE_RPC))
            if not w3.is_connected():
                raise RuntimeError(f"Cannot connect to Base RPC: {BASE_RPC}")

            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_ABI,
            )

            private_key = get_secret("LIMITLESS_PRIVATE_KEY")
            if not private_key:
                raise RuntimeError("LIMITLESS_PRIVATE_KEY not found — store in keychain or env")
            acct = Account.from_key(private_key)
            account = Account
            maker_address = Web3.to_checksum_address(acct.address)
            _log(out_path, {"event": "wallet_ready", "address": maker_address, "ts_ms": now_ms})
        except Exception as exc:
            _log(out_path, {"event": "startup_error", "error": str(exc), "ts_ms": now_ms})
            raise
    else:
        # Dry-run: still need address for balance checks
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(BASE_RPC))
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_ABI,
            )
            # Derive address from keychain key if available, fallback to env
            private_key = get_secret("LIMITLESS_PRIVATE_KEY")
            if private_key:
                from eth_account import Account as _Acct
                maker_address = Web3.to_checksum_address(_Acct.from_key(private_key).address)
            else:
                # Fallback: use the known maker address from ecosystem config
                maker_address = Web3.to_checksum_address(
                    os.environ.get("LIMITLESS_MAKER_ADDRESS", "0xBA240843fdf7EF02fb89832D84325B1488e0646f")
                )
            account = None
        except Exception as exc:
            _log(out_path, {"event": "startup_warning", "error": str(exc), "ts_ms": now_ms})
            # Continue without on-chain balance checks in dry-run
            w3 = None
            ctf = None
            maker_address = "0xBA240843fdf7EF02fb89832D84325B1488e0646f"

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    while True:
        try:
            now_ms = int(time.time() * 1000)
            filled = _collect_filled_trades(log_dir)
            claimed = _load_claimed(out_path)
            pending = {slug: trade for slug, trade in filled.items() if slug not in claimed}

            _log(out_path, {
                "event": "scan",
                "filled_total": len(filled),
                "already_claimed": len(claimed),
                "pending": len(pending),
                "ts_ms": now_ms,
            })

            for slug, trade in pending.items():
                result = try_claim(
                    slug=slug,
                    trade=trade,
                    w3=w3,
                    ctf=ctf,
                    account=account,
                    maker_address=maker_address,
                    live=args.live,
                    session=session,
                    out_path=out_path,
                    now_ms=now_ms,
                )
                # Brief pause between claims to avoid rate limits
                if result in ("claimed", "error"):
                    time.sleep(2)

        except KeyboardInterrupt:
            _log(out_path, {"event": "shutdown", "ts_ms": int(time.time() * 1000)})
            return
        except Exception as exc:
            _log(out_path, {"event": "loop_error", "error": str(exc), "ts_ms": int(time.time() * 1000)})

        time.sleep(args.loop_seconds)


if __name__ == "__main__":
    main()
