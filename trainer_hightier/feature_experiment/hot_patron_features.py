"""PIT-safe hot-patron robustness features (experiment-only join layer)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Final

import duckdb
import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]

HOT_PATRON_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "fe__hot__wager_today_over_peer_p95__adt_decile",
    "fe__hot__avg_wager_today_over_peer_p95__adt_decile",
    "fe__hot__wager_w15m_over_peer_p95__adt_decile",
    "fe__hot__wager_per_hr_over_peer_p95",
    "fe__hot__games_today_over_peer_p95",
    "fe__hot__mid_term_history_sparse_flag",
    "fe__hot__wager_today_over_own_p95__w180d",
    "fe__hot__peer_wager_z__adt_decile",
)

PEER_LOOKUP_HOT_PATRON_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "fe__hot__wager_today_over_peer_p95__adt_decile",
    "fe__hot__avg_wager_today_over_peer_p95__adt_decile",
    "fe__hot__wager_w15m_over_peer_p95__adt_decile",
    "fe__hot__wager_per_hr_over_peer_p95",
    "fe__hot__games_today_over_peer_p95",
    "fe__hot__peer_wager_z__adt_decile",
)

SERVING_SAFE_HOT_PATRON_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "fe__hot__mid_term_history_sparse_flag",
    "fe__hot__wager_today_over_own_p95__w180d",
)

_HOT_FEATURE_MAX_NULL_RATE: Final[float] = 0.98

_PEER_BASE_COLS: Final[dict[str, str]] = {
    "fe__hot__wager_today_over_peer_p95__adt_decile": "fe__canonical__wager_sum__today",
    "fe__hot__avg_wager_today_over_peer_p95__adt_decile": "fe__canonical__avg_wager__today",
    "fe__hot__wager_w15m_over_peer_p95__adt_decile": "fe__wager_sum__w15m",
    "fe__hot__peer_wager_z__adt_decile": "fe__canonical__wager_sum__today",
}


def _require_columns(df: pd.DataFrame, cols: tuple[str, ...]) -> None:
    """Validate required input columns exist."""

    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"hot_patron_features missing columns {missing}; got {list(df.columns)}")


def hot_feature_null_rates(
    df: pd.DataFrame,
    *,
    feature_columns: tuple[str, ...] = HOT_PATRON_FEATURE_COLUMNS,
) -> dict[str, float]:
    """Return per-feature null rates for hot-patron columns."""

    _require_columns(df, feature_columns)
    n_rows = int(len(df))
    if n_rows == 0:
        raise ValueError("hot_feature_null_rates requires at least one row")
    return {col: float(df[col].isna().mean()) for col in feature_columns}


def validate_hot_feature_coverage(
    df: pd.DataFrame,
    *,
    split_name: str,
    feature_columns: tuple[str, ...] = HOT_PATRON_FEATURE_COLUMNS,
    max_null_rate: float = _HOT_FEATURE_MAX_NULL_RATE,
) -> dict[str, float]:
    """Fail when a hot feature is effectively missing in one split."""

    rates = hot_feature_null_rates(df, feature_columns=feature_columns)
    bad = {col: rate for col, rate in rates.items() if rate >= float(max_null_rate)}
    if bad:
        detail = ", ".join(f"{col}={rate:.3f}" for col, rate in sorted(bad.items()))
        raise ValueError(
            f"hot feature coverage gate failed for split={split_name!r}; "
            f"max_null_rate={max_null_rate:.3f}; {detail}",
        )
    return rates


def _validate_hot_feature_parquet_coverage(
    parquet_path: Path,
    *,
    feature_columns: tuple[str, ...] = HOT_PATRON_FEATURE_COLUMNS,
    max_null_rate: float = _HOT_FEATURE_MAX_NULL_RATE,
) -> dict[str, float]:
    """Fail when materialized hot features are effectively missing in a parquet artifact."""

    p = str(Path(parquet_path).resolve()).replace("\\", "/")
    exprs = [
        f"AVG(CASE WHEN {col} IS NULL THEN 1.0 ELSE 0.0 END) AS {col}"
        for col in feature_columns
    ]
    con = duckdb.connect(database=":memory:")
    try:
        rates_row = con.execute(f"SELECT {', '.join(exprs)} FROM read_parquet('{p}')").fetchone()
    finally:
        con.close()
    if rates_row is None:
        raise ValueError(f"hot feature coverage gate failed; no rows in {parquet_path}")
    rates = {col: float(rates_row[idx]) for idx, col in enumerate(feature_columns)}
    bad = {col: rate for col, rate in rates.items() if rate >= float(max_null_rate)}
    if bad:
        detail = ", ".join(f"{col}={rate:.3f}" for col, rate in sorted(bad.items()))
        raise ValueError(
            f"hot feature coverage gate failed for parquet={parquet_path}; "
            f"max_null_rate={max_null_rate:.3f}; {detail}",
        )
    return rates


def _adt_decile(series: pd.Series) -> pd.Series:
    """Assign ADT decile buckets (0-9) from patron ADT snapshot."""

    adt = pd.to_numeric(series, errors="coerce")
    return pd.qcut(adt.rank(method="first"), 10, labels=False, duplicates="drop")


def _peer_p95(df: pd.DataFrame, value_col: str, group_cols: list[str]) -> pd.Series:
    """Same-day / ADT-decile peer 95th percentile for ``value_col``."""

    vals = pd.to_numeric(df[value_col], errors="coerce")
    tmp = df[group_cols].copy()
    tmp["_v"] = vals
    return tmp.groupby(group_cols, observed=True)["_v"].transform(lambda s: s.quantile(0.95))


def _peer_median_mad_z(df: pd.DataFrame, value_col: str, group_cols: list[str]) -> pd.Series:
    """Robust z = (x - median) / (1.4826 * MAD) within peer group."""

    vals = pd.to_numeric(df[value_col], errors="coerce")
    tmp = df[group_cols].copy()
    tmp["_v"] = vals

    def _z(s: pd.Series) -> pd.Series:
        med = s.median()
        mad = (s - med).abs().median()
        denom = 1.4826 * mad if mad > 1e-12 else np.nan
        if not np.isfinite(denom) or denom <= 0:
            return pd.Series(np.nan, index=s.index)
        return (s - med) / denom

    return tmp.groupby(group_cols, observed=True)["_v"].transform(_z)


def _mad_series(s: pd.Series) -> float:
    """Median absolute deviation for one peer group."""

    med = float(s.median())
    return float((s - med).abs().median())


def _peer_lookup_from_train_parquet(train_parquet: Path) -> pd.DataFrame:
    """Build train-only peer stats via DuckDB (fast on large train split)."""

    tp = str(Path(train_parquet).resolve()).replace("\\", "/")
    con = duckdb.connect(database=":memory:")
    try:
        sql = f"""
WITH base AS (
  SELECT
    gaming_day_event,
    player_id,
    game_id,
    patron__adt__w180d_m1snap,
    TRY_CAST(fe__canonical__wager_sum__today AS DOUBLE) AS wager_today,
    TRY_CAST(fe__canonical__avg_wager__today AS DOUBLE) AS avg_wager_today,
    TRY_CAST(fe__wager_sum__w15m AS DOUBLE) AS wager_w15m,
    TRY_CAST(fe__canonical__elapsed_sec_since_first_bet__today AS DOUBLE) AS elapsed_sec,
    NTILE(10) OVER (ORDER BY patron__adt__w180d_m1snap) - 1 AS _adt_decile
  FROM read_parquet('{tp}')
),
enriched AS (
  SELECT *,
    wager_today / GREATEST(elapsed_sec / 3600.0, 0.1) AS wager_hr,
    COUNT(DISTINCT game_id) OVER (PARTITION BY player_id, gaming_day_event) AS games_today
  FROM base
)
SELECT
  gaming_day_event,
  _adt_decile,
  quantile_cont(wager_today, 0.95) AS _peer_p95__fe__canonical__wager_sum__today,
  median(wager_today) AS _peer_med__fe__canonical__wager_sum__today,
  stddev_pop(wager_today) AS _peer_mad__fe__canonical__wager_sum__today,
  quantile_cont(avg_wager_today, 0.95) AS _peer_p95__fe__canonical__avg_wager__today,
  median(avg_wager_today) AS _peer_med__fe__canonical__avg_wager__today,
  stddev_pop(avg_wager_today) AS _peer_mad__fe__canonical__avg_wager__today,
  quantile_cont(wager_w15m, 0.95) AS _peer_p95__fe__wager_sum__w15m,
  median(wager_w15m) AS _peer_med__fe__wager_sum__w15m,
  stddev_pop(wager_w15m) AS _peer_mad__fe__wager_sum__w15m,
  quantile_cont(wager_hr, 0.95) AS _peer_p95__wager_hr,
  quantile_cont(games_today, 0.95) AS _peer_p95__games_today
FROM enriched
GROUP BY gaming_day_event, _adt_decile
"""
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def _peer_lookup_from_train(train_df: pd.DataFrame) -> pd.DataFrame:
    """Build train-only peer p95 / median-MAD lookup keyed by gaming day + ADT decile."""

    need = (
        "gaming_day_event",
        "player_id",
        "game_id",
        "patron__adt__w180d_m1snap",
        "fe__canonical__wager_sum__today",
        "fe__canonical__avg_wager__today",
        "fe__wager_sum__w15m",
        "fe__canonical__elapsed_sec_since_first_bet__today",
    )
    _require_columns(train_df, need)
    tr = train_df.copy()
    tr["_adt_decile"] = _adt_decile(tr["patron__adt__w180d_m1snap"])
    hrs = pd.to_numeric(tr["fe__canonical__elapsed_sec_since_first_bet__today"], errors="coerce") / 3600.0
    tr["_wager_hr"] = pd.to_numeric(tr["fe__canonical__wager_sum__today"], errors="coerce") / hrs.clip(lower=0.1)
    tr["_games_today"] = tr.groupby(["player_id", "gaming_day_event"])["game_id"].transform("nunique")
    peer_grp = ["gaming_day_event", "_adt_decile"]
    agg_map: dict[str, tuple[str, object]] = {
        "_peer_p95__wager_hr": ("_wager_hr", lambda s: s.quantile(0.95)),
        "_peer_p95__games_today": ("_games_today", lambda s: s.quantile(0.95)),
    }
    for _, base in _PEER_BASE_COLS.items():
        vals = pd.to_numeric(tr[base], errors="coerce")
        tr[f"_num__{base}"] = vals
        agg_map[f"_peer_p95__{base}"] = (f"_num__{base}", lambda s: s.quantile(0.95))
        agg_map[f"_peer_med__{base}"] = (f"_num__{base}", "median")
        agg_map[f"_peer_mad__{base}"] = (f"_num__{base}", _mad_series)
    return tr.groupby(peer_grp, observed=True).agg(**agg_map).reset_index()


def materialize_hot_patron_features(
    df: pd.DataFrame,
    *,
    peer_lookup: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Add hot-patron robustness columns to a training/enriched frame."""

    need = (
        "gaming_day_event",
        "player_id",
        "game_id",
        "patron__adt__w180d_m1snap",
        "fe__canonical__wager_sum__today",
        "fe__canonical__avg_wager__today",
        "fe__wager_sum__w15m",
        "fe__canonical__elapsed_sec_since_first_bet__today",
        "mid_term_snapshot_missing_flag",
        "patron__gaming_days_cnt__w180d_m1snap",
        "patron__theo_win_sum__w180d_m1snap",
    )
    _require_columns(df, need)
    out = df.copy()
    out["_adt_decile"] = _adt_decile(out["patron__adt__w180d_m1snap"])
    peer_grp = ["gaming_day_event", "_adt_decile"]
    if peer_lookup is not None and not peer_lookup.empty:
        lookup = peer_lookup.copy()
        out["gaming_day_event"] = out["gaming_day_event"].astype(str)
        lookup["gaming_day_event"] = lookup["gaming_day_event"].astype(str)
        lookup["_adt_decile"] = pd.to_numeric(lookup["_adt_decile"], errors="coerce")
        out = out.merge(lookup, on=peer_grp, how="left")
        for feat, base in _PEER_BASE_COLS.items():
            p95 = out[f"_peer_p95__{base}"]
            num = pd.to_numeric(out[base], errors="coerce")
            out[feat] = np.where(p95 > 1e-9, num / p95, np.nan)
        base = "fe__canonical__wager_sum__today"
        num = pd.to_numeric(out[base], errors="coerce")
        med = out[f"_peer_med__{base}"]
        mad = out[f"_peer_mad__{base}"]
        denom = mad
        out["fe__hot__peer_wager_z__adt_decile"] = np.where(denom > 1e-12, (num - med) / denom, np.nan)
        hrs = pd.to_numeric(out["fe__canonical__elapsed_sec_since_first_bet__today"], errors="coerce") / 3600.0
        wager_hr = pd.to_numeric(out["fe__canonical__wager_sum__today"], errors="coerce") / hrs.clip(lower=0.1)
        p95_wh = out["_peer_p95__wager_hr"]
        out["fe__hot__wager_per_hr_over_peer_p95"] = np.where(p95_wh > 1e-9, wager_hr / p95_wh, np.nan)
        games_today = out.groupby(["player_id", "gaming_day_event"])["game_id"].transform("nunique")
        p95_g = out["_peer_p95__games_today"]
        out["fe__hot__games_today_over_peer_p95"] = np.where(p95_g > 0, games_today / p95_g, np.nan)
    else:
        for feat, base in _PEER_BASE_COLS.items():
            p95 = _peer_p95(out, base, peer_grp)
            num = pd.to_numeric(out[base], errors="coerce")
            out[feat] = np.where(p95 > 1e-9, num / p95, np.nan)
        out["fe__hot__peer_wager_z__adt_decile"] = _peer_median_mad_z(
            out,
            "fe__canonical__wager_sum__today",
            peer_grp,
        )
        hrs = pd.to_numeric(out["fe__canonical__elapsed_sec_since_first_bet__today"], errors="coerce") / 3600.0
        wager_hr = pd.to_numeric(out["fe__canonical__wager_sum__today"], errors="coerce") / hrs.clip(lower=0.1)
        peer_wager_hr_p95 = (
            out.assign(_wager_hr=wager_hr)
            .groupby(peer_grp, observed=True)["_wager_hr"]
            .transform(lambda s: s.quantile(0.95))
        )
        out["fe__hot__wager_per_hr_over_peer_p95"] = np.where(
            peer_wager_hr_p95 > 1e-9,
            wager_hr / peer_wager_hr_p95,
            np.nan,
        )
        games_today = out.groupby(["player_id", "gaming_day_event"])["game_id"].transform("nunique")
        peer_games_p95 = (
            out.assign(_games=games_today)
            .groupby(peer_grp, observed=True)["_games"]
            .transform(lambda s: s.quantile(0.95))
        )
        out["fe__hot__games_today_over_peer_p95"] = np.where(
            peer_games_p95 > 0,
            games_today / peer_games_p95,
            np.nan,
        )
    miss = pd.to_numeric(out["mid_term_snapshot_missing_flag"], errors="coerce").fillna(1)
    gdays = pd.to_numeric(out["patron__gaming_days_cnt__w180d_m1snap"], errors="coerce")
    out["fe__hot__mid_term_history_sparse_flag"] = (
        (miss > 0) | (gdays.fillna(0) < 3)
    ).astype(np.float64)
    own_p95 = pd.to_numeric(out["patron__theo_win_sum__w180d_m1snap"], errors="coerce") / gdays.replace(0, np.nan)
    own_p95 = own_p95 * 1.5
    wager_today = pd.to_numeric(out["fe__canonical__wager_sum__today"], errors="coerce")
    out["fe__hot__wager_today_over_own_p95__w180d"] = np.where(
        own_p95 > 1e-9,
        wager_today / own_p95,
        np.nan,
    )
    drop_cols = [c for c in out.columns if c.startswith("_peer_") or c == "_adt_decile"]
    out = out.drop(columns=drop_cols, errors="ignore")
    return out


def join_hot_features_to_parquet(
    *,
    base_parquet: Path,
    out_parquet: Path,
    train_parquet: Path | None = None,
) -> Path:
    """Join hot features onto an enriched parquet using train-only peer lookup."""

    train_p = (
        Path(train_parquet).resolve()
        if train_parquet is not None
        else _REPO_ROOT / "trainer_hightier/artifacts/training_data/splits/train.parquet"
    )
    peer_lookup = _peer_lookup_from_train_parquet(train_p)
    lookup_p = Path(out_parquet).with_suffix(".peer_lookup.parquet")
    peer_lookup.to_parquet(lookup_p, index=False)
    bp = str(Path(base_parquet).resolve()).replace("\\", "/")
    lp = str(lookup_p.resolve()).replace("\\", "/")
    op = str(Path(out_parquet).resolve()).replace("\\", "/")
    con = duckdb.connect(database=":memory:")
    try:
        sql = f"""
COPY (
  WITH base AS (
    SELECT
      b.*,
      CAST(b.gaming_day_event AS VARCHAR) AS _gd,
      NTILE(10) OVER (ORDER BY b.patron__adt__w180d_m1snap) - 1 AS _adt_decile,
      TRY_CAST(b.fe__canonical__wager_sum__today AS DOUBLE) AS _wager_today,
      TRY_CAST(b.fe__canonical__avg_wager__today AS DOUBLE) AS _avg_wager_today,
      TRY_CAST(b.fe__wager_sum__w15m AS DOUBLE) AS _wager_w15m,
      TRY_CAST(b.fe__canonical__elapsed_sec_since_first_bet__today AS DOUBLE) AS _elapsed_sec,
      TRY_CAST(b.patron__theo_win_sum__w180d_m1snap AS DOUBLE) AS _theo_w180d,
      TRY_CAST(b.patron__gaming_days_cnt__w180d_m1snap AS DOUBLE) AS _gdays_w180d,
      TRY_CAST(b.mid_term_snapshot_missing_flag AS DOUBLE) AS _snap_miss
    FROM read_parquet('{bp}') AS b
  ),
  games AS (
    SELECT player_id, _gd, COUNT(DISTINCT game_id) AS _games_today
    FROM base
    GROUP BY player_id, _gd
  ),
  joined AS (
    SELECT
      base.*,
      games._games_today,
      lk._peer_p95__fe__canonical__wager_sum__today,
      lk._peer_med__fe__canonical__wager_sum__today,
      lk._peer_mad__fe__canonical__wager_sum__today,
      lk._peer_p95__fe__canonical__avg_wager__today,
      lk._peer_p95__fe__wager_sum__w15m,
      lk._peer_p95__wager_hr,
      lk._peer_p95__games_today
    FROM base
    LEFT JOIN games USING (player_id, _gd)
    LEFT JOIN read_parquet('{lp}') AS lk
      ON base._gd = CAST(lk.gaming_day_event AS VARCHAR)
     AND base._adt_decile = lk._adt_decile
  )
  SELECT
    * EXCLUDE (
      _gd, _adt_decile, _wager_today, _avg_wager_today, _wager_w15m, _elapsed_sec,
      _theo_w180d, _gdays_w180d, _snap_miss, _games_today,
      _peer_p95__fe__canonical__wager_sum__today, _peer_med__fe__canonical__wager_sum__today,
      _peer_mad__fe__canonical__wager_sum__today, _peer_p95__fe__canonical__avg_wager__today,
      _peer_p95__fe__wager_sum__w15m, _peer_p95__wager_hr, _peer_p95__games_today
    ),
    CASE WHEN _peer_p95__fe__canonical__wager_sum__today > 1e-9
      THEN _wager_today / _peer_p95__fe__canonical__wager_sum__today END
      AS fe__hot__wager_today_over_peer_p95__adt_decile,
    CASE WHEN _peer_p95__fe__canonical__avg_wager__today > 1e-9
      THEN _avg_wager_today / _peer_p95__fe__canonical__avg_wager__today END
      AS fe__hot__avg_wager_today_over_peer_p95__adt_decile,
    CASE WHEN _peer_p95__fe__wager_sum__w15m > 1e-9
      THEN _wager_w15m / _peer_p95__fe__wager_sum__w15m END
      AS fe__hot__wager_w15m_over_peer_p95__adt_decile,
    CASE WHEN _peer_p95__wager_hr > 1e-9
      THEN (_wager_today / GREATEST(_elapsed_sec / 3600.0, 0.1)) / _peer_p95__wager_hr END
      AS fe__hot__wager_per_hr_over_peer_p95,
    CASE WHEN _peer_p95__games_today > 0
      THEN CAST(_games_today AS DOUBLE) / _peer_p95__games_today END
      AS fe__hot__games_today_over_peer_p95,
    CASE WHEN COALESCE(_snap_miss, 1) > 0 OR COALESCE(_gdays_w180d, 0) < 3 THEN 1.0 ELSE 0.0 END
      AS fe__hot__mid_term_history_sparse_flag,
    CASE WHEN (_theo_w180d / NULLIF(_gdays_w180d, 0)) * 1.5 > 1e-9
      THEN _wager_today / ((_theo_w180d / NULLIF(_gdays_w180d, 0)) * 1.5) END
      AS fe__hot__wager_today_over_own_p95__w180d,
    CASE WHEN _peer_mad__fe__canonical__wager_sum__today > 1e-12
      THEN (_wager_today - _peer_med__fe__canonical__wager_sum__today)
           / _peer_mad__fe__canonical__wager_sum__today END
      AS fe__hot__peer_wager_z__adt_decile
  FROM joined
) TO '{op}' (FORMAT PARQUET, COMPRESSION SNAPPY)
"""
        Path(out_parquet).parent.mkdir(parents=True, exist_ok=True)
        con.execute(sql)
    finally:
        con.close()
    _validate_hot_feature_parquet_coverage(Path(out_parquet))
    return Path(out_parquet)


def main() -> None:
    """CLI: join hot-patron features onto an enriched training parquet."""

    parser = argparse.ArgumentParser(description="Materialize hot-patron experiment features")
    parser.add_argument(
        "--input",
        type=Path,
        default=_REPO_ROOT / "trainer_hightier/artifacts/training_data/training_set_fe_enriched.parquet",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_REPO_ROOT / "trainer_hightier/artifacts/training_data/training_set_hot_patron.parquet",
    )
    parser.add_argument(
        "--train-parquet",
        type=Path,
        default=_REPO_ROOT / "trainer_hightier/artifacts/training_data/splits/train.parquet",
    )
    args = parser.parse_args()
    out = join_hot_features_to_parquet(
        base_parquet=args.input,
        out_parquet=args.output,
        train_parquet=args.train_parquet,
    )
    print(f"Wrote {out} with columns {HOT_PATRON_FEATURE_COLUMNS}")


if __name__ == "__main__":
    main()
