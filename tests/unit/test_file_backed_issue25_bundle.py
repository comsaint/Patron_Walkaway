"""Issue #25: file-backed split helpers and strict env flag."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trainer.training.split_file_bundle import (
    forbid_file_backed_strict_dense_predict,
    merge_libsvm_files,
    merge_train_valid_weight_files,
    read_split_manifest,
    trainer_file_backed_strict_enabled,
    validate_libsvm_paths_exist,
    write_split_manifest,
)


def test_trainer_file_backed_strict_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRAINER_FILE_BACKED_STRICT", raising=False)
    assert trainer_file_backed_strict_enabled() is False
    monkeypatch.setenv("TRAINER_FILE_BACKED_STRICT", "1")
    assert trainer_file_backed_strict_enabled() is True
    monkeypatch.setenv("TRAINER_FILE_BACKED_STRICT", "On")
    assert trainer_file_backed_strict_enabled() is True


def test_validate_libsvm_paths_exist_requires_weight(tmp_path: Path) -> None:
    tr = tmp_path / "t.libsvm"
    va = tmp_path / "v.libsvm"
    tr.write_text("1 0:1\n", encoding="utf-8")
    va.write_text("0 0:1\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=r"\.weight"):
        validate_libsvm_paths_exist(tr, va)


def test_forbid_file_backed_strict_dense_predict_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAINER_FILE_BACKED_STRICT", "1")
    with pytest.raises(RuntimeError, match="TRAINER_FILE_BACKED_STRICT"):
        forbid_file_backed_strict_dense_predict(role="validation", detail="unit")


def test_write_split_manifest_schema_v2_fields(tmp_path: Path) -> None:
    tr = tmp_path / "t.libsvm"
    va = tmp_path / "v.libsvm"
    tr.write_text("1 0:1\n", encoding="utf-8")
    va.write_text("0 0:1\n", encoding="utf-8")
    (tmp_path / "t.libsvm.weight").write_text("1.0\n", encoding="utf-8")
    p = write_split_manifest(
        tmp_path,
        train_libsvm=tr,
        valid_libsvm=va,
        test_libsvm=None,
        feature_columns=["f0"],
        train_row_count=1,
        valid_row_count=1,
        test_row_count=None,
        feature_index_base="zero",
        trainer_file_backed_strict_effective=True,
        backends_contract=["lightgbm", "catboost", "xgboost"],
    )
    doc = read_split_manifest(tmp_path)
    assert doc["schema_version"] == 2
    assert doc["feature_index_base"] == "zero"
    assert doc["trainer_file_backed_strict_effective"] is True
    assert doc["backends_contract"] == ["lightgbm", "catboost", "xgboost"]
    assert p.is_file()


def test_merge_libsvm_and_weights(tmp_path: Path) -> None:
    tr = tmp_path / "t.libsvm"
    va = tmp_path / "v.libsvm"
    tr.write_text("1 0:1\n", encoding="utf-8")
    va.write_text("0 0:2\n", encoding="utf-8")
    tw = tmp_path / "t.libsvm.weight"
    tw.write_text("0.5\n", encoding="utf-8")
    out = tmp_path / "m.libsvm"
    n = merge_libsvm_files(out, [tr, va])
    assert n == 2
    assert "0:2" in out.read_text(encoding="utf-8")
    mw = tmp_path / "m.libsvm.weight"
    merge_train_valid_weight_files(mw, train_weight_txt=tw, valid_libsvm=va)
    lines = [ln.strip() for ln in mw.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines == ["0.5", "1.0"]
