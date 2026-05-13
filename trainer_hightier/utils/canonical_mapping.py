"""Build ``player_id`` → ``canonical_id`` from cleaned session Parquet (trainer D2 parity).

Mirrors ``trainer.training.identity_runtime.build_canonical_links_and_dummy_from_duckdb``
filters + ``trainer.identity.build_canonical_mapping_from_links`` M:N resolution.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from trainer.identity import build_canonical_mapping_from_links

from trainer_hightier.config import CanonicalMappingConfig, DuckDbRuntimeConfig
from trainer_hightier.utils.duckdb_runtime import (
    apply_duckdb_runtime_pragmas,
    execute_query_df_with_progress,
)

logger = logging.getLogger("trainer_hightier")

_CANONICAL_SESSION_COLS: frozenset[str] = frozenset(
    {
        "session_id",
        "player_id",
        "casino_player_id",
        "lud_dtm",
        "session_start_dtm",
        "session_end_dtm",
        "is_manual",
        "is_deleted",
        "is_canceled",
        "num_games_with_wager",
        "turnover",
    }
)


def default_canonical_mapping_artifacts_dir() -> Path:
    """Directory for canonical artifacts: ``trainer_hightier/artifacts/mapping``."""
    return Path(__file__).resolve().parents[1] / "artifacts" / "mapping"


def default_canonical_mapping_parquet_path() -> Path:
    """Default Parquet path for ``player_id`` / ``canonical_id``."""
    return default_canonical_mapping_artifacts_dir() / "canonical_player_mapping.parquet"


def default_canonical_mapping_sidecar_path() -> Path:
    """JSON sidecar next to mapping Parquet."""
    return default_canonical_mapping_artifacts_dir() / "canonical_mapping_meta.json"


def _normalize_cutoff_naive_hk(cutoff_dtm: datetime) -> datetime:
    ts = pd.Timestamp(cutoff_dtm)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Hong_Kong").tz_localize(None)
    return ts.to_pydatetime()


def _session_row_cutoff_sql(*, cutoff_str: str, legacy_coalesce_cutoff: bool, alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    if legacy_coalesce_cutoff:
        return f"COALESCE({p}session_end_dtm, {p}lud_dtm) <= TIMESTAMP '{cutoff_str}'"
    return (
        f"{p}session_end_dtm IS NOT NULL "
        f"AND CAST({p}session_end_dtm AS TIMESTAMP) <= TIMESTAMP '{cutoff_str}'"
    )


def _dedup_cte(path_posix: str) -> str:
    return (
        "WITH deduped AS (\n"
        "    SELECT *,\n"
        "        ROW_NUMBER() OVER (\n"
        "            PARTITION BY session_id\n"
        "            ORDER BY lud_dtm DESC NULLS LAST\n"
        "        ) AS rn\n"
        f"    FROM read_parquet('{path_posix}')\n"
        ")"
    )


def _resolve_clean_sql_and_placeholder() -> tuple[str, int]:
    try:
        from trainer.config import CASINO_PLAYER_ID_CLEAN_SQL as _sql  # type: ignore[import-untyped]
        from trainer.config import PLACEHOLDER_PLAYER_ID as _ph  # type: ignore[import-untyped]
    except ImportError:
        _sql = (
            "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
            "THEN NULL ELSE trim(casino_player_id) END"
        )
        _ph = -1
    if ";" in (_sql or ""):
        raise ValueError("CASINO_PLAYER_ID_CLEAN_SQL must not contain ';'")
    return str(_sql), int(_ph)


def _infer_cutoff_from_parquet(con: Any, path_posix: str) -> datetime:
    sql = f"""
    SELECT COALESCE(
      MAX(CAST(session_end_dtm AS TIMESTAMP)),
      MAX(CAST(lud_dtm AS TIMESTAMP))
    ) AS mx
    FROM read_parquet('{path_posix}')
    """
    df = con.execute(sql).df()
    if df.empty or pd.isna(df["mx"].iloc[0]):
        raise ValueError(
            "Cannot infer canonical cutoff: no non-null session_end_dtm/lud_dtm in cleaned Parquet."
        )
    return pd.Timestamp(df["mx"].iloc[0]).to_pydatetime()


def _validate_schema(path: Path) -> None:
    names = frozenset(pq.read_schema(path).names)
    missing = sorted(_CANONICAL_SESSION_COLS - names)
    if missing:
        raise ValueError(
            f"Cleaned session Parquet missing columns required for canonical mapping: {missing}"
        )


def _compose_links_dummy_sql(
    *,
    path_posix: str,
    cutoff_str: str,
    legacy_coalesce_cutoff: bool,
    clean_sql: str,
    placeholder: int,
) -> tuple[str, str]:
    cte = _dedup_cte(path_posix)
    row_cut = _session_row_cutoff_sql(cutoff_str=cutoff_str, legacy_coalesce_cutoff=legacy_coalesce_cutoff)
    links_sql = f"""
{cte}
SELECT player_id,
       ({clean_sql}) AS casino_player_id,
       lud_dtm
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {placeholder}
  AND {row_cut}
  AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
  AND ({clean_sql}) IS NOT NULL"""
    dummy_sql = f"""
{cte}
SELECT player_id
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {placeholder}
  AND {row_cut}
  AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
GROUP BY player_id
HAVING COUNT(session_id) = 1
   AND SUM(COALESCE(num_games_with_wager, 0)) <= 1"""
    return links_sql, dummy_sql


def _duckdb_canonical_links_and_dummy(
    *,
    path_posix: str,
    cfg: CanonicalMappingConfig,
    duckdb_runtime: DuckDbRuntimeConfig,
    clean_sql: str,
    placeholder: int,
    join_timeout_s: float,
) -> tuple[pd.DataFrame, pd.DataFrame, datetime]:
    """Open DuckDB, compute cutoff, run links + dummy queries."""
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        cutoff_raw = cfg.cutoff_dtm if cfg.cutoff_dtm is not None else _infer_cutoff_from_parquet(con, path_posix)
        cutoff_naive = _normalize_cutoff_naive_hk(cutoff_raw)
        cutoff_str = pd.Timestamp(cutoff_naive).strftime("%Y-%m-%d %H:%M:%S")
        links_sql, dummy_sql = _compose_links_dummy_sql(
            path_posix=path_posix,
            cutoff_str=cutoff_str,
            legacy_coalesce_cutoff=cfg.legacy_coalesce_cutoff,
            clean_sql=clean_sql,
            placeholder=placeholder,
        )
        links_df = execute_query_df_with_progress(
            con,
            links_sql,
            desc="[Step 3] DuckDB canonical links",
            join_timeout_s=join_timeout_s,
        )
        dummy_df = execute_query_df_with_progress(
            con,
            dummy_sql,
            desc="[Step 3] DuckDB canonical dummy (FND-12)",
            join_timeout_s=join_timeout_s,
        )
        return links_df, dummy_df, cutoff_naive
    finally:
        con.close()


def build_canonical_mapping_from_cleaned_session_parquet(
    cleaned_session_parquet: Path,
    *,
    cfg: CanonicalMappingConfig,
    duckdb_runtime: DuckDbRuntimeConfig,
    output_parquet: Path | None = None,
    output_sidecar: Path | None = None,
    duckdb_join_timeout_s: float = 3600.0,
) -> tuple[Path, Path]:
    """Run DuckDB links + dummy queries, apply M:N resolution, write mapping artifacts."""
    src = Path(cleaned_session_parquet).resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
    _validate_schema(src)

    clean_sql, placeholder = _resolve_clean_sql_and_placeholder()
    path_posix = str(src).replace("'", "''")

    out_pq = Path(output_parquet) if output_parquet is not None else default_canonical_mapping_parquet_path()
    out_side = Path(output_sidecar) if output_sidecar is not None else default_canonical_mapping_sidecar_path()
    out_pq.parent.mkdir(parents=True, exist_ok=True)

    links_df, dummy_df, cutoff_naive = _duckdb_canonical_links_and_dummy(
        path_posix=path_posix,
        cfg=cfg,
        duckdb_runtime=duckdb_runtime,
        clean_sql=clean_sql,
        placeholder=placeholder,
        join_timeout_s=duckdb_join_timeout_s,
    )

    dummy_pids: set[int] = (
        set() if dummy_df.empty else {int(x) for x in dummy_df["player_id"].tolist()}
    )
    canonical_map = build_canonical_mapping_from_links(links_df, dummy_pids)
    canonical_map.to_parquet(out_pq, index=False)

    meta = {
        "cutoff_dtm": cutoff_naive.isoformat(sep=" "),
        "dummy_player_ids": sorted(dummy_pids),
        "legacy_coalesce_cutoff": cfg.legacy_coalesce_cutoff,
        "mapping_rows": int(len(canonical_map)),
        "output_parquet": str(out_pq.resolve()),
        "source_cleaned_parquet": str(src),
    }
    out_side.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    logger.info(
        "[Step 3] canonical mapping: rows=%d dummies=%d written %s",
        len(canonical_map),
        len(dummy_pids),
        out_pq.resolve(),
    )
    return out_pq.resolve(), out_side.resolve()
