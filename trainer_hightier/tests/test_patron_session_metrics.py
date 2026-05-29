"""Patron-level session aggregates (canonical ADT report)."""

from __future__ import annotations

import csv
from datetime import date

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.patron_session_metrics import (
    compile_canonical_patron_profile_csv,
    compile_canonical_patron_session_metrics,
)


def _sess_row(pid: int, sid: int, theo: float, gd: date) -> dict:
    t = pd.Timestamp("2024-06-01 12:00:00")
    return {
        "session_id": sid,
        "player_id": pid,
        "casino_player_id": str(pid),
        "lud_dtm": t,
        "session_start_dtm": t,
        "session_end_dtm": t,
        "gaming_day_event": gd,
        "theo_win": theo,
        "is_manual": 0,
        "is_deleted": 0,
        "is_canceled": 0,
        "num_games_with_wager": 1,
        "turnover": 1.0,
        "player_win": float(pid),
        "cash_buyins": 10.0,
        "num_bets": 2,
    }


def test_patron_session_metrics_adt_sorted_desc(tmp_path) -> None:
    """ADT = total_theo_win / distinct gaming_days; sort high ADT first."""
    cleaned = tmp_path / "cleaned.parquet"
    mp = tmp_path / "map.parquet"
    out_pq = tmp_path / "adt.parquet"

    df_s = pd.DataFrame(
        [
            _sess_row(10, 1, 100.0, date(2024, 1, 1)),
            _sess_row(10, 2, 50.0, date(2024, 1, 2)),
            _sess_row(20, 3, 30.0, date(2024, 1, 1)),
        ]
    )
    pq.write_table(pa.Table.from_pandas(df_s), cleaned)

    df_m = pd.DataFrame(
        {
            "player_id": [10, 20],
            "canonical_id": ["HIGH_ADT", "LOW_ADT"],
        }
    )
    pq.write_table(pa.Table.from_pandas(df_m), mp)

    compile_canonical_patron_session_metrics(
        cleaned,
        mp,
        duckdb_runtime=DuckDbRuntimeConfig(),
        output_parquet=out_pq,
        duckdb_join_timeout_s=120.0,
    )

    got = pd.read_parquet(out_pq)
    assert list(got.columns) == ["canonical_id", "total_theo_win", "gaming_days", "adt"]
    assert got.iloc[0]["canonical_id"] == "HIGH_ADT"
    assert float(got.iloc[0]["total_theo_win"]) == 150.0
    assert int(got.iloc[0]["gaming_days"]) == 2
    assert abs(float(got.iloc[0]["adt"]) - 75.0) < 1e-9
    assert got.iloc[1]["canonical_id"] == "LOW_ADT"
    assert float(got.iloc[1]["adt"]) == 30.0


def test_patron_session_metrics_requires_theo_and_gaming_day(tmp_path) -> None:
    cleaned = tmp_path / "cleaned.parquet"
    mp = tmp_path / "map.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame({"player_id": [1], "session_id": [1]})), cleaned)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame({"player_id": [1], "canonical_id": ["a"]})), mp)
    try:
        compile_canonical_patron_session_metrics(
            cleaned,
            mp,
            duckdb_runtime=DuckDbRuntimeConfig(),
            output_parquet=tmp_path / "out.parquet",
            duckdb_join_timeout_s=60.0,
        )
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "missing columns" in str(e).lower()


def test_patron_profile_csv_aggregates_by_canonical_id(tmp_path) -> None:
    """Full patron profile CSV: sums, counts, gaming-day span, ADT."""
    cleaned = tmp_path / "cleaned.parquet"
    mp = tmp_path / "map.parquet"
    out_csv = tmp_path / "profile.csv"

    df_s = pd.DataFrame(
        [
            _sess_row(10, 1, 100.0, date(2024, 1, 1)),
            _sess_row(10, 2, 50.0, date(2024, 1, 2)),
            _sess_row(20, 3, 30.0, date(2024, 1, 1)),
        ]
    )
    pq.write_table(pa.Table.from_pandas(df_s), cleaned)
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame({"player_id": [10, 20], "canonical_id": ["HIGH_ADT", "LOW_ADT"]})
        ),
        mp,
    )

    compile_canonical_patron_profile_csv(
        cleaned,
        mp,
        duckdb_runtime=DuckDbRuntimeConfig(),
        output_csv=out_csv,
        duckdb_join_timeout_s=120.0,
    )

    with out_csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    by_id = {r["canonical_id"]: r for r in rows}
    h = by_id["HIGH_ADT"]
    assert abs(float(h["total_theo_win"]) - 150.0) < 1e-9
    assert abs(float(h["total_turnover"]) - 2.0) < 1e-9
    assert abs(float(h["total_cash_buyins"]) - 20.0) < 1e-9
    assert abs(float(h["total_player_win"]) - 20.0) < 1e-9
    assert int(float(h["unique_gaming_days"])) == 2
    assert int(float(h["total_num_bets"])) == 4
    assert int(float(h["session_count"])) == 2
    assert str(h["first_gaming_day"]) <= str(h["last_gaming_day"])
    assert abs(float(h["adt"]) - 75.0) < 1e-9

    ell = by_id["LOW_ADT"]
    assert abs(float(ell["adt"]) - 30.0) < 1e-9
    assert int(float(ell["session_count"])) == 1


def test_slow_patron_canonical_active_month_single_anchor(tmp_path) -> None:
    """Canonical active-month materializer emits one global anchor row per patron."""
    from trainer_hightier.utils.slow_patron_180d_monthly import materialize_slow_patron_canonical_active_month

    gd1 = date(2024, 1, 5)
    gd2 = date(2024, 1, 12)
    gd3 = date(2024, 2, 2)
    sess = pd.DataFrame(
        [
            _sess_row(100, 1, 100.0, gd1),
            _sess_row(100, 2, 50.0, gd2),
            _sess_row(100, 3, 200.0, gd3),
        ]
    )
    mp = tmp_path / "map.parquet"
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame({"player_id": [100], "canonical_id": ["c1"]})),
        mp,
    )
    spq = tmp_path / "sess.parquet"
    pq.write_table(pa.Table.from_pandas(sess), spq)
    out = tmp_path / "slow_canonical.parquet"

    materialize_slow_patron_canonical_active_month(
        cleaned_session_parquet=spq,
        canonical_mapping_parquet=mp,
        out_parquet=out,
        context_day=date(2024, 6, 15),
        lookback_days=180,
    )
    got = pd.read_parquet(out)
    assert len(got) == 1
    assert str(got.iloc[0]["canonical_id"]) == "c1"
    anchors = pd.to_datetime(got["anchor_gaming_day_event"]).dt.date.tolist()
    assert anchors == [date(2024, 5, 31)]
    assert "event_timestamp" in got.columns
    assert pd.notna(got.iloc[0]["event_timestamp"])


def test_slow_patron_180d_monthly_bet_grain_diagnostic_assigns_latest_anchor(tmp_path) -> None:
    """Diagnostic bet-grain path still supports multi-anchor ASOF (non-deploy)."""
    from trainer_hightier.utils.slow_patron_180d_monthly import (
        materialize_slow_patron_180d_monthly_bet_grain_diagnostic,
    )

    gd1 = date(2024, 1, 5)
    gd2 = date(2024, 1, 12)
    gd3 = date(2024, 2, 2)
    sess = pd.DataFrame(
        [
            _sess_row(100, 1, 100.0, gd1),
            _sess_row(100, 2, 50.0, gd2),
            _sess_row(100, 3, 200.0, gd3),
        ]
    )
    mp = tmp_path / "map.parquet"
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame({"player_id": [100], "canonical_id": ["c1"]})),
        mp,
    )
    spq = tmp_path / "sess.parquet"
    pq.write_table(pa.Table.from_pandas(sess), spq)

    pit = pd.Timestamp("2024-06-01 12:00:00+08:00")

    def _bet(bid: float, pgd: date) -> dict:
        pay = pd.Timestamp(pgd.year, pgd.month, pgd.day, 14, 0, 0, tz="Asia/Hong_Kong")
        return {
            "bet_id": bid,
            "session_id": 1.0,
            "player_id": 100,
            "game_id": 1.0,
            "table_id": 1.0,
            "payout_complete_dtm": pay,
            "__etl_insert_Dtm": pay,
            "wager": 1.0,
            "wager_nn": 0.0,
            "status": "WIN",
            "casino_win": 0.0,
            "payout_odds": 1.0,
            "payout_ha": 0.0,
            "base_ha": 0.0,
            "is_back_bet": 0,
            "gaming_day_event": pgd,
            "prediction_visible_ts_cf": pit,
            "__etl_insert_Dtm_synthetic": pit,
        }

    bets = pd.DataFrame([_bet(1.0, date(2024, 1, 3)), _bet(2.0, date(2024, 1, 20)), _bet(3.0, date(2024, 2, 10))])
    bpq = tmp_path / "bet.parquet"
    pq.write_table(pa.Table.from_pandas(bets), bpq)
    out = tmp_path / "slow180.parquet"

    materialize_slow_patron_180d_monthly_bet_grain_diagnostic(
        cleaned_session_parquet=spq,
        canonical_mapping_parquet=mp,
        cleaned_bet_parquet=bpq,
        out_parquet=out,
        lookback_days=180,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    got = pd.read_parquet(out).sort_values("bet_id", kind="mergesort")

    assert len(got) == 3
    r0 = got.iloc[0]
    assert pd.isna(r0["patron__theo_win_sum__w180d_m1snap"])
    r1 = got.iloc[1]
    assert float(r1["patron__theo_win_sum__w180d_m1snap"]) == 150.0
    assert int(r1["patron__gaming_days_cnt__w180d_m1snap"]) == 2
    assert float(r1["patron__adt__w180d_m1snap"]) == 75.0
    r2 = got.iloc[2]
    assert abs(float(r2["patron__theo_win_sum__w180d_m1snap"]) - 350.0) < 1e-6
    assert int(r2["patron__gaming_days_cnt__w180d_m1snap"]) == 3
    assert abs(float(r2["patron__adt__w180d_m1snap"]) - 350.0 / 3.0) < 1e-6


def test_adt_allowlist_excludes_canonical_without_slow_session_window(tmp_path) -> None:
    """High-ADT allowlist drops patrons with no session in the slow lookback window."""
    from trainer_hightier.utils.patron_session_metrics import materialize_adt_allowed_players_parquet

    cleaned = tmp_path / "cleaned.parquet"
    mp = tmp_path / "map.parquet"
    profile = tmp_path / "profile.csv"
    anchor = date(2026, 4, 30)

    pd.DataFrame(
        [
            _sess_row(10, 1, 500.0, date(2026, 4, 15)),
            _sess_row(20, 2, 500.0, date(2026, 5, 10)),
        ]
    ).to_parquet(cleaned, index=False)
    pd.DataFrame(
        {"player_id": [10, 20], "canonical_id": ["in_window", "may_only"]},
    ).to_parquet(mp, index=False)
    with profile.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["canonical_id", "adt"])
        writer.writeheader()
        writer.writerow({"canonical_id": "in_window", "adt": "500.0"})
        writer.writerow({"canonical_id": "may_only", "adt": "500.0"})

    out = tmp_path / "allow.parquet"
    materialize_adt_allowed_players_parquet(
        profile,
        mp,
        quantile=0.01,
        duckdb_runtime=DuckDbRuntimeConfig(),
        output_parquet=out,
        cleaned_session_parquet=cleaned,
        slow_active_anchor=anchor,
        slow_lookback_days=180,
    )
    allowed = pd.read_parquet(out)
    assert set(allowed["canonical_id"].astype(str)) == {"in_window"}
