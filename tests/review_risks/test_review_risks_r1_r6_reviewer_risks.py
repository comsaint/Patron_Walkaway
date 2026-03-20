"""
Minimal reproducible tests for reviewer risks on run_r1_r6_analysis.py.

These tests document current behavior / foot-guns; they do not modify production code.
See STATUS.md for how to run them.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPO_ROOT
    / "investigations"
    / "test_vs_production"
    / "checks"
    / "run_r1_r6_analysis.py"
)


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


r1r6 = _load_module("run_r1_r6_analysis_reviewer_risks", SCRIPT_PATH)


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


def _create_alerts_db(path: Path, ts_values: list[str]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE alerts (ts TEXT NOT NULL)")
        conn.executemany("INSERT INTO alerts (ts) VALUES (?)", [(t,) for t in ts_values])
        conn.commit()
    finally:
        conn.close()


class TestReviewerR2LexicalTimestampWindow:
    """
    Reviewer #1: R2 uses the same [start_ts, end_ts) *string* bounds on scored_at vs alerts.ts.
    Lexical order != chronological when representations differ (e.g. Z vs +08:00).
    """

    def test_same_instant_different_string_forms_can_yield_pl_zero_alerts_one(self, tmp_path: Path) -> None:
        pred_db = tmp_path / "pred.db"
        state_db = tmp_path / "state.db"
        # 15:00 HKT on 2026-03-19 — chronologically inside [10:00, 18:00) HKT.
        utc_z = "2026-03-19T07:00:00Z"
        hk_same_instant = "2026-03-19T15:00:00+08:00"
        start_ts = "2026-03-19T10:00:00+08:00"
        end_ts = "2026-03-19T18:00:00+08:00"

        _create_prediction_log_db(
            pred_db,
            [(utc_z, "b1", "p1", "c1", 0.99, 1, 1)],
        )
        _create_alerts_db(state_db, [hk_same_instant])

        out = r1r6._cross_check_alerts_vs_prediction_log(
            str(pred_db), str(state_db), start_ts, end_ts
        )
        assert out["status"] == "ok"
        assert out["n_prediction_log_is_alert_rows"] == 0
        assert out["n_alerts_table_rows_ts_window"] == 1
        assert out["difference_pl_minus_alerts"] == -1


class TestReviewerTrainingMetricsNested:
    """Reviewer #2: baseline only reads top-level keys; nested trainer shapes stay invisible."""

    def test_nested_test_precision_not_surfaced_but_status_ok(self, tmp_path: Path) -> None:
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        payload = {
            "model_version": "v-test",
            "rated": {"metrics": {"test_precision_at_recall_0.01": 0.42, "threshold_at_recall_0.01": 0.77}},
        }
        (model_dir / "training_metrics.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        baseline = r1r6._load_training_metrics_baseline(model_dir)
        assert baseline["status"] == "ok"
        assert baseline["test_precision_at_recall_0.01"] is None
        assert baseline["threshold_at_recall_0.01"] is None


class TestReviewerUnifiedOverlap:
    """Reviewer #3: duplicate bet_id merges with alert branch overwriting below (silent)."""

    def test_overlap_counts_and_alert_branch_wins_for_metrics(self, tmp_path: Path) -> None:
        pred_db = tmp_path / "pred.db"
        _create_prediction_log_db(
            pred_db,
            [
                ("2026-03-19T12:00:00+08:00", "b_overlap", "p1", "c1", 0.5, 0, 1),
            ],
        )
        start_ts = "2026-03-19T00:00:00+08:00"
        end_ts = "2026-03-20T00:00:00+08:00"
        labels_below = {"b_overlap": (0, 0)}
        labels_alert = {"b_overlap": (1, 0)}

        unified = r1r6._build_unified_sample_evaluation(
            str(pred_db), start_ts, end_ts, labels_below, labels_alert, target_recall=0.01
        )
        assert unified["n_duplicate_bet_id_overlap"] == 1
        assert unified["n_rows_merged_unique"] == 1

        alert_only = r1r6._build_unified_sample_evaluation(
            str(pred_db), start_ts, end_ts, {}, labels_alert, target_recall=0.01
        )
        assert (
            unified["precision_at_recall_target"]["precision_at_target_recall"]
            == alert_only["precision_at_recall_target"]["precision_at_target_recall"]
        )


class TestReviewerAllModeJoinRedundancy:
    """Reviewer #4: all-mode path triggers multiple _evaluate_join_rows_from_labels scans."""

    def test_evaluate_twice_plus_unified_calls_join_four_times(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pred_db = tmp_path / "pred.db"
        labels = tmp_path / "labels.csv"
        _create_prediction_log_db(
            pred_db,
            [("2026-03-19T12:00:00+08:00", "b1", "p1", "c1", 0.4, 0, 1)],
        )
        labels.write_text("bet_id,label\nb1,0\n", encoding="utf-8")

        n_calls = {"n": 0}
        orig = r1r6._evaluate_join_rows_from_labels

        def _wrapped(*args: object, **kwargs: object):
            n_calls["n"] += 1
            return orig(*args, **kwargs)

        monkeypatch.setattr(r1r6, "_evaluate_join_rows_from_labels", _wrapped)

        r1r6.run_evaluate_mode(
            str(pred_db),
            "2026-03-19T00:00:00+08:00",
            "2026-03-20T00:00:00+08:00",
            labels,
            0.01,
        )
        r1r6.run_evaluate_mode(
            str(pred_db),
            "2026-03-19T00:00:00+08:00",
            "2026-03-20T00:00:00+08:00",
            labels,
            0.01,
        )
        r1r6._build_unified_sample_evaluation(
            str(pred_db),
            "2026-03-19T00:00:00+08:00",
            "2026-03-20T00:00:00+08:00",
            {"b1": (0, 0)},
            {"b1": (1, 0)},
            0.01,
        )
        assert n_calls["n"] == 4


class TestReviewerSqlitePragmaInterpolation:
    """Reviewer #5: PRAGMA uses string interpolation — guard absent today (injection / robustness)."""

    def test_malformed_table_name_causes_sqlite_error_or_empty_schema(self, tmp_path: Path) -> None:
        db = tmp_path / "x.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE prediction_log (id INTEGER PRIMARY KEY, scored_at TEXT, bet_id TEXT, "
                "score REAL, is_alert INTEGER, is_rated_obs INTEGER)"
            )
            conn.commit()
            bad = "prediction_log; SELECT 1"
            with pytest.raises(sqlite3.DatabaseError):
                r1r6._sqlite_table_columns(conn, bad)
        finally:
            conn.close()

    def test_pragma_uses_f_string_interpolation_documented_in_source(self) -> None:
        src = SCRIPT_PATH.read_text(encoding="utf-8")
        assert 'f"PRAGMA table_info({table})"' in src or "f'PRAGMA table_info({table})'" in src


class TestReviewerLargeTrainingMetricsFile:
    """Reviewer #6: no max_bytes — large JSON still loads whole file into memory."""

    def test_large_training_metrics_json_still_status_ok(self, tmp_path: Path) -> None:
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        # ~600KB payload: keeps CI friendly while proving absence of a size gate.
        pad = "x" * 600_000
        body = {
            "model_version": "v-big",
            "test_precision_at_recall_0.01": 0.11,
            "pad": pad,
        }
        p = model_dir / "training_metrics.json"
        p.write_text(json.dumps(body), encoding="utf-8")
        assert p.stat().st_size > 500_000
        baseline = r1r6._load_training_metrics_baseline(model_dir)
        assert baseline["status"] == "ok"
        assert baseline["test_precision_at_recall_0.01"] == 0.11
