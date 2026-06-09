from __future__ import annotations

from pathlib import Path

from limitless_hl.claimer import _load_claimed


def test_load_claimed_ignores_dry_run_claims_and_failed_receipts(tmp_path: Path) -> None:
    claims = tmp_path / "claims.jsonl"
    claims.write_text(
        "\n".join(
            [
                '{"event":"claimed","slug":"dry","dry_run":true}',
                '{"event":"claimed","slug":"failed","status":0}',
                '{"event":"claimed","slug":"ok","status":1}',
                '{"event":"nothing_to_claim","slug":"empty"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert _load_claimed(claims) == {"ok", "empty"}
