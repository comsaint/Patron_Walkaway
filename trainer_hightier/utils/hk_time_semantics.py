"""HK timezone semantics for ``gaming_day_event`` migration (Day 1 full cutover)."""

from __future__ import annotations

from datetime import date

from typing import Final, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from trainer_hightier.config import HK_TZ

GAMING_DAY_EVENT_COLUMN: Final[str] = "gaming_day_event"
ANCHOR_GAMING_DAY_EVENT_COLUMN: Final[str] = "anchor_gaming_day_event"

# Timestamp columns converted to HK tz-aware during L0 cleansing (Included + Excluded in read set).
T_BET_TIMESTAMP_COLUMNS: Final[tuple[str, ...]] = (
    "payout_complete_dtm",
    "__etl_insert_Dtm",
)

T_SESSION_TIMESTAMP_COLUMNS: Final[tuple[str, ...]] = (
    "session_start_dtm",
    "session_end_dtm",
    "lud_dtm",
    "__etl_insert_Dtm",
)

T_BET_EVENT_TIME_COLUMN: Final[str] = "payout_complete_dtm"
T_BET_OBSERVED_AT_COLUMN: Final[str] = "__etl_insert_Dtm"
T_SESSION_EVENT_TIME_COLUMN: Final[str] = "session_end_dtm"
T_SESSION_OBSERVED_AT_COLUMN: Final[str] = "__etl_insert_Dtm"


def pandas_ts_series_to_hk_l0_contract(series: pd.Series) -> pd.Series:
    """Normalize timestamps to HK tz-aware per cleaned L0 parquet contract.

    tz-aware values are converted to HK. tz-naive values are treated as HK wall
    clock (not UTC) because cleaned bet/session parquets store naive HK instants.
    """
    s = pd.to_datetime(series, errors="coerce")
    if getattr(s.dt, "tz", None) is None:
        return s.dt.tz_localize(
            ZoneInfo(HK_TZ),
            ambiguous="NaT",
            nonexistent="shift_forward",
        )
    return s.dt.tz_convert(ZoneInfo(HK_TZ))


def duckdb_quote_ident(name: str) -> str:
    """Return a double-quoted DuckDB identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def duckdb_timestamp_to_hk_sql(column: str, *, alias: str | None = None) -> str:
    """SQL expression: convert a timestamp column to HK tz-aware (idempotent on HK-aware input)."""
    ident = duckdb_quote_ident(column)
    out_name = alias or column
    out_ident = duckdb_quote_ident(out_name)
    return (
        f"timezone('{HK_TZ}', TRY_CAST({ident} AS TIMESTAMPTZ)) AS {out_ident}"
    )


def duckdb_hk_timestamp_columns_select(
    columns: Sequence[str],
    *,
    available: frozenset[str],
) -> list[str]:
    """Build SELECT fragments for HK-normalized timestamp columns present in *available*."""
    return [
        duckdb_timestamp_to_hk_sql(col)
        for col in columns
        if col in available
    ]


def duckdb_gaming_day_event_sql(event_time_column: str) -> str:
    """Derive ``gaming_day_event`` as HK calendar date from an event timestamp column."""
    ident = duckdb_quote_ident(event_time_column)
    return (
        f"CAST(timezone('{HK_TZ}', TRY_CAST({ident} AS TIMESTAMPTZ)) AS DATE) "
        f"AS {duckdb_quote_ident(GAMING_DAY_EVENT_COLUMN)}"
    )


def duckdb_gaming_day_event_scope_and_sql(
    *,
    min_day: date | None,
    max_day: date | None,
    column_expr: str | None = None,
) -> str:
    """Return `` AND …`` SQL suffix for inclusive ``gaming_day_event`` bounds.

    Parameters
    ----------
    min_day, max_day
        Inclusive calendar-day bounds; ``None`` disables that side.
    column_expr
        DuckDB DATE expression to compare (defaults to cast ``gaming_day_event``).
    """
    col = column_expr or f'TRY_CAST({duckdb_quote_ident(GAMING_DAY_EVENT_COLUMN)} AS DATE)'
    parts: list[str] = []
    if min_day is not None:
        parts.append(f"{col} >= DATE '{min_day.isoformat()}'")
    if max_day is not None:
        parts.append(f"{col} <= DATE '{max_day.isoformat()}'")
    if not parts:
        return ""
    return " AND " + " AND ".join(parts)


def duckdb_etl_before_event_violation_count_sql(
    *,
    event_time_column: str,
    observed_at_column: str = T_BET_OBSERVED_AT_COLUMN,
    table_alias: str = "src",
) -> str:
    """Return SQL counting rows where ``observed_at < event_time`` (contract violation)."""
    evt = duckdb_quote_ident(event_time_column)
    obs = duckdb_quote_ident(observed_at_column)
    return f"""
SELECT COUNT(*)::BIGINT AS n_violations
FROM {table_alias} AS {table_alias}
WHERE TRY_CAST({table_alias}.{evt} AS TIMESTAMPTZ) IS NOT NULL
  AND TRY_CAST({table_alias}.{obs} AS TIMESTAMPTZ) IS NOT NULL
  AND TRY_CAST({table_alias}.{obs} AS TIMESTAMPTZ)
      < TRY_CAST({table_alias}.{evt} AS TIMESTAMPTZ)
""".strip()


def assert_no_etl_before_event_violations(
    con: object,
    *,
    src_read_parquet_clause: str,
    event_time_column: str,
    observed_at_column: str = T_BET_OBSERVED_AT_COLUMN,
    context: str,
) -> None:
    """Hard-fail when any row has ``observed_at < event_time``."""
    sql = f"""
WITH src AS (
  SELECT * FROM {src_read_parquet_clause}
)
{duckdb_etl_before_event_violation_count_sql(
    event_time_column=event_time_column,
    observed_at_column=observed_at_column,
    table_alias="src",
)}
"""
    row = con.execute(sql).fetchone()  # type: ignore[attr-defined]
    n = int(row[0]) if row else 0
    if n > 0:
        raise ValueError(
            f"{context}: ETL sanity gate failed — "
            f"{observed_at_column} < {event_time_column} on {n} row(s); "
            "expected observed_at >= event_time"
        )
