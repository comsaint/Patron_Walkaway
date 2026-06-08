"""Partition-pruned DuckDB reads for bounded short-term hot pools.

Kept separate from :mod:`trainer_hightier.utils.bet_l0_preprocess` so pool-read
optimizations do not invalidate L1 bet-base cache keys tied to preprocess code hash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import duckdb
import pandas as pd

from trainer_hightier.config import DuckDbRuntimeConfig, HK_TZ
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_dataset_glob_posix,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

MONTH_HOT_POOL_TABLE: Final[str] = "_month_hot_pool_bets"

_POOL_SOURCE_SELECT_SQL: Final[str] = """
    SELECT
        b.bet_id,
        b.is_back_bet,
        b.bet_type,
        b.type_of_bet,
        b.payout_complete_dtm,
        CAST(b.gaming_day_event AS TIMESTAMP) AS gaming_day_event,
        b.session_id,
        b.player_id,
        b.table_id,
        b.wager,
        b.casino_win,
        b.payout_odds,
        b.theo_win,
        b.base_ha
"""


@dataclass(frozen=True)
class MonthHotPoolSession:
    """Reusable DuckDB temp table of partition-pruned cleaned bets for one miss month."""

    conn: duckdb.DuckDBPyConnection
    table_name: str
    payout_yyyymm: str
    row_count: int

    def close(self) -> None:
        """Release the DuckDB connection backing this month pool."""
        self.conn.close()


def list_cleaned_bet_gaming_month_partitions(cleaned_root: Path) -> tuple[str, ...]:
    """Return sorted ``YYYYMM`` keys from ``gaming_month=*`` partition directories."""
    root = Path(cleaned_root).resolve()
    if not root.is_dir():
        return ()
    months: list[str] = []
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("gaming_month="):
            continue
        ym = child.name.split("=", 1)[1]
        if len(ym) == 6 and ym.isdigit():
            months.append(ym)
    return tuple(sorted(months))


def _yyyymm_range_inclusive(start_ym: str, end_ym: str) -> tuple[str, ...]:
    """Enumerate calendar months from *start_ym* through *end_ym* (six-digit ``YYYYMM``)."""
    from trainer_hightier.utils.cache_invalidation_v1 import shift_calendar_month

    cur = str(start_ym).strip()
    end = str(end_ym).strip()
    if cur > end:
        return ()
    out: list[str] = []
    while cur <= end:
        out.append(cur)
        if cur == end:
            break
        cur = shift_calendar_month(cur, delta_months=1)
    return tuple(out)


def gaming_months_for_bounded_pool(
    *,
    pool_start: datetime,
    pool_end: datetime,
    payout_yyyymm: str | None = None,
    hk_tz: str = HK_TZ,
) -> tuple[str, ...]:
    """Return partition months that may contain hot-pool rows for a bounded PIT window."""
    from trainer_hightier.utils.cache_invalidation_v1 import shift_calendar_month

    if payout_yyyymm is not None:
        ym = str(payout_yyyymm).strip()
        if len(ym) != 6 or not ym.isdigit():
            raise ValueError(f"payout_yyyymm must be six digits, got {payout_yyyymm!r}")
        return (shift_calendar_month(ym, delta_months=-1), ym)

    ps = pd.Timestamp(pool_start)
    pe = pd.Timestamp(pool_end)
    if ps.tzinfo is None:
        ps = ps.tz_localize("UTC")
    if pe.tzinfo is None:
        pe = pe.tz_localize("UTC")
    ps_hk = ps.tz_convert(hk_tz)
    pe_hk = pe.tz_convert(hk_tz)
    start_ym = shift_calendar_month(ps_hk.strftime("%Y%m"), delta_months=-1)
    end_ym = pe_hk.strftime("%Y%m")
    return _yyyymm_range_inclusive(start_ym, end_ym)


def cleaned_bet_pool_read_parquet_sql(
    cleaned_root: Path,
    *,
    pool_start: datetime,
    pool_end: datetime,
    payout_yyyymm: str | None = None,
    hk_tz: str = HK_TZ,
) -> str:
    """DuckDB ``read_parquet`` clause scoped to hot-pool calendar months (partition prune)."""
    root = Path(cleaned_root).resolve()
    if root.is_file():
        return resolved_cleaned_bet_read_parquet_sql(root)
    if not root.is_dir():
        raise FileNotFoundError(f"cleaned bet artifact not found: {root}")

    available = set(list_cleaned_bet_gaming_month_partitions(root))
    wanted = set(
        gaming_months_for_bounded_pool(
            pool_start=pool_start,
            pool_end=pool_end,
            payout_yyyymm=payout_yyyymm,
            hk_tz=hk_tz,
        ),
    )
    months = sorted(available & wanted)
    if not months:
        return resolved_cleaned_bet_read_parquet_sql(root)

    globs: list[str] = []
    for ym in months:
        part = root / f"gaming_month={ym}"
        if not part.is_dir():
            continue
        glo = cleaned_bet_dataset_glob_posix(part).replace("'", "''")
        globs.append(f"'{glo}'")
    if not globs:
        return resolved_cleaned_bet_read_parquet_sql(root)
    if len(globs) == 1:
        return f"read_parquet({globs[0]}, hive_partitioning=false)"
    return f"read_parquet([{','.join(globs)}], hive_partitioning=false)"


def _player_id_filter_sql(restrict_player_ids: tuple[int, ...] | None) -> str:
    """Return a ``WHERE`` clause restricting pool rows to *restrict_player_ids*."""
    if not restrict_player_ids:
        return ""
    ids_sql = ",".join(str(int(pid)) for pid in restrict_player_ids)
    return f" WHERE b.player_id IN ({ids_sql})"


def open_month_hot_pool_session(
    cleaned_root: Path,
    *,
    payout_yyyymm: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    hk_tz: str = HK_TZ,
    restrict_player_ids: tuple[int, ...] | None = None,
    table_name: str = MONTH_HOT_POOL_TABLE,
) -> MonthHotPoolSession:
    """Load partition-pruned cleaned bets once for a miss-month short PIT cold build."""
    ym = str(payout_yyyymm).strip()
    if len(ym) != 6 or not ym.isdigit():
        raise ValueError(f"payout_yyyymm must be six digits, got {payout_yyyymm!r}")
    root = Path(cleaned_root).resolve()
    bet_from = cleaned_bet_pool_read_parquet_sql(
        root,
        pool_start=datetime(2000, 1, 1, tzinfo=timezone.utc),
        pool_end=datetime(2099, 1, 1, tzinfo=timezone.utc),
        payout_yyyymm=ym,
        hk_tz=hk_tz,
    )
    player_filter = _player_id_filter_sql(restrict_player_ids)
    conn = duckdb.connect(database=":memory:")
    apply_duckdb_runtime_pragmas(conn, duckdb_runtime)
    conn.execute(
        f"""
        CREATE TEMP TABLE {table_name} AS
        {_POOL_SOURCE_SELECT_SQL}
        FROM {bet_from} AS b
        {player_filter}
        """,
    )
    row = conn.execute(f"SELECT COUNT(*)::BIGINT FROM {table_name}").fetchone()
    row_count = int(row[0]) if row else 0
    logger.info(
        "[month_hot_pool] loaded yyyymm=%s rows=%d table=%s restrict_pids=%s",
        ym,
        row_count,
        table_name,
        len(restrict_player_ids or ()),
    )
    return MonthHotPoolSession(
        conn=conn,
        table_name=table_name,
        payout_yyyymm=ym,
        row_count=row_count,
    )
