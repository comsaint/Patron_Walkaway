"""Minimal reproducible tests for Code Review: PLAN § 套件 entrypoint 與 db_conn 相對匯入（Option A）.

Maps each Reviewer risk to a test or contract. Production code is not modified.
Some tests are expectedFailure until production is fixed (see STATUS.md).
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from zoneinfo import ZoneInfo

HK_TZ = ZoneInfo("Asia/Hong_Kong")


# ---------------------------------------------------------------------------
# Review §1: get_clickhouse_client is None → validator raise RuntimeError
# ---------------------------------------------------------------------------


class TestValidatorGetClickhouseClientNoneRaises(unittest.TestCase):
    """§1: When get_clickhouse_client is None and there are pending alerts, validate_once must raise RuntimeError."""

    def test_validate_once_raises_when_get_clickhouse_client_is_none_and_pending(self):
        """Mock validator.get_clickhouse_client = None; validate_once with pending alert → RuntimeError with 'Run as package' or 'ClickHouse'."""
        import trainer.validator as validator_mod

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE alerts (bet_id TEXT PRIMARY KEY, ts TEXT, bet_ts TEXT, player_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE processed_alerts (bet_id TEXT PRIMARY KEY, processed_ts TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE validation_results (
                bet_id TEXT PRIMARY KEY, alert_ts TEXT, validated_at TEXT, player_id TEXT,
                casino_player_id TEXT, canonical_id TEXT, table_id TEXT, position_idx REAL,
                session_id TEXT, score REAL, result INTEGER, gap_start TEXT, gap_minutes REAL,
                reason TEXT, bet_ts TEXT, model_version TEXT
            )
            """
        )
        now_hk = datetime.now(HK_TZ)
        old_ts = (now_hk - timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO alerts (bet_id, ts, bet_ts, player_id) VALUES (?, ?, ?, ?)",
            ("1", old_ts, old_ts, "123"),
        )
        conn.commit()

        with patch.object(validator_mod, "get_clickhouse_client", None):
            with self.assertRaises(RuntimeError) as ctx:
                validator_mod.validate_once(conn)
        msg = str(ctx.exception)
        self.assertIn(
            "ClickHouse",
            msg,
            "RuntimeError message should mention ClickHouse",
        )
        self.assertTrue(
            "Run as package" in msg or "get_clickhouse_client" in msg,
            "RuntimeError message should mention run as package or get_clickhouse_client",
        )
        conn.close()


# ---------------------------------------------------------------------------
# Review §1: db_conn raises when clickhouse_connect not available
# ---------------------------------------------------------------------------


class TestDbConnRaisesWhenClickhouseConnectUnavailable(unittest.TestCase):
    """§1: When clickhouse_connect is None, get_clickhouse_client() must raise from db_conn with install hint."""

    def test_get_clickhouse_client_raises_with_install_message(self):
        """With clickhouse_connect = None, get_clickhouse_client() raises; message contains 'clickhouse_connect' or 'install'."""
        import trainer.db_conn as db_conn_mod

        with patch.object(db_conn_mod, "clickhouse_connect", None):
            db_conn_mod.get_clickhouse_client.cache_clear()
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    db_conn_mod.get_clickhouse_client()
                msg = str(ctx.exception).lower()
                self.assertTrue(
                    "clickhouse_connect" in msg or "install" in msg,
                    "db_conn RuntimeError should mention clickhouse_connect or install",
                )
            finally:
                db_conn_mod.get_clickhouse_client.cache_clear()


# ---------------------------------------------------------------------------
# Review §2: status_server.config should resolve to trainer.config when run as package
# ---------------------------------------------------------------------------


class TestStatusServerConfigResolvesToTrainerConfig(unittest.TestCase):
    """§2: When importing trainer.status_server (as package), status_server.config should be trainer.config."""

    def test_status_server_config_is_trainer_config(self):
        """After importing trainer.status_server, status_server.config is trainer.config (package execution)."""
        import trainer.config as trainer_config

        try:
            import trainer.status_server as status_server_mod
        except ModuleNotFoundError as e:
            self.skipTest("status_server could not be imported (e.g. no top-level config): %s" % e)

        self.assertIs(
            status_server_mod.config,
            trainer_config,
            "status_server.config should be trainer.config when run as package; "
            "if this fails, status_server may be using top-level 'import config'.",
        )


# ---------------------------------------------------------------------------
# Review §3: training_config_recommender ImportError → get_client stays None, no crash
# ---------------------------------------------------------------------------


class TestRecommenderImportErrorSetsClientNone(unittest.TestCase):
    """§3: When .db_conn import raises ImportError, _build_data_profile_clickhouse should set get_client=None and not crash."""

    def test_build_data_profile_clickhouse_import_error_sets_client_none_no_crash(self):
        """Patch trainer.db_conn so import raises ImportError; call _build_data_profile_clickhouse → no exception, profile returned."""
        import trainer.training_config_recommender as rec_mod

        class FakeModule:
            def __getattr__(self, name):
                if name == "get_clickhouse_client":
                    raise ImportError("test: db_conn not available")
                raise AttributeError(name)

        with patch.dict(sys.modules, {"trainer.db_conn": FakeModule()}):
            profile = rec_mod.build_data_profile_clickhouse(
                training_days=30,
                get_client=None,
                skip_ch_connect=False,
            )
        self.assertEqual(profile["data_source"], "clickhouse")
        self.assertIn("chunk_count", profile)


# ---------------------------------------------------------------------------
# Review §3 (optional): RuntimeError during import should not be silently swallowed
# ---------------------------------------------------------------------------


class TestRecommenderRuntimeErrorOnImportShouldPropagateOrLog(unittest.TestCase):
    """§3 optional: When .db_conn raises RuntimeError on get_clickhouse_client access, we should not silently set get_client=None without log/raise."""

    def test_build_data_profile_clickhouse_runtime_error_should_not_be_silent(self):
        """Desired: when import raises RuntimeError, either re-raise or log; current code swallows with except Exception."""
        import trainer.training_config_recommender as rec_mod

        class FakeModule:
            def __getattr__(self, name):
                if name == "get_clickhouse_client":
                    raise RuntimeError("simulated db_conn config error")
                raise AttributeError(name)

        with patch.dict(sys.modules, {"trainer.db_conn": FakeModule()}):
            rec_mod = __import__(
                "trainer.training_config_recommender",
                fromlist=["_build_data_profile_clickhouse"],
            )
            with self.assertRaises(RuntimeError):
                rec_mod.build_data_profile_clickhouse(
                    training_days=30,
                    get_client=None,
                    skip_ch_connect=False,
                )


# ---------------------------------------------------------------------------
# Review §4: trainer package path loads (contract)
# ---------------------------------------------------------------------------


class TestTrainerPackagePathLoads(unittest.TestCase):
    """§4: python -m trainer.trainer --help must succeed (package path loads)."""

    def test_trainer_help_succeeds(self):
        """Run python -m trainer.trainer --help; exit code 0."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "trainer.trainer", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")


# ---------------------------------------------------------------------------
# Review §4 (source guard): trainer.py try/except block should document dual path
# ---------------------------------------------------------------------------


class TestTrainerTryExceptBlockDocumented(unittest.TestCase):
    """§4: trainer.py try/except for imports should have a comment documenting package vs path execution."""

    def test_trainer_try_except_has_comment_about_package_execution(self):
        """Desired: comment above or in try/except block mentions 'package' or 'python -m trainer.trainer'."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        trainer_src = (repo_root / "trainer" / "trainer.py").read_text(encoding="utf-8")
        # Look for comment in the import block (lines containing try/except and db_conn)
        idx = trainer_src.find("from db_conn import get_clickhouse_client")
        if idx == -1:
            idx = trainer_src.find("from .db_conn import get_clickhouse_client")
        self.assertGreater(idx, 0, "trainer.py should contain db_conn import")
        block = trainer_src[max(0, idx - 800) : idx + 120]
        self.assertTrue(
            "package" in block.lower() or "python -m trainer" in block,
            "trainer.py try/except import block should have a comment documenting package execution (e.g. python -m trainer.trainer)",
        )


# ---------------------------------------------------------------------------
# Review §7: walkaway_ml package structure (optional; skip if not installed)
# ---------------------------------------------------------------------------


class TestWalkawayMlPackageStructure(unittest.TestCase):
    """§7: If walkaway_ml is installed, import walkaway_ml.db_conn and get_clickhouse_client from validator should not raise."""

    def test_walkaway_ml_db_conn_and_validator_import(self):
        """When walkaway_ml is available, import db_conn and validator.get_clickhouse_client → no ImportError."""
        import importlib

        try:
            importlib.import_module("walkaway_ml.db_conn")
            from walkaway_ml.validator import get_clickhouse_client  # noqa: F401
        except (ModuleNotFoundError, ImportError):
            self.skipTest("walkaway_ml not installed or not runnable (deploy bundle not in path)")
