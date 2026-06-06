"""Tests for walkaway labels cache v1 (L4 manifest layer)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
import pandas as pd

from trainer_hightier.utils.labels_cache_v1 import (
    label_semantic_fingerprint,
    labels_cache_is_hit,
    labels_policy_dir,
    labels_shard_cache_is_hit,
    labels_shard_dir,
    materialize_labels_v1_cached,
    materialize_labels_v1_sharded_cached,
)


def test_label_semantic_fingerprint_stable() -> None:
    assert label_semantic_fingerprint() == label_semantic_fingerprint()
    assert len(label_semantic_fingerprint()) == 64


def test_labels_cache_hit_requires_matching_manifest(tmp_path: Path) -> None:
    labels_p = tmp_path / "labels.parquet"
    pq.write_table(pa.table({"bet_id": [1.0], "canonical_id": ["c1"], "label": [0]}), labels_p)
    manifest_p = tmp_path / "manifest.json"
    semantic = label_semantic_fingerprint()
    manifest_p.write_text(
        json.dumps(
            {
                "entity_set_fingerprint": "abc",
                "label_semantic_fingerprint": semantic,
                "walkaway_gap_min": 30,
                "alert_horizon_min": 15,
            }
        ),
        encoding="utf-8",
    )
    assert labels_cache_is_hit(
        manifest_path=manifest_p,
        labels_parquet_path=labels_p,
        entity_set_fingerprint="abc",
        label_semantic_fp=semantic,
    )
    assert not labels_cache_is_hit(
        manifest_path=manifest_p,
        labels_parquet_path=labels_p,
        entity_set_fingerprint="different",
        label_semantic_fp=semantic,
    )


def test_materialize_labels_cache_hit_on_second_call(tmp_path: Path, monkeypatch) -> None:
    """Second call with same entity fp should hit without re-invoking materializer."""
    calls: list[int] = []

    def _fake_materialize(**_kwargs: object) -> Path:
        calls.append(1)
        out = tmp_path / "walkaway_labels.parquet"
        pq.write_table(pa.table({"bet_id": [1.0], "canonical_id": ["c1"], "label": [0]}), out)
        return out

    monkeypatch.setattr(
        "trainer_hightier.utils.labels_cache_v1.materialize_walkaway_labels_from_cleaned_bet",
        _fake_materialize,
    )
    kwargs = dict(
        cleaned_bet_parquet=tmp_path / "bet",
        canonical_mapping_parquet=tmp_path / "map",
        entity_set_fingerprint="entity_fp_test_123456",
        duckdb_runtime=DuckDbRuntimeConfig(),
        cache_root=tmp_path / "cache",
        out_parquet=tmp_path / "walkaway_labels.parquet",
    )
    first = materialize_labels_v1_cached(**kwargs, use_cache=True)
    second = materialize_labels_v1_cached(**kwargs, use_cache=True)
    assert first["labels_cache_hit"] is False
    assert second["labels_cache_hit"] is True
    assert len(calls) == 1


def _tiny_labels_inputs(tmp_path: Path) -> dict[str, Path]:
    t0 = pd.Timestamp("2024-06-01 12:00:00", tz="UTC")
    df_b = pd.DataFrame(
        [
            {"bet_id": 1.0, "player_id": 100, "payout_complete_dtm": t0},
            {"bet_id": 2.0, "player_id": 100, "payout_complete_dtm": t0 + pd.Timedelta(minutes=35)},
        ]
    )
    df_m = pd.DataFrame([{"player_id": 100, "canonical_id": "c1"}])
    bet_p = tmp_path / "bet_clean.parquet"
    map_p = tmp_path / "canonical_map.parquet"
    pq.write_table(pa.Table.from_pandas(df_b), bet_p)
    pq.write_table(pa.Table.from_pandas(df_m), map_p)
    return {"bet": bet_p, "map": map_p, "out": tmp_path / "walkaway_labels.parquet"}


def test_sharded_labels_cache_hit_on_second_call(tmp_path: Path) -> None:
    """Month×shard labels cache should hit when policy unchanged (P3-T-4)."""
    paths = _tiny_labels_inputs(tmp_path)
    kwargs = dict(
        cleaned_bet_parquet=paths["bet"],
        canonical_mapping_parquet=paths["map"],
        entity_set_fingerprint="entity_fp_sharded_test",
        duckdb_runtime=DuckDbRuntimeConfig(),
        cache_root=tmp_path / "cache",
        out_parquet=paths["out"],
        canonical_shard_count=1,
        use_cache=True,
    )
    first = materialize_labels_v1_sharded_cached(**kwargs)
    assert first["labels_sharded"] is True
    assert first["labels_cache_hit"] is False
    assert first["labels_grain"] == "month_x_canonical_shard"
    policy = labels_policy_dir(
        cache_root=tmp_path / "cache",
        entity_set_fingerprint="entity_fp_sharded_test",
    )
    shard_dir = labels_shard_dir(policy_dir=policy, month="202406", canonical_shard=0)
    assert (shard_dir / "data.parquet").is_file()
    assert labels_shard_cache_is_hit(
        shard_manifest_path=shard_dir / "manifest.json",
        shard_data_path=shard_dir / "data.parquet",
        entity_set_fingerprint="entity_fp_sharded_test",
        label_semantic_fp=label_semantic_fingerprint(),
        month="202406",
        canonical_shard=0,
    )
    second = materialize_labels_v1_sharded_cached(**kwargs)
    assert second["labels_cache_hit"] is True
    assert second["labels_cache_miss_shards"] == []


def test_sharded_labels_invalid_month_recomputes_shard(tmp_path: Path) -> None:
    """Dirty month should miss only that month's shards (P3-T-10 prep)."""
    paths = _tiny_labels_inputs(tmp_path)
    kwargs = dict(
        cleaned_bet_parquet=paths["bet"],
        canonical_mapping_parquet=paths["map"],
        entity_set_fingerprint="entity_fp_invalid_month",
        duckdb_runtime=DuckDbRuntimeConfig(),
        cache_root=tmp_path / "cache2",
        out_parquet=paths["out"],
        canonical_shard_count=1,
        use_cache=True,
    )
    materialize_labels_v1_sharded_cached(**kwargs)
    rerun = materialize_labels_v1_sharded_cached(**kwargs, invalid_months=("202406",))
    assert "202406:0" in rerun["labels_cache_miss_shards"]


def test_materialize_labels_use_sharded_flag(tmp_path: Path, monkeypatch) -> None:
    """``use_sharded_cache=True`` routes through sharded materializer without trainer wiring."""
    routed: list[bool] = []

    def _fake_sharded(**_kwargs: object) -> dict[str, object]:
        routed.append(True)
        return {"labels_cache_hit": False, "labels_sharded": True, "labels_row_count": 0}

    monkeypatch.setattr(
        "trainer_hightier.utils.labels_cache_v1.materialize_labels_v1_sharded_cached",
        _fake_sharded,
    )
    materialize_labels_v1_cached(
        cleaned_bet_parquet=tmp_path / "bet",
        canonical_mapping_parquet=tmp_path / "map",
        entity_set_fingerprint="fp",
        duckdb_runtime=DuckDbRuntimeConfig(),
        use_sharded_cache=True,
    )
    assert routed == [True]
