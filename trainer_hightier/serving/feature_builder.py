"""Feature materialization for high-tier serving (baseline columns, train–serve alignment)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import duckdb  # type: ignore[import-untyped]
import numpy as np
import pandas as pd

from trainer_hightier.config import DuckDbRuntimeConfig, default_hightier_serving_config
from trainer_hightier.utils.bet_l0_preprocess import _bet_feast_prediction_visible_alignment_params
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)


def attach_synthetic_etl_and_prediction_visible(bets: pd.DataFrame) -> pd.DataFrame:
    """Add ``__etl_insert_Dtm_synthetic`` and ``prediction_visible_ts_cf`` like L0 preprocess.

    Parameters
    ----------
    bets
        Must include ``__etl_insert_Dtm`` and ``payout_complete_dtm`` as datetimes (HK-aware).
    """
    if bets.empty:
        return bets
    need = ("__etl_insert_Dtm", "payout_complete_dtm")
    for c in need:
        if c not in bets.columns:
            raise ValueError(f"bets missing {c!r}; columns={list(bets.columns)}")
    cfg = default_hightier_serving_config()
    adm = int(cfg.bet_avail_delay_min)
    etl = pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce")
    pcd = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce")
    cap_td = pd.Timedelta(minutes=adm)
    syn = pd.Series(pd.NaT, index=bets.index)
    ok = etl.notna() & pcd.notna()
    syn.loc[ok] = pd.concat(
        [etl.loc[ok], pcd.loc[ok] + cap_td],
        axis=1,
    ).min(axis=1)
    out = bets.copy()
    out["__etl_insert_Dtm_synthetic"] = syn
    _, poll = _bet_feast_prediction_visible_alignment_params()
    poll_i = max(1, int(poll))
    base = pd.concat([syn, pcd + cap_td], axis=1).max(axis=1)
    bad = base.isna()
    out["prediction_visible_ts_cf"] = pd.NaT
    if (~bad).any():
        epoch_ns = base[~bad].astype("datetime64[ns]").astype("int64")
        eceil = np.ceil(epoch_ns.astype("float64") / (float(poll_i) * 1e9)) * (float(poll_i) * 1e9)
        pv_good = pd.to_datetime(eceil.astype("int64"), unit="ns", utc=True)
        pcd_tz = bets["payout_complete_dtm"].dt.tz
        if pcd_tz is None:
            pv_hk = pv_good.dt.tz_convert("Asia/Hong_Kong")
        else:
            pv_hk = pv_good.dt.tz_convert(pcd_tz)
        out.loc[~bad, "prediction_visible_ts_cf"] = pv_hk.to_numpy()
    return out


def _read_canonical_map(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"canonical mapping parquet missing: {path}")
    df = pd.read_parquet(path, columns=["player_id", "canonical_id"])
    df = df.dropna(subset=["player_id"])
    df["player_id"] = pd.to_numeric(df["player_id"], errors="coerce").astype("Int64")
    df["canonical_id"] = df["canonical_id"].astype(str)
    return df.drop_duplicates(subset=["player_id"], keep="last")


def attach_canonical_id(bets: pd.DataFrame, mapping_parquet: Path | None = None) -> pd.DataFrame:
    """Left-join ``canonical_id``; fallback to string ``player_id`` when unmapped."""
    if bets.empty:
        return bets
    mp = Path(mapping_parquet or default_canonical_mapping_parquet_path()).resolve()
    cmap = _read_canonical_map(mp)
    out = bets.merge(cmap, on="player_id", how="left")
    fallback = out["player_id"].astype(str)
    co = out["canonical_id"]
    out["canonical_id"] = co.where(co.notna() & (co.astype(str).str.strip() != ""), fallback)
    return out


def attach_trial_bet_behavior_1h(
    bets: pd.DataFrame,
    pool: pd.DataFrame,
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> pd.DataFrame:
    """Compute 1h window features identical to ``trial_bet_behavior_1h`` materialization.

    ``pool`` must include all prior rows needed for RANGE windows (HK-aware ``payout_complete_dtm``).
    """
    if bets.empty:
        return bets
    need_cols = (
        "bet_id",
        "player_id",
        "canonical_id",
        "payout_complete_dtm",
        "wager",
        "is_back_bet",
        "payout_odds",
        "prediction_visible_ts_cf",
        "__etl_insert_Dtm_synthetic",
    )
    work = pool[list(need_cols)].copy()
    work = work.drop_duplicates(subset=["bet_id"], keep="last")
    con = duckdb.connect(database=":memory:")
    try:
        if duckdb_runtime is not None:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.register("work", work)
        con.execute(
            """
            CREATE TABLE src AS
            SELECT
                CAST(bet_id AS DOUBLE) AS bet_id,
                CAST(player_id AS BIGINT) AS player_id,
                CAST(canonical_id AS VARCHAR) AS canonical_id,
                CAST(payout_complete_dtm AS TIMESTAMPTZ) AS payout_complete_dtm,
                CAST(wager AS DOUBLE) AS wager,
                CAST(is_back_bet AS INTEGER) AS is_back_bet,
                CAST(payout_odds AS DOUBLE) AS payout_odds,
                CAST(prediction_visible_ts_cf AS TIMESTAMPTZ) AS prediction_visible_ts_cf,
                CAST(__etl_insert_Dtm_synthetic AS TIMESTAMPTZ) AS __etl_insert_Dtm_synthetic
            FROM work
            """
        )
        inner = """
WITH ordered AS (
  SELECT
    *,
    COUNT(*) OVER (
      PARTITION BY canonical_id
      ORDER BY payout_complete_dtm
      RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND INTERVAL '1' MICROSECOND PRECEDING
    )::BIGINT AS _cnt,
    COALESCE(
      SUM(wager) OVER (
        PARTITION BY canonical_id
        ORDER BY payout_complete_dtm
        RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND INTERVAL '1' MICROSECOND PRECEDING
      ),
      0.0
    ) AS _wsum,
    COALESCE(
      SUM(CASE WHEN COALESCE(is_back_bet, 0) = 1 THEN 1.0 ELSE 0.0 END) OVER (
        PARTITION BY canonical_id
        ORDER BY payout_complete_dtm
        RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND INTERVAL '1' MICROSECOND PRECEDING
      ),
      0.0
    ) AS _back_sum,
    AVG(CAST(payout_odds AS DOUBLE)) OVER (
      PARTITION BY canonical_id
      ORDER BY payout_complete_dtm
      RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND INTERVAL '1' MICROSECOND PRECEDING
    ) AS _odds_avg
  FROM src
)
SELECT
  bet_id AS _bid,
  _cnt AS bet__bets_cnt__w1h,
  CAST(_wsum AS DOUBLE) AS bet__wager_sum__w1h,
  CAST(
    CASE WHEN _cnt > 0 THEN _back_sum / CAST(_cnt AS DOUBLE) ELSE 0.0 END
  AS DOUBLE) AS bet__back_bet_ratio__w1h,
  CAST(COALESCE(_odds_avg, 0.0) AS DOUBLE) AS bet__payout_odds_avg__w1h
FROM ordered
""".strip()
        feat = con.execute(inner).df()
    finally:
        con.close()
    keyed = bets.copy()
    keyed["_bid"] = keyed["bet_id"].astype(float)
    merged = keyed.merge(feat, on="_bid", how="left")
    merged = merged.drop(columns=["_bid"])
    for c in (
        "bet__bets_cnt__w1h",
        "bet__wager_sum__w1h",
        "bet__back_bet_ratio__w1h",
        "bet__payout_odds_avg__w1h",
    ):
        if c not in merged.columns:
            merged[c] = np.nan
    return merged


def join_slow_patron_snapshot(
    bets: pd.DataFrame,
    slow_parquet: Path,
    *,
    key_days: str = "anchor_gaming_day",
) -> pd.DataFrame:
    """ASOF-join slow patron monthly snapshot on ``player_id`` / ``gaming_day``."""
    if bets.empty:
        return bets
    sp = Path(slow_parquet).resolve()
    if not sp.is_file():
        raise FileNotFoundError(f"slow patron snapshot parquet missing: {sp}")
    if "gaming_day" not in bets.columns:
        raise ValueError("bets must contain gaming_day for slow patron join")
    con = duckdb.connect(database=":memory:")
    try:
        con.register("bets_in", bets)
        esc = str(sp).replace("'", "''")
        q = f"""
WITH slow AS (
  SELECT * FROM read_parquet('{esc}')
),
b AS (
  SELECT *, CAST(gaming_day AS DATE) AS bet_gday FROM bets_in
)
SELECT
  b.*,
  lst.patron__theo_win_sum__w180d_m1snap,
  lst.patron__gaming_days_cnt__w180d_m1snap,
  lst.patron__adt__w180d_m1snap
FROM b
ASOF JOIN (
  SELECT
    player_id,
    CAST({key_days} AS DATE) AS anchor_gday,
    patron__theo_win_sum__w180d_m1snap,
    patron__gaming_days_cnt__w180d_m1snap,
    patron__adt__w180d_m1snap
  FROM slow
) AS lst
  ON b.player_id = lst.player_id
 AND lst.anchor_gday <= b.bet_gday
ORDER BY b.bet_id, lst.anchor_gday
""".strip()
        out = con.execute(q).df()
        return out.drop(columns=["bet_gday"], errors="ignore")
    finally:
        con.close()


def coerce_categoricals(
    frame: pd.DataFrame,
    categorical_columns: Iterable[str],
    category_categories: dict[str, list],
) -> pd.DataFrame:
    """Cast categorical columns for LightGBM sklearn wrapper."""
    out = frame.copy()
    for c in categorical_columns:
        if c not in out.columns:
            continue
        cats = category_categories.get(c)
        if not cats:
            out[c] = out[c].astype("category")
            continue
        out[c] = pd.Categorical(out[c], categories=list(cats))
    return out


def assert_features_ready(frame: pd.DataFrame, feature_columns: tuple[str, ...]) -> None:
    """Fail fast when model inputs are missing."""
    miss = [c for c in feature_columns if c not in frame.columns]
    if miss:
        raise ValueError(f"feature columns missing from serving frame: {miss}")
