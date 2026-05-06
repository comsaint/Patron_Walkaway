"""Near-golden E2E for ``python -m parallel_lda_mvp.run_mvp`` on synthetic Parquet."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd
import pytest

from parallel_lda_mvp.run_mvp import repo_root
from tests.unit.test_parallel_lda_mvp_run_mvp import canonical_parquet_digest_sorted

pytestmark = pytest.mark.slow

_GAMING_YM = "2024-07"

_MVP_SUMMARY_IGNORE_KEYS: frozenset[str] = frozenset(
    {
        "eligible_parquet",
        "ingestion_fix_registry_yaml",
        "output_root",
        "session_parquet_for_mapping",
        "snap_root",
        # Includes per-file mtime_ns; changes when outputs are rewritten on rerun.
        "span_run_fact_input_fingerprint",
        "t_bet_paths",
        "t_session",
    }
)


def _e2e_snapshot_id() -> str:
    """Return a valid ``source_snapshot_id`` unique per xdist worker."""
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main").encode("utf-8")
    tail = hashlib.sha1(wid).hexdigest()[:12]
    return f"snap_mvp_e2e_{tail}"


def _write_synthetic_mvp_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Materialize minimal ``t_bet`` / ``t_session`` Parquet for one MVP month."""
    t_bet = tmp_path / "synth_t_bet.parquet"
    t_session = tmp_path / "synth_t_session.parquet"

    # Sessions: two players, two sessions each (avoids FND-12 dummy single-session rule).
    sess_rows = [
        {
            "session_id": 100_010,
            "player_id": 880_001,
            "casino_player_id": "CP880001A",
            "lud_dtm": pd.Timestamp("2024-06-10 12:00:00"),
            "__etl_insert_Dtm": pd.Timestamp("2024-06-10 12:00:00"),
            "session_start_dtm": pd.Timestamp("2024-06-10 11:00:00"),
            "session_end_dtm": pd.Timestamp("2024-06-10 13:00:00"),
            "is_manual": 0,
            "is_deleted": 0,
            "is_canceled": 0,
            "num_games_with_wager": 4,
            "turnover": 400.0,
        },
        {
            "session_id": 100_011,
            "player_id": 880_001,
            "casino_player_id": "CP880001B",
            "lud_dtm": pd.Timestamp("2024-06-11 12:00:00"),
            "__etl_insert_Dtm": pd.Timestamp("2024-06-11 12:00:00"),
            "session_start_dtm": pd.Timestamp("2024-06-11 11:00:00"),
            "session_end_dtm": pd.Timestamp("2024-06-11 13:00:00"),
            "is_manual": 0,
            "is_deleted": 0,
            "is_canceled": 0,
            "num_games_with_wager": 3,
            "turnover": 300.0,
        },
        {
            "session_id": 200_010,
            "player_id": 880_002,
            "casino_player_id": "CP880002A",
            "lud_dtm": pd.Timestamp("2024-06-09 09:00:00"),
            "__etl_insert_Dtm": pd.Timestamp("2024-06-09 09:00:00"),
            "session_start_dtm": pd.Timestamp("2024-06-09 08:00:00"),
            "session_end_dtm": pd.Timestamp("2024-06-09 10:00:00"),
            "is_manual": 0,
            "is_deleted": 0,
            "is_canceled": 0,
            "num_games_with_wager": 5,
            "turnover": 500.0,
        },
        {
            "session_id": 200_011,
            "player_id": 880_002,
            "casino_player_id": "CP880002B",
            "lud_dtm": pd.Timestamp("2024-06-12 15:00:00"),
            "__etl_insert_Dtm": pd.Timestamp("2024-06-12 15:00:00"),
            "session_start_dtm": pd.Timestamp("2024-06-12 14:00:00"),
            "session_end_dtm": pd.Timestamp("2024-06-12 16:00:00"),
            "is_manual": 0,
            "is_deleted": 0,
            "is_canceled": 0,
            "num_games_with_wager": 2,
            "turnover": 200.0,
        },
    ]
    pd.DataFrame(sess_rows).to_parquet(t_session, index=False)

    def _bet(
        bet_id: int,
        session_id: int,
        player_id: int,
        gd: str,
        payout: str,
        etl: str,
        wager: float,
        casino_win: float,
    ) -> dict[str, Any]:
        return {
            "bet_id": bet_id,
            "session_id": session_id,
            "player_id": player_id,
            "game_id": 7001,
            "table_id": 9001,
            "payout_complete_dtm": pd.Timestamp(payout),
            "gaming_day": pd.Timestamp(gd).date(),
            "wager": wager,
            "status": "S",
            "casino_win": casino_win,
            "payout_odds": 2.0,
            "base_ha": 0.0,
            "is_back_bet": 0,
            "position_idx": 0,
            "__etl_insert_Dtm": pd.Timestamp(etl),
            "is_deleted": 0,
            "is_canceled": 0,
            "is_manual": 0,
        }

    # 880_001: same run (15m gap), new run (>30m), multi-day run (Jul 5).
    # 880_002: same-day chain, then Jul 20 (>=3 empty calendar days vs Jul 2 → new trip).
    bet_rows = [
        _bet(
            880_001_0001,
            100_010,
            880_001,
            "2024-07-01",
            "2024-07-01 10:00:00",
            "2024-07-01 10:05:00",
            10.0,
            -1.0,
        ),
        _bet(
            880_001_0002,
            100_010,
            880_001,
            "2024-07-01",
            "2024-07-01 10:15:00",
            "2024-07-01 10:18:00",
            5.0,
            -0.5,
        ),
        _bet(
            880_001_0003,
            100_011,
            880_001,
            "2024-07-01",
            "2024-07-01 11:40:00",
            "2024-07-01 11:42:00",
            8.0,
            0.0,
        ),
        _bet(
            880_001_0004,
            100_011,
            880_001,
            "2024-07-05",
            "2024-07-05 09:00:00",
            "2024-07-05 09:02:00",
            20.0,
            -2.0,
        ),
        _bet(
            880_002_0001,
            200_010,
            880_002,
            "2024-07-02",
            "2024-07-02 08:00:00",
            "2024-07-02 08:01:00",
            12.0,
            -1.2,
        ),
        _bet(
            880_002_0002,
            200_010,
            880_002,
            "2024-07-02",
            "2024-07-02 08:20:00",
            "2024-07-02 08:22:00",
            6.0,
            0.0,
        ),
        _bet(
            880_002_0003,
            200_011,
            880_002,
            "2024-07-20",
            "2024-07-20 12:00:00",
            "2024-07-20 12:03:00",
            15.0,
            -3.0,
        ),
    ]
    pd.DataFrame(bet_rows).to_parquet(t_bet, index=False)
    return t_bet, t_session


def _list_outputs_with_manifests(month_out_root: Path, stem_prefix: str) -> list[Path]:
    """Return sorted Parquet paths matching ``{stem_prefix}__*.parquet`` with sibling manifest."""
    out: list[Path] = []
    d = month_out_root / stem_prefix
    if not d.is_dir():
        return out
    for p in sorted(d.glob(f"{stem_prefix}__*.parquet")):
        if p.is_file() and p.with_name(p.stem + ".manifest.json").is_file():
            out.append(p)
    return out


def _digest_bundle(out_ym: Path) -> dict[str, str]:
    """Map relative POSIX path (under ``snap_root``) → digest for run/trip artifacts."""
    snap_root = out_ym.parent
    dig: dict[str, str] = {}
    for prefix in ("run_fact", "trip_fact", "trip_run_map"):
        for p in _list_outputs_with_manifests(out_ym, prefix):
            rel = p.resolve().relative_to(snap_root.resolve()).as_posix()
            dig[rel] = canonical_parquet_digest_sorted(p)
    return dict(sorted(dig.items()))


def _load_run_ids(con: duckdb.DuckDBPyConnection, paths: Iterable[Path]) -> set[str]:
    ids: set[str] = set()
    for p in paths:
        rows = con.execute(
            "SELECT CAST(run_id AS VARCHAR) FROM read_parquet(?)",
            [str(p.resolve())],
        ).fetchall()
        for (r,) in rows:
            ids.add(str(r))
    return ids


def _assert_trip_run_references_run_fact(out_ym: Path) -> None:
    """``trip_run_map`` / ``trip_fact`` run ids must exist in span ``run_fact``."""
    run_paths = _list_outputs_with_manifests(out_ym, "run_fact")
    trip_map_paths = _list_outputs_with_manifests(out_ym, "trip_run_map")
    trip_fact_paths = _list_outputs_with_manifests(out_ym, "trip_fact")
    assert run_paths, "expected run_fact parquets"
    assert trip_map_paths, "expected trip_run_map parquets"
    assert trip_fact_paths, "expected trip_fact parquets"
    con = duckdb.connect()
    try:
        run_ids = _load_run_ids(con, run_paths)
        for p in trip_map_paths:
            rows = con.execute(
                "SELECT CAST(run_id AS VARCHAR) FROM read_parquet(?)",
                [str(p.resolve())],
            ).fetchall()
            for (rid,) in rows:
                assert str(rid) in run_ids, f"trip_run_map run_id {rid!r} missing in run_fact"
        for p in trip_fact_paths:
            row = con.execute(
                "SELECT CAST(first_run_id AS VARCHAR), CAST(last_run_id AS VARCHAR) "
                "FROM read_parquet(?) LIMIT 1000",
                [str(p.resolve())],
            ).fetchall()
            for first, last in row:
                assert str(first) in run_ids, f"trip_fact first_run_id {first!r} not in run_fact"
                assert str(last) in run_ids, f"trip_fact last_run_id {last!r} not in run_fact"
    finally:
        con.close()


def _summary_compare_dict(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k not in _MVP_SUMMARY_IGNORE_KEYS}


def _run_mvp_subprocess(*, cwd: Path, env: dict[str, str]) -> None:
    cmd = [sys.executable, "-m", "parallel_lda_mvp.run_mvp"]
    p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(
            "run_mvp failed\n"
            f"rc={p.returncode}\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}"
        )


@pytest.fixture()
def mvp_e2e_synth_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Paths to synthetic ``t_bet`` / ``t_session``."""
    return _write_synthetic_mvp_inputs(tmp_path)


def test_run_mvp_e2e_near_golden_and_rerun_stable(mvp_e2e_synth_inputs: tuple[Path, Path]) -> None:
    """Full pipeline: outputs present, referential checks, digest + summary stability on rerun."""
    root = repo_root()
    data_root = root / "data"
    snap = _e2e_snapshot_id()
    snap_root = data_root / "parallel_lda_mvp" / snap
    t_bet, t_session = mvp_e2e_synth_inputs

    env = os.environ.copy()
    env["PARALLEL_LDA_MVP_T_BET"] = str(t_bet.resolve())
    env["PARALLEL_LDA_MVP_T_SESSION"] = str(t_session.resolve())
    env["PARALLEL_LDA_MVP_GAMING_YM"] = _GAMING_YM
    env["PARALLEL_LDA_MVP_SNAPSHOT_ID"] = snap
    env["PARALLEL_LDA_MVP_FORCE_RECOMPUTE"] = "1"

    shutil.rmtree(snap_root, ignore_errors=True)

    try:
        _run_mvp_subprocess(cwd=root, env=env)
        out_ym = snap_root / f"gaming_ym={_GAMING_YM}"
        summary_path = out_ym / "mvp_summary.json"
        assert summary_path.is_file(), f"missing {summary_path}"

        run_paths = _list_outputs_with_manifests(out_ym, "run_fact")
        trip_fact_paths = _list_outputs_with_manifests(out_ym, "trip_fact")
        trip_map_paths = _list_outputs_with_manifests(out_ym, "trip_run_map")
        assert len(run_paths) >= 1
        assert len(trip_fact_paths) >= 1
        assert len(trip_map_paths) >= 1

        _assert_trip_run_references_run_fact(out_ym)

        dig1 = _digest_bundle(out_ym)
        sum1 = _summary_compare_dict(json.loads(summary_path.read_text(encoding="utf-8")))

        _run_mvp_subprocess(cwd=root, env=env)

        dig2 = _digest_bundle(out_ym)
        sum2 = _summary_compare_dict(json.loads(summary_path.read_text(encoding="utf-8")))
        assert dig1 == dig2, "parquet canonical digests differ between identical reruns"
        assert sum1 == sum2, "mvp_summary (path-stripped) differs between identical reruns"
    finally:
        shutil.rmtree(snap_root, ignore_errors=True)
