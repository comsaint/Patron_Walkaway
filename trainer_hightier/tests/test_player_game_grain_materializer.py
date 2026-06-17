"""Tests for player-game grain materialization and observation-time DQ."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig, SCORER_POLL_INTERVAL_SECONDS
from trainer_hightier.player_game_grain import (
    PLAYER_GAME_LABEL_COLUMN,
    PLAYER_GAME_PCD_COLUMN,
    PLAYER_GAME_READY_TS_COLUMN,
    aggregate_bets_to_player_game_rows,
    compute_serving_due_ts,
    enrich_player_game_splits_with_baseline_bet_features,
    materialize_player_game_split_parquet,
    summarize_player_game_dq,
)


def _bet_row(
    *,
    bet_id: int,
    player_id: int,
    game_id: int,
    label: int,
    pcd: str,
    pv: str,
    type_of_bet: str,
    wager: float,
    table_id: int | None = 1,
) -> dict[str, object]:
    """Build one minimal bet row for player-game tests."""

    row: dict[str, object] = {
        "bet_id": bet_id,
        "player_id": player_id,
        "game_id": game_id,
        "walkaway_label": label,
        "payout_complete_dtm": pd.Timestamp(pcd),
        "prediction_visible_ts_cf": pd.Timestamp(pv),
        "type_of_bet": type_of_bet,
        "wager": wager,
    }
    if table_id is not None:
        row["table_id"] = table_id
    return row


def test_aggregate_player_game_ready_ts_is_max_prediction_visible() -> None:
    """Late side bet raises player-game ready timestamp."""

    df = pd.DataFrame(
        [
            _bet_row(
                bet_id=1,
                player_id=10,
                game_id=100,
                label=0,
                pcd="2026-05-28 14:52:45",
                pv="2026-05-28 22:54:45+08:00",
                type_of_bet="MAIN_BET",
                wager=3000.0,
            ),
            _bet_row(
                bet_id=2,
                player_id=10,
                game_id=100,
                label=0,
                pcd="2026-05-28 14:52:45",
                pv="2026-05-28 22:54:45+08:00",
                type_of_bet="SIDE_BET",
                wager=500.0,
            ),
            _bet_row(
                bet_id=3,
                player_id=10,
                game_id=100,
                label=1,
                pcd="2026-05-28 14:52:45",
                pv="2026-05-28 22:55:30+08:00",
                type_of_bet="SIDE_BET",
                wager=500.0,
            ),
        ],
    )
    out, audit = aggregate_bets_to_player_game_rows(df)
    assert audit.output_player_games == 1
    assert out.iloc[0]["player_game_bet_count"] == 3
    assert out.iloc[0]["pg__main_bet_count"] == 1
    assert out.iloc[0]["pg__side_bet_count"] == 2
    assert out.iloc[0]["pg__wager_sum"] == pytest.approx(4000.0)
    assert out.iloc[0][PLAYER_GAME_LABEL_COLUMN] == 1
    assert out.iloc[0][PLAYER_GAME_PCD_COLUMN] == pd.Timestamp("2026-05-28 14:52:45")
    assert out.iloc[0][PLAYER_GAME_READY_TS_COLUMN] == pd.Timestamp("2026-05-28 22:55:30+08:00")


def test_aggregate_excludes_pcd_span_dq_violation() -> None:
    """Player-games with payout span above threshold are excluded."""

    df = pd.DataFrame(
        [
            _bet_row(
                bet_id=1,
                player_id=10,
                game_id=100,
                label=0,
                pcd="2026-05-28 14:52:45",
                pv="2026-05-28 22:54:45+08:00",
                type_of_bet="MAIN_BET",
                wager=100.0,
            ),
            _bet_row(
                bet_id=2,
                player_id=10,
                game_id=100,
                label=0,
                pcd="2026-05-28 14:54:00",
                pv="2026-05-28 22:54:45+08:00",
                type_of_bet="SIDE_BET",
                wager=100.0,
            ),
        ],
    )
    out, audit = aggregate_bets_to_player_game_rows(df, exclude_dq_violations=True)
    assert out.empty
    assert audit.dq_pcd_span_violations == 1
    assert audit.excluded_player_games == 1


def test_summarize_player_game_dq_flags_pv_span() -> None:
    """DQ summary marks player-games whose prediction-visible span exceeds poll interval."""

    df = pd.DataFrame(
        [
            _bet_row(
                bet_id=1,
                player_id=10,
                game_id=100,
                label=0,
                pcd="2026-05-28 14:52:45",
                pv="2026-05-28 22:54:45+08:00",
                type_of_bet="MAIN_BET",
                wager=100.0,
            ),
            _bet_row(
                bet_id=2,
                player_id=10,
                game_id=100,
                label=0,
                pcd="2026-05-28 14:52:45",
                pv="2026-05-28 22:55:31+08:00",
                type_of_bet="SIDE_BET",
                wager=100.0,
            ),
        ],
    )
    summary = summarize_player_game_dq(df, pv_span_max_seconds=SCORER_POLL_INTERVAL_SECONDS)
    assert len(summary) == 1
    assert bool(summary.iloc[0]["dq_exclude"])
    assert "pv_span" in str(summary.iloc[0]["dq_exclude_reason"])


def test_compute_serving_due_ts_adds_holdback() -> None:
    """Serving due time equals first visible timestamp plus poll interval."""

    first = pd.Timestamp("2026-05-28 22:54:45+08:00")
    due = compute_serving_due_ts(first, holdback_seconds=SCORER_POLL_INTERVAL_SECONDS)
    assert due == first + pd.Timedelta(seconds=SCORER_POLL_INTERVAL_SECONDS)


def test_aggregate_requires_columns() -> None:
    """Missing required columns fail fast with explicit error."""

    df = pd.DataFrame({"player_id": [1], "game_id": [2]})
    with pytest.raises(ValueError, match="missing columns"):
        aggregate_bets_to_player_game_rows(df)


def test_materialize_player_game_split_parquet_writes_compat_columns(tmp_path: Path) -> None:
    """Materialized split includes Step-5-compatible label and payout aliases."""

    bets = pd.DataFrame(
        [
            {
                "bet_id": 1,
                "player_id": 10,
                "game_id": 100,
                "walkaway_label": 0,
                "payout_complete_dtm": pd.Timestamp("2026-05-28 14:52:45"),
                "prediction_visible_ts_cf": pd.Timestamp("2026-05-28 22:54:45+08:00"),
                "type_of_bet": "MAIN_BET",
                "wager": 3000.0,
            },
            {
                "bet_id": 2,
                "player_id": 10,
                "game_id": 100,
                "walkaway_label": 1,
                "payout_complete_dtm": pd.Timestamp("2026-05-28 14:52:45"),
                "prediction_visible_ts_cf": pd.Timestamp("2026-05-28 22:55:30+08:00"),
                "type_of_bet": "SIDE_BET",
                "wager": 500.0,
            },
        ],
    )
    bet_path = tmp_path / "val.parquet"
    out_path = tmp_path / "player_game_val.parquet"
    bets.to_parquet(bet_path, index=False)
    audit = materialize_player_game_split_parquet(bet_path, out_path)
    out = pd.read_parquet(out_path)
    assert audit.output_player_games == 1
    assert len(out) == 1
    assert int(out.iloc[0][PLAYER_GAME_LABEL_COLUMN]) == 1
    assert out.iloc[0]["walkaway_label"] == out.iloc[0][PLAYER_GAME_LABEL_COLUMN]
    assert out.iloc[0]["payout_complete_dtm"] == out.iloc[0][PLAYER_GAME_READY_TS_COLUMN]
    assert out.iloc[0][PLAYER_GAME_READY_TS_COLUMN] == pd.Timestamp("2026-05-28 22:55:30+08:00")


def _write_split_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    """Write one minimal split parquet for enrich tests."""

    pd.DataFrame(rows).to_parquet(path, index=False)


def test_enrich_baseline_bet_features_joins_rep_bet_columns(tmp_path: Path) -> None:
    """Baseline bet features attach via representative_bet_id; txn columns stay from PG."""

    pg_dir = tmp_path / "pg_txn_splits"
    bet_dir = tmp_path / "bet_splits"
    out_dir = tmp_path / "baseline_parity_splits"
    pg_dir.mkdir()
    bet_dir.mkdir()

    pg_row = {
        "player_id": 10,
        "game_id": 100,
        "representative_bet_id": 2,
        "player_game_label": 1,
        "txn__cash_out_cnt__w1h": 9.0,
    }
    bet_rows = [
        {"bet_id": 1, "wager": 100.0},
        {"bet_id": 2, "wager": 3000.0},
    ]
    for split in ("train", "val", "test"):
        _write_split_parquet(pg_dir / f"{split}.parquet", [pg_row])
        _write_split_parquet(bet_dir / f"{split}.parquet", bet_rows)

    meta = enrich_player_game_splits_with_baseline_bet_features(
        pg_dir,
        bet_dir,
        out_dir,
        feature_columns=("wager", "txn__cash_out_cnt__w1h"),
        duckdb_runtime=DuckDbRuntimeConfig(memory_limit="512MB"),
    )
    out = pd.read_parquet(out_dir / "val.parquet")
    assert meta["join_grain"] == "representative_bet_id = bet_id"
    assert float(out.iloc[0]["wager"]) == 3000.0
    assert float(out.iloc[0]["txn__cash_out_cnt__w1h"]) == 9.0
    assert meta["split_stats"]["val"]["output_player_games"] == 1


def test_enrich_baseline_bet_features_requires_feature_columns(tmp_path: Path) -> None:
    """Empty feature column tuple fails fast."""

    with pytest.raises(ValueError, match="requires feature_columns"):
        enrich_player_game_splits_with_baseline_bet_features(
            tmp_path / "pg",
            tmp_path / "bet",
            tmp_path / "out",
            feature_columns=(),
            duckdb_runtime=DuckDbRuntimeConfig(memory_limit="512MB"),
        )
