"""Tests for L0-cleaned ``txn_lite`` materializer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from trainer_hightier.config import (
    TXN_LITE_FEATURE_COLUMNS,
    TXN_L0_CLEANED_ROOT,
    DuckDbRuntimeConfig,
    HightierServingConfig,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.feature_experiment.materialize_txn_lite import (
    discover_cleaned_txn_partitions,
    materialize_txn_lite_parquet,
    resolve_cleaned_casino_txn_read_sql,
    resolved_cleaned_casino_txn_root,
    write_txn_lite_sidecars,
)
from trainer_hightier.serving.feature_builder import attach_synthetic_etl_and_prediction_visible
from trainer_hightier.serving.txn_lite_ch_runtime import (
    compute_txn_lite_features_for_bets_ch,
    run_txn_lite_parity_gate,
)


def _write_cleaned_partition(
    root: Path,
    partition_name: str,
    *,
    rows: list[dict[str, object]],
    is_partial: bool = False,
) -> Path:
    """Write one synthetic L0 cleaned partition with optional partial sidecar."""

    part_dir = root / partition_name
    part_dir.mkdir(parents=True, exist_ok=True)
    cleaned_path = part_dir / "cleaned.parquet"
    con = duckdb.connect()
    try:
        con.register("rows_df", pd.DataFrame(rows))
        con.execute(f"COPY rows_df TO '{cleaned_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    meta = {
        "is_partial_partition": is_partial,
        "partial_partition_reasons": ["test_partial"] if is_partial else [],
    }
    (part_dir / "source_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return cleaned_path


def _write_training_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    con = duckdb.connect()
    try:
        con.register("train_df", pd.DataFrame(rows))
        con.execute(f"COPY train_df TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def test_discover_cleaned_txn_partitions_skips_partial(tmp_path: Path) -> None:
    """Partial partitions are excluded when sidecar marks them incomplete."""

    _write_cleaned_partition(
        tmp_path,
        "partition_202605",
        rows=[
            {
                "player_id": 1,
                "txn_event_ts": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "txn_available_ts": datetime(2026, 5, 1, tzinfo=timezone.utc),
            },
        ],
    )
    _write_cleaned_partition(
        tmp_path,
        "partition_202606",
        rows=[
            {
                "player_id": 2,
                "txn_event_ts": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "txn_available_ts": datetime(2026, 6, 1, tzinfo=timezone.utc),
            },
        ],
        is_partial=True,
    )
    paths, included, excluded = discover_cleaned_txn_partitions(tmp_path)
    assert len(paths) == 1
    assert included == ["partition_202605"]
    assert excluded == ["partition_202606"]


def test_resolve_cleaned_read_sql_unions_complete_partitions(tmp_path: Path) -> None:
    """Eligible partitions are unioned for DuckDB read."""

    _write_cleaned_partition(
        tmp_path,
        "partition_202601",
        rows=[
            {
                "player_id": 1,
                "txn_event_ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "txn_available_ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
            },
        ],
    )
    _write_cleaned_partition(
        tmp_path,
        "partition_202602",
        rows=[
            {
                "player_id": 2,
                "txn_event_ts": datetime(2026, 2, 1, tzinfo=timezone.utc),
                "txn_available_ts": datetime(2026, 2, 1, tzinfo=timezone.utc),
            },
        ],
    )
    read_sql, included, excluded = resolve_cleaned_casino_txn_read_sql(tmp_path)
    assert included == ["partition_202601", "partition_202602"]
    assert excluded == []
    assert "union_by_name=true" in read_sql


def test_materialize_txn_lite_pit_and_feature_columns(tmp_path: Path) -> None:
    """PIT requires both event and available timestamps before payout_complete_dtm."""

    pcd = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    cleaned_rows = [
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 11, 45, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 11, 46, 0, tzinfo=timezone.utc),
            "type": "CASHOUT",
            "sub_type": "Cash",
            "txn_value": 500.0,
            "action": "SUBMIT",
            "status": "COMPLETED",
            "buyin_status": None,
        },
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 12, 5, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 12, 5, 0, tzinfo=timezone.utc),
            "type": "CASHOUT",
            "sub_type": "Cash",
            "txn_value": 900.0,
            "action": "SUBMIT",
            "status": "COMPLETED",
            "buyin_status": None,
        },
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 11, 30, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 11, 31, 0, tzinfo=timezone.utc),
            "type": "BUYIN",
            "sub_type": "CASH",
            "txn_value": 200.0,
            "action": "SUBMIT",
            "status": "SUBMITTED",
            "buyin_status": "SUCCESS",
        },
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 11, 20, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 11, 20, 0, tzinfo=timezone.utc),
            "type": "CHANGE",
            "sub_type": "Cash",
            "txn_value": 50.0,
            "action": "SUBMIT",
            "status": "COMPLETED",
            "buyin_status": None,
        },
    ]
    _write_cleaned_partition(tmp_path, "partition_202605", rows=cleaned_rows)
    train_path = tmp_path / "training.parquet"
    _write_training_parquet(
        train_path,
        [
            {
                "bet_id": 1.0,
                "player_id": 100,
                "payout_complete_dtm": pcd,
            },
        ],
    )
    out_path = tmp_path / "txn_lite.parquet"
    meta = materialize_txn_lite_parquet(
        cleaned_casino_txn_root=tmp_path,
        training_parquet_for_bet_ids=train_path,
        out_parquet=out_path,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    con = duckdb.connect()
    try:
        row = con.execute(f"SELECT * FROM read_parquet('{out_path.as_posix()}')").fetchone()
        cols = [
            d[0]
            for d in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{out_path.as_posix()}')").fetchall()
        ]
    finally:
        con.close()
    assert cols[1:] == list(TXN_LITE_FEATURE_COLUMNS)
    assert row[1] == 1
    assert row[2] == 1.0
    assert row[3] == 500.0
    assert row[6] == 200.0
    assert meta["not_model_eligible"] is True
    assert meta["input_layer"] == "l0_cleaned"
    assert meta["included_partitions"] == ["partition_202605"]
    assert meta["valid_txn_row_count"] == 3
    assert meta["pit_available_time"] == "txn_available_ts"


def test_materialize_txn_lite_excludes_future_observable_rows(tmp_path: Path) -> None:
    """Rows observed after payout cutoff must not leak into PIT features."""

    pcd = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    cleaned_rows = [
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 11, 45, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 12, 5, 0, tzinfo=timezone.utc),
            "type": "CASHOUT",
            "sub_type": "Cash",
            "txn_value": 700.0,
            "action": "SUBMIT",
            "status": "COMPLETED",
            "buyin_status": None,
        },
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 11, 30, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 11, 31, 0, tzinfo=timezone.utc),
            "type": "BUYIN",
            "sub_type": "CASH",
            "txn_value": 200.0,
            "action": "SUBMIT",
            "status": "SUBMITTED",
            "buyin_status": "SUCCESS",
        },
    ]
    _write_cleaned_partition(tmp_path, "partition_202605", rows=cleaned_rows)
    train_path = tmp_path / "training.parquet"
    _write_training_parquet(
        train_path,
        [
            {
                "bet_id": 1.0,
                "player_id": 100,
                "payout_complete_dtm": pcd,
            },
        ],
    )
    out_path = tmp_path / "txn_lite.parquet"
    materialize_txn_lite_parquet(
        cleaned_casino_txn_root=tmp_path,
        training_parquet_for_bet_ids=train_path,
        out_parquet=out_path,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    con = duckdb.connect()
    try:
        row = con.execute(f"SELECT * FROM read_parquet('{out_path.as_posix()}')").fetchone()
    finally:
        con.close()
    assert row[1] == 0
    assert row[2] == 0.0
    assert row[3] == 0.0
    assert row[6] == 200.0


def test_write_txn_lite_sidecars_preserves_quarantine_metadata(tmp_path: Path) -> None:
    """Sidecars carry not_model_eligible and partition coverage metadata."""

    out = tmp_path / "txn_lite.parquet"
    out.write_bytes(b"parquet")
    meta = {
        "included_partitions": ["partition_202605"],
        "excluded_partial_partitions": ["partition_202606"],
        "feature_columns": list(TXN_LITE_FEATURE_COLUMNS),
    }
    mat_path, src_path = write_txn_lite_sidecars(
        run_dir=tmp_path / "run",
        materialization_meta=meta,
        out_parquet=out,
    )
    src = json.loads(src_path.read_text(encoding="utf-8"))
    mat = json.loads(mat_path.read_text(encoding="utf-8"))
    assert src["not_model_eligible"] is True
    assert src["input_layer"] == "l0_cleaned"
    assert src["excluded_partial_partitions"] == ["partition_202606"]
    assert mat["included_partitions"] == ["partition_202605"]


def test_materialize_requires_at_least_one_complete_partition(tmp_path: Path) -> None:
    """All-partial roots fail fast instead of silently producing empty features."""

    _write_cleaned_partition(
        tmp_path,
        "partition_202607",
        rows=[
            {
                "player_id": 1,
                "txn_event_ts": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "txn_available_ts": datetime(2026, 7, 1, tzinfo=timezone.utc),
            },
        ],
        is_partial=True,
    )
    train_path = tmp_path / "training.parquet"
    _write_training_parquet(
        train_path,
        [{"bet_id": 1.0, "player_id": 1, "payout_complete_dtm": datetime(2026, 7, 2, tzinfo=timezone.utc)}],
    )
    with pytest.raises(FileNotFoundError, match="No eligible cleaned partitions"):
        materialize_txn_lite_parquet(
            cleaned_casino_txn_root=tmp_path,
            training_parquet_for_bet_ids=train_path,
            out_parquet=tmp_path / "out.parquet",
            duckdb_runtime=DuckDbRuntimeConfig(),
        )


def test_smoke_materialize_against_repo_l0_cleaned_if_present(tmp_path: Path) -> None:
    """Smoke against real L0 cleaned partitions when artifacts exist locally."""

    cleaned_root = TXN_L0_CLEANED_ROOT
    if not (cleaned_root / "partition_202505" / "cleaned.parquet").is_file():
        pytest.skip("L0 cleaned partition_202505 not present")
    train_path = tmp_path / "training_smoke.parquet"
    _write_training_parquet(
        train_path,
        [
            {
                "bet_id": 1.0,
                "player_id": 1,
                "payout_complete_dtm": datetime(2025, 5, 28, 12, 0, 0, tzinfo=timezone.utc),
            },
        ],
    )
    out_path = tmp_path / "txn_lite_smoke.parquet"
    meta = materialize_txn_lite_parquet(
        cleaned_casino_txn_root=cleaned_root,
        training_parquet_for_bet_ids=train_path,
        out_parquet=out_path,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    assert out_path.is_file()
    assert meta["materialized_bet_row_count"] == 1
    assert "partition_202606" in meta["excluded_partial_partitions"]
    assert "partition_202607" in meta["excluded_partial_partitions"]
    assert len(meta["included_partitions"]) >= 17


def test_resolved_cleaned_casino_txn_root_uses_serving_override(tmp_path: Path) -> None:
    """Deploy bundle override must win over package-default cleaned txn root."""

    custom = tmp_path / "cleaned_casino_txn"
    _write_cleaned_partition(
        custom,
        "partition_202601",
        rows=[
            {
                "player_id": 1,
                "txn_event_ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "txn_available_ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
            },
        ],
    )
    set_hightier_serving_deploy_override(
        HightierServingConfig(cleaned_casino_txn_root=custom),
    )
    try:
        assert resolved_cleaned_casino_txn_root() == custom.resolve()
        paths, included, _ = discover_cleaned_txn_partitions(
            resolved_cleaned_casino_txn_root(),
            exclude_partial=True,
        )
        assert included == ["partition_202601"]
        assert len(paths) == 1
    finally:
        set_hightier_serving_deploy_override(None)


def _synthetic_txn_rows_for_pit_test() -> list[dict[str, object]]:
    """Shared BUYIN/CASHOUT rows for cleaned vs CH-runtime parity tests."""

    return [
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 11, 45, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 11, 46, 0, tzinfo=timezone.utc),
            "type": "CASHOUT",
            "sub_type": "Cash",
            "txn_value": 500.0,
            "action": "SUBMIT",
            "status": "COMPLETED",
            "buyin_status": None,
        },
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 11, 30, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 11, 31, 0, tzinfo=timezone.utc),
            "type": "BUYIN",
            "sub_type": "CASH",
            "txn_value": 200.0,
            "action": "SUBMIT",
            "status": "SUBMITTED",
            "buyin_status": "SUCCESS",
        },
        {
            "player_id": 100,
            "txn_event_ts": datetime(2026, 5, 10, 11, 20, 0, tzinfo=timezone.utc),
            "txn_available_ts": datetime(2026, 5, 10, 11, 20, 0, tzinfo=timezone.utc),
            "type": "BUYIN",
            "sub_type": "PRIZE REDEMPTION",
            "txn_value": 100.0,
            "action": "SUBMIT",
            "status": "COMPLETED",
            "buyin_status": None,
        },
    ]


def test_ch_runtime_parity_with_cleaned_on_same_txn_rows(tmp_path: Path) -> None:
    """Injected CH-runtime rows with pcd cutoff should match cleaned parquet replay."""

    pcd = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    rows = _synthetic_txn_rows_for_pit_test()
    _write_cleaned_partition(tmp_path, "partition_202605", rows=rows)
    bets = pd.DataFrame(
        [
            {
                "bet_id": 1.0,
                "player_id": 100,
                "payout_complete_dtm": pcd,
                "__etl_insert_Dtm": pcd,
            },
        ],
    )
    bets = attach_synthetic_etl_and_prediction_visible(bets)
    txn_rows = pd.DataFrame(rows)
    report = run_txn_lite_parity_gate(
        bets,
        cleaned_casino_txn_root=tmp_path,
        duckdb_runtime=DuckDbRuntimeConfig(),
        txn_rows=txn_rows,
    )
    assert report["verdict"] == "pass"
    assert report["max_diff_fraction"] == 0.0


def test_ch_runtime_excludes_late_availability_with_prediction_visible() -> None:
    """Production cutoff uses prediction_visible_ts_cf, excluding late-available txns."""

    pcd = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    pv = datetime(2026, 5, 10, 11, 50, 0, tzinfo=timezone.utc)
    txn_rows = pd.DataFrame(
        [
            {
                "player_id": 100,
                "txn_event_ts": datetime(2026, 5, 10, 11, 45, 0, tzinfo=timezone.utc),
                "txn_available_ts": datetime(2026, 5, 10, 11, 55, 0, tzinfo=timezone.utc),
                "type": "CASHOUT",
                "sub_type": "Cash",
                "txn_value": 500.0,
                "action": "SUBMIT",
                "status": "COMPLETED",
                "buyin_status": None,
            },
        ],
    )
    bets = pd.DataFrame(
        [
            {
                "bet_id": 1.0,
                "player_id": 100,
                "payout_complete_dtm": pcd,
                "prediction_visible_ts_cf": pv,
            },
        ],
    )
    out = compute_txn_lite_features_for_bets_ch(
        bets,
        duckdb_runtime=DuckDbRuntimeConfig(),
        txn_rows=txn_rows,
        use_pcd_availability_cutoff=False,
    )
    assert int(out.loc[0, "txn__has_cash_out__w15m"]) == 0
    assert float(out.loc[0, "txn__cash_out_sum__w1h"]) == 0.0


def test_ch_runtime_zero_fill_when_no_txn_rows() -> None:
    """Empty txn pool yields zero-filled txn__* features for every bet."""

    pcd = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    bets = pd.DataFrame(
        [
            {
                "bet_id": 1.0,
                "player_id": 100,
                "payout_complete_dtm": pcd,
                "prediction_visible_ts_cf": pcd,
            },
        ],
    )
    out = compute_txn_lite_features_for_bets_ch(
        bets,
        duckdb_runtime=DuckDbRuntimeConfig(),
        txn_rows=pd.DataFrame(),
    )
    assert float(out.loc[0, "txn__cash_out_cnt__w1h"]) == 0.0
    assert float(out.loc[0, "txn__buyin_cash_sum__w1h"]) == 0.0
    assert int(out.loc[0, "txn__buyin_prize_redemption_flag__w1h"]) == 0
