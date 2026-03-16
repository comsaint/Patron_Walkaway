"""Minimal reproducible tests for Round 184 Review — Step 8 抽樣篩選（策略 A）.

Review risks (Round 184 Review in STATUS.md) are turned into contract/behavior tests.
Tests that document desired behaviour not yet in production use @unittest.expectedFailure.
Tests-only: no production code changes.
"""

from __future__ import annotations

import argparse
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.config as config_mod
import trainer.features as features_mod
import trainer.trainer as trainer_mod
from trainer.trainer import _CANONICAL_MAP_SESSION_COLS


# ---------------------------------------------------------------------------
# §1 — 可觀測性：實際使用列數少於 cap 時 log 須標出 cap 值
# ---------------------------------------------------------------------------

class TestR184Step8LogIncludesCapWhenTrainSmallerThanCap(unittest.TestCase):
    """Round 184 Review §1: When len(train) < STEP8_SCREEN_SAMPLE_ROWS, log must include cap value."""

    def test_step8_sampling_log_includes_cap_value_when_k_lt_n(self):
        """In run_pipeline Step 8 block, when sampling is used and train has fewer rows than cap,
        the logger.info message must include the cap (STEP8_SCREEN_SAMPLE_ROWS) value so operators
        can distinguish 'used 5000' from 'cap 5000 but train had only 100'.
        """
        src = inspect.getsource(trainer_mod.run_pipeline)
        # Must have a format that exposes the cap when actual < cap (Round 184 Review §1).
        # e.g. "STEP8_SCREEN_SAMPLE_ROWS=%d" or "cap ... %d" in the sampling branch.
        step8_block_start = src.find("PLAN 方案 B 策略 A")
        self.assertGreater(step8_block_start, -1, "Step 8 sampling block not found")
        block = src[step8_block_start : step8_block_start + 2200]
        has_cap_in_log = (
            "STEP8_SCREEN_SAMPLE_ROWS=%d" in block
            or ("cap" in block and "%d" in block and "rows" in block.lower())
        )
        self.assertTrue(
            has_cap_in_log,
            "Step 8 sampling log must include cap value when train smaller than cap (Round 184 §1).",
        )


# ---------------------------------------------------------------------------
# §2 — 邊界：STEP8_SCREEN_SAMPLE_ROWS 為 float 時應安全
# ---------------------------------------------------------------------------

class TestR184Step8SampleRowsIntCoercionContract(unittest.TestCase):
    """Round 184 Review §2: Step 8 should coerce _sample_n to int before head()."""

    def test_step8_block_coerces_sample_n_to_int_before_head(self):
        """run_pipeline Step 8 block must use int(_sample_n) before calling head() so that
        float (e.g. 5000.0) is safely handled (Round 184 Review §2).
        """
        src = inspect.getsource(trainer_mod.run_pipeline)
        step8_block_start = src.find("_sample_n = STEP8_SCREEN_SAMPLE_ROWS")
        self.assertGreater(step8_block_start, -1, "Step 8 _sample_n assignment not found")
        block = src[step8_block_start : step8_block_start + 600]
        has_int_coercion = "int(_sample_n)" in block or "_sample_n = int(" in block
        self.assertTrue(
            has_int_coercion,
            "Step 8 must coerce _sample_n to int before head() (Round 184 §2).",
        )


class TestR184Step8FloatSampleRowsPandasBehavior(unittest.TestCase):
    """Round 184 Review §2: Document pandas behavior for head(float) — either works or raises."""

    def test_pandas_head_float_either_works_or_raises(self):
        """pandas head(5000.0): either returns 5000 rows or raises (e.g. TypeError).
        Documents that Step 8 should use int(_sample_n) for portability (Round 184 §2).
        """
        n = 10_000
        df = pd.DataFrame({"label": np.zeros(n), "f0": np.arange(n, dtype=float)})
        try:
            out = df.head(5000.0)
            self.assertEqual(len(out), 5000, "head(5000.0) should return 5000 rows when accepted")
        except TypeError:
            # Many pandas versions reject float in iloc; production must coerce to int.
            pass


# ---------------------------------------------------------------------------
# §3 — 邊界：train_df 空或極少列 → screening 回傳 []，bias fallback
# ---------------------------------------------------------------------------

class TestR184Step8EmptyFeatureMatrixReturnsEmptyList(unittest.TestCase):
    """Round 184 Review §3: screen_features with empty feature_matrix returns [] (bias fallback path)."""

    def test_screen_features_empty_matrix_returns_empty_list(self):
        """When feature_matrix has 0 rows, screen_features returns [] so downstream bias fallback applies."""
        empty_df = pd.DataFrame(columns=["label", "f0"])
        labels = pd.Series(dtype=float)
        result = features_mod.screen_features(
            feature_matrix=empty_df,
            labels=labels,
            feature_names=["f0"],
            screen_method="lgbm",
        )
        self.assertEqual(result, [], "Empty feature_matrix should yield empty screened list")


class TestR184Step8ZeroFeatureBiasFallbackContract(unittest.TestCase):
    """Round 184 Review §3: run_pipeline must have zero-feature bias fallback (R1613)."""

    def test_run_pipeline_has_zero_feature_bias_fallback(self):
        """run_pipeline source must contain the bias fallback when active_feature_cols is empty."""
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn("if not active_feature_cols:", src)
        self.assertIn("bias", src)
        self.assertIn("_placeholder_col", src)


# ---------------------------------------------------------------------------
# §4 — 使用語義：N 極小時 pipeline 仍完成不崩潰（可選）
# ---------------------------------------------------------------------------

def _make_chunks(n: int = 1):
    """Minimal monthly chunks for pipeline mocks."""
    from zoneinfo import ZoneInfo
    HK = ZoneInfo("Asia/Hong_Kong")
    base = __import__("datetime").datetime(2025, 1, 1, tzinfo=HK)
    delta = __import__("datetime").timedelta
    return [
        {
            "window_start": base + delta(days=30 * i),
            "window_end": base + delta(days=30 * (i + 1)),
            "extended_end": base + delta(days=30 * (i + 1) + 1),
        }
        for i in range(n)
    ]


def _minimal_session_parquet_for_canonical(path: Path) -> None:
    """Write a minimal session parquet with _CANONICAL_MAP_SESSION_COLS so DuckDB canonical path can read it."""
    base = {
        "session_id": "s1",
        "player_id": 10,
        "casino_player_id": "C1",
        "lud_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "session_start_dtm": pd.Timestamp("2025-01-15 11:00:00"),
        "session_end_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "is_manual": 0,
        "is_deleted": 0,
        "is_canceled": 0,
        "num_games_with_wager": 1,
        "turnover": 10.0,
    }
    df = pd.DataFrame([base])
    cols = [c for c in _CANONICAL_MAP_SESSION_COLS if c in df.columns]
    df[cols].to_parquet(path, index=False)


class TestR184Step8SmallNPipelineCompletes(unittest.TestCase):
    """Round 184 Review §4 (optional): STEP8_SCREEN_SAMPLE_ROWS=1 with small train completes without crash."""

    def test_step8_sample_rows_one_pipeline_completes(self):
        """With STEP8_SCREEN_SAMPLE_ROWS=1 and minimal train data, pipeline runs to completion (no crash)."""
        from trainer.trainer import run_pipeline

        chunks = _make_chunks(1)
        n_rows = 100
        fake_df = pd.DataFrame(
            {
                "payout_complete_dtm": pd.date_range("2025-01-01", periods=n_rows, freq="h"),
                "label": np.zeros(n_rows),
                "is_rated": [True] * n_rows,
                "canonical_id": ["C000"] * n_rows,
                "run_id": [1] * n_rows,
            }
        )
        cmap = pd.DataFrame({"player_id": [0], "canonical_id": ["C000"]})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            session_parquet = tmp_path / "gmwds_t_session.parquet"
            _minimal_session_parquet_for_canonical(session_parquet)
            # Step 7 sums Path(p).stat().st_size for chunk_paths; process_chunk returns this path
            fake_chunk_path = tmp_path / "fake.parquet"
            fake_chunk_path.write_bytes(b"x")

            _real_read_parquet = pd.read_parquet

            def read_parquet_side_effect(path, *args, **kwargs):
                # Step 3 reads session parquet; let it read the real file. Step 7+ read chunks → fake_df.
                try:
                    p = Path(path).resolve() if path else None
                    sp = session_parquet.resolve()
                except Exception:
                    return fake_df
                if p is not None and (p == sp or (hasattr(p, "name") and p.name == "gmwds_t_session.parquet")):
                    return _real_read_parquet(path, *args, **kwargs)
                return fake_df

            with (
                patch("trainer.trainer.LOCAL_PARQUET_DIR", tmp_path),
                patch("trainer.trainer.CANONICAL_MAPPING_PARQUET", tmp_path / "canonical_mapping.parquet"),
                patch("trainer.trainer.CANONICAL_MAPPING_CUTOFF_JSON", tmp_path / "canonical_mapping.cutoff.json"),
                patch("trainer.trainer.get_monthly_chunks", return_value=chunks),
                patch("trainer.trainer.STEP8_SCREEN_SAMPLE_ROWS", 1),
                patch("trainer.trainer.load_local_parquet", return_value=(pd.DataFrame(), pd.DataFrame())),
                patch("trainer.trainer.apply_dq", return_value=(pd.DataFrame(), pd.DataFrame())),
                patch("trainer.trainer.build_canonical_mapping_from_df", return_value=cmap),
                patch("trainer.trainer.get_dummy_player_ids_from_df", return_value=set()),
                patch("trainer.trainer.ensure_player_profile_ready"),
                patch("trainer.trainer.load_player_profile", return_value=None),
                patch("trainer.trainer.process_chunk", return_value=fake_chunk_path),
                patch("trainer.trainer.pd.read_parquet", side_effect=read_parquet_side_effect),
                patch("trainer.trainer.train_single_rated_model", return_value=(
                    {"model": None, "threshold": 0.5, "features": []},
                    None,
                    {},
                )),
                patch("trainer.trainer.save_artifact_bundle"),
            ):
                args = argparse.Namespace(
                    start="2025-01-01",
                    end="2025-02-01",
                    days=None,
                    use_local_parquet=True,
                    force_recompute=False,
                    skip_optuna=True,
                    recent_chunks=1,
                    no_preload=False,
                    sample_rated=None,
                )
                run_pipeline(args)


# ---------------------------------------------------------------------------
# Config contract (Round 184 related)
# ---------------------------------------------------------------------------

class TestR184Step8ConfigContract(unittest.TestCase):
    """STEP8_SCREEN_SAMPLE_ROWS exists and is Optional[int] (already in Round 182; keep for R184 suite)."""

    def test_step8_screen_sample_rows_exists(self):
        """Config must define STEP8_SCREEN_SAMPLE_ROWS."""
        self.assertTrue(hasattr(config_mod, "STEP8_SCREEN_SAMPLE_ROWS"))

    def test_step8_screen_sample_rows_none_or_int(self):
        """STEP8_SCREEN_SAMPLE_ROWS must be None or int."""
        val = getattr(config_mod, "STEP8_SCREEN_SAMPLE_ROWS", None)
        self.assertTrue(val is None or isinstance(val, int), f"Expected None or int, got {type(val)}")


if __name__ == "__main__":
    unittest.main()
