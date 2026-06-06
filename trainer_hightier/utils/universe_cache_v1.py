"""ADT rank universe cache (L2): canonical-level rank table + selected universe manifest."""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Final

import duckdb

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.duckdb_runtime import execute_sql_with_progress_oom_retry
from trainer_hightier.utils.patron_session_metrics import _validate_adt_allowlist_inputs
from trainer_hightier.utils.source_manifest_v2 import (
    default_cache_root,
    sha256_file_bytes,
    write_json_atomic,
)

logger = logging.getLogger("trainer_hightier")

ADT_RANK_KIND: Final[str] = "adt_rank_latest_v1"
ADT_RANK_SCHEMA_VERSION: Final[int] = 1
SELECTED_UNIVERSE_SCHEMA_VERSION: Final[int] = 1


def default_universe_cache_root(*, package_dir: Path | None = None) -> Path:
    """Return ``trainer_hightier/artifacts/cache/universe_v1``."""
    return default_cache_root(package_dir=package_dir) / "universe_v1"


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/").replace("'", "''")


def _adt_rank_inner_sql(
    *,
    profile_esc: str,
    map_esc: str,
    slow_coverage_subquery: str | None,
) -> str:
    """Build DuckDB SELECT for canonical rank + player projection."""
    slow_join = ""
    slow_flag = "FALSE AS has_slow_window_coverage"
    if slow_coverage_subquery is not None:
        slow_join = (
            f"LEFT JOIN ({slow_coverage_subquery}) AS slow_cov "
            "ON TRIM(CAST(m.canonical_id AS VARCHAR)) = slow_cov.canonical_id"
        )
        slow_flag = (
            "CASE WHEN slow_cov.canonical_id IS NOT NULL THEN TRUE ELSE FALSE END "
            "AS has_slow_window_coverage"
        )
    return f"""
WITH profile AS (
  SELECT
    TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
    TRY_CAST(adt AS DOUBLE) AS adt
  FROM read_csv_auto('{profile_esc}')
  WHERE TRY_CAST(adt AS DOUBLE) IS NOT NULL
),
ranked AS (
  SELECT
    canonical_id,
    adt,
    rank() OVER (ORDER BY adt ASC) AS adt_rank,
    percent_rank() OVER (ORDER BY adt ASC) AS adt_percentile
  FROM profile
)
SELECT
  TRIM(CAST(m.canonical_id AS VARCHAR)) AS canonical_id,
  TRY_CAST(m.player_id AS BIGINT) AS player_id,
  r.adt,
  CAST(r.adt_rank AS BIGINT) AS adt_rank,
  CAST(r.adt_percentile AS DOUBLE) AS adt_percentile,
  {slow_flag}
FROM read_parquet('{map_esc}') AS m
INNER JOIN ranked AS r
  ON TRIM(CAST(m.canonical_id AS VARCHAR)) = r.canonical_id
{slow_join}
WHERE TRY_CAST(m.player_id AS BIGINT) IS NOT NULL
ORDER BY canonical_id ASC, player_id ASC
""".strip()


def rank_table_dir(
    *,
    cache_root: Path,
    profile_sha256: str,
    mapping_sha256: str,
    slow_anchor: str,
) -> Path:
    """Directory for one cached ADT rank table build."""
    root = Path(cache_root).resolve()
    return (
        root
        / "adt_rank_latest"
        / f"profile={profile_sha256[:16]}"
        / f"mapping={mapping_sha256[:16]}"
        / f"slow_anchor={slow_anchor}"
    )


def load_adt_rank_manifest(path: Path) -> dict[str, Any] | None:
    """Load rank table sidecar manifest or ``None`` when missing/corrupt."""
    p = Path(path).resolve()
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def adt_rank_cache_is_hit(
    *,
    manifest_path: Path,
    data_path: Path,
    profile_sha256: str,
    mapping_sha256: str,
    slow_anchor: str,
) -> bool:
    """Return True when cached rank parquet + manifest match input fingerprints."""
    if not Path(data_path).is_file() or not Path(manifest_path).is_file():
        return False
    prev = load_adt_rank_manifest(manifest_path)
    if prev is None:
        return False
    return (
        str(prev.get("profile_snapshot_sha256")) == str(profile_sha256)
        and str(prev.get("mapping_sha256")) == str(mapping_sha256)
        and str(prev.get("slow_anchor_required")) == str(slow_anchor)
        and Path(data_path).is_file()
    )


def _copy_rank_sql_to_parquet(
    *,
    inner_sql: str,
    output_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    duckdb_join_timeout_s: float,
) -> None:
    """Materialize rank SQL to Parquet."""
    out_px = _path_esc(output_parquet)
    sql = f"COPY ({inner_sql}) TO '{out_px}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    execute_sql_with_progress_oom_retry(
        duckdb_runtime,
        sql,
        desc="[universe_v1] DuckDB ADT rank table",
        join_timeout_s=float(duckdb_join_timeout_s),
    )


def _count_rank_rows(parquet_path: Path) -> tuple[int, int]:
    """Return ``(distinct canonical_id, row_count)``."""
    p = str(Path(parquet_path).resolve()).replace("\\", "/").replace("'", "''")
    con = duckdb.connect()
    try:
        row = con.execute(
            f"SELECT count(*) AS n, count(DISTINCT canonical_id) AS c FROM read_parquet('{p}')",
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return 0, 0
    return int(row[1]), int(row[0])


def materialize_adt_rank_table_v1_cached(
    *,
    patron_profile_csv: Path,
    canonical_mapping_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    cache_root: Path | None = None,
    cleaned_session_parquet: Path | None = None,
    slow_active_anchor: date | None = None,
    slow_lookback_days: int = 180,
    duckdb_join_timeout_s: float = 3600.0,
) -> dict[str, Any]:
    """Build or reuse cached ADT rank table; return observability metadata."""
    t0 = time.perf_counter()
    src_p = Path(patron_profile_csv).resolve()
    src_m = Path(canonical_mapping_parquet).resolve()
    _validate_adt_allowlist_inputs(src_p, src_m)
    profile_sha = sha256_file_bytes(src_p)
    mapping_sha = sha256_file_bytes(src_m)
    slow_anchor_s = str(slow_active_anchor) if slow_active_anchor is not None else "none"
    uroot = default_universe_cache_root() if cache_root is None else Path(cache_root).resolve()
    out_dir = rank_table_dir(
        cache_root=uroot,
        profile_sha256=profile_sha,
        mapping_sha256=mapping_sha,
        slow_anchor=slow_anchor_s,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = out_dir / "data.parquet"
    manifest_path = out_dir / "manifest.json"
    cache_hit = adt_rank_cache_is_hit(
        manifest_path=manifest_path,
        data_path=data_path,
        profile_sha256=profile_sha,
        mapping_sha256=mapping_sha,
        slow_anchor=slow_anchor_s,
    )
    if not cache_hit:
        slow_cov_sql: str | None = None
        if cleaned_session_parquet is not None and slow_active_anchor is not None:
            from trainer_hightier.utils.slow_patron_180d_monthly import (
                canonical_ids_with_slow_session_window_subquery,
            )

            sess_p = Path(cleaned_session_parquet).resolve()
            if not sess_p.is_file():
                raise FileNotFoundError(sess_p)
            slow_cov_sql = canonical_ids_with_slow_session_window_subquery(
                sess_esc=_path_esc(sess_p),
                map_esc=_path_esc(src_m),
                lookback_days=int(slow_lookback_days),
                active_anchor=slow_active_anchor,
            )
        inner = _adt_rank_inner_sql(
            profile_esc=_path_esc(src_p),
            map_esc=_path_esc(src_m),
            slow_coverage_subquery=slow_cov_sql,
        )
        if data_path.is_file():
            data_path.unlink()
        _copy_rank_sql_to_parquet(
            inner_sql=inner,
            output_parquet=data_path,
            duckdb_runtime=duckdb_runtime,
            duckdb_join_timeout_s=duckdb_join_timeout_s,
        )
        canon_n, row_n = _count_rank_rows(data_path)
        manifest = {
            "schema_version": ADT_RANK_SCHEMA_VERSION,
            "kind": ADT_RANK_KIND,
            "profile_snapshot_sha256": profile_sha,
            "mapping_sha256": mapping_sha,
            "slow_anchor_required": slow_anchor_s,
            "canonical_count": int(canon_n),
            "player_projection_count": int(row_n),
            "rank_table_path": str(data_path.resolve()),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json_atomic(manifest_path, manifest)
    else:
        prev = load_adt_rank_manifest(manifest_path) or {}
        row_n = int(prev.get("player_projection_count") or 0)
        canon_n = int(prev.get("canonical_count") or 0)
    rank_fp = sha256_file_bytes(data_path)
    return {
        "universe_adt_rank_cache_hit": bool(cache_hit),
        "universe_adt_rank_elapsed_seconds": round(time.perf_counter() - t0, 6),
        "universe_adt_rank_table_path": str(data_path.resolve()),
        "universe_adt_rank_manifest_path": str(manifest_path.resolve()),
        "universe_adt_rank_fingerprint_sha256_hex": rank_fp,
        "universe_adt_rank_canonical_count": int(canon_n),
        "universe_adt_rank_player_projection_count": int(row_n),
        "universe_profile_snapshot_sha256": profile_sha,
        "universe_mapping_sha256": mapping_sha,
        "universe_slow_anchor_required": slow_anchor_s,
    }


def diff_selected_universe_added_player_ids(
    rank_table_path: Path,
    *,
    previous_quantile: float,
    current_quantile: float,
) -> tuple[int, ...]:
    """Return ``player_id`` values newly included when quantile **decreases**."""
    prev_q = float(previous_quantile)
    cur_q = float(current_quantile)
    if not (0.0 < prev_q < 1.0):
        raise ValueError(f"previous_quantile must be in (0,1), got {prev_q!r}")
    if not (0.0 < cur_q < 1.0):
        raise ValueError(f"current_quantile must be in (0,1), got {cur_q!r}")
    if cur_q >= prev_q:
        return ()
    p = _path_esc(rank_table_path)
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT DISTINCT TRY_CAST(player_id AS BIGINT) AS pid
            FROM read_parquet('{p}')
            WHERE has_slow_window_coverage
              AND CAST(adt_percentile AS DOUBLE) >= {cur_q}
              AND CAST(adt_percentile AS DOUBLE) < {prev_q}
            ORDER BY 1
            """,
        ).fetchall()
    finally:
        con.close()
    return tuple(int(r[0]) for r in rows if r and r[0] is not None)


def write_selected_universe_manifest(
    *,
    rank_table_path: Path,
    quantile: float,
    rank_fingerprint_sha256_hex: str,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    """Write selected-universe sidecar for one quantile filter over a rank table."""
    qf = float(quantile)
    if not (0.0 < qf < 1.0):
        raise ValueError(f"quantile must be strictly between 0 and 1, got {qf!r}")
    p = str(Path(rank_table_path).resolve()).replace("\\", "/").replace("'", "''")
    con = duckdb.connect()
    try:
        row = con.execute(
            f"""
            SELECT
              count(DISTINCT canonical_id) AS canonical_count,
              count(*) AS player_count
            FROM read_parquet('{p}')
            WHERE adt_percentile >= {qf}
              AND has_slow_window_coverage
            """,
        ).fetchone()
    finally:
        con.close()
    canon_n = int(row[0]) if row is not None else 0
    player_n = int(row[1]) if row is not None else 0
    qslug = str(qf).replace(".", "p").replace("-", "neg")
    uroot = default_universe_cache_root() if cache_root is None else Path(cache_root).resolve()
    out_dir = uroot / "selected_universe"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"selected_universe_q{qslug}_{rank_fingerprint_sha256_hex[:16]}.json"
    payload = {
        "schema_version": SELECTED_UNIVERSE_SCHEMA_VERSION,
        "selected_quantile": qf,
        "rank_table_fingerprint_sha256_hex": str(rank_fingerprint_sha256_hex),
        "selected_canonical_count": canon_n,
        "selected_player_count": player_n,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(out_path, payload)
    return {
        "selected_universe_manifest_path": str(out_path.resolve()),
        "selected_universe_canonical_count": canon_n,
        "selected_universe_player_count": player_n,
        "selected_universe_quantile": qf,
    }
