"""Expanding-window fold definitions for Time-CV feature selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

GAMING_DAY_COLUMN: str = "gaming_day_event"


@dataclass(frozen=True)
class TimeFold:
    """One expanding-window train/validation slice on ``gaming_day_event``."""

    fold_idx: int
    train_start: date
    train_end: date
    val_start: date
    val_end: date
    train_n_days: int
    val_n_days: int


def _coerce_gaming_day(value: object) -> date | None:
    """Normalize one ``gaming_day_event`` cell to ``date``."""

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def unique_gaming_days_from_series(series: pd.Series) -> tuple[date, ...]:
    """Return sorted unique gaming days from a pandas series."""

    if series.empty:
        return ()
    days = {_coerce_gaming_day(v) for v in series.tolist()}
    valid = tuple(sorted(d for d in days if d is not None))
    if not valid:
        raise ValueError(
            f"unique_gaming_days_from_series: no parseable dates in {GAMING_DAY_COLUMN!r}; "
            f"received {len(series)} rows.",
        )
    return valid


def unique_gaming_days_from_parquet(
    parquet_paths: tuple[Path, ...],
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> tuple[date, ...]:
    """Load sorted unique ``gaming_day_event`` values from one or more parquet files."""

    paths = tuple(Path(p).resolve() for p in parquet_paths)
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"unique_gaming_days_from_parquet missing files: {missing}")

    if len(paths) == 1:
        table = pq.read_table(paths[0], columns=[GAMING_DAY_COLUMN])
        return unique_gaming_days_from_series(table.column(GAMING_DAY_COLUMN).to_pandas())

    runtime = duckdb_runtime or DuckDbRuntimeConfig()
    quoted = ", ".join(f"'{str(p).replace(chr(39), chr(39) * 2)}'" for p in paths)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, runtime)
        rows = con.execute(
            f"SELECT DISTINCT CAST({GAMING_DAY_COLUMN} AS DATE) AS gd "
            f"FROM read_parquet([{quoted}]) "
            f"WHERE {GAMING_DAY_COLUMN} IS NOT NULL "
            f"ORDER BY 1",
        ).fetchall()
    finally:
        con.close()
    days = tuple(row[0] for row in rows if row[0] is not None)
    if not days:
        raise ValueError(
            f"unique_gaming_days_from_parquet: no gaming days in {len(paths)} parquet file(s).",
        )
    return days


def generate_expanding_folds(
    gaming_days: tuple[date, ...],
    *,
    n_folds: int,
    val_window_days: int,
    min_train_days: int,
) -> tuple[TimeFold, ...]:
    """Build non-overlapping validation windows with monotonically growing train spans.

    Each fold reserves ``val_window_days`` unique gaming days for validation. Training
    spans all gaming days from the corpus start through the day before validation.
    Requires at least ``min_train_days + n_folds * val_window_days`` unique days.
    """

    if n_folds < 1:
        raise ValueError(f"generate_expanding_folds: n_folds must be >= 1; got {n_folds}.")
    if val_window_days < 1:
        raise ValueError(
            f"generate_expanding_folds: val_window_days must be >= 1; got {val_window_days}.",
        )
    if min_train_days < 1:
        raise ValueError(
            f"generate_expanding_folds: min_train_days must be >= 1; got {min_train_days}.",
        )

    days = tuple(sorted(set(gaming_days)))
    needed = min_train_days + n_folds * val_window_days
    if len(days) < needed:
        raise ValueError(
            f"generate_expanding_folds: need at least {needed} unique gaming days "
            f"(min_train_days={min_train_days}, n_folds={n_folds}, "
            f"val_window_days={val_window_days}); got {len(days)}.",
        )

    folds: list[TimeFold] = []
    for fold_idx in range(n_folds):
        val_start_idx = min_train_days + fold_idx * val_window_days
        val_end_idx = val_start_idx + val_window_days - 1
        train_end_idx = val_start_idx - 1
        val_days = days[val_start_idx : val_end_idx + 1]
        if len(val_days) != val_window_days:
            raise ValueError(
                f"generate_expanding_folds fold {fold_idx}: expected {val_window_days} val days, "
                f"got {len(val_days)}.",
            )
        folds.append(
            TimeFold(
                fold_idx=fold_idx,
                train_start=days[0],
                train_end=days[train_end_idx],
                val_start=val_days[0],
                val_end=val_days[-1],
                train_n_days=train_end_idx + 1,
                val_n_days=len(val_days),
            ),
        )
    return tuple(folds)
