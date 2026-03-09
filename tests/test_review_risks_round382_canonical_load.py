"""Round 382 Review — Step 8 載入 artifact 風險點轉成最小可重現測試。

STATUS.md « Round 382 Review »：將審查風險點轉為最小可重現測試或靜態規則。
僅新增測試，不修改 production code。

Reference: PLAN § 寫出與載入（步驟 8）；STATUS Round 382 Review；DECISION_LOG.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from trainer.trainer import (
    CANONICAL_MAPPING_CUTOFF_JSON,
    CANONICAL_MAPPING_PARQUET,
    PROJECT_ROOT,
    run_pipeline,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")


def _get_step3_load_artifact_block(src: str) -> str:
    """Return the Step 3 'try load existing artifact' block (PLAN step 8)."""
    marker = "PLAN step 8: try load existing artifact once"
    idx = src.find(marker)
    if idx < 0:
        return ""
    end_marker = "if loaded_from_artifact:"
    end_idx = src.find(end_marker, idx)
    if end_idx < 0:
        return src[idx : idx + 2000]
    return src[idx:end_idx]


# ---------------------------------------------------------------------------
# R382 Review #1 — dummy_player_ids 為 null 時不應拋錯／應 fallback 或載入空 set
# ---------------------------------------------------------------------------


class TestR382_1_DummyPlayerIdsNullSafe(unittest.TestCase):
    """Review #1: Sidecar with dummy_player_ids null or missing must not cause uncaught exception."""

    def test_source_uses_or_list_for_dummy_player_ids_from_sidecar(self):
        """Rule: Assigning dummy_player_ids from sidecar must use 'or []' so null is safe (R382 #1)."""
        block = _get_step3_load_artifact_block(_TRAINER_SRC)
        self.assertIn("dummy_player_ids", block, "Step 3 load block must reference dummy_player_ids")
        # Required: .get("dummy_player_ids") or [] so that key present with value null does not yield None
        has_safe = " or []" in block and "dummy_player_ids" in block
        self.assertTrue(
            has_safe,
            "Step 3 load block must use 'or []' for dummy_player_ids from sidecar so null is safe (e.g. .get('dummy_player_ids') or [])",
        )

    def test_sidecar_dummy_player_ids_null_no_uncaught_exception(self):
        """With sidecar containing dummy_player_ids: null, run_pipeline must not raise; fallback to build."""
        sidecar = {"cutoff_dtm": "2025-06-01T00:00:00", "dummy_player_ids": None}
        valid_map = pd.DataFrame({"player_id": [1], "canonical_id": [1]})
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sidecar_path = tmp_path / "canonical_mapping.cutoff.json"
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
            parquet_path = tmp_path / "canonical_mapping.parquet"
            valid_map.to_parquet(parquet_path, index=False)
            with (
                patch("trainer.trainer.CANONICAL_MAPPING_PARQUET", parquet_path),
                patch("trainer.trainer.CANONICAL_MAPPING_CUTOFF_JSON", sidecar_path),
                patch("trainer.trainer.get_monthly_chunks") as mock_chunks,
                patch("trainer.trainer.build_canonical_links_and_dummy_from_duckdb") as mock_links,
                patch("trainer.trainer.build_canonical_mapping_from_links") as mock_build_links,
                patch("trainer.trainer.ensure_player_profile_ready"),
                patch("trainer.trainer.load_player_profile", return_value=pd.DataFrame()),
                patch("trainer.trainer.process_chunk", return_value=tmp_path / "fake.parquet"),
                patch("trainer.trainer.train_single_rated_model", return_value=({}, None, {})),
                patch("trainer.trainer.save_artifact_bundle"),
            ):
                mock_chunks.return_value = [
                    {
                        "window_start": pd.Timestamp("2025-01-01"),
                        "window_end": pd.Timestamp("2025-06-01"),
                        "extended_end": pd.Timestamp("2025-06-02"),
                    }
                ]
                mock_links.return_value = (
                    pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"]),
                    set(),
                )
                mock_build_links.return_value = valid_map
                fake_chunk_path = tmp_path / "fake.parquet"
                chunk_df = pd.DataFrame(
                    {"payout_complete_dtm": [pd.Timestamp("2025-05-15")], "label": [1], "is_rated": [True]}
                )
                chunk_df.to_parquet(fake_chunk_path, index=False)

                def read_side_effect(path, *args, **kwargs):
                    if path is not None and Path(path).resolve() == parquet_path.resolve():
                        return valid_map
                    return chunk_df

                with patch("trainer.trainer.pd.read_parquet", side_effect=read_side_effect):
                    args = argparse.Namespace(
                        start="2025-01-01",
                        end="2025-06-01",
                        days=None,
                        use_local_parquet=True,
                        force_recompute=False,
                        skip_optuna=True,
                        recent_chunks=None,
                    )
                    run_pipeline(args)
                # With .get("dummy_player_ids") or [] fix: null loads as empty set, so no fallback (artifact load succeeds)
                self.assertEqual(
                    mock_links.call_count,
                    0,
                    "With dummy_player_ids null, artifact load should succeed (empty set); no fallback to DuckDB build",
                )


# ---------------------------------------------------------------------------
# R382 Review #2 — dummy_player_ids 內含不可轉 int 之元素
# ---------------------------------------------------------------------------


class TestR382_2_DummyPlayerIdsNonIntElements(unittest.TestCase):
    """Review #2: Sidecar with non-int in dummy_player_ids must not raise uncaught; fallback or defensive load."""

    def test_sidecar_dummy_player_ids_mixed_types_no_uncaught_exception(self):
        """With sidecar containing dummy_player_ids [1, 'x', 2], run_pipeline must not raise."""
        sidecar = {"cutoff_dtm": "2025-06-01T00:00:00", "dummy_player_ids": [1, "x", 2]}
        valid_map = pd.DataFrame({"player_id": [1], "canonical_id": [1]})
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sidecar_path = tmp_path / "canonical_mapping.cutoff.json"
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
            parquet_path = tmp_path / "canonical_mapping.parquet"
            valid_map.to_parquet(parquet_path, index=False)
            with (
                patch("trainer.trainer.CANONICAL_MAPPING_PARQUET", parquet_path),
                patch("trainer.trainer.CANONICAL_MAPPING_CUTOFF_JSON", sidecar_path),
                patch("trainer.trainer.get_monthly_chunks") as mock_chunks,
                patch("trainer.trainer.build_canonical_links_and_dummy_from_duckdb") as mock_links,
                patch("trainer.trainer.build_canonical_mapping_from_links") as mock_build_links,
                patch("trainer.trainer.ensure_player_profile_ready"),
                patch("trainer.trainer.load_player_profile", return_value=pd.DataFrame()),
                patch("trainer.trainer.process_chunk", return_value=tmp_path / "fake.parquet"),
                patch("trainer.trainer.train_single_rated_model", return_value=({}, None, {})),
                patch("trainer.trainer.save_artifact_bundle"),
            ):
                mock_chunks.return_value = [
                    {
                        "window_start": pd.Timestamp("2025-01-01"),
                        "window_end": pd.Timestamp("2025-06-01"),
                        "extended_end": pd.Timestamp("2025-06-02"),
                    }
                ]
                mock_links.return_value = (
                    pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"]),
                    set(),
                )
                mock_build_links.return_value = valid_map
                fake_chunk_path = tmp_path / "fake.parquet"
                chunk_df = pd.DataFrame(
                    {"payout_complete_dtm": [pd.Timestamp("2025-05-15")], "label": [1], "is_rated": [True]}
                )
                chunk_df.to_parquet(fake_chunk_path, index=False)

                def read_side_effect(path, *args, **kwargs):
                    if path is not None and Path(path).resolve() == parquet_path.resolve():
                        return valid_map
                    return chunk_df

                with patch("trainer.trainer.pd.read_parquet", side_effect=read_side_effect):
                    args = argparse.Namespace(
                        start="2025-01-01",
                        end="2025-06-01",
                        days=None,
                        use_local_parquet=True,
                        force_recompute=False,
                        skip_optuna=True,
                        recent_chunks=None,
                    )
                    run_pipeline(args)
                self.assertGreater(mock_links.call_count, 0, "Expect fallback when dummy_player_ids has non-int")


# ---------------------------------------------------------------------------
# R382 Review #3 — TOCTOU: read_parquet 拋錯時 fallback 且 log
# ---------------------------------------------------------------------------


class TestR382_3_LoadFailureFallbackAndLog(unittest.TestCase):
    """Review #3: When read_parquet (or open sidecar) raises, exception must be caught and fallback to build."""

    def test_read_parquet_filenotfound_fallback_and_log(self):
        """When read_parquet raises FileNotFoundError, no uncaught exception and fallback to build."""
        sidecar = {"cutoff_dtm": "2025-06-01T00:00:00", "dummy_player_ids": []}
        valid_map = pd.DataFrame({"player_id": [1], "canonical_id": [1]})
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sidecar_path = tmp_path / "canonical_mapping.cutoff.json"
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
            parquet_path = tmp_path / "canonical_mapping.parquet"
            valid_map.to_parquet(parquet_path, index=False)
            with (
                patch("trainer.trainer.CANONICAL_MAPPING_PARQUET", parquet_path),
                patch("trainer.trainer.CANONICAL_MAPPING_CUTOFF_JSON", sidecar_path),
                patch("trainer.trainer.get_monthly_chunks") as mock_chunks,
                patch("trainer.trainer.build_canonical_links_and_dummy_from_duckdb") as mock_links,
                patch("trainer.trainer.build_canonical_mapping_from_links") as mock_build_links,
                patch("trainer.trainer.ensure_player_profile_ready"),
                patch("trainer.trainer.load_player_profile", return_value=pd.DataFrame()),
                patch("trainer.trainer.process_chunk", return_value=tmp_path / "fake.parquet"),
                patch("trainer.trainer.train_single_rated_model", return_value=({}, None, {})),
                patch("trainer.trainer.save_artifact_bundle"),
            ):
                mock_chunks.return_value = [
                    {
                        "window_start": pd.Timestamp("2025-01-01"),
                        "window_end": pd.Timestamp("2025-06-01"),
                        "extended_end": pd.Timestamp("2025-06-02"),
                    }
                ]
                mock_links.return_value = (
                    pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"]),
                    set(),
                )
                mock_build_links.return_value = valid_map
                fake_chunk_path = tmp_path / "fake.parquet"
                chunk_df = pd.DataFrame(
                    {"payout_complete_dtm": [pd.Timestamp("2025-05-15")], "label": [1], "is_rated": [True]}
                )
                chunk_df.to_parquet(fake_chunk_path, index=False)

                def read_raise_for_canonical(path, *args, **kwargs):
                    if path is not None and Path(path).resolve() == parquet_path.resolve():
                        raise FileNotFoundError("No such file")
                    return chunk_df

                with patch("trainer.trainer.pd.read_parquet", side_effect=read_raise_for_canonical):
                    args = argparse.Namespace(
                        start="2025-01-01",
                        end="2025-06-01",
                        days=None,
                        use_local_parquet=True,
                        force_recompute=False,
                        skip_optuna=True,
                        recent_chunks=None,
                    )
                    run_pipeline(args)
                self.assertGreater(mock_links.call_count, 0, "Expect fallback when read_parquet raises")

    def test_load_block_has_try_except_and_warning_log(self):
        """Step 3 load block must use try/except and log 'Load canonical mapping artifact failed'."""
        block = _get_step3_load_artifact_block(_TRAINER_SRC)
        self.assertIn("try:", block)
        self.assertIn("except Exception", block)
        self.assertIn("Load canonical mapping artifact failed", block)
        self.assertIn("logger.warning", block)


# ---------------------------------------------------------------------------
# R382 Review #4 — artifact 路徑在專案可控目錄下
# ---------------------------------------------------------------------------


class TestR382_4_ArtifactPathsUnderProjectRoot(unittest.TestCase):
    """Review #4: Canonical mapping artifact paths must resolve under PROJECT_ROOT."""

    def test_canonical_mapping_parquet_under_project_root(self):
        """CANONICAL_MAPPING_PARQUET.resolve() must be under PROJECT_ROOT."""
        resolved = CANONICAL_MAPPING_PARQUET.resolve()
        self.assertIn(
            PROJECT_ROOT.resolve(),
            list(resolved.parents),
            "CANONICAL_MAPPING_PARQUET must be under PROJECT_ROOT",
        )

    def test_canonical_mapping_cutoff_json_under_project_root(self):
        """CANONICAL_MAPPING_CUTOFF_JSON.resolve() must be under PROJECT_ROOT."""
        resolved = CANONICAL_MAPPING_CUTOFF_JSON.resolve()
        self.assertIn(
            PROJECT_ROOT.resolve(),
            list(resolved.parents),
            "CANONICAL_MAPPING_CUTOFF_JSON must be under PROJECT_ROOT",
        )


# ---------------------------------------------------------------------------
# R382 Review #5 — cutoff < train_end 時不讀 parquet
# ---------------------------------------------------------------------------


class TestR382_5_CutoffLtTrainEndSkipsParquetRead(unittest.TestCase):
    """Review #5: When sidecar cutoff < train_end, read_parquet must not be called for canonical mapping."""

    def test_cutoff_lt_train_end_read_parquet_not_called_for_canonical(self):
        """When artifact exists but cutoff < train_end, Step 3 must not call read_parquet for the mapping file."""
        sidecar = {"cutoff_dtm": "2025-01-01T00:00:00", "dummy_player_ids": []}
        valid_map = pd.DataFrame({"player_id": [1], "canonical_id": [1]})
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sidecar_path = tmp_path / "canonical_mapping.cutoff.json"
            sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
            parquet_path = tmp_path / "canonical_mapping.parquet"
            valid_map.to_parquet(parquet_path, index=False)
            read_calls = []

            def track_read_parquet(path, *args, **kwargs):
                read_calls.append(Path(path).resolve() if path is not None else None)
                return pd.DataFrame(
                    {"payout_complete_dtm": [pd.Timestamp("2025-05-15")], "label": [1], "is_rated": [True]}
                )

            with (
                patch("trainer.trainer.CANONICAL_MAPPING_PARQUET", parquet_path),
                patch("trainer.trainer.CANONICAL_MAPPING_CUTOFF_JSON", sidecar_path),
                patch("trainer.trainer.get_monthly_chunks") as mock_chunks,
                patch("trainer.trainer.build_canonical_links_and_dummy_from_duckdb") as mock_links,
                patch("trainer.trainer.build_canonical_mapping_from_links") as mock_build_links,
                patch("trainer.trainer.ensure_player_profile_ready"),
                patch("trainer.trainer.load_player_profile", return_value=pd.DataFrame()),
                patch("trainer.trainer.process_chunk", return_value=tmp_path / "fake.parquet"),
                patch("trainer.trainer.train_single_rated_model", return_value=({}, None, {})),
                patch("trainer.trainer.save_artifact_bundle"),
            ):
                mock_chunks.return_value = [
                    {
                        "window_start": pd.Timestamp("2025-01-01"),
                        "window_end": pd.Timestamp("2025-06-01"),
                        "extended_end": pd.Timestamp("2025-06-02"),
                    }
                ]
                mock_links.return_value = (
                    pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"]),
                    set(),
                )
                mock_build_links.return_value = valid_map
                fake_chunk_path = tmp_path / "fake.parquet"
                pd.DataFrame(
                    {"payout_complete_dtm": [pd.Timestamp("2025-05-15")], "label": [1], "is_rated": [True]}
                ).to_parquet(fake_chunk_path, index=False)
                with patch("trainer.trainer.pd.read_parquet", side_effect=track_read_parquet):
                    args = argparse.Namespace(
                        start="2025-01-01",
                        end="2025-06-01",
                        days=None,
                        use_local_parquet=True,
                        force_recompute=False,
                        skip_optuna=True,
                        recent_chunks=None,
                    )
                    run_pipeline(args)
            canonical_resolved = parquet_path.resolve()
            canonical_read_calls = [c for c in read_calls if c == canonical_resolved]
            self.assertEqual(
                len(canonical_read_calls),
                0,
                "When cutoff < train_end, read_parquet must not be called for canonical_mapping.parquet in Step 3",
            )


if __name__ == "__main__":
    unittest.main()
