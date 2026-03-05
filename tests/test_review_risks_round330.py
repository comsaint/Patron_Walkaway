"""Round 97 reviewer risks -> minimal reproducible tests (tests-only).

This file intentionally does NOT modify production code.
Unfixed risks are tracked as expected failures so they stay visible.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import unittest

import pandas as pd


def _import_module(name: str):
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module(name)


def _read_text(rel_path: str) -> str:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    return (repo_root / rel_path).read_text(encoding="utf-8")


features_mod = _import_module("trainer.features")


def _minimal_spec(candidates, **extra):
    spec = {
        "version": "2.0",
        "spec_id": "risk_spec",
        "track_llm": {"candidates": candidates},
    }
    spec.update(extra)
    return spec


class TestR2000SqlInjectionSurface(unittest.TestCase):
    """R2000: feature_id / expression should reject SQL-injection primitives."""

    def test_feature_id_with_sql_tokens_should_be_rejected(self):
        spec = _minimal_spec(
            [
                {
                    "feature_id": "x; drop table t; --",
                    "type": "window",
                    "expression": "COUNT(bet_id)",
                    "window_frame": "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW",
                }
            ]
        )
        with self.assertRaises(ValueError):
            features_mod._validate_feature_spec(spec)

    def test_expression_with_semicolon_should_be_rejected(self):
        spec = _minimal_spec(
            [
                {
                    "feature_id": "safe_name",
                    "type": "window",
                    "expression": "COUNT(bet_id); SELECT 1",
                    "window_frame": "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW",
                }
            ]
        )
        with self.assertRaises(ValueError):
            features_mod._validate_feature_spec(spec)


class TestR2001FfillLeakageAcrossPlayers(unittest.TestCase):
    """R2001: ffill postprocess should not leak across canonical_id partitions."""

    def test_ffill_should_be_grouped_by_canonical_id(self):
        bets = pd.DataFrame(
            {
                "canonical_id": ["c1", "c1", "c2", "c2"],
                "bet_id": [1, 2, 1, 2],
                "payout_complete_dtm": pd.to_datetime(
                    [
                        "2026-03-01 10:00:00",
                        "2026-03-01 10:10:00",
                        "2026-03-01 10:00:00",
                        "2026-03-01 10:10:00",
                    ]
                ),
                "wager": [100.0, 200.0, 300.0, 400.0],
            }
        )
        spec = _minimal_spec(
            [
                {
                    "feature_id": "lag_wager_ffill",
                    "type": "lag",
                    "expression": "LAG(wager, 1)",
                    "postprocess": {"fill": {"strategy": "ffill"}},
                }
            ]
        )
        out = features_mod.compute_track_llm_features(bets, spec)
        c2 = out.loc[out["canonical_id"] == "c2", "lag_wager_ffill"].tolist()
        self.assertTrue(pd.isna(c2[0]), "first row of c2 should remain NaN after grouped ffill")


class TestR2002RangeOrderTieBreaker(unittest.TestCase):
    """R2002: RANGE windows should keep deterministic tie behavior for same timestamp."""

    def test_range_window_should_keep_bet_id_tie_breaker_contract(self):
        src = inspect.getsource(features_mod.compute_track_llm_features)
        self.assertNotIn(
            'order_by = "ORDER BY payout_complete_dtm ASC"',
            src,
            "RANGE path currently drops bet_id tie-breaker and weakens G3 stability contract.",
        )


class TestR2003ConnectionCloseOnError(unittest.TestCase):
    """R2003: DuckDB connection should close via finally on execution errors."""

    def test_compute_track_llm_features_should_close_connection_in_finally(self):
        src = inspect.getsource(features_mod.compute_track_llm_features)
        self.assertIn("finally", src)
        self.assertIn("con.close()", src)


class TestR2004GuardrailsFromYaml(unittest.TestCase):
    """R2004: validator should honor guardrail values declared in YAML."""

    def test_disallowed_keywords_should_be_read_from_spec_guardrails(self):
        spec = _minimal_spec(
            [
                {
                    "feature_id": "f1",
                    "type": "window",
                    "expression": "DATE_PART('hour', payout_complete_dtm)",
                    "window_frame": "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW",
                }
            ],
            guardrails={"disallow_sql_keywords_in_expressions": ["DATE_PART"]},
        )
        with self.assertRaises(ValueError):
            features_mod._validate_feature_spec(spec)


class TestR2005DerivedDependsOnOrdering(unittest.TestCase):
    """R2005: derived features should work even when YAML candidate order is not topological."""

    def test_derived_out_of_order_should_still_compute(self):
        bets = pd.DataFrame(
            {
                "canonical_id": ["c1", "c1"],
                "bet_id": [1, 2],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-03-01 10:00:00", "2026-03-01 10:05:00"]
                ),
                "wager": [100.0, 200.0],
            }
        )
        spec = _minimal_spec(
            [
                {
                    "feature_id": "derived_a",
                    "type": "derived",
                    "expression": "base_cnt / 10.0",
                    "depends_on": ["base_cnt"],
                },
                {
                    "feature_id": "base_cnt",
                    "type": "window",
                    "expression": "COUNT(bet_id)",
                    "window_frame": "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
                },
            ]
        )
        out = features_mod.compute_track_llm_features(bets, spec)
        self.assertIn("derived_a", out.columns)


class TestR2006NullTrackHandling(unittest.TestCase):
    """R2006: validator should gracefully handle null track sections."""

    def test_validate_should_not_crash_on_null_track_llm(self):
        # Should not raise AttributeError when track_llm is explicitly null.
        features_mod._validate_feature_spec({"track_llm": None})


class TestR2007KeywordBoundaryFalsePositive(unittest.TestCase):
    """R2007: keyword checks should use token boundary, not substring contains."""

    def test_union_substring_should_not_be_treated_as_union_keyword(self):
        spec = _minimal_spec(
            [
                {
                    "feature_id": "f_uniform",
                    "type": "window",
                    "expression": "joined_value + 1",
                    "window_frame": "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW",
                }
            ]
        )
        # Expected: no ValueError for plain identifier containing "union" substring.
        features_mod._validate_feature_spec(spec)


class TestR2008PassthroughIdentifierQuoting(unittest.TestCase):
    """R2008: passthrough columns with special names should be quoted safely."""

    def test_special_passthrough_column_name_should_not_break_sql(self):
        bets = pd.DataFrame(
            {
                "canonical_id": ["c1"],
                "bet_id": [1],
                "payout_complete_dtm": pd.to_datetime(["2026-03-01 10:00:00"]),
                "wager": [100.0],
                "my column": [123],  # needs identifier quoting
            }
        )
        spec = _minimal_spec(
            [
                {
                    "feature_id": "cnt",
                    "type": "window",
                    "expression": "COUNT(bet_id)",
                    "window_frame": "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
                }
            ]
        )
        out = features_mod.compute_track_llm_features(bets, spec)
        self.assertIn("my column", out.columns)


class TestR2009RedundantCopyPath(unittest.TestCase):
    """R2009: remove redundant DataFrame copy calls in compute_track_llm_features."""

    def test_compute_track_llm_features_should_not_have_redundant_copy(self):
        src = inspect.getsource(features_mod.compute_track_llm_features)
        self.assertNotIn('df = df.copy()', src)


if __name__ == "__main__":
    unittest.main()
