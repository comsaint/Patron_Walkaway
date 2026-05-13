"""Per-canonical patron aggregates from cleaned ``t_session`` (theo + gaming days + ADT)."""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_artifacts_dir
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas, execute_sql_with_progress

logger = logging.getLogger("trainer_hightier")

_REQUIRED_CLEAN_COLS: frozenset[str] = frozenset({"player_id", "theo_win", "gaming_day"})
_PROFILE_REQUIRED_CLEAN_COLS: frozenset[str] = frozenset(
    {
        "session_id",
        "player_id",
        "gaming_day",
        "theo_win",
        "turnover",
        "player_win",
        "cash_buyins",
        "num_bets",
    }
)
_MAPPING_COLS: frozenset[str] = frozenset({"player_id", "canonical_id"})


def default_patron_session_metrics_parquet_path() -> Path:
    """ADT report Parquet next to canonical mapping artifacts."""
    return default_canonical_mapping_artifacts_dir() / "canonical_patron_session_metrics.parquet"


def default_patron_profile_csv_path() -> Path:
    """Canonical patron profile CSV under ``trainer_hightier/artifacts/profile``."""
    return Path(__file__).resolve().parents[1] / "artifacts" / "profile" / "canonical_patron_profile.csv"


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _validate_inputs(cleaned: Path, mapping: Path) -> None:
    if not cleaned.is_file():
        raise FileNotFoundError(cleaned)
    if not mapping.is_file():
        raise FileNotFoundError(mapping)
    cnames = frozenset(pq.read_schema(cleaned).names)
    mnames = frozenset(pq.read_schema(mapping).names)
    miss_c = sorted(_REQUIRED_CLEAN_COLS - cnames)
    miss_m = sorted(_MAPPING_COLS - mnames)
    if miss_c:
        raise ValueError(f"Cleaned session Parquet missing columns for patron metrics: {miss_c}")
    if miss_m:
        raise ValueError(f"Canonical mapping Parquet missing columns: {miss_m}")


def _validate_profile_inputs(cleaned: Path, mapping: Path) -> None:
    """Validate cleaned session + mapping have columns for the patron profile CSV."""
    if not cleaned.is_file():
        raise FileNotFoundError(cleaned)
    if not mapping.is_file():
        raise FileNotFoundError(mapping)
    cnames = frozenset(pq.read_schema(cleaned).names)
    mnames = frozenset(pq.read_schema(mapping).names)
    miss_c = sorted(_PROFILE_REQUIRED_CLEAN_COLS - cnames)
    miss_m = sorted(_MAPPING_COLS - mnames)
    if miss_c:
        raise ValueError(
            "Cleaned session Parquet missing columns for patron profile CSV "
            f"(re-run session preprocess): {miss_c}"
        )
    if miss_m:
        raise ValueError(f"Canonical mapping Parquet missing columns: {miss_m}")


def _patron_profile_sql(*, cleaned_posix: str, map_posix: str) -> str:
    """One row per ``canonical_id`` with turnover / theo / session counts / ADT."""
    return f"""
WITH map AS (SELECT * FROM read_parquet('{map_posix}')),
sess AS (SELECT * FROM read_parquet('{cleaned_posix}')),
joined AS (
  SELECT
    CAST(map.canonical_id AS VARCHAR) AS canonical_id,
    COALESCE(TRY_CAST(sess.theo_win AS DOUBLE), 0.0) AS theo_win,
    COALESCE(TRY_CAST(sess.turnover AS DOUBLE), 0.0) AS turnover,
    COALESCE(TRY_CAST(sess.cash_buyins AS DOUBLE), 0.0) AS cash_buyins,
    COALESCE(TRY_CAST(sess.player_win AS DOUBLE), 0.0) AS player_win,
    COALESCE(TRY_CAST(sess.num_bets AS BIGINT), CAST(0 AS BIGINT)) AS num_bets,
    sess.gaming_day AS gaming_day
  FROM sess
  INNER JOIN map
    ON TRY_CAST(sess.player_id AS BIGINT) = TRY_CAST(map.player_id AS BIGINT)
),
agg AS (
  SELECT
    canonical_id,
    CAST(SUM(theo_win) AS DOUBLE) AS total_theo_win,
    CAST(SUM(turnover) AS DOUBLE) AS total_turnover,
    CAST(SUM(cash_buyins) AS DOUBLE) AS total_cash_buyins,
    CAST(SUM(player_win) AS DOUBLE) AS total_player_win,
    CAST(COUNT(DISTINCT gaming_day) AS BIGINT) AS unique_gaming_days,
    CAST(SUM(num_bets) AS BIGINT) AS total_num_bets,
    CAST(COUNT(*) AS BIGINT) AS session_count,
    CAST(MIN(gaming_day) AS VARCHAR) AS first_gaming_day,
    CAST(MAX(gaming_day) AS VARCHAR) AS last_gaming_day,
    CASE
      WHEN COUNT(DISTINCT gaming_day) > 0 THEN
        CAST(SUM(theo_win) AS DOUBLE) / CAST(COUNT(DISTINCT gaming_day) AS DOUBLE)
      ELSE NULL
    END AS adt
  FROM joined
  GROUP BY canonical_id
)
SELECT
  canonical_id,
  total_theo_win,
  total_turnover,
  total_cash_buyins,
  total_player_win,
  unique_gaming_days,
  total_num_bets,
  session_count,
  first_gaming_day,
  last_gaming_day,
  adt
FROM agg
ORDER BY canonical_id ASC
"""


def _adt_copy_sql(*, cleaned_posix: str, map_posix: str) -> str:
    """Aggregate joined sessions → one row per ``canonical_id``, sorted by ADT descending."""
    return f"""
WITH map AS (SELECT * FROM read_parquet('{map_posix}')),
sess AS (SELECT * FROM read_parquet('{cleaned_posix}')),
joined AS (
  SELECT
    CAST(map.canonical_id AS VARCHAR) AS canonical_id,
    COALESCE(TRY_CAST(sess.theo_win AS DOUBLE), 0.0) AS theo_win,
    sess.gaming_day AS gaming_day
  FROM sess
  INNER JOIN map
    ON TRY_CAST(sess.player_id AS BIGINT) = TRY_CAST(map.player_id AS BIGINT)
),
agg AS (
  SELECT
    canonical_id,
    CAST(SUM(theo_win) AS DOUBLE) AS total_theo_win,
    CAST(COUNT(DISTINCT gaming_day) AS BIGINT) AS gaming_days,
    CASE
      WHEN COUNT(DISTINCT gaming_day) > 0 THEN
        CAST(SUM(theo_win) AS DOUBLE) / CAST(COUNT(DISTINCT gaming_day) AS DOUBLE)
      ELSE NULL
    END AS adt
  FROM joined
  GROUP BY canonical_id
)
SELECT canonical_id, total_theo_win, gaming_days, adt
FROM agg
ORDER BY adt DESC NULLS LAST, canonical_id ASC
"""


def compile_canonical_patron_session_metrics(
    cleaned_session_parquet: Path,
    canonical_mapping_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    output_parquet: Path | None = None,
    duckdb_join_timeout_s: float = 3600.0,
) -> Path:
    """Write Parquet with ``canonical_id``, ``total_theo_win``, ``gaming_days``, ``adt`` (ADT descending).

    Sessions whose ``player_id`` is absent from the mapping are dropped (inner join).
    Patrons with zero distinct ``gaming_day`` values get ``adt`` NULL and sort last.
    """
    src_c = Path(cleaned_session_parquet).resolve()
    src_m = Path(canonical_mapping_parquet).resolve()
    _validate_inputs(src_c, src_m)

    out = Path(output_parquet) if output_parquet is not None else default_patron_session_metrics_parquet_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        out.unlink()

    c_px = _path_posix(src_c).replace("'", "''")
    m_px = _path_posix(src_m).replace("'", "''")
    out_px = _path_posix(out).replace("'", "''")

    inner = _adt_copy_sql(cleaned_posix=c_px, map_posix=m_px)
    sql = f"COPY ({inner}) TO '{out_px}' (FORMAT PARQUET, COMPRESSION SNAPPY)"

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        execute_sql_with_progress(
            con,
            sql,
            desc="[Step 4] DuckDB patron ADT report",
            join_timeout_s=float(duckdb_join_timeout_s),
        )
    finally:
        con.close()

    meta = pq.ParquetFile(out).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    logger.info(
        "[Step 4] patron session metrics (canonical ADT): rows=%d written %s",
        nrows,
        out.resolve(),
    )
    return out.resolve()


def compile_canonical_patron_profile_csv(
    cleaned_session_parquet: Path,
    canonical_mapping_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    output_csv: Path | None = None,
    duckdb_join_timeout_s: float = 3600.0,
) -> Path:
    """Join cleaned sessions to mapping; write one CSV row per ``canonical_id``.

    ADT (average daily theo) = ``total_theo_win / unique_gaming_days`` when
    ``unique_gaming_days > 0``; otherwise NULL. ``COUNT(DISTINCT gaming_day)``
    ignores NULL ``gaming_day`` values (DuckDB default).

    Parameters
    ----------
    cleaned_session_parquet
        Output of :func:`trainer_hightier.02_preprocess.preprocess_sessions_from_parquet_streaming`
        (must include ``player_win``, ``cash_buyins``, ``num_bets`` from current preprocess).
    canonical_mapping_parquet
        ``player_id`` → ``canonical_id`` Parquet from Step 3.
    output_csv
        Default: :func:`default_patron_profile_csv_path`.
    """
    src_c = Path(cleaned_session_parquet).resolve()
    src_m = Path(canonical_mapping_parquet).resolve()
    _validate_profile_inputs(src_c, src_m)

    out = Path(output_csv) if output_csv is not None else default_patron_profile_csv_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        out.unlink()

    c_px = _path_posix(src_c).replace("'", "''")
    m_px = _path_posix(src_m).replace("'", "''")
    out_px = _path_posix(out).replace("'", "''")

    inner = _patron_profile_sql(cleaned_posix=c_px, map_posix=m_px)
    sql = f"COPY ({inner}) TO '{out_px}' (FORMAT CSV, HEADER true, DELIMITER ',')"

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        execute_sql_with_progress(
            con,
            sql,
            desc="[Step 4b] DuckDB patron profile CSV",
            join_timeout_s=float(duckdb_join_timeout_s),
        )
    finally:
        con.close()

    logger.info(
        "[Step 4b] patron profile CSV written %s",
        out.resolve(),
    )
    return out.resolve()


def default_adt_allowed_players_parquet_path(quantile: float) -> Path:
    """Return ``trainer_hightier/artifacts/mapping/adt_allowed_players_q{quantile}.parquet`` slug."""
    qslug = str(float(quantile)).replace(".", "p").replace("-", "neg")
    return default_canonical_mapping_artifacts_dir() / f"adt_allowed_players_q{qslug}.parquet"


def _validate_adt_allowlist_inputs(profile_csv: Path, mapping_parquet: Path) -> None:
    """Ensure CSV + mapping exist and mapping has ``player_id`` / ``canonical_id``."""
    src_p = Path(profile_csv).resolve()
    src_m = Path(mapping_parquet).resolve()
    if not src_p.is_file():
        raise FileNotFoundError(src_p)
    if not src_m.is_file():
        raise FileNotFoundError(src_m)
    mnames = frozenset(pq.read_schema(src_m).names)
    miss_m = sorted(_MAPPING_COLS - mnames)
    if miss_m:
        raise ValueError(f"Canonical mapping Parquet missing columns for ADT allowlist: {miss_m}")


def _adt_allowlist_copy_inner_sql(*, profile_esc: str, map_esc: str, quantile: float) -> str:
    """DuckDB SELECT: profile ADT threshold × mapping → one grouped row per ``player_id``."""
    qf = float(quantile)
    q_lit = repr(qf)
    return f"""
WITH threshold AS (
  SELECT quantile_cont(TRY_CAST(adt AS DOUBLE), {qf}) AS qv
  FROM read_csv_auto('{profile_esc}')
  WHERE TRY_CAST(adt AS DOUBLE) IS NOT NULL
),
joined AS (
  SELECT
    TRY_CAST(m.player_id AS BIGINT) AS player_id,
    TRIM(CAST(m.canonical_id AS VARCHAR)) AS canonical_id,
    TRY_CAST(p.adt AS DOUBLE) AS patron_adt,
    th.qv AS adt_threshold,
    CAST({q_lit} AS DOUBLE) AS adt_quantile
  FROM read_parquet('{map_esc}') AS m
  INNER JOIN read_csv_auto('{profile_esc}') AS p
    ON TRIM(CAST(m.canonical_id AS VARCHAR)) = TRIM(CAST(p.canonical_id AS VARCHAR))
  CROSS JOIN threshold AS th
  WHERE TRY_CAST(m.player_id AS BIGINT) IS NOT NULL
    AND TRY_CAST(p.adt AS DOUBLE) IS NOT NULL
    AND th.qv IS NOT NULL
    AND TRY_CAST(p.adt AS DOUBLE) >= th.qv
),
agg AS (
  SELECT
    player_id,
    max_by(canonical_id, patron_adt) AS canonical_id,
    max(patron_adt) AS adt,
    max(adt_threshold) AS adt_threshold,
    max(adt_quantile) AS adt_quantile
  FROM joined
  GROUP BY player_id
)
SELECT player_id, canonical_id, adt, adt_threshold, adt_quantile
FROM agg
ORDER BY player_id ASC
""".strip()


def _copy_adt_allowlist_select_to_parquet(
    *,
    inner_select_sql: str,
    output_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    duckdb_join_timeout_s: float,
) -> None:
    """Run ``COPY (<inner_select_sql>) TO`` Parquet via DuckDB."""
    out_px = _path_posix(output_parquet).replace("'", "''")
    sql = f"COPY ({inner_select_sql}) TO '{out_px}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        execute_sql_with_progress(
            con,
            sql,
            desc="[Step 4c] DuckDB ADT allowlist Parquet",
            join_timeout_s=float(duckdb_join_timeout_s),
        )
    finally:
        con.close()


def materialize_adt_allowed_players_parquet(
    patron_profile_csv: Path,
    canonical_mapping_parquet: Path,
    *,
    quantile: float,
    duckdb_runtime: DuckDbRuntimeConfig,
    output_parquet: Path | None = None,
    duckdb_join_timeout_s: float = 3600.0,
) -> Path:
    """Join profile ADT quantile threshold → mapping; write one Parquet row per allowed ``player_id``.

    Columns: ``player_id``, ``canonical_id``, ``adt``, ``adt_threshold``, ``adt_quantile``.
    ``quantile_cont(adt, quantile)`` uses non-null patron-profile ``adt`` rows only; patrons at or above
    the threshold keep all mapped ``player_id`` values (deduped per ``player_id``).
    """
    src_p = Path(patron_profile_csv).resolve()
    src_m = Path(canonical_mapping_parquet).resolve()
    _validate_adt_allowlist_inputs(src_p, src_m)
    qf = float(quantile)
    if not (0.0 < qf < 1.0):
        raise ValueError(f"quantile must be strictly between 0 and 1, got {qf!r}")

    out = Path(output_parquet) if output_parquet is not None else default_adt_allowed_players_parquet_path(qf)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        out.unlink()

    inner = _adt_allowlist_copy_inner_sql(
        profile_esc=_path_posix(src_p).replace("'", "''"),
        map_esc=_path_posix(src_m).replace("'", "''"),
        quantile=qf,
    )
    _copy_adt_allowlist_select_to_parquet(
        inner_select_sql=inner,
        output_parquet=out,
        duckdb_runtime=duckdb_runtime,
        duckdb_join_timeout_s=duckdb_join_timeout_s,
    )

    meta = pq.ParquetFile(out).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    logger.info(
        "[Step 4c] ADT allowed player_id Parquet: rows=%d written %s",
        nrows,
        out.resolve(),
    )
    return out.resolve()
