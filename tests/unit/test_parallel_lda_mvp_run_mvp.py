"""Unit tests for ``parallel_lda_mvp.run_mvp`` orchestration helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb

from parallel_lda_mvp.run_mvp import (
    _compute_month_bet_shas_for_span,
    _read_month_bet_sha_cache,
    _t_bet_month_content_sha256,
    _t_bet_paths_input_fingerprint,
    _trip_argv,
    _write_month_bet_sha_cache,
)


def test_trip_argv_includes_span_run_facts_and_coverage_end(tmp_path: Path) -> None:
    """Trip CLI must receive all span ``run_fact`` inputs and explicit ``--coverage-end``."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    span = [tmp_path / f"run_fact__2026-10-{d:02d}.parquet" for d in (30, 31)]
    span.append(tmp_path / "run_fact__2026-11-01.parquet")
    for p in span:
        p.write_bytes(b"x")
    tf = tmp_path / "trip_fact__2026-10-30.parquet"
    tm = tmp_path / "trip_run_map__2026-10-30.parquet"
    cmd = _trip_argv(
        py="python",
        trip_start_day="2026-10-30",
        trip_fact_parquet=tf,
        trip_run_map_parquet=tm,
        run_fact_paths=span,
        snap="snap_test",
        data_root=data_root,
        coverage_end_day="2026-11-30",
    )
    assert cmd.count("--input-run-fact") == len(span)
    assert "--coverage-end" in cmd
    i = cmd.index("--coverage-end")
    assert cmd[i + 1] == "2026-11-30"
    assert "--trip-fact-output-parquet" in cmd
    assert "--trip-run-map-output-parquet" in cmd


def test_t_bet_paths_input_fingerprint_order_invariant(tmp_path: Path) -> None:
    """Fingerprint must not depend on path list order."""
    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    fp1 = _t_bet_paths_input_fingerprint([a, b])
    fp2 = _t_bet_paths_input_fingerprint([b, a])
    assert fp1 == fp2


def test_month_bet_sha_cache_merge_roundtrip(tmp_path: Path) -> None:
    """On-disk cache merges months under the same ``t_bet`` fingerprint."""
    scratch = tmp_path / "mvp_scratch"
    fp = hashlib.sha256(b"inputs").hexdigest()
    h1 = hashlib.sha256(b"one").hexdigest()
    h2 = hashlib.sha256(b"two").hexdigest()
    _write_month_bet_sha_cache(scratch, t_bet_inputs_fingerprint=fp, span_by_ym={"2024-07": h1})
    _write_month_bet_sha_cache(scratch, t_bet_inputs_fingerprint=fp, span_by_ym={"2024-08": h2})
    got = _read_month_bet_sha_cache(scratch)
    assert got is not None
    assert got[0] == fp
    assert got[1]["2024-07"] == h1
    assert got[1]["2024-08"] == h2


def test_compute_month_bet_shas_matches_legacy(tmp_path: Path) -> None:
    """Batch + cache path must match per-month SHA for a tiny Parquet."""
    pq = tmp_path / "t_bet.parquet"
    scratch = tmp_path / "scratch"
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            COPY (
              SELECT
                CAST('2024-07-03' AS DATE) AS gaming_day,
                CAST(10 AS BIGINT) AS bet_id,
                CAST(20 AS BIGINT) AS player_id
            ) TO '{str(pq.resolve()).replace(chr(39), chr(39) + chr(39))}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    ym = "2024-07"
    legacy = _t_bet_month_content_sha256(ym, [pq], scratch)
    by_ym, stats = _compute_month_bet_shas_for_span([ym], [pq], scratch, force=True)
    assert by_ym[ym] == legacy
    assert stats["recomputed"] == 1
    by2, st2 = _compute_month_bet_shas_for_span([ym], [pq], scratch, force=False)
    assert by2[ym] == legacy
    assert st2["cache_hits"] == 1
    assert st2["recomputed"] == 0
