"""Tests for default local t_bet + ingestion registry resolution in ``lda_l1_gate1_day_range_v1``."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from pipelines.layered_data_assets.cli import lda_l1_gate1_day_range_v1 as lda_mod


def test_apply_default_lda_source_args_missing_file_raises(tmp_path: Path) -> None:
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
    )
    with pytest.raises(ValueError, match="No source mode"):
        lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)


def test_apply_default_lda_source_args_uses_gmwds_when_present(tmp_path: Path) -> None:
    bet = tmp_path / "gmwds_t_bet.parquet"
    bet.write_bytes(b"x")
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
    )
    lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.bet_parquet == bet.resolve()
    assert args.source_snapshot_id == lda_mod._DEFAULT_BET_PARQUET_SOURCE_SNAPSHOT_ID
    assert getattr(args, "_lda_defaulted_local_t_bet", False) is True


def test_apply_default_lda_source_args_respects_explicit_bet(tmp_path: Path) -> None:
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"x")
    other = tmp_path / "other.parquet"
    other.write_bytes(b"y")
    args = argparse.Namespace(
        bet_parquet=other,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id="snap_custom",
    )
    lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.bet_parquet == other
    assert args.source_snapshot_id == "snap_custom"
    assert not getattr(args, "_lda_defaulted_local_t_bet", False)


def test_apply_default_lda_source_args_keeps_user_snap_with_default_file(tmp_path: Path) -> None:
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"x")
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=" snap_user ",
    )
    lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.source_snapshot_id == " snap_user "


def test_apply_default_ingestion_registry_respects_explicit_yaml(tmp_path: Path) -> None:
    custom = tmp_path / "custom.yaml"
    custom.write_text("registry_version: v0\n", encoding="utf-8")
    args = argparse.Namespace(ingestion_fix_registry_yaml=custom)
    lda_mod.apply_default_ingestion_registry_args(args)
    assert args.ingestion_fix_registry_yaml == custom.resolve()
    assert not getattr(args, "_lda_defaulted_ingestion_registry", False)


def test_apply_default_ingestion_registry_sets_canonical_when_missing(tmp_path: Path) -> None:
    canonical = Path(lda_mod._REPO_ROOT) / "schema" / "preprocess_bet_ingestion_fix_registry.yaml"
    if not canonical.is_file():
        pytest.skip("canonical ingestion registry not present in checkout")
    args = argparse.Namespace(ingestion_fix_registry_yaml=None)
    lda_mod.apply_default_ingestion_registry_args(args)
    assert args.ingestion_fix_registry_yaml == canonical.resolve()
    assert getattr(args, "_lda_defaulted_ingestion_registry", False) is True


def test_apply_default_ingestion_registry_raises_when_explicit_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing_registry.yaml"
    args = argparse.Namespace(ingestion_fix_registry_yaml=missing)
    with pytest.raises(FileNotFoundError, match="ingestion fix registry not found"):
        lda_mod.apply_default_ingestion_registry_args(args)


def test_apply_default_ingestion_registry_raises_when_canonical_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lda_mod, "_REPO_ROOT", tmp_path)
    args = argparse.Namespace(ingestion_fix_registry_yaml=None)
    with pytest.raises(ValueError, match="Required ingestion fix registry is missing"):
        lda_mod.apply_default_ingestion_registry_args(args)
