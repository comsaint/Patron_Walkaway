"""Tests for walkaway labels cache v1 (L4 manifest layer)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DEFAULT_WALKAWAY_LABEL_CONTRACT,
    DuckDbRuntimeConfig,
    WALKAWAY_GAP_MIN,
    walkaway_label_contract_for_gap_min,
)
import pandas as pd

from trainer_hightier.utils.cache_invalidation_v1 import label_invalid_months
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
    gap60 = walkaway_label_contract_for_gap_min(60)
    assert label_semantic_fingerprint(gap60) != label_semantic_fingerprint()


def test_labels_policy_dir_includes_gap_partition() -> None:
    root = Path("/tmp/cache")
    p30 = labels_policy_dir(
        cache_root=root,
        entity_set_fingerprint="entity_fp_test_123456",
        walkaway_gap_min=30,
    )
    p60 = labels_policy_dir(
        cache_root=root,
        entity_set_fingerprint="entity_fp_test_123456",
        walkaway_gap_min=60,
    )
    assert p30 != p60
    assert "gap=30" in str(p30)
    assert "gap=60" in str(p60)


def test_labels_cache_hit_requires_matching_manifest(tmp_path: Path) -> None:
    labels_p = tmp_path / "labels.parquet"
    pq.write_table(pa.table({"bet_id": [1.0], "canonical_id": ["c1"], "label": [0]}), labels_p)
    manifest_p = tmp_path / "manifest.json"
    contract = DEFAULT_WALKAWAY_LABEL_CONTRACT
    semantic = label_semantic_fingerprint(contract)
    manifest_p.write_text(
        json.dumps(
            {
                "entity_set_fingerprint": "abc",
                "label_semantic_fingerprint": semantic,
                "walkaway_gap_min": contract.walkaway_gap_min,
                "alert_horizon_min": contract.alert_horizon_min,
            }
        ),
        encoding="utf-8",
    )
    assert labels_cache_is_hit(
        manifest_path=manifest_p,
        labels_parquet_path=labels_p,
        entity_set_fingerprint="abc",
        label_semantic_fp=semantic,
        label_contract=contract,
    )
    assert not labels_cache_is_hit(
        manifest_path=manifest_p,
        labels_parquet_path=labels_p,
        entity_set_fingerprint="different",
        label_semantic_fp=semantic,
        label_contract=contract,
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
        walkaway_gap_min=WALKAWAY_GAP_MIN,
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
        label_contract=DEFAULT_WALKAWAY_LABEL_CONTRACT,
    )
    second = materialize_labels_v1_sharded_cached(**kwargs)
    assert second["labels_cache_hit"] is True
    assert second["labels_cache_miss_shards"] == []


def test_sharded_labels_invalid_month_recomputes_shard(tmp_path: Path) -> None:
    """P3-T-10: dirty month safety window recomputes only affected month×shard entries."""
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
    expanded = sorted(label_invalid_months({"202406"}))
    assert expanded == ["202405", "202406", "202407"]
    rerun = materialize_labels_v1_sharded_cached(
        **kwargs,
        invalid_months=tuple(expanded),
    )
    assert rerun["labels_cache_hit"] is False
    assert "202406:0" in rerun["labels_cache_miss_shards"]


def test_labels_miss_when_entity_set_fingerprint_changes(tmp_path: Path, monkeypatch) -> None:
    """P3-T-5: entity set fp change forces labels rematerialize."""
    calls: list[str] = []

    def _fake_materialize(**_kwargs: object) -> Path:
        calls.append("materialize")
        out = tmp_path / "walkaway_labels.parquet"
        pq.write_table(pa.table({"bet_id": [1.0], "canonical_id": ["c1"], "label": [0]}), out)
        return out

    monkeypatch.setattr(
        "trainer_hightier.utils.labels_cache_v1.materialize_walkaway_labels_from_cleaned_bet",
        _fake_materialize,
    )
    base = dict(
        cleaned_bet_parquet=tmp_path / "bet",
        canonical_mapping_parquet=tmp_path / "map",
        duckdb_runtime=DuckDbRuntimeConfig(),
        cache_root=tmp_path / "cache",
        out_parquet=tmp_path / "walkaway_labels.parquet",
        use_cache=True,
    )
    materialize_labels_v1_cached(**base, entity_set_fingerprint="entity_fp_a" * 4)
    materialize_labels_v1_cached(**base, entity_set_fingerprint="entity_fp_b" * 4)
    assert calls == ["materialize", "materialize"]


def test_sharded_labels_miss_when_entity_set_fingerprint_changes(tmp_path: Path, monkeypatch) -> None:
    """P3-T-5 (sharded): entity set fp change rematerializes all month×shard entries."""
    shard_calls: list[str] = []
    real = __import__(
        "trainer_hightier.utils.labels_cache_v1",
        fromlist=["_materialize_labels_shard"],
    )._materialize_labels_shard

    def _spy_shard(**kwargs: object) -> Path:
        shard_calls.append(str(kwargs.get("entity_set_fingerprint")))
        return real(**kwargs)

    monkeypatch.setattr(
        "trainer_hightier.utils.labels_cache_v1._materialize_labels_shard",
        _spy_shard,
    )
    paths = _tiny_labels_inputs(tmp_path)
    base = dict(
        cleaned_bet_parquet=paths["bet"],
        canonical_mapping_parquet=paths["map"],
        duckdb_runtime=DuckDbRuntimeConfig(),
        cache_root=tmp_path / "cache_sharded_fp",
        out_parquet=paths["out"],
        canonical_shard_count=1,
        use_cache=True,
    )
    materialize_labels_v1_sharded_cached(**base, entity_set_fingerprint="entity_fp_a" * 4)
    materialize_labels_v1_sharded_cached(**base, entity_set_fingerprint="entity_fp_b" * 4)
    assert len(shard_calls) == 2
    assert shard_calls[0] != shard_calls[1]


def test_sharded_labels_legacy_policy_dir_cache_hit(tmp_path: Path) -> None:
    """Pre–gap-partition cache under ``entity_set=`` still hits for gap=30."""
    paths = _tiny_labels_inputs(tmp_path)
    legacy_policy = (
        tmp_path
        / "cache_legacy"
        / "entity_set=entity_fp_legacy"
    )
    shard_dir = legacy_policy / "month=202406" / "canonical_shard=0"
    shard_dir.mkdir(parents=True)
    pq.write_table(
        pa.table({"bet_id": [1.0], "canonical_id": ["c1"], "label": [0], "censored": [False]}),
        shard_dir / "data.parquet",
    )
    contract = DEFAULT_WALKAWAY_LABEL_CONTRACT
    semantic = label_semantic_fingerprint(contract)
    (shard_dir / "manifest.json").write_text(
        json.dumps(
            {
                "entity_set_fingerprint": "entity_fp_legacy",
                "label_semantic_fingerprint": semantic,
                "month": "202406",
                "canonical_shard": 0,
                "walkaway_gap_min": contract.walkaway_gap_min,
                "alert_horizon_min": contract.alert_horizon_min,
            }
        ),
        encoding="utf-8",
    )
    resolved = __import__(
        "trainer_hightier.utils.labels_cache_v1",
        fromlist=["_resolve_shard_cache_paths"],
    )._resolve_shard_cache_paths(
        policy_dirs=(
            labels_policy_dir(
                cache_root=tmp_path / "cache_legacy",
                entity_set_fingerprint="entity_fp_legacy",
                walkaway_gap_min=30,
            ),
            legacy_policy,
        ),
        month="202406",
        canonical_shard=0,
        entity_set_fingerprint="entity_fp_legacy",
        label_semantic_fp=semantic,
        label_contract=contract,
    )
    assert resolved is not None
    assert resolved[1] == shard_dir / "data.parquet"


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
