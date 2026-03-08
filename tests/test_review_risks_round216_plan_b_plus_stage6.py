"""Minimal reproducible tests for Round 216 Review — 方案 B+ 階段 6 第 1 步.

Converts STATUS.md « Round 216 Review » risk items into executable tests.
Tests only; no production code changes.
Tests that assert behaviour not yet in production use @unittest.expectedFailure.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _get_func_src(name: str) -> str:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    raise AssertionError(f"function {name!r} not found in trainer.py")


def _minimal_train_valid_test(feature_cols: list[str], n_train: int = 2, n_valid: int = 1):
    """Minimal train_df, valid_df for train_single_rated_model (rated rows, label 0/1)."""
    train = pd.DataFrame(
        {"label": [0, 1][:n_train], "is_rated": [True] * n_train, **{c: [0.0] * n_train for c in feature_cols}},
    )
    valid = pd.DataFrame(
        {"label": [0] * n_valid, "is_rated": [True] * n_valid, **{c: [0.0] * n_valid for c in feature_cols}},
    )
    return train, valid


def _write_libsvm_pair(
    root: Path,
    train_lines: list[str],
    weight_lines: list[str] | None,
    valid_lines: list[str],
    train_name: str = "train.libsvm",
    valid_name: str = "valid.libsvm",
) -> tuple[Path, Path]:
    """Write train_name, train_name.weight, valid_name; return (train_path, valid_path)."""
    train_p = root / train_name
    valid_p = root / valid_name
    train_p.write_text("\n".join(train_lines) + ("\n" if train_lines else ""), encoding="utf-8")
    valid_p.write_text("\n".join(valid_lines) + ("\n" if valid_lines else ""), encoding="utf-8")
    if weight_lines is not None:
        (root / (train_name + ".weight")).write_text(
            "\n".join(weight_lines) + ("\n" if weight_lines else ""), encoding="utf-8"
        )
    return train_p, valid_p


# ---------------------------------------------------------------------------
# Review #1: valid_df=None 時不應 AttributeError，validation 應來自檔案
# ---------------------------------------------------------------------------

class TestR216_1_ValidDfNoneNoAttributeError(unittest.TestCase):
    """Review #1: When valid_df is None and train_libsvm_paths exist, no AttributeError; metrics from file."""

    def test_valid_df_none_with_libsvm_paths_returns_metrics_no_attribute_error(self):
        """Call train_single_rated_model(valid_df=None, train_libsvm_paths=(train_p, valid_p)) → no AttributeError, metrics."""
        from trainer.trainer import train_single_rated_model, DATA_DIR

        # LibSVM with 0-based indices 0:,1: so num_feature=2 matches len(feature_cols)=2 (LightGBM)
        feature_cols = ["f1", "f2"]
        train_df, _ = _minimal_train_valid_test(feature_cols)
        # Use temp dir under DATA_DIR so path check passes and validation from file is used (R216 #6)
        with tempfile.TemporaryDirectory(dir=str(DATA_DIR)) as d:
            root = Path(d)
            train_p, valid_p = _write_libsvm_pair(
                root,
                train_lines=["0 0:0.0 1:0.0", "1 0:0.1 1:0.0"],
                weight_lines=["1.0", "1.0"],
                valid_lines=["0 0:0.0 1:0.0", "1 0:0.0 1:0.0"],
            )
            art, _, metrics = train_single_rated_model(
                train_df,
                None,  # valid_df=None
                feature_cols,
                run_optuna=False,
                train_libsvm_paths=(train_p, valid_p),
            )
            self.assertIsNotNone(metrics.get("rated"))
            self.assertIn("val_ap", metrics.get("rated") or {})


# ---------------------------------------------------------------------------
# Review #2: _labels_from_libsvm 檔案不存在 / 空檔
# ---------------------------------------------------------------------------

class TestR216_2_LabelsFromLibsvmFileNotFoundOrEmpty(unittest.TestCase):
    """Review #2: _labels_from_libsvm raises FileNotFoundError for nonexistent path; empty file → shape (0,)."""

    def test_labels_from_libsvm_nonexistent_path_raises_file_not_found(self):
        """_labels_from_libsvm(Path('/nonexistent')) raises FileNotFoundError (current behaviour)."""
        from trainer.trainer import _labels_from_libsvm

        nonexistent = Path("/nonexistent_path_round216_libsvm")
        with self.assertRaises(FileNotFoundError):
            _labels_from_libsvm(nonexistent)

    def test_labels_from_libsvm_empty_file_returns_empty_array(self):
        """_labels_from_libsvm(empty_file) returns array with shape (0,)."""
        from trainer.trainer import _labels_from_libsvm

        with tempfile.NamedTemporaryFile(mode="w", suffix=".libsvm", delete=False) as f:
            empty_path = Path(f.name)
        try:
            empty_path.write_text("", encoding="utf-8")
            out = _labels_from_libsvm(empty_path)
            self.assertIsInstance(out, np.ndarray)
            self.assertEqual(out.shape, (0,))
            self.assertEqual(out.dtype, np.float64)
        finally:
            empty_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Review #3: 非法 label 行被略過，回傳長度為有效行數
# ---------------------------------------------------------------------------

class TestR216_3_LabelsFromLibsvmSkipsInvalidLines(unittest.TestCase):
    """Review #3: _labels_from_libsvm skips lines that do not parse as float label; return length = valid lines."""

    def test_labels_from_libsvm_one_valid_one_invalid_returns_length_one(self):
        """File with '1 1:0.5' and 'x 1:0.5' → return length 1 (invalid line skipped)."""
        from trainer.trainer import _labels_from_libsvm

        with tempfile.NamedTemporaryFile(mode="w", suffix=".libsvm", delete=False) as f:
            path = Path(f.name)
        try:
            path.write_text("1 1:0.5\nx 1:0.5\n", encoding="utf-8")
            out = _labels_from_libsvm(path)
            self.assertEqual(len(out), 1)
            self.assertEqual(float(out[0]), 1.0)
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Review #4: booster.predict(path) 單列回傳 1 維長度 1；0 列時應有防呆
# ---------------------------------------------------------------------------

class TestR216_4_PredictPathShape(unittest.TestCase):
    """Review #4: predict from file path returns 1-d array; 0-row branch should guard before predict."""

    def test_predict_path_reshape_logic_single_row_yields_1d_length_one(self):
        """Regression: trainer's reshape of predict(path) for single row must yield 1-d length 1 (scalar or 1-elem)."""
        # Same logic as trainer: _raw = booster.predict(...); val_scores = np.asarray(_raw).reshape(-1) if np.ndim(_raw) else np.asarray([_raw]).reshape(-1)
        def reshape_like_trainer(_raw):
            return np.asarray(_raw).reshape(-1) if np.ndim(_raw) else np.asarray([_raw]).reshape(-1)

        for _raw in (0.5, np.array(0.5), np.array([0.5])):
            pred = reshape_like_trainer(_raw)
            self.assertEqual(pred.ndim, 1, f"reshape must be 1-d for _raw={_raw}")
            self.assertEqual(len(pred), 1, f"reshape must have length 1 for _raw={_raw}")

    def test_from_file_validation_branch_guards_zero_labels_before_predict(self):
        """Contract: in from-file validation branch, when len(y_vl)==0 we must not call booster.predict(path)."""
        src = _get_func_src("train_single_rated_model")
        idx = src.find("booster.predict(str(valid_libsvm_p))")
        self.assertGreater(idx, 0, "booster.predict(str(valid_libsvm_p)) not found")
        segment = src[max(0, idx - 800) : idx]
        # Require guard on len(y_vl) so predict is skipped when empty (e.g. "len(y_vl) == 0" or "len(y_vl) > 0")
        has_guard = "len(y_vl)" in segment and (
            "== 0" in segment or "> 0" in segment or ">= 1" in segment or "!= 0" in segment
        )
        self.assertTrue(
            has_guard,
            "R216 Review #4: from-file branch should guard on len(y_vl) before calling booster.predict(path).",
        )


# ---------------------------------------------------------------------------
# Review #6: valid_libsvm_p 應限制在 DATA_DIR 下（契約）
# ---------------------------------------------------------------------------

class TestR216_6_ValidLibsvmPathUnderDataDir(unittest.TestCase):
    """Review #6: Before using valid_libsvm_p for labels/predict, path must be restricted to DATA_DIR."""

    def test_train_single_rated_model_checks_valid_path_under_data_dir(self):
        """Contract: before _labels_from_libsvm(valid_libsvm_p) or predict(valid_libsvm_p), path must be checked under DATA_DIR."""
        src = _get_func_src("train_single_rated_model")
        idx_labels = src.find("_labels_from_libsvm(valid_libsvm_p)")
        idx_predict = src.find("booster.predict(str(valid_libsvm_p))")
        self.assertGreater(idx_labels, 0, "_labels_from_libsvm(valid_libsvm_p) not found")
        # Segment from unpack of train_libsvm_paths up to first file use: must contain path-under-DATA_DIR check
        idx_unpack = src.find("train_libsvm_p, valid_libsvm_p = train_libsvm_paths")
        self.assertGreater(idx_unpack, 0, "train_libsvm_p unpack not found")
        segment = src[idx_unpack : max(idx_labels, idx_predict)]
        has_check = (
            "DATA_DIR" in segment
            and "resolve" in segment
            and ("valid_libsvm_p" in segment or "path" in segment)
        )
        self.assertTrue(
            has_check,
            "R216 Review #6: between unpack and file use, valid_libsvm_p must be checked to be under DATA_DIR (resolve).",
        )


if __name__ == "__main__":
    unittest.main()
