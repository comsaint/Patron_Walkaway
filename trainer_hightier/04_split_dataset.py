"""Step 4: arrange training parquet and split into train/val/test by ``gaming_day``.

No standalone CLI — invoked from :mod:`trainer_hightier.trainer`. Only deterministic
column projection, numeric casts, and time-ordered day splits; no fit-time transforms.
Rows with ``walkaway_censored = TRUE`` (label not yet determined) are dropped and the
indicator column is omitted from split outputs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig, Step4SplitConfig
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

_PACKAGE_ROOT = Path(__file__).resolve().parent
_DEFAULT_SPLITS_PARENT = _PACKAGE_ROOT / "artifacts" / "training_data" / "splits"

REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"walkaway_label", "walkaway_censored", "canonical_id", "gaming_day"},
)

OPTIONAL_DROP_COLS: Final[frozenset[str]] = frozenset(
    {
        "__etl_insert_Dtm",
        "ingestion_episode_id",
        "__ts_ms",
        "bet_payout_type",
    },
)

_NUMERIC_TRY_DOUBLE_COLS: Final[frozenset[str]] = frozenset(
    {
        "wager",
        "casino_win",
        "payout_odds",
        "payout_ha",
        "base_ha",
        "commission",
        "max_wager",
        "std_dev",
        "theo_win",
        "theo_win_cash",
        "true_odds",
        "adjusted_theo_win",
        "position_idx",
    },
)


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _duckdb_quote_ident(name: str) -> str:
    """Quote a SQL identifier for DuckDB."""

    return '"' + str(name).replace('"', '""') + '"'


def default_splits_output_dir(*, repo_root: Path | None = None) -> Path:
    """Return default directory for Step 4 split Parquets + ``split_report.json``."""

    base = Path(__file__).resolve().parents[1] if repo_root is None else repo_root
    return (base / "trainer_hightier" / "artifacts" / "training_data" / "splits").resolve()


@dataclass(frozen=True)
class Step4Result:
    """Paths and row counts produced by :func:`arrange_and_split_training_data`."""

    splits_dir: Path
    train_parquet: Path
    val_parquet: Path
    test_parquet: Path
    split_report_json: Path
    report: dict[str, Any]


def _read_parquet_column_names(path: Path) -> list[str]:
    """Return column names from Parquet footer (no full table load)."""

    pf = pq.ParquetFile(Path(path).resolve())
    return list(pf.schema_arrow.names)


def _validate_step4_prereqs(features_parquet: Path, cols: list[str]) -> None:
    """Ensure input exists and carries Step 4 split keys."""

    p = Path(features_parquet).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"features parquet not found: {p}")
    missing = sorted(REQUIRED_COLUMNS.difference(cols))
    if missing:
        raise ValueError(
            f"Step 4 schema gate failed: missing columns {missing}; "
            f"expected at least {sorted(REQUIRED_COLUMNS)}. Got {cols!r}."
        )


def _validate_fractions(train_f: float, val_f: float) -> None:
    """Reject invalid day-fraction configuration."""

    tf, vf = float(train_f), float(val_f)
    if tf <= 0 or vf <= 0 or tf + vf >= 1.0:
        raise ValueError(
            f"train_day_fraction and val_day_fraction must be positive and sum to < 1; "
            f"got train={tf}, val={vf}."
        )


def _projection_sql_cols(cols: list[str]) -> str:
    """Build ``SELECT`` list: drop noise columns; cast known numeric strings to DOUBLE."""

    parts: list[str] = []
    for c in cols:
        if c in OPTIONAL_DROP_COLS:
            continue
        q = _duckdb_quote_ident(c)
        if c == "walkaway_censored":
            parts.append(f"COALESCE(CAST({q} AS BOOLEAN), FALSE) AS {q}")
        elif c in _NUMERIC_TRY_DOUBLE_COLS:
            parts.append(f"TRY_CAST({q} AS DOUBLE) AS {q}")
        else:
            parts.append(f"{q} AS {q}")
    return ",\n    ".join(parts)


def _create_pre_view(con: duckdb.DuckDBPyConnection, *, proj: str, src_esc: str) -> None:
    """Materialize ``_step4_pre`` from projected ``read_parquet`` (includes ``walkaway_censored``)."""

    con.execute(
        f"""
CREATE OR REPLACE TEMP VIEW _step4_pre AS
SELECT
    {proj}
FROM read_parquet('{src_esc}')
""".strip()
    )


def _count_censored_rows(con: duckdb.DuckDBPyConnection) -> int:
    """Return how many rows will be dropped due to ``walkaway_censored``."""

    r = con.execute(
        """
        SELECT COUNT(*) FROM _step4_pre
        WHERE walkaway_censored = TRUE
        """
    ).fetchone()
    return int(r[0]) if r else 0


def _create_filtered_src_view(
    con: duckdb.DuckDBPyConnection,
    *,
    slow_patron_esc: str | None = None,
) -> int:
    """Build ``_step4_src``: uncensored rows; optional slow-monthly ``canonical_id`` coverage filter.

    Returns:
        Row count excluded for missing slow monthly coverage (0 when filter disabled).
    """
    wc = _duckdb_quote_ident("walkaway_censored")
    slow_clause = ""
    if slow_patron_esc is not None:
        slow_clause = f"""
  AND TRIM(CAST(canonical_id AS VARCHAR)) IN (
    SELECT TRIM(CAST(canonical_id AS VARCHAR))
    FROM read_parquet('{slow_patron_esc}')
    WHERE canonical_id IS NOT NULL
      AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
  )"""
    con.execute(
        f"""
CREATE OR REPLACE TEMP VIEW _step4_src AS
SELECT * EXCLUDE ({wc})
FROM _step4_pre
WHERE {wc} = FALSE{slow_clause}
""".strip()
    )
    if slow_patron_esc is None:
        return 0
    excluded = con.execute(
        f"""
        SELECT COUNT(*) FROM _step4_pre
        WHERE {wc} = FALSE
          AND TRIM(CAST(canonical_id AS VARCHAR)) NOT IN (
            SELECT TRIM(CAST(canonical_id AS VARCHAR))
            FROM read_parquet('{slow_patron_esc}')
            WHERE canonical_id IS NOT NULL
              AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
          )
        """
    ).fetchone()
    return int(excluded[0]) if excluded else 0


def _gate_src_quality(con: duckdb.DuckDBPyConnection) -> int:
    """Return distinct gaming_day count; raise if null keys or no days."""

    n_bad = con.execute(
        """
        SELECT COUNT(*) FROM _step4_src
        WHERE TRY_CAST(gaming_day AS DATE) IS NULL
           OR TRIM(CAST(canonical_id AS VARCHAR)) = ''
        """
    ).fetchone()[0]
    if int(n_bad) > 0:
        raise ValueError(
            f"Step 4: {int(n_bad)} row(s) have NULL gaming_day or empty canonical_id.",
        )
    n_days = con.execute(
        """
        SELECT COUNT(DISTINCT TRY_CAST(gaming_day AS DATE))
        FROM _step4_src
        WHERE TRY_CAST(gaming_day AS DATE) IS NOT NULL
        """
    ).fetchone()[0]
    if int(n_days) < 1:
        raise ValueError("Step 4: no distinct gaming_day values; cannot time-split.")
    return int(n_days)


def _create_tagged_view(
    con: duckdb.DuckDBPyConnection,
    *,
    train_f: float,
    val_f: float,
) -> None:
    """Build ``_step4_tagged`` with ``split_tag`` from ordered calendar days."""

    sql = f"""
CREATE OR REPLACE TEMP VIEW _step4_tagged AS
WITH src AS (
  SELECT * FROM _step4_src
),
days AS (
  SELECT DISTINCT TRY_CAST(gaming_day AS DATE) AS gd FROM src
),
ordered AS (
  SELECT gd, ROW_NUMBER() OVER (ORDER BY gd) AS rnk FROM days
),
bounds AS (
  SELECT COUNT(*)::BIGINT AS n_days FROM days
),
tagged_days AS (
  SELECT
    o.gd,
    CASE
      WHEN o.rnk <= CAST(CEIL(b.n_days * {train_f}) AS BIGINT) THEN 'train'
      WHEN o.rnk <= CAST(CEIL(b.n_days * ({train_f} + {val_f})) AS BIGINT) THEN 'val'
      ELSE 'test'
    END AS split_tag
  FROM ordered o
  CROSS JOIN bounds b
)
SELECT s.*, t.split_tag
FROM src s
INNER JOIN tagged_days t ON TRY_CAST(s.gaming_day AS DATE) = t.gd
""".strip()
    con.execute(sql)


def _copy_split_parquets(con: duckdb.DuckDBPyConnection, out_dir: Path) -> tuple[Path, Path, Path]:
    """Write ``train``/``val``/``test`` Parquets (without ``split_tag`` column)."""

    train_p = (out_dir / "train.parquet").resolve()
    val_p = (out_dir / "val.parquet").resolve()
    test_p = (out_dir / "test.parquet").resolve()
    for p in (train_p, val_p, test_p):
        if p.is_file():
            p.unlink()
    train_esc = _path_posix(train_p).replace("'", "''")
    val_esc = _path_posix(val_p).replace("'", "''")
    test_esc = _path_posix(test_p).replace("'", "''")
    con.execute(
        f"COPY (SELECT * EXCLUDE (split_tag) FROM _step4_tagged WHERE split_tag = 'train') "
        f"TO '{train_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    )
    con.execute(
        f"COPY (SELECT * EXCLUDE (split_tag) FROM _step4_tagged WHERE split_tag = 'val') "
        f"TO '{val_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    )
    con.execute(
        f"COPY (SELECT * EXCLUDE (split_tag) FROM _step4_tagged WHERE split_tag = 'test') "
        f"TO '{test_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    )
    return train_p, val_p, test_p


def _split_aggregates(con: duckdb.DuckDBPyConnection) -> tuple[list[tuple[Any, ...]], int]:
    """Return per-split stats rows and distinct canonical_id count."""

    split_rows = con.execute(
        """
        SELECT split_tag, COUNT(*)::BIGINT,
               AVG(CAST(walkaway_label AS DOUBLE)),
               MIN(TRY_CAST(gaming_day AS DATE)),
               MAX(TRY_CAST(gaming_day AS DATE))
        FROM _step4_tagged
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    n_canon = con.execute(
        "SELECT COUNT(DISTINCT TRIM(CAST(canonical_id AS VARCHAR))) FROM _step4_src"
    ).fetchone()[0]
    return split_rows, int(n_canon)


def _build_step4_report(
    *,
    features_parquet: Path,
    out_dir: Path,
    train_f: float,
    val_f: float,
    n_days: int,
    n_canon: int,
    censored_rows_excluded: int,
    slow_coverage_rows_excluded: int,
    slow_patron_parquet: str | None,
    split_rows: list[tuple[Any, ...]],
    train_p: Path,
    val_p: Path,
    test_p: Path,
) -> dict[str, Any]:
    """Assemble the Step 4 JSON report payload."""

    rows_kept = sum(int(r[1]) for r in split_rows)
    return {
        "input_features_parquet": str(Path(features_parquet).resolve()),
        "splits_output_dir": str(out_dir),
        "train_day_fraction": train_f,
        "val_day_fraction": val_f,
        "censored_rows_excluded": int(censored_rows_excluded),
        "slow_coverage_rows_excluded": int(slow_coverage_rows_excluded),
        "slow_patron_parquet": slow_patron_parquet,
        "rows_after_censor_filter": int(rows_kept),
        "distinct_gaming_days": int(n_days),
        "distinct_canonical_ids": int(n_canon),
        "splits": [
            {
                "split": str(r[0]),
                "row_count": int(r[1]),
                "walkaway_label_rate": float(r[2]) if r[2] is not None else None,
                "min_gaming_day": str(r[3]) if r[3] is not None else None,
                "max_gaming_day": str(r[4]) if r[4] is not None else None,
            }
            for r in split_rows
        ],
        "outputs": {
            "train": str(train_p),
            "val": str(val_p),
            "test": str(test_p),
        },
    }


def _execute_step4_duckdb(
    *,
    proj: str,
    src_esc: str,
    out_dir: Path,
    train_f: float,
    val_f: float,
    duckdb_runtime: DuckDbRuntimeConfig,
    slow_patron_esc: str | None = None,
) -> tuple[int, Path, Path, Path, list[tuple[Any, ...]], int, int, int]:
    """Run DuckDB views and emit split Parquets; return stats for reporting."""

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        _create_pre_view(con, proj=proj, src_esc=src_esc)
        n_cens = _count_censored_rows(con)
        if n_cens > 0:
            logger.info("Step 4 excluding %d row(s) with walkaway_censored=TRUE", int(n_cens))
        n_slow_excl = _create_filtered_src_view(con, slow_patron_esc=slow_patron_esc)
        if n_slow_excl > 0:
            logger.info(
                "Step 4 excluding %d row(s) without slow monthly canonical_id coverage",
                int(n_slow_excl),
            )
        n_days = _gate_src_quality(con)
        _create_tagged_view(con, train_f=train_f, val_f=val_f)
        train_p, val_p, test_p = _copy_split_parquets(con, out_dir)
        split_rows, n_canon = _split_aggregates(con)
        return n_days, train_p, val_p, test_p, split_rows, n_canon, int(n_cens), int(n_slow_excl)
    finally:
        con.close()


def arrange_and_split_training_data(
    *,
    features_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    step4: Step4SplitConfig | None = None,
    splits_output_dir: Path | None = None,
) -> Step4Result:
    """Project/cast features, tag rows by ``gaming_day`` splits, write Parquets + JSON report."""

    cfg = step4 or Step4SplitConfig()
    _validate_fractions(cfg.train_day_fraction, cfg.val_day_fraction)
    cols = _read_parquet_column_names(Path(features_parquet))
    _validate_step4_prereqs(features_parquet, cols)
    out_dir = Path(splits_output_dir or cfg.splits_output_dir or _DEFAULT_SPLITS_PARENT).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    src_esc = _path_posix(Path(features_parquet)).replace("'", "''")
    train_f = float(cfg.train_day_fraction)
    val_f = float(cfg.val_day_fraction)
    proj = _projection_sql_cols(cols)
    slow_p = cfg.slow_patron_parquet
    slow_esc: str | None = None
    if slow_p is not None:
        slow_path = Path(slow_p).resolve()
        if not slow_path.is_file():
            raise FileNotFoundError(f"Step 4 slow_patron_parquet not found: {slow_path}")
        slow_esc = _path_posix(slow_path).replace("'", "''")
    n_days, train_p, val_p, test_p, split_rows, n_canon, n_cens_excl, n_slow_excl = _execute_step4_duckdb(
        proj=proj,
        src_esc=src_esc,
        out_dir=out_dir,
        train_f=train_f,
        val_f=val_f,
        duckdb_runtime=duckdb_runtime,
        slow_patron_esc=slow_esc,
    )

    report = _build_step4_report(
        features_parquet=features_parquet,
        out_dir=out_dir,
        train_f=train_f,
        val_f=val_f,
        n_days=n_days,
        n_canon=n_canon,
        censored_rows_excluded=n_cens_excl,
        slow_coverage_rows_excluded=n_slow_excl,
        slow_patron_parquet=str(Path(slow_p).resolve()) if slow_p is not None else None,
        split_rows=split_rows,
        train_p=train_p,
        val_p=val_p,
        test_p=test_p,
    )
    report_path = out_dir / "split_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("Step 4 wrote splits under %s (report %s)", out_dir, report_path)
    return Step4Result(
        splits_dir=out_dir,
        train_parquet=train_p,
        val_parquet=val_p,
        test_parquet=test_p,
        split_report_json=report_path,
        report=report,
    )
