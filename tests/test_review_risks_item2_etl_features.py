"""步驟 4（項目 2）2.2 etl/features 子包搬移 — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：步驟 4（項目 2）2.2 etl / features 子包搬移 » §1、§2、§3、§4。
§1：python -m trainer.etl_player_profile --help 應有 usage/help 輸出且 exit 0（契約：薄層 __main__ 轉發）。
§2：trainer/feature_spec/ 與 trainer/features/feature_spec/ 下 features_candidates.yaml 內容一致（雙份時須同步）。
§3：import trainer.etl_player_profile 後 sys.modules["trainer.etl_player_profile"] 為實作模組。
§4：trainer.features 顯式 re-export 之底線名稱存在（_validate_feature_spec、_streak_lookback_numba、_run_boundary_lookback_numba 等）。

執行方式（repo 根目錄）：
  python -m pytest tests/test_review_risks_item2_etl_features.py -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# §1 — ETL CLI 入口：--help 應有輸出且 exit 0
# ---------------------------------------------------------------------------


class TestEtlPlayerProfileCliHelpContract(unittest.TestCase):
    """Review §1: python -m trainer.etl_player_profile --help should print usage/help and exit 0 (no silent exit)."""

    def test_etl_player_profile_help_prints_and_exits_zero(self):
        """Subprocess run python -m trainer.etl_player_profile --help; expect exit 0 and stdout/stderr contain usage or help."""
        proc = subprocess.run(
            [sys.executable, "-m", "trainer.etl_player_profile", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        out_lower = out.lower()
        self.assertEqual(
            proc.returncode,
            0,
            "python -m trainer.etl_player_profile --help should exit 0 (STATUS Code Review 2.2 etl/features §1).",
        )
        self.assertGreater(
            len(out.strip()),
            0,
            "Should not exit silently; stdout or stderr must contain usage/help (STATUS Code Review 2.2 etl/features §1).",
        )
        self.assertTrue(
            "usage" in out_lower or "help" in out_lower or "argument" in out_lower or "option" in out_lower,
            "Output should mention usage, help, argument, or option: %r" % (out[:200],),
        )


# ---------------------------------------------------------------------------
# §2 — feature_spec 雙份 YAML 內容一致
# ---------------------------------------------------------------------------


class TestFeatureSpecYamlSyncContract(unittest.TestCase):
    """Review §2: When both paths exist, trainer/feature_spec/ and trainer/features/feature_spec/ YAML must match."""

    def test_feature_spec_yaml_byte_identical_when_both_exist(self):
        """If both trainer/feature_spec/features_candidates.yaml and trainer/features/feature_spec/ exist, content must be identical."""
        p1 = REPO_ROOT / "trainer" / "feature_spec" / "features_candidates.yaml"
        p2 = REPO_ROOT / "trainer" / "features" / "feature_spec" / "features_candidates.yaml"
        if not p1.exists():
            self.skipTest("trainer/feature_spec/features_candidates.yaml not found")
        if not p2.exists():
            self.skipTest("trainer/features/feature_spec/features_candidates.yaml not found")
        self.assertEqual(
            p1.read_bytes(),
            p2.read_bytes(),
            "trainer/feature_spec/ and trainer/features/feature_spec/ features_candidates.yaml must be identical (STATUS Code Review 2.2 etl/features §2).",
        )


# ---------------------------------------------------------------------------
# §3 — sys.modules 覆寫契約
# ---------------------------------------------------------------------------


class TestEtlPlayerProfileModuleIdentityContract(unittest.TestCase):
    """Review §3: After import trainer.etl_player_profile, sys.modules points to implementation module."""

    def test_sys_modules_etl_player_profile_is_implementation(self):
        """sys.modules['trainer.etl_player_profile'] must be the same object as trainer.etl.etl_player_profile."""
        import trainer.etl.etl_player_profile as impl  # noqa: F401
        import trainer.etl_player_profile  # noqa: F401

        self.assertIs(
            sys.modules["trainer.etl_player_profile"],
            impl,
            "trainer.etl_player_profile must resolve to trainer.etl.etl_player_profile (STATUS Code Review 2.2 etl/features §3).",
        )


# ---------------------------------------------------------------------------
# §4 — features 底線名稱 re-export 契約
# ---------------------------------------------------------------------------


class TestFeaturesUnderscoreReexportContract(unittest.TestCase):
    """Review §4: trainer.features must expose the explicitly re-exported underscore names used by tests/etl."""

    def test_features_exposes_validate_feature_spec(self):
        """trainer.features must expose _validate_feature_spec (used by test_feature_spec_yaml etc)."""
        import trainer.features as f

        self.assertTrue(hasattr(f, "_validate_feature_spec"), "trainer.features must re-export _validate_feature_spec (STATUS Code Review 2.2 etl/features §4).")

    def test_features_exposes_streak_and_run_boundary_numba(self):
        """trainer.features must expose _streak_lookback_numba and _run_boundary_lookback_numba (patch targets in tests)."""
        import trainer.features as f

        self.assertTrue(hasattr(f, "_streak_lookback_numba"), "trainer.features must re-export _streak_lookback_numba (STATUS Code Review 2.2 etl/features §4).")
        self.assertTrue(hasattr(f, "_run_boundary_lookback_numba"), "trainer.features must re-export _run_boundary_lookback_numba (STATUS Code Review 2.2 etl/features §4).")

    def test_features_exposes_lookback_and_profile_min_days(self):
        """trainer.features must expose _LOOKBACK_MAX_HOURS and _PROFILE_FEATURE_MIN_DAYS (used by tests/etl)."""
        import trainer.features as f

        self.assertTrue(hasattr(f, "_LOOKBACK_MAX_HOURS"), "trainer.features must re-export _LOOKBACK_MAX_HOURS (STATUS Code Review 2.2 etl/features §4).")
        self.assertTrue(hasattr(f, "_PROFILE_FEATURE_MIN_DAYS"), "trainer.features must re-export _PROFILE_FEATURE_MIN_DAYS (STATUS Code Review 2.2 etl/features §4).")
