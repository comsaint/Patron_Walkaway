"""Session L0 preprocess: ``t_session`` → cleaned Parquet (DuckDB and optional pandas shards).

See :mod:`trainer_hightier.utils.bet_l0_preprocess` for ``t_bet``. Session clean cache
fingerprints hash **this module** only (bet logic changes do not invalidate session cache).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from trainer.core.schema_io import normalize_bets_sessions
from trainer_hightier.config import (
    PREPROCESS_DEDUP_BUCKET_ESCALATION_CEILING,
    DuckDbRuntimeConfig,
    SessionPreprocessConfig,
)
from trainer_hightier.utils.canonical_mapping import _CANONICAL_SESSION_COLS
from trainer_hightier.utils.duckdb_runtime import execute_sql_with_progress_oom_retry

_01_ingest = importlib.import_module("trainer_hightier.01_data_ingest")

logger = logging.getLogger("trainer_hightier")



_SESSION_PREPROCESS_REQUIRED_EXTRAS: frozenset[str] = frozenset(
    {
        "__etl_insert_Dtm",
        "theo_win",
        "gaming_day",
        # Patron-level profile CSV (trainer_hightier.utils.patron_session_metrics)
        "player_win",
        "cash_buyins",
        "num_bets",
    }
)

SESSION_PREPROCESS_READ_COLS_ORDERED: tuple[str, ...] = (
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
    "player_win",
    "cash_buyins",
    "num_bets",
    "__etl_insert_Dtm",
    "theo_win",
    "gaming_day",
)

if frozenset(SESSION_PREPROCESS_READ_COLS_ORDERED) != (
    _CANONICAL_SESSION_COLS | _SESSION_PREPROCESS_REQUIRED_EXTRAS
):
    raise AssertionError(
        "session preprocess READ column set must equal canonical cols + Steps 3–4 extras"
    )


def _duckdb_quote_ident(name: str) -> str:
    """Return a double-quoted DuckDB identifier (escape embedded quotes)."""
    return '"' + str(name).replace('"', '""') + '"'


def _session_preprocess_read_columns_ordered(schema_names: frozenset[str]) -> tuple[str, ...]:
    """Verify L0 schema has all Step 2–4 session columns; return fixed read order."""

    missing = tuple(c for c in SESSION_PREPROCESS_READ_COLS_ORDERED if c not in schema_names)
    if missing:
        raise ValueError(
            "gmwds_t_session Parquet missing columns required for preprocess "
            f"(Steps 2–4 / trainer_hightier.02_preprocess): {list(missing)}"
        )
    return SESSION_PREPROCESS_READ_COLS_ORDERED


def _logging_redirect_tqdm_cm() -> Any:
    """Send root logging through tqdm.write while active so bars are not torn apart."""
    try:
        from tqdm.contrib.logging import logging_redirect_tqdm

        return logging_redirect_tqdm()
    except ImportError:
        return nullcontext()


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _l0_gate_where_sql(names: frozenset[str]) -> str:
    parts: list[str] = []
    if "is_deleted" in names:
        parts.append('COALESCE(TRY_CAST("is_deleted" AS BIGINT), 0) = 0')
    if "is_canceled" in names:
        parts.append('COALESCE(TRY_CAST("is_canceled" AS BIGINT), 0) = 0')
    if not parts:
        return "TRUE"
    return " AND ".join(parts)


def _fnd01_order_sql(names: frozenset[str]) -> str:
    parts: list[str] = []
    if "lud_dtm" in names:
        parts.append('"lud_dtm" DESC NULLS LAST')
    if "__etl_insert_Dtm" in names:
        parts.append('"__etl_insert_Dtm" DESC NULLS LAST')
    if not parts:
        return '"session_id" DESC NULLS LAST'
    return ", ".join(parts)


def _fnd04_activity_sql(names: frozenset[str]) -> str:
    t = 'COALESCE(TRY_CAST("turnover" AS DOUBLE), 0)' if "turnover" in names else "0.0"
    g = 'COALESCE(TRY_CAST("num_games_with_wager" AS DOUBLE), 0)' if "num_games_with_wager" in names else "0.0"
    return f"(({t} > 0) OR ({g} > 0))"


def _s1_cte_sql(names: frozenset[str]) -> str:
    """Impute ``session_end_dtm`` from ``session_start_dtm`` when both exist (trainer parity)."""
    has_end = "session_end_dtm" in names
    has_start = "session_start_dtm" in names
    if has_end and has_start:
        return """s1 AS (
  SELECT * REPLACE (
    COALESCE(
      TRY_CAST("session_end_dtm" AS TIMESTAMP),
      TRY_CAST("session_start_dtm" AS TIMESTAMP)
    ) AS "session_end_dtm"
  ) FROM l0
)"""
    return "s1 AS ( SELECT * FROM l0 )"


def _build_s2_cte_sql(names: frozenset[str]) -> str:
    """Registry synthetic; skip if raw cannot produce column (parity with pandas)."""
    need = "__etl_insert_Dtm" in names and "session_end_dtm" in names
    if not need:
        return "s2 AS ( SELECT * FROM s1 )"
    star = "s1.*"
    if "__etl_insert_Dtm_synthetic" in names:
        star = 's1.* EXCLUDE ("__etl_insert_Dtm_synthetic")'
    # Pandas: LEAST(etl, end+636s) with skipna=False → null if either input null.
    syn = f"""CASE
    WHEN s1."__etl_insert_Dtm" IS NULL OR s1."session_end_dtm" IS NULL THEN NULL
    ELSE LEAST(
      TRY_CAST(s1."__etl_insert_Dtm" AS TIMESTAMP),
      CAST(s1."session_end_dtm" AS TIMESTAMP) + INTERVAL 636 SECOND
    )
  END"""
    return f"""s2 AS (
  SELECT
    {star},
    {syn} AS "__etl_insert_Dtm_synthetic"
  FROM s1
)"""


def _duckdb_read_parquet_array_sql(paths: list[Path]) -> str:
    """Build DuckDB ``read_parquet([...])`` source from explicit Parquet paths."""
    if not paths:
        raise ValueError("paths must be non-empty for read_parquet list")
    parts: list[str] = []
    for p in paths:
        esc = _path_posix(Path(p).resolve()).replace("'", "''")
        parts.append(f"'{esc}'")
    return "read_parquet([" + ", ".join(parts) + "])"


def _duckdb_session_clean_pipeline_select_sql(
    src_clause: str,
    *,
    read_cols_ordered: tuple[str, ...],
    dedup_bucket_id: int | None = None,
    dedup_buckets: int = 1,
) -> str:
    """Single-scan pipeline: L0 gate → impute → synthetic → FND-01 → FND-04.

    Args:
        src_clause: DuckDB source SQL, either ``read_parquet('single')``
            or ``read_parquet([...])``.
    """

    n = int(dedup_buckets)
    if n < 1:
        raise ValueError(f"dedup_buckets must be >= 1, got {n}")
    bucket_filter = ""
    if n > 1:
        if dedup_bucket_id is None:
            raise ValueError("dedup_bucket_id is required when dedup_buckets > 1")
        b = int(dedup_bucket_id)
        if b < 0 or b >= n:
            raise ValueError(f"dedup_bucket_id must be in [0, {n}), got {b}")
        bucket_filter = (
            f" AND (mod(abs(hash(TRY_CAST(\"session_id\" AS DOUBLE))), {n}) = {b})"
        )

    names = frozenset(read_cols_ordered)
    l0w = _l0_gate_where_sql(names)
    s1b = _s1_cte_sql(names)
    s2b = _build_s2_cte_sql(names)
    ord_clause = _fnd01_order_sql(names)
    act = _fnd04_activity_sql(names)
    l0_projection = ", ".join(_duckdb_quote_ident(c) for c in read_cols_ordered)
    return f"""
WITH l0 AS (
  SELECT {l0_projection} FROM {src_clause}
  WHERE {l0w}{bucket_filter}
),
{s1b},
{s2b},
d AS (
  SELECT
    s2.*,
    ROW_NUMBER() OVER (
      PARTITION BY TRY_CAST("session_id" AS DOUBLE)
      ORDER BY {ord_clause}
    ) AS _rn
  FROM s2
  WHERE TRY_CAST("session_id" AS DOUBLE) IS NOT NULL
),
f AS ( SELECT * EXCLUDE (_rn) FROM d WHERE _rn = 1 )
SELECT * FROM f
WHERE COALESCE(TRY_CAST("is_deleted" AS BIGINT), 0) = 0
  AND COALESCE(TRY_CAST("is_canceled" AS BIGINT), 0) = 0
  AND ({act})
"""


def _preprocess_sessions_duckdb_single_copy(
    session_sources: list[Path],
    output_path: Path,
    duckdb_cfg: DuckDbRuntimeConfig,
    dedup_hash_buckets: int,
) -> None:
    """Run DuckDB ``COPY``; optional hash buckets + merge (matches bet preprocess pattern)."""

    if not session_sources:
        raise ValueError("session_sources must not be empty")
    if len(session_sources) == 1:
        sp = _path_posix(Path(session_sources[0]).resolve()).replace("'", "''")
        src_clause = f"read_parquet('{sp}')"
    else:
        src_clause = _duckdb_read_parquet_array_sql([Path(x) for x in session_sources])
    out = Path(output_path).resolve()
    n = int(dedup_hash_buckets)
    if n < 1:
        raise ValueError(f"dedup_hash_buckets must be >= 1, got {n}")
    schema_names = frozenset(pq.read_schema(Path(session_sources[0])).names)
    read_ordered = _session_preprocess_read_columns_ordered(schema_names)
    out_p = _path_posix(out).replace("'", "''")

    if n == 1:
        inner = _duckdb_session_clean_pipeline_select_sql(
            src_clause,
            read_cols_ordered=read_ordered,
            dedup_bucket_id=None,
            dedup_buckets=1,
        )
        sql = f"COPY ({inner}) TO '{out_p}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
        execute_sql_with_progress_oom_retry(
            duckdb_cfg,
            sql,
            desc="[Step 2] DuckDB session COPY",
            join_timeout_s=7200.0,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="hightier_sess_bkt_", dir=out.parent) as tdir:
            parts_dir = Path(tdir)
            for b in range(n):
                inner = _duckdb_session_clean_pipeline_select_sql(
                    src_clause,
                    read_cols_ordered=read_ordered,
                    dedup_bucket_id=b,
                    dedup_buckets=n,
                )
                part_p = parts_dir / f"part_{b:04d}.parquet"
                part_esc = _path_posix(part_p).replace("'", "''")
                bsql = f"COPY ({inner}) TO '{part_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
                execute_sql_with_progress_oom_retry(
                    duckdb_cfg,
                    bsql,
                    desc=f"[Step 2] DuckDB session COPY bucket {b + 1}/{n}",
                    join_timeout_s=7200.0,
                )
            glob_pat = str(parts_dir / "part_*.parquet").replace("\\", "/").replace("'", "''")
            merge_sql = (
                f"COPY (SELECT * FROM read_parquet('{glob_pat}')) "
                f"TO '{out_p}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
            )
            execute_sql_with_progress_oom_retry(
                duckdb_cfg,
                merge_sql,
                desc="[Step 2] DuckDB session merge buckets",
                join_timeout_s=7200.0,
            )


def _preprocess_sessions_pandas_shard_batches(
    src: Path,
    out: Path,
    cfg: SessionPreprocessConfig,
    duckdb_cfg: DuckDbRuntimeConfig,
) -> None:
    """Python transforms per batch of row groups → shard Parquets → DuckDB ``COPY`` merge."""
    pf = pq.ParquetFile(src)
    schema_names = frozenset(str(x) for x in pf.schema_arrow.names)
    read_cols_list = list(_session_preprocess_read_columns_ordered(schema_names))
    nrg = int(pf.num_row_groups)
    rgs = max(1, int(cfg.row_groups_per_shard))
    batch_starts = list(range(0, nrg, rgs))
    logger.info(
        "[Step 2] session pandas shards: %d row groups, %d row groups per shard (~%d shard files)",
        nrg,
        rgs,
        len(batch_starts),
    )

    with tempfile.TemporaryDirectory(prefix="hightier_sess_") as tmp:
        shards_dir = Path(tmp) / "shards"
        shards_dir.mkdir(parents=True, exist_ok=True)
        shard_idx = 0
        with _logging_redirect_tqdm_cm():
            for batch_ix in _01_ingest.tqdm_row_group_range(
                range(len(batch_starts)),
                desc="[Step 2] t_session shard batches",
                unit="batch",
            ):
                bs = batch_starts[batch_ix]
                parts: list[pd.DataFrame] = []
                for rg_i in range(bs, min(bs + rgs, nrg)):
                    parts.append(
                        pf.read_row_group(rg_i, columns=read_cols_list, use_threads=True).to_pandas()
                    )
                chunk = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
                chunk = _filter_session_l0_deleted_canceled(chunk)
                if len(chunk) == 0:
                    continue
                staged = apply_session_l0_registry_cleanup(chunk, batched_shard=True)
                _, sess_norm = normalize_bets_sessions(pd.DataFrame(), staged)
                shard = _session_coerce_ids_datetime_prepare(sess_norm)
                _session_shard_fill_defaults_for_sql(shard)
                table = pa.Table.from_pandas(shard, preserve_index=False)
                spath = shards_dir / f"part_{shard_idx:05d}.parquet"
                pq.write_table(table, spath, compression="snappy")
                shard_idx += 1
                del chunk, staged, sess_norm, shard, table, parts

        shard_files = sorted(shards_dir.glob("part_*.parquet"))
        if not shard_files:
            pd.DataFrame().to_parquet(out, index=False)
            logger.warning("[Step 2] session batched preprocess: no rows after L0 gate; wrote empty parquet")
            return

        globs = str(shards_dir / "part_*.parquet").replace("\\", "/")
        out_posix = _path_posix(out)
        logger.info(
            "[Step 2] session pandas shards: DuckDB merge %d shards (FND-01 + FND-04) → %s",
            len(shard_files),
            out,
        )
        _merge_session_shards_duckdb(globs, out_posix, shard_files[0], duckdb_runtime=duckdb_cfg)

# ``tables.t_session`` → ``SESSION-INGEST-FIX-001`` / ``synthetic_observed_at_contract.ingest_delay_cap_sec``
_SESSION_INGEST_DELAY_CAP_SEC = 636

# Bump when cache record schema or semantics change (invalidates old sidecars).
_SESSION_CLEAN_CACHE_MANIFEST_VERSION = 5


def _filter_session_l0_deleted_canceled(chunk: pd.DataFrame) -> pd.DataFrame:
    """Per-row-group gate matching DuckDB ``COALESCE(CAST(is_* AS BIGINT),0)=0`` (missing col = pass)."""
    if len(chunk) == 0:
        return chunk
    mask = pd.Series(True, index=chunk.index)
    if "is_deleted" in chunk.columns:
        del_ok = pd.to_numeric(chunk["is_deleted"], errors="coerce").fillna(0).astype("int64") == 0
        mask = mask & del_ok
    if "is_canceled" in chunk.columns:
        can_ok = pd.to_numeric(chunk["is_canceled"], errors="coerce").fillna(0).astype("int64") == 0
        mask = mask & can_ok
    return chunk.loc[mask].copy()


def impute_session_end_from_session_start(
    sessions: pd.DataFrame, *, batched_shard: bool = False
) -> pd.DataFrame:
    """Fill null ``session_end_dtm`` from ``session_start_dtm`` (in-place copy).

    Does not drop rows. Rows where both are null keep null ``session_end_dtm``.

    Args:
        batched_shard: If True, per-chunk statistics log at DEBUG (row-group pipeline).
    """
    if "session_end_dtm" not in sessions.columns:
        return sessions
    if "session_start_dtm" not in sessions.columns:
        return sessions
    out = sessions.copy()
    end = pd.to_datetime(out["session_end_dtm"], utc=False, errors="coerce")
    start = pd.to_datetime(out["session_start_dtm"], utc=False, errors="coerce")
    n_null_end = int(end.isna().sum())
    filled = end.where(~end.isna(), start)
    out["session_end_dtm"] = filled
    n_filled = int((end.isna() & start.notna()).sum())
    if n_null_end:
        _log = logger.debug if batched_shard else logger.info
        _log(
            "[Step 2] impute session_end_dtm from session_start_dtm: %d rows had null end; %d filled from start",
            n_null_end,
            n_filled,
        )
    return out


def add_etl_insert_dtm_synthetic(
    sessions: pd.DataFrame, *, batched_shard: bool = False
) -> pd.DataFrame:
    """Add ``__etl_insert_Dtm_synthetic`` per registry LEAST(etl, end + cap).

    Matches ``logical_observed_at_expr`` for ``t_session`` with ``ingest_delay_cap_sec=636``.
    If ``__etl_insert_Dtm`` or ``session_end_dtm`` is null for a row, synthetic is null
    (SQL LEAST semantics with nulls).

    Args:
        batched_shard: If True, per-chunk statistics log at DEBUG (row-group pipeline).
    """
    out = sessions.copy()
    if "__etl_insert_Dtm" not in out.columns or "session_end_dtm" not in out.columns:
        logger.warning(
            "[Step 2] skip __etl_insert_Dtm_synthetic: need __etl_insert_Dtm and session_end_dtm; "
            "got columns=%s",
            list(out.columns)[:30],
        )
        return out

    etl = pd.to_datetime(out["__etl_insert_Dtm"], utc=False, errors="coerce")
    end = pd.to_datetime(out["session_end_dtm"], utc=False, errors="coerce")
    cap = end + pd.Timedelta(seconds=_SESSION_INGEST_DELAY_CAP_SEC)
    pair = pd.DataFrame({"etl": etl, "cap": cap})
    out["__etl_insert_Dtm_synthetic"] = pair.min(axis=1, skipna=False)
    n_synth = int(out["__etl_insert_Dtm_synthetic"].notna().sum())
    _log = logger.debug if batched_shard else logger.info
    _log(
        "[Step 2] __etl_insert_Dtm_synthetic: %d/%d rows non-null (cap=%ss)",
        n_synth,
        len(out),
        _SESSION_INGEST_DELAY_CAP_SEC,
    )
    return out


def _session_coerce_ids_datetime_prepare(sessions: pd.DataFrame) -> pd.DataFrame:
    """Coerce session/player ids and session datetimes; drop null ``session_id`` (no FND-01 dedup)."""
    session_dt_cols: Dict[str, pd.Series] = {}
    for dt_col in ("session_start_dtm", "session_end_dtm", "lud_dtm"):
        if dt_col in sessions.columns:
            session_dt_cols[dt_col] = pd.to_datetime(
                sessions[dt_col], utc=False, errors="coerce"
            )

    session_id_num = pd.to_numeric(
        sessions["session_id"] if "session_id" in sessions.columns else pd.Series(np.nan, index=sessions.index),
        errors="coerce",
    )
    player_id_num = pd.to_numeric(
        sessions["player_id"] if "player_id" in sessions.columns else pd.Series(np.nan, index=sessions.index),
        errors="coerce",
    )
    _valid_session_id_mask = session_id_num.notna()
    out = sessions.loc[_valid_session_id_mask].copy()
    for dt_col, normalized in session_dt_cols.items():
        out[dt_col] = normalized.loc[_valid_session_id_mask].to_numpy()
    out["session_id"] = session_id_num.loc[_valid_session_id_mask].to_numpy()
    out["player_id"] = player_id_num.loc[_valid_session_id_mask].to_numpy()
    return out


def _session_coerce_ids_datetime_and_fnd01(sessions: pd.DataFrame) -> pd.DataFrame:
    """FND-01 dedup and column coercion (same as trainer ``apply_dq`` session prefix)."""
    out = _session_coerce_ids_datetime_prepare(sessions)
    sort_keys = [k for k in ("lud_dtm", "__etl_insert_Dtm") if k in out.columns]
    if sort_keys:
        out = out.sort_values(sort_keys, ascending=False)
    return out.drop_duplicates(subset=["session_id"], keep="first")


def apply_session_dq_keep_manual(sessions: pd.DataFrame) -> pd.DataFrame:
    """Trainer ``apply_dq`` session path without FND-02 (keep ``is_manual==1``).

    Applies FND-01 dedup, deleted/canceled flags, FND-04 activity; does **not** filter
    on ``is_manual`` (high-tier value/tier use case).
    """
    out = _session_coerce_ids_datetime_and_fnd01(sessions)
    if "num_games_with_wager" not in out.columns:
        out["num_games_with_wager"] = 0
    for flag in ("is_manual", "is_deleted", "is_canceled"):
        if flag not in out.columns:
            out[flag] = 0

    dq_mask = (out["is_deleted"] == 0) & (out["is_canceled"] == 0)
    if "turnover" in out.columns or "num_games_with_wager" in out.columns:
        _turnover = out.get("turnover", pd.Series(0.0, index=out.index)).fillna(0)
        _games = out["num_games_with_wager"].fillna(0)
        dq_mask = dq_mask & ((_turnover > 0) | (_games > 0))
    return out.loc[dq_mask].copy()


def apply_session_l0_registry_cleanup(
    sessions: pd.DataFrame, *, batched_shard: bool = False
) -> pd.DataFrame:
    """Apply high-tier session imputation + registry synthetic observed-at column.

    Args:
        batched_shard: If True (row-group batch pipeline), impute/synthetic stats use ``logger.debug``.
    """
    s = impute_session_end_from_session_start(sessions, batched_shard=batched_shard)
    return add_etl_insert_dtm_synthetic(s, batched_shard=batched_shard)


def load_sessions_via_duckdb_local_parquet_contract(session_parquet: Path) -> pd.DataFrame:
    """Parquet row-group read (tqdm) + ``is_deleted`` / ``is_canceled`` gates (no time filter).

    Public function name is historical; behaviour matches the former DuckDB
    ``read_parquet`` + predicates. Does **not** subset by ``session_start_dtm``.
    """
    if not session_parquet.is_file():
        raise FileNotFoundError(session_parquet)

    sess_path_resolved = Path(session_parquet).resolve()
    logger.info(
        "[Step 2] session Parquet ingest: row groups + deleted/canceled gate %s",
        sess_path_resolved,
    )
    sch = frozenset(pq.read_schema(sess_path_resolved).names)
    read_cols = list(_session_preprocess_read_columns_ordered(sch))
    raw = _01_ingest.read_parquet_row_groups_to_pandas(
        sess_path_resolved,
        desc="[Step 2] t_session",
        chunk_filter=_filter_session_l0_deleted_canceled,
        columns=read_cols,
    )
    logger.info(
        "[Step 2] session Parquet ingest loaded %d rows (post deleted/canceled predicates)",
        len(raw),
    )
    return raw


def _session_shard_fill_defaults_for_sql(chunk: pd.DataFrame) -> None:
    """Ensure columns exist so DuckDB FND-04 matches ``apply_session_dq_keep_manual`` defaults."""
    if "num_games_with_wager" not in chunk.columns:
        chunk["num_games_with_wager"] = 0
    if "turnover" not in chunk.columns:
        chunk["turnover"] = 0.0
    for flag in ("is_manual", "is_deleted", "is_canceled"):
        if flag not in chunk.columns:
            chunk[flag] = 0


def _dedupe_order_clause_from_shard(shard_path: Path) -> str:
    """ORDER BY fragment for FND-01 (trainer: ``lud_dtm``, ``__etl_insert_Dtm`` desc)."""
    names = frozenset(pq.ParquetFile(shard_path).schema_arrow.names)
    parts: list[str] = []
    if "lud_dtm" in names:
        parts.append('"lud_dtm" DESC NULLS LAST')
    if "__etl_insert_Dtm" in names:
        parts.append('"__etl_insert_Dtm" DESC NULLS LAST')
    if not parts:
        return '"session_id" DESC NULLS LAST'
    return ", ".join(parts)


def _merge_session_shards_duckdb(
    shards_glob_posix: str,
    output_path_posix: str,
    sample_shard: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> None:
    """FND-01 window dedup + FND-04 filter; write single Parquet via DuckDB ``COPY``."""
    order_clause = _dedupe_order_clause_from_shard(sample_shard)
    sql = f"""
    COPY (
      WITH d AS (
        SELECT
          *,
          ROW_NUMBER() OVER (
            PARTITION BY try_cast("session_id" AS DOUBLE)
            ORDER BY {order_clause}
          ) AS _rn
        FROM read_parquet('{shards_glob_posix}')
        WHERE try_cast("session_id" AS DOUBLE) IS NOT NULL
      ),
      f AS (SELECT * EXCLUDE(_rn) FROM d WHERE _rn = 1)
      SELECT * FROM f
      WHERE COALESCE(try_cast("is_deleted" AS BIGINT), 0) = 0
        AND COALESCE(try_cast("is_canceled" AS BIGINT), 0) = 0
        AND (
          COALESCE(try_cast("turnover" AS DOUBLE), 0) > 0
          OR COALESCE(try_cast("num_games_with_wager" AS DOUBLE), 0) > 0
        )
    ) TO '{output_path_posix}' (FORMAT PARQUET, COMPRESSION SNAPPY);
    """
    ddb = duckdb_runtime if duckdb_runtime is not None else DuckDbRuntimeConfig()
    execute_sql_with_progress_oom_retry(
        ddb,
        sql,
        desc="[Step 2] DuckDB merge shards",
        join_timeout_s=7200.0,
    )


def preprocess_sessions_from_parquet_streaming(
    session_parquet: Path,
    output_path: Path,
    *,
    cfg: SessionPreprocessConfig | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    extra_partition_sources: tuple[Path, ...] | None = None,
) -> tuple[Path, int]:
    """Clean ``t_session`` L0 Parquet → single cleaned Parquet.

    Default uses DuckDB end-to-end (no full pandas materialization). Optional
    ``pandas_shards`` batches multiple row groups per shard to reduce shard count
    (higher peak pandas RAM per batch).

    Args:
        session_parquet: Raw ``gmwds_t_session`` Parquet path.
        output_path: Final cleaned Parquet (parent dirs created).
        cfg: Session engine / shard batching; default :class:`SessionPreprocessConfig`.
        duckdb_runtime: Connection PRAGMAs for all DuckDB steps; default :class:`DuckDbRuntimeConfig`.
        extra_partition_sources: Extra ``t_session`` Parquet shards (duckdb-only) merged via
            multi-file ``read_parquet`` after ``session_parquet``.

    Returns:
        ``(resolved output_path, effective dedup_hash_buckets used)``.
    """
    src = Path(session_parquet).resolve()
    if not src.is_file():
        raise FileNotFoundError(src)
    sources_list: list[Path] = [src]
    if extra_partition_sources:
        uniq: dict[str, Path] = {str(src): src}
        for pp in extra_partition_sources:
            p = Path(pp).resolve()
            if not p.is_file():
                raise FileNotFoundError(p)
            uniq[str(p)] = p
        sources_list = sorted(uniq.values(), key=lambda x: str(x))
    sources = sources_list
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        out.unlink()

    _cfg = cfg if cfg is not None else SessionPreprocessConfig()
    _ddb = duckdb_runtime if duckdb_runtime is not None else DuckDbRuntimeConfig()
    _nb = int(_cfg.dedup_hash_buckets)
    if len(sources) > 1 and _cfg.engine != "duckdb":
        raise ValueError("extra_partition_sources require SessionPreprocessConfig(engine='duckdb')")
    if _nb < 1:
        raise ValueError(f"SessionPreprocessConfig.dedup_hash_buckets must be >= 1, got {_nb}")
    eff_buckets = int(_nb)
    if _cfg.engine == "duckdb":
        ceiling_eff = max(int(PREPROCESS_DEDUP_BUCKET_ESCALATION_CEILING), int(_nb))
        b = int(_nb)
        while True:
            logger.info(
                "[Step 2] session preprocess: DuckDB COPY dedup_hash_buckets=%d → %s",
                b,
                out,
            )
            try:
                _preprocess_sessions_duckdb_single_copy(sources, out, _ddb, dedup_hash_buckets=b)
            except duckdb.OutOfMemoryException as exc:
                if b >= ceiling_eff:
                    logger.warning(
                        "[Step 2] DuckDB OOM at dedup_hash_buckets=%d (ceiling=%d); aborting escalate.",
                        b,
                        ceiling_eff,
                    )
                    raise exc
                nb = min(ceiling_eff, b * 2)
                if nb <= b:
                    raise exc
                logger.warning(
                    "[Step 2] DuckDB OOM at dedup_hash_buckets=%s: %s — doubling buckets -> %s (ceiling=%s)",
                    b,
                    exc,
                    nb,
                    ceiling_eff,
                )
                b = nb
                continue
            eff_buckets = int(b)
            break
    elif _cfg.engine == "pandas_shards":
        _preprocess_sessions_pandas_shard_batches(src, out, _cfg, _ddb)
    else:
        raise ValueError(
            f"Unknown session preprocess engine {_cfg.engine!r}; expected 'duckdb' or 'pandas_shards'"
        )

    nrows = int(pq.ParquetFile(out).metadata.num_rows) if pq.ParquetFile(out).metadata else 0
    logger.info(
        "[Step 2] session preprocess done: %d rows written dedup_hash_buckets_effective=%d %s",
        nrows,
        eff_buckets,
        out,
    )
    return out, int(eff_buckets)


def preprocess_sessions_from_parquet(session_parquet: Path) -> pd.DataFrame:
    """Return cleaned sessions in a DataFrame (small data / tests only).

    Uses :func:`preprocess_sessions_from_parquet_streaming` to a temp file then
    reads the result — **not** suitable when the cleaned table exceeds RAM.
    For large L0 tables, call :func:`preprocess_sessions_from_parquet_streaming`
    and consume the Parquet path without loading all rows into pandas.
    """
    with tempfile.TemporaryDirectory(prefix="hightier_sess_df_") as tmp:
        tpath = Path(tmp) / "cleaned.parquet"
        preprocess_sessions_from_parquet_streaming(session_parquet, tpath)[0]
        return pd.read_parquet(tpath)


def _session_l0_preprocess_py_sha256() -> str:
    """SHA-256 of this module (session pipeline only)."""
    path = Path(__file__).resolve()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merge_session_source_paths(
    primary: Path,
    extra_partition_sources: tuple[Path, ...] | None,
) -> tuple[Path, ...]:
    """Sorted union of resolved session parquet paths."""

    uniq: dict[str, Path] = {}
    p0 = Path(primary).resolve()
    uniq[str(p0)] = p0
    if extra_partition_sources:
        for raw in extra_partition_sources:
            p = Path(raw).resolve()
            uniq[str(p)] = p
    return tuple(sorted(uniq.values(), key=lambda x: str(x)))


def build_session_clean_cache_record(
    source_session_parquet: Path,
    *,
    dedup_hash_buckets: int | None = None,
    extra_source_session_parquets: tuple[Path, ...] | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> dict[str, Any]:
    """Fingerprint: source L0 stats + row count + this module's source hash + dedup buckets."""

    merged = merge_session_source_paths(source_session_parquet, extra_source_session_parquets)
    stats: list[dict[str, Any]] = []
    for sp in merged:
        src = Path(sp).resolve()
        if not src.is_file():
            raise FileNotFoundError(src)
        st = src.stat()
        meta = pq.ParquetFile(src).metadata
        nrows = int(meta.num_rows) if meta is not None else -1
        stats.append(
            {
                "path": str(src),
                "mtime_ns": int(st.st_mtime_ns),
                "size_bytes": int(st.st_size),
                "num_rows": nrows,
            }
        )
    _nb = (
        int(dedup_hash_buckets)
        if dedup_hash_buckets is not None
        else SessionPreprocessConfig().dedup_hash_buckets
    )
    if _nb < 1:
        raise ValueError(f"dedup_hash_buckets must be >= 1, got {_nb}")
    body: dict[str, Any] = {
        "manifest_version": _SESSION_CLEAN_CACHE_MANIFEST_VERSION,
        "preprocess_py_sha256": _session_l0_preprocess_py_sha256(),
        "session_dedup_hash_buckets": _nb,
    }
    if len(merged) == 1:
        body["source_session"] = stats[0]
    else:
        body["manifest_kind"] = "session_clean_merge_v1"
        body["source_sessions"] = stats
    if partition_inventory_fingerprint_sha256_hex is not None:
        body["partition_inventory_fingerprint_sha256_hex"] = str(
            partition_inventory_fingerprint_sha256_hex,
        ).strip()
    return body


def _session_manifest_matches_with_bucket_alias(
    prev: dict[str, Any],
    *,
    nominal_buckets: int,
    build_cur: Callable[[int], dict[str, Any]],
) -> bool:
    """True if *prev* equals current session fingerprint for nominal or persisted dedup buckets."""

    nb = int(nominal_buckets)
    if prev == build_cur(nb):
        return True
    raw = prev.get("session_dedup_hash_buckets")
    try:
        stored = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return False
    if stored is None or stored == nb:
        return False
    return prev == build_cur(stored)


def session_clean_cache_manifest_path(cleaned_parquet: Path) -> Path:
    """Sidecar JSON next to cleaned parquet: ``cleaned__gmwds_t_session.cache.json``."""
    cp = Path(cleaned_parquet)
    return cp.parent / f"{cp.stem}.cache.json"


def session_clean_cache_is_hit(
    source_session_parquet: Path,
    cleaned_parquet: Path,
    *,
    dedup_hash_buckets: int | None = None,
    extra_source_session_parquets: tuple[Path, ...] | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> bool:
    """Return True if cleaned parquet exists and manifest matches current fingerprint."""

    cleaned = Path(cleaned_parquet)
    man = session_clean_cache_manifest_path(cleaned)
    if not cleaned.is_file() or not man.is_file():
        return False
    try:
        prev = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    try:
        nominal = (
            int(dedup_hash_buckets)
            if dedup_hash_buckets is not None
            else SessionPreprocessConfig().dedup_hash_buckets
        )

        def _cur(nb: int) -> dict[str, Any]:
            return build_session_clean_cache_record(
                source_session_parquet,
                dedup_hash_buckets=nb,
                extra_source_session_parquets=extra_source_session_parquets,
                partition_inventory_fingerprint_sha256_hex=partition_inventory_fingerprint_sha256_hex,
            )

        return _session_manifest_matches_with_bucket_alias(
            prev,
            nominal_buckets=nominal,
            build_cur=_cur,
        )
    except (FileNotFoundError, OSError):
        return False


def write_session_clean_cache_manifest(
    source_session_parquet: Path,
    cleaned_parquet: Path,
    *,
    dedup_hash_buckets: int | None = None,
    extra_source_session_parquets: tuple[Path, ...] | None = None,
    partition_inventory_fingerprint_sha256_hex: str | None = None,
) -> Path:
    """Write cache sidecar after a successful cleaned parquet write."""

    rec = build_session_clean_cache_record(
        source_session_parquet,
        dedup_hash_buckets=dedup_hash_buckets,
        extra_source_session_parquets=extra_source_session_parquets,
        partition_inventory_fingerprint_sha256_hex=partition_inventory_fingerprint_sha256_hex,
    )
    mp = session_clean_cache_manifest_path(Path(cleaned_parquet))
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("[Step 2] wrote session clean cache manifest: %s", mp.resolve())
    return mp


def default_cleaned_session_parquet_path() -> Path:
    """Default output: ``trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_session.parquet``."""
    return Path(__file__).resolve().parents[1] / "artifacts" / "cleaned" / "cleaned__gmwds_t_session.parquet"


def write_cleaned_session_parquet(
    sessions: pd.DataFrame,
    output_path: Path | None = None,
) -> Path:
    """Write cleaned ``t_session`` rows to Parquet (creates parent dirs)."""
    path = output_path if output_path is not None else default_cleaned_session_parquet_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    sessions.to_parquet(path, index=False)
    logger.info(
        "[Step 2] wrote cleaned session parquet: %s (%d rows)",
        path.resolve(),
        len(sessions),
    )
    return path


def load_bets_sessions_from_parquet_skeleton(
    bet_path: Path,
    session_path: Path,
    *,
    window_start: datetime,
    extended_end: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """TODO: Predicate pushdown for cleaned ``gmwds_t_bet`` (see ``trainer.load_local_parquet``).

    Use :func:`trainer_hightier.utils.bet_l0_preprocess.preprocess_bets_from_parquet_streaming` with
    :func:`default_cleaned_bet_parquet_path` for offline full-table cleans.
    """
    raise NotImplementedError(
        "load_bets_sessions_from_parquet_skeleton: pending windowed t_bet load; "
        "use preprocess_bets_from_parquet_streaming for full cleaned bet Parquet."
    )
