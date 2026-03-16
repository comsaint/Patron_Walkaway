"""Minimal reproducible guards for Plan B+ Stage 3 LibSVM export (Round 372 Code Review).

Converts STATUS.md « Code Review：Plan B+ 階段 3 變更 » risk items into executable
tests or lint/rule checks. Tests only; no production code changes.
Tests that assert behaviour not yet in production use @unittest.expectedFailure.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _get_func_src(name: str) -> str:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    raise AssertionError(f"function {name!r} not found in trainer.py")


def _minimal_parquet(
    dir_path: Path,
    filename: str,
    *,
    label: int | float = 0,
    is_rated: bool = True,
    feature_cols: list[str] | None = None,
    feature_values: dict[str, float | None] | None = None,
    n_rows: int = 1,
    canonical_id: str = "C0",
    run_id: int = 1,
) -> Path:
    """Write a minimal Parquet with label, is_rated, canonical_id, run_id and feature columns."""
    cols = feature_cols or ["f1"]
    df = pd.DataFrame(
        {
            "label": [label] * n_rows,
            "is_rated": [is_rated] * n_rows,
            "canonical_id": [canonical_id] * n_rows,
            "run_id": [run_id] * n_rows,
            **{c: [feature_values.get(c, 0.0) if feature_values else 0.0] * n_rows for c in cols},
        }
    )
    out = dir_path / filename
    df.to_parquet(out, index=False)
    return out


class TestR372LibSVM_1_NaNNotWrittenAsLiteral(unittest.TestCase):
    """Review #1: LibSVM output must not contain literal 'nan' (LightGBM may not parse)."""

    def test_libsvm_output_contains_no_nan_literal_when_feature_is_nan(self):
        """Given Parquet with one feature = NaN, exported LibSVM line must not contain string 'nan'."""
        from trainer.trainer import _export_parquet_to_libsvm

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p = _minimal_parquet(root, "train.parquet", feature_values={"f1": float("nan")})
            valid_p = _minimal_parquet(root, "valid.parquet", feature_values={"f1": 0.0})
            _export_parquet_to_libsvm(train_p, valid_p, ["f1"], root)
            libsvm = root / "train_for_lgb.libsvm"
            content = libsvm.read_text(encoding="utf-8")
        self.assertNotIn("nan", content, "LibSVM must not contain literal 'nan'; coerce NaN to 0 or omit.")


class TestR372LibSVM_2_ZeroRatedRowsWarningOrRaise(unittest.TestCase):
    """Review #2: When train has 0 is_rated rows, must warn or raise (empty file breaks lgb.Dataset)."""

    def test_zero_rated_train_rows_should_warn_or_raise(self):
        """When train Parquet has all is_rated=False, export should log warning or raise ValueError."""
        from trainer.trainer import _export_parquet_to_libsvm
        import logging

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p = _minimal_parquet(root, "train.parquet", is_rated=False, n_rows=2)
            valid_p = _minimal_parquet(root, "valid.parquet", is_rated=True, n_rows=1)
            with self.assertLogs("trainer", level=logging.WARNING) as cm:
                _export_parquet_to_libsvm(train_p, valid_p, ["f1"], root)
            train_lines = (root / "train_for_lgb.libsvm").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(train_lines), 0)
        self.assertTrue(
            any("0" in m or "train" in m.lower() or "row" in m.lower() for m in cm.output),
            "Expected a warning when train has 0 rated rows.",
        )


class TestR372LibSVM_3_FileNotFoundOnMissingPath(unittest.TestCase):
    """Review #3: Missing train_path/valid_path should raise FileNotFoundError with clear message."""

    def test_missing_train_path_raises_filenotfounderror(self):
        """Calling with non-existent train_path should raise FileNotFoundError (or message contains 'not found')."""
        from trainer.trainer import _export_parquet_to_libsvm

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fake_train = root / "nonexistent_train.parquet"
            valid_p = _minimal_parquet(root, "valid.parquet")
            with self.assertRaises((FileNotFoundError, OSError)) as ctx:
                _export_parquet_to_libsvm(fake_train, valid_p, ["f1"], root)
        self.assertTrue(
            "not found" in str(ctx.exception).lower() or "FileNotFoundError" in type(ctx.exception).__name__
            or str(fake_train) in str(ctx.exception),
            "Error message should indicate path not found.",
        )


class TestR372LibSVM_4_BinaryLabelEnforced(unittest.TestCase):
    """Review #4: Label should be 0/1 for binary; non-binary should coerce or raise."""

    def test_non_binary_label_should_be_coerced_or_raise(self):
        """Parquet with label=2 should yield LibSVM with label 0 or 1, or raise ValueError."""
        from trainer.trainer import _export_parquet_to_libsvm

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p = _minimal_parquet(root, "train.parquet", label=2)
            valid_p = _minimal_parquet(root, "valid.parquet", label=0)
            _export_parquet_to_libsvm(train_p, valid_p, ["f1"], root)
            first_line = (root / "train_for_lgb.libsvm").read_text(encoding="utf-8").strip().splitlines()[0]
        label_val = int(first_line.split()[0])
        self.assertIn(label_val, (0, 1), "LibSVM label must be 0 or 1 for binary classification.")


class TestR372LibSVM_5_CallSiteUsesInternalPathsOnly(unittest.TestCase):
    """Review #5: Call site must pass only internal paths (step7_*, DATA_DIR/export)."""

    def test_export_libsvm_is_called_with_step7_paths_and_export_dir(self):
        """Rule: _export_parquet_to_libsvm must be called with step7_train_path, step7_valid_path, export_dir."""
        self.assertIn("_export_parquet_to_libsvm(", _SRC)
        self.assertIn("step7_train_path", _SRC)
        self.assertIn("step7_valid_path", _SRC)
        # Call site is after the function def; first occurrence is "def _export_parquet_to_libsvm("
        idx_def = _SRC.find("def _export_parquet_to_libsvm(")
        idx_call = _SRC.find("_export_parquet_to_libsvm(", idx_def + 30) if idx_def >= 0 else _SRC.find("_export_parquet_to_libsvm(")
        self.assertGreater(idx_call, 0, "Call to _export_parquet_to_libsvm not found.")
        snippet = _SRC[idx_call : idx_call + 450]
        self.assertIn("step7_train_path", snippet, "First arg at call site should be step7_train_path.")
        self.assertIn("step7_valid_path", snippet, "Second arg at call site should be step7_valid_path.")
        self.assertTrue(
            "DATA_DIR" in snippet or ('"export"' in snippet or "'export'" in snippet),
            "Export dir at call site should be DATA_DIR / 'export' or similar.",
        )


class TestR372LibSVM_6_AtomicWriteViaTempThenRename(unittest.TestCase):
    """Review #6: Write to temp then rename to avoid inconsistent .libsvm/.weight on failure."""

    def test_export_uses_temp_file_then_rename(self):
        """_export_parquet_to_libsvm should write to .tmp (or similar) then os.replace to final path."""
        src = _get_func_src("_export_parquet_to_libsvm")
        # Require file-level atomicity: .tmp in open path, or os.replace / Path.replace (not str.replace)
        has_tmp_path = ".tmp" in src and ("open(" in src or "libsvm" in src)
        has_os_replace = "os.replace(" in src or "os.rename(" in src
        has_path_replace = "Path(" in src and ".replace(" in src and "replace('" not in src and "replace(\"" not in src
        self.assertTrue(
            has_tmp_path or has_os_replace or has_path_replace,
            "Export should use temp file then rename/replace to avoid partial .libsvm/.weight on failure.",
        )


class TestR372LibSVM_7_BatchSizeDocumentedOrConfigurable(unittest.TestCase):
    """Review #7 (optional): batch_size 50_000 should be configurable or documented."""

    def test_batch_size_present_in_export_function(self):
        """Guard: export uses a batch_size (for fetchmany); future: config or param."""
        src = _get_func_src("_export_parquet_to_libsvm")
        self.assertIn("batch_size", src)
        self.assertIn("fetchmany", src)


class TestR372LibSVM_8_CommonFeaturesOrExplicitContract(unittest.TestCase):
    """Review #8: Common-feature handling or docstring that feature_cols must exist in both Parquets."""

    def test_valid_missing_feature_column_handled_gracefully(self):
        """When valid Parquet lacks a feature in feature_cols, export should warn and use common or fail with clear error."""
        from trainer.trainer import _export_parquet_to_libsvm

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # Train has f1, f2; valid has only f1
            train_df = pd.DataFrame({
                "label": [0], "is_rated": [True], "canonical_id": ["C0"], "run_id": [1],
                "f1": [0.0], "f2": [1.0],
            })
            valid_df = pd.DataFrame({
                "label": [0], "is_rated": [True], "canonical_id": ["C0"], "run_id": [1],
                "f1": [0.0],
            })
            train_p = root / "train.parquet"
            valid_p = root / "valid.parquet"
            train_df.to_parquet(train_p, index=False)
            valid_df.to_parquet(valid_p, index=False)
            # Either: success using common [f1] only, or clear error (not opaque DuckDB error)
            try:
                _export_parquet_to_libsvm(train_p, valid_p, ["f1", "f2"], root)
                # If we get here, implementation used common cols only
                valid_lines = (root / "valid_for_lgb.libsvm").read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(valid_lines), 1)
            except Exception as e:
                msg = str(e).lower()
                self.assertTrue(
                    "f2" in msg or "column" in msg or "common" in msg or "exist" in msg or "missing" in msg,
                    f"Error message should mention missing column or common features; got: {e!r}",
                )


if __name__ == "__main__":
    unittest.main()
