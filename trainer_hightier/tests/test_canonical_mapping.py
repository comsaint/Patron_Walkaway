"""Canonical mapping from cleaned session Parquet (D2 parity helpers)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import CanonicalMappingConfig, DuckDbRuntimeConfig
from trainer_hightier.utils.canonical_mapping import (
    build_canonical_mapping_from_cleaned_session_parquet,
    canonical_mapping_content_fingerprint,
)
from trainer_hightier.utils.source_manifest_v2 import sha256_file_bytes


def _base_row(**kwargs):
    ws = datetime(2024, 1, 10, 12, 0, 0)
    d = dict(
        session_id=0,
        player_id=0,
        casino_player_id="x",
        lud_dtm=ws,
        session_start_dtm=ws,
        session_end_dtm=ws,
        is_manual=0,
        is_deleted=0,
        is_canceled=0,
        num_games_with_wager=2,
        turnover=10.0,
    )
    d.update(kwargs)
    return d


def test_canonical_mapping_card_swap_latest_wins_and_dummy_excluded(tmp_path) -> None:
    """Case 2: same player_id → two casino_player_id; keep latest lud_dtm. FND-12 excluded."""
    t0 = datetime(2024, 1, 5, 0, 0, 0)
    t1 = datetime(2024, 3, 1, 0, 0, 0)
    rows = [
        _base_row(session_id=501, player_id=900, casino_player_id="card_old", lud_dtm=t0, session_end_dtm=t0),
        _base_row(session_id=502, player_id=900, casino_player_id="card_new", lud_dtm=t1, session_end_dtm=t1),
        _base_row(
            session_id=503,
            player_id=901,
            casino_player_id="solo",
            lud_dtm=t0,
            session_end_dtm=t0,
            num_games_with_wager=1,
        ),
    ]
    pq_path = tmp_path / "cleaned.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), pq_path)

    out_dir = tmp_path / "mapping"
    out_pq = out_dir / "canonical_player_mapping.parquet"
    out_js = out_dir / "canonical_mapping_meta.json"
    cfg = CanonicalMappingConfig(cutoff_dtm=datetime(2024, 12, 31))
    build_canonical_mapping_from_cleaned_session_parquet(
        pq_path,
        cfg=cfg,
        duckdb_runtime=DuckDbRuntimeConfig(),
        output_parquet=out_pq,
        output_sidecar=out_js,
        duckdb_join_timeout_s=120.0,
    )
    mp = pd.read_parquet(out_pq)
    assert len(mp) == 1
    assert int(mp["player_id"].iloc[0]) == 900
    assert str(mp["canonical_id"].iloc[0]) == "card_new"
    assert "casino_player_id" in mp.columns
    assert str(mp["casino_player_id"].iloc[0]) == "card_new"
    assert out_js.is_file()
    assert mp["player_id"].is_monotonic_increasing


def test_canonical_mapping_content_fingerprint_invariant_to_row_order(tmp_path) -> None:
    """Content fingerprint matches when only parquet row order differs."""
    base = tmp_path / "base.parquet"
    shuf = tmp_path / "shuf.parquet"
    pq.write_table(
        pa.table(
            {
                "player_id": [1, 2, 3],
                "canonical_id": ["c1", "c2", "c3"],
                "casino_player_id": ["c1", "c2", "c3"],
            }
        ),
        base,
    )
    pq.write_table(
        pa.table(
            {
                "player_id": [3, 1, 2],
                "canonical_id": ["c3", "c1", "c2"],
                "casino_player_id": ["c3", "c1", "c2"],
            }
        ),
        shuf,
    )
    assert canonical_mapping_content_fingerprint(base) == canonical_mapping_content_fingerprint(shuf)
    assert sha256_file_bytes(base) != sha256_file_bytes(shuf)
