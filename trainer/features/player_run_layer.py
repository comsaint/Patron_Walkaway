"""Player-layer features from L1 run primitives (PIT-safe, run_end_ts < bet_time).

Aggregates closed runs per patron using ``run_fact`` (+ optional ``run_bet_map``)
and bridge bet/session Parquet. When ``run_bet_map`` is absent (e.g.
``parallel_lda_mvp`` snapshot layout), bets are mapped to runs by
``player_id`` (bridge bet has no ``canonical_id``) + ``payout_complete_dtm``
within ``[run_start_ts, run_end_ts]`` (same bets as a Hive ``run_bet_map`` when
staging is consistent). Window semantics for each bet at time ``t =
payout_complete_dtm``:

- **PIT**: only runs with ``run_end_ts < t`` (strict).
- **Rolling window** ``[t - W days, t)``: ``run_end_ts >= t - W days AND run_end_ts < t``.
- **active_days**: ``COUNT(DISTINCT gaming_day)`` over bets belonging to qualifying runs.

Raises ``RuntimeError`` when local layered assets or required columns are missing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

import duckdb
import numpy as np
import pandas as pd

from trainer.features.features import _track_section_enabled_in_spec, get_candidate_feature_ids
from trainer.training.data_sources import LOCAL_PARQUET_DIR, PROJECT_ROOT, load_trainer_local_parquet_bridge_manifest
from trainer.training.data_sources import resolve_local_parquet_bet_session_paths_from_manifest
from trainer.training.data_sources import trainer_local_parquet_bridge_manifest_path
from trainer.training.l2_bundle_materialize import read_bridge_source_snapshot_id

logger = logging.getLogger(__name__)

_PLAYER_RUN_WINDOWS_DAYS: Tuple[int, ...] = (7, 30, 90, 180, 365)
_MAX_WINDOW_DAYS = max(_PLAYER_RUN_WINDOWS_DAYS)
_JOIN_LOOKBACK_PAD_DAYS = 40


def _duck_read_parquet(paths: Sequence[str]) -> str:
    """Return DuckDB ``read_parquet(...)`` expression for one or many files."""
    cleaned = [str(Path(p).resolve()).replace("\\", "/").replace("'", "''") for p in paths]
    if len(cleaned) == 1:
        return f"read_parquet('{cleaned[0]}', union_by_name=true)"
    inner = ", ".join(f"'{p}'" for p in cleaned)
    return f"read_parquet([{inner}], union_by_name=true)"


def _escape_sql_path(path: Path) -> str:
    """Escape a filesystem path for DuckDB single-quoted string literals."""
    return str(path.resolve()).replace("\\", "/").replace("'", "''")


def _glob_parquet_files(root: Path, subdir: str) -> List[str]:
    """Return sorted parquet paths under ``root/subdir`` (recursive)."""
    base = root / subdir
    if not base.is_dir():
        return []
    return sorted(str(p.resolve()).replace("\\", "/") for p in base.rglob("*.parquet"))


def _mvp_parquet_under_snap(snap_root: Path, subdir: str, filename_prefix: str) -> List[str]:
    """Collect Parquet paths under ``snap_root/gaming_ym=*/subdir/{prefix}*.parquet`` (MVP layout)."""
    out: List[str] = []
    if not snap_root.is_dir():
        return []
    for ym_dir in sorted(snap_root.glob("gaming_ym=*")):
        gdir = ym_dir / subdir
        if not gdir.is_dir():
            continue
        for fpath in sorted(gdir.glob(f"{filename_prefix}*.parquet")):
            if fpath.is_file():
                out.append(str(fpath.resolve()).replace("\\", "/"))
    return out


def _resolve_snap_root_dir(raw: object, manifest_path: Path) -> Optional[Path]:
    """Resolve manifest ``snap_root`` (repo-relative or absolute) to an existing directory."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    p = Path(s).expanduser()
    if p.is_absolute():
        cand = p.resolve()
        return cand if cand.is_dir() else None
    mp = manifest_path.parent.resolve()
    pr = PROJECT_ROOT.resolve()
    for anchor in (pr, mp):
        cand = (anchor / p).resolve()
        if cand.is_dir():
            return cand
    return None


def resolve_run_primitive_parquet_paths(
    *,
    snapshot_id: str,
    bet_path: Path,
    manifest: dict,
    manifest_path: Path,
) -> Tuple[List[str], List[str], Path, bool]:
    """Discover ``run_fact`` / ``run_bet_map`` parquet lists and membership mode.

    Search order (run_fact):
    1. ``data/l1_layered/<snapshot_id>/run_fact/**/*.parquet`` (Hive L1 layout).
    2. Manifest ``snap_root`` + MVP layout ``gaming_ym=*/run_fact/run_fact__*.parquet``.

    ``run_bet_map``: optional Hive glob under ``l1_layered``; optional MVP glob
    ``gaming_ym=*/run_bet_map/run_bet_map*.parquet``. When still empty, caller uses
    **implicit** membership via run time envelope + ``player_id`` on bet/run_fact.

    Returns
    -------
    run_fact_paths, run_bet_map_paths, resolved_bet_path, implicit_membership
    """
    if not bet_path.is_file():
        raise RuntimeError(f"Bet Parquet missing at {bet_path}")

    l1_root = LOCAL_PARQUET_DIR / "l1_layered" / snapshot_id
    rf = _glob_parquet_files(l1_root, "run_fact")
    bm = _glob_parquet_files(l1_root, "run_bet_map")

    if not rf:
        snap = _resolve_snap_root_dir(manifest.get("snap_root"), manifest_path)
        if snap is not None:
            rf_mvp = _mvp_parquet_under_snap(snap, "run_fact", "run_fact__")
            if rf_mvp:
                rf = rf_mvp
                logger.info(
                    "player_run: using %d MVP run_fact shard(s) under snap_root=%s",
                    len(rf),
                    snap,
                )
            bm_mvp = _mvp_parquet_under_snap(snap, "run_bet_map", "run_bet_map")
            if bm_mvp:
                bm = bm_mvp

    if not rf:
        snap_hint = manifest.get("snap_root")
        raise RuntimeError(
            "player_run_asset requires run_fact Parquet. Tried:\n"
            f"  — {l1_root / 'run_fact'}\n"
            f"  — MVP layout under snap_root={snap_hint!r} (resolved from bridge manifest)\n"
            "Materialize run_fact (e.g. ``python -m parallel_lda_mvp.run_mvp …`` with emit-trainer bridge, "
            "or LDA Gate1 ``run_fact`` under data/l1_layered)."
        )

    implicit = len(bm) == 0
    if implicit:
        logger.info(
            "player_run: no run_bet_map shards — using implicit bet↔run membership "
            "(player_id + payout_complete_dtm within [run_start_ts, run_end_ts])."
        )

    return rf, bm, bet_path, implicit


def _parquet_column_names(path: Path) -> Set[str]:
    """Return column names for a Parquet file."""
    import pyarrow.parquet as pq

    try:
        return set(pq.read_schema(path).names)
    except Exception as exc:
        raise RuntimeError(f"Cannot read Parquet schema from {path}") from exc


def _require_player_run_enabled(spec: dict) -> None:
    """Raise when ``player_run_asset`` is disabled in *spec*."""
    if not _track_section_enabled_in_spec(spec, "player_run_asset"):
        raise RuntimeError(
            "attach_player_run_features called while player_run_asset is disabled in feature spec."
        )


def _bet_membership_join_sql(*, bet_from: str, implicit: bool, bm_from: str = "") -> str:
    """JOIN clause linking ``rf`` to bet rows (explicit ``run_bet_map`` or time envelope)."""
    if implicit:
        # Bridge bet Parquet (_REQUIRED_BET_PARQUET_COLS) has player_id, not canonical_id.
        return f"""INNER JOIN {bet_from} AS b
  ON CAST(b.player_id AS VARCHAR) = CAST(rf.player_id AS VARCHAR)
 AND CAST(b.payout_complete_dtm AS TIMESTAMP) >= CAST(rf.run_start_ts AS TIMESTAMP)
 AND CAST(b.payout_complete_dtm AS TIMESTAMP) <= CAST(rf.run_end_ts AS TIMESTAMP)"""
    if not bm_from:
        raise ValueError("bm_from required when implicit_membership is False")
    return f"""INNER JOIN {bm_from} AS bm ON rf.run_id = bm.run_id
INNER JOIN {bet_from} AS b
  ON CAST(b.bet_id AS VARCHAR) = CAST(bm.bet_id AS VARCHAR)"""


def _join_run_feats_to_bet_bg(*, implicit: bool, bet_from: str, bm_from: str, join_kw: str) -> str:
    """Link ``run_feats r`` to bridge bet rows for sidecar aggregates (gaming_day, table_id, …)."""
    norm = join_kw.strip().upper()
    if norm not in {"LEFT JOIN", "INNER JOIN"}:
        raise ValueError(f"join_kw must be LEFT JOIN or INNER JOIN, got {join_kw!r}")
    if implicit:
        return f"""{join_kw} {bet_from} AS bg
  ON CAST(bg.player_id AS VARCHAR) = CAST(r.player_id AS VARCHAR)
 AND CAST(bg.payout_complete_dtm AS TIMESTAMP) >= r.run_start_ts
 AND CAST(bg.payout_complete_dtm AS TIMESTAMP) <= r.run_end_ts"""
    if not bm_from:
        raise ValueError("bm_from required when implicit_membership is False")
    return f"""{join_kw} {bm_from} AS bm ON bm.run_id = r.run_id
{join_kw} {bet_from} AS bg
  ON CAST(bg.bet_id AS VARCHAR) = CAST(bm.bet_id AS VARCHAR)"""


def _build_run_feats_sql(
    *,
    rf_from: str,
    bet_from: str,
    sess_from: str,
    implicit_membership: bool,
    bm_from: str = "",
) -> str:
    """Return SQL creating TEMP TABLE run_feats (one row per run_id)."""
    join_block = _bet_membership_join_sql(bet_from=bet_from, implicit=implicit_membership, bm_from=bm_from)
    return f"""
CREATE OR REPLACE TEMP TABLE run_core AS
SELECT
  rf.run_id,
  CAST(rf.canonical_id AS VARCHAR) AS canonical_id,
  MAX(CAST(rf.player_id AS VARCHAR)) AS player_id,
  CAST(rf.run_end_ts AS TIMESTAMP) AS run_end_ts,
  CAST(rf.run_start_ts AS TIMESTAMP) AS run_start_ts,
  SUM(CAST(b.wager AS DOUBLE)) AS run_turnover,
  SUM(-CAST(b.casino_win AS DOUBLE)) AS run_player_win,
  COUNT(*)::BIGINT AS run_bet_count,
  COUNT(DISTINCT CAST(b.table_id AS VARCHAR)) AS run_distinct_tables
FROM {rf_from} AS rf
{join_block}
WHERE CAST(rf.canonical_id AS VARCHAR) IN (SELECT canonical_id FROM evt_cids)
GROUP BY rf.run_id, rf.canonical_id, rf.run_end_ts, rf.run_start_ts;

CREATE OR REPLACE TEMP TABLE run_sess AS
SELECT DISTINCT rf.run_id, CAST(b.session_id AS VARCHAR) AS session_id
FROM {rf_from} AS rf
{join_block}
WHERE CAST(rf.canonical_id AS VARCHAR) IN (SELECT canonical_id FROM evt_cids);

CREATE OR REPLACE TEMP TABLE run_theo_tbl AS
SELECT
  rs.run_id,
  SUM(CAST(s.theo_win AS DOUBLE)) AS run_theo
FROM run_sess rs
INNER JOIN {sess_from} AS s
  ON CAST(s.session_id AS VARCHAR) = rs.session_id
GROUP BY rs.run_id;

CREATE OR REPLACE TEMP TABLE run_feats AS
SELECT
  rc.run_id,
  rc.canonical_id,
  rc.player_id,
  rc.run_end_ts,
  rc.run_start_ts,
  rc.run_turnover,
  rc.run_player_win,
  rc.run_bet_count,
  rc.run_distinct_tables,
  COALESCE(rt.run_theo, 0.0) AS run_theo,
  GREATEST(
    EXTRACT(EPOCH FROM (rc.run_end_ts - rc.run_start_ts)) / 60.0,
    0.0
  ) AS run_duration_min,
  CASE WHEN rc.run_player_win > 0 THEN 1 ELSE 0 END AS run_win_flag
FROM run_core rc
LEFT JOIN run_theo_tbl rt ON rc.run_id = rt.run_id;
"""


def _window_case_sql(col: str, days: int) -> str:
    """CASE expression restricting *col* to window ``[t-{days}d, t)``."""
    return (
        f"CASE WHEN r.run_end_ts >= e.t - INTERVAL '{days}' DAY "
        f"AND r.run_end_ts < e.t THEN {col} ELSE NULL END"
    )


def _sum_case(col: str, days: int) -> str:
    """SUM aggregate for *col* restricted to window."""
    return f"SUM(COALESCE({_window_case_sql(col, days)}, 0))"


def _count_run_case(days: int) -> str:
    """Count runs whose ``run_end_ts`` falls in window."""
    return (
        "SUM(CASE WHEN r.run_end_ts >= e.t - INTERVAL '%d' DAY "
        "AND r.run_end_ts < e.t THEN 1 ELSE 0 END)" % days
    )


def _avg_case(col: str, days: int) -> str:
    """AVG aggregate for window."""
    return f"AVG({_window_case_sql(col, days)})"


def _build_main_agg_sql(*, join_horizon_days: int) -> str:
    """SQL selecting per-bet window aggregates into TEMP TABLE player_win_agg."""
    lines: List[str] = ["CREATE OR REPLACE TEMP TABLE player_win_agg AS", "SELECT", "  e.bet_id"]
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        lines.append(f", {_sum_case('r.run_turnover', d)} AS player_run_turnover_sum_{d}d")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        lines.append(f", {_sum_case('r.run_theo', d)} AS player_run_theo_sum_{d}d")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        lines.append(f", {_sum_case('CAST(r.run_bet_count AS DOUBLE)', d)} AS player_run_num_bets_sum_{d}d")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        lines.append(f", {_sum_case('r.run_player_win', d)} AS player_run_player_win_sum_{d}d")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        lines.append(f", {_count_run_case(d)} AS player_run_count_{d}d")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        lines.append(f", {_avg_case('CAST(r.run_win_flag AS DOUBLE)', d)} AS player_run_winning_run_rate_{d}d")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        lines.append(f", {_avg_case('r.run_duration_min', d)} AS player_run_avg_duration_min_{d}d")
    lines.append(
        f"""
FROM evt e
LEFT JOIN run_feats r
  ON CAST(e.canonical_id AS VARCHAR) = r.canonical_id
 AND r.run_end_ts < e.t
 AND r.run_end_ts >= e.t - INTERVAL '{join_horizon_days}' DAY
GROUP BY e.bet_id;
"""
    )
    return "\n".join(lines)


def _build_days_since_sql(
    *,
    bet_from: str,
    bm_from: str,
    implicit_membership: bool,
    join_horizon_days: int,
    extras_sql: str,
) -> str:
    """active_days + distinct tables + optional pit/gaming extras + days_since."""
    parts: List[str] = ["CREATE OR REPLACE TEMP TABLE player_win_days AS", "SELECT", "  e.bet_id"]
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        parts.append(
            f", COUNT(DISTINCT CASE WHEN r.run_end_ts >= e.t - INTERVAL '{d}' DAY "
            f"AND r.run_end_ts < e.t THEN CAST(bg.gaming_day AS VARCHAR) END) "
            f"AS player_run_active_days_{d}d"
        )
        parts.append(
            f", COUNT(DISTINCT CASE WHEN r.run_end_ts >= e.t - INTERVAL '{d}' DAY "
            f"AND r.run_end_ts < e.t THEN CAST(bg.table_id AS VARCHAR) END) "
            f"AS player_run_distinct_table_cnt_{d}d"
        )
    parts.append(extras_sql)
    parts.append(
        """
,
  EXTRACT(EPOCH FROM (e.t - MAX(CASE WHEN r.run_end_ts < e.t THEN r.run_end_ts END))) / 86400.0
    AS player_run_days_since_last_run,
  EXTRACT(EPOCH FROM (
      e.t - MIN(CASE WHEN r.run_end_ts < e.t THEN r.run_start_ts END)
  )) / 86400.0 AS player_run_days_since_first_run
"""
    )
    run_bg = _join_run_feats_to_bet_bg(
        implicit=implicit_membership,
        bet_from=bet_from,
        bm_from=bm_from,
        join_kw="LEFT JOIN",
    )
    parts.append(
        f"""
FROM evt e
LEFT JOIN run_feats r
  ON CAST(e.canonical_id AS VARCHAR) = r.canonical_id
 AND r.run_end_ts < e.t
 AND r.run_end_ts >= e.t - INTERVAL '{join_horizon_days}' DAY
{run_bg}
GROUP BY e.bet_id;
"""
    )
    return "\n".join(parts)


def _build_top_table_share_sql(*, bet_from: str, bm_from: str, implicit_membership: bool) -> str:
    """TEMP TABLE top_share_w with player_run_top_table_share_*d columns."""
    blocks = []
    for d in (30, 90):
        run_bg = _join_run_feats_to_bet_bg(
            implicit=implicit_membership,
            bet_from=bet_from,
            bm_from=bm_from,
            join_kw="INNER JOIN",
        )
        blocks.append(
            f"""
CREATE OR REPLACE TEMP TABLE top_share_{d} AS
WITH agg AS (
  SELECT
    e.bet_id,
    CAST(bg.table_id AS VARCHAR) AS table_id,
    COUNT(*)::DOUBLE AS c
  FROM evt e
  INNER JOIN run_feats r
    ON CAST(e.canonical_id AS VARCHAR) = r.canonical_id
   AND r.run_end_ts >= e.t - INTERVAL '{d}' DAY
   AND r.run_end_ts < e.t
  {run_bg}
  GROUP BY e.bet_id, table_id
),
tot AS (
  SELECT bet_id, SUM(c) AS tc FROM agg GROUP BY bet_id
)
SELECT
  a.bet_id,
  MAX(a.c) / NULLIF(MAX(t.tc), 0) AS player_run_top_table_share_{d}d
FROM agg a
INNER JOIN tot t USING (bet_id)
GROUP BY a.bet_id;
"""
        )
    blocks.append(
        """
CREATE OR REPLACE TEMP TABLE top_share_w AS
SELECT
  COALESCE(a30.bet_id, a90.bet_id) AS bet_id,
  a30.player_run_top_table_share_30d,
  a90.player_run_top_table_share_90d
FROM top_share_30 a30
FULL OUTER JOIN top_share_90 a90 USING (bet_id);
"""
    )
    return "\n".join(blocks)


def _apply_derived_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Append ratio / RTP features derived from window sums."""
    out = df
    pairs = [(30, 180), (7, 30)]
    for short_d, long_d in pairs:
        t_s = out.get(f"player_run_turnover_sum_{short_d}d")
        t_l = out.get(f"player_run_turnover_sum_{long_d}d")
        nb_s = out.get(f"player_run_num_bets_sum_{short_d}d")
        nb_l = out.get(f"player_run_num_bets_sum_{long_d}d")
        c_s = out.get(f"player_run_count_{short_d}d")
        c_l = out.get(f"player_run_count_{long_d}d")
        pw_s = out.get(f"player_run_player_win_sum_{short_d}d")
        th_s = out.get(f"player_run_theo_sum_{short_d}d")
        if t_s is not None and nb_s is not None:
            out[f"player_run_turnover_per_bet_mean_{short_d}d"] = np.where(
                nb_s > 0, t_s / nb_s, np.nan
            )
        if t_s is not None and t_l is not None:
            out[f"player_run_turnover_{short_d}d_over_{long_d}d"] = np.where(
                t_l > 0, t_s / t_l, np.nan
            )
        if t_s is not None and nb_s is not None and t_l is not None and nb_l is not None:
            m_s = np.where(nb_s > 0, t_s / nb_s, np.nan)
            m_l = np.where(nb_l > 0, t_l / nb_l, np.nan)
            out[f"player_run_turnover_per_bet_{short_d}d_over_{long_d}d"] = np.where(
                m_l > 0, m_s / m_l, np.nan
            )
        if c_s is not None and c_l is not None:
            out[f"player_run_count_{short_d}d_over_{long_d}d"] = np.where(
                c_l > 0, c_s / c_l, np.nan
            )
        if pw_s is not None and t_s is not None:
            out[f"player_run_actual_rtp_{short_d}d"] = np.where(
                t_s > 0, pw_s / t_s, np.nan
            )
        if pw_s is not None and th_s is not None:
            out[f"player_run_actual_vs_theo_ratio_{short_d}d"] = np.where(
                th_s != 0, pw_s / th_s, np.nan
            )
    ad30 = out.get("player_run_active_days_30d")
    ct30 = out.get("player_run_count_30d")
    if ad30 is not None and ct30 is not None:
        out["player_run_active_days_per_session_30d"] = np.where(
            ct30 > 0, ad30 / ct30, np.nan
        )
    wr30 = out.get("player_run_winning_run_rate_30d")
    wr180 = out.get("player_run_winning_run_rate_180d")
    if wr30 is not None:
        out["player_run_win_session_rate_30d"] = wr30
    if wr180 is not None:
        out["player_run_win_session_rate_180d"] = wr180
    return out


def _extras_distinct_sql(bet_cols: Set[str], wanted: Set[str]) -> str:
    """Optional COUNT DISTINCT fragments for pit / gaming area (30d only).

    When bridge bet schema lacks the source column, keep the output column as
    ``NULL`` to preserve feature contract while allowing MVP bridge to run.
    """
    frag = ""
    if "player_run_distinct_pit_cnt_30d" in wanted:
        if "pit_id" in bet_cols:
            frag += (
                ", COUNT(DISTINCT CASE WHEN r.run_end_ts >= e.t - INTERVAL '30' DAY "
                "AND r.run_end_ts < e.t THEN CAST(bg.pit_id AS VARCHAR) END) "
                "AS player_run_distinct_pit_cnt_30d"
            )
        else:
            logger.warning(
                "player_run: bet Parquet missing pit_id; emit NULL for player_run_distinct_pit_cnt_30d"
            )
            frag += ", CAST(NULL AS DOUBLE) AS player_run_distinct_pit_cnt_30d"
    ga_col = None
    if "player_run_distinct_gaming_area_cnt_30d" in wanted:
        for cand in ("gaming_area", "extended_zone"):
            if cand in bet_cols:
                ga_col = cand
                break
        if ga_col is not None:
            frag += (
                f", COUNT(DISTINCT CASE WHEN r.run_end_ts >= e.t - INTERVAL '30' DAY "
                f"AND r.run_end_ts < e.t THEN CAST(bg.{ga_col} AS VARCHAR) END) "
                f"AS player_run_distinct_gaming_area_cnt_30d"
            )
        else:
            logger.warning(
                "player_run: bet Parquet missing gaming_area/extended_zone; emit NULL for "
                "player_run_distinct_gaming_area_cnt_30d"
            )
            frag += ", CAST(NULL AS DOUBLE) AS player_run_distinct_gaming_area_cnt_30d"
    return frag


def attach_player_run_features(
    labeled: pd.DataFrame,
    feature_spec: dict,
    *,
    use_local_parquet: bool,
    feature_cols: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """Attach ``player_run_*`` columns to *labeled* using L1 run primitives."""
    if labeled.empty:
        return labeled.copy()
    missing = {"bet_id", "canonical_id", "payout_complete_dtm"} - set(labeled.columns)
    if missing:
        raise ValueError(f"attach_player_run_features: labeled missing columns {sorted(missing)}")
    if not isinstance(feature_spec, dict):
        raise TypeError("feature_spec must be a dict")
    _require_player_run_enabled(feature_spec)
    if not bool(use_local_parquet):
        raise RuntimeError(
            "player_run_asset materialization requires local Parquet mode (--use-local-parquet). "
            "Run primitives read L1 assets under data/l1_layered/<source_snapshot_id>/."
        )
    sid = read_bridge_source_snapshot_id()
    if not sid:
        raise RuntimeError(
            "trainer_local_parquet_bridge.manifest.json missing source_snapshot_id — "
            "cannot locate L1 run_fact/run_bet_map assets."
        )
    mp = trainer_local_parquet_bridge_manifest_path()
    manifest = load_trainer_local_parquet_bridge_manifest()
    bet_path, sess_path = resolve_local_parquet_bet_session_paths_from_manifest(manifest, manifest_path=mp)
    rf_list, bm_list, bet_path_resolved, implicit = resolve_run_primitive_parquet_paths(
        snapshot_id=sid,
        bet_path=bet_path,
        manifest=manifest,
        manifest_path=mp,
    )

    wanted_list = (
        list(feature_cols)
        if feature_cols is not None
        else get_candidate_feature_ids(feature_spec, "player_run_asset", screening_only=False)
    )
    wanted_set = {str(x) for x in wanted_list if str(x)}
    bet_cols = _parquet_column_names(bet_path_resolved)
    extras_sql = _extras_distinct_sql(bet_cols, wanted_set)

    evt = labeled[["bet_id", "canonical_id", "payout_complete_dtm"]].copy()
    evt["canonical_id"] = evt["canonical_id"].astype(str)
    evt["bet_id"] = evt["bet_id"].astype(str)
    evt["t"] = pd.to_datetime(evt["payout_complete_dtm"], errors="coerce").astype("datetime64[ns]")

    rf_from = _duck_read_parquet(rf_list)
    bm_from = _duck_read_parquet(bm_list) if bm_list else ""
    bet_from = _duck_read_parquet([str(bet_path_resolved)])
    sess_from = _duck_read_parquet([str(sess_path)])

    join_horizon = _MAX_WINDOW_DAYS + _JOIN_LOOKBACK_PAD_DAYS

    con = duckdb.connect(":memory:")
    try:
        con.register("evt", evt)
        con.execute("CREATE TEMP TABLE evt_cids AS SELECT DISTINCT canonical_id FROM evt;")
        con.execute(
            _build_run_feats_sql(
                rf_from=rf_from,
                bet_from=bet_from,
                sess_from=sess_from,
                implicit_membership=implicit,
                bm_from=bm_from,
            )
        )
        con.execute(_build_main_agg_sql(join_horizon_days=join_horizon))
        con.execute(
            _build_days_since_sql(
                bet_from=bet_from,
                bm_from=bm_from,
                implicit_membership=implicit,
                join_horizon_days=join_horizon,
                extras_sql=extras_sql,
            )
        )
        con.execute(
            _build_top_table_share_sql(
                bet_from=bet_from,
                bm_from=bm_from,
                implicit_membership=implicit,
            )
        )
        agg_df = con.execute("SELECT * FROM player_win_agg").df()
        days_df = con.execute("SELECT * FROM player_win_days").df()
        top_df = con.execute("SELECT * FROM top_share_w").df()
    finally:
        con.close()

    merged_df = agg_df.merge(days_df, on="bet_id", how="outer").merge(top_df, on="bet_id", how="left")
    merged_df.drop(columns=[c for c in merged_df.columns if str(c).startswith("_")], inplace=True, errors="ignore")

    out = labeled.copy()
    out["bet_id"] = out["bet_id"].astype(str)
    out = out.merge(merged_df, on="bet_id", how="left")
    out = _apply_derived_ratios(out)

    missing_cols = [c for c in sorted(wanted_set) if c not in out.columns]
    if missing_cols:
        raise RuntimeError(
            "player_run materialization did not produce declared YAML columns: "
            f"{missing_cols[:20]}{' …' if len(missing_cols) > 20 else ''}"
        )

    drop_candidates = [c for c in out.columns if c.startswith("player_run_") and c not in wanted_set]
    if drop_candidates:
        out = out.drop(columns=drop_candidates)
    return out


def player_run_asset_requested(spec: Optional[dict]) -> bool:
    """Return True when YAML enables ``player_run_asset``."""
    if not isinstance(spec, dict):
        return False
    return _track_section_enabled_in_spec(spec, "player_run_asset")


def ensure_player_run_layer_assets_ready() -> None:
    """Verify bridge manifest + ``run_fact`` Parquet exist (Hive L1 or MVP ``snap_root``).

    ``run_bet_map`` is optional; when absent, training uses implicit bet↔run membership.

    Raises
    ------
    RuntimeError
        When snapshot id is missing or run_fact assets cannot be resolved.
    """
    sid = read_bridge_source_snapshot_id()
    if not sid:
        raise RuntimeError(
            "player_run_asset requires trainer_local_parquet_bridge.manifest.json with "
            "non-empty source_snapshot_id."
        )
    mp = trainer_local_parquet_bridge_manifest_path()
    manifest = load_trainer_local_parquet_bridge_manifest()
    bet_path, _sess = resolve_local_parquet_bet_session_paths_from_manifest(manifest, manifest_path=mp)
    resolve_run_primitive_parquet_paths(
        snapshot_id=sid,
        bet_path=bet_path,
        manifest=manifest,
        manifest_path=mp,
    )
