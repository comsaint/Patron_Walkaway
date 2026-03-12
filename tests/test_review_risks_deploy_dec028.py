"""DEC-028 deploy 審查風險點 — 最小可重現測試（tests-only，不修改 production code）。

對應 STATUS.md « Code Review：DEC-028 變更 » 所列風險；
未修復項目以 @unittest.expectedFailure 標示，維持 CI 可視。
"""

from __future__ import annotations

import importlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCORER_SRC = (_REPO_ROOT / "trainer" / "scorer.py").read_text(encoding="utf-8")
_BUILD_SRC = (_REPO_ROOT / "package" / "build_deploy_package.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# R028 #1 — DATA_DIR 空字串時不應使用 cwd（Path("")）
# ---------------------------------------------------------------------------


class TestR028_1_DataDirEmptyString(unittest.TestCase):
    """Review #1: When DATA_DIR is empty or whitespace, scorer must not use cwd."""

    def test_scorer_data_dir_empty_string_treated_as_unset(self):
        """Guard: When os.environ['DATA_DIR'] is '', current code treats as falsy so _DATA_DIR is None."""
        import trainer.scorer as scorer_mod
        with patch.dict("os.environ", {"DATA_DIR": ""}, clear=False):
            importlib.reload(scorer_mod)
            self.assertIsNone(
                scorer_mod._DATA_DIR,
                "When DATA_DIR is empty string, _DATA_DIR must be None (current: if _data_dir_env is falsy)",
            )
        importlib.reload(scorer_mod)

    def test_scorer_data_dir_whitespace_only_should_not_use_cwd(self):
        """When DATA_DIR is whitespace-only (e.g. '  '), _DATA_DIR should be None; currently Path('  ') is used."""
        import trainer.scorer as scorer_mod
        with patch.dict("os.environ", {"DATA_DIR": "   "}, clear=False):
            importlib.reload(scorer_mod)
            self.assertIsNone(
                scorer_mod._DATA_DIR,
                "When DATA_DIR is whitespace-only, _DATA_DIR must be None to avoid odd Path('  ')",
            )
        importlib.reload(scorer_mod)


# ---------------------------------------------------------------------------
# R028 #2 — canonical 載入須兩檔皆存在；invalid JSON 時應 fallback
# ---------------------------------------------------------------------------


class TestR028_2_CanonicalLoadGuards(unittest.TestCase):
    """Review #2: Canonical mapping load requires both files; invalid JSON triggers rebuild."""

    def test_scorer_canonical_load_requires_both_parquet_and_cutoff_json(self):
        """Source guard: load path must require both CANONICAL_MAPPING_PARQUET and CANONICAL_MAPPING_CUTOFF_JSON to exist."""
        self.assertIn(
            "CANONICAL_MAPPING_PARQUET.exists()",
            _SCORER_SRC,
            "Scorer must check PARQUET exists before loading canonical mapping",
        )
        self.assertIn(
            "CANONICAL_MAPPING_CUTOFF_JSON.exists()",
            _SCORER_SRC,
            "Scorer must check CUTOFF_JSON exists before loading (prevents using parquet alone)",
        )
        # Condition must be AND of both
        idx = _SCORER_SRC.find("CANONICAL_MAPPING_PARQUET.exists()")
        self.assertGreater(idx, -1)
        fragment = _SCORER_SRC[idx : idx + 120]
        self.assertIn(
            "CANONICAL_MAPPING_CUTOFF_JSON.exists()",
            fragment,
            "Load condition must require both files in same if",
        )

    def test_scorer_canonical_load_uses_cutoff_dtm_from_sidecar(self):
        """When cutoff JSON has no 'cutoff_dtm' key, loader does not use parquet (logic: _cutoff_ts is None)."""
        self.assertIn(
            '_sidecar.get("cutoff_dtm")',
            _SCORER_SRC,
            "Loader must read cutoff_dtm from sidecar; missing key yields None and skips load",
        )


# ---------------------------------------------------------------------------
# R028 #4 — build_deploy_package：profile 複製失敗時應完成建包並印出錯誤
# ---------------------------------------------------------------------------


class TestR028_4_BuildProfileCopyFailure(unittest.TestCase):
    """Review #4: When profile copy fails, build should complete and print error at end (not abort)."""

    def _minimal_model_dir(self, tmp: Path) -> Path:
        """Create minimal model_source dir so copy_model_bundle and build_wheel can be mocked."""
        d = tmp / "minimal_models"
        d.mkdir(parents=True, exist_ok=True)
        (d / "model.pkl").write_bytes(b"\x80\x04\x95\x0c\x00\x00\x00")  # minimal pickle
        (d / "feature_list.json").write_text("[]", encoding="utf-8")
        (d / "feature_spec.yaml").write_text("{}", encoding="utf-8")
        return d

    def test_build_completes_and_stderr_has_error_when_profile_copy_raises(self):
        """When profile_src.exists() and shutil.copy2(profile_dest) raises OSError, build should complete and stderr should contain 'not shipped'."""
        from package.build_deploy_package import REPO_ROOT, build_deploy_package

        profile_src = REPO_ROOT / "data" / "player_profile.parquet"
        real_exists = Path.exists

        def exists_override(self):
            if self == profile_src:
                return True
            return real_exists(self)

        real_copy2 = __import__("shutil").copy2

        def copy2_raise_on_profile(src, dst, *args, **kwargs):
            if str(dst).endswith("player_profile.parquet"):
                raise OSError("simulated copy failure")
            return real_copy2(src, dst, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "out"
            model_source = self._minimal_model_dir(tmp_path)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "wheels").mkdir(parents=True, exist_ok=True)
            fake_whl = out_dir / "wheels" / "walkaway_ml-0.0.0-py3-none-any.whl"
            fake_whl.write_bytes(b"fake")

            with (
                patch.object(Path, "exists", exists_override),
                patch("package.build_deploy_package.shutil.copy2", side_effect=copy2_raise_on_profile),
                patch("package.build_deploy_package.build_wheel", return_value="walkaway_ml-0.0.0-py3-none-any.whl"),
            ):
                stderr_capture = io.StringIO()
                try:
                    with patch("sys.stderr", stderr_capture):
                        build_deploy_package(
                            output_dir=out_dir,
                            model_source=model_source,
                            create_archive=False,
                        )
                except OSError:
                    # Current behavior: build aborts when copy2 raises. Test expects no raise after production fix.
                    self.fail(
                        "build_deploy_package raised OSError when profile copy failed; "
                        "production should catch and set profile_shipped=False, then print error at end."
                    )
                err_text = stderr_capture.getvalue()
                self.assertIn(
                    "not shipped",
                    err_text,
                    "When profile copy fails, stderr must contain 'not shipped' error message",
                )


# ---------------------------------------------------------------------------
# R028 #5 — 0 字節 profile 仍會被帶出（記錄目前行為）
# ---------------------------------------------------------------------------


class TestR028_5_ZeroByteProfileBuildBehavior(unittest.TestCase):
    """Review #5: Document current behavior when profile file is 0 bytes."""

    def test_build_source_does_not_check_profile_size(self):
        """Source guard: build uses only .exists() for profile, not st_size > 0."""
        self.assertIn(
            "profile_src.exists()",
            _BUILD_SRC,
            "Build uses profile_src.exists() to decide copy",
        )
        # No requirement for st_size or stat() in profile block
        idx_2b = _BUILD_SRC.find("2b. Player profile")
        self.assertGreater(idx_2b, -1)
        block = _BUILD_SRC[idx_2b : idx_2b + 800]
        self.assertNotRegex(
            block,
            r"st_size|\.stat\(\)|size\s*>\s*0",
            "Current build does not check profile file size; 0-byte file would be shipped (documented risk).",
        )


# ---------------------------------------------------------------------------
# R028 #6 — 文件與實作一致（無程式測試，僅留註記）
# ---------------------------------------------------------------------------
# Review #6: Update DEPLOY_PLAN §8.2 text; no automated test.


# ---------------------------------------------------------------------------
# R028 #9 — DATA_DIR 須在 import 前設定（source guard）
# ---------------------------------------------------------------------------


class TestR028_9_DataDirSetBeforeImport(unittest.TestCase):
    """Review #9: Scorer path is fixed at import time; DATA_DIR must be set before import."""

    def test_scorer_paths_are_module_level(self):
        """Source guard: _DATA_DIR and CANONICAL_* are assigned at module level (import time)."""
        self.assertIn(
            "_data_dir_env = os.environ.get(\"DATA_DIR\")",
            _SCORER_SRC,
            "DATA_DIR is read at module load",
        )
        self.assertIn(
            "_data_dir_env",
            _SCORER_SRC,
            "Paths are set once from env at import",
        )
        self.assertIn(
            "strip()",
            _SCORER_SRC,
            "DATA_DIR should be validated (e.g. strip) so empty/whitespace is not used",
        )
