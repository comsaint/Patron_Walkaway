"""Train–Serve Parity：SCORER_LOOKBACK_HOURS 契約與 Track Human 產出一致性測試。

訓練／評估／serving 一律使用 SCORER_LOOKBACK_HOURS（預設 8h）；TRAINER_USE_LOOKBACK 已移除。
Run from repo root:
  python -m pytest tests/test_review_risks_train_serve_parity_config.py -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_BASE = datetime(2025, 1, 1)


def _bets(rows, canonical_id="P1", table_id="T1", player_id=1):
    """Minimal bets df from (offset_min, bet_id, status?) tuples."""
    records = []
    for item in rows:
        if len(item) == 3:
            offset_min, bid, status = item
        else:
            offset_min, bid = item
            status = "LOSE"
        records.append({
            "canonical_id": canonical_id,
            "bet_id": bid,
            "payout_complete_dtm": _BASE + timedelta(minutes=offset_min),
            "status": status,
            "table_id": table_id,
            "player_id": player_id,
        })
    return pd.DataFrame(records)


TRACK_HUMAN_COLS = (
    "loss_streak",
    "run_id",
    "minutes_since_run_start",
    "bets_in_run_so_far",
    "wager_sum_in_run_so_far",
)


class TestScorerLookbackHoursTypeContract(unittest.TestCase):
    """§4: 契約測試 — SCORER_LOOKBACK_HOURS 為數值型（未來 env 擴充時防呆）。"""

    def test_scorer_lookback_hours_is_numeric(self):
        """config 模組載入後，SCORER_LOOKBACK_HOURS 為 int 或 float。"""
        import trainer.config as config  # noqa: E402

        val = getattr(config, "SCORER_LOOKBACK_HOURS", None)
        self.assertIsNotNone(val, "config must define SCORER_LOOKBACK_HOURS")
        self.assertIsInstance(
            val,
            (int, float),
            "SCORER_LOOKBACK_HOURS must be int or float (STATUS Code Review §4).",
        )
        self.assertGreater(val, 0, "SCORER_LOOKBACK_HOURS must be positive")


class TestTrackHumanParitySameLookback(unittest.TestCase):
    """PLAN § Train–Serve Parity 步驟 3：同一批 bets、相同 lookback_hours 時，Track Human 產出一致。"""

    def test_add_track_human_features_deterministic_for_same_lookback(self):
        """相同 (bets, canonical_map, window_end, lookback_hours=8) 呼叫兩次，Track Human 欄位數值一致。"""
        from trainer.trainer import add_track_human_features

        rows = [(0, 1, "LOSE"), (10, 2, "LOSE"), (20, 3, "WIN"), (30, 4, "LOSE")]
        bets = _bets(rows)
        bets["wager"] = 1.0
        canonical_map = pd.DataFrame({"player_id": [1], "canonical_id": ["P1"]})
        window_end = _BASE + timedelta(minutes=25)
        lookback_hours = 8.0

        out1 = add_track_human_features(bets, canonical_map, window_end, lookback_hours=lookback_hours)
        out2 = add_track_human_features(bets, canonical_map, window_end, lookback_hours=lookback_hours)

        for col in TRACK_HUMAN_COLS:
            self.assertIn(col, out1.columns, f"missing col {col} in out1")
            self.assertIn(col, out2.columns, f"missing col {col} in out2")
            pd.testing.assert_series_equal(
                out1[col],
                out2[col],
                check_names=True,
                obj=f"Track Human parity: {col}",
            )

    def test_add_track_human_features_missing_canonical_id_returns_zeros(self):
        """缺 canonical_id 時五個 Track Human 欄位皆為 0（STATUS Code Review §3）。"""
        from trainer.trainer import add_track_human_features

        bets = _bets([(0, 1, "LOSE")])
        bets = bets.drop(columns=["canonical_id"])
        canonical_map = pd.DataFrame({"player_id": [1], "canonical_id": ["P1"]})
        window_end = _BASE + timedelta(minutes=10)

        out = add_track_human_features(bets, canonical_map, window_end)

        for col in TRACK_HUMAN_COLS:
            self.assertIn(col, out.columns, f"missing col {col}")
            self.assertTrue(
                (out[col] == 0).all(),
                f"{col} should be all 0 when canonical_id missing; got {out[col].tolist()}",
            )


if __name__ == "__main__":
    unittest.main()
