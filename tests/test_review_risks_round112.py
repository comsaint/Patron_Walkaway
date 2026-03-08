"""Minimal reproducible guards for Round 112 reviewer risks.

Scope:
- Tests-only changes (no production edits).
- Convert reviewed risks into executable guards.
- Unresolved risks are marked expectedFailure to keep CI stable while
  preserving visibility.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

import pandas as pd
import yaml

# Keep imports stable when this file is run directly.
repo_root = Path(__file__).resolve().parents[1]
repo_root_str = str(repo_root)
if repo_root_str not in sys.path:
    sys.path.insert(0, repo_root_str)

import trainer.features as features_mod  # noqa: E402


class TestRound112RiskGuards(unittest.TestCase):
    """Executable guards for R112-1 ~ R112-5."""

    def test_r112_1_passthrough_should_have_explicit_sql_branch(self):
        """R112-1: compute_track_llm_features should explicitly handle passthrough."""
        src = inspect.getsource(features_mod.compute_track_llm_features)
        self.assertIn(
            'ftype == "passthrough"',
            src,
            "Expected explicit passthrough branch instead of implicit derived fallback.",
        )

    def test_r112_2_unknown_aggregate_should_be_rejected(self):
        """R112-2: validation should reject unknown aggregate function names."""
        bad_spec = {
            "guardrails": {
                "allowed_aggregate_functions": ["COUNT", "SUM", "AVG", "MIN", "MAX", "STDDEV_SAMP"]
            },
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "x_bad",
                        "type": "window",
                        "dtype": "float",
                        "expression": "SOME_CUSTOM_FUNC(wager)",
                        "window_frame": "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
                    }
                ]
            },
            "track_human": {"candidates": []},
            "track_profile": {"candidates": []},
        }
        with self.assertRaises(ValueError):
            features_mod._validate_feature_spec(bad_spec)  # pylint: disable=protected-access

    def test_r112_2_count_star_should_be_accepted(self):
        """R112-2: COUNT(*) should remain acceptable for window features."""
        good_spec = {
            "guardrails": {"allowed_aggregate_functions": ["COUNT"]},
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "cum_bets",
                        "type": "window",
                        "dtype": "int",
                        "expression": "COUNT(*)",
                        "window_frame": "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
                    }
                ]
            },
            "track_human": {"candidates": []},
            "track_profile": {"candidates": []},
        }
        features_mod._validate_feature_spec(good_spec)  # pylint: disable=protected-access

    def test_r112_3_coerce_feature_dtypes_is_in_place(self):
        """R112-3: helper currently mutates caller DataFrame in place."""
        df = pd.DataFrame({"a": ["1", "2", "x"], "b": [1.0, 2.0, 3.0]})
        out = features_mod.coerce_feature_dtypes(df, ["a", "b"])
        self.assertIs(out, df, "Expected in-place behavior (same DataFrame object).")
        self.assertTrue(pd.api.types.is_numeric_dtype(df["a"]))
        self.assertTrue(pd.isna(df.loc[2, "a"]))

    def test_r112_4_screening_eligible_string_false_should_be_excluded(self):
        """R112-4: string 'false' should be treated as screening disabled."""
        spec = {
            "track_llm": {
                "candidates": [
                    {"feature_id": "keep_me", "dtype": "float"},
                    {"feature_id": "drop_me", "dtype": "float", "screening_eligible": "false"},
                ]
            }
        }
        got = features_mod.get_candidate_feature_ids(spec, "track_llm", screening_only=True)
        self.assertNotIn("drop_me", got)

    def test_r112_4_screening_eligible_zero_should_be_excluded(self):
        """R112-4: numeric 0 should be treated as screening disabled."""
        spec = {
            "track_llm": {
                "candidates": [
                    {"feature_id": "keep_me", "dtype": "float"},
                    {"feature_id": "drop_me", "dtype": "float", "screening_eligible": 0},
                ]
            }
        }
        got = features_mod.get_candidate_feature_ids(spec, "track_llm", screening_only=True)
        self.assertNotIn("drop_me", got)

    def test_r112_5_load_feature_spec_accepts_pi_sin_cos(self):
        """R112-5: current template with pi/sin/cos should load successfully."""
        tpl = (
            Path(__file__).resolve().parents[1]
            / "trainer"
            / "feature_spec"
            / "features_candidates.template.yaml"
        )
        spec = features_mod.load_feature_spec(tpl)
        self.assertIn("track_llm", spec)

    def test_r112_5_time_of_day_features_range_within_minus1_to_1(self):
        """R112-5: derived time-of-day encodings should stay in [-1, 1]."""
        bets = pd.DataFrame(
            {
                "canonical_id": ["C1", "C1", "C2"],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-01-01 00:00:00", "2026-01-01 12:30:00", "2026-01-01 23:59:00"]
                ),
                "bet_id": [1, 2, 3],
            }
        )
        spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "time_of_day_sin",
                        "type": "derived",
                        "dtype": "float",
                        "expression": (
                            "sin(2 * pi() * (date_part('hour', payout_complete_dtm) * 60 "
                            "+ date_part('minute', payout_complete_dtm)) / 1440)"
                        ),
                    },
                    {
                        "feature_id": "time_of_day_cos",
                        "type": "derived",
                        "dtype": "float",
                        "expression": (
                            "cos(2 * pi() * (date_part('hour', payout_complete_dtm) * 60 "
                            "+ date_part('minute', payout_complete_dtm)) / 1440)"
                        ),
                    },
                ]
            }
        }
        out = features_mod.compute_track_llm_features(bets, spec)
        self.assertTrue(((out["time_of_day_sin"] >= -1.0) & (out["time_of_day_sin"] <= 1.0)).all())
        self.assertTrue(((out["time_of_day_cos"] >= -1.0) & (out["time_of_day_cos"] <= 1.0)).all())


class TestRound112LintLikeRules(unittest.TestCase):
    """Lint-like static checks for documentation-level risk assumptions."""

    def test_template_contains_prev_status_screening_disabled(self):
        """Guard the Step-1 expectation: prev_status must remain screening_eligible=false."""
        tpl = (
            Path(__file__).resolve().parents[1]
            / "trainer"
            / "feature_spec"
            / "features_candidates.template.yaml"
        )
        with tpl.open(encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)

        llm = (spec.get("track_llm") or {}).get("candidates") or []
        prev = [c for c in llm if c.get("feature_id") == "prev_status"]
        self.assertTrue(prev, "prev_status candidate should exist in template.")
        self.assertIs(
            prev[0].get("screening_eligible"),
            False,
            "prev_status must not participate in screening.",
        )


if __name__ == "__main__":
    unittest.main()
