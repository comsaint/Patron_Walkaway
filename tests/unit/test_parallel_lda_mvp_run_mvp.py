"""Unit tests for ``parallel_lda_mvp.run_mvp`` orchestration helpers."""

from __future__ import annotations

from pathlib import Path

from parallel_lda_mvp.run_mvp import _trip_argv


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
