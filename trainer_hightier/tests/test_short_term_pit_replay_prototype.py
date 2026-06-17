"""Tests for short-term PIT event replay prototype."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import duckdb

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.feature_experiment.materialize_fe_derived import (
    materialize_fe_derived_short_term_parquet,
)
from trainer_hightier.feature_experiment.short_term_pit_replay_prototype import (
    PROTOTYPE_OUTPUT_COLUMNS,
    _emit_prototype_features,
    _EntityReplayState,
    _PoolEvent,
    _replay_features,
    benchmark_replay_vs_bounded,
    compare_replay_to_oracle,
    evaluate_replay_go_no_go,
    materialize_short_term_replay_prototype,
)
from trainer_hightier.serving.scorer import compute_scoring_bounds_for_bets
from trainer_hightier.config import default_hightier_serving_config


def _write_fixture(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    target_bet_ids: tuple[float, ...],
    payout_yyyymm: str = "202406",
    canonical_by_player: dict[int, str] | None = None,
) -> tuple[Path, Path, Path, str]:
    """Write cleaned bets, mapping, and training parquet for replay tests."""
    cleaned = tmp_path / "cleaned"
    cleaned.mkdir()
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), cleaned / "bets.parquet")
    player_ids = sorted({int(row["player_id"]) for row in rows})
    cmap = tmp_path / "map.parquet"
    if canonical_by_player is None:
        mapping_rows = [{"player_id": pid, "canonical_id": f"c{pid}"} for pid in player_ids]
    else:
        mapping_rows = [
            {"player_id": pid, "canonical_id": canonical_by_player[pid]}
            for pid in player_ids
        ]
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame(mapping_rows)),
        cmap,
    )
    train_rows = [row for row in rows if float(row["bet_id"]) in target_bet_ids]
    train = tmp_path / "train.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(train_rows)), train)
    return cleaned, cmap, train, payout_yyyymm


def _oracle_and_replay(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    target_bet_ids: tuple[float, ...],
    payout_yyyymm: str = "202406",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Materialize bounded oracle and replay prototype on the same fixture."""
    cleaned, cmap, train, ym = _write_fixture(
        tmp_path,
        rows,
        target_bet_ids=target_bet_ids,
        payout_yyyymm=payout_yyyymm,
    )
    oracle_out = tmp_path / "oracle.parquet"
    replay_out = tmp_path / "replay.parquet"
    materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=oracle_out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        short_term_columns=(
            "fe__bets_cnt__w15m",
            "fe__wager_sum__w15m",
            "fe__time_since_last_bet_sec",
            "fe__odds__payout_odds_step_ratio",
        ),
        trial_columns=("bet__bets_cnt__w1h",),
        payout_yyyymm=ym,
    )
    materialize_short_term_replay_prototype(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=replay_out,
        payout_yyyymm=ym,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    return pq.read_table(oracle_out).to_pandas(), pq.read_table(replay_out).to_pandas()


def _base_row(
    *,
    bet_id: float,
    player_id: int,
    pcd: pd.Timestamp,
    wager: float,
    payout_odds: float = 2.0,
) -> dict[str, object]:
    """Build one cleaned bet row."""
    day = pcd.tz_convert("Asia/Hong_Kong").date()
    return {
        "bet_id": bet_id,
        "player_id": player_id,
        "session_id": 1,
        "table_id": 1,
        "gaming_day_event": day,
        "payout_complete_dtm": pcd,
        "wager": wager,
        "is_back_bet": 0,
        "payout_odds": payout_odds,
        "casino_win": 0.0,
        "theo_win": 1.0,
        "base_ha": 0.01,
        "bet_type": "PLAYER",
        "type_of_bet": "MAIN",
        "prediction_visible_ts_cf": pcd,
        "__etl_insert_Dtm_synthetic": pcd,
    }


def test_replay_emit_before_update_w15m(tmp_path: Path) -> None:
    """Prior bets inside 15m are counted; current bet is excluded."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=10), wager=50.0),
        _base_row(bet_id=3.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=20), wager=25.0),
    ]
    oracle_df, replay_df = _oracle_and_replay(tmp_path, rows, target_bet_ids=(3.0,))
    report = compare_replay_to_oracle(replay_df, oracle_df)
    assert report["passed"] is True
    got = replay_df.iloc[0]
    assert int(got["fe__bets_cnt__w15m"]) == 1
    assert float(got["fe__wager_sum__w15m"]) == pytest.approx(50.0)


def test_replay_window_boundary_exactly_15m(tmp_path: Path) -> None:
    """A bet exactly 15m before target is inside the DuckDB RANGE lower bound."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    target_pcd = t0 + pd.Timedelta(minutes=30)
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
        _base_row(bet_id=2.0, player_id=10, pcd=target_pcd - pd.Timedelta(minutes=15), wager=40.0),
        _base_row(bet_id=3.0, player_id=10, pcd=target_pcd, wager=25.0),
    ]
    oracle_df, replay_df = _oracle_and_replay(tmp_path, rows, target_bet_ids=(3.0,))
    report = compare_replay_to_oracle(replay_df, oracle_df)
    assert report["passed"] is True
    assert int(replay_df.iloc[0]["fe__bets_cnt__w15m"]) == 1


def test_replay_same_timestamp_tie_break(tmp_path: Path) -> None:
    """Same-timestamp peers are excluded from RANGE windows for the later bet_id."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0, payout_odds=2.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0, wager=50.0, payout_odds=4.0),
    ]
    oracle_df, replay_df = _oracle_and_replay(tmp_path, rows, target_bet_ids=(2.0,))
    report = compare_replay_to_oracle(replay_df, oracle_df)
    assert report["passed"] is True
    got = replay_df.iloc[0]
    assert int(got["fe__bets_cnt__w15m"]) == 0
    assert int(got["bet__bets_cnt__w1h"]) == 0
    assert float(got["fe__time_since_last_bet_sec"]) == pytest.approx(0.0)
    assert float(got["fe__odds__payout_odds_step_ratio"]) == pytest.approx(2.0)


def test_replay_oracle_parity_multi_target(tmp_path: Path) -> None:
    """Replay matches bounded oracle across multiple target rows."""
    t0 = pd.Timestamp("2024-06-01 06:30:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0, payout_odds=2.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=30),
            wager=50.0,
            payout_odds=3.0,
        ),
        _base_row(
            bet_id=3.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=45),
            wager=25.0,
            payout_odds=1.5,
        ),
    ]
    oracle_df, replay_df = _oracle_and_replay(tmp_path, rows, target_bet_ids=(2.0, 3.0))
    report = compare_replay_to_oracle(replay_df, oracle_df)
    assert report["passed"] is True, report
    merged = replay_df.merge(oracle_df, on="bet_id", suffixes=("_replay", "_oracle"))
    assert len(merged) == 2


def test_replay_internal_state_matches_bounds(tmp_path: Path) -> None:
    """Direct state emit matches scorer bounds on a tiny in-memory slice."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=5), wager=50.0),
    ]
    targets = pd.DataFrame(rows[-1:])
    targets["canonical_id"] = "c10"
    bounds = compute_scoring_bounds_for_bets(targets, cfg=default_hightier_serving_config())
    events_df = pd.DataFrame(rows)[
        ["bet_id", "player_id", "payout_complete_dtm", "wager", "payout_odds"]
    ]
    state = _EntityReplayState()
    state.append(
        _PoolEvent(
            bet_id=1.0,
            player_id=10,
            canonical_id="c10",
            pcd=t0,
            wager=100.0,
            payout_odds=2.0,
        ),
    )
    bound = bounds.iloc[0]
    emitted = _emit_prototype_features(
        state,
        pool_start=pd.Timestamp(bound["pool_start"]),
        scoring_pcd=pd.Timestamp(bound["scoring_pcd"]),
        target_bet_id=2.0,
        payout_odds=2.0,
        trial_pool_start=pd.Timestamp(bound["pool_start"]),
    )
    replay_df, _ = _replay_features(
        events_df,
        targets,
        bounds,
        canonical_by_player={10: "c10"},
    )
    assert emitted["fe__bets_cnt__w15m"] == replay_df.iloc[0]["fe__bets_cnt__w15m"]


def test_materialize_replay_prototype_writes_columns(tmp_path: Path) -> None:
    """End-to-end materializer writes all prototype columns."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=10), wager=50.0),
    ]
    cleaned, cmap, train, ym = _write_fixture(tmp_path, rows, target_bet_ids=(2.0,))
    out = tmp_path / "out.parquet"
    _, metrics = materialize_short_term_replay_prototype(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=out,
        payout_yyyymm=ym,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    df = pq.read_table(out).to_pandas()
    assert list(df.columns) == list(PROTOTYPE_OUTPUT_COLUMNS)
    assert metrics["output_rows"] == 1
    assert metrics["max_state_keys"] >= 1


@pytest.mark.slow
def test_replay_benchmark_smoke(tmp_path: Path) -> None:
    """Optional smoke benchmark reports parity and speedup metadata."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(
            bet_id=float(i),
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=i),
            wager=float(i + 1),
            payout_odds=1.0 + 0.1 * i,
        )
        for i in range(30)
    ]
    cleaned, cmap, train, ym = _write_fixture(
        tmp_path,
        rows,
        target_bet_ids=tuple(float(i) for i in range(5, 30)),
    )
    bench = benchmark_replay_vs_bounded(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        payout_yyyymm=ym,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        target_limit=25,
        out_dir=tmp_path / "bench",
    )
    assert bench["parity"]["passed"] is True
    assert bench["replay_metrics"]["output_rows"] == 25
    assert bench["speedup_ratio"] is not None


def test_go_no_go_decision_on_sample() -> None:
    """Document go/no-go gate: parity required; 3x speedup is integration threshold."""
    decision = evaluate_replay_go_no_go(
        parity_passed=True,
        replay_elapsed_seconds=1.0,
        bounded_elapsed_seconds=2.5,
    )
    assert decision["decision"] == "stop_or_optimize"
    assert decision["parity_passed"] is True
    assert decision["speedup_ratio"] == pytest.approx(2.5)


def _oracle_and_indexed(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    target_bet_ids: tuple[float, ...],
    payout_yyyymm: str = "202406",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Materialize bounded oracle and indexed replay on the same fixture."""
    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        INDEXED_PROTOTYPE_OUTPUT_COLUMNS,
        materialize_short_term_replay_indexed_prototype,
        resolve_scorer_short_pit_prototype_gate_columns,
        split_scorer_short_pit_gate_columns,
    )

    cleaned, cmap, train, ym = _write_fixture(
        tmp_path,
        rows,
        target_bet_ids=target_bet_ids,
        payout_yyyymm=payout_yyyymm,
    )
    oracle_out = tmp_path / "oracle.parquet"
    replay_out = tmp_path / "replay_indexed.parquet"
    gate_columns = resolve_scorer_short_pit_prototype_gate_columns()
    short_term_columns, trial_columns = split_scorer_short_pit_gate_columns(gate_columns)
    materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=oracle_out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        short_term_columns=short_term_columns,
        trial_columns=trial_columns,
        payout_yyyymm=ym,
    )
    materialize_short_term_replay_indexed_prototype(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=replay_out,
        payout_yyyymm=ym,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    oracle_df = pq.read_table(oracle_out).to_pandas()
    replay_df = pq.read_table(replay_out).to_pandas()
    report = compare_replay_to_oracle(
        replay_df,
        oracle_df,
        columns=gate_columns,
    )
    assert report["passed"] is True, report
    return oracle_df, replay_df


def test_indexed_replay_oracle_parity_multi_target(tmp_path: Path) -> None:
    """Indexed replay matches bounded oracle across multiple target rows."""
    t0 = pd.Timestamp("2024-06-01 06:30:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0, payout_odds=2.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=30),
            wager=50.0,
            payout_odds=3.0,
        ),
        _base_row(
            bet_id=3.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=45),
            wager=25.0,
            payout_odds=1.5,
        ),
    ]
    oracle_df, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(2.0, 3.0))
    assert len(replay_df) == 2


def test_indexed_replay_same_timestamp_tie_break(tmp_path: Path) -> None:
    """Indexed replay preserves same-timestamp lag and RANGE semantics."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0, payout_odds=2.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0, wager=50.0, payout_odds=4.0),
    ]
    oracle_df, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(2.0,))
    got = replay_df.iloc[0]
    assert int(got["fe__bets_cnt__w15m"]) == 0
    assert int(got["bet__bets_cnt__w1h"]) == 0
    assert float(got["fe__time_since_last_bet_sec"]) == pytest.approx(0.0)
    assert float(got["fe__odds__payout_odds_step_ratio"]) == pytest.approx(2.0)


def test_materialize_indexed_replay_writes_phase_timings(tmp_path: Path) -> None:
    """Indexed materializer records phase timing metrics."""
    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        INDEXED_PROTOTYPE_OUTPUT_COLUMNS,
        materialize_short_term_replay_indexed_prototype,
    )

    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=10), wager=50.0),
    ]
    cleaned, cmap, train, ym = _write_fixture(tmp_path, rows, target_bet_ids=(2.0,))
    out = tmp_path / "out_indexed.parquet"
    _, metrics = materialize_short_term_replay_indexed_prototype(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=out,
        payout_yyyymm=ym,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    assert "phase_timings" in metrics
    assert metrics["phase_timings"]["emit_s"] >= 0.0
    assert metrics["max_array_len"] >= 1
    df = pq.read_table(out).to_pandas()
    assert list(df.columns) == list(INDEXED_PROTOTYPE_OUTPUT_COLUMNS)


def test_indexed_replay_bet_pack_1h_oracle_parity(tmp_path: Path) -> None:
    """Indexed replay matches bounded oracle for full trial 1h pack."""
    t0 = pd.Timestamp("2024-06-01 12:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=10.0, payout_odds=2.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=30),
            wager=20.0,
            payout_odds=3.0,
        ),
        _base_row(
            bet_id=3.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=45),
            wager=99.0,
            payout_odds=9.0,
        ),
        _base_row(
            bet_id=4.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(hours=2),
            wager=1.0,
            payout_odds=1.0,
        ),
    ]
    rows[0]["is_back_bet"] = 1
    rows[3]["is_back_bet"] = 1
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(1.0, 2.0, 3.0, 4.0))
    r1 = replay_df.loc[replay_df["bet_id"] == 1.0].iloc[0]
    assert int(r1["bet__bets_cnt__w1h"]) == 0
    assert float(r1["bet__wager_sum__w1h"]) == 0.0
    r2 = replay_df.loc[replay_df["bet_id"] == 2.0].iloc[0]
    assert int(r2["bet__bets_cnt__w1h"]) == 1
    assert float(r2["bet__wager_sum__w1h"]) == pytest.approx(10.0)
    assert float(r2["bet__back_bet_ratio__w1h"]) == pytest.approx(1.0)
    assert float(r2["bet__payout_odds_avg__w1h"]) == pytest.approx(2.0)
    r3 = replay_df.loc[replay_df["bet_id"] == 3.0].iloc[0]
    assert int(r3["bet__bets_cnt__w1h"]) == 2
    assert float(r3["bet__wager_sum__w1h"]) == pytest.approx(30.0)
    assert float(r3["bet__back_bet_ratio__w1h"]) == pytest.approx(0.5)
    assert float(r3["bet__payout_odds_avg__w1h"]) == pytest.approx(2.5)
    r4 = replay_df.loc[replay_df["bet_id"] == 4.0].iloc[0]
    assert int(r4["bet__bets_cnt__w1h"]) == 0


def test_indexed_replay_bet_pack_cross_player_same_canonical(tmp_path: Path) -> None:
    """Trial 1h pack aggregates prior bets across player_ids under one canonical_id."""
    t0 = pd.Timestamp("2024-06-01 12:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=10.0, payout_odds=2.0),
        _base_row(
            bet_id=2.0,
            player_id=20,
            pcd=t0 + pd.Timedelta(minutes=20),
            wager=5.0,
            payout_odds=3.0,
        ),
        _base_row(
            bet_id=3.0,
            player_id=20,
            pcd=t0 + pd.Timedelta(minutes=40),
            wager=7.0,
            payout_odds=4.0,
        ),
    ]
    rows[1]["is_back_bet"] = 1
    cleaned, cmap, train, ym = _write_fixture(
        tmp_path,
        rows,
        target_bet_ids=(1.0, 2.0, 3.0),
        canonical_by_player={10: "same_patron", 20: "same_patron"},
    )
    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        INDEXED_PROTOTYPE_OUTPUT_COLUMNS,
        materialize_short_term_replay_indexed_prototype,
        resolve_scorer_short_pit_prototype_gate_columns,
        split_scorer_short_pit_gate_columns,
    )

    gate_columns = resolve_scorer_short_pit_prototype_gate_columns()
    gate_fe_cols, gate_trial_cols = split_scorer_short_pit_gate_columns(gate_columns)
    oracle_out = tmp_path / "oracle_cross.parquet"
    replay_out = tmp_path / "replay_cross.parquet"
    materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=oracle_out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        short_term_columns=gate_fe_cols,
        trial_columns=gate_trial_cols,
        payout_yyyymm=ym,
    )
    materialize_short_term_replay_indexed_prototype(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=replay_out,
        payout_yyyymm=ym,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    replay_df = pq.read_table(replay_out).to_pandas()
    oracle_df = pq.read_table(oracle_out).to_pandas()
    report = compare_replay_to_oracle(
        replay_df,
        oracle_df,
        columns=gate_columns,
    )
    assert report["passed"] is True, report
    r2 = replay_df.loc[replay_df["bet_id"] == 2.0].iloc[0]
    assert int(r2["bet__bets_cnt__w1h"]) == 1
    assert float(r2["bet__wager_sum__w1h"]) == pytest.approx(10.0)
    r3 = replay_df.loc[replay_df["bet_id"] == 3.0].iloc[0]
    assert int(r3["bet__bets_cnt__w1h"]) == 2
    assert float(r3["bet__wager_sum__w1h"]) == pytest.approx(15.0)
    assert float(r3["bet__back_bet_ratio__w1h"]) == pytest.approx(0.5)


def test_indexed_replay_window_boundary_exactly_5m(tmp_path: Path) -> None:
    """A bet exactly 5m before target is inside the DuckDB RANGE lower bound."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    target_pcd = t0 + pd.Timedelta(minutes=20)
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
        _base_row(bet_id=2.0, player_id=10, pcd=target_pcd - pd.Timedelta(minutes=5), wager=50.0),
        _base_row(bet_id=3.0, player_id=10, pcd=target_pcd, wager=25.0),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(3.0,))
    got = replay_df.iloc[0]
    assert int(got["fe__rate__bets_cnt__w5m"]) == 1
    assert int(got["fe__bets_cnt__w15m"]) == 1


def test_indexed_replay_range_ratios_and_velocity(tmp_path: Path) -> None:
    """RANGE ratio and velocity columns match bounded oracle on a dense fixture."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=10.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=3), wager=20.0),
        _base_row(bet_id=3.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=10), wager=30.0),
        _base_row(bet_id=4.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=40), wager=40.0),
        _base_row(bet_id=5.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=50), wager=50.0),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(5.0,))
    got = replay_df.iloc[0]
    assert int(got["fe__rate__bets_cnt__w5m"]) == 0
    assert int(got["fe__bets_cnt__w15m"]) == 1
    assert int(got["fe__bets_cnt__w1d"]) == 4
    assert float(got["fe__wager_sum__w1d"]) == pytest.approx(100.0)
    assert float(got["fe__wager_sum__w15m_over_w1d"]) == pytest.approx(0.4)
    assert float(got["fe__bets_cnt__w15m_over_w1d"]) == pytest.approx(0.25)
    assert float(got["fe__rate__velocity__w15m_over_w1h"]) == pytest.approx(1.0)


def test_indexed_replay_range_ratios_null_when_empty_1d(tmp_path: Path) -> None:
    """Ratio columns are null when the 1d denominator window is empty."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(1.0,))
    got = replay_df.iloc[0]
    assert int(got["fe__bets_cnt__w1d"]) == 0
    assert pd.isna(got["fe__wager_sum__w15m_over_w1d"])
    assert pd.isna(got["fe__bets_cnt__w15m_over_w1d"])
    assert pd.isna(got["fe__rate__velocity__w5m_over_w15m"])


def test_indexed_replay_avg_stddev_z_oracle_parity(tmp_path: Path) -> None:
    """WP-9.3 z-score columns match bounded oracle on a multi-bet fixture."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=10.0, payout_odds=2.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=10),
            wager=30.0,
            payout_odds=4.0,
        ),
        _base_row(
            bet_id=3.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=20),
            wager=50.0,
            payout_odds=6.0,
        ),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(3.0,))
    got = replay_df.iloc[0]
    assert float(got["fe__odds__payout_odds_z__w1h"]) == pytest.approx(3.0)
    assert float(got["fe__stake__wager_z__w1h"]) == pytest.approx(3.0)


def test_indexed_replay_z_null_when_single_prior_row(tmp_path: Path) -> None:
    """Single-row prior window yields STDDEV_POP=0 and null z-scores."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=10.0, payout_odds=2.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=10),
            wager=20.0,
            payout_odds=3.0,
        ),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(2.0,))
    got = replay_df.iloc[0]
    assert pd.isna(got["fe__odds__payout_odds_z__w1h"])
    assert pd.isna(got["fe__stake__wager_z__w1h"])


def test_indexed_replay_z_null_when_null_odds_or_wager(tmp_path: Path) -> None:
    """Null target odds/wager yields null z-score even with valid prior window."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=10.0, payout_odds=2.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=10),
            wager=20.0,
            payout_odds=4.0,
        ),
        _base_row(
            bet_id=3.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=20),
            wager=30.0,
            payout_odds=6.0,
        ),
    ]
    rows[2]["payout_odds"] = None
    rows[2]["wager"] = None
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(3.0,))
    got = replay_df.iloc[0]
    assert pd.isna(got["fe__odds__payout_odds_z__w1h"])
    assert pd.isna(got["fe__stake__wager_z__w1h"])
    assert pd.isna(got["fe__payout_odds_z_prior_w30d"])


def test_indexed_replay_max_ratio_duplicate_max(tmp_path: Path) -> None:
    """Duplicate prior max odds/wager still yield stable recent-max ratios."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0, payout_odds=4.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=10),
            wager=100.0,
            payout_odds=4.0,
        ),
        _base_row(
            bet_id=3.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=20),
            wager=50.0,
            payout_odds=2.0,
        ),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(3.0,))
    got = replay_df.iloc[0]
    assert float(got["fe__odds__payout_odds_to_recent_max_ratio__w1h"]) == pytest.approx(0.5)
    assert float(got["fe__stake__wager_to_recent_max_ratio__w1h"]) == pytest.approx(0.5)


def test_indexed_replay_max_ratio_expired_max(tmp_path: Path) -> None:
    """A prior max outside the 1h window is excluded from recent-max ratios."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=1000.0, payout_odds=100.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(hours=2),
            wager=10.0,
            payout_odds=2.0,
        ),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(2.0,))
    got = replay_df.iloc[0]
    assert pd.isna(got["fe__odds__payout_odds_to_recent_max_ratio__w1h"])
    assert pd.isna(got["fe__stake__wager_to_recent_max_ratio__w1h"])


def test_indexed_replay_max_ratio_ignores_null_prior_values(tmp_path: Path) -> None:
    """Null prior odds/wager are ignored when computing recent-max ratios."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0, payout_odds=2.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=10),
            wager=50.0,
            payout_odds=4.0,
        ),
        _base_row(
            bet_id=3.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=20),
            wager=25.0,
            payout_odds=8.0,
        ),
    ]
    rows[0]["payout_odds"] = None
    rows[0]["wager"] = None
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(3.0,))
    got = replay_df.iloc[0]
    assert float(got["fe__odds__payout_odds_to_recent_max_ratio__w1h"]) == pytest.approx(2.0)
    assert float(got["fe__stake__wager_to_recent_max_ratio__w1h"]) == pytest.approx(0.5)


def test_indexed_replay_interarrival_same_timestamp(tmp_path: Path) -> None:
    """Same-timestamp target gets zero gap and null lag2 when only one prior exists."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0, wager=50.0),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(2.0,))
    got = replay_df.iloc[0]
    assert float(got["fe__time_since_last_bet_sec"]) == pytest.approx(0.0)
    assert pd.isna(got["fe__interarrival__lag2_sec"])
    assert pd.isna(got["fe__interarrival__last_gap_to_recent_mean_ratio__w1h"])
    assert pd.isna(got["fe__interarrival__cv__w1h"])
    assert pd.isna(got["fe__interarrival__last_gap_z__w7d"])


def test_indexed_replay_interarrival_lag2_missing_with_single_prior(tmp_path: Path) -> None:
    """Second bet in sequence has a gap but no lag2 because only one prior row exists."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=100.0),
        _base_row(
            bet_id=2.0,
            player_id=10,
            pcd=t0 + pd.Timedelta(minutes=10),
            wager=50.0,
        ),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(2.0,))
    got = replay_df.iloc[0]
    assert float(got["fe__time_since_last_bet_sec"]) == pytest.approx(600.0)
    assert pd.isna(got["fe__interarrival__lag2_sec"])
    assert pd.isna(got["fe__interarrival__last_gap_to_recent_mean_ratio__w1h"])
    assert pd.isna(got["fe__interarrival__cv__w1h"])
    assert pd.isna(got["fe__interarrival__last_gap_z__w7d"])


def test_indexed_replay_interarrival_long_gap_z7d_oracle_parity(tmp_path: Path) -> None:
    """Long final gap yields finite w7d z-score matching bounded oracle."""
    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=10.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=5), wager=10.0),
        _base_row(bet_id=3.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=15), wager=10.0),
        _base_row(bet_id=4.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=30), wager=10.0),
        _base_row(bet_id=5.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=45), wager=10.0),
        _base_row(bet_id=6.0, player_id=10, pcd=t0 + pd.Timedelta(hours=8), wager=10.0),
    ]
    oracle_df, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(5.0, 6.0))
    r5 = replay_df.loc[replay_df["bet_id"] == 5.0].iloc[0]
    o5 = oracle_df.loc[oracle_df["bet_id"] == 5.0].iloc[0]
    assert float(r5["fe__time_since_last_bet_sec"]) == pytest.approx(900.0)
    assert float(r5["fe__interarrival__lag2_sec"]) == pytest.approx(900.0)
    assert float(r5["fe__interarrival__last_gap_to_recent_mean_ratio__w1h"]) == pytest.approx(
        float(o5["fe__interarrival__last_gap_to_recent_mean_ratio__w1h"]),
    )
    assert float(r5["fe__interarrival__cv__w1h"]) == pytest.approx(
        float(o5["fe__interarrival__cv__w1h"]),
    )
    r6 = replay_df.loc[replay_df["bet_id"] == 6.0].iloc[0]
    assert float(r6["fe__time_since_last_bet_sec"]) == pytest.approx(8 * 3600 - 45 * 60)
    assert abs(float(r6["fe__interarrival__last_gap_z__w7d"])) > 1.0
    # w7d z is mid/composite in production routing; compare via bounded SQL outside gate scope.
    cleaned = tmp_path / "cleaned"
    cmap = tmp_path / "map.parquet"
    train = tmp_path / "train.parquet"
    z_out = tmp_path / "oracle_w7d_z.parquet"
    materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=z_out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        short_term_columns=("fe__interarrival__last_gap_z__w7d",),
        trial_columns=(),
        payout_yyyymm="202406",
    )
    o6 = pq.read_table(z_out).to_pandas().loc[lambda df: df["bet_id"] == 6.0].iloc[0]
    assert float(r6["fe__interarrival__last_gap_z__w7d"]) == pytest.approx(
        float(o6["fe__interarrival__last_gap_z__w7d"]),
    )


def test_indexed_replay_today_same_gaming_day_progression(tmp_path: Path) -> None:
    """Same-gaming-day counters accumulate prior bets in ``pcd, bet_id`` order."""
    hk = "Asia/Hong_Kong"
    t0 = pd.Timestamp("2024-06-01 10:00:00", tz=hk)
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=t0, wager=10.0),
        _base_row(bet_id=2.0, player_id=10, pcd=t0, wager=20.0),
        _base_row(bet_id=3.0, player_id=10, pcd=t0 + pd.Timedelta(minutes=5), wager=30.0),
    ]
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(2.0, 3.0))
    r2 = replay_df.loc[replay_df["bet_id"] == 2.0].iloc[0]
    assert float(r2["fe__canonical__bets_cnt__today"]) == pytest.approx(1.0)
    assert float(r2["fe__canonical__wager_sum__today"]) == pytest.approx(10.0)
    assert float(r2["fe__canonical__avg_wager__today"]) == pytest.approx(10.0)
    assert float(r2["fe__canonical__elapsed_sec_since_first_bet__today"]) == pytest.approx(0.0)
    r3 = replay_df.loc[replay_df["bet_id"] == 3.0].iloc[0]
    assert float(r3["fe__canonical__bets_cnt__today"]) == pytest.approx(2.0)
    assert float(r3["fe__canonical__wager_sum__today"]) == pytest.approx(30.0)
    assert float(r3["fe__canonical__elapsed_sec_since_first_bet__today"]) == pytest.approx(300.0)


def test_indexed_replay_today_respects_gaming_day_boundary(tmp_path: Path) -> None:
    """Prior bets on a different ``gaming_day_event`` do not count toward today."""
    hk = "Asia/Hong_Kong"
    day_a = pd.Timestamp("2024-06-01", tz=hk)
    day_b = pd.Timestamp("2024-06-02", tz=hk)
    rows = [
        _base_row(bet_id=1.0, player_id=10, pcd=day_a + pd.Timedelta(hours=23), wager=10.0),
        _base_row(bet_id=2.0, player_id=10, pcd=day_b + pd.Timedelta(hours=1), wager=20.0),
    ]
    rows[0]["gaming_day_event"] = day_a.date()
    rows[1]["gaming_day_event"] = day_b.date()
    _, replay_df = _oracle_and_indexed(tmp_path, rows, target_bet_ids=(2.0,))
    got = replay_df.iloc[0]
    assert float(got["fe__canonical__bets_cnt__today"]) == pytest.approx(0.0)
    assert float(got["fe__canonical__wager_sum__today"]) == pytest.approx(0.0)
    assert pd.isna(got["fe__canonical__avg_wager__today"])
    assert float(got["fe__canonical__elapsed_sec_since_first_bet__today"]) == pytest.approx(0.0)


def test_indexed_replay_today_derives_gaming_day_from_pcd_when_column_missing() -> None:
    """Pool rows without ``gaming_day_event`` derive HK calendar day from payout time."""
    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        _gaming_day_ordinals,
    )

    hk = "Asia/Hong_Kong"
    pcd = pd.Series(
        [
            pd.Timestamp("2024-05-31 18:00:00", tz="UTC"),
            pd.Timestamp("2024-05-31 19:00:00", tz="UTC"),
        ],
    )
    got = _gaming_day_ordinals(pcd, None, hk_tz=hk)
    hk_day = (
        pd.Timestamp("2024-06-01", tz=hk)
        .to_datetime64()
        .astype("datetime64[D]")
        .astype("int64")
    )
    assert got[0] == hk_day
    assert got[1] == hk_day


def test_scorer_gate_columns_subset_of_indexed_output() -> None:
    """Prototype gate scope must stay within indexed replay output columns."""
    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        INDEXED_PROTOTYPE_OUTPUT_COLUMNS,
        PROTOTYPE_GATE_IGNORE_COLUMNS,
        resolve_production_scorer_short_pit_gate_columns,
        resolve_scorer_short_pit_prototype_gate_columns,
    )

    indexed = set(INDEXED_PROTOTYPE_OUTPUT_COLUMNS)
    gate = resolve_scorer_short_pit_prototype_gate_columns()
    production = resolve_production_scorer_short_pit_gate_columns()
    missing = [c for c in gate if c not in indexed]
    assert not missing, missing
    assert len(production) == 20
    assert len(gate) == 19
    for ignored in PROTOTYPE_GATE_IGNORE_COLUMNS:
        assert ignored in production
        assert ignored not in gate


def _synthetic_parity_report(
    *,
    fe_mismatch_cols: tuple[str, ...] = (),
    bet_waiver_mismatch_count: int = 0,
    compared_rows: int = 500_000,
) -> dict:
    """Build a parity dict and run pinned waiver governance (WP-10 test helper)."""
    from trainer_hightier.config import LEGACY_BET_PACK_1H_COLUMNS
    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        apply_parity_waiver_governance,
        resolve_scorer_short_pit_prototype_gate_columns,
    )

    columns: dict[str, dict[str, object]] = {}
    for col in resolve_scorer_short_pit_prototype_gate_columns():
        mismatch_count = 1 if col in fe_mismatch_cols else 0
        if col in LEGACY_BET_PACK_1H_COLUMNS and bet_waiver_mismatch_count > 0:
            mismatch_count = bet_waiver_mismatch_count
        info: dict[str, object] = {"mismatch_count": mismatch_count}
        if mismatch_count > 0 and col.startswith("bet__"):
            info["sample_bet_ids"] = [12345]
        columns[col] = info
    raw = {"columns": columns, "compared_rows": compared_rows}
    return apply_parity_waiver_governance(raw, compared_rows=compared_rows)


def test_evaluate_full_month_cold_build_gate_decisions() -> None:
    """WP-10 decision thresholds: parity + output validation + 3x speedup."""
    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        evaluate_full_month_cold_build_gate,
    )

    integrate = evaluate_full_month_cold_build_gate(
        parity=_synthetic_parity_report(),
        output_validation_passed=True,
        replay_elapsed_seconds=100.0,
        bounded_elapsed_seconds=350.0,
    )
    assert integrate["decision"] == "integrate_candidate"
    assert integrate["final_integration_met"] is True

    continue_proto = evaluate_full_month_cold_build_gate(
        parity=_synthetic_parity_report(),
        output_validation_passed=True,
        replay_elapsed_seconds=100.0,
        bounded_elapsed_seconds=200.0,
    )
    assert continue_proto["decision"] == "continue_prototype"

    stop = evaluate_full_month_cold_build_gate(
        parity=_synthetic_parity_report(fe_mismatch_cols=("fe__bets_cnt__w15m",)),
        output_validation_passed=True,
        replay_elapsed_seconds=100.0,
        bounded_elapsed_seconds=400.0,
    )
    assert stop["decision"] == "stop_indexed_replay"


def test_apply_parity_waiver_governance_accepts_legacy_bet_pack() -> None:
    """Pinned DL-001 waiver: fe__* hard parity + small bet__* mismatch ratio."""
    parity = _synthetic_parity_report(bet_waiver_mismatch_count=54, compared_rows=600_000)
    assert parity["hard_parity_passed"] is True
    assert parity["waiver_accepted"] is True
    assert parity["passed"] is False

    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        evaluate_full_month_cold_build_gate,
    )

    gate = evaluate_full_month_cold_build_gate(
        parity=parity,
        output_validation_passed=True,
        replay_elapsed_seconds=100.0,
        bounded_elapsed_seconds=350.0,
    )
    assert gate["decision"] == "integrate_candidate_with_bet_pack_waiver"
    assert gate["final_integration_met"] is True


def test_apply_parity_waiver_governance_rejects_excessive_bet_mismatch() -> None:
    """Bet-pack waiver must fail when mismatch ratio exceeds pinned bound."""
    parity = _synthetic_parity_report(bet_waiver_mismatch_count=50_000, compared_rows=500_000)
    assert parity["hard_parity_passed"] is True
    assert parity["waiver_accepted"] is False


def test_unique_int_player_ids_deduplicates_and_sorts() -> None:
    """Player id normalization returns deterministic unique integers."""
    from trainer_hightier.feature_experiment.short_term_pit_replay_prototype import (
        unique_int_player_ids,
    )

    got = unique_int_player_ids(pd.Series([3, 1, 3, None, "2", 1.0]))
    assert got == (1, 2, 3)


def test_load_replay_events_deduplicates_restrict_player_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indexed replay pool load passes unique player ids to hot-pool session."""
    from trainer_hightier.utils import cleaned_bet_pool_read as pool_read

    captured: dict[str, tuple[int, ...]] = {}

    class _FakeSession:
        conn = duckdb.connect(database=":memory:")
        table_name = pool_read.MONTH_HOT_POOL_TABLE

        def close(self) -> None:
            self.conn.close()

    def _fake_open_month_hot_pool_session(
        cleaned_root: Path,
        *,
        payout_yyyymm: str,
        duckdb_runtime: DuckDbRuntimeConfig,
        hk_tz: str,
        restrict_player_ids: tuple[int, ...] | None = None,
        table_name: str = pool_read.MONTH_HOT_POOL_TABLE,
    ) -> _FakeSession:
        del cleaned_root, payout_yyyymm, duckdb_runtime, hk_tz, table_name
        captured["restrict_player_ids"] = tuple(restrict_player_ids or ())
        session = _FakeSession()
        session.conn.execute(
            f"CREATE TEMP TABLE {session.table_name} AS "
            "SELECT 1::DOUBLE AS bet_id, 10::BIGINT AS player_id, "
            "TIMESTAMP '2024-06-01 00:00:00' AS payout_complete_dtm, "
            "TIMESTAMP '2024-06-01' AS gaming_day_event, "
            "1.0::DOUBLE AS wager, 0.0::DOUBLE AS casino_win, 0.0::DOUBLE AS theo_win, "
            "0::INTEGER AS is_back_bet, 1.5::DOUBLE AS payout_odds "
            "WHERE FALSE",
        )
        return session

    monkeypatch.setattr(
        pool_read,
        "open_month_hot_pool_session",
        _fake_open_month_hot_pool_session,
    )
    from trainer_hightier.feature_experiment.short_term_pit_replay_prototype import (
        _load_replay_events,
    )

    _load_replay_events(
        tmp_path,
        payout_yyyymm="202406",
        player_ids=(10, 20, 10, 20, 10),
        duckdb_runtime=DuckDbRuntimeConfig(),
        hk_tz="Asia/Hong_Kong",
    )
    assert captured["restrict_player_ids"] == (10, 20)


def test_indexed_replay_whale_benchmark_reports_phase_timings(tmp_path: Path) -> None:
    """Synthetic whale fixture exposes emit phase metrics for benchmarking."""
    from trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype import (
        materialize_short_term_replay_indexed_prototype,
    )

    t0 = pd.Timestamp("2024-06-01 06:00:00", tz="UTC")
    rows = [
        _base_row(
            bet_id=float(i),
            player_id=10,
            pcd=t0 + pd.Timedelta(seconds=i),
            wager=10.0 + float(i % 7),
            payout_odds=1.5 + float(i % 3) * 0.1,
        )
        for i in range(1, 1201)
    ]
    cleaned, cmap, train, ym = _write_fixture(
        tmp_path,
        rows,
        target_bet_ids=tuple(float(i) for i in range(1, 1201)),
    )
    out = tmp_path / "whale_replay.parquet"
    _, metrics = materialize_short_term_replay_indexed_prototype(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=out,
        payout_yyyymm=ym,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    assert metrics["max_entity_target_rows"] == 1200
    assert metrics["unique_target_player_ids"] == 1
    assert metrics["target_player_id_rows"] == 1200
    assert metrics["emit_entity_count"] == 1
    assert "phase_timings" in metrics
    assert metrics["phase_timings"]["emit_s"] >= 0.0
