"""tests/test_trainer.py
========================
Unit tests for trainer/trainer.py — sample_weight correctness and artifact bundle completeness.

No ClickHouse; uses synthetic DataFrames and AST/source inspection to avoid
importing trainer (which pulls in db_conn/clickhouse_connect).
PLAN Step 10: sample_weight correctness, artifact bundle completeness.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import unittest
from datetime import datetime

import pandas as pd


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TRAINER_TREE = ast.parse(_TRAINER_SRC)

# Issue #12 PR-12.2 / PR-12.3: data ingress + feature-pipeline coordination
# moved into trainer/training/data_sources.py and
# trainer/training/feature_pipeline.py respectively. AST helpers below
# transparently fall back to those modules when a symbol is re-exported, so
# existing review-risk source contracts still apply.
_DATA_SOURCES_PATH = _REPO_ROOT / "trainer" / "training" / "data_sources.py"
_DATA_SOURCES_SRC = (
    _DATA_SOURCES_PATH.read_text(encoding="utf-8")
    if _DATA_SOURCES_PATH.exists() else ""
)
_DATA_SOURCES_TREE = (
    ast.parse(_DATA_SOURCES_SRC) if _DATA_SOURCES_SRC else ast.parse("")
)
_FEATURE_PIPELINE_PATH = _REPO_ROOT / "trainer" / "training" / "feature_pipeline.py"
_FEATURE_PIPELINE_SRC = (
    _FEATURE_PIPELINE_PATH.read_text(encoding="utf-8")
    if _FEATURE_PIPELINE_PATH.exists() else ""
)
_FEATURE_PIPELINE_TREE = (
    ast.parse(_FEATURE_PIPELINE_SRC) if _FEATURE_PIPELINE_SRC else ast.parse("")
)
_ARTIFACT_BUNDLE_PATH = _REPO_ROOT / "trainer" / "core" / "training_artifact_bundle.py"
_ARTIFACT_BUNDLE_SRC = (
    _ARTIFACT_BUNDLE_PATH.read_text(encoding="utf-8")
    if _ARTIFACT_BUNDLE_PATH.exists()
    else ""
)
_ARTIFACT_BUNDLE_TREE = (
    ast.parse(_ARTIFACT_BUNDLE_SRC) if _ARTIFACT_BUNDLE_SRC else ast.parse("")
)
_PIPELINE_RUN_CORE_PATH = _REPO_ROOT / "trainer" / "training" / "pipeline_run_core.py"
_PIPELINE_RUN_CORE_SRC = (
    _PIPELINE_RUN_CORE_PATH.read_text(encoding="utf-8")
    if _PIPELINE_RUN_CORE_PATH.exists()
    else ""
)
_PIPELINE_RUN_CORE_TREE = (
    ast.parse(_PIPELINE_RUN_CORE_SRC) if _PIPELINE_RUN_CORE_SRC else ast.parse("")
)
_COMMON_RUNTIME_PATH = _REPO_ROOT / "trainer" / "training" / "common_runtime.py"
_COMMON_RUNTIME_SRC = (
    _COMMON_RUNTIME_PATH.read_text(encoding="utf-8")
    if _COMMON_RUNTIME_PATH.exists()
    else ""
)
_COMMON_RUNTIME_TREE = (
    ast.parse(_COMMON_RUNTIME_SRC) if _COMMON_RUNTIME_SRC else ast.parse("")
)
_MODEL_EVAL_RUNTIME_PATH = _REPO_ROOT / "trainer" / "training" / "model_eval_runtime.py"
_MODEL_EVAL_RUNTIME_SRC = (
    _MODEL_EVAL_RUNTIME_PATH.read_text(encoding="utf-8")
    if _MODEL_EVAL_RUNTIME_PATH.exists()
    else ""
)
_MODEL_EVAL_RUNTIME_TREE = (
    ast.parse(_MODEL_EVAL_RUNTIME_SRC) if _MODEL_EVAL_RUNTIME_SRC else ast.parse("")
)

# Issue #12 PR-12.1: optional functional import for chunk-cache round-trip
# checks. The module imports ClickHouse helpers at top level; if the
# environment cannot satisfy that, fall back to AST-only assertions.
try:
    from trainer.training import trainer as _trainer_module
    _TRAINER_IMPORTED = True
except Exception:  # noqa: BLE001
    _trainer_module = None  # type: ignore[assignment]
    _TRAINER_IMPORTED = False


def _get_func_src(name: str) -> str:
    if name == "run_pipeline":
        for tree, src in ((_PIPELINE_RUN_CORE_TREE, _PIPELINE_RUN_CORE_SRC),):
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name == "run_pipeline_core":
                    seg = ast.get_source_segment(src, node) or ""
                    if seg:
                        return seg
    for node in _TRAINER_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_TRAINER_SRC, node) or ""
    # Issue #12 PR-12.2 / PR-12.3 fallback: function may have been extracted
    # to data_sources.py / feature_pipeline.py (zero-behavior-change split).
    for tree, src in (
        (_DATA_SOURCES_TREE, _DATA_SOURCES_SRC),
        (_FEATURE_PIPELINE_TREE, _FEATURE_PIPELINE_SRC),
        (_ARTIFACT_BUNDLE_TREE, _ARTIFACT_BUNDLE_SRC),
        (_COMMON_RUNTIME_TREE, _COMMON_RUNTIME_SRC),
        (_MODEL_EVAL_RUNTIME_TREE, _MODEL_EVAL_RUNTIME_SRC),
        (_PIPELINE_RUN_CORE_TREE, _PIPELINE_RUN_CORE_SRC),
    ):
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(src, node) or ""
    return ""


def _get_assign_src(name: str) -> str:
    """Return source for a module-level assignment (e.g. _SESSION_SELECT_COLS).

    Searches trainer.py first then falls back to data_sources.py so contracts
    that locked specific constants still apply after PR-12.2's symbol move.
    """
    for tree, src in (
        (_TRAINER_TREE, _TRAINER_SRC),
        (_DATA_SOURCES_TREE, _DATA_SOURCES_SRC),
        (_ARTIFACT_BUNDLE_TREE, _ARTIFACT_BUNDLE_SRC),
        (_COMMON_RUNTIME_TREE, _COMMON_RUNTIME_SRC),
        (_MODEL_EVAL_RUNTIME_TREE, _MODEL_EVAL_RUNTIME_SRC),
        (_PIPELINE_RUN_CORE_TREE, _PIPELINE_RUN_CORE_SRC),
    ):
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        return ast.get_source_segment(src, node) or ""
    return ""


# ---------------------------------------------------------------------------
# sample_weight correctness (spec: 1/N_run per row)
# ---------------------------------------------------------------------------

def _sample_weight_spec(df: pd.DataFrame) -> pd.Series:
    """Replicate trainer.compute_sample_weights spec: weight = 1 / N_run per (canonical_id, run_id)."""
    if "run_id" not in df.columns or "canonical_id" not in df.columns:
        return pd.Series(1.0, index=df.index)
    run_key = df["canonical_id"].astype(str) + "_" + df["run_id"].astype(str)
    n_run = run_key.map(run_key.value_counts())
    return (1.0 / n_run).fillna(1.0)


class TestSampleWeightCorrectness(unittest.TestCase):
    """Test that the documented sample_weight formula (1/N_run) is correct."""

    def test_single_visit_all_rows_same_weight(self):
        """One (canonical_id, run_id) → each row gets weight 1/N."""
        df = pd.DataFrame({
            "canonical_id": ["P1", "P1", "P1"],
            "run_id": [7, 7, 7],
        })
        sw = _sample_weight_spec(df)
        self.assertEqual(len(sw), 3)
        self.assertAlmostEqual(sw.iloc[0], 1.0 / 3.0)
        self.assertAlmostEqual(sw.iloc[1], 1.0 / 3.0)
        self.assertAlmostEqual(sw.iloc[2], 1.0 / 3.0)

    def test_two_visits_weights_sum_to_one_per_visit(self):
        """Weights per run sum to 1.0 (each run contributes equally to loss)."""
        df = pd.DataFrame({
            "canonical_id": ["P1", "P1", "P2", "P2", "P2"],
            "run_id": [1, 1, 1, 2, 2],
        })
        sw = _sample_weight_spec(df)
        # (P1, run=1): 2 rows → 0.5 each
        self.assertAlmostEqual(sw.iloc[0], 0.5)
        self.assertAlmostEqual(sw.iloc[1], 0.5)
        # (P2, run=1): 1 row → 1.0
        self.assertAlmostEqual(sw.iloc[2], 1.0)
        # (P2, run=2): 2 rows → 0.5 each
        self.assertAlmostEqual(sw.iloc[3], 0.5)
        self.assertAlmostEqual(sw.iloc[4], 0.5)

    def test_trainer_compute_sample_weights_implements_spec(self):
        """trainer.compute_sample_weights source implements run_key and 1/n_run."""
        src = _get_func_src("compute_sample_weights")
        self.assertIn("run_id", src)
        self.assertIn("run_key", src)
        self.assertIn("value_counts", src)
        self.assertTrue(
            "1.0" in src and ("/ n_run" in src or "/n_run" in src),
            "compute_sample_weights should use 1/N_run",
        )


# ---------------------------------------------------------------------------
# get_model_version — format
# ---------------------------------------------------------------------------

class TestGetModelVersion(unittest.TestCase):
    def test_model_version_format_in_source(self):
        """get_model_version returns YYYYMMDD-HHMMSS-<suffix> per docstring."""
        src = _get_func_src("get_model_version")
        self.assertIn("strftime", src)
        self.assertIn("%Y%m%d", src)
        self.assertIn("%H%M%S", src)


# ---------------------------------------------------------------------------
# save_artifact_bundle — writes required files
# ---------------------------------------------------------------------------

class TestArtifactBundleCompleteness(unittest.TestCase):
    def test_save_artifact_bundle_writes_single_model_pkl(self):
        """save_artifact_bundle must write model.pkl (v10 single-model, DEC-021)."""
        src = _get_func_src("save_artifact_bundle")
        self.assertIn("model.pkl", src)

    def test_save_artifact_bundle_writes_model_version_and_feature_list(self):
        """save_artifact_bundle must write model_version and feature_list.json."""
        src = _get_func_src("save_artifact_bundle")
        self.assertIn("model_version", src)
        self.assertIn("feature_list.json", src)

    def test_save_artifact_bundle_does_not_write_walkaway_pkl(self):
        """save_artifact_bundle must not emit legacy walkaway_model.pkl (DEC-040)."""
        src = _get_func_src("save_artifact_bundle")
        self.assertNotIn("walkaway_model.pkl", src)

    def test_save_artifact_bundle_supports_model_metadata_json(self):
        """save_artifact_bundle may write model_metadata.json when caller passes model_metadata."""
        src = _get_func_src("save_artifact_bundle")
        self.assertIn("model_metadata.json", src)
        self.assertIn("model_metadata", src)


# ---------------------------------------------------------------------------
# model_metadata.json — split summaries + run params (schema v1)
# ---------------------------------------------------------------------------


class TestModelMetadataPipelineWiring(unittest.TestCase):
    """Source contracts: helpers exist and run_pipeline wires them into the bundle."""

    def test_trainer_defines_split_metadata_helpers(self):
        self.assertIn("def split_row_metadata_from_dataframes", _TRAINER_SRC)
        self.assertIn("def split_row_metadata_from_parquet_paths", _TRAINER_SRC)
        self.assertIn("def build_model_metadata_document", _TRAINER_SRC)
        self.assertIn("def split_row_metadata_to_mlflow_string_params", _TRAINER_SRC)

    def test_run_pipeline_computes_split_meta_and_passes_to_save_and_mlflow(self):
        src = _get_func_src("run_pipeline")
        l2_src = (_REPO_ROOT / "trainer" / "training" / "pipeline_l2_bundle.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("split_row_metadata_from_parquet_paths", src)
        self.assertIn("split_row_metadata_from_dataframes", src)
        self.assertIn("_split_row_meta", src)
        self.assertIn("_model_used_split_meta", src)
        self.assertIn("rated_only=True", src)
        self.assertIn("model_used_splits=_model_used_split_meta", l2_src)
        self.assertIn("model_metadata=_model_meta_doc", l2_src)

    def test_metadata_builder_exposes_model_used_splits_and_optuna_effective_state(self):
        src = _get_func_src("build_model_metadata_document")
        self.assertIn('"model_used_splits"', src)
        self.assertIn('"optuna_hpo_effective_enabled"', src)
        self.assertIn('"optuna_hpo_objective_mode"', src)


class TestLibsvmOptunaProvenanceWiring(unittest.TestCase):
    """Source contracts for LibSVM path effective-HPO provenance."""

    def test_helper_exists_for_libsvm_optuna_skip_manifest(self):
        self.assertIn("def _write_skipped_optuna_manifest_for_libsvm", _TRAINER_SRC)

    def test_train_single_rated_model_records_effective_hpo_skip_on_libsvm(self):
        src = _get_func_src("train_single_rated_model")
        self.assertIn("_write_skipped_optuna_manifest_for_libsvm", src)
        self.assertIn("optuna_hpo_effective_enabled", _TRAINER_SRC)


class TestA3ValWindowWiring(unittest.TestCase):
    """Source contracts for A3 bakeoff validation span recomputation."""

    def test_a3_recomputes_val_window_after_loading_compare_valid(self):
        src = _get_func_src("train_single_rated_model")
        self.assertIn("_compare_valid_for_span", src)
        self.assertIn("_bake_val_wh, _bake_val_mah", src)
        self.assertIn("val_dec026_window_hours=_bake_val_wh", src)
        self.assertIn("val_dec026_min_alerts_per_hour=_bake_val_mah", src)


# ---------------------------------------------------------------------------
# Review risks: required DQ filter + reason_code_map.json presence
# ---------------------------------------------------------------------------

class TestReviewRiskGuards(unittest.TestCase):
    def test_load_clickhouse_data_session_query_has_fnd04_turnover_guard(self):
        """PLAN Step 1 / SSOT §5: sessions must satisfy turnover>0 OR num_games_with_wager>0."""
        # 1) Column availability: selection must include the fields we filter on.
        sess_cols_src = _get_assign_src("_SESSION_SELECT_COLS")
        self.assertIn("num_games_with_wager", sess_cols_src)
        self.assertIn("turnover", sess_cols_src)

        # 2) DQ filter: query must explicitly filter sessions with no activity.
        src = _get_func_src("load_clickhouse_data")
        self.assertRegex(src, r"COALESCE\(\s*turnover\s*,\s*0\s*\)\s*>\s*0")
        self.assertRegex(src, r"COALESCE\(\s*num_games_with_wager\s*,\s*0\s*\)\s*>\s*0")

    def test_save_artifact_bundle_writes_reason_code_map_json(self):
        """PLAN Artifacts: reason_code_map.json (feature -> reason_code mapping) must be written."""
        src = _get_func_src("save_artifact_bundle")
        self.assertIn("reason_code_map.json", src)

    def test_apply_dq_filters_sessions_by_is_manual_fnd02(self):
        """FND-02: apply_dq must actively filter is_manual=1 sessions (not just ensure column exists)."""
        src = _get_func_src("apply_dq")
        # Must have an actual boolean comparison, not just column initialisation.
        self.assertRegex(
            src,
            r'sessions\["is_manual"\]\s*==\s*0',
            "apply_dq must filter sessions where is_manual == 0 (FND-02)",
        )

    def test_apply_dq_filters_sessions_by_fnd04_turnover(self):
        """FND-04: apply_dq must filter sessions with no real activity (turnover/num_games)."""
        src = _get_func_src("apply_dq")
        self.assertIn("_turnover", src)
        self.assertIn("_games", src)
        self.assertRegex(
            src,
            r"\(_turnover\s*>\s*0\)\s*\|\s*\(_games\s*>\s*0\)",
            "apply_dq must keep sessions where turnover>0 OR num_games_with_wager>0 (FND-04)",
        )

    def test_effective_window_is_used_for_profile_flows(self):
        """Profile freshness-check 與 profile 載入須使用 effective_start / effective_end。"""
        src = _get_func_src("run_pipeline")
        self.assertIn("effective_start", src)
        self.assertIn("effective_end", src)
        self.assertRegex(
            src,
            r"ensure_player_profile_ready\(\s*effective_start,\s*effective_end",
            "Profile freshness check must use effective window derived from Step 1 chunks",
        )
        self.assertRegex(
            src,
            r"load_player_profile\(\s*effective_start,\s*effective_end",
            "Profile table load must use effective window derived from Step 1 chunks",
        )

    def test_effective_window_is_used_for_local_identity_sessions(self):
        """Local sessions pull for identity mapping 須使用 effective 視窗。"""
        src = _get_func_src("run_pipeline")
        self.assertRegex(
            src,
            r"load_local_parquet\(\s*effective_start,\s*effective_end\s*\+\s*timedelta",
            "Local canonical mapping bootstrap must use effective window",
        )
        self.assertIn("apply_dq(", src)
        self.assertIn("sessions_all", src)
        self.assertIn("effective_start", src)
        self.assertIn("effective_end + timedelta(days=1)", src)


# ---------------------------------------------------------------------------
# Issue #12 PR-12.1 — refactor guardrails (lock contracts, not implementation)
#
# These tests freeze the public-ish contracts that subsequent refactor PRs
# (12.2 → 12.5) must preserve unchanged. They intentionally test:
#   1. Input source semantics (ClickHouse / local Parquet) — signatures and DQ
#      column lists.
#   2. Chunk cache key + sidecar semantics — components, fingerprint pipe
#      format, JSON sidecar shape, miss-reason buckets, filename patterns.
#   3. Artifact key set — files written by ``save_artifact_bundle``.
#   4. Core metrics keys — return-dict keys for test/train metrics helpers.
#
# Each PR in the refactor series MUST keep this class green. If a contract
# legitimately needs to change (e.g. a constant moves to a shared module),
# update the assertion AND call it out in the PR description.
# ---------------------------------------------------------------------------


class TestRefactorGuardrailsInputSources(unittest.TestCase):
    """PR-12.1 §1 — input source semantics (CH / local parquet) frozen."""

    def test_data_ingress_lives_in_data_sources_module(self):
        """PR-12.2 boundary: ingress + parquet helpers must originate in
        ``trainer.training.data_sources`` so trainer.py is only orchestration.
        ``trainer.training.trainer`` continues to re-export the names so
        external call sites (parallel_lda_mvp, gbm_bakeoff, scripts) keep
        working unchanged."""
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        from trainer.training import data_sources as _ds
        # Ingress functions originate in data_sources.
        for name in (
            "load_clickhouse_data",
            "load_local_parquet",
            "_local_parquet_source_data_hash",
            "_parquet_stable_rowgroups_schema_digest",
            "_detect_local_data_end",
            "_parquet_date_range",
            "_parse_obj_to_date",
        ):
            self.assertEqual(
                getattr(_trainer_module, name).__module__,
                _ds.__name__,
                f"{name} should live in trainer.training.data_sources",
            )
        # Column lists / SQL templates originate in data_sources too.
        for name in (
            "_BET_SELECT_COLS",
            "_SESSION_SELECT_COLS",
            "_REQUIRED_BET_PARQUET_COLS",
            "_OPTIONAL_BET_LDA_RUN_TRIP_COLS",
            "_CANONICAL_MAP_SESSION_COLS",
            "LOCAL_PARQUET_DIR",
            "trainer_local_parquet_bridge_manifest_path",
        ):
            self.assertIs(
                getattr(_trainer_module, name),
                getattr(_ds, name),
                f"{name} must be re-exported from data_sources",
            )

    def test_load_clickhouse_data_signature_unchanged(self):
        """Public callers pass exactly (window_start, extended_end)."""
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        sig = inspect.signature(_trainer_module.load_clickhouse_data)
        self.assertEqual(list(sig.parameters), ["window_start", "extended_end"])

    def test_load_local_parquet_signature_unchanged(self):
        """Local parquet path keeps sessions_only kwarg (canonical-map use)."""
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        sig = inspect.signature(_trainer_module.load_local_parquet)
        self.assertEqual(
            list(sig.parameters),
            ["window_start", "extended_end", "sessions_only"],
        )
        self.assertEqual(
            sig.parameters["sessions_only"].default, False,
        )

    def test_canonical_map_session_cols_dq_set(self):
        """FND-02 / FND-04 columns must remain in the canonical-map session pull."""
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        cols = set(_trainer_module._CANONICAL_MAP_SESSION_COLS)
        for needed in (
            "session_id", "player_id", "casino_player_id",
            "lud_dtm", "session_start_dtm", "session_end_dtm",
            "is_manual", "is_deleted", "is_canceled",
            "num_games_with_wager", "turnover",
        ):
            self.assertIn(needed, cols, f"FND-02/04 column missing: {needed}")

    def test_required_bet_parquet_cols_set(self):
        """Column-pushdown list for the t_bet Parquet read must keep the
        union needed by DQ + Track Human + Track LLM YAML."""
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        cols = set(_trainer_module._REQUIRED_BET_PARQUET_COLS)
        for needed in (
            "bet_id", "session_id", "player_id", "game_id", "table_id",
            "payout_complete_dtm", "gaming_day",
            "wager", "status", "casino_win",
            "payout_odds", "base_ha", "is_back_bet", "position_idx",
        ):
            self.assertIn(needed, cols, f"missing required bet col: {needed}")

    def test_optional_lda_run_trip_cols_present(self):
        """Run/trip LDA bridge columns are loaded when present."""
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        cols = set(_trainer_module._OPTIONAL_BET_LDA_RUN_TRIP_COLS)
        for needed in (
            "lda_l1_run_bet_count",
            "lda_trip_run_count",
            "lda_run_ord_in_trip",
            "lda_trip_is_closed",
            "lda_l1_run_duration_min",
        ):
            self.assertIn(needed, cols, f"missing LDA L1 col: {needed}")

    def test_apply_dq_signature_unchanged(self):
        """Refactor must keep apply_dq's positional/keyword shape."""
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        sig = inspect.signature(_trainer_module.apply_dq)
        self.assertEqual(
            list(sig.parameters),
            ["bets", "sessions", "window_start", "extended_end",
             "bets_history_start"],
        )
        self.assertIsNone(sig.parameters["bets_history_start"].default)

    def test_feature_pipeline_owns_dq_and_track_human(self):
        """PR-12.3 boundary: ``apply_dq`` and ``add_run_state_machine_features``
        originate in ``trainer.training.feature_pipeline``; trainer.py only
        re-exports them."""
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        from trainer.training import feature_pipeline as _fp
        for name in ("apply_dq", "add_run_state_machine_features"):
            self.assertEqual(
                getattr(_trainer_module, name).__module__,
                _fp.__name__,
                f"{name} should live in trainer.training.feature_pipeline",
            )

    def test_metrics_eval_owns_pure_helpers(self):
        """PR-12.4 boundary: pure metric helpers (precision rescaling,
        production neg/pos ratio guard) originate in
        ``trainer.training.metrics_eval``; trainer.py only re-exports them.
        """
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")
        from trainer.training import metrics_eval as _me
        for name in (
            "_precision_prod_adjusted",
            "_warn_if_invalid_production_neg_pos_ratio",
        ):
            self.assertEqual(
                getattr(_trainer_module, name).__module__,
                _me.__name__,
                f"{name} should live in trainer.training.metrics_eval",
            )
        # Public (non-underscore) aliases also exposed for new callers.
        self.assertTrue(hasattr(_me, "precision_prod_adjusted"))
        self.assertTrue(hasattr(_me, "warn_if_invalid_production_neg_pos_ratio"))


class TestRefactorGuardrailsChunkCache(unittest.TestCase):
    """PR-12.1 §2 — chunk cache key + sidecar semantics frozen."""

    _CHUNK = {
        "window_start": datetime(2024, 1, 1),
        "window_end": datetime(2024, 1, 2),
    }

    def setUp(self) -> None:
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")

    def test_components_key_set(self):
        comps = _trainer_module._chunk_cache_components(
            self._CHUNK,
            data_hash="abc",
            profile_hash="ph",
            feature_spec_hash="fs",
            neg_sample_frac=0.5,
        )
        self.assertEqual(
            sorted(comps),
            sorted([
                "window_start", "window_end", "data_hash",
                "cfg_hash", "profile_hash",
                "feature_spec_hash", "neg_sample_frac",
            ]),
        )
        self.assertIsInstance(comps["neg_sample_frac"], float)
        # Final-stage spec hash is annotated with chunk-final schema version.
        self.assertTrue(
            comps["feature_spec_hash"].endswith(
                ":" + _trainer_module._CHUNK_FINAL_SCHEMA_VERSION,
            ),
            comps["feature_spec_hash"],
        )

    def test_components_requires_data_hash_or_bets(self):
        with self.assertRaises(ValueError):
            _trainer_module._chunk_cache_components(self._CHUNK)

    def test_fingerprint_pipe_format(self):
        comps = _trainer_module._chunk_cache_components(
            self._CHUNK, data_hash="abc",
            profile_hash="ph", feature_spec_hash="fs",
            neg_sample_frac=0.5,
        )
        fp = _trainer_module._fingerprint_from_chunk_cache_components(comps)
        parts = fp.split("|")
        self.assertEqual(len(parts), 7)
        # window_start | window_end | data_hash | cfg_hash | profile_hash
        # | spec<feature_spec_hash> | ns<neg_sample_frac:.4f>
        self.assertEqual(parts[0], "2024-01-01T00:00:00")
        self.assertEqual(parts[1], "2024-01-02T00:00:00")
        self.assertEqual(parts[2], "abc")
        self.assertEqual(parts[4], "ph")
        self.assertTrue(parts[5].startswith("spec"))
        self.assertTrue(parts[6].startswith("ns"))
        # Round-trip: parse back to a components-shaped dict.
        parsed = _trainer_module._parse_chunk_cache_fingerprint_pipe(fp)
        self.assertIsNotNone(parsed)
        assert parsed is not None  # for type checkers
        self.assertEqual(parsed["data_hash"], "abc")
        self.assertEqual(parsed["profile_hash"], "ph")
        self.assertEqual(
            parsed["feature_spec_hash"],
            "fs:" + _trainer_module._CHUNK_FINAL_SCHEMA_VERSION,
        )
        self.assertAlmostEqual(parsed["neg_sample_frac"], 0.5)

    def test_prefeatures_key_uses_placeholder_and_full_neg_frac(self):
        """R6 prefeatures cache key must scrub LLM spec + neg_sample_frac."""
        comps = _trainer_module._chunk_cache_components(
            self._CHUNK, data_hash="abc",
            profile_hash="ph", feature_spec_hash="fs",
            neg_sample_frac=0.5,
        )
        pre = _trainer_module._prefeatures_cache_components(comps)
        self.assertEqual(
            pre["feature_spec_hash"],
            _trainer_module._CHUNK_PREFEATURES_SPEC_PLACEHOLDER,
        )
        self.assertEqual(pre["neg_sample_frac"], 1.0)

    def test_sidecar_payload_shape_and_roundtrip(self):
        comps = _trainer_module._chunk_cache_components(
            self._CHUNK, data_hash="abc",
            profile_hash="ph", feature_spec_hash="fs",
            neg_sample_frac=0.5,
        )
        fp = _trainer_module._fingerprint_from_chunk_cache_components(comps)
        side = _trainer_module._write_chunk_cache_sidecar(
            fp, comps, source_mode="local_parquet",
        )
        obj = json.loads(side)
        self.assertEqual(obj["v"], _trainer_module._CHUNK_CACHE_SIDECAR_VERSION)
        self.assertEqual(obj["fingerprint"], fp)
        self.assertEqual(
            set(obj["pipeline"]),
            {
                "window_start", "window_end", "data_hash", "cfg_hash",
                "profile_hash", "feature_spec_hash", "neg_sample_frac",
            },
        )
        self.assertEqual(obj["source"]["mode"], "local_parquet")
        round_fp, round_pipe = _trainer_module._read_chunk_cache_sidecar(side)
        self.assertEqual(round_fp, fp)
        self.assertIsNotNone(round_pipe)
        assert round_pipe is not None
        self.assertEqual(round_pipe["data_hash"], "abc")

    def test_chunk_paths_filename_pattern(self):
        chunk = {
            "window_start": datetime(2024, 1, 1),
            "window_end": datetime(2024, 1, 5),
        }
        self.assertEqual(
            _trainer_module._chunk_parquet_path(chunk).name,
            "chunk_20240101_20240105.parquet",
        )
        self.assertEqual(
            _trainer_module._chunk_prefeatures_parquet_path(chunk).name,
            "chunk_20240101_20240105.prefeatures.parquet",
        )
        self.assertEqual(
            _trainer_module._chunk_prefeatures_sidecar_path(chunk).name,
            "chunk_20240101_20240105.prefeatures.cache_key",
        )

    def test_miss_reasons_diff_buckets(self):
        prev = _trainer_module._chunk_cache_components(
            self._CHUNK, data_hash="A",
            profile_hash="ph", feature_spec_hash="fs",
            neg_sample_frac=0.5,
        )
        prev_fp = _trainer_module._fingerprint_from_chunk_cache_components(prev)
        # data hash flipped → "data" bucket reported
        cur = _trainer_module._chunk_cache_components(
            self._CHUNK, data_hash="B",
            profile_hash="ph", feature_spec_hash="fs",
            neg_sample_frac=0.5,
        )
        self.assertIn(
            "data",
            _trainer_module._chunk_cache_miss_reasons(prev_fp, prev, cur),
        )
        # neg_sample_frac flipped → "neg_sample" bucket
        cur2 = _trainer_module._chunk_cache_components(
            self._CHUNK, data_hash="A",
            profile_hash="ph", feature_spec_hash="fs",
            neg_sample_frac=0.25,
        )
        self.assertIn(
            "neg_sample",
            _trainer_module._chunk_cache_miss_reasons(prev_fp, prev, cur2),
        )


class TestRefactorGuardrailsArtifactBundle(unittest.TestCase):
    """PR-12.1 §3 — artifact bundle key set frozen."""

    def test_save_artifact_bundle_writes_canonical_files(self):
        """Bundle must continue to emit the v10 artifact set (DEC-021)."""
        src = _get_func_src("save_artifact_bundle")
        # Files written directly inside save_artifact_bundle.
        for fname in (
            "model.pkl",
            "feature_list.json",
            "reason_code_map.json",
            "model_version",
            "training_metrics.json",
            "feature_spec.yaml",
            "model_metadata.json",
        ):
            self.assertIn(fname, src, f"missing artifact: {fname}")
        # v2 metrics / feature_importance / comparison_metrics are written
        # via the helper — assert it is invoked from save_artifact_bundle.
        self.assertIn("write_training_metrics_v2_sidecars", src)
        # training_provenance.json is written when alignment payload provided.
        self.assertIn("training_provenance.json", src)

    def test_pipeline_l2_bundle_logs_training_metrics_v3_to_mlflow(self) -> None:
        """L2 path must upload contract metrics JSON under the model_bundle artifact prefix."""
        pl2 = _REPO_ROOT / "trainer" / "training" / "pipeline_l2_bundle.py"
        src = pl2.read_text(encoding="utf-8")
        self.assertIn("training_metrics.v3.json", src)
        self.assertIn("log_artifact_safe", src)
        self.assertIn("log_metrics_safe", src)
        self.assertIn("MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH", src)

    def test_save_artifact_bundle_does_not_revive_legacy_dual_pkl(self):
        """DEC-040: walkaway_model.pkl must not return."""
        src = _get_func_src("save_artifact_bundle")
        self.assertNotIn("walkaway_model.pkl", src)


class TestRefactorGuardrailsMetricsKeys(unittest.TestCase):
    """PR-12.1 §4 — core metrics return-dict keys frozen.

    Drives the cheap zero/empty fallback paths so the contract is exercised
    end-to-end (rather than scraping f-string templates from source).
    """

    _TARGET_RECALLS = (0.001, 0.01, 0.1, 0.5)

    def setUp(self) -> None:
        if not _TRAINER_IMPORTED:
            self.skipTest("trainer module not importable in this env")

    def test_compute_test_metrics_returns_canonical_keys(self):
        # Empty test set hits the early-return zeroed path which still
        # publishes every documented key (DEC-026).
        out = _trainer_module._compute_test_metrics(
            model=None,
            threshold=0.5,
            X_test=pd.DataFrame(),
            y_test=pd.Series(dtype=float),
            log_results=False,
        )
        for k in (
            "test_ap", "test_precision", "test_recall", "test_f1",
            "test_samples", "test_positives", "test_random_ap",
            "test_threshold_uncalibrated",
            "test_precision_prod_adjusted", "test_neg_pos_ratio",
            "production_neg_pos_ratio_assumed",
        ):
            self.assertIn(k, out, f"missing test metric key: {k}")
        for r in self._TARGET_RECALLS:
            for tmpl in (
                "test_precision_at_recall_{r}",
                "threshold_at_recall_{r}",
                "n_alerts_at_recall_{r}",
                "alerts_per_minute_at_recall_{r}",
                "test_precision_at_recall_{r}_prod_adjusted",
            ):
                self.assertIn(
                    tmpl.format(r=r), out,
                    f"missing recall-keyed metric: {tmpl.format(r=r)}",
                )

    def test_train_metrics_dict_returns_canonical_keys(self):
        import numpy as _np
        out = _trainer_module._train_metrics_dict_from_y_scores(
            y_train=_np.array([], dtype=float),
            train_scores=_np.array([], dtype=float),
            threshold=0.5,
            log_results=False,
        )
        for k in (
            "train_ap", "train_precision", "train_recall", "train_f1",
            "train_samples", "train_positives", "train_random_ap",
        ):
            self.assertIn(k, out, f"missing train metric key: {k}")

    def test_compute_train_metrics_delegates_to_y_scores_helper(self):
        """Refactor must keep the y_scores helper as the single keys source."""
        src = _get_func_src("_compute_train_metrics")
        self.assertIn("_train_metrics_dict_from_y_scores", src)


if __name__ == "__main__":
    unittest.main()
