"""
Review-risks tests for one-line automation changes in run_r1_r6_analysis.py.

Only tests are added. No production code changes.
"""

from __future__ import annotations

import csv
import importlib.util
import os
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
SCRIPT_PATH = (
    REPO_ROOT
    / "investigations"
    / "test_vs_production"
    / "checks"
    / "run_r1_r6_analysis.py"
)
r1r6 = _load_module("run_r1_r6_one_line_mod", SCRIPT_PATH)


def _create_prediction_log_db(path: Path, rows: list[tuple[str, str, str, str, float, int, int]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE prediction_log (
              prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
              scored_at TEXT NOT NULL,
              bet_id TEXT,
              player_id TEXT,
              canonical_id TEXT,
              score REAL,
              is_alert INTEGER,
              is_rated_obs INTEGER
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO prediction_log (
              scored_at, bet_id, player_id, canonical_id, score, is_alert, is_rated_obs
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class TestR1R6OneLineAutomationReviewRisks:
    def test_default_snapshot_paths_should_not_collide_within_same_day(self):
        start_ts = "2026-03-19T00:00:00+08:00"
        end_ts = "2026-03-20T00:00:00+08:00"
        sample1, labels1 = r1r6._default_snapshot_paths(start_ts, end_ts)
        sample2, labels2 = r1r6._default_snapshot_paths(start_ts, end_ts)
        assert sample1 != sample2
        assert labels1 != labels2

    def test_run_all_should_fail_without_overwrite_when_output_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            db = t / "prediction_log.db"
            _create_prediction_log_db(
                db,
                [("2026-03-19T20:00:00+08:00", "b1", "p1", "c1", 0.2, 0, 1)],
            )
            out_csv = t / "sample.csv"
            out_csv.write_text("bet_id,score,scored_at,is_alert,is_rated_obs,bin_id\nb0,0.1,x,0,1,0\n", encoding="utf-8")
            # Expectation for target behavior: fail-fast instead of silently overwrite.
            with pytest.raises((FileExistsError, ValueError), match="overwrite|exists"):
                r1r6.run_sample_mode(
                    db_path=str(db),
                    start_ts="2026-03-19T00:00:00+08:00",
                    end_ts="2026-03-20T00:00:00+08:00",
                    sample_size=10,
                    bins=2,
                    seed=42,
                    out_csv=out_csv,
                )

    def test_autolabel_summary_reports_duplicate_bet_ids(self, monkeypatch: pytest.MonkeyPatch):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            db = t / "prediction_log.db"
            sample_csv = t / "sample.csv"
            labels_csv = t / "labels.csv"
            _create_prediction_log_db(
                db,
                [("2026-03-19T20:00:00+08:00", "b1", "p1", "c1", 0.1, 0, 1)],
            )
            with sample_csv.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["bet_id", "score", "scored_at", "is_alert", "is_rated_obs", "bin_id"])
                w.writerow(["b1", "0.1", "2026-03-19T20:00:00+08:00", "0", "1", "0"])
                w.writerow(["b1", "0.1", "2026-03-19T20:00:00+08:00", "0", "1", "0"])

            class _DummyCH:
                def query_df(self, *_args, **_kwargs):
                    import pandas as pd  # type: ignore[import-untyped]

                    return pd.DataFrame(
                        {
                            "bet_id": ["b1"],
                            "player_id": ["p1"],
                            "payout_complete_dtm": ["2026-03-19T20:00:00"],
                        }
                    )

            def _dummy_compute_labels(*_args, **_kwargs):
                import pandas as pd  # type: ignore[import-untyped]

                return pd.DataFrame({"bet_id": ["b1"], "label": [1], "censored": [0]})

            monkeypatch.setattr(r1r6, "get_clickhouse_client", lambda: _DummyCH())
            monkeypatch.setattr(r1r6, "compute_labels", _dummy_compute_labels)

            out = r1r6.run_autolabel_mode(
                db_path=str(db),
                start_ts="2026-03-19T00:00:00+08:00",
                end_ts="2026-03-20T00:00:00+08:00",
                sample_csv=sample_csv,
                out_labels_csv=labels_csv,
                player_chunk_size=100,
            )
            summary = out.get("summary", {})
            assert "n_duplicate_bet_id" in summary
            assert int(summary["n_duplicate_bet_id"]) >= 1

    def test_resolve_pred_db_path_can_prefer_env_file_over_process_env(self, monkeypatch: pytest.MonkeyPatch):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            env_file = t / ".env"
            env_file.write_text("PREDICTION_LOG_DB_PATH=/from/env/file.db\n", encoding="utf-8")
            monkeypatch.setenv("PREDICTION_LOG_DB_PATH", "/from/process/env.db")
            resolved, _env_used = r1r6._resolve_pred_db_path(str(env_file), "")
            assert resolved == "/from/env/file.db"

    def test_autolabel_rejects_invalid_table_identifier_from_config(self, monkeypatch: pytest.MonkeyPatch):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            db = t / "prediction_log.db"
            sample_csv = t / "sample.csv"
            labels_csv = t / "labels.csv"
            _create_prediction_log_db(
                db,
                [("2026-03-19T20:00:00+08:00", "b1", "p1", "c1", 0.1, 0, 1)],
            )
            sample_csv.write_text(
                "bet_id,score,scored_at,is_alert,is_rated_obs,bin_id\nb1,0.1,2026-03-19T20:00:00+08:00,0,1,0\n",
                encoding="utf-8",
            )

            class _DummyCH:
                def query_df(self, *_args, **_kwargs):
                    import pandas as pd  # type: ignore[import-untyped]

                    return pd.DataFrame(
                        {
                            "bet_id": ["b1"],
                            "player_id": ["p1"],
                            "payout_complete_dtm": ["2026-03-19T20:00:00"],
                        }
                    )

            def _dummy_compute_labels(*_args, **_kwargs):
                import pandas as pd  # type: ignore[import-untyped]

                return pd.DataFrame({"bet_id": ["b1"], "label": [1], "censored": [0]})

            monkeypatch.setattr(r1r6, "get_clickhouse_client", lambda: _DummyCH())
            monkeypatch.setattr(r1r6, "compute_labels", _dummy_compute_labels)
            monkeypatch.setattr(r1r6.config, "SOURCE_DB", "bad-db;DROP TABLE x")
            monkeypatch.setattr(r1r6.config, "TBET", "tb")

            with pytest.raises(ValueError, match="identifier|invalid|SOURCE_DB|TBET"):
                r1r6.run_autolabel_mode(
                    db_path=str(db),
                    start_ts="2026-03-19T00:00:00+08:00",
                    end_ts="2026-03-20T00:00:00+08:00",
                    sample_csv=sample_csv,
                    out_labels_csv=labels_csv,
                    player_chunk_size=100,
                )


def test_review_risks_file_loads() -> None:
    # Minimal non-xfail smoke to ensure file is collected.
    assert SCRIPT_PATH.exists()
    assert os.path.isfile(SCRIPT_PATH)
