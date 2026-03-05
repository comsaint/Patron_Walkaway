import inspect
import json
import re
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch


class TestR300SchemaSidecarHorizonGuardrail(unittest.TestCase):
    """R300: writer-side schema hash must include horizon tag (_mlb=...)."""

    def test_write_local_parquet_sidecar_hash_formula_includes_horizon_tag(self):
        import trainer.etl_player_profile as etl_mod

        src = inspect.getsource(etl_mod._persist_local_parquet)
        self.assertIn(
            "max_lookback_days",
            src,
            "Writer should accept max_lookback_days so sidecar hash can encode horizon.",
        )
        self.assertIn(
            "_horizon_tag",
            src,
            "Writer hash formula should define a horizon tag.",
        )
        self.assertIn(
            "_mlb=",
            src,
            "Writer hash formula should include '_mlb=<days>' to match reader-side R200 logic.",
        )


class TestR301SampleRatedMetadataGuardrail(unittest.TestCase):
    """R301: training_metrics.json should always include sample_rated_n metadata."""

    def test_training_metrics_contains_sample_rated_n_key_even_when_none(self):
        import trainer.trainer as trainer_mod

        with TemporaryDirectory() as td:
            with patch.object(trainer_mod, "MODEL_DIR", trainer_mod.Path(td)):
                trainer_mod.save_artifact_bundle(
                    rated=None,
                    feature_cols=[],
                    combined_metrics={},
                    model_version="test-v1",
                    fast_mode=False,
                )
            payload = json.loads(
                (trainer_mod.Path(td) / "training_metrics.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "sample_rated_n",
                payload,
                "training_metrics.json should record sample_rated_n for auditability.",
            )
            self.assertIsNone(
                payload["sample_rated_n"],
                "When --sample-rated is not used, sample_rated_n should be null.",
            )


class TestR302SampleRatedValidationGuardrail(unittest.TestCase):
    """R302: --sample-rated must reject zero/negative values explicitly."""

    def test_run_pipeline_has_positive_integer_guard_for_sample_rated(self):
        import trainer.trainer as trainer_mod

        src = inspect.getsource(trainer_mod.run_pipeline)
        has_guard = bool(
            re.search(r"sample_rated_n\s*(<=\s*0|<\s*1)", src)
            and "--sample-rated" in src
            and "SystemExit" in src
        )
        self.assertTrue(
            has_guard,
            "run_pipeline should reject --sample-rated <= 0 with a clear SystemExit message.",
        )


class TestR303NoPreloadOrthogonalityGuardrail(unittest.TestCase):
    """R303: no-preload warning should consider --sample-rated path as effective."""

    def test_r118_warning_condition_accounts_for_sample_rated(self):
        import trainer.trainer as trainer_mod

        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "if no_preload and not fast_mode and sample_rated_n is None:",
            src,
            "R118 warning condition should avoid warning when --sample-rated is present.",
        )


class TestR304DeadConstantGuardrail(unittest.TestCase):
    """R304: legacy FAST_MODE_RATED_SAMPLE_N should be removed or marked deprecated."""

    def test_fast_mode_rated_sample_constant_removed_or_deprecated(self):
        import trainer.trainer as trainer_mod

        src = inspect.getsource(trainer_mod)
        has_legacy_constant = "FAST_MODE_RATED_SAMPLE_N" in src
        has_deprecation_marker = "DEPRECATED(DEC-017)" in src
        self.assertTrue(
            (not has_legacy_constant) or has_deprecation_marker,
            "FAST_MODE_RATED_SAMPLE_N is legacy dead code unless explicitly marked deprecated.",
        )


if __name__ == "__main__":
    unittest.main()
