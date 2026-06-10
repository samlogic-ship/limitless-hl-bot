import json

from limitless_hl.polymarket_feed import PolymarketFeed, pm_slug


def test_pm_slug_15m_all_symbols_on_grid():
    # 15m twins exist for every Limitless symbol (verified live 2026-06-10)
    exp_ms = 1_781_051_400_000  # divisible by 900s grid
    for sym in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"):
        slug = pm_slug(sym, "15m", exp_ms)
        assert slug == f"{sym.lower()}-updown-15m-{exp_ms // 1000 - 900}"


def test_pm_slug_5m_subset_only():
    exp_ms = 1_781_051_400_000
    assert pm_slug("BTC", "5m", exp_ms) == f"btc-updown-5m-{exp_ms // 1000 - 300}"
    assert pm_slug("DOGE", "5m", exp_ms) is None  # no 5m twin for DOGE
    assert pm_slug("HYPE", "5m", exp_ms) is None


def test_pm_slug_rejects_off_grid_and_unknown():
    assert pm_slug("BTC", "15m", 1_781_051_400_000 + 37_000) is None  # off the 900s grid
    assert pm_slug("BTC", "1h", 1_781_051_400_000) is None  # no hourly twins
    assert pm_slug("PEPE", "15m", 1_781_051_400_000) is None


class _StubFeed(PolymarketFeed):
    """Feed with canned gamma/CLOB responses (captured live 2026-06-10)."""

    def __init__(self, book, outcomes='["Up", "Down"]', **kwargs):
        super().__init__(**kwargs)
        self._book = book
        self._outcomes = outcomes

    def _get(self, url):
        if "gamma" in url:
            return [{
                "markets": [{
                    "clobTokenIds": json.dumps(["111", "222"]),
                    "outcomes": self._outcomes,
                }],
            }]
        return self._book


def test_feed_reads_up_token_mid():
    feed = _StubFeed(book={
        "bids": [{"price": "0.47", "size": "10"}, {"price": "0.48", "size": "5"}],
        "asks": [{"price": "0.52", "size": "10"}, {"price": "0.50", "size": "5"}],
    })
    out = feed.implied_up_prob("BTC", "15m", 1_781_051_400_000)
    assert out is not None
    assert abs(out["up_prob"] - 0.49) < 1e-9  # (0.48 + 0.50) / 2
    assert abs(out["spread"] - 0.02) < 1e-9


def test_feed_handles_reversed_outcome_order():
    feed = _StubFeed(
        book={"bids": [{"price": "0.40"}], "asks": [{"price": "0.44"}]},
        outcomes='["Down", "Up"]',
    )
    # Up sits at index 1 -> token "222"; stub serves the same book either way,
    # the point is it does not crash and still produces a mid.
    out = feed.implied_up_prob("ETH", "15m", 1_781_051_400_000)
    assert out is not None and abs(out["up_prob"] - 0.42) < 1e-9


def test_feed_rejects_wide_or_empty_books():
    wide = _StubFeed(book={"bids": [{"price": "0.10"}], "asks": [{"price": "0.90"}]})
    assert wide.implied_up_prob("BTC", "15m", 1_781_051_400_000) is None
    empty = _StubFeed(book={"bids": [], "asks": [{"price": "0.5"}]})
    assert empty.implied_up_prob("BTC", "15m", 1_781_051_400_000) is None


def test_feed_returns_none_when_gamma_down():
    class _Down(PolymarketFeed):
        def _get(self, url):
            return None

    assert _Down().implied_up_prob("BTC", "15m", 1_781_051_400_000) is None
