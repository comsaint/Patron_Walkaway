"""
STATUS.md —「Code Review（高可靠性覆核）：Phase 2 剩餘項」（2026-03-22）所列風險之 MRE／契約測試。

**僅新增／變更 tests**；不改 production。若修正 production（例如 lookback 上限、CLI 空路徑擋下），請同步調整本檔預期。

鏡像函式 `_baseline_get_with_rated_fallback` 須與
`investigations/test_vs_production/checks/run_r1_r6_analysis.py` 同名函式保持行為一致。

執行方式見 `.cursor/plans/STATUS.md` 同日期「MRE／契約測試落地」。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _baseline_get_with_rated_fallback(data: Dict[str, Any], key: str) -> Any:
    """Mirror of run_r1_r6_analysis._baseline_get_with_rated_fallback (keep in sync)."""
    v = data.get(key)
    if v is not None:
        return v
    rated = data.get("rated")
    if not isinstance(rated, dict):
        return None
    v2 = rated.get(key)
    if v2 is not None:
        return v2
    m = rated.get("metrics")
    if isinstance(m, dict):
        v3 = m.get(key)
        if v3 is not None:
            return v3
    return None


class TestPhase2ReviewerLookbackTimedeltaOverflowMre(unittest.TestCase):
    """Reviewer §1：極大 SCORER_LOOKBACK_HOURS 由 config cap，timedelta 不 OverflowError。"""

    def test_fresh_interpreter_huge_lookback_capped_safe_for_timedelta(self) -> None:
        code = r"""
import os
import sys
os.environ["SCORER_LOOKBACK_HOURS"] = "1000000000"
import trainer.core.config as c
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
h = c.SCORER_LOOKBACK_HOURS
if h > c.SCORER_LOOKBACK_HOURS_MAX:
    sys.exit(2)
now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
try:
    now - timedelta(hours=h)
except OverflowError:
    sys.exit(1)
sys.exit(0)
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=(proc.stdout + proc.stderr)[:4000],
        )


class TestPhase2ReviewerLookbackTruncateMre(unittest.TestCase):
    """Reviewer §2：int(float(...)) 截斷小數。"""

    def test_decimal_string_truncates_toward_zero(self) -> None:
        code = r"""
import os
os.environ["SCORER_LOOKBACK_HOURS"] = "7.9"
import trainer.core.config as c
print(c.SCORER_LOOKBACK_HOURS)
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "7")


class TestPhase2ReviewerRuntimeTtlParseFailureMre(unittest.TestCase):
    """Reviewer §3：TTL 開啟且 updated_at 不可解析 → 退回 bundle。"""

    def test_garbage_updated_at_with_ttl_falls_back_to_bundle(self) -> None:
        import trainer.serving.scorer as sc
        from trainer.serving.scorer import (
            ensure_runtime_rated_threshold_schema,
            read_effective_runtime_rated_threshold,
            upsert_runtime_rated_threshold,
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            conn = sqlite3.connect(str(db_path))
            try:
                ensure_runtime_rated_threshold_schema(conn)
                upsert_runtime_rated_threshold(conn, 0.71, source="mre")
                conn.commit()
                conn.execute(
                    "UPDATE runtime_rated_threshold SET updated_at = ? WHERE id = 1",
                    ("not-a-valid-timestamp",),
                )
                conn.commit()
                # scorer 綁定 `trainer.config`（非單獨 patch core 即可生效）
                with patch.object(sc.config, "RUNTIME_THRESHOLD_MAX_AGE_HOURS", 1.0):
                    got = read_effective_runtime_rated_threshold(conn, 0.55)
                self.assertAlmostEqual(got, 0.55)
            finally:
                conn.close()


class TestPhase2ReviewerRuntimeEmptyUpdatedAtSkipsTtlMre(unittest.TestCase):
    """Reviewer §4（修正後）：TTL 開啟且 updated_at 空白 → 退回 bundle。"""

    def test_empty_updated_at_with_ttl_falls_back_to_bundle(self) -> None:
        import trainer.serving.scorer as sc
        from trainer.serving.scorer import (
            ensure_runtime_rated_threshold_schema,
            read_effective_runtime_rated_threshold,
            upsert_runtime_rated_threshold,
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            conn = sqlite3.connect(str(db_path))
            try:
                ensure_runtime_rated_threshold_schema(conn)
                upsert_runtime_rated_threshold(conn, 0.71, source="mre")
                conn.commit()
                conn.execute(
                    "UPDATE runtime_rated_threshold SET updated_at = ? WHERE id = 1",
                    ("",),
                )
                conn.commit()
                with patch.object(sc.config, "RUNTIME_THRESHOLD_MAX_AGE_HOURS", 1e-6):
                    got = read_effective_runtime_rated_threshold(conn, 0.55)
                self.assertAlmostEqual(got, 0.55)
            finally:
                conn.close()


class TestPhase2ReviewerRuntimeStaleRowMre(unittest.TestCase):
    """TTL：過期列應退回 bundle（補強 reviewer 語意）。"""

    def test_stale_updated_at_with_ttl_falls_back_to_bundle(self) -> None:
        import trainer.serving.scorer as sc
        from trainer.serving.scorer import (
            ensure_runtime_rated_threshold_schema,
            read_effective_runtime_rated_threshold,
            upsert_runtime_rated_threshold,
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            conn = sqlite3.connect(str(db_path))
            try:
                ensure_runtime_rated_threshold_schema(conn)
                upsert_runtime_rated_threshold(conn, 0.71, source="mre")
                conn.commit()
                conn.execute(
                    "UPDATE runtime_rated_threshold SET updated_at = ? WHERE id = 1",
                    ("1999-01-01T00:00:00+08:00",),
                )
                conn.commit()
                with patch.object(sc.config, "RUNTIME_THRESHOLD_MAX_AGE_HOURS", 1.0):
                    got = read_effective_runtime_rated_threshold(conn, 0.55)
                self.assertAlmostEqual(got, 0.55)
            finally:
                conn.close()


class TestPhase2ReviewerUpsertOutOfRangeStoredMre(unittest.TestCase):
    """Reviewer §5：upsert 拒絕區外值；DB 內手動汙染仍由讀取端退回 bundle。"""

    def test_upsert_out_of_range_raises(self) -> None:
        from trainer.serving.scorer import (
            ensure_runtime_rated_threshold_schema,
            upsert_runtime_rated_threshold,
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            conn = sqlite3.connect(str(db_path))
            try:
                ensure_runtime_rated_threshold_schema(conn)
                with self.assertRaises(ValueError):
                    upsert_runtime_rated_threshold(conn, 1.5, source="mre_bad_writer")
            finally:
                conn.close()

    def test_out_of_range_value_in_db_read_returns_bundle(self) -> None:
        from trainer.serving.scorer import (
            ensure_runtime_rated_threshold_schema,
            read_effective_runtime_rated_threshold,
            upsert_runtime_rated_threshold,
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            conn = sqlite3.connect(str(db_path))
            try:
                ensure_runtime_rated_threshold_schema(conn)
                upsert_runtime_rated_threshold(conn, 0.61, source="mre")
                conn.commit()
                conn.execute(
                    "UPDATE runtime_rated_threshold SET rated_threshold = ? WHERE id = 1",
                    (1.5,),
                )
                conn.commit()
                self.assertAlmostEqual(
                    read_effective_runtime_rated_threshold(conn, 0.55), 0.55
                )
            finally:
                conn.close()


class TestPhase2ReviewerCalibrateEmptyPredictionLogPathMre(unittest.TestCase):
    """Reviewer §6：PREDICTION_LOG_DB_PATH 空字串時 --init-schema 明確退出（非零）。"""

    def test_init_schema_with_empty_prediction_log_path_exits_nonzero(self) -> None:
        env = {**os.environ, "PREDICTION_LOG_DB_PATH": ""}
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "trainer.scripts.calibrate_threshold_from_prediction_log",
                "--init-schema",
            ],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        out = proc.stderr + proc.stdout
        self.assertTrue(
            "empty" in out.lower() or "PREDICTION_LOG_DB_PATH" in out,
            msg=out[:2000],
        )


class TestPhase2ReviewerBaselineTopLevelFalsyMre(unittest.TestCase):
    """Reviewer §7（精確 MRE）：`v is not None` 語意 — 頂層 0.0 不遞補 metrics 較大值。"""

    def test_top_level_zero_does_not_fallback_to_metrics(self) -> None:
        data = {
            "test_precision_at_recall_0.01": 0.0,
            "rated": {
                "metrics": {"test_precision_at_recall_0.01": 0.42},
            },
        }
        v = _baseline_get_with_rated_fallback(data, "test_precision_at_recall_0.01")
        self.assertEqual(v, 0.0)

    def test_explicit_none_top_level_falls_through_to_metrics(self) -> None:
        """None 使第一分支不成立，會讀 rated.metrics（與「鍵缺失」同路徑）。"""
        data = {
            "test_precision_at_recall_0.01": None,
            "rated": {
                "metrics": {"test_precision_at_recall_0.01": 0.42},
            },
        }
        v = _baseline_get_with_rated_fallback(data, "test_precision_at_recall_0.01")
        self.assertEqual(v, 0.42)


class TestPhase2ReviewerCalibrateNoEnvGateMre(unittest.TestCase):
    """Reviewer §8：目前無寫入閘門 env；合法阈應可寫入臨時 state DB。"""

    def test_set_runtime_threshold_succeeds_without_extra_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st = Path(td) / "state.db"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "trainer.scripts.calibrate_threshold_from_prediction_log",
                    "--state-db",
                    str(st),
                    "--set-runtime-threshold",
                    "0.62",
                    "--source",
                    "mre_no_gate",
                ],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("0.62", proc.stdout)


if __name__ == "__main__":
    unittest.main()
