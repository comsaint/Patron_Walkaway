"""Tests for trainer.serving.feature_audit SQLite audit helpers."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from trainer.serving import feature_audit as fa


def test_write_serving_feature_audit_inserts_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pl.db"
        df = pd.DataFrame(
            {
                "bet_id": [1, 2],
                "player_id": ["p1", "p2"],
                "canonical_id": ["c1", "c2"],
                "session_id": ["s1", "s2"],
                "casino_player_id": [None, "x"],
                "feat_a": [0.0, 1.0],
                "feat_b": [np.nan, 2.0],
                "score": [0.1, 0.6],
                "margin": [-0.2, 0.1],
                "is_rated_obs": [1, 1],
                "is_rated": [True, True],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-01-01 12:00:00", "2026-01-01 13:00:00"]
                ),
            }
        )
        artifacts = {
            "feature_list_meta": [
                {"name": "feat_a", "track": "track_human"},
                {"name": "feat_b", "track": "track_profile"},
            ],
            "feature_spec": {"track_llm": {"candidates": []}},
        }
        fa.write_serving_feature_audit(
            pred_log_path=str(db),
            df=df,
            model_features=["feat_a", "feat_b"],
            artifacts=artifacts,
            feature_list=["feat_a", "feat_b"],
            scored_at="2026-04-27T12:00:00+08:00",
            model_version="unit-test",
            effective_threshold=0.5,
            bundle_threshold=0.5,
            store_long_values=True,
            retention_hours=24.0,
            sample_rows=2,
            source="serving",
        )
        conn = sqlite3.connect(str(db))
        try:
            n_runs = conn.execute("SELECT COUNT(*) FROM feature_audit_runs").fetchone()[0]
            n_sum = conn.execute("SELECT COUNT(*) FROM feature_audit_feature_summary").fetchone()[0]
            n_row = conn.execute("SELECT COUNT(*) FROM feature_audit_row_sample").fetchone()[0]
            n_long = conn.execute("SELECT COUNT(*) FROM feature_audit_feature_sample_long").fetchone()[0]
        finally:
            conn.close()
        assert n_runs == 1
        assert n_sum == 2
        assert n_row == 2
        assert n_long == 4


def test_write_training_summary_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "tr.db"
        df = pd.DataFrame({"feat_x": [0.0, 3.0], "is_rated": [True, True]})
        fa.write_training_feature_audit_run(
            out_db_path=str(db),
            df=df,
            model_features=["feat_x"],
            feature_list=["feat_x"],
            feature_list_meta=None,
            feature_spec=None,
            model_version="train",
            bundle_threshold=0.4,
            effective_threshold=0.4,
            retention_hours=24.0,
        )
        conn = sqlite3.connect(str(db))
        try:
            src = conn.execute("SELECT source FROM feature_audit_runs").fetchone()[0]
            n_row = conn.execute("SELECT COUNT(*) FROM feature_audit_row_sample").fetchone()[0]
            n_long = conn.execute("SELECT COUNT(*) FROM feature_audit_feature_sample_long").fetchone()[0]
        finally:
            conn.close()
        assert src == "training"
        assert n_row == 0
        assert n_long == 0


def test_prune_feature_audit_old_removes_stale() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "p.db"
        conn = sqlite3.connect(str(db))
        try:
            fa.ensure_feature_audit_schema(conn)
            conn.execute(
                """
                INSERT INTO feature_audit_runs (
                    scored_at, model_version, feature_list_hash, feature_spec_hash,
                    effective_threshold, bundle_threshold, row_count, rated_count,
                    sample_count, source, model_features_json
                ) VALUES (?, 'm', 'h1', 'h2', 0.5, 0.5, 1, 1, 0, 'serving', '[]')
                """,
                ("1999-01-01T00:00:00+08:00",),
            )
            rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO feature_audit_feature_summary (
                    audit_run_id, feature_name, track, count_n, null_count, zero_count,
                    mean_v, std_v, min_v, p01, p05, p50, p95, p99, max_v
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (rid, "f", "unknown", 1, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            )
            conn.commit()
            fa.prune_feature_audit_old(conn, retention_hours=1.0)
            n = conn.execute("SELECT COUNT(*) FROM feature_audit_runs").fetchone()[0]
        finally:
            conn.close()
        assert n == 0


def test_feature_list_fingerprint_stable() -> None:
    h1 = fa.feature_list_fingerprint(["a", "b"], None)
    h2 = fa.feature_list_fingerprint(["a", "b"], None)
    assert h1 == h2
    h3 = fa.feature_list_fingerprint(["b", "a"], None)
    assert h1 != h3
