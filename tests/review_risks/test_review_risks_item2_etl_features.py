"""步驟 4（項目 2）2.2 etl/features 子包搬移 — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：步驟 4（項目 2）2.2 etl / features 子包搬移 » §1、§2、§3、§4。
§1：python -m trainer.etl_player_profile --help 應有 usage/help 輸出且 exit 0（契約：薄層 __main__ 轉發）。
§2：候選 spec 僅 **`trainer/feature_spec/features_candidates.yaml`**（SSOT）；**不得**再存在 `trainer/features/feature_spec/features_candidates.yaml`（已移除冗餘拷貝）。
§3：import trainer.etl_player_profile 後 sys.modules["trainer.etl_player_profile"] 為實作模組。
§4：trainer.features 顯式 re-export 之底線名稱存在（_validate_feature_spec、_streak_lookback_numba、_run_boundary_lookback_numba 等）。

執行方式（repo 根目錄）：
  python -m pytest tests/review_risks/test_review_risks_item2_etl_features.py -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


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
# §2 — feature_spec 單一路徑（SSOT）
# ---------------------------------------------------------------------------


class TestFeatureSpecSingleSourceContract(unittest.TestCase):
    """Review §2 (2026-04-18): Canonical candidates YAML only under trainer/feature_spec/."""

    def test_canonical_features_candidates_yaml_exists(self):
        """trainer/feature_spec/features_candidates.yaml must exist (SSOT)."""
        p1 = REPO_ROOT / "trainer" / "feature_spec" / "features_candidates.yaml"
        self.assertTrue(
            p1.is_file(),
            "Canonical spec must exist at trainer/feature_spec/features_candidates.yaml (STATUS Code Review 2.2 etl/features §2).",
        )

    def test_redundant_features_subpackage_yaml_absent(self):
        """Duplicate trainer/features/feature_spec/features_candidates.yaml must not exist."""
        p2 = REPO_ROOT / "trainer" / "features" / "feature_spec" / "features_candidates.yaml"
        self.assertFalse(
            p2.exists(),
            "Redundant trainer/features/feature_spec/features_candidates.yaml must not exist; edit trainer/feature_spec/features_candidates.yaml only (STATUS Code Review 2.2 etl/features §2).",
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
