"""Validated entry-price gating: cut <min longshots and the coinflip band.

Backed by 723 resolved fills (scored_daemon + shadow_daemon): every low-score
bucket loses, and within score>=2.0 only price in [0.35,0.45) and [0.55,0.65)
are net positive. <0.35, [0.45,0.55), and >=0.65 bleed to the 3% taker fee.
"""
from limitless_hl.daemon import _price_block_reason, build_parser


def test_below_min_price_blocked():
    assert _price_block_reason(0.30, 0.35, None) == "below_min_price"
    assert _price_block_reason(0.34999, 0.35, None) == "below_min_price"


def test_at_or_above_min_price_allowed():
    assert _price_block_reason(0.35, 0.35, None) is None
    assert _price_block_reason(0.42, 0.35, None) is None


def test_exclude_band_blocks_coinflip_middle():
    band = [0.45, 0.55]
    assert _price_block_reason(0.45, 0.35, band) == "in_exclude_band"   # LO inclusive
    assert _price_block_reason(0.50, 0.35, band) == "in_exclude_band"
    assert _price_block_reason(0.549, 0.35, band) == "in_exclude_band"


def test_exclude_band_boundaries_tradeable():
    band = [0.45, 0.55]
    assert _price_block_reason(0.44, 0.35, band) is None   # just below band -> keep
    assert _price_block_reason(0.55, 0.35, band) is None   # HI exclusive -> keep


def test_min_price_takes_precedence_in_band():
    # price below min is blocked for min reason regardless of band
    assert _price_block_reason(0.20, 0.35, [0.45, 0.55]) == "below_min_price"


def test_no_gates_allows_everything():
    assert _price_block_reason(0.10, 0.0, None) is None


def test_parser_accepts_exclude_price_band():
    args = build_parser().parse_args(
        ["--min-price", "0.35", "--max-price", "0.65", "--exclude-price-band", "0.45", "0.55"]
    )
    assert args.exclude_price_band == [0.45, 0.55]
    assert args.min_price == 0.35
