"""Inclusive calendar-day lists for LDA orchestration (YYYY-MM-DD strings)."""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path


def inclusive_iso_date_strings(date_from: str, date_to: str) -> list[str]:
    """Return each calendar day from ``date_from`` through ``date_to`` inclusive as ``YYYY-MM-DD``.

    Raises:
        ValueError: if strings are not valid dates or ``date_to`` is before ``date_from``.
    """
    a = _parse_iso_date(date_from, param="date_from")
    b = _parse_iso_date(date_to, param="date_to")
    if b < a:
        raise ValueError(f"date_to {date_to!r} must be on or after date_from {date_from!r}")
    out: list[str] = []
    cur = a
    while cur <= b:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _parse_iso_date(value: str, *, param: str) -> date:
    """Parse ``YYYY-MM-DD`` into a :class:`datetime.date`.

    Raises:
        ValueError: if the string is not ``YYYY-MM-DD`` or not a real calendar day.
    """
    s = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"{param} must be YYYY-MM-DD, got {value!r}")
    y, m, d = (int(s[0:4]), int(s[5:7]), int(s[8:10]))
    try:
        return date(y, m, d)
    except ValueError as exc:
        raise ValueError(f"{param} invalid calendar date {value!r}: {exc}") from exc


def distinct_gaming_days_from_t_bet_parquet(parquet_path: Path) -> list[str]:
    """Return sorted ``YYYY-MM-DD`` strings for each ``gaming_day`` present in a Parquet file.

    Uses DuckDB ``GROUP BY gaming_day``. On very large files this may still scan many row groups
    (column pruning applies; cost is data-dependent).

    Args:
        parquet_path: File containing a ``gaming_day`` column (``DATE`` or castable).

    Returns:
        Unique calendar days, sorted ascending.

    Raises:
        FileNotFoundError: If ``parquet_path`` is not a file.
        ValueError: If DuckDB fails, ``gaming_day`` is missing, or the file has no rows.
    """
    resolved = parquet_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"t_bet parquet not found: {resolved}")
    try:
        import duckdb
    except ImportError as exc:
        raise ValueError("duckdb is required to infer gaming_day range from Parquet") from exc
    con = duckdb.connect(database=":memory:")
    try:
        rows = con.execute(
            """
            SELECT CAST(gaming_day AS VARCHAR) AS d
            FROM read_parquet(?)
            GROUP BY 1
            ORDER BY 1
            """,
            [str(resolved)],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        raise ValueError(f"No rows or no gaming_day values in {resolved}")
    out = [str(r[0]).strip() for r in rows if r[0] is not None and str(r[0]).strip()]
    if not out:
        raise ValueError(f"No non-null gaming_day values in {resolved}")
    for d in out:
        _parse_iso_date(d, param="gaming_day")
    return out


def distinct_gaming_days_from_l0_t_bet_layout(data_root: Path) -> list[str]:
    """Return sorted ``gaming_day`` partition labels that have L0 ``t_bet`` Parquet parts.

    Looks under ``<data_root>/l0_layered/*/t_bet/gaming_day=<YYYY-MM-DD>/part-*.parquet``.

    Args:
        data_root: Directory containing ``l0_layered`` (typically repo ``data``).

    Returns:
        Sorted unique ``YYYY-MM-DD`` strings.

    Raises:
        ValueError: If ``l0_layered`` is missing, empty, or no partition contains a ``part-*.parquet`` file.
    """
    root = (data_root / "l0_layered").resolve()
    if not root.is_dir():
        raise ValueError(f"l0_layered not found or not a directory: {root}")
    seen: set[str] = set()
    for part_dir in root.glob("*/t_bet/gaming_day=*"):
        if not part_dir.is_dir():
            continue
        name = part_dir.name
        if not name.startswith("gaming_day="):
            continue
        day = name.split("=", 1)[1].strip()
        if not day:
            continue
        if not any(p.is_file() for p in part_dir.glob("part-*.parquet")):
            continue
        _parse_iso_date(day, param="gaming_day")
        seen.add(day)
    if not seen:
        raise ValueError(f"No L0 t_bet partitions with part-*.parquet under {root}")
    return sorted(seen)
