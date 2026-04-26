"""B2: ``t_game`` Parquet-backed table context (PIT-safe, pushdown + small materialization).

Reads only ``data/gmwds_t_game.parquet`` via DuckDB with ``table_id`` and time filters.
No full-file pandas loads. Features use **strictly prior** resolved games vs each bet's
``payout_complete_dtm`` (``merge_asof`` with ``allow_exact_matches=False``).
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_TG_CORE_COLS = frozenset(
    {"game_id", "table_id", "payout_complete_dtm", "outcome", "game_status"}
)


def _to_hk_naive_datetime64_ns(series: pd.Series) -> pd.Series:
    """Normalize datetime-like values to HK-local tz-naive datetime64[ns]."""
    ts = pd.to_datetime(series, errors="coerce")
    if isinstance(ts.dtype, pd.DatetimeTZDtype):
        ts = ts.dt.tz_convert("Asia/Hong_Kong").dt.tz_localize(None)
    return ts.astype("datetime64[ns]")


def _schema_names(parquet_path: Path) -> set[str]:
    import pyarrow.parquet as pq

    return set(pq.read_schema(parquet_path).names)


def _dedupe_order_sql(names: set[str]) -> str:
    clauses: list[str] = []
    if "__ts_ms" in names:
        clauses.append("CAST(__ts_ms AS BIGINT) DESC NULLS LAST")
    if "__etl_insert_Dtm" in names:
        clauses.append("CAST(__etl_insert_Dtm AS TIMESTAMP) DESC NULLS LAST")
    clauses.append("payout_complete_dtm DESC NULLS LAST")
    return ", ".join(clauses)


def materialize_resolved_t_games(
    parquet_path: Path,
    table_ids: Sequence[Any],
    t_min: Union[pd.Timestamp, datetime],
    t_max: Union[pd.Timestamp, datetime],
) -> pd.DataFrame:
    """Load deduped RESOLVED ``t_game`` rows for given tables and payout time window.

    Parameters
    ----------
    parquet_path:
        Path to ``gmwds_t_game.parquet`` (must exist).
    table_ids:
        Distinct ``table_id`` values to push down (empty -> empty frame).
    t_min, t_max:
        Half-open filter ``t_min <= payout_complete_dtm < t_max`` (naive HK-local).

    Returns
    -------
    DataFrame
        Columns include at least
        ``game_id, table_id, payout_complete_dtm, outcome, game_status`` and optional
        ``num_players, total_turnover, casino_win``.
    """
    try:
        import duckdb
    except ImportError as e:
        raise RuntimeError("materialize_resolved_t_games requires duckdb") from e

    p = Path(parquet_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"t_game parquet not found: {p}")
    names = _schema_names(p)
    miss = _TG_CORE_COLS - names
    if miss:
        raise ValueError(f"t_game parquet missing required columns: {sorted(miss)}")

    _ids = sorted(
        {int(x) for x in table_ids if pd.notna(x)},
    )
    if not _ids:
        return pd.DataFrame()

    t0 = pd.Timestamp(t_min)
    t1 = pd.Timestamp(t_max)
    if getattr(t0, "tzinfo", None) is not None:
        t0 = t0.tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    if getattr(t1, "tzinfo", None) is not None:
        t1 = t1.tz_convert("Asia/Hong_Kong").replace(tzinfo=None)

    if "__etl_insert_Dtm" not in names:
        raise ValueError(
            "t_game parquet missing required visibility column '__etl_insert_Dtm' while T_GAME features are enabled"
        )
    extra = [c for c in ("num_players", "total_turnover", "casino_win") if c in names]
    meta = [c for c in ("__ts_ms", "__etl_insert_Dtm") if c in names]
    sel = sorted(_TG_CORE_COLS | set(extra) | set(meta))
    order_expr = _dedupe_order_sql(names)
    path_sql = str(p).replace("'", "''")
    t0s = t0.strftime("%Y-%m-%d %H:%M:%S")
    t1s = t1.strftime("%Y-%m-%d %H:%M:%S")

    con = duckdb.connect(database=":memory:")
    try:
        con.register("tid", pd.DataFrame({"table_id": np.array(_ids, dtype=np.int64)}))
        sql = f"""
        WITH raw AS (
          SELECT {", ".join('"' + c.replace('"', '""') + '"' for c in sel)}
          FROM read_parquet('{path_sql}')
          WHERE table_id IN (SELECT table_id FROM tid)
            AND payout_complete_dtm >= TIMESTAMP '{t0s}'
            AND payout_complete_dtm < TIMESTAMP '{t1s}'
            AND CAST(__etl_insert_Dtm AS TIMESTAMP) <= TIMESTAMP '{t1s}'
        ),
        dedup AS (
          SELECT *,
            ROW_NUMBER() OVER (PARTITION BY game_id ORDER BY {order_expr}) AS rn
          FROM raw
        )
        SELECT * EXCLUDE (rn) FROM dedup
        WHERE rn = 1 AND upper(CAST(game_status AS VARCHAR)) = 'RESOLVED'
        """
        return con.execute(sql).df()
    finally:
        con.close()


def _outcome_streak_and_rates(g: pd.DataFrame) -> pd.DataFrame:
    """Per-table sorted games: streak / rolling rates from **prior** rows (shift)."""
    g = g.sort_values("payout_complete_dtm", kind="stable").reset_index(drop=True)
    o = g["outcome"].astype(str).str.upper().str.strip()
    is_void = o.isin(["VOID", "UNRESOLVED", ""])
    bank = o.eq("BANKER").astype(np.float64)
    play = o.eq("PLAYER").astype(np.float64)
    tie = o.eq("TIE").astype(np.float64)

    streak: list[int] = []
    cur = 0
    last: Optional[str] = None
    for i, lab in enumerate(o.to_numpy()):
        if is_void.iloc[i]:
            cur = 0
            last = None
            streak.append(0)
            continue
        lab_s = str(lab)
        if last is None or lab_s != last:
            cur = 1
            last = lab_s
        else:
            cur += 1
        streak.append(cur)

    g = g.copy()
    g["_streak_incl"] = streak
    g["_streak_prior"] = pd.Series(g["_streak_incl"], dtype="float64").shift(1).fillna(0.0)
    for col, ser in (("_b", bank), ("_p", play), ("_t", tie)):
        g[col] = ser
    g["banker_rate_w20games"] = g["_b"].shift(1).rolling(20, min_periods=1).mean().fillna(0.0)
    g["player_rate_w20games"] = g["_p"].shift(1).rolling(20, min_periods=1).mean().fillna(0.0)
    g["tie_rate_w20games"] = g["_t"].shift(1).rolling(20, min_periods=1).mean().fillna(0.0)
    g["current_outcome_streak_len"] = g["_streak_prior"].clip(lower=0, upper=1e6).astype("float64")

    if "num_players" in g.columns:
        npv = pd.to_numeric(g["num_players"], errors="coerce").fillna(0.0)
        g["table_num_players"] = npv.shift(1).fillna(0.0)
    else:
        g["table_num_players"] = 0.0
    g["patron_is_sole_player"] = (g["table_num_players"] == 1.0).astype("float64")

    if "total_turnover" in g.columns:
        to = pd.to_numeric(g["total_turnover"], errors="coerce").fillna(0.0)
        _ts = pd.DatetimeIndex(g["payout_complete_dtm"])
        _tos = pd.Series(to.shift(1).to_numpy(), index=_ts)
        _r5 = _tos.rolling("5min", min_periods=1).sum()
        _r15 = _tos.rolling("15min", min_periods=1).sum()
        ratio = (_r5 / _r15.replace(0, np.nan)).to_numpy()
        g["table_turnover_w5m_over_w15m"] = np.nan_to_num(ratio, nan=0.0).clip(0.0, 10.0)
    else:
        g["table_turnover_w5m_over_w15m"] = 0.0

    if "casino_win" in g.columns:
        cw = pd.to_numeric(g["casino_win"], errors="coerce").fillna(0.0)
        _ts2 = pd.DatetimeIndex(g["payout_complete_dtm"])
        _cws = pd.Series(cw.shift(1).to_numpy(), index=_ts2)
        g["table_net_outcome_w15m"] = (
            _cws.rolling("15min", min_periods=1).sum().to_numpy().clip(-1e9, 1e9)
        )
    else:
        g["table_net_outcome_w15m"] = 0.0

    drop_cols = [c for c in g.columns if c.startswith("_")]
    return g.drop(columns=drop_cols, errors="ignore")


def _build_game_feature_timeline(games: pd.DataFrame) -> pd.DataFrame:
    if games.empty:
        return pd.DataFrame(
            columns=[
                "table_id",
                "payout_complete_dtm",
                "current_outcome_streak_len",
                "banker_rate_w20games",
                "player_rate_w20games",
                "tie_rate_w20games",
                "table_num_players",
                "patron_is_sole_player",
                "table_turnover_w5m_over_w15m",
                "table_net_outcome_w15m",
            ]
        )
    games = games.copy()
    games["payout_complete_dtm"] = _to_hk_naive_datetime64_ns(games["payout_complete_dtm"])
    games["game_visible_dtm"] = _to_hk_naive_datetime64_ns(games["__etl_insert_Dtm"])
    games = games[
        games["payout_complete_dtm"].notna() & games["game_visible_dtm"].notna()
    ].sort_values(
        ["table_id", "payout_complete_dtm"], kind="stable"
    )
    parts: list[pd.DataFrame] = []
    for tid, grp in games.groupby("table_id", sort=False):
        grp = grp.sort_values("payout_complete_dtm", kind="stable")
        parts.append(_outcome_streak_and_rates(grp))
    out = pd.concat(parts, ignore_index=True)
    keep = [
        "table_id",
        "payout_complete_dtm",
        "game_visible_dtm",
        "current_outcome_streak_len",
        "banker_rate_w20games",
        "player_rate_w20games",
        "tie_rate_w20games",
        "table_num_players",
        "patron_is_sole_player",
        "table_turnover_w5m_over_w15m",
        "table_net_outcome_w15m",
    ]
    return out[[c for c in keep if c in out.columns]]


def join_t_game_features_for_bets(
    bets_df: pd.DataFrame,
    *,
    t_game_parquet: Path,
    window_start: Union[pd.Timestamp, datetime],
    window_end: Union[pd.Timestamp, datetime],
) -> pd.DataFrame:
    """Attach B2 ``t_game`` features to *bets_df* (mutates copy only).

    Parameters
    ----------
    bets_df:
        Must include ``table_id``, ``payout_complete_dtm``, ``bet_id``.
    t_game_parquet:
        Path to ``gmwds_t_game.parquet``.
    window_start, window_end:
        Materialization bounds (typically chunk ``history_start`` .. ``extended_end``).

    Returns
    -------
    Copy of ``bets_df`` with B2 columns added (0-filled on failure / missing join).
    """
    req = {"table_id", "payout_complete_dtm", "bet_id"}
    miss = req - set(bets_df.columns)
    if miss:
        raise ValueError(f"join_t_game_features_for_bets: bets_df missing {sorted(miss)}")

    out = bets_df.copy()
    out["payout_complete_dtm"] = _to_hk_naive_datetime64_ns(out["payout_complete_dtm"])
    feat_cols = [
        "current_outcome_streak_len",
        "banker_rate_w20games",
        "player_rate_w20games",
        "tie_rate_w20games",
        "table_num_players",
        "patron_is_sole_player",
        "table_turnover_w5m_over_w15m",
        "table_net_outcome_w15m",
    ]
    for c in feat_cols:
        if c not in out.columns:
            out[c] = 0.0

    tids = out["table_id"].tolist()
    raw = materialize_resolved_t_games(t_game_parquet, tids, window_start, window_end)
    if raw.empty:
        logger.info("t_game context: no resolved rows in window (tables=%d)", len(set(tids)))
        return out

    timeline = _build_game_feature_timeline(raw)
    if timeline.empty:
        return out

    left = out.reset_index(drop=True).copy()
    left["_row"] = np.arange(len(left), dtype=np.int64)
    merged_parts: list[pd.DataFrame] = []
    right_cols = [c for c in timeline.columns if c != "table_id"]
    for tid, left_grp in left.groupby("table_id", sort=False):
        right_grp = timeline[timeline["table_id"] == tid]
        if right_grp.empty:
            _empty = left_grp.copy()
            for c in right_cols:
                if c not in _empty.columns:
                    _empty[c] = np.nan
            merged_parts.append(_empty)
            continue
        lg = left_grp.drop(columns=feat_cols, errors="ignore").sort_values(
            "payout_complete_dtm", kind="stable"
        )
        rg = right_grp.sort_values("game_visible_dtm", kind="stable")
        mg = pd.merge_asof(
            lg,
            rg[right_cols],
            left_on="payout_complete_dtm",
            right_on="game_visible_dtm",
            direction="backward",
            allow_exact_matches=False,
        )
        merged_parts.append(mg)
    merged = pd.concat(merged_parts, ignore_index=True).sort_values("_row", kind="stable")
    for c in feat_cols:
        if c in merged.columns:
            out[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0.0).to_numpy()
    return out
