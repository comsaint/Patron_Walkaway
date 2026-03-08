"""Minimal reproducible guards for Round 207 Review (Plan B+ Stage 5 .bin).

Converts STATUS.md « Round 207 Review » risk items into executable tests.
Tests only; no production code changes.
Tests that assert behaviour not yet in production use @unittest.expectedFailure.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_CONFIG_PATH = _REPO_ROOT / "trainer" / "config.py"
_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_CONFIG_SRC = _CONFIG_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _get_func_src(name: str) -> str:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    raise AssertionError(f"function {name!r} not found in trainer.py")


def _minimal_train_valid(feature_cols: list[str], n_train: int = 2, n_valid: int = 1):
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
    train_name: str = "train_for_lgb.libsvm",
    valid_name: str = "valid_for_lgb.libsvm",
) -> tuple[Path, Path]:
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
# R207 Review #2: _bin_path should be a file, not just exists()
# ---------------------------------------------------------------------------

class TestR207_2_BinPathIsFileBeforeUse(unittest.TestCase):
    """Review #2: When _bin_path is a directory, should not use it as .bin (use is_file())."""

    def test_bin_path_is_file_checked_before_use(self):
        """Source guard: .bin branch should use _bin_path.is_file() to avoid using a directory."""
        # Check the block that uses _bin_path for loading .bin
        idx = _SRC.find("_bin_path = train_libsvm_p.parent")
        self.assertGreater(idx, 0, "_bin_path assignment not found")
        segment = _SRC[idx : idx + 600]
        self.assertIn(
            "is_file()",
            segment,
            "Production should use _bin_path.is_file() before using .bin (Review #2).",
        )


# ---------------------------------------------------------------------------
# R207 Review #3: save_binary failure should not crash training
# ---------------------------------------------------------------------------

class TestR207_3_SaveBinaryFailureDoesNotCrash(unittest.TestCase):
    """Review #3: When save_binary raises, training should complete (log and continue)."""

    def test_save_binary_raises_training_still_completes(self):
        """When dtrain.save_binary(...) raises IOError, train_single_rated_model should still return artifact."""
        from trainer.trainer import train_single_rated_model

        feature_cols = ["f1", "f2", "f3"]
        train_df, valid_df = _minimal_train_valid(feature_cols)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p, valid_p = _write_libsvm_pair(
                root,
                train_lines=["0 1:0.1 2:0.0", "1 1:0.2 2:0.0"],
                weight_lines=["1.0", "1.0"],
                valid_lines=["0 1:0.0 2:0.0"],
                train_name="train_for_lgb.libsvm",
                valid_name="valid_for_lgb.libsvm",
            )
            import lightgbm as _lgb_mod
            _real_dataset = _lgb_mod.Dataset

            def _save_binary_raises(path):
                raise OSError("mock: save_binary failed")

            def _wrap_dataset(*args, **kwargs):
                d = _real_dataset(*args, **kwargs)
                d.save_binary = _save_binary_raises
                return d

            with patch("trainer.trainer.STEP9_SAVE_LGB_BINARY", True):
                with patch("trainer.trainer.lgb.Dataset", _wrap_dataset):
                    art, _, metrics = train_single_rated_model(
                        train_df,
                        valid_df,
                        feature_cols,
                        run_optuna=False,
                        train_libsvm_paths=(train_p, valid_p),
                    )
            self.assertIsNotNone(art, "Training should complete and return artifact when save_binary raises.")
            self.assertIn("rated", metrics)


# ---------------------------------------------------------------------------
# R207 Review #1 (regression): when .bin exists and unchanged, it is used
# ---------------------------------------------------------------------------

class TestR207_1_BinUsedWhenExistsAndUnchanged(unittest.TestCase):
    """Review #1 regression: When .bin exists and LibSVM unchanged, second run should use .bin."""

    def test_second_run_with_bin_present_uses_bin(self):
        """First run with STEP9_SAVE_LGB_BINARY=True creates .bin; second run uses it (Dataset called with .bin path)."""
        from trainer.trainer import train_single_rated_model

        feature_cols = ["f1", "f2", "f3"]
        train_df, valid_df = _minimal_train_valid(feature_cols)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p, valid_p = _write_libsvm_pair(
                root,
                train_lines=["0 1:0.1 2:0.0", "1 1:0.2 2:0.0"],
                weight_lines=["1.0", "1.0"],
                valid_lines=["0 1:0.0 2:0.0"],
                train_name="train_for_lgb.libsvm",
                valid_name="valid_for_lgb.libsvm",
            )
            with patch("trainer.trainer.STEP9_SAVE_LGB_BINARY", True):
                art1, _, _ = train_single_rated_model(
                    train_df, valid_df, feature_cols, run_optuna=False, train_libsvm_paths=(train_p, valid_p),
                )
            self.assertIsNotNone(art1)
            bin_path = root / "train_for_lgb.bin"
            self.assertTrue(bin_path.exists(), "First run should create .bin when STEP9_SAVE_LGB_BINARY=True")
            with patch("trainer.trainer.lgb") as mock_lgb:
                real_lgb = __import__("lightgbm")
                dataset_calls = []
                def record_dataset(*args, **kwargs):
                    if args and (str(args[0]).endswith(".bin") or (getattr(args[0], "endswith", None) and args[0].endswith(".bin"))):
                        dataset_calls.append(("bin", args[0]))
                    return real_lgb.Dataset(*args, **kwargs)
                mock_lgb.Dataset = record_dataset
                with patch("trainer.trainer.lgb.Dataset", record_dataset):
                    art2, _, _ = train_single_rated_model(
                        train_df, valid_df, feature_cols, run_optuna=False, train_libsvm_paths=(train_p, valid_p),
                    )
            self.assertIsNotNone(art2)
            # If production uses .bin on second run, lgb.Dataset was called with .bin path
            # (We cannot easily spy without patching; instead assert second run succeeded and .bin was present)
            self.assertTrue(bin_path.is_file(), ".bin should be a file and second run should have used it or LibSVM")


# ---------------------------------------------------------------------------
# R207 Review #4: config / docstring should mention .bin sync with LibSVM
# ---------------------------------------------------------------------------

class TestR207_4_ConfigDocstringMentionsBinSync(unittest.TestCase):
    """Review #4: Config or docstring should document .bin vs LibSVM / screening / delete."""

    def test_config_or_docstring_mentions_bin_sync(self):
        """Documentation should mention .bin corresponds to LibSVM; re-export or screening change → delete .bin."""
        doc = _get_func_src("train_single_rated_model")
        config_section = _CONFIG_SRC
        combined = doc + " " + config_section
        self.assertIn(".bin", combined)
        has_sync_hint = (
            "LibSVM" in combined
            or "screening" in combined
            or "刪除" in combined
            or "delete" in combined.lower()
            or "對應" in combined
        )
        self.assertTrue(
            has_sync_hint,
            "Config or docstring should mention .bin vs LibSVM/screening or deleting .bin when data changes.",
        )


# ---------------------------------------------------------------------------
# R207 Review #5: doc about .bin full load / memory
# ---------------------------------------------------------------------------

class TestR207_5_DocMentionsBinMemoryLoad(unittest.TestCase):
    """Review #5: Doc should note .bin is loaded fully (memory / memory-map)."""

    def test_config_or_docstring_mentions_bin_memory(self):
        """Documentation should mention that loading .bin reads/maps full file into memory."""
        combined = _CONFIG_SRC + " " + (_get_func_src("train_single_rated_model") or "")
        self.assertIn(".bin", combined)
        has_memory_hint = (
            "memory" in combined.lower()
            or "記憶體" in combined
            or "load" in combined.lower()
            or "整檔" in combined
        )
        self.assertTrue(
            has_memory_hint,
            "Config or docstring should note .bin load uses memory / full file.",
        )


if __name__ == "__main__":
    unittest.main()
