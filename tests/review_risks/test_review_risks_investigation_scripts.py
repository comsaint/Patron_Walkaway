"""
Review-risks tests for investigation helper scripts.

Scope:
- investigations/test_vs_production/checks/preflight_check.py
- investigations/test_vs_production/checks/investigate_r2_window.py

Only tests are added (no production changes).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


REPO_ROOT = Path(__file__).resolve().parents[2]
PRECHECK_PATH = REPO_ROOT / "investigations" / "test_vs_production" / "checks" / "preflight_check.py"
R2_PATH = REPO_ROOT / "investigations" / "test_vs_production" / "checks" / "investigate_r2_window.py"

precheck = _load_module("preflight_check_mod", PRECHECK_PATH)
r2 = _load_module("investigate_r2_window_mod", R2_PATH)


class TestInvestigationScriptReviewRisks:
    def test_r2_timezone_order_should_be_datetime_aware(self, monkeypatch: pytest.MonkeyPatch):
        """
        Risk #2: start/end comparison should be datetime-based, not lexical string-based.

        Example:
        - start: 2026-03-19T00:00:00+09:00
        - end:   2026-03-19T00:00:00+08:00
        Chronologically, start is earlier than end.
        Lexicographically, "+09:00" > "+08:00" and can be misclassified.
        """
        start_dt = r2._parse_iso_ts("2026-03-19T00:00:00+09:00")
        end_dt = r2._parse_iso_ts("2026-03-19T00:00:00+08:00")
        assert start_dt < end_dt

    def test_r2_only_start_should_error(self, monkeypatch: pytest.MonkeyPatch):
        """
        Risk #1: providing only --start-ts should error clearly (instead of silent fallback).
        """
        monkeypatch.setattr(
            "sys.argv",
            [
                "investigate_r2_window.py",
                "--start-ts",
                "2026-03-19T00:00:00+08:00",
            ],
        )
        rc = r2.main()
        assert rc == 2

    def test_r2_only_end_should_error(self, monkeypatch: pytest.MonkeyPatch):
        """
        Risk #1: providing only --end-ts should error clearly (instead of silent fallback).
        """
        monkeypatch.setattr(
            "sys.argv",
            [
                "investigate_r2_window.py",
                "--end-ts",
                "2026-03-20T00:00:00+08:00",
            ],
        )
        rc = r2.main()
        assert rc == 2

    def test_preflight_env_fallback_should_continue_when_first_missing_required_keys(self):
        """
        Risk #3: if first candidate env lacks required keys, loader should continue to next candidate.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env1 = root / "first.env"
            env2 = root / "second.env"
            env1.write_text("FOO=bar\n", encoding="utf-8")
            env2.write_text(
                "PREDICTION_LOG_DB_PATH=/x/prediction_log.db\nDATA_DIR=/x/data\n",
                encoding="utf-8",
            )
            out = precheck.load_env_candidates((env1, env2))
            assert out["used_file"] == str(env2)
            assert out["vars"].get("PREDICTION_LOG_DB_PATH") == "/x/prediction_log.db"
            assert out["vars"].get("DATA_DIR") == "/x/data"

    def test_preflight_sqlite_read_should_use_readonly_or_timeout_hardening(self):
        """
        Risk #4: preflight DB read should use readonly URI and timeout/retry for WAL lock robustness.
        """
        src = PRECHECK_PATH.read_text(encoding="utf-8")
        assert "mode=ro" in src or "timeout=" in src

    def test_r2_script_minimal_smoke_with_temp_dbs(self):
        """
        Minimal reproducible success path for current implementation:
        - prediction_log DB exists and has required table
        - state DB exists and has alerts table
        """
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            pred_db = t / "prediction_log.db"
            state_db = t / "state.db"

            conn = sqlite3.connect(pred_db)
            conn.execute(
                """
                CREATE TABLE prediction_log (
                  prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  scored_at TEXT,
                  bet_id TEXT,
                  score REAL,
                  is_alert INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO prediction_log (scored_at, bet_id, score, is_alert) VALUES (?, ?, ?, ?)",
                ("2026-03-19T12:00:00+08:00", "b1", 0.2, 0),
            )
            conn.commit()
            conn.close()

            conn = sqlite3.connect(state_db)
            conn.execute("CREATE TABLE alerts (scored_at TEXT)")
            conn.execute("INSERT INTO alerts (scored_at) VALUES (?)", ("2026-03-19T12:00:00+08:00",))
            conn.commit()
            conn.close()

            import sys

            argv_bak = list(sys.argv)
            try:
                sys.argv = [
                    "investigate_r2_window.py",
                    "--start-ts",
                    "2026-03-19T00:00:00+08:00",
                    "--end-ts",
                    "2026-03-20T00:00:00+08:00",
                    "--pred-db-path",
                    str(pred_db),
                    "--state-db-path",
                    str(state_db),
                ]
                rc = r2.main()
            finally:
                sys.argv = argv_bak
            assert rc == 0

