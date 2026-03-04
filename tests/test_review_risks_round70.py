"""tests/test_review_risks_round70.py
=====================================
Minimal reproducible guardrail tests for Round 7 review findings (R83–R91).

Tests-only — no production code changes.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCORER_PATH = _REPO_ROOT / "trainer" / "scorer.py"
_ETL_PATH = _REPO_ROOT / "trainer" / "etl_player_profile.py"

_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")
_ETL_SRC = _ETL_PATH.read_text(encoding="utf-8")

_SCORER_TREE = ast.parse(_SCORER_SRC)
_ETL_TREE = ast.parse(_ETL_SRC)


def _get_func_src(tree: ast.Module, src: str, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


class TestR83ScorerModelSpecificFeatureSubset(unittest.TestCase):
    """R83: nonrated prediction should use nonrated model feature subset."""

    def test_nonrated_predict_does_not_use_global_feature_list_directly(self):
        src = _get_func_src(_SCORER_TREE, _SCORER_SRC, "_score_df")
        self.assertGreater(len(src), 0, "_score_df not found")
        self.assertNotIn(
            "df.loc[nonrated_mask, feature_list]",
            src,
            "nonrated predict should use model-specific features, not global feature_list (R83)",
        )
        self.assertRegex(
            src,
            r'nonrated.*get\(\s*"features"|_model_nr.*get\(\s*"features"',
            "nonrated path should read feature subset from nonrated artifact (R83)",
        )


class TestR84ScorerProfileLoadVolume(unittest.TestCase):
    """R84: scorer should load latest snapshot per player, not full 365d history."""

    def test_load_profile_query_has_latest_per_player_logic(self):
        src = _get_func_src(_SCORER_TREE, _SCORER_SRC, "_load_profile_for_scoring")
        self.assertGreater(len(src), 0, "_load_profile_for_scoring not found")
        self.assertRegex(
            src,
            r"LIMIT\s+1\s+BY\s+canonical_id|argMax\(|groupby\(\s*[\"']canonical_id[\"']\s*\).*last\(",
            "profile loader should implement latest-per-canonical_id reduction (R84)",
        )


class TestR85ScorerProfileCache(unittest.TestCase):
    """R85: scorer profile loading should have cache / TTL guard."""

    def test_profile_loader_has_cache_or_ttl(self):
        has_cache = bool(
            re.search(r"_profile_cache|lru_cache|cache_ttl|ttl|loaded_at|expires_at", _SCORER_SRC, re.I)
        )
        self.assertTrue(
            has_cache,
            "scorer profile loading should include cache/TTL to avoid per-tick reload (R85)",
        )


class TestR86EtlWindowBoundaryByTimestamp(unittest.TestCase):
    """R86: ETL window inclusion should be timestamp-based, not date-only."""

    def test_compute_profile_uses_session_ts_for_window_flags(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_compute_profile")
        self.assertGreater(len(src), 0, "_compute_profile not found")
        self.assertRegex(
            src,
            r'sessions\[f"_in_\{days\}d"\]\s*=\s*sessions\["_session_ts"\]',
            "window flags should be based on _session_ts to avoid boundary leakage (R86)",
        )


class TestR87EtlQueryExplicitSelect(unittest.TestCase):
    """R87: avoid SELECT * EXCEPT in ETL SQL."""

    def test_load_sessions_query_does_not_use_select_star(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_load_sessions")
        self.assertGreater(len(src), 0, "_load_sessions not found")
        self.assertNotRegex(
            src,
            r"SELECT\s+\*\s+EXCEPT",
            "ETL query should use explicit column projection, not SELECT * EXCEPT (R87)",
        )


class TestR88EtlAtomicParquetWrite(unittest.TestCase):
    """R88: local parquet write should be atomic/safe under concurrency."""

    def test_write_local_parquet_uses_atomic_replace_or_lock(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_persist_local_parquet")
        self.assertGreater(len(src), 0, "_persist_local_parquet not found")
        self.assertRegex(
            src,
            r"os\.replace\(|tempfile|flock|portalocker|msvcrt\.locking",
            "local parquet writer should use atomic replace or file lock (R88)",
        )


class TestR89EtlFnd12Vectorized(unittest.TestCase):
    """R89: FND-12 should avoid groupby apply(lambda) for performance."""

    def test_exclude_fnd12_does_not_use_apply_lambda(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_exclude_fnd12_dummies")
        self.assertGreater(len(src), 0, "_exclude_fnd12_dummies not found")
        self.assertNotRegex(
            src,
            r"\.apply\(\s*lambda",
            "_exclude_fnd12_dummies should use vectorized groupby sum, not apply(lambda) (R89)",
        )


class TestR90EtlBackfillReuse(unittest.TestCase):
    """R90: backfill should reuse expensive mapping/client setup across days."""

    def test_backfill_has_reuse_hook_for_mapping_or_client(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "backfill")
        self.assertGreater(len(src), 0, "backfill not found")
        self.assertRegex(
            src,
            r"canonical_map|cached_mapping|client\s*=|shared_client|reuse",
            "backfill should include reuse hook for canonical mapping/client (R90)",
        )


class TestR91EtlUnusedImportGuard(unittest.TestCase):
    """R91: etl script should not keep unused imports."""

    def test_hashlib_import_is_used_or_removed(self):
        tree = _ETL_TREE
        imported_hashlib = False
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "hashlib":
                        imported_hashlib = True

        if not imported_hashlib:
            return  # already fixed

        # hashlib is imported: require at least one symbol usage
        used = any(isinstance(n, ast.Name) and n.id == "hashlib" for n in ast.walk(tree))
        self.assertTrue(
            used,
            "hashlib is imported but unused in etl_player_profile.py (R91)",
        )


if __name__ == "__main__":
    unittest.main()
