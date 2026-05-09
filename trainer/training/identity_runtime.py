"""Canonical mapping and chunk-scoped PIT identity (DuckDB).

Issue #33 / Phase A: split from ``trainer.training.trainer`` for clearer boundaries
and smaller import surfaces for tools that only need identity helpers.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Set, Tuple

import numpy as np
import pandas as pd

try:
    import config as _cfg  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

from trainer.training.common_runtime import DATA_DIR
from trainer.training.data_sources import _CANONICAL_MAP_SESSION_COLS

logger = logging.getLogger("trainer")

PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
SESSION_AVAIL_DELAY_MIN = _cfg.SESSION_AVAIL_DELAY_MIN
CASINO_PLAYER_ID_CLEAN_SQL: str = getattr(
    _cfg,
    "CASINO_PLAYER_ID_CLEAN_SQL",
    "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END",
)


def _compute_canonical_map_duckdb_budget(available_bytes: Optional[int]) -> int:
    """Compute DuckDB memory_limit (bytes) for canonical mapping. DEC-027: uses config.get_duckdb_memory_limit_bytes."""
    get_limit = getattr(_cfg, "get_duckdb_memory_limit_bytes", None)
    if get_limit is not None:
        return get_limit("canonical_map", available_bytes)
    _min_gb = getattr(_cfg, "DUCKDB_MEMORY_LIMIT_MIN_GB", 1.0)
    lo = int(_min_gb * 1024**3)
    if available_bytes is None:
        return lo
    frac = getattr(_cfg, "DUCKDB_RAM_FRACTION", 0.45)
    _max_gb = getattr(_cfg, "DUCKDB_MEMORY_LIMIT_MAX_GB", 24.0)
    hi = int(_max_gb * 1024**3)
    return max(lo, min(hi, int(available_bytes * frac)))


def _session_row_cutoff_sql(
    *,
    cutoff_str: str,
    legacy_coalesce_cutoff: bool,
    table_alias: str = "",
) -> str:
    """SQL predicate: which session rows are admitted at ``train_end`` (asset-layer contract)."""
    p = f"{table_alias}." if table_alias else ""
    if legacy_coalesce_cutoff:
        return f"COALESCE({p}session_end_dtm, {p}lud_dtm) <= TIMESTAMP '{cutoff_str}'"
    return (
        f"{p}session_end_dtm IS NOT NULL "
        f"AND CAST({p}session_end_dtm AS TIMESTAMP) <= TIMESTAMP '{cutoff_str}'"
    )


def _pit_link_usable_time_sql(
    *,
    delay_min: int,
    legacy_coalesce_cutoff: bool,
    table_alias: str = "",
) -> str:
    """Expression for ``link_usable_time`` in PIT link tables."""
    p = f"{table_alias}." if table_alias else ""
    if legacy_coalesce_cutoff:
        return (
            f"CAST(COALESCE({p}session_end_dtm, {p}lud_dtm) AS TIMESTAMP) "
            f"+ INTERVAL '{delay_min}' MINUTE"
        )
    return f"CAST({p}session_end_dtm AS TIMESTAMP) + INTERVAL '{delay_min}' MINUTE"


def build_canonical_links_and_dummy_from_duckdb(
    session_parquet_path: Path,
    train_end: datetime,
    *,
    legacy_coalesce_cutoff: bool = False,
) -> Tuple[pd.DataFrame, Set[int]]:
    """Build links (player_id, casino_player_id, lud_dtm) and FND-12 dummy set from session Parquet via DuckDB.

    PLAN canonical-mapping-full-history Step 2. Uses FND-01 dedup, FND-02/FND-04 DQ,
    FND-03 (CASINO_PLAYER_ID_CLEAN_SQL), FND-12 dummy detection. train_end should be
    timezone-consistent with the Parquet session timestamps (naive with naive data).

    Default cutoff uses ``session_end_dtm`` only (NULL ends excluded), aligned with
    ``schema/time_semantics_registry.yaml``. Set ``legacy_coalesce_cutoff=True`` only
    for historical parity (COALESCE(session_end_dtm, lud_dtm) cutoff).
    """
    try:
        import duckdb
    except ImportError as e:
        raise RuntimeError(
            "build_canonical_links_and_dummy_from_duckdb requires duckdb; install with: pip install duckdb"
        ) from e

    path = Path(session_parquet_path).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Session Parquet not found: {path}")

    required = set(_CANONICAL_MAP_SESSION_COLS)
    try:
        import pyarrow.parquet as _pq_sess

        schema_names = set(_pq_sess.read_schema(path).names)
    except Exception as e:
        raise ValueError(f"Session Parquet schema read failed: {e}") from e
    missing = required - schema_names
    if missing:
        raise ValueError(f"Session Parquet missing required columns: {sorted(missing)}")

    get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
    if get_cfg is not None:
        threads = get_cfg("canonical_map")[4]
    else:
        threads = getattr(_cfg, "CANONICAL_MAP_DUCKDB_THREADS", 1)
    try:
        threads = max(1, int(threads))
    except (TypeError, ValueError):
        raise ValueError("CANONICAL_MAP_DUCKDB_THREADS must be a positive integer")

    clean_sql = getattr(_cfg, "CASINO_PLAYER_ID_CLEAN_SQL", None) or CASINO_PLAYER_ID_CLEAN_SQL
    if ";" in (clean_sql or ""):
        raise ValueError("CASINO_PLAYER_ID_CLEAN_SQL must not contain semicolon")

    path_escaped = str(path).replace("'", "''")
    _te = train_end
    if hasattr(_te, "tzinfo") and _te.tzinfo is not None:
        _te = pd.Timestamp(_te).tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    cutoff_str = pd.Timestamp(_te).strftime("%Y-%m-%d %H:%M:%S")
    placeholder = PLACEHOLDER_PLAYER_ID

    cte = f"""WITH deduped AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY session_id
            ORDER BY lud_dtm DESC NULLS LAST
        ) AS rn
    FROM read_parquet('{path_escaped}')
)"""
    row_cut = _session_row_cutoff_sql(
        cutoff_str=cutoff_str, legacy_coalesce_cutoff=legacy_coalesce_cutoff
    )
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

    temp_dir_raw = str(DATA_DIR / "duckdb_tmp")
    if "'" in temp_dir_raw:
        temp_dir = str(DATA_DIR / "duckdb_tmp")
    else:
        temp_dir = temp_dir_raw
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    temp_dir_sql = temp_dir.replace("'", "''")

    try:
        import psutil as _psutil

        _avail = int(_psutil.virtual_memory().available)
    except Exception:
        _avail = None
    _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
    if callable(_resolve_runtime):
        runtime_policy = _resolve_runtime(
            "canonical_map",
            _avail,
            input_bytes=int(path.stat().st_size),
        )
    else:
        budget_bytes = _compute_canonical_map_duckdb_budget(_avail)
        runtime_policy = {
            "stage": "canonical_map",
            "memory_limit_bytes": budget_bytes,
            "threads": int(threads),
            "temp_directory": temp_dir,
            "preserve_insertion_order": False,
        }
    mem_gb = float(runtime_policy["memory_limit_bytes"]) / 1024**3

    con = duckdb.connect(":memory:")
    try:
        _apply_runtime = getattr(_cfg, "apply_duckdb_runtime", None)
        if callable(_apply_runtime):
            _apply_runtime(con, runtime_policy)
        else:
            con.execute(f"SET memory_limit = '{mem_gb}GB'")
            con.execute(f"SET threads = {int(threads)}")
            con.execute(f"SET temp_directory = '{temp_dir_sql}'")
            con.execute("SET preserve_insertion_order = false")
        logger.info(
            "Canonical mapping DuckDB runtime: memory_limit=%.2fGB  threads=%d  temp_directory=%s",
            mem_gb,
            int(runtime_policy["threads"]),
            str(runtime_policy["temp_directory"]),
        )
        try:
            links_df = con.execute(links_sql).df()
            dummy_df = con.execute(dummy_sql).df()
        except Exception as exc:
            _hint = (
                " If OOM: ensure temp_directory is writable, or reduce CANONICAL_MAP_DUCKDB_THREADS / "
                "memory limit; see PLAN Canonical mapping DuckDB 對齊 Step 7."
            )
            raise RuntimeError(
                f"Canonical mapping DuckDB query failed: {exc!s}.{_hint}"
            ) from exc
        dummy_pids: Set[int] = set() if dummy_df.empty else set(dummy_df["player_id"].astype(int).tolist())
        return (links_df, dummy_pids)
    finally:
        con.close()


def build_canonical_links_and_dummy_from_duckdb_legacy(
    session_parquet_path: Path,
    train_end: datetime,
) -> Tuple[pd.DataFrame, Set[int]]:
    """Legacy reference: COALESCE(session_end_dtm, lud_dtm) <= cutoff. Do not use in new pipelines."""
    return build_canonical_links_and_dummy_from_duckdb(
        session_parquet_path, train_end, legacy_coalesce_cutoff=True
    )


def build_pit_session_links_from_duckdb(
    session_parquet_path: Path,
    train_end: datetime,
    *,
    legacy_coalesce_cutoff: bool = False,
) -> pd.DataFrame:
    """Build PIT session link timeline for :func:`merge_pit_canonical_to_bets` (B3).

    Same dedup/DQ/cutoff as canonical DuckDB links, plus ``link_usable_time`` column.
    """
    try:
        import duckdb
    except ImportError as e:
        raise RuntimeError(
            "build_pit_session_links_from_duckdb requires duckdb; install with: pip install duckdb"
        ) from e

    path = Path(session_parquet_path).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Session Parquet not found: {path}")

    required = set(_CANONICAL_MAP_SESSION_COLS)
    try:
        import pyarrow.parquet as _pq_sess

        schema_names = set(_pq_sess.read_schema(path).names)
    except Exception as e:
        raise ValueError(f"Session Parquet schema read failed: {e}") from e
    missing = required - schema_names
    if missing:
        raise ValueError(f"Session Parquet missing required columns: {sorted(missing)}")

    get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
    if get_cfg is not None:
        threads = get_cfg("canonical_map")[4]
    else:
        threads = getattr(_cfg, "CANONICAL_MAP_DUCKDB_THREADS", 1)
    try:
        threads = max(1, int(threads))
    except (TypeError, ValueError):
        raise ValueError("CANONICAL_MAP_DUCKDB_THREADS must be a positive integer")

    clean_sql = getattr(_cfg, "CASINO_PLAYER_ID_CLEAN_SQL", None) or CASINO_PLAYER_ID_CLEAN_SQL
    if ";" in (clean_sql or ""):
        raise ValueError("CASINO_PLAYER_ID_CLEAN_SQL must not contain semicolon")

    path_escaped = str(path).replace("'", "''")
    _te = train_end
    if hasattr(_te, "tzinfo") and _te.tzinfo is not None:
        _te = pd.Timestamp(_te).tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    cutoff_str = pd.Timestamp(_te).strftime("%Y-%m-%d %H:%M:%S")
    placeholder = PLACEHOLDER_PLAYER_ID
    delay_min = int(SESSION_AVAIL_DELAY_MIN)

    cte = f"""WITH deduped AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY session_id
            ORDER BY lud_dtm DESC NULLS LAST
        ) AS rn
    FROM read_parquet('{path_escaped}')
)"""
    row_cut = _session_row_cutoff_sql(
        cutoff_str=cutoff_str, legacy_coalesce_cutoff=legacy_coalesce_cutoff
    )
    link_usable = _pit_link_usable_time_sql(
        delay_min=delay_min, legacy_coalesce_cutoff=legacy_coalesce_cutoff
    )
    pit_sql = f"""
{cte}
SELECT player_id,
       ({clean_sql}) AS casino_player_id,
       lud_dtm,
       {link_usable} AS link_usable_time
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {placeholder}
  AND {row_cut}
  AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
  AND ({clean_sql}) IS NOT NULL"""

    temp_dir_raw = str(DATA_DIR / "duckdb_tmp")
    temp_dir = temp_dir_raw if "'" not in temp_dir_raw else str(DATA_DIR / "duckdb_tmp")
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    temp_dir_sql = temp_dir.replace("'", "''")

    try:
        import psutil as _psutil

        _avail = int(_psutil.virtual_memory().available)
    except Exception:
        _avail = None
    _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
    if callable(_resolve_runtime):
        runtime_policy = _resolve_runtime(
            "canonical_map",
            _avail,
            input_bytes=int(path.stat().st_size),
        )
    else:
        budget_bytes = _compute_canonical_map_duckdb_budget(_avail)
        runtime_policy = {
            "stage": "canonical_map",
            "memory_limit_bytes": budget_bytes,
            "threads": int(threads),
            "temp_directory": temp_dir,
            "preserve_insertion_order": False,
        }
    mem_gb = float(runtime_policy["memory_limit_bytes"]) / 1024**3

    con = duckdb.connect(":memory:")
    try:
        _apply_runtime = getattr(_cfg, "apply_duckdb_runtime", None)
        if callable(_apply_runtime):
            _apply_runtime(con, runtime_policy)
        else:
            con.execute(f"SET memory_limit = '{mem_gb}GB'")
            con.execute(f"SET threads = {int(runtime_policy['threads'])}")
            con.execute(f"SET temp_directory = '{temp_dir_sql}'")
            con.execute("SET preserve_insertion_order = false")
        out = con.execute(pit_sql).df()
    finally:
        con.close()
    if out.empty:
        return pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm", "link_usable_time"])
    out["casino_player_id"] = out["casino_player_id"].astype(str)
    out["lud_dtm"] = pd.to_datetime(out["lud_dtm"], errors="coerce")
    out["link_usable_time"] = pd.to_datetime(out["link_usable_time"], errors="coerce")
    return out.sort_values(
        ["player_id", "link_usable_time", "lud_dtm"],
        ascending=True,
        kind="stable",
    ).reset_index(drop=True)


def build_pit_session_links_from_duckdb_legacy(
    session_parquet_path: Path,
    train_end: datetime,
) -> pd.DataFrame:
    """Legacy PIT links using COALESCE(session_end_dtm, lud_dtm); reference only."""
    return build_pit_session_links_from_duckdb(
        session_parquet_path, train_end, legacy_coalesce_cutoff=True
    )


def attach_pit_identity_chunk_duckdb(
    bets_df: pd.DataFrame,
    session_parquet_path: Path,
    observation_end: datetime,
    *,
    legacy_coalesce_cutoff: bool = False,
) -> pd.DataFrame:
    """Attach ``canonical_id`` / ``_pit_rated`` using chunk-scoped DuckDB ASOF join.

    Uses only distinct ``player_id`` observed in *bets_df* and session links with
    business time <= ``observation_end`` so we avoid global 10M+ link materialization.
    """
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError(
            "attach_pit_identity_chunk_duckdb requires duckdb; install with: pip install duckdb"
        ) from exc

    required = {"bet_id", "player_id", "payout_complete_dtm"}
    missing = required - set(bets_df.columns)
    if missing:
        raise ValueError(f"attach_pit_identity_chunk_duckdb: missing columns {sorted(missing)}")
    if bets_df.empty:
        out = bets_df.copy()
        out["canonical_id"] = out["player_id"].astype(str)
        out["_pit_rated"] = False
        return out
    path = Path(session_parquet_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Session Parquet not found: {path}")

    work = bets_df.loc[:, ["bet_id", "player_id", "payout_complete_dtm"]].copy()
    work["payout_complete_dtm"] = pd.to_datetime(work["payout_complete_dtm"], errors="coerce")
    if work["payout_complete_dtm"].dt.tz is not None:
        work["payout_complete_dtm"] = work["payout_complete_dtm"].dt.tz_convert("Asia/Hong_Kong").dt.tz_localize(None)
    work["_pit_row"] = np.arange(len(work), dtype=np.int64)
    work = work[work["player_id"].notna() & work["payout_complete_dtm"].notna()].copy()
    if work.empty:
        out = bets_df.copy()
        out["canonical_id"] = out["player_id"].astype(str)
        out["_pit_rated"] = False
        return out

    _oe = pd.Timestamp(observation_end)
    if _oe.tzinfo is not None:
        _oe = _oe.tz_convert("Asia/Hong_Kong").tz_localize(None)
    cutoff_str = _oe.strftime("%Y-%m-%d %H:%M:%S")
    delay_min = int(SESSION_AVAIL_DELAY_MIN)
    clean_sql = getattr(_cfg, "CASINO_PLAYER_ID_CLEAN_SQL", None) or CASINO_PLAYER_ID_CLEAN_SQL
    if ";" in str(clean_sql):
        raise ValueError("CASINO_PLAYER_ID_CLEAN_SQL must not contain semicolon")
    path_sql = str(path).replace("'", "''")
    row_cut = _session_row_cutoff_sql(
        cutoff_str=cutoff_str,
        legacy_coalesce_cutoff=legacy_coalesce_cutoff,
        table_alias="d",
    )
    link_usable = _pit_link_usable_time_sql(
        delay_min=delay_min,
        legacy_coalesce_cutoff=legacy_coalesce_cutoff,
        table_alias="d",
    )

    sql = f"""
WITH bets_identity AS (
    SELECT _pit_row, bet_id, player_id, CAST(payout_complete_dtm AS TIMESTAMP) AS payout_complete_dtm
    FROM bets_input
),
player_scope AS (
    SELECT DISTINCT player_id
    FROM bets_identity
),
deduped AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY session_id
               ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC NULLS LAST
           ) AS rn
    FROM read_parquet('{path_sql}')
),
links AS (
    SELECT d.player_id,
           ({clean_sql}) AS casino_player_id,
           CAST(d.lud_dtm AS TIMESTAMP) AS lud_dtm,
           {link_usable} AS link_usable_time
    FROM deduped d
    INNER JOIN player_scope p
        ON d.player_id = p.player_id
    WHERE d.rn = 1
      AND d.is_manual = 0
      AND d.is_deleted = 0 AND d.is_canceled = 0
      AND d.player_id IS NOT NULL AND d.player_id != {PLACEHOLDER_PLAYER_ID}
      AND {row_cut}
      AND (COALESCE(d.turnover, 0) > 0 OR COALESCE(d.num_games_with_wager, 0) > 0)
      AND ({clean_sql}) IS NOT NULL
),
asof_joined AS (
    SELECT b._pit_row,
           b.player_id,
           b.bet_id,
           b.payout_complete_dtm,
           l.casino_player_id
    FROM bets_identity b
    ASOF LEFT JOIN links l
      ON b.player_id = l.player_id
     AND b.payout_complete_dtm >= l.link_usable_time
)
SELECT _pit_row,
       player_id,
       casino_player_id
FROM asof_joined
ORDER BY _pit_row
"""

    try:
        import psutil as _psutil

        _avail = int(_psutil.virtual_memory().available)
    except Exception:
        _avail = None
    _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
    if callable(_resolve_runtime):
        runtime_policy = _resolve_runtime("canonical_map", _avail, input_bytes=int(path.stat().st_size))
    else:
        runtime_policy = {
            "memory_limit_bytes": int(2 * 1024**3),
            "threads": 1,
            "temp_directory": str(DATA_DIR / "duckdb_tmp"),
            "preserve_insertion_order": False,
        }

    con = duckdb.connect(":memory:")
    try:
        _apply_runtime = getattr(_cfg, "apply_duckdb_runtime", None)
        if callable(_apply_runtime):
            _apply_runtime(con, runtime_policy)
        else:
            _mem_bytes = int(runtime_policy.get("memory_limit_bytes", int(2 * 1024**3)))
            _threads = int(runtime_policy.get("threads", 1))
            _temp_dir = str(runtime_policy.get("temp_directory", str(DATA_DIR / "duckdb_tmp")))
            _preserve = bool(runtime_policy.get("preserve_insertion_order", False))
            _mem_gb = max(1.0, float(_mem_bytes) / (1024**3))
            _temp_sql = _temp_dir.replace("'", "''")
            con.execute(f"SET memory_limit = '{_mem_gb:.2f}GB'")
            con.execute(f"SET threads = {max(1, _threads)}")
            con.execute(f"SET temp_directory = '{_temp_sql}'")
            con.execute(
                f"SET preserve_insertion_order = {'true' if _preserve else 'false'}"
            )
        con.register("bets_input", work)
        t0 = time.perf_counter()
        out_df = con.execute(sql).df()
        elapsed = time.perf_counter() - t0
    finally:
        con.close()

    logger.info(
        "Chunk PIT identity DuckDB: players=%d links_join_rows=%d elapsed=%.1fs mem_limit=%.2fGB threads=%d",
        int(work["player_id"].nunique()),
        len(out_df),
        elapsed,
        float(runtime_policy["memory_limit_bytes"]) / 1024**3,
        int(runtime_policy["threads"]),
    )
    merged = bets_df.copy()
    merged["canonical_id"] = merged["player_id"].astype(str)
    merged["_pit_rated"] = False
    if out_df.empty:
        return merged
    out_df = out_df.sort_values("_pit_row", kind="stable")
    _cpid = out_df["casino_player_id"]
    _pid = out_df["player_id"].astype(str)
    _canon = _cpid.where(_cpid.notna(), _pid).astype(str).to_numpy()
    _rated = _cpid.notna().to_numpy()
    _row = out_df["_pit_row"].to_numpy(dtype=np.int64)
    _n = len(merged)
    _ok = (_row >= 0) & (_row < _n)
    if not np.all(_ok):
        logger.warning(
            "Chunk PIT identity DuckDB: dropped %d out-of-range _pit_row writebacks",
            int((~_ok).sum()),
        )
    _row_pos = _row[_ok]
    _canon_pos = _canon[_ok]
    _rated_pos = _rated[_ok]
    _canon_arr = merged["canonical_id"].to_numpy(copy=True)
    _rated_arr = merged["_pit_rated"].to_numpy(copy=True)
    _canon_arr[_row_pos] = _canon_pos
    _rated_arr[_row_pos] = _rated_pos
    merged["canonical_id"] = _canon_arr
    merged["_pit_rated"] = _rated_arr
    return merged


def attach_pit_identity_chunk_duckdb_legacy(
    bets_df: pd.DataFrame,
    session_parquet_path: Path,
    observation_end: datetime,
) -> pd.DataFrame:
    """Legacy COALESCE(session_end_dtm, lud_dtm) PIT attach; reference only."""
    return attach_pit_identity_chunk_duckdb(
        bets_df, session_parquet_path, observation_end, legacy_coalesce_cutoff=True
    )


def _apply_cutoff_window_identity_fallback(
    bets_df: pd.DataFrame,
    canonical_map: pd.DataFrame,
) -> pd.DataFrame:
    """Apply cutoff-window identity mapping without ``canonical_id`` collisions."""
    out = bets_df.drop(columns=["canonical_id", "_pit_rated"], errors="ignore").copy()
    if not canonical_map.empty and "player_id" in canonical_map.columns:
        out = out.merge(
            canonical_map[["player_id", "canonical_id"]].drop_duplicates("player_id"),
            on="player_id",
            how="left",
        )
    else:
        out["canonical_id"] = out["player_id"].astype(str)
    out["canonical_id"] = out["canonical_id"].fillna(out["player_id"].astype(str))
    out["canonical_id"] = out["canonical_id"].astype(str)
    return out
