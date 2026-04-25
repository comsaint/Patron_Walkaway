"""tests/test_trainer.py
========================
Unit tests for trainer/trainer.py — sample_weight correctness and artifact bundle completeness.

No ClickHouse; uses synthetic DataFrames and AST/source inspection to avoid
importing trainer (which pulls in db_conn/clickhouse_connect).
PLAN Step 10: sample_weight correctness, artifact bundle completeness.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import pandas as pd


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_func_src(name: str) -> str:
    for node in _TRAINER_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_TRAINER_SRC, node) or ""
    return ""


def _get_assign_src(name: str) -> str:
    """Return source for a module-level assignment (e.g. _SESSION_SELECT_COLS)."""
    for node in _TRAINER_TREE.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.get_source_segment(_TRAINER_SRC, node) or ""
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
        self.assertIn("split_row_metadata_from_parquet_paths", src)
        self.assertIn("split_row_metadata_from_dataframes", src)
        self.assertIn("_split_row_meta", src)
        self.assertIn("_model_used_split_meta", src)
        self.assertIn("rated_only=True", src)
        self.assertIn("build_model_metadata_document(", src)
        self.assertIn("model_used_splits=_model_used_split_meta", src)
        self.assertIn("model_metadata=_model_meta_doc", src)
        self.assertIn("split_boundary_params=_split_mlflow_meta", src)

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

    def test_recent_chunks_effective_window_is_used_for_profile_flows(self):
        """--recent-chunks must drive profile freshness-check and profile table load window."""
        src = _get_func_src("run_pipeline")
        self.assertIn("effective_start", src)
        self.assertIn("effective_end", src)
        self.assertRegex(
            src,
            r"ensure_player_profile_ready\(\s*effective_start,\s*effective_end",
            "Profile freshness check must use effective window after chunk trim",
        )
        self.assertRegex(
            src,
            r"load_player_profile\(\s*effective_start,\s*effective_end",
            "Profile table load must use effective window after chunk trim",
        )

    def test_recent_chunks_effective_window_is_used_for_local_identity_sessions(self):
        """--recent-chunks must also constrain local sessions pull for identity mapping."""
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


if __name__ == "__main__":
    unittest.main()
