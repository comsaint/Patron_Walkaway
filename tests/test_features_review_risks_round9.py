"""tests/test_features_review_risks_round9.py
=============================================
Guardrail tests for the Round 9 review risks (R17–R21) in `trainer/features.py`.

Constraint: this round only adds tests (no production code changes).  Therefore
tests that currently expose a bug are marked with `unittest.expectedFailure`,
to be removed once the corresponding production fixes are implemented.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _import_features():
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.features")


FEATURES = _import_features()

compute_table_hc = FEATURES.compute_table_hc
compute_run_boundary = FEATURES.compute_run_boundary
screen_features = FEATURES.screen_features

try:
    from trainer.config import BET_AVAIL_DELAY_MIN
except Exception:
    BET_AVAIL_DELAY_MIN = 1

_BASE = datetime(2025, 1, 1)


class TestFeaturesReviewRisksRound9(unittest.TestCase):
    def test_r17_screen_features_prunes_highly_correlated_pair(self):
        """R17: correlation pruning should drop one of a highly correlated pair."""
        n = 200
        x = np.arange(n, dtype=float)
        # A & B are almost identical (|r| ~ 1), C is unrelated noise.
        A = x
        B = x + 1e-9 * np.random.RandomState(0).normal(size=n)
        C = np.random.RandomState(1).normal(size=n)

        df = pd.DataFrame({"A": A, "B": B, "C": C})
        labels = pd.Series((x >= (n // 2)).astype(int))

        selected = screen_features(
            feature_matrix=df,
            labels=labels,
            feature_names=["A", "B", "C"],
            corr_threshold=0.95,
            top_k=None,
            use_lgbm=False,
        )

        # After pruning, at most one of {A, B} should remain.
        kept_corr_pair = set(selected) & {"A", "B"}
        self.assertEqual(len(kept_corr_pair), 1)
        # C should remain (it is not highly correlated with A/B).
        self.assertIn("C", selected)

    def test_r18_table_hc_ignores_nan_player_id(self):
        """R18: NaN player_id must not count toward unique headcount."""
        t_pool = 0
        t_target = BET_AVAIL_DELAY_MIN + 1
        df = pd.DataFrame(
            [
                {
                    "bet_id": 1,
                    "payout_complete_dtm": _BASE + timedelta(minutes=t_pool),
                    "table_id": "T1",
                    "player_id": np.nan,
                },
                {
                    "bet_id": 2,
                    "payout_complete_dtm": _BASE + timedelta(minutes=t_target),
                    "table_id": "T1",
                    "player_id": 99,
                },
            ]
        )

        hc = compute_table_hc(df, cutoff_time=None)
        # The only prior bet in-window has NaN player_id → count should be 0.
        self.assertEqual(int(hc.loc[1]), 0)

    def test_r20_table_hc_ignores_nat_payout_complete_dtm(self):
        """R20: NaT payout_complete_dtm rows must not contaminate windows."""
        df = pd.DataFrame(
            [
                {
                    "bet_id": 1,
                    "payout_complete_dtm": pd.NaT,
                    "table_id": "T1",
                    "player_id": 1,
                },
                {
                    "bet_id": 2,
                    "payout_complete_dtm": _BASE + timedelta(minutes=10),
                    "table_id": "T1",
                    "player_id": 2,
                },
            ]
        )
        hc = compute_table_hc(df, cutoff_time=None)
        # The prior row has NaT time and should be ignored → headcount at bet_id=2 is 0.
        self.assertEqual(int(hc.loc[1]), 0)

    def test_r19_build_entity_set_applies_hist_avg_bet_cap(self):
        """R19: build_entity_set should clip wager/payout/turnover by HIST_AVG_BET_CAP (F2)."""
        # This is enforced as a lint-like structural rule:
        # `build_entity_set` should contain a `.clip(...HIST_AVG_BET_CAP...)` call.
        src = inspect.getsource(FEATURES.build_entity_set)
        tree = ast.parse(src)

        def _call_uses_cap(node: ast.Call) -> bool:
            # Match: <expr>.clip(upper=HIST_AVG_BET_CAP) or .clip(HIST_AVG_BET_CAP)
            # We accept any of: keyword upper=Name('HIST_AVG_BET_CAP') or positional.
            for kw in node.keywords:
                if kw.arg == "upper" and isinstance(kw.value, ast.Name) and kw.value.id == "HIST_AVG_BET_CAP":
                    return True
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id == "HIST_AVG_BET_CAP":
                    return True
            return False

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "clip":
                if _call_uses_cap(node):
                    found = True
                    break

        self.assertTrue(found, "build_entity_set is expected to clip numeric columns using HIST_AVG_BET_CAP")

    def test_r21_compute_run_boundary_accepts_cutoff_time_param(self):
        """R21: compute_run_boundary should accept cutoff_time to match other Track B APIs."""
        sig = inspect.signature(compute_run_boundary)
        self.assertIn("cutoff_time", sig.parameters)


if __name__ == "__main__":
    unittest.main()

