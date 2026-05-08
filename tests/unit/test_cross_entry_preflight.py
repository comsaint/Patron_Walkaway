"""WS4 v2 / WS5: cross-entry data preflight (local bridge vs ClickHouse) + gate contracts."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pandas as pd

from trainer.training.cross_entry_preflight import run_cross_entry_data_preflight

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_repo_text(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


class TestCrossEntryPreflight(unittest.TestCase):
    def test_local_delegates_to_bridge_ensure(self) -> None:
        log = logging.getLogger("test_ws4")
        with patch(
            "trainer.training.local_bridge_preflight.ensure_local_bridge_ready_for_training"
        ) as m_ensure:
            run_cross_entry_data_preflight(
                entry="trainer",
                use_local_parquet=True,
                logger=log,
            )
        m_ensure.assert_called_once_with(logger=log)

    def test_clickhouse_path_calls_query_df(self) -> None:
        log = logging.getLogger("test_ws4b")
        with patch("trainer.core.db_conn.query_df", return_value=MagicMock()) as m_core:
            run_cross_entry_data_preflight(
                entry="scorer",
                use_local_parquet=False,
                logger=log,
            )
        self.assertGreaterEqual(m_core.call_count, 1)
        m_core.assert_any_call("SELECT 1 AS _ok")

    def test_clickhouse_failure_wraps_runtimeerror(self) -> None:
        log = logging.getLogger("test_ws4c")
        with patch(
            "trainer.core.db_conn.query_df",
            side_effect=OSError("boom"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                run_cross_entry_data_preflight(
                    entry="validator",
                    use_local_parquet=False,
                    logger=log,
                )
        self.assertIn("DataPreflight[validator]", str(ctx.exception))
        self.assertIn("ClickHouse check failed", str(ctx.exception))
        self.assertIn("CH_HOST", str(ctx.exception))


class TestProductionFreshnessPreflight(unittest.TestCase):
    """Issue #19: bundle global_window.end vs warehouse max(__etl_insert_Dtm)."""

    def test_strict_requires_model_dir(self) -> None:
        log = logging.getLogger("test_fresh_strict")
        prev_md = os.environ.pop("MODEL_DIR", None)
        try:
            with patch.dict(os.environ, {"PRODUCTION_FRESHNESS_STRICT": "1"}, clear=False):
                with patch("trainer.core.db_conn.query_df", return_value=MagicMock()):
                    with self.assertRaises(RuntimeError) as ctx:
                        run_cross_entry_data_preflight(
                            entry="scorer",
                            use_local_parquet=False,
                            logger=log,
                        )
            self.assertIn("MODEL_DIR", str(ctx.exception))
        finally:
            if prev_md is not None:
                os.environ["MODEL_DIR"] = prev_md

    def test_freshness_ok_when_etl_after_train_end(self) -> None:
        log = logging.getLogger("test_fresh_ok")
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "model.pkl").write_bytes(b"x")
            meta = {"global_window": {"end": "2025-06-01T00:00:00"}}
            (bundle / "model_metadata.json").write_text(
                json.dumps(meta),
                encoding="utf-8",
            )

            def _side_effect(sql: str) -> object:
                if "SELECT 1" in sql:
                    return MagicMock()
                if "max(__etl_insert_Dtm)" in sql:
                    return pd.DataFrame({"mx": [pd.Timestamp("2099-01-01")]})
                raise AssertionError(sql)

            with patch.dict(
                os.environ,
                {"MODEL_DIR": str(bundle), "PRODUCTION_FRESHNESS_STRICT": "0"},
                clear=False,
            ):
                with patch("trainer.core.db_conn.query_df", side_effect=_side_effect):
                    run_cross_entry_data_preflight(
                        entry="scorer",
                        use_local_parquet=False,
                        logger=log,
                    )

    def test_freshness_strict_raises_when_etl_before_train_end(self) -> None:
        log = logging.getLogger("test_fresh_bad")
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "model.pkl").write_bytes(b"x")
            meta = {"global_window": {"end": "2025-06-01T00:00:00"}}
            (bundle / "model_metadata.json").write_text(
                json.dumps(meta),
                encoding="utf-8",
            )

            def _side_effect(sql: str) -> object:
                if "SELECT 1" in sql:
                    return MagicMock()
                if "max(__etl_insert_Dtm)" in sql:
                    return pd.DataFrame({"mx": [pd.Timestamp("2020-01-01")]})
                raise AssertionError(sql)

            with patch.dict(
                os.environ,
                {"MODEL_DIR": str(bundle), "PRODUCTION_FRESHNESS_STRICT": "1"},
                clear=False,
            ):
                with patch("trainer.core.db_conn.query_df", side_effect=_side_effect):
                    with self.assertRaises(RuntimeError) as ctx:
                        run_cross_entry_data_preflight(
                            entry="scorer",
                            use_local_parquet=False,
                            logger=log,
                        )
        self.assertIn("PRODUCTION_FRESHNESS_STRICT", str(ctx.exception))


class TestWS5EntrypointPreflightContract(unittest.TestCase):
    """WS5: source-level guard so trainer/backtester/serving keep calling cross_entry hook."""

    def test_trainer_run_pipeline_includes_trainer_entry_preflight(self) -> None:
        src = _read_repo_text("trainer/training/trainer.py")
        self.assertIn("run_cross_entry_data_preflight", src)
        self.assertIn('entry="trainer"', src)

    def test_backtester_main_includes_backtester_entry_preflight(self) -> None:
        src = _read_repo_text("trainer/training/backtester.py")
        self.assertIn("run_cross_entry_data_preflight", src)
        self.assertIn('entry="backtester"', src)

    def test_serving_scorer_main_includes_scorer_entry_preflight(self) -> None:
        src = _read_repo_text("trainer/serving/scorer.py")
        self.assertIn("run_cross_entry_data_preflight", src)
        self.assertIn('entry="scorer"', src)

    def test_serving_validator_main_includes_validator_entry_preflight(self) -> None:
        src = _read_repo_text("trainer/serving/validator.py")
        self.assertIn("run_cross_entry_data_preflight", src)
        self.assertIn('entry="validator"', src)

    def test_cli_reexports_document_ws4_preflight_location(self) -> None:
        self.assertIn("WS4 v2", _read_repo_text("trainer/scorer.py"))
        self.assertIn("WS4 v2", _read_repo_text("trainer/validator.py"))


class TestWS5ResourceGuardMessages(unittest.TestCase):
    """WS5: keep high-RAM / long-runtime and ClickHouse failure hints grep-friendly."""

    def test_mvp_full_subprocess_start_logs_high_ram_hint(self) -> None:
        from trainer.training import local_bridge_preflight as lbp

        log = logging.getLogger("test_ws5_mvp")
        log.setLevel(logging.WARNING)
        with patch(
            "trainer.training.local_bridge_preflight.subprocess.run",
            return_value=CompletedProcess([], 0),
        ):
            with self.assertLogs(log, level="WARNING") as cm:
                lbp._run_full_mvp_with_bridge_emit_subprocess(
                    repo_root=_REPO_ROOT,
                    logger=log,
                )
        joined = " ".join(cm.output)
        self.assertIn("high RAM", joined)
        self.assertIn("long runtime", joined)


if __name__ == "__main__":
    unittest.main()
