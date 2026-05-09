"""Issue #8: high-roller segmentation helpers (theo quantile, split export, routed metrics).

Uses DuckDB for quantiles and filtered Parquet materialisation to stay memory-aware
on laptop-scale runs (no full-table duplicate in pandas unless caller loads it).
"""

from __future__ import annotations

import logging
import math
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple

logger = logging.getLogger("trainer")

_THEO_COL_SAFE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_theo_feature_name(theo_col: str) -> str:
    """Return *theo_col* if safe for SQL identifiers; else raise ValueError."""
    if not _THEO_COL_SAFE.match(str(theo_col or "").strip()):
        raise ValueError(
            "HIGH_ROLLER_THEO_FEATURE must match ^[a-zA-Z_][a-zA-Z0-9_]*$, "
            f"got {theo_col!r}"
        )
    return str(theo_col).strip()


def _duckdb_quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _duckdb_quote_path(p: Path) -> str:
    return str(p).replace("'", "''")


def compute_high_roller_cutoff_from_train_parquet(
    train_parquet_path: Path,
    theo_col: str,
    quantile: float,
) -> Tuple[float, Dict[str, Any]]:
    """Return (cutoff, audit) where high segment is rows with COALESCE(theo,0) >= cutoff.

    Cutoff is ``quantile_cont(q)`` over rated train rows only (``is_rated`` true).

    The audit dict includes rated-only segment row counts at this cutoff (same
    definition as downstream ``count_rated_rows_parquet`` on segment Parquets)
    so callers can detect degenerate quantiles **before** materialising splits.
    """
    import duckdb

    tc = validate_theo_feature_name(theo_col)
    q = float(quantile)
    if not (0.0 < q < 1.0) or not math.isfinite(q):
        raise ValueError(f"quantile must be finite in (0,1), got {quantile!r}")

    tp = _duckdb_quote_path(Path(train_parquet_path))
    col = _duckdb_quote_ident(tc)
    con = duckdb.connect(":memory:")
    try:
        # One scan over rated rows: threshold + counts (detect constant / heavy
        # left-tail skew where q sits on the minimum and the low segment is empty).
        row = con.execute(
            f"""
            WITH rated AS (
              SELECT COALESCE(t.{col}, 0.0) AS x
              FROM read_parquet('{tp}') AS t
              WHERE COALESCE(t.is_rated, false) = true
            ),
            thr_val AS (
              SELECT quantile_cont(x, {q:.12f}) AS thr FROM rated
            ),
            counts AS (
              SELECT
                COUNT(*) AS n_rated,
                COUNT(*) FILTER (WHERE x < (SELECT thr FROM thr_val)) AS n_low,
                COUNT(*) FILTER (WHERE x >= (SELECT thr FROM thr_val)) AS n_high
              FROM rated
            )
            SELECT thr_val.thr, counts.n_rated, counts.n_low, counts.n_high
            FROM thr_val CROSS JOIN counts
            """
        ).fetchone()
        if not row:
            raise RuntimeError(
                "Issue #8 cutoff query returned no row (unexpected); "
                f"train_parquet={train_parquet_path!s}"
            )
        thr_raw, n_rated, n_low, n_high = row[0], int(row[1] or 0), int(row[2] or 0), int(
            row[3] or 0
        )
        if n_rated <= 0:
            raise RuntimeError(
                "Issue #8 segmentation cannot proceed: no rated train rows "
                f"(is_rated=true) in {train_parquet_path!s}."
            )
        if thr_raw is None or not math.isfinite(float(thr_raw)):
            raise RuntimeError(
                "Issue #8 segmentation cannot proceed: non-finite theo quantile "
                f"(thr={thr_raw!r}) over {n_rated} rated train rows in "
                f"{train_parquet_path!s}."
            )
        thr = float(thr_raw)
    finally:
        con.close()

    meta = {
        "high_roller_cutoff_theo": thr,
        "high_roller_theo_feature": tc,
        "high_roller_quantile": q,
        "high_roller_train_parquet": str(train_parquet_path),
        "high_roller_rated_train_row_count": n_rated,
        "high_roller_segment_train_rated_rows_low": n_low,
        "high_roller_segment_train_rated_rows_high": n_high,
    }
    return thr, meta


def materialize_segment_parquet_splits(
    train_path: Path,
    valid_path: Path,
    test_path: Optional[Path],
    theo_col: str,
    cutoff: float,
    segment: Literal["high", "low"],
    out_dir: Path,
) -> Tuple[Path, Path, Optional[Path]]:
    """Write filtered train/valid[/test] Parquets for one segment; return paths."""
    import duckdb

    tc = validate_theo_feature_name(theo_col)
    col = _duckdb_quote_ident(tc)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    seg_cond = f"COALESCE(t.{col}, 0.0) >= {float(cutoff):.17g}"
    if segment == "low":
        seg_cond = f"COALESCE(t.{col}, 0.0) < {float(cutoff):.17g}"

    con = duckdb.connect(":memory:")
    try:
        tp = _duckdb_quote_path(Path(train_path))
        vp = _duckdb_quote_path(Path(valid_path))
        train_out = out_dir / "train_segment.parquet"
        valid_out = out_dir / "valid_segment.parquet"
        con.execute(
            f"""
            COPY (
              SELECT t.* FROM read_parquet('{tp}') AS t
              WHERE {seg_cond}
            ) TO '{_duckdb_quote_path(train_out)}' (FORMAT PARQUET)
            """
        )
        con.execute(
            f"""
            COPY (
              SELECT t.* FROM read_parquet('{vp}') AS t
              WHERE {seg_cond}
            ) TO '{_duckdb_quote_path(valid_out)}' (FORMAT PARQUET)
            """
        )
        test_out: Optional[Path] = None
        if test_path is not None and Path(test_path).exists():
            tsp = _duckdb_quote_path(Path(test_path))
            test_out = out_dir / "test_segment.parquet"
            con.execute(
                f"""
                COPY (
                  SELECT t.* FROM read_parquet('{tsp}') AS t
                  WHERE {seg_cond}
                ) TO '{_duckdb_quote_path(test_out)}' (FORMAT PARQUET)
                """
            )
    finally:
        con.close()

    if not train_out.is_file() or not valid_out.is_file():
        raise RuntimeError(
            f"Segment materialisation failed for {segment!r} under {out_dir}"
        )
    return train_out, valid_out, test_out


def parquet_has_column(path: Path, col: str) -> bool:
    """Return True if *col* exists in Parquet schema."""
    import pyarrow.parquet as pq

    try:
        return str(col) in pq.read_schema(path).names
    except Exception:
        return False


def validate_high_roller_theo_nonempty_on_rated_train(train_parquet_path: Path, theo_col: str) -> None:
    """Fail-fast when no rated train row carries a non-null theo proxy.

    Issue #8 uses ``HIGH_ROLLER_THEO_FEATURE`` as segmentation proxy; silent NULL-only
    columns degenerate segmentation downstream — abort early with an actionable error.
    """
    tc = validate_theo_feature_name(theo_col)
    import duckdb

    tp = _duckdb_quote_path(Path(train_parquet_path))
    col = _duckdb_quote_ident(tc)
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            f"""
            SELECT COUNT(*) FILTER (
              WHERE COALESCE(is_rated, false) = true AND {col} IS NOT NULL
            )
            FROM read_parquet('{tp}') AS t
            """
        ).fetchone()
        nn = int(row[0] or 0) if row else 0
        if nn <= 0:
            raise RuntimeError(
                f"Issue #8 segmentation requires rated train rows with non-null {tc!r}. "
                f"(Ensure ``player_run_asset`` materializes this column when HIGH_ROLLER_SEGMENT_ENABLE "
                f"is True.) Got nn_non_null_rated={nn} in {train_parquet_path}."
            )
    finally:
        con.close()


def count_rated_rows_parquet(path: Path) -> int:
    """Return COUNT(*) for rated rows in a Parquet file (DuckDB)."""
    import duckdb

    pp = _duckdb_quote_path(Path(path))
    con = duckdb.connect(":memory:")
    try:
        n = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{pp}') WHERE COALESCE(is_rated, false) = true"
        ).fetchone()[0]
        return int(n)
    finally:
        con.close()


def routed_test_metrics_payload(
    *,
    test_df,
    theo_col: str,
    cutoff: float,
    feature_cols: list[str],
    model_high,
    model_low,
    thr_high: float,
    thr_low: float,
) -> Dict[str, Any]:
    """Optional routed test metrics: pick model by theo vs cutoff on rated rows."""
    import numpy as np
    import pandas as pd

    if test_df is None or getattr(test_df, "empty", True):
        return {"high_roller_routed_test_skipped": "no_test_df"}
    if theo_col not in test_df.columns:
        return {"high_roller_routed_test_skipped": f"missing_column:{theo_col}"}

    rated = test_df[test_df["is_rated"]].copy()
    if rated.empty:
        return {"high_roller_routed_test_skipped": "empty_rated_test"}

    cols = [c for c in feature_cols if c in rated.columns]
    if not cols:
        return {"high_roller_routed_test_skipped": "no_feature_overlap"}

    theo = pd.to_numeric(rated[theo_col], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    is_high = theo >= float(cutoff)
    y = rated["label"].to_numpy(dtype=float)

    # Import lazily to avoid heavy import at module load.
    from trainer.core.config import TRAIN_METRICS_PREDICT_BATCH_ROWS
    from trainer.training.trainer import _batched_model_positive_class_scores, _dataframe_for_lgb_predict

    _batch = int(max(1, TRAIN_METRICS_PREDICT_BATCH_ROWS))

    scores = np.zeros(len(rated), dtype=float)
    thrs = np.zeros(len(rated), dtype=float)
    if is_high.any():
        Xh = _dataframe_for_lgb_predict(model_high, rated.loc[is_high], cols)
        scores[is_high] = _batched_model_positive_class_scores(
            model_high, Xh, _batch
        )
        thrs[is_high] = float(thr_high)
    if (~is_high).any():
        Xl = _dataframe_for_lgb_predict(model_low, rated.loc[~is_high], cols)
        scores[~is_high] = _batched_model_positive_class_scores(
            model_low, Xl, _batch
        )
        thrs[~is_high] = float(thr_low)

    pred = (scores >= thrs).astype(int)
    tp = int(np.sum((pred == 1) & (y == 1.0)))
    fp = int(np.sum((pred == 1) & (y == 0.0)))
    fn = int(np.sum((pred == 0) & (y == 1.0)))
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {
        "overall_routed_test_precision": float(prec),
        "overall_routed_test_recall": float(rec),
        "overall_routed_test_tp": tp,
        "overall_routed_test_fp": fp,
        "overall_routed_test_fn": fn,
        "overall_routed_test_rows": int(len(rated)),
        "overall_routed_test_high_rows": int(np.sum(is_high)),
        "overall_routed_test_low_rows": int(np.sum(~is_high)),
    }
