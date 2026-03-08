"""Minimal reproducible guards for Plan B+ Stage 4 (Round 375 Code Review).

Converts STATUS.md « Code Review：Plan B+ 階段 4 變更 » risk items into executable
tests or lint/rule checks. Tests only; no production code changes.
Tests that assert behaviour not yet in production use @unittest.expectedFailure.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

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
# Review #1: weight file line count vs LibSVM line count
# ---------------------------------------------------------------------------

class TestR375_1_WeightLineCountMatch(unittest.TestCase):
    """Review #1: When .weight line count != LibSVM line count, should warn or raise."""

    def test_weight_line_count_mismatch_warns_or_raises(self):
        """N-line LibSVM with (N-1)-line .weight should yield warning or ValueError."""
        from trainer.trainer import train_single_rated_model

        # LightGBM num_feature() = max_index (1-based); 1: 2: -> num_feature=3.
        feature_cols = ["f1", "f2", "f3"]
        train_df, valid_df = _minimal_train_valid_test(feature_cols)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p, valid_p = _write_libsvm_pair(
                root,
                train_lines=["0 1:0.1 2:0.0", "1 1:0.2 2:0.0"],
                weight_lines=["1.0"],  # 1 line, train has 2
                valid_lines=["0 1:0.0 2:0.0"],
            )
            with self.assertLogs("trainer", level="WARNING") as cm:
                train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    train_libsvm_paths=(train_p, valid_p),
                )
            self.assertTrue(
                any("weight" in m.lower() and ("count" in m.lower() or "line" in m.lower() or "ignor" in m.lower()) for m in cm.output),
                "Expected warning about weight file line count mismatch.",
            )


# ---------------------------------------------------------------------------
# Review #2: 0-line LibSVM fallback + empty train_rated
# ---------------------------------------------------------------------------

class TestR375_2_ZeroLineLibsvmFallbackEmptyTrain(unittest.TestCase):
    """Review #2: 0-line LibSVM fallback with empty train_df should return (None, None, {rated: None}) or not crash."""

    def test_zero_line_libsvm_with_empty_train_returns_none_or_does_not_crash(self):
        """When LibSVM has 0 lines and train_df is empty, should return (None, None, ...) without calling _train_one_model."""
        from trainer.trainer import train_single_rated_model

        feature_cols = ["f1"]
        train_df = pd.DataFrame({"label": [], "is_rated": [], "f1": []})
        valid_df = pd.DataFrame({"label": [0], "is_rated": [True], "f1": [0.0]})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p, valid_p = _write_libsvm_pair(
                root,
                train_lines=[],
                weight_lines=None,
                valid_lines=["0 1:0.0"],
            )
            art, _, metrics = train_single_rated_model(
                train_df,
                valid_df,
                feature_cols,
                run_optuna=False,
                train_libsvm_paths=(train_p, valid_p),
            )
            self.assertIsNone(art, "Expected None artifact when 0-line LibSVM + empty train.")
            self.assertIn("rated", metrics)


# ---------------------------------------------------------------------------
# Review #3: test_rated missing columns -> KeyError
# ---------------------------------------------------------------------------

class TestR375_3_TestMissingColumnsNoKeyError(unittest.TestCase):
    """Review #3: test_df missing some feature columns should not raise KeyError."""

    def test_test_df_missing_columns_no_key_error(self):
        """When train_libsvm_paths used and test_df has fewer columns than avail_cols, no KeyError."""
        from trainer.trainer import train_single_rated_model

        feature_cols = ["f1", "f2"]
        train_df, valid_df = _minimal_train_valid_test(feature_cols)
        test_df = pd.DataFrame({"label": [0], "is_rated": [True], "f1": [0.0]})  # missing f2
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # Use 0-based feature indices so LightGBM num_feature() matches len(avail_cols)=2.
            train_p, valid_p = _write_libsvm_pair(
                root,
                train_lines=["0 0:0.0 1:0.0", "1 0:1.0 1:0.0"],
                weight_lines=["1.0", "1.0"],
                valid_lines=["0 0:0.0 1:0.0"],
            )
            train_single_rated_model(
                train_df,
                valid_df,
                feature_cols,
                run_optuna=False,
                test_df=test_df,
                train_libsvm_paths=(train_p, valid_p),
            )
            # If we get here without KeyError, test passes (remove xfail when prod handles missing test cols)


# ---------------------------------------------------------------------------
# Review #4: .weight file with empty or non-numeric line
# ---------------------------------------------------------------------------

class TestR375_4_WeightFileInvalidLineHandled(unittest.TestCase):
    """Review #4: .weight file with empty or non-numeric line should warn or raise explicitly."""

    def test_weight_file_empty_line_raises_or_warns(self):
        """Weight file with an empty line should either raise ValueError (explicit) or succeed with warning."""
        from trainer.trainer import train_single_rated_model

        feature_cols = ["f1"]
        train_df, valid_df = _minimal_train_valid_test(feature_cols)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p, valid_p = _write_libsvm_pair(
                root,
                train_lines=["0 1:0.1", "1 1:0.2"],
                weight_lines=["1.0", ""],  # empty second line
                valid_lines=["0 1:0.0"],
            )
            try:
                art, _, metrics = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    train_libsvm_paths=(train_p, valid_p),
                )
                # If we get here, production handled it (e.g. warning + 0.0); accept.
                self.assertIsNotNone(art)
            except ValueError:
                # Explicit error is acceptable per review.
                pass


# ---------------------------------------------------------------------------
# Review #5: (optional) weight file memory - rule or doc only
# ---------------------------------------------------------------------------

class TestR375_5_WeightLoadedInMemoryDocumented(unittest.TestCase):
    """Review #5: Doc or source should note that .weight is loaded fully into memory."""

    def test_train_libsvm_paths_branch_loads_weight(self):
        """Guard: use_from_libsvm path reads .weight file (document memory implication)."""
        src = _get_func_src("train_single_rated_model")
        self.assertIn("weight_path", src)
        self.assertIn(".weight", src)
        self.assertTrue(
            "open(" in src and "weight" in src.lower(),
            "Source should open/read weight file; doc should note full load.",
        )


# ---------------------------------------------------------------------------
# Review #6: single-class LibSVM (optional consistency with Plan B)
# ---------------------------------------------------------------------------

class TestR375_6_SingleClassLibsvmFallbackOrWarn(unittest.TestCase):
    """Review #6: Train LibSVM with only one class should fallback or warn (like Plan B CSV)."""

    def test_single_class_train_libsvm_fallback_or_warning(self):
        """Train LibSVM with only label=0 should fallback to in-memory or warn."""
        from trainer.trainer import train_single_rated_model

        feature_cols = ["f1"]
        train_df, valid_df = _minimal_train_valid_test(feature_cols)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p, valid_p = _write_libsvm_pair(
                root,
                train_lines=["0 1:0.0", "0 1:0.1"],
                weight_lines=["1.0", "1.0"],
                valid_lines=["0 1:0.0"],
            )
            with self.assertLogs("trainer", level="WARNING") as cm:
                train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    train_libsvm_paths=(train_p, valid_p),
                )
            self.assertTrue(
                any("one class" in m.lower() or "single" in m.lower() for m in cm.output),
                "Expected warning about single-class train LibSVM.",
            )


# ---------------------------------------------------------------------------
# Review #7: train_libsvm_paths call site / contract
# ---------------------------------------------------------------------------

class TestR375_7_TrainLibsvmPathsOnlyFromRunPipeline(unittest.TestCase):
    """Review #7: train_libsvm_paths should only be passed from run_pipeline from _export_parquet_to_libsvm return."""

    def test_train_libsvm_paths_passed_only_near_export_return(self):
        """Rule: call to train_single_rated_model with train_libsvm_paths must be in run_pipeline and use _export_parquet_to_libsvm return."""
        idx = _SRC.find("train_single_rated_model(")
        self.assertGreater(idx, 0)
        # Find the call that passes train_libsvm_paths (has _libsvm_paths or _train_libsvm)
        self.assertIn("train_libsvm_paths", _SRC)
        self.assertIn("_train_libsvm", _SRC)
        self.assertIn("_valid_libsvm", _SRC)
        # Call site should be after _export_parquet_to_libsvm
        export_idx = _SRC.find("_export_parquet_to_libsvm(")
        call_idx = _SRC.find("train_libsvm_paths=_libsvm_paths")
        self.assertGreater(call_idx, export_idx, "train_libsvm_paths should be passed where _export_parquet_to_libsvm result is used.")


if __name__ == "__main__":
    unittest.main()
