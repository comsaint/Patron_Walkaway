"""Tests for serving-data copy behavior in package.build_deploy_package."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from package.build_deploy_package import (
    RAW_CH_MIRROR_FILES_EXCLUDED,
    build_deploy_package,
    copy_serving_data_artifacts,
)


def _minimal_model_dir(base: Path) -> Path:
    d = base / "minimal_models"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.pkl").write_bytes(b"\x80\x04\x95\x0c\x00\x00\x00")
    (d / "feature_list.json").write_text("[]", encoding="utf-8")
    (d / "feature_spec.yaml").write_text("{}", encoding="utf-8")
    return d


def test_copy_serving_data_artifacts_skips_raw_parquet(tmp_path: Path) -> None:
    """Raw CH mirror parquets must never be copied into deploy data/."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "player_profile.parquet").write_bytes(b"PAR1" + b"\x00" * 20)
    for name in RAW_CH_MIRROR_FILES_EXCLUDED:
        (src / name).write_text("dummy", encoding="utf-8")
    dst = tmp_path / "dst"
    res = copy_serving_data_artifacts(src, dst, strict_data=False)
    assert res.profile_shipped
    assert sorted(res.excluded_raw_present) == sorted(RAW_CH_MIRROR_FILES_EXCLUDED)
    for name in RAW_CH_MIRROR_FILES_EXCLUDED:
        assert not (dst / name).exists()


def test_copy_serving_data_artifacts_canonical_pair(tmp_path: Path) -> None:
    """Both canonical files must be present to copy either."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "player_profile.parquet").write_bytes(b"PAR1" + b"\x00" * 20)
    (src / "canonical_mapping.parquet").write_text("x", encoding="utf-8")
    dst = tmp_path / "dst"
    res = copy_serving_data_artifacts(src, dst, strict_data=False)
    assert res.profile_shipped
    assert res.canonical_pair_skipped_incomplete_source
    assert not (dst / "canonical_mapping.parquet").exists()


def test_strict_data_raises_when_profile_missing(tmp_path: Path) -> None:
    """--strict-data must fail before wheel build when profile is absent."""
    out = tmp_path / "out"
    model_source = _minimal_model_dir(tmp_path)
    src_data = tmp_path / "empty_data"
    src_data.mkdir()
    with pytest.raises(FileNotFoundError, match="--strict-data"):
        build_deploy_package(
            output_dir=out,
            model_source=model_source,
            create_archive=False,
            data_source=src_data,
            strict_data=True,
        )


def test_build_copies_optional_canonical_and_schema_hash(tmp_path: Path) -> None:
    """Full serving bundle: profile + canonical pair + schema hash."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "player_profile.parquet").write_bytes(b"PAR1" + b"\x00" * 20)
    (src / "canonical_mapping.parquet").write_text("pq", encoding="utf-8")
    (src / "canonical_mapping.cutoff.json").write_text('{"cutoff_dtm": "2026-01-01T00:00:00"}', encoding="utf-8")
    (src / "player_profile.schema_hash").write_text("abc", encoding="utf-8")

    out = tmp_path / "out"
    model_source = _minimal_model_dir(tmp_path)
    fake_whl = "walkaway_ml-0.0.0-py3-none-any.whl"

    with patch("package.build_deploy_package.build_wheel", return_value=fake_whl):
        build_deploy_package(
            output_dir=out,
            model_source=model_source,
            create_archive=False,
            data_source=src,
            strict_data=False,
        )

    data_dir = out / "data"
    assert (data_dir / "player_profile.parquet").is_file()
    assert (data_dir / "canonical_mapping.parquet").is_file()
    assert (data_dir / "canonical_mapping.cutoff.json").is_file()
    assert (data_dir / "player_profile.schema_hash").read_text(encoding="utf-8") == "abc"


def test_build_raw_parquets_not_shipped_even_when_present(tmp_path: Path) -> None:
    """Large raw mirrors under data source must not appear in deploy output."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "player_profile.parquet").write_bytes(b"PAR1" + b"\x00" * 20)
    (src / "gmwds_t_bet.parquet").write_text("big", encoding="utf-8")

    out = tmp_path / "out"
    model_source = _minimal_model_dir(tmp_path)
    fake_whl = "walkaway_ml-0.0.0-py3-none-any.whl"

    with patch("package.build_deploy_package.build_wheel", return_value=fake_whl):
        build_deploy_package(
            output_dir=out,
            model_source=model_source,
            create_archive=False,
            data_source=src,
            strict_data=False,
        )

    assert not (out / "data" / "gmwds_t_bet.parquet").exists()


def test_build_non_strict_missing_profile_stderr(tmp_path: Path) -> None:
    """Without strict-data, missing profile completes build and prints not shipped."""
    src = tmp_path / "src"
    src.mkdir()

    out = tmp_path / "out"
    model_source = _minimal_model_dir(tmp_path)
    fake_whl = "walkaway_ml-0.0.0-py3-none-any.whl"

    stderr_capture = io.StringIO()
    with patch("package.build_deploy_package.build_wheel", return_value=fake_whl):
        with patch("sys.stderr", stderr_capture):
            build_deploy_package(
                output_dir=out,
                model_source=model_source,
                create_archive=False,
                data_source=src,
                strict_data=False,
            )
    assert "not shipped" in stderr_capture.getvalue()
