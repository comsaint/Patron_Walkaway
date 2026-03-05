"""tests/test_review_risks_round80.py
=====================================
Minimal reproducible guardrail tests for Round 11 review findings (R92–R97).

Tests-only — no production code changes.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DB_CONN_PATH = _REPO_ROOT / "trainer" / "db_conn.py"
_ETL_PATH = _REPO_ROOT / "trainer" / "etl_player_profile.py"
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_PROFILE_HASH_TEST_PATH = _REPO_ROOT / "tests" / "test_profile_schema_hash.py"

_DB_CONN_SRC = _DB_CONN_PATH.read_text(encoding="utf-8")
_ETL_SRC = _ETL_PATH.read_text(encoding="utf-8")
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_PROFILE_HASH_TEST_SRC = _PROFILE_HASH_TEST_PATH.read_text(encoding="utf-8")

_ETL_TREE = ast.parse(_ETL_SRC)
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_func_src(tree: ast.Module, src: str, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


class TestR92DbConnImportCompatibility(unittest.TestCase):
    """R92: db_conn should support both package and non-package entrypoints."""

    def test_db_conn_config_import_uses_try_except_fallback(self):
        self.assertRegex(
            _DB_CONN_SRC,
            r"try:\s*\n\s*import config\b",
            "db_conn.py should first try plain `import config` (non-package mode) (R92)",
        )
        self.assertRegex(
            _DB_CONN_SRC,
            r"except ModuleNotFoundError:\s*\n\s*import trainer\.config as config",
            "db_conn.py should fallback to `trainer.config` import (package mode) (R92)",
        )


class TestR93ComputeProfileSnapshotDateDefinition(unittest.TestCase):
    """R93: _compute_profile should not reference undefined snapshot_date."""

    def test_compute_profile_has_snapshot_date_defined(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_compute_profile")
        self.assertGreater(len(src), 0, "_compute_profile not found")

        # Accept either:
        # 1) snapshot_date is passed as function parameter, or
        # 2) snapshot_date is derived inside the function from snapshot_dtm.
        has_param = re.search(r"def _compute_profile\([^)]*snapshot_date", src)
        # Match "snapshot_date =" or "snapshot_date: date =" (type-annotated assign)
        has_local_assign = re.search(r"\bsnapshot_date\s*(?::\s*\w+\s*)?=", src)
        self.assertTrue(
            bool(has_param) or bool(has_local_assign),
            "_compute_profile references snapshot_date but does not define it (R93)",
        )


class TestR94SchemaHashCoversComputeLogic(unittest.TestCase):
    """R94: schema hash should include compute-logic drift signal."""

    def test_schema_hash_references_compute_profile_logic(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "compute_profile_schema_hash")
        self.assertGreater(len(src), 0, "compute_profile_schema_hash not found")
        self.assertRegex(
            src,
            r"_compute_profile|inspect\.getsource|compute_source_hash|logic_hash",
            "compute_profile_schema_hash should include compute-logic signal, not only column lists (R94)",
        )


class TestR95SidecarWriteAtomicOrder(unittest.TestCase):
    """R95: sidecar write should be atomic and ordered safely around parquet replace."""

    def test_sidecar_written_before_or_atomically_with_parquet_replace(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_persist_local_parquet")
        self.assertGreater(len(src), 0, "_persist_local_parquet not found")

        idx_replace_parquet = src.find("os.replace(tmp_path, LOCAL_PROFILE_PARQUET)")
        idx_sidecar_write = src.find("LOCAL_PROFILE_SCHEMA_HASH")

        self.assertGreaterEqual(idx_replace_parquet, 0, "parquet os.replace call not found")
        self.assertGreaterEqual(idx_sidecar_write, 0, "schema sidecar write not found")
        self.assertLess(
            idx_sidecar_write,
            idx_replace_parquet,
            "schema sidecar should be written (or temp-written+replaced) before parquet replace to avoid mismatch race (R95)",
        )


class TestR96ClickHouseSchemaGuard(unittest.TestCase):
    """R96: ClickHouse path should have explicit schema/version guard policy."""

    def test_ensure_profile_ready_mentions_or_checks_clickhouse_schema_version(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "ensure_player_profile_daily_ready")
        self.assertGreater(len(src), 0, "ensure_player_profile_daily_ready not found")
        self.assertRegex(
            src,
            r"profile_version|schema_version|ClickHouse mode.*schema|check.*clickhouse",
            "ensure_player_profile_daily_ready should include explicit ClickHouse schema/version guard (R96)",
        )


class TestR97SchemaHashTestFragility(unittest.TestCase):
    """R97: avoid overly broad global patches in tests."""

    def test_profile_schema_hash_tests_do_not_globally_patch_path_exists(self):
        self.assertNotIn(
            'patch("pathlib.Path.exists", return_value=True)',
            _PROFILE_HASH_TEST_SRC,
            "test_profile_schema_hash.py uses global Path.exists patch; prefer precise path-level mocks (R97)",
        )


if __name__ == "__main__":
    unittest.main()
