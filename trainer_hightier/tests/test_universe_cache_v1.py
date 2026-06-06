"""Tests for ADT rank universe cache (L2)."""

from __future__ import annotations

import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.universe_cache_v1 import (
    adt_rank_cache_is_hit,
    diff_selected_universe_added_player_ids,
    materialize_adt_rank_table_v1_cached,
    write_selected_universe_manifest,
)


def _write_profile_csv(path: Path) -> None:
    path.write_text(
        "canonical_id,adt\n"
        "c_low,10.0\n"
        "c_mid,50.0\n"
        "c_high,90.0\n",
        encoding="utf-8",
    )


def _write_mapping_parquet(path: Path) -> None:
    buf = io.BytesIO()
    pq.write_table(
        pa.table(
            {
                "player_id": [1, 2, 3, 4],
                "canonical_id": ["c_low", "c_mid", "c_mid", "c_high"],
            }
        ),
        buf,
    )
    path.write_bytes(buf.getvalue())


def test_materialize_adt_rank_table_builds_and_hits_cache(tmp_path: Path) -> None:
    profile = tmp_path / "profile.csv"
    mapping = tmp_path / "mapping.parquet"
    cache = tmp_path / "cache"
    _write_profile_csv(profile)
    _write_mapping_parquet(mapping)
    duck = DuckDbRuntimeConfig()
    first = materialize_adt_rank_table_v1_cached(
        patron_profile_csv=profile,
        canonical_mapping_parquet=mapping,
        duckdb_runtime=duck,
        cache_root=cache,
        cleaned_session_parquet=None,
        slow_active_anchor=None,
    )
    assert first["universe_adt_rank_cache_hit"] is False
    assert first["universe_adt_rank_player_projection_count"] == 4
    assert first["universe_adt_rank_canonical_count"] == 3
    assert Path(str(first["universe_adt_rank_table_path"])).is_file()
    second = materialize_adt_rank_table_v1_cached(
        patron_profile_csv=profile,
        canonical_mapping_parquet=mapping,
        duckdb_runtime=duck,
        cache_root=cache,
        cleaned_session_parquet=None,
        slow_active_anchor=None,
    )
    assert second["universe_adt_rank_cache_hit"] is True
    assert adt_rank_cache_is_hit(
        manifest_path=Path(str(first["universe_adt_rank_manifest_path"])),
        data_path=Path(str(first["universe_adt_rank_table_path"])),
        profile_sha256=str(first["universe_profile_snapshot_sha256"]),
        mapping_sha256=str(first["universe_mapping_sha256"]),
        slow_anchor=str(first["universe_slow_anchor_required"]),
    )


def test_write_selected_universe_manifest_counts(tmp_path: Path) -> None:
    profile = tmp_path / "profile.csv"
    mapping = tmp_path / "mapping.parquet"
    cache = tmp_path / "cache"
    _write_profile_csv(profile)
    _write_mapping_parquet(mapping)
    duck = DuckDbRuntimeConfig()
    rank_meta = materialize_adt_rank_table_v1_cached(
        patron_profile_csv=profile,
        canonical_mapping_parquet=mapping,
        duckdb_runtime=duck,
        cache_root=cache,
    )
    sel = write_selected_universe_manifest(
        rank_table_path=Path(str(rank_meta["universe_adt_rank_table_path"])),
        quantile=0.5,
        rank_fingerprint_sha256_hex=str(rank_meta["universe_adt_rank_fingerprint_sha256_hex"]),
        cache_root=cache,
    )
    assert sel["selected_universe_player_count"] == 0
    assert sel["selected_universe_canonical_count"] == 0
    assert Path(str(sel["selected_universe_manifest_path"])).is_file()


def test_diff_selected_universe_added_player_ids_on_quantile_decrease(tmp_path: Path) -> None:
    rank_p = tmp_path / "rank.parquet"
    pq.write_table(
        pa.table(
            {
                "canonical_id": ["c1", "c2", "c3", "c4"],
                "player_id": [10, 20, 30, 40],
                "adt": [10.0, 40.0, 60.0, 90.0],
                "adt_rank": [1, 2, 3, 4],
                "adt_percentile": [0.0, 0.33, 0.66, 1.0],
                "has_slow_window_coverage": [True, True, True, True],
            }
        ),
        rank_p,
    )
    added = diff_selected_universe_added_player_ids(rank_p, previous_quantile=0.99, current_quantile=0.5)
    assert added == (30,)
    added_wide = diff_selected_universe_added_player_ids(rank_p, previous_quantile=0.99, current_quantile=0.25)
    assert added_wide == (20, 30)
    assert diff_selected_universe_added_player_ids(rank_p, previous_quantile=0.5, current_quantile=0.99) == ()


def test_write_selected_universe_manifest_rejects_bad_quantile(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="quantile must be strictly between"):
        write_selected_universe_manifest(
            rank_table_path=tmp_path / "missing.parquet",
            quantile=1.0,
            rank_fingerprint_sha256_hex="abc",
            cache_root=tmp_path,
        )
