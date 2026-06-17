"""Tests for Wave 5 player-game shadow scorer."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from trainer_hightier.player_game_grain import BET_ID_COLUMN, GAME_ID_COLUMN, PLAYER_ID_COLUMN
from trainer_hightier.serving.model_bundle import HightierModelBundle
from trainer_hightier.serving.player_game_ready_queue import (
    refetch_player_game_from_frame,
    run_player_game_ready_queue_dry_run_cycle,
)
from trainer_hightier.serving.player_game_shadow_scorer import (
    _merge_txn_pg_features,
    build_player_game_shadow_gate_report,
    evaluate_player_game_shadow_gate,
    extract_representative_bet_rows,
    run_player_game_shadow_scoring,
    summarize_player_game_shadow_comparison,
)
from trainer_hightier.serving.state_db import init_state_db


def _conn(db_path: Path) -> sqlite3.Connection:
    init_state_db(db_path)
    return sqlite3.connect(db_path)


def _bet(bet_id: int, pv: str) -> dict[str, object]:
    return {
        "bet_id": bet_id,
        "player_id": 10,
        "game_id": 100,
        "payout_complete_dtm": pd.Timestamp("2026-05-28 14:52:45+00:00"),
        "prediction_visible_ts_cf": pd.Timestamp(pv),
        "wager": 100.0,
        "casino_win": 0.0,
        "is_back_bet": 0,
        "bet_type": "MAIN",
        "type_of_bet": "MAIN_BET",
        "__etl_insert_Dtm": pd.Timestamp("2026-05-28 14:53:00+00:00"),
    }


def test_extract_representative_bet_rows_uses_rep_id() -> None:
    """Pick configured representative bet when present in re-fetch."""

    pending = pd.DataFrame(
        [
            {
                "player_id": 10,
                "game_id": 100,
                "player_game_ready_ts": "2026-05-28T22:54:45+00:00",
                "representative_bet_id": 2,
                "bet_count": 2,
            },
        ],
    )
    all_bets = pd.DataFrame([_bet(1, "2026-05-28T22:54:45+00:00"), _bet(2, "2026-05-28T22:55:00+00:00")])

    def fetch(_pid: int, _gid: int) -> pd.DataFrame:
        return refetch_player_game_from_frame(all_bets, _pid, _gid)

    rep = extract_representative_bet_rows(pending, fetch_fn=fetch)
    assert len(rep) == 1
    assert int(rep.iloc[0][BET_ID_COLUMN]) == 2


def test_merge_txn_pg_features_overwrites_txn_columns() -> None:
    """PG txn columns replace bet-level txn on staged rows."""

    staged = pd.DataFrame(
        [
            {
                PLAYER_ID_COLUMN: 10,
                GAME_ID_COLUMN: 100,
                "txn__cash_out_cnt__w1h": 1.0,
            },
        ],
    )
    txn_pg = pd.DataFrame(
        [
            {
                PLAYER_ID_COLUMN: 10,
                GAME_ID_COLUMN: 100,
                "txn__cash_out_cnt__w1h": 9.0,
            },
        ],
    )
    out = _merge_txn_pg_features(staged, txn_pg)
    assert float(out.iloc[0]["txn__cash_out_cnt__w1h"]) == 9.0


def test_shadow_scoring_writes_pg_shadow_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed ready-queue rows produce shadow scores without alerts."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    pv = "2026-05-28T22:54:45+00:00"
    bets = pd.DataFrame([_bet(1, pv)])

    def fetch(_pid: int, _gid: int) -> pd.DataFrame:
        return refetch_player_game_from_frame(bets, _pid, _gid)

    run_player_game_ready_queue_dry_run_cycle(
        conn,
        incremental_bets=bets,
        now_ts=datetime(2026, 5, 28, 22, 56, 0, tzinfo=timezone.utc),
        fetch_fn=fetch,
    )
    feat_cols = ("wager", "casino_win", "is_back_bet")
    model = LogisticRegression()
    x_train = np.zeros((2, len(feat_cols)))
    y_train = np.array([0, 1])
    model.fit(x_train, y_train)
    bundle = HightierModelBundle(
        bundle_dir=tmp_path,
        model=model,
        threshold=0.5,
        feature_columns=feat_cols,
        categorical_columns=(),
        category_categories={},
        model_version="shadow-test",
        training_metrics={"score_aggregation": "native"},
        score_aggregation="native",
    )

    class _Batch:
        cursor = pd.Series(dtype="datetime64[ns, UTC]")
        pool = bets.copy()
        pool_window_start = None
        pool_window_end = None

    def fake_build_staged_features(_batch, *, mapping_parquet, supplier_plan):
        return pd.DataFrame(
            [
                {
                    PLAYER_ID_COLUMN: 10,
                    GAME_ID_COLUMN: 100,
                    "wager": 1.0,
                    "casino_win": 0.0,
                    "is_back_bet": 0,
                },
            ],
        )

    def fake_attach_feast(staged, _adapter, **kwargs):
        return staged, pd.DataFrame(), object()

    monkeypatch.setattr(
        "trainer_hightier.serving.scorer._build_staged_features",
        fake_build_staged_features,
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.scorer._attach_feast_mid_slow",
        fake_attach_feast,
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.player_game_shadow_scorer.compute_txn_pg_features_for_ready_rows",
        lambda *_a, **_k: pd.DataFrame(
            columns=[PLAYER_ID_COLUMN, GAME_ID_COLUMN],
        ),
    )
    from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
    from trainer_hightier.serving.feature_supply import ScorerSupplierPlan

    monkeypatch.setattr(
        "trainer_hightier.serving.feature_supply.load_frozen_registry_for_bundle",
        lambda _p: load_candidate_registry(),
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.feature_supply.build_scorer_supplier_plan",
        lambda _snap, feat_cols: ScorerSupplierPlan(
            baseline_cols=tuple(feat_cols),
            feast_trial_cols=(),
            feast_mid_cols=(),
            mid_composite_cols=(),
            feast_slow_cols=(),
            short_term_cols=(),
            unknown_cols=(),
            txn_cols=(),
        ),
    )

    summary = run_player_game_shadow_scoring(
        conn,
        batch=_Batch(),
        pg_bundle=bundle,
        mapping_parquet=None,
        feast_adapter=object(),
        manifest=None,
        fetch_fn=fetch,
    )
    row = conn.execute(
        "SELECT player_game_score, shadow_alert FROM pg_shadow_scores",
    ).fetchone()
    conn.close()
    assert summary.n_scored == 1
    assert row is not None
    assert float(row[0]) >= 0.0


def test_shadow_comparison_counts_overlap_and_alerts(tmp_path: Path) -> None:
    """W6 summary joins legacy alerts with shadow scores on player-game keys."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    conn.execute(
        """
        INSERT INTO alerts (bet_id, player_id, game_id, score, ts)
        VALUES ('1', '10', '100', 0.9, '2026-05-28T23:00:00+08:00')
        """,
    )
    conn.execute(
        """
        INSERT INTO pg_shadow_scores (
            player_id, game_id, player_game_ready_ts, scored_at,
            representative_bet_id, player_game_score, threshold,
            shadow_alert, model_version, bet_count
        ) VALUES (
            10, 100, '2026-05-28T22:54:45+00:00', '2026-05-28T23:01:00+08:00',
            1, 0.85, 0.5, 1, 'shadow-test', 2
        )
        """,
    )
    conn.execute(
        """
        INSERT INTO pg_completed_player_games (
            player_id, game_id, player_game_ready_ts, dry_run_completed_at,
            representative_bet_id, bet_count, pending_age_sec, ready_lag_sec,
            late_after_score_hypothetical, attempt_count
        ) VALUES (
            10, 100, '2026-05-28T22:54:45+00:00', '2026-05-28T23:01:00+08:00',
            1, 2, 45.0, 75.0, 0, 1
        )
        """,
    )
    conn.commit()
    summary = summarize_player_game_shadow_comparison(conn)
    gate = evaluate_player_game_shadow_gate(summary)
    conn.close()
    assert summary.n_overlap == 1
    assert summary.n_legacy_alert == 1
    assert summary.n_shadow_alert == 1
    assert summary.n_both_alert == 1
    assert summary.ready_lag_sec_p95 == 75.0
    assert gate["checks"]["ready_lag_p95_ok"] is True
    assert gate["checks"]["min_overlap_ok"] is False


def test_shadow_gate_report_includes_dry_run_metrics(tmp_path: Path) -> None:
    """W6 report bundles comparison, gate, and ready-queue dry-run stats."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    conn.execute(
        """
        INSERT INTO pg_completed_player_games (
            player_id, game_id, player_game_ready_ts, dry_run_completed_at,
            representative_bet_id, bet_count, pending_age_sec, ready_lag_sec,
            late_after_score_hypothetical, attempt_count
        ) VALUES (
            10, 100, '2026-05-28T22:54:45+00:00', '2026-05-28T23:01:00+08:00',
            1, 2, 45.0, 75.0, 0, 1
        )
        """,
    )
    conn.commit()
    report = build_player_game_shadow_gate_report(conn)
    conn.close()
    assert report["report_kind"] == "player_game_shadow_gate_w6"
    assert report["dry_run_metrics"]["n_completed"] == 1
    assert "gate" in report
