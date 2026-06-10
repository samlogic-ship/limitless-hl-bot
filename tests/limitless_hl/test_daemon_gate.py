from limitless_hl.daemon import _polymarket_gate


def _cand(slug, side, prob):
    return {"slug": slug, "side": side, "fair_probability": prob}


def _book(slug, pm_up):
    return {"slug": slug, "pm_up_prob": pm_up}


def test_gate_disabled_passes_everything():
    kept, blocked = _polymarket_gate([_cand("a", "UP", 0.95)], [_book("a", 0.40)], 0.0)
    assert len(kept) == 1 and blocked == []


def test_gate_blocks_model_hotter_than_polymarket():
    kept, blocked = _polymarket_gate([_cand("a", "UP", 0.70)], [_book("a", 0.50)], 0.10)
    assert kept == [] and len(blocked) == 1
    assert abs(blocked[0]["pm_side_prob"] - 0.50) < 1e-9


def test_gate_passes_agreement():
    kept, blocked = _polymarket_gate([_cand("a", "UP", 0.55)], [_book("a", 0.50)], 0.10)
    assert len(kept) == 1 and blocked == []


def test_gate_handles_down_side():
    # DOWN candidate at 0.80 vs Polymarket P(down) = 1 - 0.45 = 0.55 -> 0.25 gap
    kept, blocked = _polymarket_gate([_cand("a", "DOWN", 0.80)], [_book("a", 0.45)], 0.10)
    assert kept == [] and len(blocked) == 1


def test_gate_never_blocks_when_polymarket_more_optimistic():
    kept, blocked = _polymarket_gate([_cand("a", "UP", 0.55)], [_book("a", 0.75)], 0.10)
    assert len(kept) == 1 and blocked == []


def test_gate_passes_markets_without_twin():
    kept, blocked = _polymarket_gate([_cand("a", "UP", 0.99)], [], 0.10)
    assert len(kept) == 1 and blocked == []
