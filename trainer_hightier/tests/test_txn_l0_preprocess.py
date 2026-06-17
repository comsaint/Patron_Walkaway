"""Unit tests for ``t_casino_txn`` L0 preprocess (registry-driven correction)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from trainer_hightier.config import TXN_L0_INGEST_FIX_RULE_ID
from trainer_hightier.preprocess_bet_fix_registry import (
    load_preprocess_txn_ingestion_fix_registry,
    resolve_txn_ingest_fix001_cap_binding,
)
from trainer_hightier.utils.partition_inventory import (
    list_casino_txn_raw_partition_dirs,
    scan_casino_txn_partition_root,
)
from trainer_hightier.utils.txn_l0_schema import (
    TXN_L0_DERIVED_COLUMN_TYPES,
    TXN_L0_RAW_COLUMN_TYPES,
    TXN_L0_SCHEMA_DDL_REF,
    assert_dictionary_section5_matches_ddl,
    assert_txn_l0_schema_matches_ddl,
    parse_t_casino_txn_ddl_columns,
    txn_l0_schema_fingerprint_sha256_hex,
)
from trainer_hightier.utils.txn_l0_preprocess import (
    TxnL0PreflightHardFailError,
    TxnL0PreprocessConfig,
    assess_partial_partition,
    build_txn_l0_clean_cache_record,
    default_preprocess_registry_yaml_path,
    fingerprint_raw_partition,
    materialize_txn_l0_partition,
    resolve_raw_partition_read_sql,
    run_txn_l0_preflight,
    validate_raw_partition_dir,
)

_DEFAULT_REGISTRY = default_preprocess_registry_yaml_path()

_MIN_COLS: tuple[str, ...] = tuple(col for col, _ in TXN_L0_RAW_COLUMN_TYPES)


def _txn_row(**kwargs: object) -> dict[str, object]:
    """Build a minimal raw ``t_casino_txn`` row for fixtures."""

    merged: dict[str, object] = dict(
        casino_txn_id=1001,
        start_dtm=pd.Timestamp("2025-06-01 12:00:00"),
        __etl_insert_Dtm=pd.Timestamp("2025-06-01 12:00:10"),
        updated_dtm=pd.Timestamp("2025-06-01 12:00:10"),
        __op="c",
        __deleted="False",
        type="BUYIN",
        status="COMPLETED",
        action="SUBMIT",
        txn_value=1000.0,
        player_id=42,
    )
    merged.update(kwargs)
    return merged


def _write_partition(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    part_dir = tmp_path / "partition_202606"
    part_dir.mkdir(parents=True)
    df = pd.DataFrame(rows)
    for col in _MIN_COLS:
        if col not in df.columns:
            df[col] = None
    df = df[list(_MIN_COLS)]
    pq.write_table(pa.Table.from_pandas(df), part_dir / "part_000.parquet")
    return part_dir


@pytest.fixture
def registry_path() -> Path:
    if not _DEFAULT_REGISTRY.is_file():
        pytest.skip(f"registry missing {_DEFAULT_REGISTRY}")
    return _DEFAULT_REGISTRY


@pytest.fixture
def cfg(registry_path: Path) -> TxnL0PreprocessConfig:
    return TxnL0PreprocessConfig(preprocess_registry_yaml=registry_path)


def test_txn_l0_raw_schema_matches_clickhouse_ddl() -> None:
    """L0 raw column order and DuckDB casts align with schema/schema.txt DDL."""

    ddl_cols = parse_t_casino_txn_ddl_columns()
    assert len(ddl_cols) == 65
    assert [name for name, _ in ddl_cols] == [name for name, _ in TXN_L0_RAW_COLUMN_TYPES]
    assert_txn_l0_schema_matches_ddl()
    assert_dictionary_section5_matches_ddl()
    assert TXN_L0_SCHEMA_DDL_REF == "schema/schema.txt#t_casino_txn"


def test_registry_txn_fix001_cap_binding(registry_path: Path) -> None:
    doc = load_preprocess_txn_ingestion_fix_registry(registry_path)
    cap, fix_id, fix_ver, applied = resolve_txn_ingest_fix001_cap_binding(doc)
    assert cap == 128
    assert fix_id == TXN_L0_INGEST_FIX_RULE_ID
    assert fix_ver == "v1"
    assert applied == [f"{TXN_L0_INGEST_FIX_RULE_ID}:v1"]


def test_validate_raw_partition_dir_rejects_bad_name(tmp_path: Path) -> None:
    bad = tmp_path / "not_a_partition"
    bad.mkdir()
    with pytest.raises(ValueError, match="partition_YYYYMM"):
        validate_raw_partition_dir(bad)


def test_preflight_uncovered_observed_before_event_hard_fail(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
) -> None:
    part = _write_partition(
        tmp_path,
        [
            _txn_row(
                casino_txn_id=1,
                start_dtm=pd.Timestamp("2025-06-02 12:00:00"),
                __etl_insert_Dtm=pd.Timestamp("2025-06-02 11:59:00"),
            ),
        ],
    )
    raw_read = resolve_raw_partition_read_sql(part)
    con = duckdb.connect()
    try:
        with pytest.raises(TxnL0PreflightHardFailError) as exc_info:
            run_txn_l0_preflight(con, raw_read=raw_read, cfg=cfg)
        assert exc_info.value.evidence["uncovered_observed_before_event_rows"] == 1
    finally:
        con.close()


def test_preflight_covered_episode_passes(tmp_path: Path, cfg: TxnL0PreprocessConfig) -> None:
    part = _write_partition(
        tmp_path,
        [
            _txn_row(
                casino_txn_id=2,
                start_dtm=pd.Timestamp("2025-05-27 18:00:00"),
                __etl_insert_Dtm=pd.Timestamp("2025-05-27 12:00:00"),
            ),
        ],
    )
    raw_read = resolve_raw_partition_read_sql(part)
    con = duckdb.connect()
    try:
        evidence = run_txn_l0_preflight(con, raw_read=raw_read, cfg=cfg)
        assert evidence["preflight_status"] == "pass"
        assert evidence["uncovered_observed_before_event_rows"] == 0
    finally:
        con.close()


def test_dedup_delete_aware_and_latest_wins(tmp_path: Path, cfg: TxnL0PreprocessConfig) -> None:
    part = _write_partition(
        tmp_path,
        [
            _txn_row(casino_txn_id=10, txn_value=100.0, type="BUYIN"),
            _txn_row(
                casino_txn_id=10,
                txn_value=200.0,
                type="CASHOUT",
                __etl_insert_Dtm=pd.Timestamp("2025-06-01 12:01:00"),
            ),
        ],
    )
    out = tmp_path / "out"
    materialize_txn_l0_partition(part, out, cfg=cfg)
    got = pd.read_parquet(out / "cleaned.parquet")
    assert len(got) == 1
    assert float(got.iloc[0]["txn_value"]) == 200.0


def test_dedup_excludes_delete_marker_logical_id(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
) -> None:
    part = _write_partition(
        tmp_path,
        [
            _txn_row(casino_txn_id=20, __op="c", __deleted="False"),
            _txn_row(
                casino_txn_id=20,
                __op="d",
                __deleted="True",
                __etl_insert_Dtm=pd.Timestamp("2025-06-01 12:05:00"),
            ),
        ],
    )
    out = tmp_path / "out_del"
    materialize_txn_l0_partition(part, out, cfg=cfg)
    got = pd.read_parquet(out / "cleaned.parquet")
    assert got.empty


def test_hard_exclude_missing_required_fields(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
) -> None:
    part = _write_partition(
        tmp_path,
        [
            _txn_row(casino_txn_id=None),
            _txn_row(casino_txn_id=30, start_dtm=None),
            _txn_row(casino_txn_id=31, __etl_insert_Dtm=None),
            _txn_row(casino_txn_id=32),
        ],
    )
    out = tmp_path / "out_excl"
    materialize_txn_l0_partition(part, out, cfg=cfg)
    got = pd.read_parquet(out / "cleaned.parquet")
    assert len(got) == 1
    assert int(got.iloc[0]["casino_txn_id"]) == 32
    report = json.loads((out / "txn_l0_materialization_report.json").read_text(encoding="utf-8"))
    assert report["counts"]["hard_excluded_rows"] == 3


def test_covered_correction_txn_available_ts_ge_event(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
    registry_path: Path,
) -> None:
    cap, _, _, _ = resolve_txn_ingest_fix001_cap_binding(
        load_preprocess_txn_ingestion_fix_registry(registry_path)
    )
    part = _write_partition(
        tmp_path,
        [
            _txn_row(
                casino_txn_id=40,
                start_dtm=pd.Timestamp("2025-05-27 18:00:00"),
                __etl_insert_Dtm=pd.Timestamp("2025-05-27 12:00:00"),
            ),
        ],
    )
    out = tmp_path / "out_corr"
    materialize_txn_l0_partition(part, out, cfg=cfg)
    got = pd.read_parquet(out / "cleaned.parquet")
    assert len(got) == 1
    row = got.iloc[0]
    assert row["observed_at_correction_rule_id"] == f"{TXN_L0_INGEST_FIX_RULE_ID}:v1"
    event = pd.Timestamp(row["txn_event_ts"])
    avail = pd.Timestamp(row["txn_available_ts"])
    assert avail >= event
    assert avail == event
    assert cap == 128


def test_suspicious_non_positive_txn_value_flag(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
) -> None:
    part = _write_partition(
        tmp_path,
        [_txn_row(casino_txn_id=50, txn_value=0.0)],
    )
    out = tmp_path / "out_flag"
    materialize_txn_l0_partition(part, out, cfg=cfg)
    got = pd.read_parquet(out / "cleaned.parquet")
    assert bool(got.iloc[0]["is_suspicious_non_positive_txn_value"]) is True


def test_sidecar_schema_and_deterministic_fingerprint(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
) -> None:
    part = _write_partition(tmp_path, [_txn_row(casino_txn_id=60)])
    out = tmp_path / "out_fp"
    materialize_txn_l0_partition(part, out, cfg=cfg)
    fp1 = fingerprint_raw_partition(part)
    materialize_txn_l0_partition(part, out, cfg=cfg)
    fp2 = fingerprint_raw_partition(part)
    assert fp1 == fp2
    meta = json.loads((out / "source_metadata.json").read_text(encoding="utf-8"))
    report = json.loads((out / "txn_l0_materialization_report.json").read_text(encoding="utf-8"))
    preflight = json.loads((out / "txn_l0_preflight_report.json").read_text(encoding="utf-8"))
    assert meta["not_model_eligible"] is True
    assert report["not_model_eligible"] is True
    assert preflight["preflight_status"] == "pass"
    assert report["raw_partition_fingerprint"] == fp1
    assert "materialization_report_fingerprint" in report
    assert "clean_cache_fingerprint_sha256_hex" in report
    assert meta["clean_cache_fingerprint_sha256_hex"] == report["clean_cache_fingerprint_sha256_hex"]


def test_clean_cache_fingerprint_changes_with_correction_policy(
    tmp_path: Path,
    registry_path: Path,
) -> None:
    part = _write_partition(tmp_path, [_txn_row(casino_txn_id=61)])
    base = build_txn_l0_clean_cache_record(
        part,
        preprocess_registry_yaml=registry_path,
    )
    alt_registry = tmp_path / "registry_cap_122.yaml"
    doc = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    txn = doc["tables"]["gmwds_t_casino_txn"]
    txn["bulk_historical_ingest_episodes"]["synthetic_observed_at_contract"][
        "ingest_delay_cap_sec"
    ] = 122
    for rule in txn["active_rules"]:
        if rule.get("fix_rule_id") == "TXN-INGEST-FIX-001":
            rule["action"]["params"]["cap_delay_sec"] = 122
    alt_registry.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    alt = build_txn_l0_clean_cache_record(
        part,
        preprocess_registry_yaml=alt_registry,
    )
    assert base["fingerprint_sha256_hex"] != alt["fingerprint_sha256_hex"]
    assert base["txn_ingest_cap_sec"] == 128
    assert alt["txn_ingest_cap_sec"] == 122


def test_registry_cap_mismatch_fail_fast(tmp_path: Path, registry_path: Path) -> None:
    bad_registry = tmp_path / "registry_mismatch.yaml"
    doc = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    txn = doc["tables"]["gmwds_t_casino_txn"]
    txn["active_rules"][0]["action"]["params"]["cap_delay_sec"] = 122
    bad_registry.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="ingest_delay_cap_sec mismatch"):
        resolve_txn_ingest_fix001_cap_binding(
            load_preprocess_txn_ingestion_fix_registry(bad_registry)
        )


def test_raw_partition_discovery_helpers(tmp_path: Path) -> None:
    part = _write_partition(tmp_path, [_txn_row(casino_txn_id=62)])
    txn_root = part.parent
    listed = list_casino_txn_raw_partition_dirs(txn_root)
    assert listed == [part.resolve()]
    stats = scan_casino_txn_partition_root(txn_root)
    assert len(stats) == 1
    assert stats[0].role == "t_casino_txn"
    assert stats[0].yyyymm == "202606"


def _arrow_type_name(field: pa.Field) -> str:
    """Normalize Arrow type name for contract comparison."""

    t = field.type
    if pa.types.is_decimal(t):
        return f"decimal128({t.precision},{t.scale})"
    if pa.types.is_timestamp(t):
        return "timestamptz" if t.tz is not None else "timestamp"
    if pa.types.is_int64(t):
        return "int64"
    if pa.types.is_int32(t):
        return "int32"
    if pa.types.is_uint8(t):
        return "uint8"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "varchar"
    if pa.types.is_date32(t):
        return "date32"
    if pa.types.is_boolean(t):
        return "boolean"
    return str(t)


_DICT_TO_ARROW: dict[str, str] = {
    "BIGINT": "int64",
    "INTEGER": "int32",
    "UTINYINT": "uint8",
    "VARCHAR": "varchar",
    "DATE": "date32",
    "TIMESTAMPTZ": "timestamptz",
    "DECIMAL(19,4)": "decimal128(19,4)",
    "BOOLEAN": "boolean",
}


def test_materialized_output_matches_dictionary_schema(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
) -> None:
    part = _write_partition(
        tmp_path,
        [
            _txn_row(casino_txn_id=63, txn_value=12345.6789),
            _txn_row(
                casino_txn_id=64,
                start_dtm=pd.Timestamp("2025-05-27 18:00:00", tz="UTC"),
                __etl_insert_Dtm=pd.Timestamp("2025-05-27 12:00:00", tz="UTC"),
            ),
        ],
    )
    out = tmp_path / "out_schema"
    materialize_txn_l0_partition(part, out, cfg=cfg)
    schema = pq.read_schema(out / "cleaned.parquet")
    expected_cols = [c for c, _ in TXN_L0_RAW_COLUMN_TYPES] + [
        c for c, _ in TXN_L0_DERIVED_COLUMN_TYPES
    ]
    assert schema.names == expected_cols
    for col, duck_type in TXN_L0_RAW_COLUMN_TYPES:
        got = _arrow_type_name(schema.field(col))
        want = _DICT_TO_ARROW[duck_type]
        assert got == want, f"{col}: got {got}, want {want}"
    for col, duck_type in TXN_L0_DERIVED_COLUMN_TYPES:
        got = _arrow_type_name(schema.field(col))
        want = _DICT_TO_ARROW[duck_type]
        assert got == want, f"{col}: got {got}, want {want}"
    report = json.loads((out / "txn_l0_materialization_report.json").read_text(encoding="utf-8"))
    assert report["schema_fingerprint_sha256_hex"] == txn_l0_schema_fingerprint_sha256_hex()
    assert report["cleaning_policy_id"] == "t_casino_txn_l0_v2_schema"


def test_assess_partial_partition_flags_small_month(tmp_path: Path) -> None:
    sibling_root = tmp_path / "cleaned"
    for name, rows, shards in (
        ("partition_202604", 3_800_000, 10),
        ("partition_202605", 4_000_000, 11),
    ):
        part_dir = sibling_root / name
        part_dir.mkdir(parents=True)
        (part_dir / "txn_l0_materialization_report.json").write_text(
            json.dumps(
                {
                    "counts": {"post_dedup_rows": rows},
                    "partition_coverage": {"shard_count": shards},
                }
            ),
            encoding="utf-8",
        )
    tiny = _write_partition(tmp_path / "raw", [_txn_row(casino_txn_id=80)])
    tiny_rows = 76
    got = assess_partial_partition(
        tiny,
        raw_rows=tiny_rows,
        post_dedup_rows=tiny_rows,
        cleaned_root=sibling_root,
    )
    assert got["is_partial_partition"] is True
    assert "post_dedup_rows_below_absolute_floor" in got["partial_partition_reasons"]
    assert "shard_count_at_or_below_partial_threshold" in got["partial_partition_reasons"]


def test_materialize_sidecar_includes_partial_partition_coverage(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
) -> None:
    sibling_root = tmp_path / "siblings"
    for name, rows, shards in (
        ("partition_202604", 3_800_000, 10),
        ("partition_202605", 4_000_000, 11),
    ):
        part_dir = sibling_root / name
        part_dir.mkdir(parents=True)
        (part_dir / "txn_l0_materialization_report.json").write_text(
            json.dumps(
                {
                    "counts": {"post_dedup_rows": rows},
                    "partition_coverage": {"shard_count": shards},
                }
            ),
            encoding="utf-8",
        )
    part = _write_partition(tmp_path / "raw2", [_txn_row(casino_txn_id=81)])
    out = tmp_path / "out_partial"
    import trainer_hightier.utils.txn_l0_preprocess as txn_mod

    original = txn_mod.TXN_L0_CLEANED_ROOT
    txn_mod.TXN_L0_CLEANED_ROOT = sibling_root
    try:
        materialize_txn_l0_partition(part, out, cfg=cfg)
    finally:
        txn_mod.TXN_L0_CLEANED_ROOT = original
    report = json.loads((out / "txn_l0_materialization_report.json").read_text(encoding="utf-8"))
    meta = json.loads((out / "source_metadata.json").read_text(encoding="utf-8"))
    coverage = report["partition_coverage"]
    assert coverage["is_partial_partition"] is True
    assert meta["is_partial_partition"] is True
    assert meta["partial_partition_reasons"]


def test_preflight_failure_writes_evidence_without_cleaned(
    tmp_path: Path,
    cfg: TxnL0PreprocessConfig,
) -> None:
    part = _write_partition(
        tmp_path,
        [
            _txn_row(
                casino_txn_id=70,
                start_dtm=pd.Timestamp("2025-06-03 10:00:00"),
                __etl_insert_Dtm=pd.Timestamp("2025-06-03 09:00:00"),
            ),
        ],
    )
    out = tmp_path / "out_fail"
    with pytest.raises(TxnL0PreflightHardFailError):
        materialize_txn_l0_partition(part, out, cfg=cfg)
    assert not (out / "cleaned.parquet").exists()
    evidence = json.loads((out / "txn_l0_preflight_evidence.json").read_text(encoding="utf-8"))
    assert evidence["preflight_status"] == "hard_fail"
    assert evidence["uncovered_observed_before_event_rows"] == 1


def test_smoke_real_partition_if_present(cfg: TxnL0PreprocessConfig, tmp_path: Path) -> None:
    from trainer_hightier.config import TXN_L0_RAW_ROOT

    for part_name in ("partition_202505", "partition_202605"):
        part = TXN_L0_RAW_ROOT / part_name
        if not part.is_dir():
            pytest.skip(f"real partition not present: {part}")
        out = tmp_path / part_name
        materialize_txn_l0_partition(part, out, cfg=cfg)
        assert (out / "cleaned.parquet").is_file()
        assert (out / "txn_l0_materialization_report.json").is_file()
        meta = json.loads((out / "source_metadata.json").read_text(encoding="utf-8"))
        assert meta["not_model_eligible"] is True
