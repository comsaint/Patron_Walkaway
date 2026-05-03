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


def _args_for_validate(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "raw_t_bet_parquet": None,
        "bet_parquet": None,
        "l0_existing": False,
        "source_snapshot_id": None,
        "raw_t_session_parquet": None,
        "eligible_player_ids_parquet": None,
        "cutoff_dtm": None,
        "eligible_build_max_session_rows": 5_000_000,
        "eligible_build_duckdb_memory_limit_mb": None,
        "eligible_build_duckdb_threads": 1,
        "eligible_build_failure_context": None,
        "eligible_build_run_log": None,
        "resume": False,
        "force": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_validate_mode_raw_requires_eligible_or_session_cutoff() -> None:
    args = _args_for_validate(raw_t_bet_parquet=Path("bet.parquet"))
    assert lda_mod._validate_mode(args) == 2


def test_validate_mode_raw_session_requires_cutoff() -> None:
    args = _args_for_validate(
        raw_t_bet_parquet=Path("bet.parquet"),
        raw_t_session_parquet=Path("session.parquet"),
    )
    assert lda_mod._validate_mode(args) == 2


def test_validate_mode_raw_with_session_and_cutoff_ok() -> None:
    args = _args_for_validate(
        raw_t_bet_parquet=Path("bet.parquet"),
        raw_t_session_parquet=Path("session.parquet"),
        cutoff_dtm="2026-01-31T23:59:59+08:00",
    )
    assert lda_mod._validate_mode(args) is None


def test_validate_mode_raw_with_explicit_eligible_ok() -> None:
    args = _args_for_validate(
        raw_t_bet_parquet=Path("bet.parquet"),
        eligible_player_ids_parquet=Path("eligible.parquet"),
    )
    assert lda_mod._validate_mode(args) is None


def test_validate_mode_rejects_negative_eligible_build_max_rows() -> None:
    args = _args_for_validate(
        raw_t_bet_parquet=Path("bet.parquet"),
        raw_t_session_parquet=Path("session.parquet"),
        cutoff_dtm="2026-01-31T23:59:59+08:00",
        eligible_build_max_session_rows=-1,
    )
    assert lda_mod._validate_mode(args) == 2


def test_validate_mode_rejects_eligible_build_threads_zero() -> None:
    args = _args_for_validate(
        raw_t_bet_parquet=Path("bet.parquet"),
        raw_t_session_parquet=Path("session.parquet"),
        cutoff_dtm="2026-01-31T23:59:59+08:00",
        eligible_build_duckdb_threads=0,
    )
    assert lda_mod._validate_mode(args) == 2


def test_validate_mode_rejects_eligible_build_memory_below_64() -> None:
    args = _args_for_validate(
        raw_t_bet_parquet=Path("bet.parquet"),
        raw_t_session_parquet=Path("session.parquet"),
        cutoff_dtm="2026-01-31T23:59:59+08:00",
        eligible_build_duckdb_memory_limit_mb=32,
    )
    assert lda_mod._validate_mode(args) == 2


def test_assert_eligible_session_row_budget_noop_when_disabled() -> None:
    lda_mod._assert_eligible_session_row_budget(10**9, max_rows=0)


def test_assert_eligible_session_row_budget_raises_when_over() -> None:
    with pytest.raises(RuntimeError, match="eligible-build-max-session-rows"):
        lda_mod._assert_eligible_session_row_budget(100, max_rows=50)


def test_parse_args_eligible_build_defaults(tmp_path: Path) -> None:
    bet = tmp_path / "b.parquet"
    bet.write_bytes(b"x")
    ns = lda_mod._parse_args(
        [
            "--bet-parquet",
            str(bet),
            "--source-snapshot-id",
            "snap_x",
        ]
    )
    assert ns.eligible_build_max_session_rows == 5_000_000
    assert ns.eligible_build_duckdb_threads == 1
    assert ns.eligible_build_duckdb_memory_limit_mb is None
