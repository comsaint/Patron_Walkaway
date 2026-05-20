"""Feature materialization for high-tier serving (baseline columns, train–serve alignment)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import duckdb  # type: ignore[import-untyped]
import pyarrow.parquet as pq
import numpy as np
import pandas as pd

from trainer_hightier.config import (
    SLOW_PATRON_GRAIN_BET,
    SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    DuckDbRuntimeConfig,
    default_hightier_serving_config,
)
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
    syn = pd.Series(pd.NaT, index=bets.index, dtype=etl.dtype)
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
    pv_col = pd.Series(pd.NaT, index=bets.index, dtype=pcd.dtype)
    if (~bad).any():
        base_good = pd.to_datetime(base[~bad], errors="coerce")
        if getattr(base_good.dt, "tz", None) is None:
            base_good = base_good.dt.tz_localize("Asia/Hong_Kong", ambiguous="NaT", nonexistent="shift_forward")
        base_good_utc = base_good.dt.tz_convert("UTC")
        epoch_ns = base_good_utc.astype("int64")
        eceil = np.ceil(epoch_ns.astype("float64") / (float(poll_i) * 1e9)) * (float(poll_i) * 1e9)
        pv_good = pd.to_datetime(eceil.astype("int64"), unit="ns", utc=True)
        pcd_tz = bets["payout_complete_dtm"].dt.tz
        if pcd_tz is None:
            pv_out = pv_good.dt.tz_convert("Asia/Hong_Kong")
        else:
            pv_out = pv_good.dt.tz_convert(pcd_tz)
        pv_col.loc[~bad] = pv_out.to_numpy()
    out["prediction_visible_ts_cf"] = pv_col
    return out


def _read_canonical_map(path: Path) -> pd.DataFrame:
    """Load mapping parquet; supports legacy two-column files via ``casino_player_id`` fallback."""
    if not path.is_file():
        raise FileNotFoundError(f"canonical mapping parquet missing: {path}")
    schema_names = set(pq.read_schema(path).names)
    cols = ["player_id", "canonical_id"]
    if "casino_player_id" in schema_names:
        cols.append("casino_player_id")
    df = pd.read_parquet(path, columns=cols)
    df = df.dropna(subset=["player_id"])
    df["player_id"] = pd.to_numeric(df["player_id"], errors="coerce").astype("Int64")
    df["canonical_id"] = df["canonical_id"].astype(str)
    if "casino_player_id" not in df.columns:
        df["casino_player_id"] = df["canonical_id"].astype(str)
    else:
        df["casino_player_id"] = df["casino_player_id"].astype(str)
    return df.drop_duplicates(subset=["player_id"], keep="last")


def attach_canonical_id(bets: pd.DataFrame, mapping_parquet: Path | None = None) -> pd.DataFrame:
    """Left-join ``canonical_id`` and ``casino_player_id``; fallback canonical to ``player_id`` when unmapped.

    Drops placeholder ``casino_player_id`` from ClickHouse (often all-null) before merge to avoid ``_x/_y``
    suffix collisions.
    """
    if bets.empty:
        return bets
    mp = Path(mapping_parquet or default_canonical_mapping_parquet_path()).resolve()
    cmap = _read_canonical_map(mp)
    work = bets.drop(columns=["casino_player_id"], errors="ignore")
    out = work.merge(cmap, on="player_id", how="left")
    fallback = out["player_id"].astype(str)
    co = out["canonical_id"]
    mapped = co.notna() & (co.astype(str).str.strip() != "")
    out["canonical_id"] = co.where(mapped, fallback)
    cp = out["casino_player_id"]
    out["casino_player_id"] = cp.where(mapped, pd.NA)
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
    for _fc in ("wager", "payout_odds"):
        if _fc in work.columns:
            work[_fc] = pd.to_numeric(work[_fc], errors="coerce").astype("float64")
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


def _infer_slow_patron_snap_date_column(schema_cols: Iterable[str]) -> str:
    """Pick ASOF anchor date column present in patron-grain monthly slow snapshot Parquet."""

    cols = tuple(str(c) for c in schema_cols)
    by_lower = {c.lower(): c for c in cols}
    for token in ("anchor_gaming_day", "gaming_day"):
        if token in by_lower:
            pick = by_lower[token]
            logger.info("[feature_builder] slow patron ASOF anchor column inferred as %s", pick)
            return pick
    raise ValueError(
        "slow patron snapshot parquet must expose 'anchor_gaming_day' "
        "(preferred) or 'gaming_day'; "
        f"got columns (sample): {sorted(cols)[:40]}"
    )


def _slow_parquet_join_mode(
    schema_cols: Iterable[str],
    *,
    prefer_grain: str | None = None,
) -> str:
    """Return join mode: ``canonical_asof``, ``player_asof``, or ``bet_merge``."""

    by_lower = {str(c).lower(): str(c) for c in schema_cols}
    feast_bet = ("bet_id" in by_lower) and ("prediction_visible_ts_cf" in by_lower)
    canonical_asof = ("canonical_id" in by_lower) and (
        ("anchor_gaming_day" in by_lower) or ("gaming_day" in by_lower)
    )
    legacy_player = ("player_id" in by_lower) and (
        ("anchor_gaming_day" in by_lower) or ("gaming_day" in by_lower)
    )
    grain = str(prefer_grain or "").strip().lower()
    if grain == SLOW_PATRON_GRAIN_CANONICAL_ASOF:
        if not canonical_asof:
            raise ValueError(
                "[feature_builder] manifest requests canonical_asof slow grain but parquet schema "
                f"lacks canonical_id + anchor_gaming_day; columns={sorted(by_lower.keys())[:40]}"
            )
        return "canonical_asof"
    if grain == SLOW_PATRON_GRAIN_BET:
        if not feast_bet:
            raise ValueError(
                "[feature_builder] manifest requests bet_grain slow join but parquet lacks "
                "bet_id + prediction_visible_ts_cf"
            )
        return "bet_merge"
    if grain in ("player_asof", "player"):
        if not legacy_player:
            raise ValueError("[feature_builder] manifest requests player_asof but parquet schema unsupported")
        return "player_asof"

    # Production canonical ASOF artifact (no bet_id).
    if canonical_asof and not feast_bet:
        return "canonical_asof"

    if feast_bet and not legacy_player and not canonical_asof:
        return "bet_merge"

    if legacy_player and not feast_bet:
        return "player_asof"

    if canonical_asof and feast_bet:
        logger.warning(
            "[feature_builder] slow patron parquet has bet-grain and canonical ASOF columns; "
            "defaulting to canonical_asof for production safety. columns=%s",
            sorted(by_lower.keys())[:30],
        )
        return "canonical_asof"

    if feast_bet and legacy_player:
        logger.warning(
            "[feature_builder] slow patron parquet exposes bet-grain and player anchor; "
            "using bet-merge (legacy). columns=%s",
            sorted(by_lower.keys())[:30],
        )
        return "bet_merge"

    raise ValueError(
        "[feature_builder] slow patron parquet unsupported schema "
        "(need canonical_id+anchor_gaming_day, player_id+anchor, or bet_id+prediction_visible_ts_cf); "
        f"columns sample={sorted(by_lower.keys())[:40]}"
    )


def _join_slow_patron_bet_snapshot(
    bets: pd.DataFrame,
    slow_parquet: Path,
) -> pd.DataFrame:
    """Left-merge Feast bet-grain slow features on normalized ``bet_id``."""

    if "bet_id" not in bets.columns:
        raise ValueError("[feature_builder] bets missing bet_id column for Feast slow merge")

    slow_df = pd.read_parquet(Path(slow_parquet).resolve())
    by_lower = {str(c).lower(): str(c) for c in slow_df.columns}
    rk = by_lower.get("bet_id")
    if rk is None:
        raise ValueError("[feature_builder] bet-grain slow parquet missing bet_id column")

    if rk not in slow_df.columns:
        raise ValueError(f"[feature_builder] slow parquet lacks bet key column {rk!r}")

    feat_cols = [c for c in slow_df.columns if str(c).lower() != "bet_id"]

    left = bets.copy()
    left["_slow_bid_merge"] = pd.to_numeric(left["bet_id"], errors="coerce")

    right = pd.DataFrame({"_slow_bid_merge": pd.to_numeric(slow_df[rk], errors="coerce")})
    for c in feat_cols:
        right[c] = slow_df[c]

    out = left.merge(right, on="_slow_bid_merge", how="left")
    return out.drop(columns=["_slow_bid_merge"], errors="ignore")


def join_fe_derived_snapshot(bets: pd.DataFrame, fe_parquet: Path) -> pd.DataFrame:
    """Left-merge bundled ``fe_derived`` features on normalized ``bet_id``."""

    if bets.empty:
        return bets
    sp = Path(fe_parquet).resolve()
    if not sp.is_file():
        raise FileNotFoundError(f"fe_derived parquet missing: {sp}")
    if "bet_id" not in bets.columns:
        raise ValueError("[feature_builder] bets missing bet_id column for fe_derived merge")
    fe_df = pd.read_parquet(sp)
    by_lower = {str(c).lower(): str(c) for c in fe_df.columns}
    rk = by_lower.get("bet_id")
    if rk is None:
        raise ValueError("[feature_builder] fe_derived parquet missing bet_id column")
    feat_cols = [c for c in fe_df.columns if str(c).lower() != "bet_id"]
    left = bets.copy()
    left["_fe_bid_merge"] = pd.to_numeric(left["bet_id"], errors="coerce")
    right = pd.DataFrame({"_fe_bid_merge": pd.to_numeric(fe_df[rk], errors="coerce")})
    for c in feat_cols:
        right[c] = fe_df[c]
    out = left.merge(right, on="_fe_bid_merge", how="left")
    logger.info("[feature_builder] joined fe_derived via bundled parquet %s (%d cols)", sp, len(feat_cols))
    return out.drop(columns=["_fe_bid_merge"], errors="ignore")


def join_production_fe_suppliers(
    bets: pd.DataFrame,
    *,
    fe_short_term_parquet: Path | None,
    mid_term_snapshot_parquet: Path | None,
    short_term_columns: tuple[str, ...],
    mid_term_columns: tuple[str, ...],
) -> pd.DataFrame:
    """Join short-term bet-grain and mid-term canonical ASOF ``fe__*`` suppliers."""

    if bets.empty:
        return bets
    if not short_term_columns and not mid_term_columns:
        return bets
    if "canonical_id" not in bets.columns:
        raise ValueError("[feature_builder] bets missing canonical_id for production fe join")
    if "gaming_day" not in bets.columns:
        raise ValueError("[feature_builder] bets missing gaming_day for production fe join")
    if mid_term_columns and mid_term_snapshot_parquet is None:
        raise ValueError("[feature_builder] mid_term_columns require mid_term_snapshot_parquet")

    from trainer_hightier.config import DuckDbRuntimeConfig
    from trainer_hightier.feature_experiment.dataset_enrich import _MID_TERM_DERIVED_EXPRS

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, DuckDbRuntimeConfig())
        con.register("bets_in", bets)
        short_join = ""
        short_select = ""
        if short_term_columns and fe_short_term_parquet is not None:
            sp = Path(fe_short_term_parquet).resolve()
            if not sp.is_file():
                raise FileNotFoundError(f"fe_short_term parquet missing: {sp}")
            esc = str(sp).replace("'", "''")
            short_select = ",\n  ".join(f's."{c}" AS "{c}"' for c in short_term_columns)
            short_join = f"""
LEFT JOIN read_parquet('{esc}') AS s
  ON TRY_CAST(b.bet_id AS DOUBLE) = s.bet_id"""

        mid_cte = ""
        mid_select = ""
        exclude_cols = ""
        if mid_term_columns and mid_term_snapshot_parquet is not None:
            mp = Path(mid_term_snapshot_parquet).resolve()
            if not mp.is_file():
                raise FileNotFoundError(f"mid_term snapshot parquet missing: {mp}")
            mesc = str(mp).replace("'", "''")
            mid_cte = f"""
mid_snap AS (
  SELECT * FROM read_parquet('{mesc}')
),
b_with_day AS (
  SELECT
    b.*,
    TRIM(CAST(b.canonical_id AS VARCHAR)) AS _cid,
    CAST(b.gaming_day AS DATE) AS _gday
  FROM bets_in AS b
),
mid_asof AS (
  SELECT
    bw.*,
    lst.fe__bets_cnt__w1d AS _snap_bets_cnt_w1d,
    lst.fe__wager_sum__w1d AS _snap_wager_sum_w1d,
    lst.fe__prior_odds_mean_w30d AS _snap_prior_odds_mean_w30d,
    lst.fe__prior_odds_std_w30d AS _snap_prior_odds_std_w30d,
    lst.fe__std_wager_w7d AS _snap_std_wager_w7d,
    lst.fe__avg_abs_wager_w7d AS _snap_avg_abs_wager_w7d,
    lst.fe__interarrival_avg_w7d AS _snap_interarrival_avg_w7d,
    lst.fe__interarrival_std_w7d AS _snap_interarrival_std_w7d
  FROM b_with_day AS bw
  LEFT JOIN LATERAL (
    SELECT *
    FROM mid_snap AS s
    WHERE TRIM(CAST(s.canonical_id AS VARCHAR)) = bw._cid
      AND CAST(s.anchor_gaming_day AS DATE) < bw._gday
    ORDER BY CAST(s.anchor_gaming_day AS DATE) DESC
    LIMIT 1
  ) AS lst ON TRUE
)"""
            mid_parts = []
            for col in mid_term_columns:
                expr = _MID_TERM_DERIVED_EXPRS.get(col, f'CAST(b."{col}" AS DOUBLE)')
                mid_parts.append(f'{expr} AS "{col}"')
            mid_select = ",\n  ".join(mid_parts)
            exclude_cols = ", ".join(
                (
                    "_cid",
                    "_gday",
                    "_snap_bets_cnt_w1d",
                    "_snap_wager_sum_w1d",
                    "_snap_prior_odds_mean_w30d",
                    "_snap_prior_odds_std_w30d",
                    "_snap_std_wager_w7d",
                    "_snap_avg_abs_wager_w7d",
                    "_snap_interarrival_avg_w7d",
                    "_snap_interarrival_std_w7d",
                )
            )

        if mid_cte:
            base_from = "FROM mid_asof AS b"
            base_select = f"b.* EXCLUDE ({exclude_cols})" if exclude_cols else "b.*"
        else:
            base_from = "FROM bets_in AS b"
            base_select = "b.*"

        select_parts = [base_select]
        if short_select:
            select_parts.append(short_select)
        if mid_select:
            select_parts.append(mid_select)

        if mid_cte:
            sql = f"""
WITH {mid_cte.strip()}
SELECT
  {", ".join(select_parts)}
{base_from}
{short_join}
""".strip()
        else:
            sql = f"""
SELECT
  {", ".join(select_parts)}
{base_from}
{short_join}
""".strip()
        out = con.execute(sql).df()
        logger.info(
            "[feature_builder] joined production fe suppliers short=%d mid=%d",
            len(short_term_columns),
            len(mid_term_columns),
        )
        return out
    finally:
        con.close()


def attach_mid_term_composite_columns(
    bets: pd.DataFrame,
    columns: tuple[str, ...],
) -> pd.DataFrame:
    """Derive bet-grain mid-term composite ``fe__*`` from Feast + short-term inputs."""
    if bets.empty or not columns:
        return bets
    out = bets.copy()
    col_set = set(columns)
    if "fe__wager_sum__w15m_over_w1d" in col_set:
        num = pd.to_numeric(out.get("fe__wager_sum__w15m"), errors="coerce")
        den = pd.to_numeric(out.get("fe__wager_sum__w1d"), errors="coerce")
        out["fe__wager_sum__w15m_over_w1d"] = np.where(
            den > 1e-9,
            num / den,
            np.nan,
        )
    if "fe__wager_cv_w7d" in col_set:
        avg_abs = pd.to_numeric(out.get("fe__avg_abs_wager_w7d"), errors="coerce")
        std_w = pd.to_numeric(out.get("fe__std_wager_w7d"), errors="coerce")
        out["fe__wager_cv_w7d"] = np.where(
            avg_abs > 1e-12,
            std_w / avg_abs,
            np.nan,
        )
    if "fe__payout_odds_z_prior_w30d" in col_set:
        prior_mean = pd.to_numeric(out.get("fe__prior_odds_mean_w30d"), errors="coerce")
        prior_std = pd.to_numeric(out.get("fe__prior_odds_std_w30d"), errors="coerce")
        odds = pd.to_numeric(out.get("payout_odds"), errors="coerce")
        out["fe__payout_odds_z_prior_w30d"] = np.where(
            prior_std.abs() > 1e-12,
            (odds - prior_mean) / prior_std,
            np.nan,
        )
    if "fe__interarrival__last_gap_z__w7d" in col_set:
        gap = pd.to_numeric(out.get("fe__time_since_last_bet_sec"), errors="coerce")
        avg_gap = pd.to_numeric(out.get("fe__interarrival_avg_w7d"), errors="coerce")
        std_gap = pd.to_numeric(out.get("fe__interarrival_std_w7d"), errors="coerce")
        out["fe__interarrival__last_gap_z__w7d"] = np.where(
            std_gap > 1e-9,
            (gap - avg_gap) / std_gap,
            np.nan,
        )
    logger.info("[feature_builder] attached mid_term composite cols=%d", len(columns))
    return out


def _join_slow_patron_canonical_asof_snapshot(
    bets: pd.DataFrame,
    slow_parquet: Path,
    *,
    key_days: str | None = None,
) -> pd.DataFrame:
    """ASOF-join slow patron features on ``canonical_id`` × ``gaming_day``."""

    if "canonical_id" not in bets.columns:
        raise ValueError("bets must contain canonical_id for canonical ASOF slow join")
    if "gaming_day" not in bets.columns:
        raise ValueError("bets must contain gaming_day for canonical ASOF slow join")
    schema_cols = list(pq.read_schema(slow_parquet).names)
    by_lower = {str(c).lower(): str(c) for c in schema_cols}
    exp = key_days.strip() if key_days is not None and str(key_days).strip() else ""
    if exp:
        anchor_sql_col = by_lower.get(exp.lower())
        if anchor_sql_col is None:
            raise ValueError(f"slow patron parquet lacks ASOF column {exp!r}")
    else:
        anchor_sql_col = _infer_slow_patron_snap_date_column(schema_cols)
    con = duckdb.connect(database=":memory:")
    try:
        con.register("bets_in", bets)
        esc = str(slow_parquet).replace("'", "''")
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
    TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
    CAST("{anchor_sql_col.replace('"', '""')}" AS DATE) AS anchor_gday,
    patron__theo_win_sum__w180d_m1snap,
    patron__gaming_days_cnt__w180d_m1snap,
    patron__adt__w180d_m1snap
  FROM slow
) AS lst
  ON b.canonical_id = lst.canonical_id
 AND lst.anchor_gday <= b.bet_gday
ORDER BY b.bet_id, lst.anchor_gday
""".strip()
        out = con.execute(q).df()
        return out.drop(columns=["bet_gday"], errors="ignore")
    finally:
        con.close()


def join_slow_patron_snapshot(
    bets: pd.DataFrame,
    slow_parquet: Path,
    *,
    key_days: str | None = None,
    slow_grain: str | None = None,
) -> pd.DataFrame:
    """Attach slow patron 180d features (canonical ASOF, player ASOF, or legacy bet-grain)."""

    if bets.empty:
        return bets
    sp = Path(slow_parquet).resolve()
    if not sp.is_file():
        raise FileNotFoundError(f"slow patron snapshot parquet missing: {sp}")
    schema_cols = list(pq.read_schema(sp).names)
    mode = _slow_parquet_join_mode(schema_cols, prefer_grain=slow_grain)
    if mode == "bet_merge":
        logger.info("[feature_builder] joining slow patron via Feast bet-grain parquet %s", sp)
        return _join_slow_patron_bet_snapshot(bets, sp)
    if mode == "canonical_asof":
        logger.info("[feature_builder] joining slow patron via canonical ASOF parquet %s", sp)
        return _join_slow_patron_canonical_asof_snapshot(bets, sp, key_days=key_days)

    by_lower = {str(c).lower(): str(c) for c in schema_cols}
    exp = (
        key_days.strip()
        if key_days is not None and str(key_days).strip()
        else ""
    )
    if exp:
        el = exp.lower()
        if el not in by_lower:
            raise ValueError(
                f"slow patron parquet lacks explicit ASOF column {exp!r}; columns={sorted(schema_cols)}"
            )
        anchor_sql_col = by_lower[el]
        logger.debug("[feature_builder] slow patron ASOF column from kwarg=%s", anchor_sql_col)
    else:
        anchor_sql_col = _infer_slow_patron_snap_date_column(schema_cols)

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
    CAST("{anchor_sql_col.replace('"', '""')}" AS DATE) AS anchor_gday,
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
