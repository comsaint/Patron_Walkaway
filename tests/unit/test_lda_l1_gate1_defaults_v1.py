"""Tests for default local t_bet source resolution in ``lda_l1_gate1_day_range_v1``."""

import argparse
import importlib.util
from pathlib import Path

import pytest


def _load_lda_l1_script():
    """Load the orchestrator module from ``scripts/`` (not a package)."""
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "lda_l1_gate1_day_range_v1.py"
    spec = importlib.util.spec_from_file_location("lda_l1_gate1_day_range_v1", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_apply_default_lda_source_args_missing_file_raises(tmp_path: Path) -> None:
    mod = _load_lda_l1_script()
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
    )
    with pytest.raises(ValueError, match="No source mode"):
        mod.apply_default_lda_source_args(args, data_root=tmp_path)


def test_apply_default_lda_source_args_uses_gmwds_when_present(tmp_path: Path) -> None:
    mod = _load_lda_l1_script()
    bet = tmp_path / "gmwds_t_bet.parquet"
    bet.write_bytes(b"x")
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
    )
    mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.bet_parquet == bet.resolve()
    assert args.source_snapshot_id == mod._DEFAULT_BET_PARQUET_SOURCE_SNAPSHOT_ID
    assert getattr(args, "_lda_defaulted_local_t_bet", False) is True


def test_apply_default_lda_source_args_respects_explicit_bet(tmp_path: Path) -> None:
    mod = _load_lda_l1_script()
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"x")
    other = tmp_path / "other.parquet"
    other.write_bytes(b"y")
    args = argparse.Namespace(
        bet_parquet=other,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id="snap_custom",
    )
    mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.bet_parquet == other
    assert args.source_snapshot_id == "snap_custom"
    assert not getattr(args, "_lda_defaulted_local_t_bet", False)


def test_apply_default_lda_source_args_keeps_user_snap_with_default_file(tmp_path: Path) -> None:
    mod = _load_lda_l1_script()
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"x")
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=" snap_user ",
    )
    mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.source_snapshot_id == " snap_user "
