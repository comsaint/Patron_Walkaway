"""Tests for ``trainer_hightier.01_data_ingest`` offline path + schema QC."""

from __future__ import annotations

import importlib

import pandas as pd
import pytest

ds = importlib.import_module("trainer_hightier.01_data_ingest")
LocalParquetPaths = ds.LocalParquetPaths
assert_local_parquet_files_exist = ds.assert_local_parquet_files_exist
validate_offline_inputs_or_raise = ds.validate_offline_inputs_or_raise
validate_session_ingress_or_raise = ds.validate_session_ingress_or_raise


def test_validate_session_ingress_ok_without_bet_parquet(tmp_path) -> None:
    paths = LocalParquetPaths(tmp_path)
    sess_df = pd.DataFrame(
        {c: pd.Series([], dtype="float64") for c in ds._REQUIRED_SESSION_PARQUET_COLS}
    )
    sess_df.to_parquet(paths.session_parquet, index=False)
    rep = validate_session_ingress_or_raise(paths)
    assert rep.session.num_rows == 0
    assert rep.missing_required_session_cols == ()


def test_validate_session_ingress_raises_if_session_file_missing(tmp_path) -> None:
    paths = LocalParquetPaths(tmp_path)
    with pytest.raises(FileNotFoundError):
        validate_session_ingress_or_raise(paths)


def test_validate_session_ingress_reports_missing_session_column(tmp_path) -> None:
    paths = LocalParquetPaths(tmp_path)
    pd.DataFrame({"session_id": [1]}).to_parquet(paths.session_parquet, index=False)
    with pytest.raises(ValueError, match="gmwds_t_session missing columns"):
        validate_session_ingress_or_raise(paths)


def test_read_parquet_row_groups_matches_pandas(tmp_path) -> None:
    """Row-group reader should match ``pd.read_parquet`` on a small multi-RG file."""
    path = tmp_path / "rg.parquet"
    df = pd.DataFrame({"x": range(25), "y": [1.0] * 25})
    df.to_parquet(path, index=False, row_group_size=10)
    got = ds.read_parquet_row_groups_to_pandas(path, desc="test")
    exp = pd.read_parquet(path)
    pd.testing.assert_frame_equal(got.sort_values("x").reset_index(drop=True), exp)


def test_preflight_scan_parquet_row_groups_no_error(tmp_path) -> None:
    path = tmp_path / "p.parquet"
    pd.DataFrame({"a": [1, 2, 3]}).to_parquet(path, index=False, row_group_size=2)
    ds.preflight_scan_parquet_row_groups(path, desc="test")


def _write_empty_parquets_with_contract_columns(paths: LocalParquetPaths) -> None:
    bet_df = pd.DataFrame({c: pd.Series([], dtype="float64") for c in ds._REQUIRED_BET_PARQUET_COLS})
    sess_df = pd.DataFrame(
        {c: pd.Series([], dtype="float64") for c in ds._REQUIRED_SESSION_PARQUET_COLS}
    )
    bet_df.to_parquet(paths.bet_parquet, index=False)
    sess_df.to_parquet(paths.session_parquet, index=False)


def test_assert_parquet_missing_raises(tmp_path) -> None:
    paths = LocalParquetPaths(tmp_path)
    with pytest.raises(FileNotFoundError):
        assert_local_parquet_files_exist(paths)


def test_validate_offline_contract_empty_tables_ok(tmp_path) -> None:
    paths = LocalParquetPaths(tmp_path)
    _write_empty_parquets_with_contract_columns(paths)
    report = validate_offline_inputs_or_raise(paths)
    assert report.bet.num_rows == 0
    assert report.session.num_rows == 0
    assert report.missing_required_bet_cols == ()
    assert report.missing_required_session_cols == ()


def test_validate_offline_reports_missing_required_column(tmp_path) -> None:
    paths = LocalParquetPaths(tmp_path)
    sess_df = pd.DataFrame({"session_id": pd.Series([], dtype="int64")})
    sess_df.to_parquet(paths.session_parquet, index=False)

    pd.DataFrame(
        {c: pd.Series([], dtype="float64") for c in ds._REQUIRED_BET_PARQUET_COLS}
    ).to_parquet(paths.bet_parquet, index=False)

    with pytest.raises(ValueError, match="missing columns"):
        validate_offline_inputs_or_raise(paths)


def test_validate_partition_session_ingress_or_raise_ok(tmp_path) -> None:
    shard = tmp_path / "t_session__part_202501.parquet"
    sess_df = pd.DataFrame(
        {c: pd.Series([], dtype="float64") for c in ds._REQUIRED_SESSION_PARQUET_COLS}
    )
    sess_df.to_parquet(shard, index=False)
    rep = ds.validate_partition_session_ingress_or_raise((shard,))
    assert rep.session.num_rows == 0
    assert rep.missing_required_session_cols == ()


def test_validate_partition_bet_ingress_or_raise_ok(tmp_path) -> None:
    shard = tmp_path / "t_bet__part_202501.parquet"
    bet_df = pd.DataFrame({c: pd.Series([], dtype="float64") for c in ds._REQUIRED_BET_PARQUET_COLS})
    bet_df.to_parquet(shard, index=False)
    rep = ds.validate_partition_bet_ingress_or_raise((shard,))
    assert rep.num_rows == 0
