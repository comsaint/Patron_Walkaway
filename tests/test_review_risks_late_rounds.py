"""Consolidated tests for late-round review risks."""

from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

def _import_module(name: str):
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module(name)

trainer_mod = _import_module("trainer.trainer")
features_mod = _import_module("trainer.features")
backtester_mod = _import_module("trainer.backtester")
labels_mod = _import_module("trainer.labels")
identity_mod = _import_module("trainer.identity")
config_mod = _import_module("trainer.config")
time_fold_mod = _import_module("trainer.time_fold")

def _read_text(rel_path: str) -> str:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    return (repo_root / rel_path).read_text(encoding="utf-8")

class TestR1500SingleModelOnly(unittest.TestCase):
    """R1500: trainer should no longer use dual-model training."""

    def test_run_pipeline_should_not_call_train_dual_model(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertNotIn(
            "train_dual_model(",
            src,
            "v10 single-model pipeline should not call train_dual_model().",
        )


class TestR1501NoNonratedArtifact(unittest.TestCase):
    """R1501: artifact bundle should not emit nonrated artifacts in v10."""

    def test_save_artifact_bundle_should_not_write_nonrated_model(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertNotIn(
            "nonrated_model.pkl",
            src,
            "save_artifact_bundle should not write nonrated_model.pkl in v10.",
        )


class TestR1502BacktesterSingleModel(unittest.TestCase):
    """R1502: backtester metrics should not require nonrated threshold."""

    def test_compute_micro_metrics_should_not_take_nonrated_threshold(self):
        sig = inspect.signature(backtester_mod.compute_micro_metrics)
        self.assertNotIn(
            "nonrated_threshold",
            sig.parameters,
            "compute_micro_metrics should be rated-only in v10.",
        )


class TestR1503ValidationClassGuard(unittest.TestCase):
    """R1503: _train_one_model should guard val set with at least one negative."""

    def test_train_one_model_has_negative_class_guard_in_val(self):
        src = inspect.getsource(trainer_mod._train_one_model)
        self.assertIn(
            "(y_val == 0)",
            src,
            "_train_one_model should require at least one negative in validation.",
        )


class TestR1504AtomicPklWrites(unittest.TestCase):
    """R1504: model artifacts should be written atomically."""

    def test_save_artifact_bundle_uses_atomic_rename_for_pkl(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertTrue(
            ("os.replace(" in src) or (".replace(" in src and "tmp" in src),
            "save_artifact_bundle should use temp file + atomic replace for pkl writes.",
        )


class TestR1505ScreenFeaturesAllNaN(unittest.TestCase):
    """R1505: screen_features should gracefully handle all-zero/all-NaN features."""

    def test_screen_features_all_zero_variance_returns_empty(self):
        df = pd.DataFrame(
            {
                "f1": [1.0] * 8,
                "f2": [np.nan] * 8,
            }
        )
        y = pd.Series([0, 1, 0, 1, 0, 1, 0, 1], dtype="int8")
        out = features_mod.screen_features(
            feature_matrix=df,
            labels=y,
            feature_names=["f1", "f2"],
            top_k=None,
            use_lgbm=False,
        )
        self.assertEqual(
            out,
            [],
            "All zero-variance/all-NaN candidates should return empty feature list.",
        )


class TestR1506NoFeaturetoolsTrackA(unittest.TestCase):
    """R1506: DEC-022 says Featuretools Track A should be removed."""

    def test_features_module_should_not_reference_featuretools(self):
        src = inspect.getsource(features_mod)
        self.assertNotIn(
            "featuretools",
            src.lower(),
            "features.py should not reference featuretools after DEC-022 migration.",
        )


class TestR1507ReasonCodePrefix(unittest.TestCase):
    """R1507: fallback reason code prefix should not be TRACK_A_ in v10."""

    def test_reason_code_map_should_not_use_track_a_prefix(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertNotIn(
            "TRACK_A_",
            src,
            "reason_code_map fallback should not use TRACK_A_ prefix after DEC-022.",
        )


class TestR1508VisitTerminology(unittest.TestCase):
    """R1508: backtester should avoid stale visit terminology where possible."""

    def test_backtester_should_not_use_visit_variable_names(self):
        src = inspect.getsource(backtester_mod.compute_macro_by_gaming_day_metrics)
        self.assertNotIn(
            "visit_",
            src,
            "compute_macro_by_gaming_day_metrics should avoid visit_* variable names.",
        )


class TestR1509TrainSingleClassGuard(unittest.TestCase):
    """R1509: _train_one_model should guard training labels against single class."""

    def test_train_one_model_checks_train_labels_have_two_classes(self):
        src = inspect.getsource(trainer_mod._train_one_model)
        self.assertTrue(
            ("y_train.nunique()" in src) or ("(y_train == 0)" in src),
            "_train_one_model should explicitly guard single-class training labels.",
        )


class TestR1510RunKeyCollision(unittest.TestCase):
    """R1510: run_key should avoid plain string concatenation collisions."""

    def test_compute_sample_weights_should_not_use_plain_string_concat_key(self):
        src = inspect.getsource(trainer_mod.compute_sample_weights)
        self.assertNotIn(
            ' + "_" + ',
            src,
            "compute_sample_weights should avoid string-concat run keys due to collision risk.",
        )


class TestR1611TrainEndTzNaive(unittest.TestCase):
    """R1611: train_end should be normalized to tz-naive in run_pipeline."""

    def test_run_pipeline_should_strip_tz_for_train_end(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "train_end = train_end.replace(tzinfo=None)",
            src,
            "run_pipeline should explicitly strip tzinfo from train_end per DEC-018.",
        )


class TestR1612ValidationWarningDiagnostics(unittest.TestCase):
    """R1612: validation fallback warning should include negative-class count."""

    def test_train_one_model_warning_mentions_negatives(self):
        src = inspect.getsource(trainer_mod._train_one_model).lower()
        self.assertIn(
            "negatives",
            src,
            "_train_one_model warning should mention negatives for observability.",
        )


class TestR1613ZeroFeatureEarlyExit(unittest.TestCase):
    """R1613: pipeline should fail fast with explicit message on zero features."""

    def test_run_pipeline_has_explicit_zero_feature_guard(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "screen_features + Track B fallback both returned empty feature list",
            src,
            "run_pipeline should raise a clear SystemExit before LightGBM zero-column crash.",
        )


class TestR1614LabelsTzBoundaryGuard(unittest.TestCase):
    """R1614: compute_labels should handle tz-aware boundaries safely."""

    def test_compute_labels_accepts_tz_aware_boundaries(self):
        bets_df = pd.DataFrame(
            {
                "canonical_id": ["c1", "c1"],
                "bet_id": [1, 2],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-03-01 10:00:00", "2026-03-01 10:10:00"]
                ),
            }
        )
        window_end = pd.Timestamp("2026-03-01 10:30:00", tz="Asia/Hong_Kong")
        extended_end = pd.Timestamp("2026-03-01 11:30:00", tz="Asia/Hong_Kong")
        out = labels_mod.compute_labels(bets_df, window_end=window_end, extended_end=extended_end)
        self.assertIn("label", out.columns)
        self.assertIn("censored", out.columns)


class TestR1615ThresholdMinAlertConfig(unittest.TestCase):
    """R1615: minimum alert count should be configurable, not magic number."""

    def test_min_alert_count_is_config_backed(self):
        self.assertTrue(
            hasattr(config_mod, "MIN_THRESHOLD_ALERT_COUNT"),
            "config.py should define MIN_THRESHOLD_ALERT_COUNT.",
        )
        src = inspect.getsource(trainer_mod._train_one_model)
        self.assertNotIn(
            "alert_counts >= 5",
            src,
            "_train_one_model should use config.MIN_THRESHOLD_ALERT_COUNT, not hard-coded 5.",
        )


class TestR1616ResolveCanonicalNoneFallback(unittest.TestCase):
    """R1616: unresolved identity fallback should return None, not empty string."""

    def test_resolve_canonical_id_returns_none_when_unresolved(self):
        mapping_df = pd.DataFrame(columns=["player_id", "canonical_id"])
        out = identity_mod.resolve_canonical_id(
            player_id=None,
            session_id=None,
            mapping_df=mapping_df,
            session_lookup=None,
            obs_time=None,
        )
        self.assertIsNone(
            out,
            "resolve_canonical_id should return None for unresolvable identity.",
        )


class TestR1700DefaultObsTimeTimezone(unittest.TestCase):
    """R1700: resolve_canonical_id default obs_time should be HK-local naive, not UTC-naive."""

    def test_resolve_canonical_id_should_not_use_utc_now_default(self):
        src = inspect.getsource(identity_mod.resolve_canonical_id)
        self.assertNotIn(
            "datetime.now(timezone.utc).replace(tzinfo=None)",
            src,
            "resolve_canonical_id default now should be HK-local naive, not UTC-naive.",
        )


class TestR1701CasinoPlayerIdParity(unittest.TestCase):
    """R1701: pandas cleaner and SQL cleaner should use the same invalid-token set."""

    def test_cleaner_should_not_treat_nan_none_as_invalid_if_sql_does_not(self):
        cleaned = identity_mod._clean_casino_player_id(pd.Series(["nan", "none"]))
        # Current SQL clean fragment only lists '', 'null'. If pandas returns NA here,
        # parity is broken (one side treats these as invalid, the other may not).
        self.assertEqual(
            cleaned.tolist(),
            ["nan", "none"],
            "pandas cleaner marks 'nan'/'none' invalid while SQL cleaner may not.",
        )


class TestR1702ProfileJoinTimezoneNormalization(unittest.TestCase):
    """R1702: profile PIT join should convert to HK before stripping timezone."""

    def test_join_player_profile_converts_utc_to_hk_before_strip(self):
        bets = pd.DataFrame(
            {
                "canonical_id": ["C1"],
                "payout_complete_dtm": [pd.Timestamp("2026-03-05 10:00:00")],  # HK naive
            }
        )
        profile = pd.DataFrame(
            {
                "canonical_id": ["C1"],
                # 02:00 UTC == 10:00 HK; should match after tz_convert(HK)->naive
                "snapshot_dtm": [pd.Timestamp("2026-03-05 02:00:00", tz="UTC")],
                "sessions_7d": [7.0],
            }
        )
        out = features_mod.join_player_profile(
            bets_df=bets,
            profile_df=profile,
            feature_cols=["sessions_7d"],
        )
        self.assertEqual(
            float(out.loc[0, "sessions_7d"]),
            7.0,
            "UTC profile timestamp should match HK-naive bet time after proper conversion.",
        )


class TestR1706WagerFilterSpecAlignment(unittest.TestCase):
    """R1706: apply_dq wager filter should be explicitly aligned with PLAN/SSOT."""

    def test_apply_dq_should_not_hard_filter_wager_positive_without_spec(self):
        src = inspect.getsource(trainer_mod.apply_dq)
        self.assertNotIn(
            '.fillna(0) > 0',
            src,
            "apply_dq currently hard-filters wager>0; verify this is intended and documented.",
        )


class TestR1707DummyDetectionGhostSessions(unittest.TestCase):
    """R1707: dummy-player detection should be self-contained for FND-04 ghost-session exclusion."""

    def test_identify_dummy_player_ids_excludes_ghost_sessions_without_prefilter(self):
        deduped = pd.DataFrame(
            {
                "session_id": [1],
                "player_id": [42],
                "is_manual": [0],
                "is_deleted": [0],
                "is_canceled": [0],
                "num_games_with_wager": [0],
                "turnover": [0.0],  # ghost session
            }
        )
        out = identity_mod._identify_dummy_player_ids(deduped)
        self.assertNotIn(
            42,
            out,
            "Ghost session should not count toward dummy-account detection.",
        )


class TestR1709ScreenStage2Params(unittest.TestCase):
    """R1709: lgb.train stage-2 params should not include sklearn-only n_estimators."""

    def test_screen_features_stage2_should_not_set_n_estimators_param(self):
        src = inspect.getsource(features_mod.screen_features)
        self.assertNotIn(
            '"n_estimators": 100',
            src,
            "screen_features Stage-2 should rely on num_boost_round for lgb.train.",
        )


class TestR1710GapStartSelfLabelSemantics(unittest.TestCase):
    """R1710: lock current semantics — gap_start bet itself is labeled as 1."""

    def test_gap_start_bet_itself_labeled_one(self):
        bets_df = pd.DataFrame(
            {
                "canonical_id": ["c1"],
                "bet_id": [1],
                "payout_complete_dtm": pd.to_datetime(["2026-03-01 10:00:00"]),
            }
        )
        # terminal determinable: 10:00 + WALKAWAY_GAP_MIN(30m) <= 11:00
        out = labels_mod.compute_labels(
            bets_df,
            window_end=pd.Timestamp("2026-03-01 10:30:00"),
            extended_end=pd.Timestamp("2026-03-01 11:00:00"),
        )
        self.assertEqual(int(out.loc[0, "label"]), 1)
        self.assertFalse(bool(out.loc[0, "censored"]))


class TestR1711SplitFractionConfigParity(unittest.TestCase):
    """R1711: guard against drift between chunk split defaults and config row split fractions."""

    def test_time_fold_split_defaults_match_config(self):
        time_fold_mod = _import_module("trainer.time_fold")
        sig = inspect.signature(time_fold_mod.get_train_valid_test_split)
        self.assertEqual(float(sig.parameters["train_frac"].default), float(config_mod.TRAIN_SPLIT_FRAC))
        self.assertEqual(float(sig.parameters["valid_frac"].default), float(config_mod.VALID_SPLIT_FRAC))


if __name__ == "__main__":
    unittest.main()


class TestR1900G2RecoveryMissing(unittest.TestCase):
    """R1900: apply_dq should recover invalid bet player_id via session player_id (G2)."""

    def test_apply_dq_should_recover_player_id_from_session_before_drop(self):
        bets = pd.DataFrame(
            {
                "bet_id": [1],
                "session_id": [1001],
                "player_id": [-1],  # invalid in bet row, should be recovered from session
                "table_id": [10],
                "payout_complete_dtm": [pd.Timestamp("2026-03-01 10:00:00")],
                "gaming_day": [pd.Timestamp("2026-03-01").date()],
                "status": ["LOSE"],
            }
        )
        sessions = pd.DataFrame(
            {
                "session_id": [1001],
                "player_id": [42],
                "is_manual": [0],
                "is_deleted": [0],
                "is_canceled": [0],
                "num_games_with_wager": [1],
                "turnover": [100.0],
                "lud_dtm": [pd.Timestamp("2026-03-01 10:30:00")],
                "__etl_insert_Dtm": [pd.Timestamp("2026-03-01 10:31:00")],
                "session_start_dtm": [pd.Timestamp("2026-03-01 09:00:00")],
                "session_end_dtm": [pd.Timestamp("2026-03-01 10:20:00")],
            }
        )

        bets_clean, _ = trainer_mod.apply_dq(
            bets=bets,
            sessions=sessions,
            window_start=datetime(2026, 3, 1, 0, 0, 0),
            extended_end=datetime(2026, 3, 2, 0, 0, 0),
        )
        self.assertEqual(len(bets_clean), 1, "G2 should keep this row after recovery.")
        self.assertEqual(int(bets_clean.iloc[0]["player_id"]), 42)


class TestR1901ResolveFallbackSemantics(unittest.TestCase):
    """R1901: resolve_canonical_id step-3 fallback returns str(player_id) for unrated (aligned with PLAN)."""

    def test_resolve_returns_str_player_id_for_unrated_player_not_in_mapping(self):
        mapping_df = pd.DataFrame({"player_id": [1], "canonical_id": ["CARD001"]})
        out = identity_mod.resolve_canonical_id(
            player_id=999,  # not in mapping → unrated; step-3 fallback
            session_id=None,
            mapping_df=mapping_df,
            session_lookup=None,
            obs_time=datetime(2026, 3, 1, 12, 0, 0),
        )
        self.assertEqual(out, "999", "Step 3 fallback: unrated but valid player_id → str(player_id).")


class TestR1902BacktesterArtifactPath(unittest.TestCase):
    """R1902: backtester should prefer model.pkl under single-model v10."""

    def test_backtester_loader_should_reference_model_pkl(self):
        src = _read_text("trainer/backtester.py")
        self.assertIn(
            '"model.pkl"',
            src,
            "Single-model loader should read model.pkl in v10.",
        )


class TestR1903ScorerApiArtifactPath(unittest.TestCase):
    """R1903: scorer/api loader should prefer model.pkl under single-model v10."""

    def test_scorer_loader_should_reference_model_pkl(self):
        src = _read_text("trainer/scorer.py")
        self.assertIn('"model.pkl"', src, "scorer loader should read model.pkl in v10.")

    def test_api_loader_should_reference_model_pkl(self):
        src = _read_text("trainer/api_server.py")
        self.assertIn('"model.pkl"', src, "api loader should read model.pkl in v10.")


class TestR1904TrainerDocstringStale(unittest.TestCase):
    """R1904: trainer module doc should no longer describe dual-model artifacts."""

    def test_trainer_doc_should_not_mention_nonrated_model_pkl(self):
        src = _read_text("trainer/trainer.py")
        head = "\n".join(src.splitlines()[:60]).lower()
        self.assertNotIn("nonrated_model.pkl", head)


class TestR1907ScreenFeaturesNaNSemantics(unittest.TestCase):
    """R1907: screen_features should not erase NaN semantics via blanket fillna(0)."""

    def test_screen_features_should_not_unconditionally_fillna_zero(self):
        src = inspect.getsource(_import_module("trainer.features").screen_features)
        self.assertNotIn("X_filled = X.fillna(0)", src)


class TestR1909TimeFoldImportLocation(unittest.TestCase):
    """R1909: time_fold split defaults should not re-import config inside function."""

    def test_time_fold_split_should_not_import_config_inside_function(self):
        src = inspect.getsource(_import_module("trainer.time_fold").get_train_valid_test_split)
        self.assertNotIn("from config import TRAIN_SPLIT_FRAC", src)
        self.assertNotIn("from trainer.config import TRAIN_SPLIT_FRAC", src)


class TestR1601TrainEndTimezoneStrip(unittest.TestCase):
    """R1601: tz-aware train_end should be converted to HK before stripping tz."""

    def test_run_pipeline_should_convert_before_tz_strip(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        # Minimum source-level contract: conversion step exists in train_end handling.
        self.assertIn(
            "tz_convert",
            src,
            "run_pipeline should convert to HK timezone before removing tzinfo.",
        )


class TestR1602ApplyDQWagerZeroGuard(unittest.TestCase):
    """R1602: apply_dq should not pass zero-wager bets downstream."""

    def test_apply_dq_excludes_zero_wager_rows(self):
        bets = pd.DataFrame(
            {
                "bet_id": [1, 2],
                "session_id": [10, 10],
                "player_id": [100, 100],
                "table_id": [1, 1],
                "wager": [0.0, 50.0],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-03-01 10:00:00", "2026-03-01 10:05:00"]
                ),
                "gaming_day": [pd.Timestamp("2026-03-01").date()] * 2,
                "status": ["LOSE", "WIN"],
            }
        )
        sessions = pd.DataFrame(
            {
                "session_id": [10],
                "player_id": [100],
                "is_manual": [0],
                "is_deleted": [0],
                "is_canceled": [0],
                "num_games_with_wager": [2],
                "turnover": [50.0],
                "lud_dtm": [pd.Timestamp("2026-03-01 10:30:00")],
                "__etl_insert_Dtm": [pd.Timestamp("2026-03-01 10:31:00")],
                "session_start_dtm": [pd.Timestamp("2026-03-01 09:00:00")],
                "session_end_dtm": [pd.Timestamp("2026-03-01 10:20:00")],
            }
        )

        bets_clean, _ = trainer_mod.apply_dq(
            bets=bets,
            sessions=sessions,
            window_start=datetime(2026, 3, 1, 0, 0, 0),
            extended_end=datetime(2026, 3, 2, 0, 0, 0),
        )
        self.assertTrue(
            (bets_clean["wager"].fillna(0) > 0).all(),
            "apply_dq should exclude wager<=0 rows as defense-in-depth.",
        )


class TestR1605BiasFallbackArtifactRisk(unittest.TestCase):
    """R1605: bias-only fallback should not silently create production artifacts."""

    def test_run_pipeline_should_not_use_bias_constant_fallback(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertNotIn(
            'bias_col = "bias"',
            src,
            "prefer hard fail or explicit artifact safety flag over silent bias fallback.",
        )


class TestR1607BacktesterDocstringStale(unittest.TestCase):
    """R1607: backtester module docstring should reflect single-threshold mode."""

    def test_backtester_doc_should_not_claim_dual_2d_threshold_search(self):
        src = _read_text("trainer/backtester.py").lower()
        head = "\n".join(src.splitlines()[:25])
        self.assertNotIn("dual-model backtester", head)
        self.assertNotIn("2d threshold search", head)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
