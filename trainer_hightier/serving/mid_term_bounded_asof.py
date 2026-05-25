"""Option B bounded mid-term ASOF helpers (train enrich, Feast scorer, offline replay)."""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from trainer_hightier.config import (
    MID_TERM_ANCHOR_AUDIT_COLUMN,
    MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN,
    MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN,
    PRODUCTION_MID_ASOF_BACKFILL_DAYS,
)

FEAST_MID_ANCHOR_COLUMN: str = "anchor_gaming_day"


def resolve_mid_asof_backfill_days(n_days: int | None = None) -> int:
    """Return validated bounded ASOF window length (gaming days)."""
    n = int(PRODUCTION_MID_ASOF_BACKFILL_DAYS if n_days is None else n_days)
    if n < 1:
        raise ValueError(f"mid ASOF backfill days must be >= 1, got {n!r}")
    return n


def _to_date(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    return ts.date()


def is_mid_anchor_valid(
    gaming_day: Any,
    anchor_day: Any,
    n_days: int | None = None,
) -> bool:
    """True when anchor is strictly before gaming_day and age is in ``[1, N]``."""
    g = _to_date(gaming_day)
    a = _to_date(anchor_day)
    if g is None or a is None:
        return False
    if a >= g:
        return False
    age = (g - a).days
    n = resolve_mid_asof_backfill_days(n_days)
    return 1 <= age <= n


def mid_asof_lateral_lower_bound_sql(gday_sql: str, n_days: int | None = None) -> str:
    """SQL fragment: anchor must be within the last N gaming days before bet day."""
    n = resolve_mid_asof_backfill_days(n_days)
    return (
        f"AND CAST(s.anchor_gaming_day AS DATE) >= "
        f"CAST({gday_sql} AS DATE) - INTERVAL '{n}' DAY"
    )


def mid_snapshot_missing_flag_sql(
    anchor_sql: str,
    gday_sql: str,
    n_days: int | None = None,
) -> str:
    """DuckDB expression for per-row ``mid_term_snapshot_missing_flag``."""
    n = resolve_mid_asof_backfill_days(n_days)
    return f"""CASE
      WHEN {anchor_sql} IS NULL OR {gday_sql} IS NULL THEN 1
      WHEN DATE_DIFF('day', CAST({anchor_sql} AS DATE), CAST({gday_sql} AS DATE)) NOT BETWEEN 1 AND {n} THEN 1
      ELSE 0
    END"""


def _resolve_anchor_series(df: pd.DataFrame, anchor_column: str | None) -> pd.Series:
    if anchor_column and anchor_column in df.columns:
        return df[anchor_column]
    for col in (FEAST_MID_ANCHOR_COLUMN, MID_TERM_ANCHOR_AUDIT_COLUMN):
        if col in df.columns:
            return df[col]
    return pd.Series(pd.NA, index=df.index)


def apply_mid_term_bounded_asof(
    df: pd.DataFrame,
    *,
    mid_primitive_columns: tuple[str, ...],
    gaming_day_column: str = "gaming_day",
    anchor_column: str | None = None,
    n_days: int | None = None,
    write_audit_columns: bool = True,
) -> pd.DataFrame:
    """Null mid primitives outside the bounded window; write audit columns when requested."""
    if df.empty or not mid_primitive_columns:
        return df
    if gaming_day_column not in df.columns:
        raise ValueError(
            f"apply_mid_term_bounded_asof requires {gaming_day_column!r}; "
            f"columns={list(df.columns)}",
        )
    n = resolve_mid_asof_backfill_days(n_days)
    out = df.copy()
    gdays = pd.to_datetime(out[gaming_day_column], errors="coerce").dt.normalize()
    anchors = pd.to_datetime(_resolve_anchor_series(out, anchor_column), errors="coerce").dt.normalize()
    age_days = (gdays - anchors).dt.days
    valid = anchors.notna() & gdays.notna() & (age_days >= 1) & (age_days <= n)
    missing = (~valid).astype(np.int8)

    if write_audit_columns:
        out[MID_TERM_ANCHOR_AUDIT_COLUMN] = anchors.dt.date
        out[MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN] = age_days.where(valid, other=pd.NA).astype("Int64")
        out[MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN] = missing

    for col in mid_primitive_columns:
        if col not in out.columns:
            continue
        out.loc[~valid, col] = np.nan
    return out
