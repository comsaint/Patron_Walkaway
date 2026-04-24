"""Step 10 — Feature Spec YAML 靜態驗證測試 (PLAN Step 10 / DEC-024).

Covers:
- YAML 可成功載入，feature_id 唯一
- Track LLM window_frame 無 FOLLOWING（look-ahead 防漏）
- Track LLM expression 僅使用白名單函數（無 SELECT/FROM/JOIN/UNION/WITH）
- derived 的 depends_on 無循環依賴
- load_feature_spec 對非法 YAML 拋出 ValueError

All tests use the shipped features_candidates.yaml as the canonical
reference so that any future edits to the spec are automatically validated.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import tempfile
import unittest

import yaml


def _import_features():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return importlib.import_module("trainer.features")


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC_YAML = REPO_ROOT / "trainer" / "feature_spec" / "features_candidates.yaml"

features_mod = _import_features()


class TestLoadFeatureSpecTemplateLoads(unittest.TestCase):
    """Template YAML can be loaded without errors."""

    def test_template_loads_without_error(self):
        spec = features_mod.load_feature_spec(SPEC_YAML)
        self.assertIsInstance(spec, dict)

    def test_spec_has_required_top_level_keys(self):
        spec = features_mod.load_feature_spec(SPEC_YAML)
        for key in ("version", "spec_id", "track_llm", "track_human", "track_profile"):
            self.assertIn(key, spec, f"Missing top-level key: {key}")

    def test_template_yaml_file_exists(self):
        self.assertTrue(SPEC_YAML.exists(), f"Spec YAML missing at {SPEC_YAML}")


class TestFeatureIdUniqueness(unittest.TestCase):
    """feature_id values are unique across all tracks."""

    def test_all_feature_ids_unique_in_template(self):
        spec = features_mod.load_feature_spec(SPEC_YAML)
        all_ids = []
        for track_key in ("track_llm", "track_human", "track_profile"):
            track = spec.get(track_key, {})
            for cand in track.get("candidates", []):
                all_ids.append(cand.get("feature_id", ""))
        self.assertEqual(
            len(all_ids),
            len(set(all_ids)),
            f"Duplicate feature_ids found: {[x for x in all_ids if all_ids.count(x) > 1]}",
        )

    def test_duplicate_feature_id_raises_value_error(self):
        spec = {
            "track_llm": {
                "candidates": [
                    {"feature_id": "dup_feat", "type": "window", "expression": "COUNT(bet_id)",
                     "window_frame": "ROWS BETWEEN 5 PRECEDING AND CURRENT ROW"},
                    {"feature_id": "dup_feat", "type": "window", "expression": "SUM(wager)",
                     "window_frame": "ROWS BETWEEN 5 PRECEDING AND CURRENT ROW"},
                ]
            }
        }
        with self.assertRaises(ValueError) as ctx:
            features_mod._validate_feature_spec(spec)
        self.assertIn("dup_feat", str(ctx.exception))


class TestNoFollowingInWindowFrame(unittest.TestCase):
    """Track LLM window_frame must not contain FOLLOWING."""

    def test_template_has_no_following_in_any_window_frame(self):
        spec = features_mod.load_feature_spec(SPEC_YAML)
        for cand in spec.get("track_llm", {}).get("candidates", []):
            wf = cand.get("window_frame", "") or ""
            self.assertNotIn(
                "FOLLOWING",
                wf.upper(),
                f"'{cand['feature_id']}' has FOLLOWING in window_frame: {wf!r}",
            )

    def test_following_in_window_frame_raises_value_error(self):
        spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "bad_feat",
                        "type": "window",
                        "expression": "COUNT(bet_id)",
                        "window_frame": "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING",
                    }
                ]
            }
        }
        with self.assertRaises(ValueError) as ctx:
            features_mod._validate_feature_spec(spec)
        self.assertIn("FOLLOWING", str(ctx.exception))
        self.assertIn("bad_feat", str(ctx.exception))


class TestNoSQLKeywordsInExpressions(unittest.TestCase):
    """Track LLM expressions must not contain SQL structural keywords."""

    FORBIDDEN_KEYWORDS = ["SELECT", "FROM", "JOIN", "UNION", "WITH"]

    def test_template_expressions_contain_no_sql_keywords(self):
        spec = features_mod.load_feature_spec(SPEC_YAML)
        for cand in spec.get("track_llm", {}).get("candidates", []):
            expr = cand.get("expression", "") or ""
            for kw in self.FORBIDDEN_KEYWORDS:
                self.assertNotIn(
                    kw,
                    expr.upper(),
                    f"'{cand['feature_id']}' expression contains '{kw}': {expr!r}",
                )

    def test_select_in_expression_raises_value_error(self):
        spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "inject_feat",
                        "type": "window",
                        "expression": "SELECT secret FROM other_table",
                        "window_frame": "ROWS BETWEEN 5 PRECEDING AND CURRENT ROW",
                    }
                ]
            }
        }
        with self.assertRaises(ValueError) as ctx:
            features_mod._validate_feature_spec(spec)
        self.assertIn("SELECT", str(ctx.exception))

    def test_join_in_expression_raises_value_error(self):
        spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "join_feat",
                        "type": "window",
                        "expression": "wager JOIN other ON ...",
                        "window_frame": "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW",
                    }
                ]
            }
        }
        with self.assertRaises(ValueError) as ctx:
            features_mod._validate_feature_spec(spec)
        self.assertIn("JOIN", str(ctx.exception))


class TestNoCyclicDependsOn(unittest.TestCase):
    """derived features must not have circular depends_on."""

    def test_no_circular_depends_on_raises_for_self_loop(self):
        spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "self_loop",
                        "type": "derived",
                        "expression": "self_loop * 2",
                        "depends_on": ["self_loop"],
                    }
                ]
            }
        }
        with self.assertRaises(ValueError) as ctx:
            features_mod._validate_feature_spec(spec)
        self.assertIn("self_loop", str(ctx.exception))

    def test_no_circular_depends_on_raises_for_two_node_cycle(self):
        spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "feat_a",
                        "type": "derived",
                        "expression": "feat_b + 1",
                        "depends_on": ["feat_b"],
                    },
                    {
                        "feature_id": "feat_b",
                        "type": "derived",
                        "expression": "feat_a + 1",
                        "depends_on": ["feat_a"],
                    },
                ]
            }
        }
        with self.assertRaises(ValueError) as ctx:
            features_mod._validate_feature_spec(spec)
        self.assertIn("Circular", str(ctx.exception))

    def test_valid_linear_depends_on_does_not_raise(self):
        spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "feat_base",
                        "type": "window",
                        "expression": "COUNT(bet_id)",
                        "window_frame": "ROWS BETWEEN 5 PRECEDING AND CURRENT ROW",
                    },
                    {
                        "feature_id": "feat_derived",
                        "type": "derived",
                        "expression": "feat_base / 10.0",
                        "depends_on": ["feat_base"],
                    },
                ]
            }
        }
        # Should not raise
        features_mod._validate_feature_spec(spec)

    def test_template_has_no_circular_depends_on(self):
        """The template YAML must load without circular-dependency errors."""
        spec = features_mod.load_feature_spec(SPEC_YAML)
        # If no exception, the template is clean
        self.assertIsNotNone(spec)


class TestTemplateDtypeIntegrity(unittest.TestCase):
    """Step 8: Template candidates use allowed dtypes only (int, float, str)."""

    ALLOWED_DTYPES = {"int", "float", "str"}

    def test_template_candidates_have_allowed_dtype_or_none(self):
        spec = features_mod.load_feature_spec(SPEC_YAML)
        for track_key in ("track_llm", "track_human", "track_profile"):
            track = spec.get(track_key) or {}
            for cand in track.get("candidates", []):
                fid = cand.get("feature_id", "")
                dtype = cand.get("dtype")
                if dtype is not None:
                    self.assertIn(
                        dtype,
                        self.ALLOWED_DTYPES,
                        f"[{track_key}] feature_id {fid!r} has dtype {dtype!r}; allowed: {self.ALLOWED_DTYPES}",
                    )


class TestTrackHumanRunBoundaryInputContract(unittest.TestCase):
    """Run-boundary family must include wager in input_columns."""

    _RUN_BOUNDARY_FIDS = {
        "minutes_since_run_start",
        "bets_in_run_so_far",
        "wager_sum_in_run_so_far",
        "net_win_in_run_so_far",
        "net_win_per_bet_in_run",
    }

    def test_run_boundary_features_require_wager_input(self):
        spec = features_mod.load_feature_spec(SPEC_YAML)
        track_human = (spec.get("track_human") or {}).get("candidates", [])
        by_id = {
            c.get("feature_id"): c
            for c in track_human
            if isinstance(c, dict) and c.get("feature_id")
        }

        missing = sorted(fid for fid in self._RUN_BOUNDARY_FIDS if fid not in by_id)
        self.assertFalse(
            missing,
            f"Missing expected run-boundary candidates in track_human: {missing}",
        )

        for fid in sorted(self._RUN_BOUNDARY_FIDS):
            inputs = by_id[fid].get("input_columns") or []
            self.assertIn(
                "wager",
                inputs,
                f"{fid} must include 'wager' in input_columns to compute wager_sum_in_run_so_far correctly.",
            )


class TestTrackHumanWave2PersonalizedContract(unittest.TestCase):
    """Wave 2 personalized features must use python_vectorized contract."""

    _WAVE2_PERSONALIZED_FIDS = {
        "run_duration_vs_personal_avg",
        "bets_in_run_vs_personal_avg",
        "pace_vs_personal_baseline",
    }

    def test_wave2_personalized_features_require_python_vectorized_contract(self):
        spec = features_mod.load_feature_spec(SPEC_YAML)
        track_human = (spec.get("track_human") or {}).get("candidates", [])
        by_id = {
            c.get("feature_id"): c
            for c in track_human
            if isinstance(c, dict) and c.get("feature_id")
        }
        for fid in sorted(self._WAVE2_PERSONALIZED_FIDS):
            self.assertIn(fid, by_id, f"Missing expected Wave 2 feature: {fid}")
            cand = by_id[fid]
            self.assertEqual(cand.get("type"), "python_vectorized", f"{fid} must be python_vectorized")
            self.assertEqual(
                cand.get("function_name"),
                "compute_wave2_personalized_features",
                f"{fid} must use compute_wave2_personalized_features",
            )
            self.assertTrue(cand.get("input_columns"), f"{fid} must define input_columns")
            self.assertTrue(cand.get("output_columns"), f"{fid} must define output_columns")


class TestLoadFeatureSpecFileNotFound(unittest.TestCase):
    """load_feature_spec raises FileNotFoundError for missing paths."""

    def test_missing_path_raises_file_not_found_error(self):
        with self.assertRaises(FileNotFoundError):
            features_mod.load_feature_spec("/nonexistent/path/features.yaml")


class TestLoadFeatureSpecViaYAMLFile(unittest.TestCase):
    """Round-trip: write a minimal valid YAML and load it."""

    def _write_and_load(self, spec_dict: dict) -> dict:
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as fh:
            yaml.dump(spec_dict, fh, allow_unicode=True)
            tmp_path = fh.name
        self.addCleanup(os.remove, tmp_path)
        return features_mod.load_feature_spec(tmp_path)

    def test_minimal_valid_spec_loads(self):
        spec = {
            "version": "2.0",
            "spec_id": "test_spec",
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "cnt_w15m",
                        "type": "window",
                        "expression": "COUNT(bet_id)",
                        "window_frame": "RANGE BETWEEN INTERVAL 15 MINUTE PRECEDING AND CURRENT ROW",
                    }
                ]
            },
        }
        loaded = self._write_and_load(spec)
        self.assertEqual(loaded["spec_id"], "test_spec")


if __name__ == "__main__":
    unittest.main()
