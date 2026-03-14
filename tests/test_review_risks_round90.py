"""tests/test_review_risks_round90.py
=====================================
Minimal reproducible guardrail tests for Review Round 14 findings (R98–R104).

Tests-only: no production code changes.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ETL_PATH = _REPO_ROOT / "trainer" / "etl" / "etl_player_profile.py"  # 項目 2.2: 實作在 etl 子包
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_PROFILE_HASH_TEST_PATH = _REPO_ROOT / "tests" / "test_profile_schema_hash.py"

_ETL_SRC = _ETL_PATH.read_text(encoding="utf-8")
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_PROFILE_HASH_TEST_SRC = _PROFILE_HASH_TEST_PATH.read_text(encoding="utf-8")

_ETL_TREE = ast.parse(_ETL_SRC)
_TRAINER_TREE = ast.parse(_TRAINER_SRC)
_PROFILE_HASH_TEST_TREE = ast.parse(_PROFILE_HASH_TEST_SRC)


def _get_func_src(tree: ast.Module, src: str, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _get_method_src(
    tree: ast.Module,
    src: str,
    class_name: str,
    method_name: str,
) -> str:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == method_name:
                    return ast.get_source_segment(src, sub) or ""
    return ""


class TestR98ComputeSourceHashNormalization(unittest.TestCase):
    """R98: normalize source newlines before hashing compute logic."""

    def test_compute_profile_schema_hash_normalizes_line_endings(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "compute_profile_schema_hash")
        self.assertGreater(len(src), 0, "compute_profile_schema_hash not found")
        self.assertRegex(
            src,
            r"replace\(\s*[\"']\\r\\n[\"']\s*,\s*[\"']\\n[\"']\s*\)|ast\.dump\(",
            "compute_profile_schema_hash should normalize CRLF/LF or use AST-based hash (R98)",
        )


class TestR99LocalSessionProjection(unittest.TestCase):
    """R99: local parquet read should project columns to reduce memory."""

    def test_load_sessions_local_uses_column_projection(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_load_sessions_local")
        self.assertGreater(len(src), 0, "_load_sessions_local not found")
        self.assertRegex(
            src,
            r"read_parquet\([^)]*columns\s*=",
            "_load_sessions_local should call pd.read_parquet(..., columns=...) (R99)",
        )


class TestR100DateParseHelperDuplication(unittest.TestCase):
    """R100: avoid duplicated date parsing helpers across modules."""

    def test_etl_should_not_define_private_duplicate_date_parser(self):
        self.assertNotRegex(
            _ETL_SRC,
            r"def\s+_coerce_to_date\(",
            "etl_player_profile.py defines _coerce_to_date; prefer shared helper to avoid drift (R100)",
        )


class TestR101HermeticSchemaHashTest(unittest.TestCase):
    """R101: schema-hash tests should not read real workspace data implicitly."""

    def test_matching_hash_test_passes_explicit_session_parquet(self):
        src = _get_method_src(
            _PROFILE_HASH_TEST_TREE,
            _PROFILE_HASH_TEST_SRC,
            "TestEnsureProfileReadySchemaMismatch",
            "test_matching_hash_does_not_delete_parquet",
        )
        self.assertGreater(len(src), 0, "target test method not found")
        self.assertRegex(
            src,
            r"compute_profile_schema_hash\([^)]*session_parquet\s*=",
            "test_matching_hash_does_not_delete_parquet should pass session_parquet explicitly (R101)",
        )


class TestR102SnapshotAvailabilityCutoff(unittest.TestCase):
    """R102: snapshot cutoff should include availability delay after day end."""

    def test_build_profile_snapshot_dtm_includes_availability_delay(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "build_player_profile")
        self.assertGreater(len(src), 0, "build_player_profile not found")
        self.assertRegex(
            src,
            r"SESSION_AVAIL_DELAY_MIN|timedelta\(\s*minutes\s*=",
            "build_player_profile should incorporate availability delay in snapshot cutoff (R102)",
        )


class TestR103MissingDQColumnGuard(unittest.TestCase):
    """R103: local loader should warn/fail when key DQ columns are missing."""

    def test_load_sessions_local_has_missing_dq_column_guard(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_load_sessions_local")
        self.assertGreater(len(src), 0, "_load_sessions_local not found")
        self.assertRegex(
            src,
            r"if\s+[\"']is_manual[\"']\s+not\s+in\s+df\.columns|Missing DQ column",
            "_load_sessions_local should explicitly guard missing DQ columns (R103)",
        )


class TestR104LocalWriteMemoryPattern(unittest.TestCase):
    """R104: avoid full read-then-concat rewrite pattern for large parquet."""

    def test_persist_local_parquet_avoids_full_existing_read(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_persist_local_parquet")
        self.assertGreater(len(src), 0, "_persist_local_parquet not found")
        self.assertNotRegex(
            src,
            r"existing\s*=\s*pd\.read_parquet\(",
            "_persist_local_parquet still does full existing parquet read (R104)",
        )


if __name__ == "__main__":
    unittest.main()
