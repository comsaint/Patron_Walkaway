"""Round 379 Code Review — Canonical mapping ClickHouse 路徑寫出風險點轉成測試。

STATUS.md « Code Review：Round 379 變更 »：將審查風險點轉為最小可重現測試或契約檢查。
僅新增測試，不修改 production code。

Reference: PLAN § Canonical mapping 二、寫出與載入；STATUS Round 379；DECISION_LOG.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# Paths for source inspection
_REPO_ROOT = Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_run_pipeline_step3_ch_write_section(src: str) -> str:
    """Return the source block for Step 3 ClickHouse path write (else branch write)."""
    # Find "PLAN § Canonical mapping 步驟 7" comment and the following if/try/except block
    marker = "PLAN § Canonical mapping 步驟 7：ClickHouse 路徑建完後也寫出"
    idx = src.find(marker)
    if idx < 0:
        return ""
    # Include from marker to the end of the except block (next _el = time.perf_counter)
    end_marker = "_el = time.perf_counter() - t0"
    end_idx = src.find(end_marker, idx)
    if end_idx < 0:
        return src[idx : idx + 1200]
    return src[idx:end_idx]


# ---------------------------------------------------------------------------
# R379 Review #1 — 寫出失敗時 exception 被捕獲且 log
# ---------------------------------------------------------------------------


class TestR379_1_WriteFailureCaughtAndLogged(unittest.TestCase):
    """Review #1: When sidecar (or parquet) write fails, exception is caught and warning is logged."""

    def test_ch_write_block_has_try_except_and_warning_log(self):
        """ClickHouse path write block must use try/except and logger.warning with artifact failed message."""
        block = _get_run_pipeline_step3_ch_write_section(_TRAINER_SRC)
        self.assertIn("try:", block, "Write block should be inside try")
        self.assertIn("except Exception", block, "Write block should catch Exception")
        self.assertIn(
            "Write canonical mapping artifact failed",
            block,
            "Must log 'Write canonical mapping artifact failed' on exception",
        )
        self.assertIn("logger.warning", block, "Must call logger.warning on write failure")


# ---------------------------------------------------------------------------
# R379 Review #2 — 空 map 不寫出
# ---------------------------------------------------------------------------


class TestR379_2_EmptyMapNotWritten(unittest.TestCase):
    """Review #2: When canonical_map is empty (e.g. ClickHouse failed), do not write parquet/sidecar."""

    def test_ch_write_guarded_by_not_canonical_map_empty(self):
        """ClickHouse path write must be guarded by not canonical_map.empty."""
        block = _get_run_pipeline_step3_ch_write_section(_TRAINER_SRC)
        self.assertIn(
            "canonical_map.empty",
            block,
            "Write must be guarded by canonical_map.empty check so empty map is not written",
        )
        self.assertIn(
            "not canonical_map.empty",
            block,
            "Write condition must require non-empty map",
        )


# ---------------------------------------------------------------------------
# R379 Review #3 — train_end 序列化後可被 pd.Timestamp 解析
# ---------------------------------------------------------------------------


class TestR379_3_CutoffDtmParseableByScorer(unittest.TestCase):
    """Review #3: Sidecar cutoff_dtm format must be parseable by pd.Timestamp (scorer/loader contract)."""

    def test_date_isoformat_roundtrip_parseable(self):
        """datetime.date produces string that pd.Timestamp can parse."""
        d = date(2025, 2, 1)
        s = d.isoformat()
        ts = pd.Timestamp(s)
        self.assertEqual(ts.date(), d)

    def test_pd_timestamp_naive_isoformat_roundtrip(self):
        """pd.Timestamp (naive) isoformat is parseable by pd.Timestamp."""
        t = pd.Timestamp("2025-02-01 12:00:00")
        s = t.isoformat()
        ts = pd.Timestamp(s)
        self.assertEqual(ts, t)

    def test_pd_timestamp_tz_isoformat_roundtrip(self):
        """pd.Timestamp (tz-aware) isoformat is parseable (may normalize tz)."""
        t = pd.Timestamp("2025-02-01 12:00:00", tz="Asia/Hong_Kong")
        s = t.isoformat()
        ts = pd.Timestamp(s)
        self.assertIsNotNone(ts)
        # Same instant
        self.assertEqual(ts.tz_localize(None) if ts.tz else ts, t.tz_localize(None) if t.tz else t)

    def test_sidecar_cutoff_dtm_key_and_parseable(self):
        """Sidecar must have cutoff_dtm key and value parseable by pd.Timestamp."""
        sidecar = {"cutoff_dtm": "2025-02-01T00:00:00", "dummy_player_ids": [1, 2]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(sidecar, f, indent=0)
            path = Path(f.name)
        try:
            with open(path, encoding="utf-8") as _f:
                loaded = json.load(_f)
            self.assertIn("cutoff_dtm", loaded)
            ts = pd.Timestamp(loaded["cutoff_dtm"])
            self.assertIsNotNone(ts)
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# R379 Review #4 — dummy_player_ids JSON 可序列化／roundtrip
# ---------------------------------------------------------------------------


class TestR379_4_DummyPlayerIdsJsonRoundtrip(unittest.TestCase):
    """Review #4: dummy_player_ids in sidecar must be JSON-serializable and roundtrip as list of int."""

    def test_sidecar_dummy_player_ids_list_int_roundtrip(self):
        """Sidecar with dummy_player_ids as list of Python int round-trips."""
        sidecar = {"cutoff_dtm": "2025-02-01T00:00:00", "dummy_player_ids": [1, 2]}
        s = json.dumps(sidecar)
        loaded = json.loads(s)
        self.assertEqual(loaded["dummy_player_ids"], [1, 2])

    def test_sidecar_dummy_player_ids_from_numpy_int_serializable_as_int(self):
        """When dummy_player_ids contains numpy ints, list(int(x) for x in ...) is JSON-serializable."""
        dummy_set = {np.int64(1), np.int64(2)}
        as_list = [int(x) for x in dummy_set]
        sidecar = {"cutoff_dtm": "2025-02-01T00:00:00", "dummy_player_ids": sorted(as_list)}
        s = json.dumps(sidecar)
        loaded = json.loads(s)
        self.assertEqual(set(loaded["dummy_player_ids"]), {1, 2})


# ---------------------------------------------------------------------------
# R379 Review #5 — 寫出在 try 內（權限失敗時不 crash）
# ---------------------------------------------------------------------------


class TestR379_5_WriteInsideTry(unittest.TestCase):
    """Review #5: Parquet and sidecar write must be inside try so permission/IO errors are caught."""

    def test_ch_write_to_parquet_and_open_inside_try(self):
        """to_parquet and open (sidecar) must be inside the same try block."""
        block = _get_run_pipeline_step3_ch_write_section(_TRAINER_SRC)
        self.assertIn("to_parquet", block, "Must call to_parquet")
        self.assertIn("open(", block, "Must open sidecar file")
        # Try must wrap both
        try_start = block.find("try:")
        parquet_idx = block.find("to_parquet")
        open_idx = block.find("open(")
        except_idx = block.find("except Exception")
        self.assertGreater(try_start, -1)
        self.assertGreater(except_idx, try_start)
        self.assertGreater(parquet_idx, try_start)
        self.assertLess(parquet_idx, except_idx)
        self.assertGreater(open_idx, try_start)
        self.assertLess(open_idx, except_idx)


# ---------------------------------------------------------------------------
# R379 Review #6 — Sidecar 格式與 scorer 載入契約一致
# ---------------------------------------------------------------------------


class TestR379_6_SidecarFormatContract(unittest.TestCase):
    """Review #6: Sidecar format must match what scorer/trainer load logic expects."""

    def test_sidecar_has_required_keys(self):
        """Sidecar must have cutoff_dtm and dummy_player_ids (scorer/trainer load contract)."""
        # Scorer/trainer load: _sidecar.get("cutoff_dtm"), _sidecar.get("dummy_player_ids", [])
        required = {"cutoff_dtm", "dummy_player_ids"}
        sidecar = {"cutoff_dtm": "2025-02-01T00:00:00", "dummy_player_ids": []}
        self.assertTrue(required.issubset(sidecar.keys()), "Sidecar must have cutoff_dtm and dummy_player_ids")

    def test_loaded_sidecar_cutoff_parseable_for_scorer_condition(self):
        """Loaded sidecar cutoff_dtm must be parseable for scorer condition (cutoff_naive >= now_naive)."""
        sidecar = {"cutoff_dtm": "2030-01-01T00:00:00", "dummy_player_ids": [1]}
        cutoff_ts = pd.Timestamp(sidecar["cutoff_dtm"])
        now_ts = pd.Timestamp("2025-01-01")
        self.assertGreaterEqual(cutoff_ts.replace(tzinfo=None) if cutoff_ts.tz else cutoff_ts, now_ts)


if __name__ == "__main__":
    unittest.main()
