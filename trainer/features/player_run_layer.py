"""Player-layer features from L1 run primitives (PIT-safe, run_end_ts < bet_time).

Aggregates closed runs per patron using ``run_fact`` (+ optional ``run_bet_map``)
and bridge bet/session Parquet. When ``run_bet_map`` is absent (e.g.
``parallel_lda_mvp`` snapshot layout), bets are mapped to runs by
``player_id`` (bridge bet has no ``canonical_id``) + ``payout_complete_dtm``
within ``[run_start_ts, run_end_ts]`` (same bets as a Hive ``run_bet_map`` when
staging is consistent).

**Trial (Section A)** — frozen ids in ``SECTION_A_PLAYER_RUN_FEATURE_IDS``: features
materialise on **month-start snapshots** (`snapshot_ts` at 00:00 on the first
calendar day of each relevant month per patron). Rolling windows anchor at that
snapshot time (runs with ``run_end_ts < snapshot_ts``, window
``[snapshot_ts − W days, snapshot_ts)``); each row is merged back onto bets via
backward ``merge_asof`` so ``snapshot_ts <= bet_event_time``. When the YAML asks
for any feature **outside** Section A the legacy **per-bet** path stays
enabled.

Window semantics **per evaluation time** ``t`` (bet ``payout_complete_dtm``, or the
above ``snapshot_ts``):

- **PIT**: only runs with ``run_end_ts < t`` (strict).
- **Rolling window** ``[t - W days, t)``: ``run_end_ts >= t - W days AND run_end_ts < t``.
- **active_days**: ``COUNT(DISTINCT gaming_day)`` over bets belonging to qualifying runs.

Raises ``RuntimeError`` when local layered assets or required columns are missing.
"""

from __future__ import annotations

import logging
from time import perf_counter
from pathlib import Path
from typing import Any, List, Optional, Sequence, Set, Tuple

import duckdb
import numpy as np
import pandas as pd

from trainer.core._duckdb_runtime import apply_duckdb_runtime, resolve_duckdb_runtime_policy
from trainer.features.features import _track_section_enabled_in_spec, get_candidate_feature_ids
from trainer.training.data_sources import LOCAL_PARQUET_DIR, PROJECT_ROOT, load_trainer_local_parquet_bridge_manifest
from trainer.training.data_sources import resolve_local_parquet_bet_session_paths_from_manifest
from trainer.training.data_sources import trainer_local_parquet_bridge_manifest_path
from trainer.training.l2_bundle_materialize import read_bridge_source_snapshot_id

logger = logging.getLogger(__name__)

_PLAYER_RUN_WINDOWS_DAYS: Tuple[int, ...] = (7, 30, 90, 180, 365)
_MAX_WINDOW_DAYS = max(_PLAYER_RUN_WINDOWS_DAYS)
_JOIN_LOOKBACK_PAD_DAYS = 40
_FETCH_VECTORS_PER_CHUNK = 64

SECTION_A_PLAYER_RUN_FEATURE_IDS: Tuple[str, ...] = (
    "player_run_days_since_last_run",
    "player_run_days_since_first_run",
    "player_run_count_7d",
    "player_run_count_30d",
    "player_run_count_90d",
    "player_run_count_180d",
    "player_run_count_365d",
    "player_run_active_days_30d",
    "player_run_active_days_90d",
    "player_run_active_days_365d",
    "player_run_wager_sum_7d",
    "player_run_wager_sum_30d",
    "player_run_wager_sum_90d",
    "player_run_wager_sum_180d",
    "player_run_wager_sum_365d",
)
_SECTION_A_FROZEN: Set[str] = set(SECTION_A_PLAYER_RUN_FEATURE_IDS)


def _monthly_snapshot_mode_requested(wanted: Set[str]) -> bool:
    """Return True when every requested ``player_run_*`` id belongs to frozen Section A."""
    return bool(wanted) and wanted <= _SECTION_A_FROZEN


def _build_monthly_snap_event_table(evt: pd.DataFrame) -> pd.DataFrame:
    """Build synthetic eval rows: each (canonical_id, month-begin snapshot_ts) in labeled span."""
    req = {"canonical_id", "t"}
    missing = req - set(evt.columns)
    if missing:
        raise ValueError("_build_monthly_snap_event_table: evt missing columns " + repr(sorted(missing)))
    if evt.empty:
        return pd.DataFrame(columns=["bet_id", "canonical_id", "payout_complete_dtm", "t", "snapshot_ts"])

    snap_rows: List[Tuple[str, pd.Timestamp]] = []
    canon_sub = evt[["canonical_id", "t"]].dropna(subset=["t"]).copy()
    canon_sub["canonical_id"] = canon_sub["canonical_id"].astype(str)
    for cid, grp in canon_sub.groupby("canonical_id", sort=False):
        ser = grp["t"]
        if ser.empty:
            continue
        mn = ser.dt.to_period("M").min()
        mx = ser.dt.to_period("M").max()
        if pd.isna(mn) or pd.isna(mx):
            continue
        lo = pd.Timestamp(mn.to_timestamp())
        hi = pd.Timestamp(mx.to_timestamp())
        for ts in pd.date_range(lo, hi, freq="MS"):
            snap_rows.append((str(cid), pd.Timestamp(ts)))

    if not snap_rows:
        return pd.DataFrame(columns=["bet_id", "canonical_id", "payout_complete_dtm", "t", "snapshot_ts"])

    uniq = pd.DataFrame(snap_rows, columns=["canonical_id", "snapshot_ts"]).drop_duplicates()
    uniq["canonical_id"] = uniq["canonical_id"].astype(str)
    uniq["bet_id"] = np.arange(len(uniq), dtype=np.int64).astype(str)
    uniq["t"] = uniq["snapshot_ts"].astype("datetime64[ns]")
    uniq["payout_complete_dtm"] = uniq["snapshot_ts"]
    return uniq[["bet_id", "canonical_id", "payout_complete_dtm", "t", "snapshot_ts"]]


def _merge_snapshot_features_onto_labeled(labeled_evt: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    """Backward ``merge_asof`` on ``canonical_id`` so ``snapshot_ts <= bet_event_time``."""
    feat_cols = [c for c in feats.columns if c.startswith("player_run_")]
    if not feat_cols:
        return labeled_evt[["bet_id"]].copy()

    slim = feats[["canonical_id", "snapshot_ts"] + feat_cols].copy()
    slim["canonical_id_str"] = slim["canonical_id"].astype(str)
    slim["snapshot_ts"] = pd.to_datetime(slim["snapshot_ts"], errors="coerce").astype("datetime64[ns]")

    lab_s = labeled_evt[["bet_id", "canonical_id", "t"]].copy()
    lab_s["canonical_id_str"] = lab_s["canonical_id"].astype(str)
    lab_s["bet_t"] = pd.to_datetime(lab_s["t"], errors="coerce").astype("datetime64[ns]")

    if lab_s.empty:
        out_empty = labeled_evt[["bet_id"]].copy()
        for c in feat_cols:
            out_empty[c] = np.nan
        return out_empty

    right_cols = ["snapshot_ts"] + feat_cols
    merged_parts: List[pd.DataFrame] = []
    for cid, left_g in lab_s.groupby("canonical_id_str", sort=False):
        left_g = left_g.sort_values("bet_t", kind="mergesort")
        rg = slim.loc[slim["canonical_id_str"] == cid, right_cols].sort_values(
            "snapshot_ts", kind="mergesort"
        )
        if rg.empty:
            mg = left_g.copy()
            mg["snapshot_ts"] = pd.NaT
            for c in feat_cols:
                mg[c] = np.nan
            merged_parts.append(mg)
            continue
        mg = pd.merge_asof(
            left_g,
            rg,
            left_on="bet_t",
            right_on="snapshot_ts",
            direction="backward",
            allow_exact_matches=True,
        )
        merged_parts.append(mg)

    merged = pd.concat(merged_parts, ignore_index=True)
    ok = merged["snapshot_ts"].notna() & merged["bet_t"].notna()
    if ok.any() and ((merged.loc[ok, "snapshot_ts"] > merged.loc[ok, "bet_t"]).any()):
        raise RuntimeError("monthly_snapshot PIT violation: snapshot_ts > bet_event_time")

    return labeled_evt[["bet_id"]].merge(merged[["bet_id"] + feat_cols], on="bet_id", how="left")


def _main_agg_output_columns() -> Set[str]:
    """Columns produced by ``player_win_agg`` (all definable window aggregates)."""
    out: Set[str] = set()
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        out.add(f"player_run_wager_sum_{d}d")
        out.add(f"player_run_theo_sum_{d}d")
        out.add(f"player_run_num_bets_sum_{d}d")
        out.add(f"player_run_player_win_sum_{d}d")
        out.add(f"player_run_count_{d}d")
        out.add(f"player_run_winning_run_rate_{d}d")
        out.add(f"player_run_avg_duration_min_{d}d")
    return out


def _days_output_columns() -> Set[str]:
    """Columns produced by ``player_win_days``."""
    out: Set[str] = {
        "player_run_distinct_pit_cnt_30d",
        "player_run_distinct_gaming_area_cnt_30d",
        "player_run_days_since_last_run",
        "player_run_days_since_first_run",
    }
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        out.add(f"player_run_active_days_{d}d")
        out.add(f"player_run_distinct_table_cnt_{d}d")
    return out


_TOP_SHARE_OUTPUT_COLUMNS: Set[str] = {"player_run_top_table_share_30d", "player_run_top_table_share_90d"}
_MAIN_AGG_OUTPUT_COLUMNS: Set[str] = _main_agg_output_columns()
_DAYS_OUTPUT_COLUMNS: Set[str] = _days_output_columns()


def _derived_dependency_columns(wanted: Set[str]) -> Set[str]:
    """Return base columns required to compute requested derived ratios."""
    need: Set[str] = set()
    for short_d, long_d in ((30, 180), (7, 30)):
        if f"player_run_wager_per_bet_mean_{short_d}d" in wanted:
            need |= {f"player_run_wager_sum_{short_d}d", f"player_run_num_bets_sum_{short_d}d"}
        if f"player_run_wager_{short_d}d_over_{long_d}d" in wanted:
            need |= {f"player_run_wager_sum_{short_d}d", f"player_run_wager_sum_{long_d}d"}
        if f"player_run_wager_per_bet_{short_d}d_over_{long_d}d" in wanted:
            need |= {
                f"player_run_wager_sum_{short_d}d",
                f"player_run_num_bets_sum_{short_d}d",
                f"player_run_wager_sum_{long_d}d",
                f"player_run_num_bets_sum_{long_d}d",
            }
        if f"player_run_count_{short_d}d_over_{long_d}d" in wanted:
            need |= {f"player_run_count_{short_d}d", f"player_run_count_{long_d}d"}
        if f"player_run_actual_rtp_{short_d}d" in wanted:
            need |= {f"player_run_player_win_sum_{short_d}d", f"player_run_wager_sum_{short_d}d"}
        if f"player_run_actual_vs_theo_ratio_{short_d}d" in wanted:
            need |= {f"player_run_player_win_sum_{short_d}d", f"player_run_theo_sum_{short_d}d"}
    if "player_run_active_days_per_session_30d" in wanted:
        need |= {"player_run_active_days_30d", "player_run_count_30d"}
    if "player_run_win_session_rate_30d" in wanted:
        need.add("player_run_winning_run_rate_30d")
    if "player_run_win_session_rate_180d" in wanted:
        need.add("player_run_winning_run_rate_180d")
    return need


def _materialized_column_set(wanted: Set[str]) -> Set[str]:
    """Return columns to fetch from DuckDB before pandas derived features."""
    all_known = _MAIN_AGG_OUTPUT_COLUMNS | _DAYS_OUTPUT_COLUMNS | _TOP_SHARE_OUTPUT_COLUMNS
    return (wanted & all_known) | (_derived_dependency_columns(wanted) & all_known)


def _build_final_projection_sql(
    *,
    materialized_cols: Set[str],
    include_main: bool,
    include_days: bool,
    include_top: bool,
) -> str:
    """Build final SELECT for only required columns."""
    if not (include_main or include_days or include_top):
        raise RuntimeError("No player_run source tables selected for final projection.")

    ordered_cols = sorted(materialized_cols)

    if include_main and include_days:
        key_expr = "COALESCE(a.bet_id, d.bet_id)"
        from_sql = "FROM player_win_agg a\nFULL OUTER JOIN player_win_days d ON a.bet_id = d.bet_id"
    elif include_main:
        key_expr = "a.bet_id"
        from_sql = "FROM player_win_agg a"
    else:
        key_expr = "d.bet_id"
        from_sql = "FROM player_win_days d"

    if include_top:
        from_sql += f"\nLEFT JOIN top_share_w t ON {key_expr} = t.bet_id"

    select_lines: List[str] = [f"SELECT {key_expr} AS bet_id"]
    for col in ordered_cols:
        if col in _TOP_SHARE_OUTPUT_COLUMNS:
            if include_top:
                select_lines.append(f", t.{col} AS {col}")
            continue
        if col in _MAIN_AGG_OUTPUT_COLUMNS and include_main:
            select_lines.append(f", a.{col} AS {col}")
            continue
        if col in _DAYS_OUTPUT_COLUMNS and include_days:
            select_lines.append(f", d.{col} AS {col}")
            continue
    return "\n".join(select_lines + [from_sql])


class _NoopProgressBar:
    """No-op progress bar fallback when tqdm is unavailable."""

    def set_description(self, desc: str, refresh: bool = False) -> None:
        """Keep tqdm-like API without side effects."""
        _ = (desc, refresh)

    def update(self, n: int = 1) -> None:
        """Keep tqdm-like API without side effects."""
        _ = n

    def close(self) -> None:
        """Keep tqdm-like API without side effects."""
        return None


def _new_progress_bar(total: int, desc: str) -> Any:
    """Return a tqdm progress bar or a no-op fallback."""
    try:
        from tqdm import tqdm

        return tqdm(total=total, desc=desc, unit="stage", leave=False)
    except Exception:
        return _NoopProgressBar()


def _available_memory_bytes() -> Optional[int]:
    """Best-effort available RAM in bytes for DuckDB runtime policy."""
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        return None


def _run_sql_stage(con: duckdb.DuckDBPyConnection, stage_name: str, sql: str) -> None:
    """Execute SQL stage and log elapsed seconds."""
    t0 = perf_counter()
    con.execute(sql)
    logger.info("player_run: %-22s done in %.1fs", stage_name, perf_counter() - t0)


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
  SUM(CAST(b.wager AS DOUBLE)) AS run_wager_sum,
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
  rc.run_wager_sum,
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


def _build_main_agg_sql(*, join_horizon_days: int, materialized_cols: Set[str]) -> str:
    """SQL selecting window aggregates keyed by synthetic ``evt.bet_id`` into ``player_win_agg``."""
    lines: List[str] = ["CREATE OR REPLACE TEMP TABLE player_win_agg AS", "SELECT", "  e.bet_id"]
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        nm = f"player_run_wager_sum_{d}d"
        if nm in materialized_cols:
            lines.append(f", {_sum_case('r.run_wager_sum', d)} AS {nm}")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        nm = f"player_run_theo_sum_{d}d"
        if nm in materialized_cols:
            lines.append(f", {_sum_case('r.run_theo', d)} AS {nm}")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        nm = f"player_run_num_bets_sum_{d}d"
        if nm in materialized_cols:
            lines.append(f", {_sum_case('CAST(r.run_bet_count AS DOUBLE)', d)} AS {nm}")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        nm = f"player_run_player_win_sum_{d}d"
        if nm in materialized_cols:
            lines.append(f", {_sum_case('r.run_player_win', d)} AS {nm}")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        nm = f"player_run_count_{d}d"
        if nm in materialized_cols:
            lines.append(f", {_count_run_case(d)} AS {nm}")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        nm = f"player_run_winning_run_rate_{d}d"
        if nm in materialized_cols:
            lines.append(f", {_avg_case('CAST(r.run_win_flag AS DOUBLE)', d)} AS {nm}")
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        nm = f"player_run_avg_duration_min_{d}d"
        if nm in materialized_cols:
            lines.append(f", {_avg_case('r.run_duration_min', d)} AS {nm}")
    if len(lines) <= 3:
        raise RuntimeError("player_win_agg: no main_agg columns matched materialized_cols")
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
    materialized_cols: Set[str],
) -> str:
    """active_days / distinct_tables / extras / days_since into ``player_win_days`` keyed by ``bet_id``."""
    parts: List[str] = ["CREATE OR REPLACE TEMP TABLE player_win_days AS", "SELECT", "  e.bet_id"]
    for d in _PLAYER_RUN_WINDOWS_DAYS:
        ad_col = f"player_run_active_days_{d}d"
        tb_col = f"player_run_distinct_table_cnt_{d}d"
        if ad_col in materialized_cols:
            parts.append(
                f", COUNT(DISTINCT CASE WHEN r.run_end_ts >= e.t - INTERVAL '{d}' DAY "
                f"AND r.run_end_ts < e.t THEN CAST(bg.gaming_day AS VARCHAR) END) AS {ad_col}"
            )
        if tb_col in materialized_cols:
            parts.append(
                f", COUNT(DISTINCT CASE WHEN r.run_end_ts >= e.t - INTERVAL '{d}' DAY "
                f"AND r.run_end_ts < e.t THEN CAST(bg.table_id AS VARCHAR) END) AS {tb_col}"
            )
    if extras_sql:
        parts.append(extras_sql)
    time_parts: List[str] = []
    if "player_run_days_since_last_run" in materialized_cols:
        time_parts.append(
            "EXTRACT(EPOCH FROM (ANY_VALUE(e.t) - MAX(CASE WHEN r.run_end_ts < e.t THEN "
            "r.run_end_ts END))) / 86400.0 AS player_run_days_since_last_run"
        )
    if "player_run_days_since_first_run" in materialized_cols:
        time_parts.append(
            "EXTRACT(EPOCH FROM (ANY_VALUE(e.t) - MIN(CASE WHEN r.run_end_ts < e.t THEN "
            "r.run_start_ts END))) / 86400.0 AS player_run_days_since_first_run"
        )
    if time_parts:
        parts.append(", " + ", ".join(time_parts))
    if len(parts) <= 3 and not extras_sql and not time_parts:
        raise RuntimeError("player_win_days: empty SELECT — no materialized day columns matched")
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
        t_s = out.get(f"player_run_wager_sum_{short_d}d")
        t_l = out.get(f"player_run_wager_sum_{long_d}d")
        nb_s = out.get(f"player_run_num_bets_sum_{short_d}d")
        nb_l = out.get(f"player_run_num_bets_sum_{long_d}d")
        c_s = out.get(f"player_run_count_{short_d}d")
        c_l = out.get(f"player_run_count_{long_d}d")
        pw_s = out.get(f"player_run_player_win_sum_{short_d}d")
        th_s = out.get(f"player_run_theo_sum_{short_d}d")
        if t_s is not None and nb_s is not None:
            out[f"player_run_wager_per_bet_mean_{short_d}d"] = np.where(
                nb_s > 0, t_s / nb_s, np.nan
            )
        if t_s is not None and t_l is not None:
            out[f"player_run_wager_{short_d}d_over_{long_d}d"] = np.where(
                t_l > 0, t_s / t_l, np.nan
            )
        if t_s is not None and nb_s is not None and t_l is not None and nb_l is not None:
            m_s = np.where(nb_s > 0, t_s / nb_s, np.nan)
            m_l = np.where(nb_l > 0, t_l / nb_l, np.nan)
            out[f"player_run_wager_per_bet_{short_d}d_over_{long_d}d"] = np.where(
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
    materialized_cols = _materialized_column_set(wanted_set)
    include_main = bool(materialized_cols & _MAIN_AGG_OUTPUT_COLUMNS)
    include_days = bool(materialized_cols & _DAYS_OUTPUT_COLUMNS)
    include_top = bool(materialized_cols & _TOP_SHARE_OUTPUT_COLUMNS)
    monthly_snap = _monthly_snapshot_mode_requested(wanted_set)
    if monthly_snap:
        logger.info(
            "player_run: monthly snapshot mode (Section A): %d feature_ids",
            len(wanted_set),
        )

    bet_cols = _parquet_column_names(bet_path_resolved)
    extras_sql = _extras_distinct_sql(bet_cols, wanted_set)

    labeled_evt = labeled[["bet_id", "canonical_id", "payout_complete_dtm"]].copy()
    labeled_evt["canonical_id"] = labeled_evt["canonical_id"].astype(str)
    labeled_evt["bet_id"] = labeled_evt["bet_id"].astype(str)
    labeled_evt["t"] = pd.to_datetime(labeled_evt["payout_complete_dtm"], errors="coerce").astype(
        "datetime64[ns]"
    )

    snap_grid: Optional[pd.DataFrame] = None
    if monthly_snap:
        snap_grid = _build_monthly_snap_event_table(labeled_evt)
        duck_evt = snap_grid[["bet_id", "canonical_id", "payout_complete_dtm", "t"]]
    else:
        duck_evt = labeled_evt[["bet_id", "canonical_id", "payout_complete_dtm", "t"]]

    rf_from = _duck_read_parquet(rf_list)
    bm_from = _duck_read_parquet(bm_list) if bm_list else ""
    bet_from = _duck_read_parquet([str(bet_path_resolved)])
    sess_from = _duck_read_parquet([str(sess_path)])

    join_horizon = _MAX_WINDOW_DAYS + _JOIN_LOOKBACK_PAD_DAYS

    con = duckdb.connect(":memory:")
    progress = _new_progress_bar(total=9, desc="Step 6/11 player_run")
    try:
        runtime_policy = resolve_duckdb_runtime_policy(
            "profile",
            _available_memory_bytes(),
            input_bytes=int(duck_evt.memory_usage(deep=True).sum()),
        )
        apply_duckdb_runtime(con, runtime_policy)
        logger.info(
            "player_run: DuckDB runtime memory_limit=%.2fGB threads=%d temp_directory=%s",
            float(runtime_policy["memory_limit_bytes"]) / 1024**3,
            int(runtime_policy["threads"]),
            str(runtime_policy["temp_directory"]),
        )
        progress.set_description("Step 6/11 player_run: register_evt")
        t_reg = perf_counter()
        con.register("evt", duck_evt)
        logger.info("player_run: %-22s done in %.1fs", "register_evt", perf_counter() - t_reg)
        progress.update(1)

        progress.set_description("Step 6/11 player_run: evt_cids")
        _run_sql_stage(con, "evt_cids", "CREATE TEMP TABLE evt_cids AS SELECT DISTINCT canonical_id FROM evt;")
        progress.update(1)

        progress.set_description("Step 6/11 player_run: run_feats")
        _run_sql_stage(
            con,
            "run_feats",
            _build_run_feats_sql(
                rf_from=rf_from,
                bet_from=bet_from,
                sess_from=sess_from,
                implicit_membership=implicit,
                bm_from=bm_from,
            ),
        )
        progress.update(1)

        if include_main:
            progress.set_description("Step 6/11 player_run: main_agg")
            _run_sql_stage(
                con,
                "main_agg",
                _build_main_agg_sql(join_horizon_days=join_horizon, materialized_cols=materialized_cols),
            )
        else:
            logger.info("player_run: skip main_agg (not required by wanted_set)")
        progress.update(1)

        if include_days:
            progress.set_description("Step 6/11 player_run: days_since")
            _run_sql_stage(
                con,
                "days_since",
                _build_days_since_sql(
                    bet_from=bet_from,
                    bm_from=bm_from,
                    implicit_membership=implicit,
                    join_horizon_days=join_horizon,
                    extras_sql=extras_sql,
                    materialized_cols=materialized_cols,
                ),
            )
        else:
            logger.info("player_run: skip days_since (not required by wanted_set)")
        progress.update(1)

        if include_top:
            progress.set_description("Step 6/11 player_run: top_share")
            _run_sql_stage(
                con,
                "top_share",
                _build_top_table_share_sql(
                    bet_from=bet_from,
                    bm_from=bm_from,
                    implicit_membership=implicit,
                ),
            )
        else:
            logger.info("player_run: skip top_share (not required by wanted_set)")
        progress.update(1)

        final_sql = _build_final_projection_sql(
            materialized_cols=materialized_cols,
            include_main=include_main,
            include_days=include_days,
            include_top=include_top,
        )
        progress.set_description("Step 6/11 player_run: projection_sql")
        _run_sql_stage(con, "projection_sql", f"CREATE OR REPLACE TEMP TABLE player_run_all AS\n{final_sql};")
        progress.update(1)

        progress.set_description("Step 6/11 player_run: fetch_chunked")
        t_fetch = perf_counter()
        con.execute("SELECT * FROM player_run_all")
        chunks: List[pd.DataFrame] = []
        fetched_rows = 0
        while True:
            chunk_df = con.fetch_df_chunk(vectors_per_chunk=_FETCH_VECTORS_PER_CHUNK)
            if chunk_df.empty:
                break
            chunks.append(chunk_df)
            fetched_rows += len(chunk_df)
            logger.info("player_run: fetched chunk rows=%d total=%d", len(chunk_df), fetched_rows)
        merged_df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=["bet_id"])
        logger.info("player_run: %-22s done in %.1fs rows=%d", "fetch_chunked", perf_counter() - t_fetch, len(merged_df))
        progress.update(1)
    finally:
        progress.close()
        con.close()

    merged_df.drop(columns=[c for c in merged_df.columns if str(c).startswith("_")], inplace=True, errors="ignore")

    join_frame = merged_df
    if monthly_snap and snap_grid is not None:
        wide = merged_df.merge(
            snap_grid[["bet_id", "canonical_id", "snapshot_ts"]],
            on="bet_id",
            how="left",
        )
        join_frame = _merge_snapshot_features_onto_labeled(labeled_evt, wide)

    out = labeled
    if str(out["bet_id"].dtype) != "object":
        out = out.copy()
    out["bet_id"] = out["bet_id"].astype(str, copy=False)
    out = out.merge(join_frame, on="bet_id", how="left")
    progress = _new_progress_bar(total=1, desc="Step 6/11 player_run: ratios")
    progress.update(0)
    out = _apply_derived_ratios(out)
    progress.update(1)
    progress.close()

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
