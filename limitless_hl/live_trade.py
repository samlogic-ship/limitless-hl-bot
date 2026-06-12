from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Protocol

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True, slots=True)
class LimitlessCredentials:
    token_id: str
    token_secret: str


@dataclass(frozen=True, slots=True)
class LimitlessOrderIntent:
    market_slug: str
    token_id: str
    side: Literal["BUY", "SELL"]
    price: float
    size: float
    order_type: Literal["FAK", "FOK", "GTC"]
    verifying_contract: str
    client_order_id: str
    post_only: bool = False


@dataclass(frozen=True, slots=True)
class LimitlessOrderBuilder:
    maker: str
    owner_id: int
    fee_rate_bps: int = 0
    # signature_type: 0 = plain EOA (signer == maker),
    #                 1 = proxy wallet (signer = embedded EOA, maker = proxy contract)
    signature_type: int = 0
    # signer overrides maker in the order message when set (required for proxy/smart wallets)
    signer: str | None = None

    def build_unsigned_payload(
        self,
        intent: LimitlessOrderIntent,
        *,
        salt: int | None = None,
        timestamp_ms: int | None = None,
    ) -> dict[str, Any]:
        if intent.price <= 0 or intent.price >= 1:
            raise ValueError("Limitless binary price must be between 0 and 1")
        if intent.size <= 0:
            raise ValueError("Limitless order size must be positive")
        now_ms = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
        order = {
            "salt": str(salt if salt is not None else now_ms),
            "maker": self.maker,
            "signer": self.signer if self.signer else self.maker,
            "taker": ZERO_ADDRESS,
            "tokenId": str(intent.token_id),
            "makerAmount": _maker_amount(intent),
            "takerAmount": _taker_amount(intent),
            "expiration": "0",
            "nonce": 0,
            "feeRateBps": self.fee_rate_bps,
            "side": 0 if intent.side == "BUY" else 1,
            "signatureType": self.signature_type,
            "price": round(intent.price, 6),
        }
        payload = {
            "order": order,
            "ownerId": self.owner_id,
            "orderType": intent.order_type,
            "marketSlug": intent.market_slug,
            "clientOrderId": intent.client_order_id,
            "timestamp": now_ms,
            "recvWindow": 5000,
        }
        if intent.post_only:
            payload["postOnly"] = True
        return payload

    def sign_payload(self, payload: dict[str, Any], verifying_contract: str, private_key: str) -> dict[str, Any]:
        signed_payload = json.loads(json.dumps(payload))
        # Deep-copy the order before signing: encode_typed_data converts uint256
        # string values (e.g. expiration="0") to integers in-place, which would
        # corrupt the payload we send to the API.
        message = _typed_order_message(copy.deepcopy(signed_payload["order"]), verifying_contract)
        signature = Account.sign_message(encode_typed_data(full_message=message), private_key).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        signed_payload["order"]["signature"] = signature
        return signed_payload


class LimitlessSubmitter:
    def __init__(
        self,
        credentials: LimitlessCredentials,
        builder: LimitlessOrderBuilder,
        private_key: str,
        base_url: str = "https://api.limitless.exchange",
        timeout: int = 15,
    ):
        self.credentials = credentials
        self.builder = builder
        self.private_key = private_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def submit_intent(self, intent: LimitlessOrderIntent) -> dict[str, Any]:
        payload = self.builder.build_unsigned_payload(intent)
        signed = self.builder.sign_payload(payload, intent.verifying_contract, self.private_key)
        body = json.dumps(signed, separators=(",", ":"), sort_keys=True)
        headers = sign_hmac_headers(self.credentials, "POST", "/orders", body)
        response = self.session.post(f"{self.base_url}/orders", data=body, headers=headers, timeout=self.timeout)
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text}
        if response.status_code >= 400:
            raise RuntimeError(f"Limitless order failed {response.status_code}: {data}")
        execution = data.get("execution") if isinstance(data, dict) else {}
        return {
            "submitted": True,
            "matched": bool((execution or {}).get("matched")),
            "filled_usdc": _filled_usdc(execution or {}),
            "raw": data,
        }


class LimitlessLeg(Protocol):
    def submit(self, candidate: dict[str, Any]) -> dict[str, Any]:
        ...


class TradeState(str, Enum):
    LIMITLESS_SUBMITTED = "limitless_submitted"
    LIMITLESS_UNFILLED = "limitless_unfilled"
    LIMITLESS_FILLED_UNHEDGED = "limitless_filled_unhedged"
    HEDGED = "hedged"
    HEDGE_FAILED = "hedge_failed"


@dataclass(frozen=True, slots=True)
class PairTradeResult:
    state: TradeState
    candidate: dict[str, Any]
    limitless_result: dict[str, Any] | None = None
    hedge_result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload


class PairTradeRunner:
    """Limitless-only execution. The Hyperliquid hedge leg was removed 2026-06-12
    (this arb runs unhedged); the result shape is kept for log compatibility."""

    def __init__(self, limitless: LimitlessLeg):
        self.limitless = limitless

    def run(self, candidate: dict[str, Any]) -> PairTradeResult:
        limitless_result = self.limitless.submit(candidate)
        if not bool(limitless_result.get("matched")):
            return PairTradeResult(
                state=TradeState.LIMITLESS_UNFILLED,
                candidate=dict(candidate),
                limitless_result=limitless_result,
            )
        return PairTradeResult(
            state=TradeState.LIMITLESS_FILLED_UNHEDGED,
            candidate=dict(candidate),
            limitless_result=limitless_result,
        )


def estimate_delta_hedge_notional(candidate: dict[str, Any], filled_usdc: float) -> float:
    if filled_usdc <= 0:
        return 0.0
    price = float(candidate.get("limit_price") or 0)
    current_price = float(candidate.get("hyperliquid_mid") or 0)
    threshold_price = float(candidate.get("threshold_price") or 0)
    seconds_to_expiry = int(candidate.get("seconds_to_expiry") or 0)
    annualized_volatility = float(candidate.get("annualized_volatility") or 0.75)
    min_notional = float(candidate.get("min_hedge_notional_usdc") or 11.0)
    max_multiplier = float(candidate.get("max_hedge_delta_multiplier") or 1.0)

    if price <= 0 or current_price <= 0 or threshold_price <= 0 or seconds_to_expiry <= 0:
        return round(filled_usdc, 6)

    years = seconds_to_expiry / (365.0 * 24.0 * 60.0 * 60.0)
    sigma_sqrt_t = max(annualized_volatility, 0.01) * math.sqrt(max(years, 1e-12))
    z = math.log(current_price / threshold_price) / sigma_sqrt_t
    normal_pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    contracts = filled_usdc / price
    raw_notional = contracts * normal_pdf / sigma_sqrt_t
    capped = min(filled_usdc * max(max_multiplier, 0.0), raw_notional)
    if capped <= 0:
        return round(min(filled_usdc * max(max_multiplier, 0.0), max(min_notional, 0.0)), 6)
    return round(max(min_notional, capped), 6)


def candidate_to_limitless_intent(
    candidate: dict[str, Any],
    market_details: dict[str, Any],
    *,
    client_order_id: str,
    order_type: Literal["FAK", "FOK", "GTC"] = "FAK",
) -> LimitlessOrderIntent:
    side = str(candidate.get("side") or "")
    tokens = market_details.get("tokens") or {}
    token_id = tokens.get("yes") if side == "UP" else tokens.get("no")
    venue = market_details.get("venue") or {}
    verifying_contract = str(venue.get("exchange") or "")
    price = float(candidate.get("limit_price") or 0)
    stake = float(candidate.get("stake_usdc") or 0)
    if side not in {"UP", "DOWN"}:
        raise ValueError(f"unsupported candidate side: {side}")
    if not token_id:
        raise ValueError("market details missing yes/no token id")
    if not verifying_contract:
        raise ValueError("market details missing venue.exchange")
    if price <= 0:
        raise ValueError("candidate missing positive limit_price")
    if stake <= 0:
        raise ValueError("candidate missing positive stake_usdc")
    # Round contracts down to the exchange tick.
    # Rule: for a price with N decimal places, contracts (in 1e-6 units) must be
    # a multiple of 10^(N+1).  E.g. price=0.34 → tick=1000 contracts.
    price_str = f"{price:.10f}".rstrip("0")
    decimals = len(price_str.split(".")[1]) if "." in price_str else 0
    contract_tick = 10 ** (decimals + 1)          # tick in 1e-6 contract units
    size_raw_contracts = int(stake / price * 1_000_000)
    size_contracts = (size_raw_contracts // contract_tick) * contract_tick
    size = size_contracts / 1_000_000

    return LimitlessOrderIntent(
        market_slug=str(candidate.get("slug") or market_details.get("slug") or ""),
        token_id=str(token_id),
        side="BUY",
        price=price,
        size=size,
        order_type=order_type,
        verifying_contract=verifying_contract,
        client_order_id=client_order_id,
    )


def sign_hmac_headers(
    credentials: LimitlessCredentials,
    method: str,
    path: str,
    body: str = "",
    timestamp: str | None = None,
) -> dict[str, str]:
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    message = f"{ts}\n{method.upper()}\n{path}\n{body}"
    signature = base64.b64encode(
        hmac.new(base64.b64decode(credentials.token_secret), message.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "lmts-api-key": credentials.token_id,
        "lmts-timestamp": ts,
        "lmts-signature": signature,
    }


def _maker_amount(intent: LimitlessOrderIntent) -> int:
    if intent.order_type == "FOK" and intent.side == "BUY":
        return _scale(intent.size)
    if intent.side == "BUY":
        return _scale(intent.price * intent.size)
    return _scale(intent.size)


def _taker_amount(intent: LimitlessOrderIntent) -> int:
    if intent.order_type == "FOK":
        return 1
    if intent.side == "BUY":
        return _scale(intent.size)
    return _scale(intent.price * intent.size)


def _scale(value: float) -> int:
    return int(round(value * 1_000_000))


def _typed_order_message(order: dict[str, Any], verifying_contract: str) -> dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": "Limitless CTF Exchange",
            "version": "1",
            "chainId": 8453,
            "verifyingContract": verifying_contract,
        },
        "message": order,
    }


def _filled_usdc(execution: dict[str, Any]) -> float:
    totals = execution.get("totalsRaw") if isinstance(execution, dict) else None
    if not isinstance(totals, dict):
        return 0.0
    raw = totals.get("usdGross") or totals.get("usdNet") or 0
    try:
        return float(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0
