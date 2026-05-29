"""Tests for session cleaned-parquet cache sidecar."""

from __future__ import annotations

import importlib
import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_hpre = importlib.import_module("trainer_hightier.02_preprocess")


def test_session_clean_cache_hit(tmp_path) -> None:
    raw = tmp_path / "gmwds_t_session.parquet"
    df = pd.DataFrame({"session_id": [1], "x": [1.0]})
    pq.write_table(pa.Table.from_pandas(df), raw)

    cleaned = tmp_path / "cleaned__gmwds_t_session.parquet"
    df.iloc[0:0].to_parquet(cleaned, index=False)
    rec = _hpre.build_session_clean_cache_record(raw)
    mp = _hpre.session_clean_cache_manifest_path(cleaned)
    mp.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")

    assert _hpre.session_clean_cache_is_hit(raw, cleaned)


def test_session_clean_cache_miss_without_manifest(tmp_path) -> None:
    raw = tmp_path / "gmwds_t_session.parquet"
    pd.DataFrame({"a": [1]}).to_parquet(raw, index=False)
    cleaned = tmp_path / "cleaned__gmwds_t_session.parquet"
    pd.DataFrame({"a": [1]}).to_parquet(cleaned, index=False)

    assert not _hpre.session_clean_cache_is_hit(raw, cleaned)


def test_session_clean_cache_miss_when_row_count_changes(tmp_path) -> None:
    raw = tmp_path / "gmwds_t_session.parquet"
    pd.DataFrame({"session_id": [1]}).to_parquet(raw, index=False)
    cleaned = tmp_path / "cleaned__gmwds_t_session.parquet"
    pd.DataFrame({"session_id": [1]}).to_parquet(cleaned, index=False)
    old_rec = _hpre.build_session_clean_cache_record(raw)
    _hpre.session_clean_cache_manifest_path(cleaned).write_text(
        json.dumps(old_rec, sort_keys=True), encoding="utf-8"
    )

    pd.DataFrame({"session_id": [1, 2]}).to_parquet(raw, index=False)
    assert not _hpre.session_clean_cache_is_hit(raw, cleaned)


def test_session_clean_cache_hit_with_stored_higher_dedup_buckets(tmp_path) -> None:
    """Nominal bucket count can be lower than persisted OOM-escalated count and still cache-hit."""

    raw = tmp_path / "gmwds_t_session.parquet"
    df = pd.DataFrame({"session_id": [1], "x": [1.0]})
    pq.write_table(pa.Table.from_pandas(df), raw)

    cleaned = tmp_path / "cleaned__gmwds_t_session.parquet"
    df.iloc[0:0].to_parquet(cleaned, index=False)
    rec = _hpre.build_session_clean_cache_record(raw, dedup_hash_buckets=16)
    mp = _hpre.session_clean_cache_manifest_path(cleaned)
    mp.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")

    assert _hpre.session_clean_cache_is_hit(raw, cleaned, dedup_hash_buckets=8)
