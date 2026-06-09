from __future__ import annotations

from limitless_hl.clients import HyperliquidClient, LimitlessClient
from limitless_hl.model import EdgeConfig
from limitless_hl.scanner import LimitlessHyperliquidScanner


def test_limitless_client_parses_crypto_market_payload_and_orderbook() -> None:
    client = LimitlessClient(base_url="https://example.test")
    payload = {
        "data": [
            {
                "title": "BTC Up or Down - Hourly",
                "slug": "btc-up-or-down-hourly-1",
                "tradeType": "clob",
                "volumeFormatted": "131.5",
                "expirationTimestamp": 1_000_000,
                "metadata": {
                    "openPrice": "62535.68448145",
                    "openPriceCapturedAt": "2026-06-09T01:00:00.000Z",
                },
            },
            {"title": "NBA Finals", "slug": "sports", "tradeType": "clob", "metadata": {}},
        ]
    }
    orderbook_payload = {
        "bids": [{"price": 0.71, "size": 600_000_000}],
        "asks": [{"price": 0.77, "size": 300_000_000}],
    }

    markets = client.parse_active_markets(payload)
    book = client.parse_orderbook(orderbook_payload)

    assert [market.slug for market in markets] == ["btc-up-or-down-hourly-1"]
    assert markets[0].symbol == "BTC"
    assert markets[0].interval == "1h"
    assert markets[0].threshold_price == 62535.68448145
    assert book.up_bid == 0.71
    assert book.up_ask == 0.77
    assert book.up_ask_size == 300.0
    assert book.down_bid == 0.23
    assert book.down_ask == 0.29


def test_hyperliquid_client_parses_all_mids_for_supported_symbols() -> None:
    mids = HyperliquidClient.parse_all_mids({"BTC": "62789.5", "ETH": "1671.5", "PURR": "0.12"})

    assert mids == {"BTC": 62789.5, "ETH": 1671.5, "PURR": 0.12}


def test_scanner_emits_ranked_candidates_from_clients() -> None:
    class FakeLimitless:
        def active_crypto_markets(self):
            return [
                LimitlessClient.parse_market(
                    {
                        "title": "BTC Up or Down - Hourly",
                        "slug": "btc-up-or-down-hourly-1",
                        "tradeType": "clob",
                        "volumeFormatted": "1000",
                        "expirationTimestamp": 1_000_900_000,
                        "metadata": {"openPrice": "100", "openPriceCapturedAt": "2026-06-09T01:00:00.000Z"},
                    }
                )
            ]

        def orderbook(self, slug: str):
            assert slug == "btc-up-or-down-hourly-1"
            return LimitlessClient.parse_orderbook(
                {"bids": [{"price": 0.65, "size": 300_000_000}], "asks": [{"price": 0.70, "size": 300_000_000}]}
            )

    class FakeHyperliquid:
        def all_mids(self):
            return {"BTC": 101.0}

    scanner = LimitlessHyperliquidScanner(
        limitless=FakeLimitless(),
        hyperliquid=FakeHyperliquid(),
        config=EdgeConfig(min_edge=0.03, annualized_volatility=0.80),
    )

    candidates = scanner.scan(now_ms=1_000_000_000)

    assert len(candidates) == 1
    assert candidates[0].symbol == "BTC"
    assert candidates[0].edge > 0.03


def test_scanner_diagnostics_reports_rejected_markets() -> None:
    class FakeLimitless:
        def active_crypto_markets(self):
            return [
                LimitlessClient.parse_market(
                    {
                        "title": "BTC Up or Down - Hourly",
                        "slug": "btc-up-or-down-hourly-1",
                        "tradeType": "clob",
                        "volumeFormatted": "1000",
                        "expirationTimestamp": 1_000_900_000,
                        "metadata": {"openPrice": "100", "openPriceCapturedAt": "2026-06-09T01:00:00.000Z"},
                    }
                )
            ]

        def orderbook(self, slug: str):
            return LimitlessClient.parse_orderbook(
                {"bids": [{"price": 0.49, "size": 1_000_000}], "asks": [{"price": 0.51, "size": 1_000_000}]}
            )

    class FakeHyperliquid:
        def all_mids(self):
            return {"BTC": 100.0}

    scanner = LimitlessHyperliquidScanner(
        limitless=FakeLimitless(),
        hyperliquid=FakeHyperliquid(),
        config=EdgeConfig(min_edge=0.20, annualized_volatility=0.80, min_size_usdc=25),
    )

    report = scanner.scan_report(now_ms=1_000_000_000)

    assert report["market_count"] == 1
    assert report["candidate_count"] == 0
    assert report["rejected_count"] == 1
    assert report["rejections"][0]["reason"] == "no_positive_edge"
