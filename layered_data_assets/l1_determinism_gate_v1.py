"""Gate 1 L1 determinism (implementation plan §8.1 item 1；LDA-E1-08).

同輸入、不同 DuckDB ``memory_limit``／``threads``（§7.1 執行參數）下，比對列數與 row-level canonical fingerprint。
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from layered_data_assets.oom_runner_v1 import apply_duckdb_resource_pragmas
from layered_data_assets.run_bet_map_v1 import materialize_run_bet_map_v1
from layered_data_assets.run_day_bridge_v1 import materialize_run_day_bridge_v1
from layered_data_assets.run_fact_v1 import RUN_BREAK_MIN_DEFAULT, materialize_run_fact_v1

ArtifactKind = Literal["run_fact", "run_bet_map", "run_day_bridge"]

EMPTY_ROW_AGG_SHA256_HEX = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

GATE1_DEFAULT_DUCKDB_PROFILES: list[tuple[int | None, int]] = [
    (None, 2),
    (512, 2),
    (256, 2),
    (128, 2),
]


def parquet_path_sql_literal(path: Path) -> str:
    """Escape a filesystem path for embedding in single-quoted SQL."""
    return str(path.resolve()).replace("'", "''")


def run_fact_parquet_row_fingerprint(con: Any, parquet_path: Path) -> tuple[int, str]:
    """Return ``(row_count, hex(sha256(sorted_row_canonical_concat)))`` for ``run_fact`` Parquet."""
    p = parquet_path_sql_literal(parquet_path)
    sql = f"""
SELECT
  COUNT(*)::BIGINT,
  hex(sha256(COALESCE(string_agg(row_line, chr(2) ORDER BY run_id), '')))
FROM (
  SELECT
    concat_ws(
      chr(1),
      run_id,
      CAST(player_id AS VARCHAR),
      CAST(first_bet_id AS VARCHAR),
      CAST(last_bet_id AS VARCHAR),
      strftime(run_start_ts, '%Y-%m-%dT%H:%M:%S.%f'),
      strftime(run_end_ts, '%Y-%m-%dT%H:%M:%S.%f'),
      CAST(run_end_gaming_day AS VARCHAR),
      CAST(bet_count AS VARCHAR),
      run_definition_version,
      source_namespace
    ) AS row_line,
    run_id
  FROM read_parquet('{p}')
) t
"""
    row = con.execute(sql).fetchone()
    if row is None:
        raise RuntimeError("run_fact fingerprint query returned no row")
    n, h = row
    return int(n) if n is not None else 0, str(h) if h is not None else EMPTY_ROW_AGG_SHA256_HEX


def run_bet_map_parquet_row_fingerprint(con: Any, parquet_path: Path) -> tuple[int, str]:
    """Return ``(row_count, row_fingerprint_hex)`` for ``run_bet_map`` Parquet."""
    p = parquet_path_sql_literal(parquet_path)
    sql = f"""
SELECT
  COUNT(*)::BIGINT,
  hex(sha256(COALESCE(string_agg(row_line, chr(2) ORDER BY run_id, payout_complete_dtm, bet_id), '')))
FROM (
  SELECT
    concat_ws(
      chr(1),
      run_id,
      CAST(bet_id AS VARCHAR),
      CAST(player_id AS VARCHAR),
      strftime(payout_complete_dtm, '%Y-%m-%dT%H:%M:%S.%f'),
      bet_gaming_day,
      run_end_gaming_day
    ) AS row_line,
    run_id,
    payout_complete_dtm,
    bet_id
  FROM read_parquet('{p}')
) t
"""
    row = con.execute(sql).fetchone()
    if row is None:
        raise RuntimeError("run_bet_map fingerprint query returned no row")
    n, h = row
    return int(n) if n is not None else 0, str(h) if h is not None else EMPTY_ROW_AGG_SHA256_HEX


def run_day_bridge_parquet_row_fingerprint(con: Any, parquet_path: Path) -> tuple[int, str]:
    """Return ``(row_count, row_fingerprint_hex)`` for ``run_day_bridge`` Parquet."""
    p = parquet_path_sql_literal(parquet_path)
    sql = f"""
SELECT
  COUNT(*)::BIGINT,
  hex(sha256(COALESCE(string_agg(row_line, chr(2) ORDER BY run_id, player_id), '')))
FROM (
  SELECT
    concat_ws(
      chr(1),
      run_id,
      CAST(player_id AS VARCHAR),
      bet_gaming_day,
      run_end_gaming_day,
      strftime(run_start_ts, '%Y-%m-%dT%H:%M:%S.%f'),
      strftime(run_end_ts, '%Y-%m-%dT%H:%M:%S.%f')
    ) AS row_line,
    run_id,
    player_id
  FROM read_parquet('{p}')
) t
"""
    row = con.execute(sql).fetchone()
    if row is None:
        raise RuntimeError("run_day_bridge fingerprint query returned no row")
    n, h = row
    return int(n) if n is not None else 0, str(h) if h is not None else EMPTY_ROW_AGG_SHA256_HEX


def _fingerprint_for_artifact(con: Any, artifact: ArtifactKind, parquet_path: Path) -> tuple[int, str]:
    if artifact == "run_fact":
        return run_fact_parquet_row_fingerprint(con, parquet_path)
    if artifact == "run_bet_map":
        return run_bet_map_parquet_row_fingerprint(con, parquet_path)
    if artifact == "run_day_bridge":
        return run_day_bridge_parquet_row_fingerprint(con, parquet_path)
    raise ValueError(f"unknown artifact: {artifact!r}")


def _materialize_one_profile_run_fact(
    duckdb_module: Any,
    *,
    input_paths: list[Path],
    output_parquet: Path,
    run_end_gaming_day: str,
    run_break_min: float,
    memory_limit_mb: int | None,
    threads: int,
) -> dict[str, Any]:
    con = duckdb_module.connect(database=":memory:")
    try:
        apply_duckdb_resource_pragmas(con, memory_limit_mb=memory_limit_mb, threads=threads)
        return materialize_run_fact_v1(
            con=con,
            input_paths=input_paths,
            output_parquet=output_parquet,
            run_end_gaming_day=run_end_gaming_day,
            run_break_min=run_break_min,
        )
    finally:
        con.close()


def _materialize_one_profile_run_bet_map(
    duckdb_module: Any,
    *,
    input_paths: list[Path],
    output_parquet: Path,
    run_end_gaming_day: str,
    run_break_min: float,
    memory_limit_mb: int | None,
    threads: int,
) -> dict[str, Any]:
    con = duckdb_module.connect(database=":memory:")
    try:
        apply_duckdb_resource_pragmas(con, memory_limit_mb=memory_limit_mb, threads=threads)
        return materialize_run_bet_map_v1(
            con=con,
            input_paths=input_paths,
            output_parquet=output_parquet,
            run_end_gaming_day=run_end_gaming_day,
            run_break_min=run_break_min,
        )
    finally:
        con.close()


def _materialize_one_profile_run_day_bridge(
    duckdb_module: Any,
    *,
    input_paths: list[Path],
    output_parquet: Path,
    bet_gaming_day: str,
    run_break_min: float,
    memory_limit_mb: int | None,
    threads: int,
) -> dict[str, Any]:
    con = duckdb_module.connect(database=":memory:")
    try:
        apply_duckdb_resource_pragmas(con, memory_limit_mb=memory_limit_mb, threads=threads)
        return materialize_run_day_bridge_v1(
            con=con,
            input_paths=input_paths,
            output_parquet=output_parquet,
            bet_gaming_day=bet_gaming_day,
            run_break_min=run_break_min,
        )
    finally:
        con.close()


def _materialize_run_end_partition_artifact(
    duckdb_module: Any,
    artifact: Literal["run_fact", "run_bet_map"],
    *,
    resolved: list[Path],
    output_parquet: Path,
    run_end_gaming_day: str,
    run_break_min: float,
    memory_limit_mb: int | None,
    threads: int,
) -> dict[str, Any]:
    """Materialize ``run_fact`` or ``run_bet_map`` for one profile."""
    if artifact == "run_fact":
        return _materialize_one_profile_run_fact(
            duckdb_module,
            input_paths=resolved,
            output_parquet=output_parquet,
            run_end_gaming_day=run_end_gaming_day,
            run_break_min=run_break_min,
            memory_limit_mb=memory_limit_mb,
            threads=threads,
        )
    return _materialize_one_profile_run_bet_map(
        duckdb_module,
        input_paths=resolved,
        output_parquet=output_parquet,
        run_end_gaming_day=run_end_gaming_day,
        run_break_min=run_break_min,
        memory_limit_mb=memory_limit_mb,
        threads=threads,
    )


def _gate1_materialize_stats_for_profile(
    duckdb_module: Any,
    artifact: ArtifactKind,
    *,
    resolved: list[Path],
    output_parquet: Path,
    run_end_gaming_day: str | None,
    bet_gaming_day: str | None,
    run_break_min: float,
    memory_limit_mb: int | None,
    threads: int,
) -> dict[str, Any]:
    """Dispatch materialization for one DuckDB resource profile."""
    if artifact in ("run_fact", "run_bet_map"):
        if run_end_gaming_day is None:
            raise ValueError(f"run_end_gaming_day is required for {artifact}")
        return _materialize_run_end_partition_artifact(
            duckdb_module,
            artifact,
            resolved=resolved,
            output_parquet=output_parquet,
            run_end_gaming_day=run_end_gaming_day,
            run_break_min=run_break_min,
            memory_limit_mb=memory_limit_mb,
            threads=threads,
        )
    if bet_gaming_day is None:
        raise ValueError("bet_gaming_day is required for run_day_bridge")
    return _materialize_one_profile_run_day_bridge(
        duckdb_module,
        input_paths=resolved,
        output_parquet=output_parquet,
        bet_gaming_day=bet_gaming_day,
        run_break_min=run_break_min,
        memory_limit_mb=memory_limit_mb,
        threads=threads,
    )


def _gate1_one_profile_result(
    duckdb_module: Any,
    artifact: ArtifactKind,
    *,
    resolved: list[Path],
    output_parquet: Path,
    attempt_index: int,
    memory_limit_mb: int | None,
    threads: int,
    run_break_min: float,
    run_end_gaming_day: str | None,
    bet_gaming_day: str | None,
) -> dict[str, Any]:
    """Materialize + fingerprint for one profile; returns one ``results[]`` element."""
    stats = _gate1_materialize_stats_for_profile(
        duckdb_module,
        artifact,
        resolved=resolved,
        output_parquet=output_parquet,
        run_end_gaming_day=run_end_gaming_day,
        bet_gaming_day=bet_gaming_day,
        run_break_min=run_break_min,
        memory_limit_mb=memory_limit_mb,
        threads=threads,
    )
    con2 = duckdb_module.connect(database=":memory:")
    try:
        n_fp, fp = _fingerprint_for_artifact(con2, artifact, output_parquet)
    finally:
        con2.close()
    return {
        "attempt_index": attempt_index,
        "memory_limit_mb": memory_limit_mb,
        "output_parquet": str(output_parquet.resolve()),
        "row_count": int(stats["row_count"]),
        "row_fingerprint_row_count": n_fp,
        "row_fingerprint_sha256_hex": fp,
        "threads": threads,
    }


def gate1_l1_report_across_duckdb_profiles(
    *,
    duckdb_module: Any,
    artifact: ArtifactKind,
    input_paths: list[Path],
    output_dir: Path,
    profiles: Sequence[tuple[int | None, int]] | None,
    run_end_gaming_day: str | None = None,
    bet_gaming_day: str | None = None,
    run_break_min: float = RUN_BREAK_MIN_DEFAULT,
) -> dict[str, Any]:
    """Materialize once per profile and compare row counts + row fingerprints."""
    if not input_paths:
        raise ValueError("input_paths must be non-empty")
    resolved = [p.resolve() for p in input_paths]
    profs = list(profiles) if profiles is not None else list(GATE1_DEFAULT_DUCKDB_PROFILES)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_out: list[dict[str, Any]] = []
    for i, (mem_mb, thr) in enumerate(profs):
        out_p = output_dir / f"{artifact}_profile_{i}.parquet"
        rows_out.append(
            _gate1_one_profile_result(
                duckdb_module,
                artifact,
                resolved=resolved,
                output_parquet=out_p,
                attempt_index=i,
                memory_limit_mb=mem_mb,
                threads=thr,
                run_break_min=run_break_min,
                run_end_gaming_day=run_end_gaming_day,
                bet_gaming_day=bet_gaming_day,
            )
        )
    counts = {r["row_count"] for r in rows_out}
    fps = {r["row_fingerprint_sha256_hex"] for r in rows_out}
    match_n = len(counts) == 1
    match_fp = len(fps) == 1
    match_fp_rows = all(r["row_fingerprint_row_count"] == r["row_count"] for r in rows_out)
    return {
        "all_row_counts_match": match_n,
        "all_row_fingerprint_row_counts_match_stats": match_fp_rows,
        "all_row_fingerprints_match": match_fp,
        "artifact": artifact,
        "profiles_evaluated": len(rows_out),
        "profiles_requested": [[a, b] for a, b in profs],
        "results": rows_out,
        "run_break_min": float(run_break_min),
        "unique_fingerprints": len(fps),
        "unique_row_counts": len(counts),
    }


def gate1_report_to_json(report: dict[str, Any]) -> str:
    """Stable JSON string for stdout / CI artifacts."""
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
