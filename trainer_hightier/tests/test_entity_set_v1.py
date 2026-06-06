"""Tests for training entity set v1 (ADT universe projection)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import (
    BetPreprocessConfig,
    DuckDbRuntimeConfig,
    L0PreprocessDataScopeConfig,
    TRAINING_DATA_SCOPE_TEST_UNBOUNDED,
)
from trainer_hightier.utils.bet_l0_preprocess import (
    bet_clean_cache_manifest_path,
    default_preprocess_registry_yaml_path,
    segment_cleaned_bet_from_base_parquet,
)
from trainer_hightier.utils.entity_set_v1 import (
    materialize_entity_set_v1_cached,
    retire_bet_segment_cache_sidecar,
    training_scope_fingerprint,
)
from trainer_hightier.utils.patron_session_metrics import materialize_adt_allowed_players_parquet
from trainer_hightier.tests.test_bet_preprocess import _bet_row, read_cleaned_bet_dataset
from trainer_hightier.utils.source_manifest_v2 import sha256_file_bytes

_hpre = importlib.import_module("trainer_hightier.02_preprocess")


def _write_rank_table(path: Path) -> None:
    pq.write_table(
        pa.table(
            {
                "canonical_id": ["vip", "c1"],
                "player_id": [100, 200],
                "adt": [1_000_000.0, 1.0],
                "adt_rank": [2, 1],
                "adt_percentile": [1.0, 0.0],
                "has_slow_window_coverage": [True, True],
            }
        ),
        path,
    )


def test_entity_set_matches_legacy_segment_row_set(tmp_path: Path) -> None:
    """Entity set v1 projection row set aligns with legacy allowlist segment (P2-T-5)."""
    profile_csv = tmp_path / "canonical_patron_profile.csv"
    mapping_pq = tmp_path / "canonical_mapping.parquet"
    pd.DataFrame(
        [{"canonical_id": f"c{i}", "adt": float(i)} for i in range(1, 51)]
        + [{"canonical_id": "vip", "adt": 1_000_000.0}]
    ).to_csv(profile_csv, index=False)
    pd.DataFrame(
        [{"player_id": 100, "canonical_id": "vip"}, {"player_id": 200, "canonical_id": "c1"}]
    ).to_parquet(mapping_pq)
    allowed_pq = tmp_path / "adt_allowed.parquet"
    materialize_adt_allowed_players_parquet(
        profile_csv,
        mapping_pq,
        quantile=0.99,
        duckdb_runtime=DuckDbRuntimeConfig(),
        output_parquet=allowed_pq,
    )
    rank_pq = tmp_path / "rank.parquet"
    _write_rank_table(rank_pq)
    rank_fp = sha256_file_bytes(rank_pq)
    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    df = pd.DataFrame(
        [
            _bet_row(bet_id=1, player_id=100, payout_complete_dtm=t_pay),
            _bet_row(bet_id=2, player_id=200, payout_complete_dtm=t_pay),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    base = tmp_path / "base_ds"
    registry = default_preprocess_registry_yaml_path()
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        base,
        cfg=BetPreprocessConfig(
            data_scope=L0PreprocessDataScopeConfig(),
            preprocess_registry_yaml=registry,
            dedup_hash_buckets=1,
        ),
    )
    legacy_out = tmp_path / "legacy_seg"
    segment_cleaned_bet_from_base_parquet(
        base,
        allowed_pq,
        legacy_out,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    entity_out = tmp_path / "entity_out"
    scope = TRAINING_DATA_SCOPE_TEST_UNBOUNDED
    source_fp = "abc123" * 10 + "abcd"
    first = materialize_entity_set_v1_cached(
        base_cleaned_parquet=base,
        rank_table_path=rank_pq,
        rank_fingerprint_sha256_hex=rank_fp,
        selected_quantile=0.99,
        training_scope=scope,
        source_manifest_v2_fingerprint_sha256_hex=source_fp,
        duckdb_runtime=DuckDbRuntimeConfig(),
        cache_root=tmp_path / "cache",
        output_parquet=entity_out,
        use_cache=False,
    )
    legacy_rows = read_cleaned_bet_dataset(legacy_out)
    entity_rows = read_cleaned_bet_dataset(entity_out)
    assert len(legacy_rows) == len(entity_rows) == 1
    assert int(legacy_rows.iloc[0]["player_id"]) == int(entity_rows.iloc[0]["player_id"]) == 100
    assert first["entity_set_cache_hit"] is False
    second = materialize_entity_set_v1_cached(
        base_cleaned_parquet=base,
        rank_table_path=rank_pq,
        rank_fingerprint_sha256_hex=rank_fp,
        selected_quantile=0.99,
        training_scope=scope,
        source_manifest_v2_fingerprint_sha256_hex=source_fp,
        duckdb_runtime=DuckDbRuntimeConfig(),
        cache_root=tmp_path / "cache",
        output_parquet=entity_out,
        use_cache=True,
    )
    assert second["entity_set_cache_hit"] is True
    assert (tmp_path / "cache").exists()


def test_retire_bet_segment_cache_sidecar(tmp_path: Path) -> None:
    out = tmp_path / "cleaned__gmwds_t_bet"
    out.mkdir()
    sidecar = bet_clean_cache_manifest_path(out)
    sidecar.write_text("{}", encoding="utf-8")
    assert retire_bet_segment_cache_sidecar(out) is True
    assert not sidecar.is_file()
    assert retire_bet_segment_cache_sidecar(out) is False


def test_training_scope_fingerprint_stable() -> None:
    scope = TRAINING_DATA_SCOPE_TEST_UNBOUNDED
    assert training_scope_fingerprint(scope) == training_scope_fingerprint(scope)
