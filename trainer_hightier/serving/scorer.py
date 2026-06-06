"""High-tier ClickHouse scorer: incremental bets, baseline features, ``state.db`` alerts."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from trainer_hightier.bet_contract import assert_bets_gaming_day_event_contract

from trainer_hightier.config import HightierServingConfig, default_hightier_serving_config
from trainer_hightier.serving.adt_allowlist import (
    check_training_allowlist_sha256,
    load_adt_allowlist_ids,
    resolve_adt_allowlist_path,
    sha256_file,
)
from trainer_hightier.serving.ch_adapter import (
    CH_TBET_CASINO_WIN_SELECT,
    CH_TBET_GAMING_DAY_EVENT_NOT_NULL_PRED,
    CH_TBET_GAMING_DAY_EVENT_SELECT,
    CH_TBET_PAYOUT_ODDS_SELECT,
    CH_TBET_WAGER_SELECT,
    ch_tbet_gaming_day_event_sql,
    get_clickhouse_client,
)
from trainer_hightier.serving.contracts import (
    META_KEY_ACTIVE_ADT_ALLOWLIST_SHA256,
    META_KEY_ACTIVE_ADT_ALLOWLIST_VERSION,
    META_KEY_ACTIVE_SNAPSHOT_VERSION,
    META_KEY_ADT_ALLOWLIST_HEALTH,
    META_KEY_MID_TERM_ANCHOR_MAX,
    META_KEY_MID_TERM_FRESHNESS_STATUS,
    META_KEY_MID_TERM_STALENESS_DAYS,
    META_KEY_SLOW_ANCHOR_MAX,
    META_KEY_SLOW_FRESHNESS_STATUS,
    META_KEY_SLOW_STALENESS_DAYS,
    META_KEY_SNAPSHOT_SCORING_DEGRADED,
)
from trainer_hightier.feature_experiment.feature_cadence import (
    short_term_enrich_columns_with_dependencies,
)
from trainer_hightier.serving.feature_builder import (
    assert_features_ready,
    attach_canonical_id,
    attach_mid_term_composite_columns,
    attach_short_term_pit_features,
    attach_synthetic_etl_and_prediction_visible,
    attach_trial_bet_behavior_1h,
    prepare_lgbm_feature_matrix,
)
from trainer_hightier.serving.feast_readiness import (
    evaluate_feast_readiness_gate,
    load_feast_online_readiness,
    resolve_feast_readiness_path,
    run_deploy_feast_readiness_check,
)
from trainer_hightier.serving.feast_online_adapter import (
    FeastLookupDiagnostics,
    FeastLookupResult,
    OnlineFeastAdapter,
    apply_entity_missing_policy,
    build_cycle_readiness_summary,
    build_production_feast_adapter,
    compute_row_missing_audits,
    default_feast_repo_path,
    join_feast_lookup,
    run_feast_scorer_schema_smoke_check,
)
from trainer_hightier.serving.feature_supply import (
    ScorerSupplierPlan,
    assert_scorer_supplier_plan_or_raise,
    build_scorer_supplier_plan,
    load_frozen_registry_for_bundle,
    scorer_supplier_route_counts,
)
from trainer_hightier.serving.snapshot_freshness import (
    LayerFreshnessResult,
    SnapshotValidationResult,
    build_scoring_snapshot_gate,
    evaluate_mid_term_freshness,
    evaluate_slow_freshness,
    post_join_feature_smoke,
    read_mid_term_anchor_max,
    read_slow_anchor_max,
    validate_mid_term_artifact,
    validate_slow_artifact,
)
from trainer_hightier.serving.feature_state_store import ActiveSnapshotManifest, read_active_manifest
from trainer_hightier.evaluation.player_alert_policy import (
    apply_serving_player_alert_suppression,
    warn_player_alert_policy_mismatch,
)
from trainer_hightier.serving.model_bundle import HightierModelBundle, load_hightier_model_bundle
from trainer_hightier.serving.prediction_log import (
    append_hightier_prediction_log,
    append_skipped_entity_missing_log,
    init_prediction_log_db,
)
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

_LAST_SCORER_CYCLE_METRICS: dict[str, Any] | None = None


def get_last_scorer_cycle_metrics() -> dict[str, Any] | None:
    """Metrics from the most recent :func:`score_once` call (for P6-3 dry-run reports)."""
    return _LAST_SCORER_CYCLE_METRICS


def _record_scorer_cycle_metrics(
    *,
    model_version: str,
    cycle_readiness: dict[str, Any],
    n_alerts: int,
    n_batch_rows: int = 0,
    queue_drained: bool = True,
) -> None:
    global _LAST_SCORER_CYCLE_METRICS
    _LAST_SCORER_CYCLE_METRICS = {
        "model_version": str(model_version),
        "cycle_readiness": dict(cycle_readiness),
        "n_alerts": int(n_alerts),
        "n_batch_rows": int(n_batch_rows),
        "queue_drained": bool(queue_drained),
    }


def _queue_drained_from_batch_rows(n_batch_rows: int, *, cap: int) -> bool:
    """Return True when incremental batch is below per-cycle cap (no backlog)."""
    return int(n_batch_rows) < int(max(1, cap))


def compute_scorer_cycle_sleep_seconds(
    *,
    batch_rows: int,
    cfg: HightierServingConfig | None = None,
) -> float:
    """Poll sleep after one cycle; zero when backlog drain is enabled and batch hit cap."""
    c = cfg or default_hightier_serving_config()
    cap = int(c.hightier_scorer_max_bets_per_cycle)
    if bool(c.scorer_backlog_no_sleep_enabled) and int(batch_rows) >= cap:
        return 0.0
    return float(c.scorer_poll_interval_seconds)


def _log_scorer_cycle_summary(
    *,
    cycle_num: int,
    metrics: dict[str, Any],
    sleep_s: float,
    elapsed_s: float | None,
    cfg: HightierServingConfig,
) -> None:
    """One INFO line per scorer cycle for operator visibility."""
    cr = dict(metrics.get("cycle_readiness") or {})
    n_scored = int(cr.get("n_scored") or metrics.get("n_batch_rows") or 0)
    n_skipped = int(cr.get("n_skipped_entity_missing") or 0)
    n_alerts = int(metrics.get("n_alerts") or 0)
    latency_ms = float(cr.get("lookup_latency_ms") or 0.0)
    queue_drained = bool(metrics.get("queue_drained", True))
    batch_rows = int(metrics.get("n_batch_rows") or 0)
    cap = int(cfg.hightier_scorer_max_bets_per_cycle)
    elapsed_part = f" elapsed_s={elapsed_s:.1f}" if elapsed_s is not None else ""
    logger.info(
        "[hightier_scorer] cycle#%d scored=%d alerts=%d skipped=%d latency_ms=%.1f "
        "queue_drained=%s batch_rows=%d cap=%d sleep_s=%s%s",
        cycle_num,
        n_scored,
        n_alerts,
        n_skipped,
        latency_ms,
        queue_drained,
        batch_rows,
        cap,
        sleep_s,
        elapsed_part,
    )
    warn_ms = float(cfg.scorer_feast_lookup_latency_warn_ms)
    if latency_ms > warn_ms:
        logger.warning(
            "[hightier_scorer] cycle#%d feast lookup latency_ms=%.1f exceeds warn threshold=%.1f",
            cycle_num,
            latency_ms,
            warn_ms,
        )
    if n_skipped > 0:
        rate = float(cr.get("entity_missing_rate") or 0.0)
        logger.warning(
            "[hightier_scorer] cycle#%d entity_missing skipped=%d rate=%.4f",
            cycle_num,
            n_skipped,
            rate,
        )


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
#: GDP_GMWDS_Raw §4 money columns are ``Decimal(19,4)`` (e.g. ``payout_odds`` 0–100).
#: Cast at read time so clickhouse_connect / DuckDB never infer a narrow DECIMAL from a
#: mostly-small sample (production crash: ``100.0000`` vs inferred ``DECIMAL(6,4)``).
_TBET_CASINO_PLAYER_ID_SELECT = "CAST(NULL AS Nullable(String)) AS casino_player_id"
_TBET_WAGER_SELECT = CH_TBET_WAGER_SELECT
_TBET_CASINO_WIN_SELECT = CH_TBET_CASINO_WIN_SELECT
_TBET_PAYOUT_ODDS_SELECT = CH_TBET_PAYOUT_ODDS_SELECT


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
    """Normalize ClickHouse warehouse timestamps to HK (in-place).

    Naive warehouse timestamps are interpreted as UTC before conversion.
    """
    if not bets.empty and "payout_complete_dtm" in bets.columns:
        bets["payout_complete_dtm"] = _warehouse_ts_series_to_hk(bets["payout_complete_dtm"])
    if not bets.empty and "__etl_insert_Dtm" in bets.columns:
        bets["__etl_insert_Dtm"] = _warehouse_ts_series_to_hk(bets["__etl_insert_Dtm"])


def _postprocess_cleaned_l0_bets_timestamps(bets: pd.DataFrame) -> None:
    """Normalize cleaned L0 parquet timestamps to HK (in-place).

    Naive parquet timestamps are HK wall clock, not UTC.
    """
    from trainer_hightier.utils.hk_time_semantics import pandas_ts_series_to_hk_l0_contract

    if not bets.empty and "payout_complete_dtm" in bets.columns:
        bets["payout_complete_dtm"] = pandas_ts_series_to_hk_l0_contract(
            bets["payout_complete_dtm"],
        )
    if not bets.empty and "__etl_insert_Dtm" in bets.columns:
        bets["__etl_insert_Dtm"] = pandas_ts_series_to_hk_l0_contract(
            bets["__etl_insert_Dtm"],
        )


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
          AND {CH_TBET_GAMING_DAY_EVENT_NOT_NULL_PRED}
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


def _incremental_bet_select_list(*, casino_player_id_select: str) -> str:
    """Shared SELECT column list for incremental ``t_bet`` fetches."""
    return f"""
                bet_id,
                is_back_bet,
                bet_type,
                type_of_bet,
                __etl_insert_Dtm,
                payout_complete_dtm,
                {CH_TBET_GAMING_DAY_EVENT_SELECT},
                session_id,
                player_id,
                game_id,
                table_id,
                position_idx,
                {_TBET_WAGER_SELECT},
                {_TBET_CASINO_WIN_SELECT},
                {_TBET_PAYOUT_ODDS_SELECT},
                status,
                {casino_player_id_select}
    """.strip()


def _build_allowlist_external_data(player_ids: frozenset[int]) -> Any:
    """Build ClickHouse external input payload for allowlist ``player_id`` join."""
    from clickhouse_connect.driver.external import ExternalData

    if not player_ids:
        raise ValueError("allowlist player_ids must be non-empty for external input join")
    lines = "\n".join(str(int(x)) for x in sorted(int(x) for x in player_ids))
    payload = (lines + "\n").encode("utf-8") if lines else b""
    return ExternalData(
        file_name="adt_allowlist.tsv",
        data=payload,
        fmt="TSV",
        structure=["player_id Int64"],
    )


def _fetch_bets_incremental_allowlist_chunk(
    client: Any,
    *,
    cfg: HightierServingConfig,
    allowlist_player_ids: frozenset[int],
    params: dict[str, Any],
    etl_filter: str,
    limit_rows: int,
    placeholder: int,
) -> pd.DataFrame:
    """Legacy allowlist path: chunked ``player_id IN (...)`` queries merged client-side."""
    lim = int(max(1, limit_rows))
    cid_sel = _TBET_CASINO_PLAYER_ID_SELECT
    cap = int(cfg.hightier_scorer_chunk_merge_row_cap)
    chunk_sz = int(cfg.hightier_scorer_player_id_chunk_size)
    chunks = split_allowlist_player_id_chunks(allowlist_player_ids, chunk_sz)
    select_cols = _incremental_bet_select_list(casino_player_id_select=cid_sel)
    frames: list[pd.DataFrame] = []
    for i, chunk in enumerate(chunks):
        in_list = ",".join(str(int(x)) for x in chunk)
        q = f"""
            SELECT
                {select_cols}
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE payout_complete_dtm >= %(start)s
              AND payout_complete_dtm <= %(bet_avail)s
              AND payout_complete_dtm IS NOT NULL
              AND {CH_TBET_GAMING_DAY_EVENT_NOT_NULL_PRED}
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
    logger.debug(
        "[hightier_scorer] fetch_bets_incremental allowlist_chunks=%d chunk_size=%d merged_rows=%d final_k=%d",
        len(chunks),
        chunk_sz,
        len(bets),
        lim,
    )
    return bets


def _fetch_bets_incremental_allowlist_external(
    client: Any,
    *,
    cfg: HightierServingConfig,
    allowlist_player_ids: frozenset[int],
    params: dict[str, Any],
    etl_filter: str,
    limit_rows: int,
    placeholder: int,
) -> pd.DataFrame:
    """Single-query allowlist path: ``INNER JOIN adt_allowlist`` via query-time external input."""
    cid_sel = _TBET_CASINO_PLAYER_ID_SELECT
    select_cols = _incremental_bet_select_list(casino_player_id_select=cid_sel)
    external = _build_allowlist_external_data(allowlist_player_ids)
    etl_filter_t = etl_filter.replace("__etl_insert_Dtm", "t.__etl_insert_Dtm") if etl_filter else ""
    q = f"""
        SELECT
            {select_cols}
        FROM {cfg.source_db}.{cfg.tbet} AS t FINAL
        INNER JOIN adt_allowlist AS al ON t.player_id = al.player_id
        WHERE t.payout_complete_dtm >= %(start)s
          AND t.payout_complete_dtm <= %(bet_avail)s
          AND t.payout_complete_dtm IS NOT NULL
          AND {ch_tbet_gaming_day_event_sql(table_alias="t")} IS NOT NULL
          AND t.wager > 0
          AND t.player_id IS NOT NULL
          AND t.player_id != {placeholder}
          {etl_filter_t}
        ORDER BY t.__etl_insert_Dtm ASC, t.bet_id ASC
        LIMIT %(lim)s
    """
    bets = client.query_df(q, parameters=params, external_data=external)
    logger.debug(
        "[hightier_scorer] fetch_bets_incremental allowlist_external_input n_allowlist=%d rows=%d final_k=%d",
        len(allowlist_player_ids),
        len(bets),
        int(max(1, limit_rows)),
    )
    return bets


def _fetch_bets_incremental_allowlist(
    client: Any,
    *,
    cfg: HightierServingConfig,
    allowlist_player_ids: frozenset[int],
    params: dict[str, Any],
    etl_filter: str,
    limit_rows: int,
    placeholder: int,
) -> pd.DataFrame:
    """Route allowlist incremental fetch via external input or legacy chunk merge."""
    mode = str(cfg.scorer_allowlist_join_mode or "external_input").strip().lower()
    if mode == "chunk":
        return _fetch_bets_incremental_allowlist_chunk(
            client,
            cfg=cfg,
            allowlist_player_ids=allowlist_player_ids,
            params=params,
            etl_filter=etl_filter,
            limit_rows=limit_rows,
            placeholder=placeholder,
        )
    if mode != "external_input":
        raise ValueError(
            f"scorer_allowlist_join_mode must be 'external_input' or 'chunk', got {mode!r}"
        )
    try:
        return _fetch_bets_incremental_allowlist_external(
            client,
            cfg=cfg,
            allowlist_player_ids=allowlist_player_ids,
            params=params,
            etl_filter=etl_filter,
            limit_rows=limit_rows,
            placeholder=placeholder,
        )
    except Exception as exc:
        if not bool(cfg.scorer_allowlist_join_fallback_to_chunk):
            raise RuntimeError(
                "[hightier_scorer] allowlist external-input join failed; "
                "set scorer_allowlist_join_fallback_to_chunk=True to use legacy chunk path"
            ) from exc
        logger.warning(
            "[hightier_scorer] allowlist external-input join failed (%s); falling back to chunk path",
            exc,
        )
        return _fetch_bets_incremental_allowlist_chunk(
            client,
            cfg=cfg,
            allowlist_player_ids=allowlist_player_ids,
            params=params,
            etl_filter=etl_filter,
            limit_rows=limit_rows,
            placeholder=placeholder,
        )


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
    When non-empty, use configured allowlist join mode (default: external input single query).
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
    select_cols = _incremental_bet_select_list(casino_player_id_select=cid_sel)

    if allowlist_player_ids is None:
        q = f"""
            SELECT
                {select_cols}
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE payout_complete_dtm >= %(start)s
              AND payout_complete_dtm <= %(bet_avail)s
              AND payout_complete_dtm IS NOT NULL
              AND {CH_TBET_GAMING_DAY_EVENT_NOT_NULL_PRED}
              AND wager > 0
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
              {etl_filter}
            ORDER BY __etl_insert_Dtm ASC, bet_id ASC
            LIMIT %(lim)s
        """
        bets = client.query_df(q, parameters=params)
    else:
        bets = _fetch_bets_incremental_allowlist(
            client,
            cfg=cfg,
            allowlist_player_ids=allowlist_player_ids,
            params=params,
            etl_filter=etl_filter,
            limit_rows=lim,
            placeholder=placeholder,
        )

    _postprocess_incremental_bets_timestamps(bets)
    if not bets.empty:
        assert_bets_gaming_day_event_contract(bets, "hightier_fetch_bets_incremental")
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
                {CH_TBET_GAMING_DAY_EVENT_SELECT},
                session_id,
                player_id,
                game_id,
                table_id,
                position_idx,
                {_TBET_WAGER_SELECT},
                {_TBET_CASINO_WIN_SELECT},
                {_TBET_PAYOUT_ODDS_SELECT},
                status,
                {cid_sel}
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE payout_complete_dtm >= %(ws)s
              AND payout_complete_dtm <= %(we)s
              AND payout_complete_dtm IS NOT NULL
              AND {CH_TBET_GAMING_DAY_EVENT_NOT_NULL_PRED}
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
    logger.debug(
        "[hightier_scorer] fetch_bet_pool_window chunks=%d chunk_size=%d unique_players=%d rows=%d",
        n_chunks,
        chunk_sz,
        len(unique_ids),
        len(bets),
    )
    return bets


def compute_hot_pool_window_start(
    bets: pd.DataFrame,
    *,
    cfg: HightierServingConfig | None = None,
) -> datetime:
    """Earliest hot-pool open bound: lookback plus same-gaming-day coverage floor."""
    if bets.empty:
        raise ValueError("compute_hot_pool_window_start requires non-empty bets")
    cfg = cfg or default_hightier_serving_config()
    p_min = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").min()
    if pd.isna(p_min):
        raise ValueError("bets payout_complete_dtm is all null")
    lookback_start = (
        p_min - timedelta(hours=int(cfg.hot_feature_pool_lookback_hours))
    ).to_pydatetime()
    candidates: list[datetime] = [lookback_start]
    if "gaming_day_event" not in bets.columns:
        raise ValueError(
            "compute_hot_pool_window_start requires gaming_day_event on bets; "
            f"got columns={list(bets.columns)}",
        )
    hk = ZoneInfo(cfg.hk_tz)
    for raw_gday in pd.to_datetime(bets["gaming_day_event"], errors="coerce").dropna().unique():
        gday = pd.Timestamp(raw_gday).date()
        day_start = datetime(gday.year, gday.month, gday.day, 0, 0, 0, tzinfo=hk)
        if getattr(p_min, "tzinfo", None) is not None:
            day_start = day_start.astimezone(p_min.tzinfo)
        candidates.append(day_start)
    pool_start = min(candidates)
    if getattr(p_min, "tzinfo", None) is not None and pool_start.tzinfo is None:
        pool_start = pool_start.replace(tzinfo=p_min.tzinfo)
    return pool_start


def compute_scoring_bounds_for_bets(
    bets: pd.DataFrame,
    *,
    cfg: HightierServingConfig | None = None,
) -> pd.DataFrame:
    """Per-scoring-bet hot-pool bounds (batch-invariant PIT; production contract).

    Each row defines ``[pool_start, scoring_pcd]`` for one target ``bet_id``. Co-batching
    must not widen a bet's usable pool beyond its own lookback / gaming-day floor.
    """
    if bets.empty:
        return pd.DataFrame(
            columns=["bet_id", "player_id", "canonical_id", "pool_start", "scoring_pcd"],
        )
    if "payout_complete_dtm" not in bets.columns:
        raise ValueError(
            "bets missing payout_complete_dtm for scoring bounds; "
            f"got columns={list(bets.columns)}",
        )
    if "bet_id" not in bets.columns:
        raise ValueError(
            f"bets missing bet_id for scoring bounds; got columns={list(bets.columns)}",
        )
    cfg = cfg or default_hightier_serving_config()
    pcd = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce", utc=True)
    lookback_h = int(cfg.hot_feature_pool_lookback_hours)
    lookback_start = pcd - pd.Timedelta(hours=lookback_h)
    pool_start = lookback_start.copy()
    if "gaming_day_event" not in bets.columns:
        raise ValueError(
            "compute_scoring_bounds_for_bets requires gaming_day_event on bets; "
            f"got columns={list(bets.columns)}",
        )
    hk = ZoneInfo(cfg.hk_tz)
    day_starts: list[pd.Timestamp] = []
    for raw_gday, p_row in zip(
        pd.to_datetime(bets["gaming_day_event"], errors="coerce"),
        pcd,
        strict=True,
    ):
        if pd.isna(raw_gday) or pd.isna(p_row):
            day_starts.append(pd.NaT)
            continue
        gday = pd.Timestamp(raw_gday).date()
        day_open = datetime(gday.year, gday.month, gday.day, 0, 0, 0, tzinfo=hk)
        if getattr(p_row, "tzinfo", None) is not None:
            day_open = day_open.astimezone(p_row.tzinfo)
        day_starts.append(pd.Timestamp(day_open))
    day_start_s = pd.Series(day_starts, index=bets.index)
    pool_start = pd.concat([lookback_start, day_start_s], axis=1).min(axis=1)
    out = pd.DataFrame(
        {
            "bet_id": pd.to_numeric(bets["bet_id"], errors="coerce"),
            "player_id": pd.to_numeric(bets["player_id"], errors="coerce"),
            "pool_start": pool_start,
            "scoring_pcd": pcd,
        },
    )
    if "canonical_id" in bets.columns:
        out["canonical_id"] = bets["canonical_id"].astype(str).str.strip()
    return out.dropna(subset=["bet_id", "scoring_pcd"]).reset_index(drop=True)


@dataclass(frozen=True)
class _ScoringBatch:
    """Bounded incremental batch ready for feature build."""

    bets: pd.DataFrame
    cursor: pd.Series
    pool: pd.DataFrame
    pool_window_start: datetime | None = None
    pool_window_end: datetime | None = None


def _fetch_scoring_batch(
    conn: sqlite3.Connection,
    *,
    high_adt_only: bool,
    allowlist_ids: frozenset[int],
) -> _ScoringBatch | None:
    """Phase 1: fetch incremental bets and bounded hot-feature pool."""

    cfg = default_hightier_serving_config()
    last = get_last_processed_etl_insert(conn)
    lookback = float(cfg.scorer_dynamic_lookback_cap_hours)
    lim = int(cfg.hightier_scorer_max_bets_per_cycle)
    cursor_all: Optional[pd.Series] = None

    if high_adt_only:
        probe = fetch_bets_incremental_etl_probe(last, lookback_hours=lookback, limit_rows=lim)
        if probe.empty:
            return None
        cursor_pre_probe = _effective_etl_cursor(probe)
        if last is not None:
            probe = probe[cursor_pre_probe > last].copy()
        if probe.empty:
            return None
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
            return None
    else:
        bets = fetch_bets_incremental(
            last,
            lookback_hours=lookback,
            limit_rows=lim,
            allowlist_player_ids=None,
        )
        if bets.empty:
            return None

    cursor_pre = _effective_etl_cursor(bets)
    if last is not None:
        bets = bets[cursor_pre > last].copy()
    if bets.empty:
        if high_adt_only and cursor_all is not None:
            max_c = cursor_all.max()
            if pd.notna(max_c):
                set_last_processed_etl_insert(conn, max_c.to_pydatetime())
            conn.commit()
        return None

    cursor = _effective_etl_cursor(bets)
    p_max = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce").max()
    pool_start = compute_hot_pool_window_start(bets, cfg=cfg)
    pool_end = p_max.to_pydatetime()
    # Production hot pool uses batch player_ids only (no canonical alias fanout).
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
    pool = attach_synthetic_etl_and_prediction_visible(pool)
    return _ScoringBatch(
        bets=bets,
        cursor=cursor,
        pool=pool,
        pool_window_start=pool_start,
        pool_window_end=pool_end,
    )


def _build_staged_features(
    batch: _ScoringBatch,
    *,
    mapping_parquet: Path | None,
    supplier_plan: ScorerSupplierPlan,
) -> pd.DataFrame:
    """Phase 2: hot PIT + short-term bounded PIT on the scoring batch.

    ClickHouse ``fetch_bet_pool_window`` supplies the hot pool; player fanout is
    capped by ``hightier_scorer_pool_player_fanout_cap`` with ``expand_canonical_aliases=False``
    policy aligned to training materialize (see ``short_term_scoring_context``).
    """
    from trainer_hightier.serving.short_term_scoring_context import attach_live_short_term_pit

    pool = attach_canonical_id(batch.pool, mapping_parquet=mapping_parquet)
    staged = attach_synthetic_etl_and_prediction_visible(batch.bets)
    staged = attach_canonical_id(staged, mapping_parquet=mapping_parquet)
    mid_term_for_deps = supplier_plan.mid_composite_cols
    short_cols = short_term_enrich_columns_with_dependencies(
        supplier_plan.short_term_cols,
        mid_term_for_deps,
    )
    return attach_live_short_term_pit(staged, pool, short_columns=short_cols)


def _log_scorer_readiness_summary(
    *,
    bundle: HightierModelBundle,
    supplier_plan: ScorerSupplierPlan,
) -> None:
    """Log scorer v2 supplier routes at startup."""
    routes = scorer_supplier_route_counts(supplier_plan)
    logger.info(
        "[hightier_scorer] readiness model_version=%s routes=%s feast_mid=%d mid_composite=%d "
        "feast_slow=%d short_term=%d feast_repo=%s entity_missing_fail_fraction=%s",
        bundle.model_version,
        routes,
        len(supplier_plan.feast_mid_cols),
        len(supplier_plan.mid_composite_cols),
        len(supplier_plan.feast_slow_cols),
        len(supplier_plan.short_term_cols),
        default_feast_repo_path(),
        default_hightier_serving_config().scorer_feast_entity_missing_fail_fraction,
    )


def _attach_feast_mid_slow(
    staged: pd.DataFrame,
    adapter: OnlineFeastAdapter,
    *,
    mid_columns: tuple[str, ...],
    slow_columns: tuple[str, ...],
    fail_fraction: float,
) -> tuple[pd.DataFrame, pd.DataFrame, FeastLookupDiagnostics]:
    """Feast online lookup for mid/long columns; returns (scorable, skipped, diagnostics)."""
    feast_cols = tuple(dict.fromkeys([*mid_columns, *slow_columns]))
    if staged.empty or not feast_cols:
        empty_diag = FeastLookupDiagnostics(
            lookup_latency_ms=0.0,
            n_requested=len(staged),
            n_mid_present=0,
            n_slow_present=0,
            n_entity_missing=0,
        )
        return staged.copy(), staged.iloc[0:0].copy(), empty_diag
    cids = sorted(
        {str(x).strip() for x in staged["canonical_id"].tolist() if str(x).strip()}
    )
    t0 = time.perf_counter()
    lookup_df = adapter.lookup_mid_slow(
        cids,
        mid_columns=mid_columns,
        slow_columns=slow_columns,
    )
    lookup_latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    lookup = join_feast_lookup(
        staged,
        lookup_df,
        feature_columns=feast_cols,
        mid_columns=mid_columns,
        slow_columns=slow_columns,
    )
    lookup_diag = FeastLookupDiagnostics(
        lookup_latency_ms=lookup_latency_ms,
        n_requested=lookup.diagnostics.n_requested,
        n_mid_present=lookup.diagnostics.n_mid_present,
        n_slow_present=lookup.diagnostics.n_slow_present,
        n_entity_missing=lookup.diagnostics.n_entity_missing,
        cell_null_counts=dict(lookup.diagnostics.cell_null_counts),
    )
    lookup = FeastLookupResult(
        feature_columns=lookup.feature_columns,
        values=lookup.values,
        entity_missing=lookup.entity_missing,
        diagnostics=lookup_diag,
    )
    scorable, skipped, diag = apply_entity_missing_policy(
        lookup.values,
        lookup,
        fail_fraction=fail_fraction,
        mid_columns=mid_columns,
        slow_columns=slow_columns,
    )
    return scorable, skipped, diag


def _commit_scoring_cursor(
    conn: sqlite3.Connection,
    cursor: pd.Series,
    row_indices: pd.Index,
) -> None:
    """Advance ETL watermark to the max cursor among *row_indices*."""
    if row_indices.empty:
        return
    max_cursor = cursor.loc[row_indices].max()
    if pd.notna(max_cursor):
        set_last_processed_etl_insert(conn, max_cursor.to_pydatetime())


def _build_player_game_alert_frame(
    staged: pd.DataFrame,
    prob: np.ndarray,
    *,
    threshold: float,
    scored_at_iso: str,
    model_version: str,
) -> tuple[pd.DataFrame, int]:
    """One alert row per ``player_id + game_id`` when ``max(score) >= threshold``."""

    if staged.empty:
        return pd.DataFrame(), 0
    for col in ("player_id", "game_id", "bet_id", "payout_complete_dtm"):
        if col not in staged.columns:
            raise ValueError(
                f"_build_player_game_alert_frame missing column {col!r}; got {list(staged.columns)!r}",
            )
    work = staged.copy()
    work["_score"] = np.asarray(prob, dtype=np.float64).reshape(-1)
    work["player_id"] = pd.to_numeric(work["player_id"], errors="coerce").astype("Int64")
    work["game_id"] = pd.to_numeric(work["game_id"], errors="coerce").astype("Int64")
    valid = work["player_id"].notna() & work["game_id"].notna() & np.isfinite(work["_score"].to_numpy())
    excluded = int((~valid).sum())
    if excluded > 0:
        logger.warning(
            "[hightier_scorer] excluded %d scored bets with null player_id/game_id or non-finite score",
            excluded,
        )
    work = work.loc[valid]
    if work.empty:
        return pd.DataFrame(), excluded
    work["_bet_id_sort"] = pd.to_numeric(work["bet_id"], errors="coerce").fillna(-1)
    work = work.sort_values(
        by=["_score", "payout_complete_dtm", "_bet_id_sort"],
        ascending=[False, True, True],
    )
    rep = work.groupby(["player_id", "game_id"], as_index=False, dropna=True).first()
    counts = work.groupby(["player_id", "game_id"], dropna=True).size().reset_index(name="player_game_bet_count")
    rep = rep.merge(counts, on=["player_id", "game_id"], how="left")
    rep["player_game_score"] = rep["_score"]
    thr = float(threshold)
    alerted = rep.loc[rep["player_game_score"] >= thr].copy()
    if alerted.empty:
        return pd.DataFrame(), excluded
    nom = (
        alerted["casino_player_id"]
        if "casino_player_id" in alerted.columns
        else pd.Series("", index=alerted.index)
    )
    rated = nom.notna() & (nom.astype(str).str.strip() != "")
    alerts = pd.DataFrame(
        {
            "bet_id": alerted["bet_id"].astype(str),
            "ts": scored_at_iso,
            "bet_ts": alerted["payout_complete_dtm"],
            "player_id": alerted["player_id"],
            "casino_player_id": nom,
            "table_id": alerted["table_id"] if "table_id" in alerted.columns else None,
            "position_idx": alerted["position_idx"] if "position_idx" in alerted.columns else None,
            "visit_start_ts": None,
            "visit_end_ts": None,
            "session_count": None,
            "bet_count": alerted["player_game_bet_count"],
            "visit_avg_bet": alerted["wager"] if "wager" in alerted.columns else None,
            "historical_avg_bet": None,
            "score": alerted["player_game_score"],
            "session_id": alerted["session_id"] if "session_id" in alerted.columns else None,
            "loss_streak": 0,
            "bets_last_5m": 0.0,
            "bets_last_15m": 0.0,
            "bets_last_30m": 0.0,
            "wager_last_10m": 0.0,
            "wager_last_30m": 0.0,
            "cum_bets": 0.0,
            "cum_wager": 0.0,
            "avg_wager_sofar": alerted["wager"] if "wager" in alerted.columns else None,
            "session_duration_min": 0.0,
            "bets_per_minute": 0.0,
            "canonical_id": alerted["canonical_id"] if "canonical_id" in alerted.columns else None,
            "is_rated_obs": np.where(rated, 1, 0),
            "reason_codes": None,
            "model_version": model_version,
            "margin": alerted["player_game_score"] - thr,
            "scored_at": scored_at_iso,
            "game_id": alerted["game_id"],
            "player_game_score": alerted["player_game_score"],
            "player_game_bet_count": alerted["player_game_bet_count"],
        }
    )
    return alerts, excluded


def score_once(
    conn: sqlite3.Connection,
    bundle: HightierModelBundle,
    *,
    feast_adapter: OnlineFeastAdapter,
    mapping_parquet: Path | None = None,
    manifest: ActiveSnapshotManifest | None = None,
    high_adt_only: bool,
    allowlist_ids: frozenset[int],
) -> int:
    """One incremental scoring cycle; returns number of alerts written."""
    from trainer_hightier.serving.flight_recorder import scorer_hooks as _flight_rec

    cfg = default_hightier_serving_config()
    cap = int(cfg.hightier_scorer_max_bets_per_cycle)
    last_etl = get_last_processed_etl_insert(conn)
    _flight_rec.on_score_once_begin(
        high_adt_only=high_adt_only,
        allowlist_ids=allowlist_ids,
        last_etl=last_etl,
    )
    batch = _fetch_scoring_batch(
        conn,
        high_adt_only=high_adt_only,
        allowlist_ids=allowlist_ids,
    )
    if batch is None:
        _flight_rec.on_score_once_empty()
        _record_scorer_cycle_metrics(
            model_version=str(bundle.model_version),
            cycle_readiness={},
            n_alerts=0,
            n_batch_rows=0,
            queue_drained=True,
        )
        return 0
    pool_ws = batch.pool_window_start or datetime.now(ZoneInfo(HK_TZ))
    pool_we = batch.pool_window_end or datetime.now(ZoneInfo(HK_TZ))
    _flight_rec.on_batch_ready(
        batch,
        last_etl=last_etl,
        high_adt_only=high_adt_only,
        allowlist_ids=allowlist_ids,
        pool_window_start=pool_ws,
        pool_window_end=pool_we,
    )
    n_batch_rows = len(batch.bets)
    queue_drained = _queue_drained_from_batch_rows(n_batch_rows, cap=cap)

    registry_snap = load_frozen_registry_for_bundle(Path(bundle.bundle_dir))
    supplier_plan = build_scorer_supplier_plan(registry_snap, bundle.feature_columns)
    assert_scorer_supplier_plan_or_raise(supplier_plan)
    staged = _build_staged_features(
        batch,
        mapping_parquet=mapping_parquet,
        supplier_plan=supplier_plan,
    )
    _flight_rec.on_stage(staged, "stage_05_staged_features")
    n_before_feast = len(staged)
    fail_frac = float(cfg.scorer_feast_entity_missing_fail_fraction)
    staged, skipped, feast_diag = _attach_feast_mid_slow(
        staged,
        feast_adapter,
        mid_columns=supplier_plan.feast_mid_cols,
        slow_columns=supplier_plan.feast_slow_cols,
        fail_fraction=fail_frac,
    )
    _flight_rec.on_stage(staged, "stage_06_feast_mid_slow_lookup")
    from trainer_hightier.serving.mid_term_bounded_asof import apply_mid_term_bounded_asof

    staged = apply_mid_term_bounded_asof(
        staged,
        mid_primitive_columns=supplier_plan.feast_mid_cols,
        n_days=int(cfg.production_mid_asof_backfill_days),
    )
    staged = attach_mid_term_composite_columns(staged, supplier_plan.mid_composite_cols)
    _flight_rec.on_stage(staged, "stage_07_after_composite_features")
    cycle_summary = build_cycle_readiness_summary(
        supplier_routes=scorer_supplier_route_counts(supplier_plan),
        feast_mid_columns=supplier_plan.feast_mid_cols,
        feast_slow_columns=supplier_plan.feast_slow_cols,
        short_term_columns=supplier_plan.short_term_cols,
        n_requested=n_before_feast,
        n_scored=len(staged),
        n_skipped_entity_missing=len(skipped),
        entity_missing_fail_fraction=fail_frac,
        feast_diag=feast_diag,
    )
    cycle_log = cycle_summary.to_log_dict()
    logger.debug("[hightier_scorer] cycle_readiness %s", cycle_log)
    scored_at_iso = datetime.now(ZoneInfo(HK_TZ)).isoformat()
    pl_path = cfg.prediction_log_db_path
    if skipped.shape[0] and pl_path is not None and str(pl_path).strip():
        try:
            append_skipped_entity_missing_log(
                pl_path,
                scored_at=scored_at_iso,
                model_version=str(bundle.model_version),
                skipped=skipped,
                feature_columns=bundle.feature_columns,
                threshold=float(bundle.threshold),
                feast_mid_cols=supplier_plan.feast_mid_cols,
                feast_slow_cols=supplier_plan.feast_slow_cols,
                short_term_cols=supplier_plan.short_term_cols,
                snapshot_version=manifest.version if manifest is not None else None,
            )
        except Exception as exc:
            logger.warning("[hightier_scorer] skipped prediction_log write failed: %s", exc)
    if staged.empty:
        _commit_scoring_cursor(conn, batch.cursor, batch.bets.index)
        conn.commit()
        _flight_rec.on_score_once_end(
            n_batch_rows=n_batch_rows,
            n_alerts=0,
            prob=None,
            staged=staged,
            features=None,
            feature_columns=bundle.feature_columns,
            supplier_plan=supplier_plan,
            row_audits=None,
            cycle_readiness=cycle_log,
        )
        _record_scorer_cycle_metrics(
            model_version=str(bundle.model_version),
            cycle_readiness=cycle_log,
            n_alerts=0,
            n_batch_rows=n_batch_rows,
            queue_drained=queue_drained,
        )
        return 0

    mid_cols = tuple(
        dict.fromkeys([*supplier_plan.feast_mid_cols, *supplier_plan.mid_composite_cols])
    )
    uses_feast_mid = bool(supplier_plan.feast_mid_cols or supplier_plan.mid_composite_cols)
    mid_path = manifest.mid_term_snapshot_parquet if manifest is not None else None
    mid_val = (
        SnapshotValidationResult(
            layer="mid_term",
            ok=True,
            hard_failure=False,
            status="fresh",
            message="feast online supplier",
        )
        if uses_feast_mid
        else (
            validate_mid_term_artifact(
                Path(mid_path) if mid_path is not None else None,
                manifest_grain=(manifest.raw.get("mid_term_grain") if manifest is not None else None),
            )
            if mid_cols
            else None
        )
    )
    slow_val = (
        SnapshotValidationResult(
            layer="slow_patron",
            ok=True,
            hard_failure=False,
            status="fresh",
            message="feast online supplier",
        )
        if supplier_plan.feast_slow_cols
        else (
            validate_slow_artifact(
                Path(manifest.slow_patron_parquet) if manifest is not None else None,
                manifest_grain=(manifest.raw.get("slow_patron_grain") if manifest is not None else None),
            )
            if any(f.startswith("patron__") for f in bundle.feature_columns)
            else SnapshotValidationResult(
                layer="slow_patron",
                ok=True,
                hard_failure=False,
                status="fresh",
                message="slow not required",
            )
        )
    )
    feast_readiness = (
        load_feast_online_readiness(resolve_feast_readiness_path(cfg))
        if (uses_feast_mid or supplier_plan.feast_slow_cols)
        else None
    )
    mid_anchor = (
        feast_readiness.mid_term.anchor_gaming_day_event_max
        if uses_feast_mid and feast_readiness and feast_readiness.mid_term
        else read_mid_term_anchor_max(Path(mid_path) if mid_path else None, manifest.raw if manifest else None)
    )
    slow_anchor = (
        feast_readiness.slow_patron.anchor_gaming_day_event_max
        if supplier_plan.feast_slow_cols and feast_readiness and feast_readiness.slow_patron
        else read_slow_anchor_max(
            Path(manifest.slow_patron_parquet) if manifest is not None else None,
            manifest.raw if manifest else None,
        )
    )
    mid_fresh = (
        evaluate_mid_term_freshness(
            anchor_max=mid_anchor,
            hard_cap_days=int(cfg.mid_term_stale_hard_cap_days),
            close_hour=int(cfg.gaming_day_close_hour),
        )
        if mid_cols
        else LayerFreshnessResult(
            layer="mid_term",
            status="fresh",
            staleness_days=0,
            anchor_max=None,
            message="mid_term not required",
        )
    )
    slow_fresh = (
        evaluate_slow_freshness(
            anchor_max=slow_anchor,
            monthly_grace_days=int(cfg.slow_monthly_grace_days),
            hard_cap_days=int(cfg.slow_stale_hard_cap_days),
            close_hour=int(cfg.gaming_day_close_hour),
        )
        if supplier_plan.feast_slow_cols
        else LayerFreshnessResult(
            layer="slow_patron",
            status="fresh",
            staleness_days=0,
            anchor_max=None,
            message="slow not required",
        )
    )
    gate = build_scoring_snapshot_gate(
        mid_term=mid_fresh,
        slow=slow_fresh,
        mid_validation=mid_val,
        slow_validation=slow_val,
    )
    if not gate.allow_scoring:
        raise RuntimeError(
            f"[hightier_scorer] snapshot gate blocked scoring: {gate.hard_failure_reason}"
        )
    if gate.degraded:
        logger.warning(
            "[hightier_scorer] degraded snapshot scoring mid=%s slow=%s mid_stale=%s slow_stale=%s",
            mid_fresh.status,
            slow_fresh.status,
            mid_fresh.staleness_days,
            slow_fresh.staleness_days,
        )
    smoke_failures = post_join_feature_smoke(staged, mid_term_columns=mid_cols)
    if smoke_failures:
        raise ValueError(
            "[hightier_scorer] post-join feature smoke failed: " + "; ".join(smoke_failures)
        )

    meta_set(conn, META_KEY_MID_TERM_FRESHNESS_STATUS, mid_fresh.status)
    meta_set(conn, META_KEY_SLOW_FRESHNESS_STATUS, slow_fresh.status)
    meta_set(conn, META_KEY_SNAPSHOT_SCORING_DEGRADED, "1" if gate.degraded else "0")
    if mid_anchor is not None:
        meta_set(conn, META_KEY_MID_TERM_ANCHOR_MAX, mid_anchor.isoformat())
    if slow_anchor is not None:
        meta_set(conn, META_KEY_SLOW_ANCHOR_MAX, slow_anchor.isoformat())
    if mid_fresh.staleness_days is not None:
        meta_set(conn, META_KEY_MID_TERM_STALENESS_DAYS, str(mid_fresh.staleness_days))
    if slow_fresh.staleness_days is not None:
        meta_set(conn, META_KEY_SLOW_STALENESS_DAYS, str(slow_fresh.staleness_days))

    assert_features_ready(staged, bundle.feature_columns)
    X = prepare_lgbm_feature_matrix(
        staged,
        feature_columns=bundle.feature_columns,
        categorical_columns=bundle.categorical_columns,
        category_categories=dict(bundle.category_categories),
    )
    _flight_rec.on_stage(X, "stage_08_model_feature_matrix")
    prob = bundle.model.predict_proba(X)[:, 1]
    thr = float(bundle.threshold)
    row_audits = compute_row_missing_audits(
        X,
        bundle.feature_columns,
        feast_mid_cols=supplier_plan.feast_mid_cols,
        feast_slow_cols=supplier_plan.feast_slow_cols,
        short_term_cols=supplier_plan.short_term_cols,
    )
    from trainer_hightier.feature_experiment.feature_cadence import runtime_inputs_from_registry
    from trainer_hightier.serving.feast_online_adapter import enrich_row_audits_composite_upstream

    _reg_by_id = {r.feature_id: r for r in registry_snap.rows}
    runtime_inputs_map = {
        comp: runtime_inputs_from_registry(_reg_by_id.get(comp), comp)
        for comp in supplier_plan.mid_composite_cols
    }
    row_audits = enrich_row_audits_composite_upstream(
        staged,
        row_audits,
        composite_cols=supplier_plan.mid_composite_cols,
        runtime_inputs_by_feature=runtime_inputs_map,
    )
    alerts, excluded_pg = _build_player_game_alert_frame(
        staged,
        prob,
        threshold=thr,
        scored_at_iso=scored_at_iso,
        model_version=str(bundle.model_version),
    )
    policy = cfg.player_alert_policy
    raised_alerts, _suppressed_alerts, alert_policy_decisions = apply_serving_player_alert_suppression(
        alerts,
        conn=conn,
        suppression_enabled=bool(policy.suppression_enabled),
        cooldown_min=int(policy.cooldown_min),
    )
    n = int(len(raised_alerts))
    if pl_path is not None and str(pl_path).strip():
        try:
            from trainer_hightier.serving.feast_readiness import compute_batch_mid_null_top_features

            mid_top = (
                compute_batch_mid_null_top_features(staged, supplier_plan.feast_mid_cols)
                if supplier_plan.feast_mid_cols
                else []
            )
            mid_top_json = (
                json.dumps(mid_top, separators=(",", ":"), ensure_ascii=False) if mid_top else None
            )
            append_hightier_prediction_log(
                pl_path,
                scored_at=scored_at_iso,
                model_version=str(bundle.model_version),
                staged=staged,
                prob=prob,
                threshold=thr,
                features=X,
                feature_columns=bundle.feature_columns,
                row_audits=row_audits,
                scoring_status="scored",
                snapshot_version=manifest.version if manifest is not None else None,
                mid_term_freshness_status=mid_fresh.status,
                slow_freshness_status=slow_fresh.status,
                snapshot_scoring_degraded=gate.degraded,
                mid_term_anchor_gaming_day_event_max=(
                    mid_anchor.isoformat() if mid_anchor is not None else None
                ),
                mid_term_snapshot_age_days=mid_fresh.staleness_days,
                mid_null_top_features_json=mid_top_json,
                alert_policy_decisions=alert_policy_decisions,
            )
        except Exception as exc:
            logger.warning("[hightier_scorer] prediction_log write failed: %s", exc)
    scored_indices = staged.index
    if excluded_pg > 0:
        cycle_log = dict(cycle_log)
        cycle_log["excluded_bets_player_game"] = excluded_pg
    if n == 0:
        _commit_scoring_cursor(conn, batch.cursor, scored_indices)
        conn.commit()
        _flight_rec.on_score_once_end(
            n_batch_rows=n_batch_rows,
            n_alerts=0,
            prob=prob,
            staged=staged,
            features=X,
            feature_columns=bundle.feature_columns,
            supplier_plan=supplier_plan,
            row_audits=row_audits,
            cycle_readiness=cycle_log,
        )
        _record_scorer_cycle_metrics(
            model_version=str(bundle.model_version),
            cycle_readiness=cycle_log,
            n_alerts=0,
            n_batch_rows=n_batch_rows,
            queue_drained=queue_drained,
        )
        return 0
    append_alerts(conn, raised_alerts)
    _commit_scoring_cursor(conn, batch.cursor, scored_indices)
    conn.commit()
    _flight_rec.on_score_once_end(
        n_batch_rows=n_batch_rows,
        n_alerts=n,
        prob=prob,
        staged=staged,
        features=X,
        feature_columns=bundle.feature_columns,
        supplier_plan=supplier_plan,
        row_audits=row_audits,
        cycle_readiness=cycle_log,
    )
    _record_scorer_cycle_metrics(
        model_version=str(bundle.model_version),
        cycle_readiness=cycle_log,
        n_alerts=n,
        n_batch_rows=n_batch_rows,
        queue_drained=queue_drained,
    )
    if n > 0:
        logger.info("[hightier_scorer] wrote %d alerts (threshold=%.6f)", n, thr)
    else:
        logger.debug("[hightier_scorer] wrote %d alerts (threshold=%.6f)", n, thr)
    return n


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pr = argparse.ArgumentParser(description="trainer_hightier ClickHouse scorer daemon")
    pr.add_argument("--once", action="store_true", help="single cycle then exit")
    pr.add_argument("--bundle-dir", type=Path, default=None)
    pr.add_argument("--slow-parquet", type=Path, default=None, help="deprecated: mid/long use Feast online lookup")
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
    pr.add_argument(
        "--feast-deploy-smoke",
        action="store_true",
        help="run Feast readiness + allowlist online lookup smoke then exit (no scoring)",
    )
    pr.add_argument(
        "--refresh-feast-readiness",
        action="store_true",
        help="rebuild feast_online_readiness.json from spike reports then exit",
    )
    pr.add_argument(
        "--dry-run-report",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="after --once, write scorer_dry_run_report.json (optional PATH)",
    )
    args = pr.parse_args(argv)
    cfg = default_hightier_serving_config()
    init_state_db(Path(cfg.state_db_path))
    init_prediction_log_db(cfg.prediction_log_db_path)
    bundle = load_hightier_model_bundle(bundle_dir=args.bundle_dir)
    warn_player_alert_policy_mismatch(
        logger,
        training_metrics=bundle.training_metrics,
        serving_policy=cfg.player_alert_policy,
    )
    map_path = Path(args.canonical_mapping).resolve() if args.canonical_mapping else None
    cli_al = Path(args.adt_allowlist).resolve() if args.adt_allowlist else None
    high_adt_only = bool(cfg.high_adt_only) and (not bool(args.no_high_adt_only))
    feast_adapter = build_production_feast_adapter()
    registry_snap = load_frozen_registry_for_bundle(Path(bundle.bundle_dir))
    supplier_plan = build_scorer_supplier_plan(registry_snap, bundle.feature_columns)
    assert_scorer_supplier_plan_or_raise(supplier_plan)
    if args.slow_parquet is not None:
        logger.warning("[hightier_scorer] --slow-parquet is ignored in scorer v2 (Feast mid/long supplier)")
    if args.refresh_feast_readiness:
        from trainer_hightier.serving.feast_readiness import refresh_readiness_from_spike_reports

        doc = refresh_readiness_from_spike_reports()
        logger.info("[hightier_scorer] feast_readiness refreshed %s", doc.to_dict())
        return 0
    if cfg.scorer_feast_schema_smoke_enabled and (
        supplier_plan.feast_mid_cols or supplier_plan.feast_slow_cols
    ):
        smoke = run_feast_scorer_schema_smoke_check(
            default_feast_repo_path(),
            mid_columns=supplier_plan.feast_mid_cols,
            slow_columns=supplier_plan.feast_slow_cols,
            probe_canonical_id=str(cfg.scorer_feast_schema_smoke_probe_canonical_id),
        )
        logger.info("[hightier_scorer] feast_schema_smoke ok %s", smoke.to_log_dict())
    if cfg.scorer_feast_readiness_enabled and (
        supplier_plan.feast_mid_cols or supplier_plan.feast_slow_cols
    ):
        feast_gate = evaluate_feast_readiness_gate(
            load_feast_online_readiness(resolve_feast_readiness_path(cfg)),
            require_mid=bool(supplier_plan.feast_mid_cols),
            require_slow=bool(supplier_plan.feast_slow_cols),
            readiness_path=resolve_feast_readiness_path(cfg),
            close_hour=int(cfg.gaming_day_close_hour),
            mid_hard_cap_days=int(cfg.mid_term_stale_hard_cap_days),
            slow_hard_cap_days=int(cfg.slow_stale_hard_cap_days),
            slow_grace_days=int(cfg.slow_monthly_grace_days),
        )
        if not feast_gate.ok:
            raise RuntimeError(feast_gate.hard_failure_reason)
        logger.info("[hightier_scorer] feast_readiness ok %s", feast_gate.to_log_dict())
    if args.feast_deploy_smoke:
        if map_path is None or cli_al is None:
            raise SystemExit("--feast-deploy-smoke requires --canonical-mapping and --adt-allowlist")
        deploy_gate = run_deploy_feast_readiness_check(
            require_mid=bool(supplier_plan.feast_mid_cols),
            require_slow=bool(supplier_plan.feast_slow_cols),
            allowlist_parquet=cli_al,
            canonical_mapping_parquet=map_path,
            mid_columns=supplier_plan.feast_mid_cols,
            slow_columns=supplier_plan.feast_slow_cols,
            run_lookup_smoke=True,
        )
        logger.info("[hightier_scorer] feast_deploy_smoke %s", deploy_gate.to_log_dict())
        return 0 if deploy_gate.ok else 1
    _log_scorer_readiness_summary(bundle=bundle, supplier_plan=supplier_plan)
    al_cache: dict[str, Any] = {}
    boot_logged = False
    cycle_num = 0
    cycle_t0 = time.perf_counter() if args.dry_run_report is not None else None

    while True:
        cycle_num += 1
        cycle_loop_t0 = time.perf_counter()
        sleep_s = float(cfg.scorer_poll_interval_seconds)
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

            allow_ids = al_cache.get("ids", frozenset()) if high_adt_only else frozenset()
            if high_adt_only and not isinstance(allow_ids, frozenset):
                allow_ids = frozenset(allow_ids)
            score_once(
                conn,
                bundle,
                feast_adapter=feast_adapter,
                mapping_parquet=map_path,
                manifest=man,
                high_adt_only=high_adt_only,
                allowlist_ids=allow_ids,
            )
            metrics = get_last_scorer_cycle_metrics() or {}
            batch_rows = int(metrics.get("n_batch_rows") or 0)
            sleep_s = compute_scorer_cycle_sleep_seconds(batch_rows=batch_rows, cfg=cfg)
            elapsed_s = round(time.perf_counter() - cycle_loop_t0, 1)
            _log_scorer_cycle_summary(
                cycle_num=cycle_num,
                metrics=metrics,
                sleep_s=sleep_s,
                elapsed_s=elapsed_s,
                cfg=cfg,
            )
        finally:
            conn.close()
        if args.once:
            if args.dry_run_report is not None:
                from trainer_hightier.serving.scorer_dry_run import (
                    build_dry_run_report_from_cycle,
                    default_scorer_dry_run_report_path,
                    write_scorer_dry_run_report,
                )

                metrics = get_last_scorer_cycle_metrics()
                if metrics is None:
                    logger.warning("[hightier_scorer] dry-run-report skipped: no cycle metrics recorded")
                else:
                    elapsed = (
                        round(time.perf_counter() - cycle_t0, 3) if cycle_t0 is not None else None
                    )
                    report = build_dry_run_report_from_cycle(
                        model_version=str(metrics["model_version"]),
                        cycle_readiness=dict(metrics["cycle_readiness"]),
                        n_alerts=int(metrics["n_alerts"]),
                        elapsed_seconds=elapsed,
                        feast_readiness_path=resolve_feast_readiness_path(cfg),
                        notes="scorer --once --dry-run-report",
                    )
                    out = (
                        default_scorer_dry_run_report_path()
                        if args.dry_run_report == ""
                        else Path(args.dry_run_report)
                    )
                    write_scorer_dry_run_report(report, out)
            break
        time.sleep(sleep_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
