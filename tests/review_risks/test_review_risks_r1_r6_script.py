"""
Review-risks tests for run_r1_r6_analysis.py.

Only tests are added. No production code changes.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
r1r6 = _load_module("run_r1_r6_analysis_mod", SCRIPT_PATH)


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


class TestR1R6ReviewRisks:
    def test_autolabel_should_fail_on_ambiguous_player_to_canonical_mapping(self, monkeypatch: pytest.MonkeyPatch):
        """
        Risk #1: same player_id mapped to multiple canonical_id should fail/warn,
        not silently overwrite.
        """
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            db = t / "prediction_log.db"
            sample_csv = t / "sample.csv"
            out_csv = t / "labels.csv"
            _create_prediction_log_db(
                db,
                [
                    ("2026-03-19T20:00:00+08:00", "b1", "p1", "c1", 0.1, 0, 1),
                    ("2026-03-19T20:01:00+08:00", "b2", "p1", "c2", 0.2, 0, 1),
                ],
            )
            with sample_csv.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["bet_id", "score", "scored_at", "is_alert", "is_rated_obs", "bin_id"])
                w.writerow(["b1", "0.1", "2026-03-19T20:00:00+08:00", "0", "1", "0"])
                w.writerow(["b2", "0.2", "2026-03-19T20:01:00+08:00", "0", "1", "0"])

            class _DummyCH:
                def query_df(self, *_args, **_kwargs):
                    import pandas as pd  # type: ignore[import-untyped]

                    return pd.DataFrame(
                        {
                            "bet_id": ["b1", "b2"],
                            "player_id": ["p1", "p1"],
                            "payout_complete_dtm": ["2026-03-19T20:00:00", "2026-03-19T20:01:00"],
                        }
                    )

            monkeypatch.setattr(r1r6, "get_clickhouse_client", lambda: _DummyCH())
            with pytest.raises(ValueError, match="ambiguous|multiple canonical"):
                r1r6.run_autolabel_mode(
                    db_path=str(db),
                    start_ts="2026-03-19T00:00:00+08:00",
                    end_ts="2026-03-20T00:00:00+08:00",
                    sample_csv=sample_csv,
                    out_labels_csv=out_csv,
                    player_chunk_size=100,
                )

    def test_evaluate_should_exclude_censored_rows(self):
        """
        Risk #2: evaluate should exclude censored=1 rows when provided.
        """
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            db = t / "prediction_log.db"
            labels = t / "labels.csv"
            _create_prediction_log_db(
                db,
                [
                    ("2026-03-19T20:00:00+08:00", "b1", "p1", "c1", 0.9, 1, 1),
                    ("2026-03-19T20:01:00+08:00", "b2", "p2", "c2", 0.8, 1, 1),
                ],
            )
            with labels.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["bet_id", "label", "censored"])
                w.writerow(["b1", "1", "0"])
                w.writerow(["b2", "1", "1"])  # should be excluded in target behavior

            out = r1r6.run_evaluate_mode(
                db_path=str(db),
                start_ts="2026-03-19T00:00:00+08:00",
                end_ts="2026-03-20T00:00:00+08:00",
                labels_csv=labels,
                target_recall=0.01,
            )
            assert out["n_labeled_matched"] == 1

    def test_all_mode_error_message_should_include_failed_step(self, monkeypatch: pytest.MonkeyPatch):
        """
        Risk #3: all mode should include failed step name for observability.
        """
        monkeypatch.setattr(
            r1r6,
            "parse_args",
            lambda: SimpleNamespace(
                mode="all",
                start_ts="2026-03-19T00:00:00+08:00",
                end_ts="2026-03-20T00:00:00+08:00",
                env_file="",
                pred_db_path=str(REPO_ROOT / "local_state" / "prediction_log.db"),
                pretty=False,
                sample_size=10,
                bins=2,
                seed=42,
                out_csv=str(REPO_ROOT / "investigations/test_vs_production/snapshots/_tmp_sample.csv"),
                labels_csv="",
                target_recall=0.01,
                sample_csv=str(REPO_ROOT / "investigations/test_vs_production/snapshots/_tmp_sample.csv"),
                out_labels_csv=str(REPO_ROOT / "investigations/test_vs_production/snapshots/_tmp_labels.csv"),
                player_chunk_size=100,
                max_players=20000,
            ),
        )
        monkeypatch.setattr(r1r6, "run_sample_mode", lambda **_k: {"mode": "sample"})
        monkeypatch.setattr(r1r6, "run_autolabel_mode", lambda **_k: (_ for _ in ()).throw(RuntimeError("boom")))

        out = io.StringIO()
        err = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        rc = r1r6.main()
        assert rc == 2
        assert "autolabel" in err.getvalue().lower()

    def test_sampling_should_not_use_builtin_hash_for_reproducibility(self):
        """
        Risk #4: builtin hash() is process-randomized; prefer stable hash/random.Random(seed).
        """
        src = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "hash((" not in src

    def test_autolabel_should_have_guardrail_for_large_player_set(self):
        """
        Risk #5: autolabel should include explicit guardrail for huge player set.
        """
        src = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "max_players" in src or "max_rows" in src

    def test_main_errors_should_go_to_stderr(self, monkeypatch: pytest.MonkeyPatch):
        """
        Risk #6: errors should be written to stderr, stdout reserved for JSON payload.
        """
        monkeypatch.setattr(
            r1r6,
            "parse_args",
            lambda: SimpleNamespace(
                mode="evaluate",
                start_ts="2026-03-19T00:00:00+08:00",
                end_ts="2026-03-20T00:00:00+08:00",
                env_file="",
                pred_db_path="",
                pretty=False,
                sample_size=10,
                bins=2,
                seed=42,
                out_csv="x.csv",
                labels_csv="x.csv",
                target_recall=0.01,
                sample_csv="x.csv",
                out_labels_csv="x.csv",
                player_chunk_size=100,
                max_players=20000,
            ),
        )
        out = io.StringIO()
        err = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        rc = r1r6.main()
        assert rc == 2
        assert err.getvalue().strip() != ""

    def test_sample_mode_minimal_smoke(self):
        """
        Minimal passing path: sample mode with temp prediction_log DB.
        """
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            db = t / "prediction_log.db"
            out_csv = t / "sample.csv"
            _create_prediction_log_db(
                db,
                [
                    ("2026-03-19T20:00:00+08:00", "b1", "p1", "c1", 0.1, 0, 1),
                    ("2026-03-19T20:01:00+08:00", "b2", "p2", "c2", 0.2, 0, 1),
                ],
            )
            payload = r1r6.run_sample_mode(
                db_path=str(db),
                start_ts="2026-03-19T00:00:00+08:00",
                end_ts="2026-03-20T00:00:00+08:00",
                sample_size=10,
                bins=2,
                seed=42,
                out_csv=out_csv,
            )
            assert payload["mode"] == "sample"
            assert out_csv.exists()
