"""High-tier ClickHouse scorer: incremental bets, baseline features, ``state.db`` alerts."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from trainer_hightier.bet_contract import assert_bets_gaming_day_contract

from trainer_hightier.config import HightierServingConfig, default_hightier_serving_config
from trainer_hightier.serving.adt_allowlist import (
    check_training_allowlist_sha256,
    load_adt_allowlist_ids,
    resolve_adt_allowlist_path,
    sha256_file,
)
from trainer_hightier.serving.ch_adapter import get_clickhouse_client
from trainer_hightier.serving.contracts import (
    META_KEY_ACTIVE_ADT_ALLOWLIST_SHA256,
    META_KEY_ACTIVE_ADT_ALLOWLIST_VERSION,
    META_KEY_ACTIVE_SNAPSHOT_VERSION,
    META_KEY_ADT_ALLOWLIST_HEALTH,
)
from trainer_hightier.serving.feature_builder import (
    assert_features_ready,
    attach_canonical_id,
    attach_synthetic_etl_and_prediction_visible,
    attach_trial_bet_behavior_1h,
    coerce_categoricals,
    join_fe_derived_snapshot,
    join_slow_patron_snapshot,
)
from trainer_hightier.serving.feature_state_store import ActiveSnapshotManifest, read_active_manifest
from trainer_hightier.serving.model_bundle import HightierModelBundle, load_hightier_model_bundle
from trainer_hightier.serving.prediction_log import append_hightier_prediction_log, init_prediction_log_db
from trainer_hightier.serving.runtime_config import HK_TZ
from trainer_hightier.serving.state_db import (
    append_alerts,
    apply_sqlite_serving_pragmas,
    connect_state_db,
    get_last_processed_etl_insert,
    init_state_db,
    meta_get,
    meta_set,
    set_last_processed_etl_insert,
)

logger = logging.getLogger(__name__)
UTC_TZ = timezone.utc


def _sync_allowlist_cache(
    al_cache: dict[str, Any],
    *,
    cfg: HightierServingConfig,
    manifest: ActiveSnapshotManifest | None,
    bundle: HightierModelBundle,
    cli_allowlist: Path | None,
    high_adt_only: bool,
) -> None:
    """Refresh ``al_cache`` keys: ``path``, ``sha256``, ``ids``, ``allow_key``."""
    if not high_adt_only:
        al_cache.clear()
        al_cache["high_adt_only"] = False
        return
    path = resolve_adt_allowlist_path(cfg, manifest=manifest, cli_path=cli_allowlist)
    if not path.is_file():
        raise FileNotFoundError(f"adt allowlist parquet missing: {path}")
    st = path.stat()
    key = (str(path.resolve()), st.st_mtime_ns)
    if al_cache.get("allow_key") == key and "ids" in al_cache:
        return
    sha = sha256_file(path)
    hash_ok = check_training_allowlist_sha256(
        bundle.training_metrics,
        sha,
        fail_fast=bool(cfg.adt_allowlist_fail_on_training_hash_mismatch),
    )
    al_cache["allow_key"] = key
    al_cache["path"] = path
    al_cache["sha256"] = sha
    al_cache["hash_ok"] = hash_ok
    al_cache["ids"] = load_adt_allowlist_ids(path)
    al_cache["high_adt_only"] = True


def _log_scorer_boot_line(
    *,
    bundle: HightierModelBundle,
    high_adt_only: bool,
    al_path: Path | None,
    al_sha: str | None,
    man_ver: str | None,
    man_al_ver: str | None,
) -> None:
    logger.info(
        "[hightier_scorer] boot high_adt_only=%s model_version=%s allowlist_path=%s allowlist_sha=%s "
        "manifest_version=%s manifest_adt_allowlist_version=%s",
        high_adt_only,
        bundle.model_version,
        al_path,
        (al_sha[:16] + "…") if al_sha else None,
        man_ver,
        man_al_ver,
    )


def _warehouse_ts_series_to_hk(series: pd.Series) -> pd.Series:
    s = pd.to_datetime(series, errors="coerce")
    if getattr(s.dt, "tz", None) is None:
        s = s.dt.tz_localize(UTC_TZ, ambiguous="NaT", nonexistent="shift_forward")
    return s.dt.tz_convert(ZoneInfo(HK_TZ))


def _effective_etl_cursor(bets: pd.DataFrame) -> pd.Series:
    if "__etl_insert_Dtm" in bets.columns:
        cur = _warehouse_ts_series_to_hk(bets["__etl_insert_Dtm"])
    else:
        cur = pd.Series(pd.NaT, index=bets.index)
    if "payout_complete_dtm" in bets.columns:
        pay = _warehouse_ts_series_to_hk(bets["payout_complete_dtm"])
        cur = cur.fillna(pay)
    return cur


#: ``t_bet`` has no loyalty id; keep a nullable column for downstream schema / API parity.
_TBET_CASINO_PLAYER_ID_SELECT = "CAST(NULL AS Nullable(String)) AS casino_player_id"
_TBET_WAGER_SELECT = "CAST(wager AS Float64) AS wager"
_TBET_CASINO_WIN_SELECT = "CAST(casino_win AS Float64) AS casino_win"


def _incremental_params_and_etl_filter(
    cfg: HightierServingConfig,
    last_etl: Optional[pd.Timestamp],
    *,
    lookback_hours: float,
    limit_rows: int,
) -> tuple[dict[str, Any], str]:
    """Shared ClickHouse parameter dict and ``last_etl`` SQL fragment for incremental paths."""
    now_hk = datetime.now(ZoneInfo(HK_TZ))
    end = now_hk
    bet_avail = end - timedelta(minutes=int(cfg.bet_avail_delay_min))
    start = end - timedelta(hours=float(lookback_hours))
    params: dict[str, Any] = {
        "start": start,
        "bet_avail": bet_avail,
        "lim": int(max(1, limit_rows)),
    }
    etl_filter = ""
    if last_etl is not None:
        etl_filter = "AND __etl_insert_Dtm > %(last_etl)s"
        le = pd.Timestamp(last_etl)
        if le.tzinfo is None:
            le = le.tz_localize(UTC_TZ)
        params["last_etl"] = le.to_pydatetime()
    return params, etl_filter


def split_allowlist_player_id_chunks(ids: frozenset[int], chunk_size: int) -> list[list[int]]:
    """Stable sorted chunks of ``player_id`` for bounded ``IN`` lists."""
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size!r}")
    if not ids:
        return []
    sorted_ids = sorted(int(x) for x in ids)
    return [sorted_ids[i : i + chunk_size] for i in range(0, len(sorted_ids), chunk_size)]


def merge_incremental_chunk_frames(frames: list[pd.DataFrame], limit_rows: int) -> pd.DataFrame:
    """Dedupe by ``bet_id``, globally sort by ETL cursor, take first ``limit_rows`` rows."""
    nonempty = [f for f in frames if f is not None and not f.empty]
    if not nonempty:
        return pd.DataFrame()
    merged = pd.concat(nonempty, ignore_index=True)
    merged = merged.drop_duplicates(subset=["bet_id"], keep="first")
    merged["_s_etl"] = pd.to_datetime(merged["__etl_insert_Dtm"], errors="coerce")
    merged["_s_bid"] = pd.to_numeric(merged["bet_id"], errors="coerce").fillna(-1).astype("int64")
    merged = merged.sort_values(by=["_s_etl", "_s_bid"], ascending=[True, True], na_position="last")
    merged = merged.drop(columns=["_s_etl", "_s_bid"])
    return merged.head(int(max(1, limit_rows))).reset_index(drop=True)


def _postprocess_incremental_bets_timestamps(bets: pd.DataFrame) -> None:
    """Normalize warehouse timestamps to HK (in-place)."""
    if not bets.empty and "payout_complete_dtm" in bets.columns:
        _pc = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce")
        if getattr(_pc.dt, "tz", None) is None:
            _pc = _pc.dt.tz_localize(UTC_TZ, ambiguous="NaT", nonexistent="shift_forward")
        bets["payout_complete_dtm"] = _pc.dt.tz_convert(ZoneInfo(HK_TZ))
    if not bets.empty and "__etl_insert_Dtm" in bets.columns:
        _etl = pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce")
        if getattr(_etl.dt, "tz", None) is None:
            _etl = _etl.dt.tz_localize(UTC_TZ, ambiguous="NaT", nonexistent="shift_forward")
        bets["__etl_insert_Dtm"] = _etl.dt.tz_convert(ZoneInfo(HK_TZ))


def fetch_bets_incremental_etl_probe(
    last_etl: Optional[pd.Timestamp],
    *,
    lookback_hours: float,
    limit_rows: int,
) -> pd.DataFrame:
    """Lightweight global top-``K`` probe (no ``player_id`` filter) for watermark parity with allowlist mode."""
    cfg = default_hightier_serving_config()
    client = get_clickhouse_client()
    params, etl_filter = _incremental_params_and_etl_filter(
        cfg, last_etl, lookback_hours=lookback_hours, limit_rows=limit_rows
    )
    placeholder = int(cfg.placeholder_player_id)
    q = f"""
        SELECT
            bet_id,
            __etl_insert_Dtm,
            payout_complete_dtm
        FROM {cfg.source_db}.{cfg.tbet} FINAL
        WHERE payout_complete_dtm >= %(start)s
          AND payout_complete_dtm <= %(bet_avail)s
          AND payout_complete_dtm IS NOT NULL
          AND gaming_day IS NOT NULL
          AND wager > 0
          AND player_id IS NOT NULL
          AND player_id != {placeholder}
          {etl_filter}
        ORDER BY __etl_insert_Dtm ASC, bet_id ASC
        LIMIT %(lim)s
    """
    probe = client.query_df(q, parameters=params)
    _postprocess_incremental_bets_timestamps(probe)
    return probe


def fetch_bets_incremental(
    last_etl: Optional[pd.Timestamp],
    *,
    lookback_hours: float,
    limit_rows: int,
    allowlist_player_ids: Optional[frozenset[int]] = None,
) -> pd.DataFrame:
    """Fetch new settled bets ordered by arrival (__etl_insert_Dtm).

    When ``allowlist_player_ids`` is ``None``, use a single global query (debug / full population).
    When it is empty, return an empty frame without hitting ClickHouse.
    When non-empty, query per ``player_id`` chunk (short ``IN`` lists), merge, then take top ``limit_rows``.
    """
    cfg = default_hightier_serving_config()
    if allowlist_player_ids is not None and not allowlist_player_ids:
        return pd.DataFrame()

    client = get_clickhouse_client()
    params, etl_filter = _incremental_params_and_etl_filter(
        cfg, last_etl, lookback_hours=lookback_hours, limit_rows=limit_rows
    )
    placeholder = int(cfg.placeholder_player_id)
    lim = int(max(1, limit_rows))
    cid_sel = _TBET_CASINO_PLAYER_ID_SELECT
    cap = int(cfg.hightier_scorer_chunk_merge_row_cap)

    if allowlist_player_ids is None:
        q = f"""
            SELECT
                bet_id,
                is_back_bet,
                bet_type,
                type_of_bet,
                __etl_insert_Dtm,
                payout_complete_dtm,
                gaming_day,
                session_id,
                player_id,
                table_id,
                position_idx,
                {_TBET_WAGER_SELECT},
                {_TBET_CASINO_WIN_SELECT},
                payout_odds,
                status,
                {cid_sel}
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE payout_complete_dtm >= %(start)s
              AND payout_complete_dtm <= %(bet_avail)s
              AND payout_complete_dtm IS NOT NULL
              AND gaming_day IS NOT NULL
              AND wager > 0
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
              {etl_filter}
            ORDER BY __etl_insert_Dtm ASC, bet_id ASC
            LIMIT %(lim)s
        """
        bets = client.query_df(q, parameters=params)
    else:
        chunk_sz = int(cfg.hightier_scorer_player_id_chunk_size)
        chunks = split_allowlist_player_id_chunks(allowlist_player_ids, chunk_sz)
        frames: list[pd.DataFrame] = []
        for i, chunk in enumerate(chunks):
            in_list = ",".join(str(int(x)) for x in chunk)
            q = f"""
                SELECT
                    bet_id,
                    is_back_bet,
                    bet_type,
                    type_of_bet,
                    __etl_insert_Dtm,
                    payout_complete_dtm,
                    gaming_day,
                    session_id,
                    player_id,
                    table_id,
                    position_idx,
                    {_TBET_WAGER_SELECT},
                    {_TBET_CASINO_WIN_SELECT},
                    payout_odds,
                    status,
                    {cid_sel}
                FROM {cfg.source_db}.{cfg.tbet} FINAL
                WHERE payout_complete_dtm >= %(start)s
                  AND payout_complete_dtm <= %(bet_avail)s
                  AND payout_complete_dtm IS NOT NULL
                  AND gaming_day IS NOT NULL
                  AND wager > 0
                  AND player_id IS NOT NULL
                  AND player_id != {placeholder}
                  AND player_id IN ({in_list})
                  {etl_filter}
                ORDER BY __etl_insert_Dtm ASC, bet_id ASC
                LIMIT %(lim)s
            """
            frames.append(client.query_df(q, parameters=params))
            if cap > 0:
                total_so_far = sum(len(f) for f in frames)
                if total_so_far > cap:
                    raise RuntimeError(
                        "incremental chunk merge exceeds hightier_scorer_chunk_merge_row_cap="
                        f"{cap} ({total_so_far} rows after chunk {i + 1}/{len(chunks)})"
                    )
        bets = merge_incremental_chunk_frames(frames, lim)
        logger.info(
            "[hightier_scorer] fetch_bets_incremental allowlist_chunks=%d chunk_size=%d merged_rows=%d final_k=%d",
            len(chunks),
            chunk_sz,
            len(bets),
            lim,
        )

    _postprocess_incremental_bets_timestamps(bets)
    if not bets.empty:
        assert_bets_gaming_day_contract(bets, "hightier_fetch_bets_incremental")
    return bets


def fetch_bet_pool_window(
    *,
    player_ids: list[int],
    window_start: datetime,
    window_end: datetime,
) -> pd.DataFrame:
    """Fetch bet history for rolling 1h features (bounded window)."""
    if not player_ids:
        return pd.DataFrame()
    cfg = default_hightier_serving_config()
    client = get_clickhouse_client()
    bet_avail = datetime.now(ZoneInfo(HK_TZ)) - timedelta(minutes=int(cfg.bet_avail_delay_min))
    end = min(window_end, bet_avail)
    placeholder = int(cfg.placeholder_player_id)
    cid_sel = _TBET_CASINO_PLAYER_ID_SELECT
    unique_ids = sorted({int(x) for x in player_ids})
    chunk_sz = int(cfg.hightier_scorer_player_id_chunk_size)
    cap = int(cfg.hightier_scorer_chunk_merge_row_cap)
    frames: list[pd.DataFrame] = []
    n_chunks = (len(unique_ids) + chunk_sz - 1) // chunk_sz if unique_ids else 0
    for i in range(0, len(unique_ids), chunk_sz):
        chunk = unique_ids[i : i + chunk_sz]
        in_list = ",".join(str(x) for x in chunk)
        q = f"""
            SELECT
                bet_id,
                is_back_bet,
                bet_type,
                type_of_bet,
                __etl_insert_Dtm,
                payout_complete_dtm,
                gaming_day,
                session_id,
                player_id,
                table_id,
                position_idx,
                {_TBET_WAGER_SELECT},
                {_TBET_CASINO_WIN_SELECT},
                payout_odds,
                status,
                {cid_sel}
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE payout_complete_dtm >= %(ws)s
              AND payout_complete_dtm <= %(we)s
              AND payout_complete_dtm IS NOT NULL
              AND gaming_day IS NOT NULL
              AND wager > 0
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
              AND player_id IN ({in_list})
            ORDER BY payout_complete_dtm ASC, bet_id ASC
        """
        frames.append(client.query_df(q, parameters={"ws": window_start, "we": end}))
        if cap > 0:
            total_so_far = sum(len(f) for f in frames)
            if total_so_far > cap:
                raise RuntimeError(
                    "pool chunk merge exceeds hightier_scorer_chunk_merge_row_cap="
                    f"{cap} ({total_so_far} rows after chunk {i // chunk_sz + 1}/{n_chunks})"
                )
    nonempty = [f for f in frames if f is not None and not f.empty]
    if not nonempty:
        return pd.DataFrame()
    bets = pd.concat(nonempty, ignore_index=True)
    bets = bets.drop_duplicates(subset=["bet_id"], keep="first")
    bets["_s_pc"] = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce")
    bets["_s_bid"] = pd.to_numeric(bets["bet_id"], errors="coerce").fillna(-1).astype("int64")
    bets = bets.sort_values(by=["_s_pc", "_s_bid"], ascending=[True, True], na_position="last")
    bets = bets.drop(columns=["_s_pc", "_s_bid"]).reset_index(drop=True)

    _pc = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce")
    if getattr(_pc.dt, "tz", None) is None:
        _pc = _pc.dt.tz_localize(UTC_TZ, ambiguous="NaT", nonexistent="shift_forward")
    bets["payout_complete_dtm"] = _pc.dt.tz_convert(ZoneInfo(HK_TZ))
    _etl = pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce")
    if getattr(_etl.dt, "tz", None) is None:
        _etl = _etl.dt.tz_localize(UTC_TZ, ambiguous="NaT", nonexistent="shift_forward")
    bets["__etl_insert_Dtm"] = _etl.dt.tz_convert(ZoneInfo(HK_TZ))
    logger.info(
        "[hightier_scorer] fetch_bet_pool_window chunks=%d chunk_size=%d unique_players=%d rows=%d",
        n_chunks,
        chunk_sz,
        len(unique_ids),
        len(bets),
    )
    return bets


def score_once(
    conn: sqlite3.Connection,
    bundle: HightierModelBundle,
    *,
    slow_parquet: Path,
    fe_parquet: Path | None = None,
    mapping_parquet: Path | None = None,
    high_adt_only: bool,
    allowlist_ids: frozenset[int],
) -> int:
    """One incremental scoring cycle; returns number of alerts written."""
    cfg = default_hightier_serving_config()
    last = get_last_processed_etl_insert(conn)
    lookback = float(cfg.scorer_dynamic_lookback_cap_hours)
    lim = int(cfg.hightier_scorer_max_bets_per_cycle)

    cursor_all: Optional[pd.Series] = None
    if high_adt_only:
        probe = fetch_bets_incremental_etl_probe(last, lookback_hours=lookback, limit_rows=lim)
        if probe.empty:
            return 0
        cursor_pre_probe = _effective_etl_cursor(probe)
        if last is not None:
            probe = probe[cursor_pre_probe > last].copy()
        if probe.empty:
            return 0
        cursor_all = _effective_etl_cursor(probe)
        bets = fetch_bets_incremental(
            last,
            lookback_hours=lookback,
            limit_rows=lim,
            allowlist_player_ids=allowlist_ids,
        )
        if bets.empty:
            max_c = cursor_all.max()
            if pd.notna(max_c):
                set_last_processed_etl_insert(conn, max_c.to_pydatetime())
            conn.commit()
            return 0
    else:
        bets = fetch_bets_incremental(
            last,
            lookback_hours=lookback,
            limit_rows=lim,
            allowlist_player_ids=None,
        )
        if bets.empty:
            return 0

    cursor_pre = _effective_etl_cursor(bets)
    if last is not None:
        bets = bets[cursor_pre > last].copy()
    if bets.empty:
        if high_adt_only and cursor_all is not None:
            max_c = cursor_all.max()
            if pd.notna(max_c):
                set_last_processed_etl_insert(conn, max_c.to_pydatetime())
            conn.commit()
        return 0

    cursor = _effective_etl_cursor(bets)
    p_min = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").min()
    p_max = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").max()
    pool_start = (p_min - timedelta(hours=int(cfg.hot_feature_pool_lookback_hours))).to_pydatetime()
    pool_end = p_max.to_pydatetime()
    pids = sorted({int(x) for x in bets["player_id"].dropna().unique().tolist()})
    fan_cap = int(cfg.hightier_scorer_pool_player_fanout_cap)
    if len(pids) > fan_cap:
        logger.warning(
            "[hightier_scorer] large player fanout (%d); truncating window pool to cap OOM risk (%d)",
            len(pids),
            fan_cap,
        )
        pids = pids[:fan_cap]
    pool = fetch_bet_pool_window(player_ids=pids, window_start=pool_start, window_end=pool_end)
    pool2 = attach_synthetic_etl_and_prediction_visible(pool)
    pool2 = attach_canonical_id(pool2, mapping_parquet=mapping_parquet)
    staged = attach_synthetic_etl_and_prediction_visible(bets)
    staged = attach_canonical_id(staged, mapping_parquet=mapping_parquet)
    staged = attach_trial_bet_behavior_1h(staged, pool2)
    staged = join_slow_patron_snapshot(staged, slow_parquet)
    if fe_parquet is not None and fe_parquet.is_file():
        staged = join_fe_derived_snapshot(staged, fe_parquet)
    assert_features_ready(staged, bundle.feature_columns)
    X = coerce_categoricals(
        staged[list(bundle.feature_columns)].copy(),
        bundle.categorical_columns,
        dict(bundle.category_categories),
    )
    prob = bundle.model.predict_proba(X)[:, 1]
    thr = float(bundle.threshold)
    scored_at_iso = datetime.now(ZoneInfo(HK_TZ)).isoformat()
    pl_path = cfg.prediction_log_db_path
    if pl_path is not None and str(pl_path).strip():
        try:
            append_hightier_prediction_log(
                pl_path,
                scored_at=scored_at_iso,
                model_version=str(bundle.model_version),
                staged=staged,
                prob=prob,
                threshold=thr,
                features=X,
                feature_columns=bundle.feature_columns,
            )
        except Exception as exc:
            logger.warning("[hightier_scorer] prediction_log write failed: %s", exc)

    m = prob >= thr
    n = int(m.sum())
    if n == 0:
        max_cursor = cursor.max()
        if pd.notna(max_cursor):
            set_last_processed_etl_insert(conn, max_cursor.to_pydatetime())
        conn.commit()
        return 0
    out = staged.loc[m].copy()
    out["score"] = prob[m]
    now_iso = scored_at_iso
    nom = out["casino_player_id"] if "casino_player_id" in out.columns else pd.Series("", index=out.index)
    rated = nom.notna() & (nom.astype(str).str.strip() != "")
    alerts = pd.DataFrame(
        {
            "bet_id": out["bet_id"].astype(str),
            "ts": now_iso,
            "bet_ts": out["payout_complete_dtm"],
            "player_id": out["player_id"],
            "casino_player_id": nom,
            "table_id": out["table_id"],
            "position_idx": out["position_idx"],
            "visit_start_ts": None,
            "visit_end_ts": None,
            "session_count": None,
            "bet_count": None,
            "visit_avg_bet": out["wager"],
            "historical_avg_bet": None,
            "score": out["score"],
            "session_id": out["session_id"],
            "loss_streak": 0,
            "bets_last_5m": 0.0,
            "bets_last_15m": 0.0,
            "bets_last_30m": 0.0,
            "wager_last_10m": 0.0,
            "wager_last_30m": 0.0,
            "cum_bets": 0.0,
            "cum_wager": 0.0,
            "avg_wager_sofar": out["wager"],
            "session_duration_min": 0.0,
            "bets_per_minute": 0.0,
            "canonical_id": out["canonical_id"],
            "is_rated_obs": np.where(rated, 1, 0),
            "reason_codes": None,
            "model_version": bundle.model_version,
            "margin": out["score"] - thr,
            "scored_at": now_iso,
        }
    )
    append_alerts(conn, alerts)
    idx = out.index
    max_cursor = cursor.loc[idx].max()
    if pd.notna(max_cursor):
        set_last_processed_etl_insert(conn, max_cursor.to_pydatetime())
    conn.commit()
    logger.info("[hightier_scorer] wrote %d alerts (threshold=%.6f)", n, thr)
    return n


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pr = argparse.ArgumentParser(description="trainer_hightier ClickHouse scorer daemon")
    pr.add_argument("--once", action="store_true", help="single cycle then exit")
    pr.add_argument("--bundle-dir", type=Path, default=None)
    pr.add_argument("--slow-parquet", type=Path, default=None, help="override slow patron snapshot path")
    pr.add_argument("--canonical-mapping", type=Path, default=None)
    pr.add_argument(
        "--adt-allowlist",
        type=Path,
        default=None,
        help="override ADT allowlist parquet (otherwise manifest / config / default quantile path)",
    )
    pr.add_argument(
        "--no-high-adt-only",
        action="store_true",
        help="score all players (debug/regression only; not for routine production)",
    )
    args = pr.parse_args(argv)
    cfg = default_hightier_serving_config()
    init_state_db(Path(cfg.state_db_path))
    init_prediction_log_db(cfg.prediction_log_db_path)
    bundle = load_hightier_model_bundle(bundle_dir=args.bundle_dir)
    slow_override = Path(args.slow_parquet).resolve() if args.slow_parquet else None
    map_path = Path(args.canonical_mapping).resolve() if args.canonical_mapping else None
    cli_al = Path(args.adt_allowlist).resolve() if args.adt_allowlist else None
    high_adt_only = bool(cfg.high_adt_only) and (not bool(args.no_high_adt_only))
    al_cache: dict[str, Any] = {}
    boot_logged = False

    def resolve_slow() -> Path:
        if slow_override is not None:
            return slow_override
        man = read_active_manifest()
        if man is None:
            raise RuntimeError("No active snapshot manifest; run snapshot_updater or pass --slow-parquet")
        return man.slow_patron_parquet

    def resolve_fe(man: ActiveSnapshotManifest | None) -> Path | None:
        if man is None:
            return None
        fp = man.fe_derived_parquet
        return fp if fp is not None and str(fp).strip() else None

    while True:
        conn = connect_state_db(Path(cfg.state_db_path))
        try:
            man = read_active_manifest()
            if man is not None:
                prev_v = meta_get(conn, META_KEY_ACTIVE_SNAPSHOT_VERSION)
                if prev_v != man.version:
                    logger.info("[hightier_scorer] snapshot manifest version %s -> %s", prev_v, man.version)
                    meta_set(conn, META_KEY_ACTIVE_SNAPSHOT_VERSION, man.version)
                    conn.commit()
            _sync_allowlist_cache(
                al_cache,
                cfg=cfg,
                manifest=man,
                bundle=bundle,
                cli_allowlist=cli_al,
                high_adt_only=high_adt_only,
            )
            if high_adt_only:
                al_path = al_cache.get("path")
                al_sha = al_cache.get("sha256")
                health = "ok" if al_cache.get("hash_ok", True) else "degraded_hash_mismatch"
                meta_set(conn, META_KEY_ADT_ALLOWLIST_HEALTH, health)
                if al_sha:
                    meta_set(conn, META_KEY_ACTIVE_ADT_ALLOWLIST_SHA256, str(al_sha))
                    mver = man.adt_allowlist_version if man is not None else None
                    meta_set(conn, META_KEY_ACTIVE_ADT_ALLOWLIST_VERSION, str(mver or al_sha))
                conn.commit()
            else:
                meta_set(conn, META_KEY_ADT_ALLOWLIST_HEALTH, "full_population_mode")
                conn.commit()

            if not boot_logged:
                _log_scorer_boot_line(
                    bundle=bundle,
                    high_adt_only=high_adt_only,
                    al_path=al_cache.get("path") if high_adt_only else None,
                    al_sha=al_cache.get("sha256") if high_adt_only else None,
                    man_ver=man.version if man is not None else None,
                    man_al_ver=man.adt_allowlist_version if man is not None else None,
                )
                boot_logged = True

            sp = resolve_slow()
            if not sp.is_file():
                raise FileNotFoundError(f"slow patron parquet missing: {sp}")
            fe_sp = resolve_fe(man)
            allow_ids = al_cache.get("ids", frozenset()) if high_adt_only else frozenset()
            if high_adt_only and not isinstance(allow_ids, frozenset):
                allow_ids = frozenset(allow_ids)
            score_once(
                conn,
                bundle,
                slow_parquet=sp,
                fe_parquet=fe_sp,
                mapping_parquet=map_path,
                high_adt_only=high_adt_only,
                allowlist_ids=allow_ids,
            )
        finally:
            conn.close()
        if args.once:
            break
        time.sleep(float(cfg.scorer_poll_interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
