"""Tests for default local t_bet + ingestion registry resolution in ``lda_l1_gate1_day_range_v1``."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
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
        raw_t_session_parquet=None,
        eligible_player_ids_parquet=None,
        canonical_mapping_parquet=None,
        cutoff_dtm=None,
    )
    lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.bet_parquet == bet.resolve()
    assert args.source_snapshot_id == lda_mod._DEFAULT_BET_PARQUET_SOURCE_SNAPSHOT_ID
    assert getattr(args, "_lda_defaulted_local_t_bet", False) is True
    assert args.raw_t_session_parquet is None


def test_apply_default_gmwds_prefers_canonical_mapping_when_present(tmp_path: Path) -> None:
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"b")
    (tmp_path / "canonical_mapping.parquet").write_bytes(b"m")
    (tmp_path / "gmwds_t_session.parquet").write_bytes(b"s")
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
        raw_t_session_parquet=None,
        eligible_player_ids_parquet=None,
        canonical_mapping_parquet=None,
        cutoff_dtm=None,
    )
    lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.canonical_mapping_parquet == (tmp_path / "canonical_mapping.parquet").resolve()
    assert args.raw_t_session_parquet is None
    assert getattr(args, "_lda_defaulted_canonical_for_rated", False) is True


def test_apply_default_gmwds_path_b_sets_session_and_cutoff_from_sidecar(tmp_path: Path) -> None:
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"b")
    (tmp_path / "gmwds_t_session.parquet").write_bytes(b"s")
    side = tmp_path / "canonical_mapping.cutoff.json"
    side.write_text(
        '{"cutoff_dtm": "2026-03-01T00:00:00", "dummy_player_ids": []}\n',
        encoding="utf-8",
    )
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
        raw_t_session_parquet=None,
        eligible_player_ids_parquet=None,
        canonical_mapping_parquet=None,
        cutoff_dtm=None,
    )
    lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.raw_t_session_parquet == (tmp_path / "gmwds_t_session.parquet").resolve()
    assert args.cutoff_dtm == "2026-03-01T00:00:00"
    assert getattr(args, "_lda_defaulted_gmwds_session", False) is True
    assert getattr(args, "_lda_defaulted_cutoff_from_sidecar", False) is True


def test_apply_default_gmwds_path_b_raises_when_session_without_cutoff_sidecar(
    tmp_path: Path,
) -> None:
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"b")
    (tmp_path / "gmwds_t_session.parquet").write_bytes(b"s")
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
        raw_t_session_parquet=None,
        eligible_player_ids_parquet=None,
        canonical_mapping_parquet=None,
        cutoff_dtm=None,
    )
    with pytest.raises(ValueError, match="canonical_mapping.cutoff.json"):
        lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)


def test_read_cutoff_dtm_prefix_from_canonical_sidecar(tmp_path: Path) -> None:
    p = tmp_path / "canonical_mapping.cutoff.json"
    p.write_text(
        '{"cutoff_dtm": "2026-01-15T12:00:00+08:00", "dummy_player_ids": [1, 2, 3]}',
        encoding="utf-8",
    )
    assert lda_mod._read_cutoff_dtm_prefix_from_canonical_sidecar(p) == "2026-01-15T12:00:00+08:00"


def test_apply_default_gmwds_path_b_uses_cli_cutoff_without_sidecar(tmp_path: Path) -> None:
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"b")
    (tmp_path / "gmwds_t_session.parquet").write_bytes(b"s")
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
        raw_t_session_parquet=None,
        eligible_player_ids_parquet=None,
        canonical_mapping_parquet=None,
        cutoff_dtm="2026-02-01T00:00:00Z",
    )
    lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.raw_t_session_parquet == (tmp_path / "gmwds_t_session.parquet").resolve()
    assert args.cutoff_dtm == "2026-02-01T00:00:00Z"
    assert not getattr(args, "_lda_defaulted_cutoff_from_sidecar", False)


def test_apply_default_gmwds_path_b_skipped_when_explicit_eligible(tmp_path: Path) -> None:
    (tmp_path / "gmwds_t_bet.parquet").write_bytes(b"b")
    (tmp_path / "gmwds_t_session.parquet").write_bytes(b"s")
    elig = tmp_path / "eligible.parquet"
    elig.write_bytes(b"e")
    args = argparse.Namespace(
        bet_parquet=None,
        raw_t_bet_parquet=None,
        l0_existing=False,
        source_snapshot_id=None,
        raw_t_session_parquet=None,
        eligible_player_ids_parquet=elig,
        canonical_mapping_parquet=None,
        cutoff_dtm=None,
    )
    lda_mod.apply_default_lda_source_args(args, data_root=tmp_path)
    assert args.raw_t_session_parquet is None
    assert args.cutoff_dtm is None


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
        "canonical_mapping_parquet": None,
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


def test_validate_mode_raw_with_canonical_mapping_only_ok(tmp_path: Path) -> None:
    cm = tmp_path / "canonical_mapping.parquet"
    cm.write_bytes(b"x")
    args = _args_for_validate(
        raw_t_bet_parquet=Path("bet.parquet"),
        canonical_mapping_parquet=cm,
    )
    assert lda_mod._validate_mode(args) is None


def test_validate_mode_bet_with_session_requires_cutoff_or_allowlist(tmp_path: Path) -> None:
    bet = tmp_path / "t_bet.parquet"
    sess = tmp_path / "t_session.parquet"
    bet.write_bytes(b"a")
    sess.write_bytes(b"b")
    args = _args_for_validate(
        bet_parquet=bet,
        source_snapshot_id="snap_x",
        raw_t_session_parquet=sess,
    )
    assert lda_mod._validate_mode(args) == 2


def test_validate_mode_bet_with_session_and_cutoff_ok(tmp_path: Path) -> None:
    bet = tmp_path / "t_bet.parquet"
    sess = tmp_path / "t_session.parquet"
    bet.write_bytes(b"a")
    sess.write_bytes(b"b")
    args = _args_for_validate(
        bet_parquet=bet,
        source_snapshot_id="snap_x",
        raw_t_session_parquet=sess,
        cutoff_dtm="2026-01-31T23:59:59+08:00",
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


def test_materialize_eligible_from_canonical_mapping_parquet_dedup(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    src = tmp_path / "canonical_mapping.parquet"
    pd.DataFrame({"player_id": [1, 1, 2], "canonical_id": ["a", "a", "b"]}).to_parquet(
        src, index=False
    )
    out = lda_mod._materialize_eligible_from_canonical_mapping_parquet(
        canonical_mapping_parquet=src,
        data_root=tmp_path,
        ignore_cache=True,
    )
    got = pd.read_parquet(out)
    assert sorted(int(x) for x in got["player_id"].tolist()) == [1, 2]
    out2 = lda_mod._materialize_eligible_from_canonical_mapping_parquet(
        canonical_mapping_parquet=src,
        data_root=tmp_path,
        ignore_cache=False,
    )
    assert out2 == out


def _args_for_resolve(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "eligible_player_ids_parquet": None,
        "canonical_mapping_parquet": None,
        "raw_t_bet_parquet": None,
        "bet_parquet": None,
        "raw_t_session_parquet": None,
        "cutoff_dtm": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_resolve_eligible_builds_missing_canonical_via_trainer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bet = tmp_path / "t_bet.parquet"
    sess = tmp_path / "t_session.parquet"
    bet.write_bytes(b"b")
    sess.write_bytes(b"s")
    expected_cm = tmp_path / "canonical_mapping.parquet"
    expected_elig = tmp_path / "eligible.parquet"
    calls: dict[str, object] = {}

    def _fake_build(*, raw_t_session_parquet: Path, cutoff_dtm, canonical_mapping_parquet: Path, sidecar_json: Path, emit_stderr=None) -> Path:
        calls["build"] = (raw_t_session_parquet, cutoff_dtm, canonical_mapping_parquet, sidecar_json)
        pd.DataFrame({"player_id": [1], "canonical_id": ["x"]}).to_parquet(canonical_mapping_parquet, index=False)
        return canonical_mapping_parquet

    def _fake_materialize(*, canonical_mapping_parquet: Path, data_root: Path, emit_stderr=None, ignore_cache: bool = False) -> Path:
        calls["materialize"] = (canonical_mapping_parquet, data_root, ignore_cache)
        expected_elig.write_bytes(b"e")
        return expected_elig

    monkeypatch.setattr(lda_mod, "_build_canonical_mapping_parquet_via_trainer", _fake_build)
    monkeypatch.setattr(lda_mod, "_materialize_eligible_from_canonical_mapping_parquet", _fake_materialize)
    args = _args_for_resolve(
        bet_parquet=bet,
        raw_t_session_parquet=sess,
        cutoff_dtm="2026-03-01T00:00:00",
    )
    out = lda_mod._resolve_eligible_player_ids_parquet(
        args=args,
        data_root=tmp_path,
        dry_run=False,
        force=True,
    )
    assert out == expected_elig
    assert calls["build"] is not None
    cm_path, root_path, ignore_cache = calls["materialize"]  # type: ignore[misc]
    assert cm_path == expected_cm.resolve()
    assert root_path == tmp_path
    assert ignore_cache is True


def test_resolve_eligible_missing_explicit_canonical_without_session_raises(tmp_path: Path) -> None:
    args = _args_for_resolve(
        canonical_mapping_parquet=tmp_path / "missing.parquet",
        bet_parquet=tmp_path / "t_bet.parquet",
    )
    with pytest.raises(FileNotFoundError, match="canonical-mapping parquet not found"):
        lda_mod._resolve_eligible_player_ids_parquet(
            args=args,
            data_root=tmp_path,
            dry_run=False,
            force=False,
        )


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
