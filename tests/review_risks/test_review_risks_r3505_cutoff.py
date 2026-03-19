"""Code Review R3505 cutoff_time 正規化 — 最小可重現測試與 lint 規則 (tests-only).

對應 STATUS「Code Review：R3505 cutoff_time 正規化變更」四項風險；
不修改 production，僅以測試／source 檢查編碼預期行為。
"""

from __future__ import annotations

import inspect
import pathlib
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd

# Resolve trainer.scorer (re-exports trainer.serving.scorer)
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _minimal_bets_df():
    """Minimal non-empty bets so build_features_for_scoring reaches cutoff_naive and Track Human."""
    return pd.DataFrame({
        "bet_id": ["b1"],
        "session_id": ["s1"],
        "player_id": [100],
        "payout_complete_dtm": pd.to_datetime(["2025-01-01 10:00:00"]),
        "status": ["WIN"],
        "wager": [10.0],
    })


def _empty_sessions():
    return pd.DataFrame()


def _empty_canonical_map():
    return pd.DataFrame()


class TestR3505CutoffUsesHkTzConstant(unittest.TestCase):
    """Risk 1: cutoff 正規化應使用 HK_TZ 常數，而非硬編碼 'Asia/Hong_Kong'。"""

    def test_cutoff_normalization_uses_hk_tz_not_literal(self):
        """Lint 規則：build_features_for_scoring 內 cutoff 的 tz_convert 使用 HK_TZ，不用 'Asia/Hong_Kong'。"""
        from trainer import scorer as scorer_mod
        src = inspect.getsource(scorer_mod.build_features_for_scoring)
        # 擷取「Normalise cutoff_time」到「D2 identity」之間的區塊
        start = src.find("Normalise cutoff_time to tz-naive HK")
        end = src.find("# ── D2 identity", start) if start >= 0 else -1
        self.assertGreaterEqual(start, 0, "Source should contain R3505 cutoff comment")
        self.assertGreater(end, start, "Source should contain D2 identity block after cutoff")
        block = src[start:end]
        self.assertIn(
            "tz_convert(HK_TZ)",
            block,
            "Cutoff normalization should use tz_convert(HK_TZ) for config/SSOT consistency.",
        )
        self.assertNotIn(
            '"Asia/Hong_Kong"',
            block,
            "Cutoff normalization should not hardcode 'Asia/Hong_Kong'.",
        )


class TestR3505CutoffRejectsInvalid(unittest.TestCase):
    """Risk 2: 無效或缺失的 cutoff_time（None / NaT）應被拒絕。"""

    def test_build_features_for_scoring_rejects_none_cutoff(self):
        """傳入 cutoff_time=None 時應 raise ValueError（或等同），錯誤訊息提及 cutoff。"""
        from trainer.scorer import build_features_for_scoring
        bets = _minimal_bets_df()
        with self.assertRaises((ValueError, TypeError)) as ctx:
            build_features_for_scoring(bets, _empty_sessions(), _empty_canonical_map(), None)
        self.assertIn("cutoff", str(ctx.exception).lower())

    def test_build_features_for_scoring_rejects_nat_cutoff(self):
        """傳入 cutoff_time=pd.NaT 時應 raise ValueError（或等同），避免下游比較全 NaN。"""
        from trainer.scorer import build_features_for_scoring
        bets = _minimal_bets_df()
        with self.assertRaises((ValueError, TypeError)) as ctx:
            build_features_for_scoring(bets, _empty_sessions(), _empty_canonical_map(), pd.NaT)
        self.assertIn("cutoff", str(ctx.exception).lower())


class TestR3505CutoffDownstreamType(unittest.TestCase):
    """Risk 3: 傳給 compute_loss_streak / compute_run_boundary 的 cutoff 應為 datetime。"""

    def test_downstream_receives_datetime_when_naive_datetime_passed(self):
        """傳入 naive datetime 時，下游收到的 cutoff_time 型別應為 datetime。"""
        from trainer.scorer import build_features_for_scoring
        cutoff = datetime(2025, 1, 1, 12, 0, 0)
        received = {}

        def _capture_streak(bets_df, cutoff_time=None, lookback_hours=None):
            received["cutoff_time"] = cutoff_time
            return pd.Series(0, index=bets_df.index)

        def _capture_boundary(bets_df, cutoff_time=None, lookback_hours=None):
            received["cutoff_time_boundary"] = cutoff_time
            return pd.DataFrame({
                "run_id": 0,
                "minutes_since_run_start": 0.0,
                "bets_in_run_so_far": 0,
                "wager_sum_in_run_so_far": 0.0,
            }, index=bets_df.index)

        bets = _minimal_bets_df()
        with patch("trainer.serving.scorer.compute_loss_streak", side_effect=_capture_streak), patch(
            "trainer.serving.scorer.compute_run_boundary", side_effect=_capture_boundary
        ):
            build_features_for_scoring(bets, _empty_sessions(), _empty_canonical_map(), cutoff)
        self.assertIn("cutoff_time", received)
        self.assertIs(type(received["cutoff_time"]), datetime, "Downstream should receive datetime")

    def test_downstream_receives_datetime_when_naive_timestamp_passed(self):
        """傳入 naive pd.Timestamp 時，下游收到的 cutoff_time 型別應為 datetime（非 Timestamp）。"""
        from trainer.scorer import build_features_for_scoring
        cutoff = pd.Timestamp("2025-01-01 12:00:00")
        received = {}

        def _capture_streak(bets_df, cutoff_time=None, lookback_hours=None):
            received["cutoff_time"] = cutoff_time
            return pd.Series(0, index=bets_df.index)

        def _capture_boundary(bets_df, cutoff_time=None, lookback_hours=None):
            return pd.DataFrame({
                "run_id": 0,
                "minutes_since_run_start": 0.0,
                "bets_in_run_so_far": 0,
                "wager_sum_in_run_so_far": 0.0,
            }, index=bets_df.index)

        bets = _minimal_bets_df()
        with patch("trainer.serving.scorer.compute_loss_streak", side_effect=_capture_streak), patch(
            "trainer.serving.scorer.compute_run_boundary", side_effect=_capture_boundary
        ):
            build_features_for_scoring(bets, _empty_sessions(), _empty_canonical_map(), cutoff)
        self.assertIn("cutoff_time", received)
        self.assertIs(type(received["cutoff_time"]), datetime, "Downstream should receive datetime, not Timestamp")


class TestR3505CutoffDateOrString(unittest.TestCase):
    """Risk 4: date 或字串輸入應有明確行為（支援或明確拒絕）。"""

    def test_build_features_for_scoring_cutoff_date_raises(self):
        """傳入 date(2025,1,1) 時應 raise ValueError 或明確 doc 不支援。"""
        from trainer.scorer import build_features_for_scoring
        bets = _minimal_bets_df()
        with self.assertRaises((ValueError, TypeError)):
            build_features_for_scoring(bets, _empty_sessions(), _empty_canonical_map(), date(2025, 1, 1))

    def test_build_features_for_scoring_cutoff_string_behavior(self):
        """傳入字串 cutoff 時：要麼 raise，要麼回傳 DataFrame（不靜默崩潰）。"""
        from trainer.scorer import build_features_for_scoring
        bets = _minimal_bets_df()
        try:
            out = build_features_for_scoring(
                bets, _empty_sessions(), _empty_canonical_map(), "2025-01-01 12:00:00"
            )
            self.assertIsInstance(out, pd.DataFrame, "If string is accepted, must return DataFrame")
        except (ValueError, TypeError):
            pass  # Explicit reject is acceptable


if __name__ == "__main__":
    unittest.main()
