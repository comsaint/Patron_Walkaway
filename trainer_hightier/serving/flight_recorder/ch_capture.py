"""ClickHouse query metadata + result capture for flight recorder."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Optional, Sequence

import pandas as pd
from zoneinfo import ZoneInfo

from trainer_hightier.config import HK_TZ, HightierServingConfig, default_hightier_serving_config
from trainer_hightier.serving.ch_adapter import (
    CH_TBET_CASINO_WIN_SELECT,
    CH_TBET_GAMING_DAY_EVENT_NOT_NULL_PRED,
    CH_TBET_GAMING_DAY_EVENT_SELECT,
    CH_TBET_PAYOUT_ODDS_SELECT,
    CH_TBET_WAGER_SELECT,
    ch_tbet_gaming_day_event_sql,
)
from trainer_hightier.serving.flight_recorder.parquet_io import write_parquet_safe
from trainer_hightier.serving.flight_recorder.redact import redact_sql, redact_value

# ``t_bet`` does not expose loyalty/casino id in all deployed warehouses. Keep the
# downstream replay schema stable without assuming the raw ClickHouse column exists.
_TBET_CASINO_PLAYER_ID_SELECT = "CAST(NULL AS Nullable(String)) AS casino_player_id"
BUSINESS_KEY_BET_ID: Final[str] = "bet_id"


def _incremental_select_cols() -> str:
    """SELECT column list aligned with scorer incremental fetch."""
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
                {CH_TBET_WAGER_SELECT},
                {CH_TBET_CASINO_WIN_SELECT},
                {CH_TBET_PAYOUT_ODDS_SELECT},
                status,
                {_TBET_CASINO_PLAYER_ID_SELECT}
    """.strip()


def _pool_select_cols() -> str:
    """SELECT column list aligned with scorer ``fetch_bet_pool_window``."""
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
                {CH_TBET_WAGER_SELECT},
                {CH_TBET_CASINO_WIN_SELECT},
                {CH_TBET_PAYOUT_ODDS_SELECT},
                status,
                {_TBET_CASINO_PLAYER_ID_SELECT}
    """.strip()


def _strip_final(sql: str) -> str:
    """Remove ``FINAL`` modifier for diagnostic non-final replay."""
    return re.sub(r"\bFINAL\b", "", sql, flags=re.IGNORECASE)


def default_business_key(fetch: str) -> str:
    """Return default diff key for a fetch type."""
    return BUSINESS_KEY_BET_ID


def _infer_skip_reason(record: dict[str, Any]) -> str | None:
    """Infer why a manifest cannot be re-executed when ``requeryable`` is unset."""
    if str(record.get("error") or "").strip():
        return str(record["error"])
    mode = str(record.get("mode", ""))
    if mode == "empty_allowlist":
        return "empty_allowlist"
    sql = str(record.get("sql_final") or record.get("sql") or "").strip()
    if not sql:
        return "empty_sql"
    if "..." in sql:
        return "placeholder_sql_not_executable"
    fetch = str(record.get("fetch", ""))
    ext = dict(record.get("external_inputs") or {})
    if fetch == "fetch_bet_pool_window":
        if not ext.get("player_ids"):
            return "pool_window_missing_player_id_list"
    if fetch == "fetch_bets_incremental" and mode.startswith("allowlist"):
        if not ext.get("allowlist_player_ids"):
            return "allowlist_external_input_missing_payload"
    if fetch == "fetch_bets_by_canonical_id":
        if not ext.get("player_ids"):
            return "validator_canonical_missing_player_ids"
    if fetch == "fetch_bet_payout_times_by_bet_ids":
        if not ext.get("bet_ids"):
            return "validator_bet_id_lookup_missing_bet_ids"
    return None


def finalize_query_manifest(record: dict[str, Any], *, cfg: HightierServingConfig | None = None) -> dict[str, Any]:
    """Add replay-contract fields (``sql_final``, ``requeryable``, ``business_key``, …)."""
    cfg = cfg or default_hightier_serving_config()
    out = dict(record)
    sql_raw = str(out.get("sql_final") or out.get("sql") or "").strip()
    out["sql_final"] = redact_sql(sql_raw) if sql_raw else ""
    out["sql_non_final"] = _strip_final(out["sql_final"]) if out["sql_final"] else ""
    out["sql"] = out["sql_final"]
    out.setdefault("source_table", cfg.tbet)
    out.setdefault("result_table", cfg.tbet)
    out.setdefault("business_key", default_business_key(str(out.get("fetch", ""))))
    ext = dict(out.get("external_inputs") or {})
    out["external_inputs"] = redact_value(ext)
    if "requeryable" not in out:
        skip = _infer_skip_reason(out)
        out["requeryable"] = skip is None
        if skip is not None:
            out["skip_reason"] = skip
    elif out["requeryable"] is False and not out.get("skip_reason"):
        out["skip_reason"] = _infer_skip_reason(out) or "not_requeryable"
    return out


def build_pool_chunk_sql(
    player_ids: Sequence[int],
    *,
    use_final: bool = True,
    cfg: HightierServingConfig | None = None,
) -> str:
    """Executable pool-window SQL for one player-id chunk."""
    cfg = cfg or default_hightier_serving_config()
    placeholder = int(cfg.placeholder_player_id)
    in_list = ",".join(str(int(x)) for x in player_ids)
    final_kw = " FINAL" if use_final else ""
    return f"""
            SELECT
                {_pool_select_cols()}
            FROM {cfg.source_db}.{cfg.tbet}{final_kw}
            WHERE payout_complete_dtm >= %(ws)s
              AND payout_complete_dtm <= %(we)s
              AND payout_complete_dtm IS NOT NULL
              AND {CH_TBET_GAMING_DAY_EVENT_NOT_NULL_PRED}
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
              AND player_id IN ({in_list})
            ORDER BY payout_complete_dtm ASC, bet_id ASC
        """


def build_incremental_query_record(
    *,
    last_etl: Optional[pd.Timestamp],
    lookback_hours: float,
    limit_rows: int,
    allowlist_player_ids: Optional[frozenset[int]] = None,
    allowlist_join_mode: str | None = None,
    cfg: HightierServingConfig | None = None,
) -> dict[str, Any]:
    """Build redacted SQL/parameters metadata for incremental ``t_bet FINAL`` fetch."""
    cfg = cfg or default_hightier_serving_config()
    now_hk = datetime.now(ZoneInfo(HK_TZ))
    bet_avail = now_hk - timedelta(minutes=int(cfg.bet_avail_delay_min))
    start = now_hk - timedelta(hours=float(lookback_hours))
    params: dict[str, Any] = {
        "start": start.isoformat(),
        "bet_avail": bet_avail.isoformat(),
        "lim": int(max(1, limit_rows)),
    }
    etl_filter = ""
    if last_etl is not None:
        etl_filter = "AND __etl_insert_Dtm > %(last_etl)s"
        params["last_etl"] = pd.Timestamp(last_etl).isoformat()
    select_cols = _incremental_select_cols()
    placeholder = int(cfg.placeholder_player_id)
    external_inputs: dict[str, Any] = {}
    if allowlist_player_ids is None:
        sql = f"""
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
        mode = "global"
    elif not allowlist_player_ids:
        return finalize_query_manifest(
            {"fetch": "fetch_bets_incremental", "mode": "empty_allowlist", "final": True},
            cfg=cfg,
        )
    else:
        ids_sorted = sorted(int(x) for x in allowlist_player_ids)
        external_inputs["allowlist_player_ids"] = ids_sorted
        join_mode = str(allowlist_join_mode or "external_input").strip().lower()
        etl_filter_t = etl_filter.replace("__etl_insert_Dtm", "t.__etl_insert_Dtm") if etl_filter else ""
        if join_mode == "chunk":
            in_list = ",".join(str(x) for x in ids_sorted[: min(len(ids_sorted), 32)])
            if len(ids_sorted) > 32:
                in_list = f"{in_list} /* +{len(ids_sorted) - 32} more ids in external_inputs */"
            sql = f"""
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
            mode = "allowlist_chunk"
        else:
            sql = f"""
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
            mode = "allowlist_external_input"
    record = {
        "fetch": "fetch_bets_incremental",
        "mode": mode,
        "final": True,
        "sql": redact_sql(sql),
        "parameters": redact_value(params),
        "lookback_hours": float(lookback_hours),
        "limit_rows": int(limit_rows),
        "external_inputs": external_inputs,
    }
    return finalize_query_manifest(record, cfg=cfg)


def build_pool_query_record(
    *,
    player_ids: list[int],
    window_start: datetime,
    window_end: datetime,
    cfg: HightierServingConfig | None = None,
) -> dict[str, Any]:
    """Build redacted SQL/parameters metadata for short-term pool fetch."""
    cfg = cfg or default_hightier_serving_config()
    unique_ids = sorted({int(x) for x in player_ids})
    chunk_sz = int(cfg.hightier_scorer_player_id_chunk_size)
    chunk = unique_ids[:chunk_sz] if unique_ids else []
    sql = build_pool_chunk_sql(chunk, use_final=True, cfg=cfg) if chunk else ""
    record = {
        "fetch": "fetch_bet_pool_window",
        "final": True,
        "sql": redact_sql(sql) if sql else "",
        "parameters": redact_value(
            {
                "ws": window_start.isoformat(),
                "we": window_end.isoformat(),
                "n_players": len(unique_ids),
                "n_chunks": max(1, (len(unique_ids) + cfg.hightier_scorer_player_id_chunk_size - 1)
                                // cfg.hightier_scorer_player_id_chunk_size)
                if unique_ids
                else 0,
            }
        ),
        "external_inputs": {"player_ids": unique_ids},
    }
    return finalize_query_manifest(record, cfg=cfg)


def build_validator_canonical_query_record(
    *,
    player_ids: Sequence[int],
    start: datetime,
    end: datetime,
    cfg: HightierServingConfig | None = None,
) -> dict[str, Any]:
    """Build metadata for ``fetch_bets_by_canonical_id`` (TBET FINAL)."""
    cfg = cfg or default_hightier_serving_config()
    placeholder = int(cfg.placeholder_player_id)
    unique = sorted({int(x) for x in player_ids})
    sql = f"""
            SELECT bet_id, player_id, payout_complete_dtm
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE player_id IN %(players)s
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
              AND payout_complete_dtm >= %(start)s
              AND payout_complete_dtm <= %(end)s
              AND payout_complete_dtm IS NOT NULL
              AND wager > 0
            ORDER BY player_id, payout_complete_dtm
        """
    record = {
        "fetch": "fetch_bets_by_canonical_id",
        "final": True,
        "sql": redact_sql(sql),
        "parameters": redact_value(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        ),
        "external_inputs": {"player_ids": unique},
    }
    return finalize_query_manifest(record, cfg=cfg)


def build_validator_bet_id_query_record(
    *,
    bet_ids: Sequence[int],
    cfg: HightierServingConfig | None = None,
) -> dict[str, Any]:
    """Build metadata for ``fetch_bet_payout_times_by_bet_ids`` (no-bet retry path)."""
    cfg = cfg or default_hightier_serving_config()
    placeholder = int(cfg.placeholder_player_id)
    unique = sorted({int(x) for x in bet_ids})
    sql = f"""
            SELECT bet_id, payout_complete_dtm, player_id
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE bet_id IN %(ids)s
              AND payout_complete_dtm IS NOT NULL
              AND wager > 0
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
        """
    record = {
        "fetch": "fetch_bet_payout_times_by_bet_ids",
        "lookup": "VALIDATOR_NO_BET_BET_ID_LOOKUP",
        "final": True,
        "sql": redact_sql(sql),
        "parameters": redact_value({}),
        "external_inputs": {"bet_ids": unique},
    }
    return finalize_query_manifest(record, cfg=cfg)


def save_clickhouse_capture(
    ch_dir: Path,
    basename: str,
    frame: pd.DataFrame,
    query_record: dict[str, Any],
) -> None:
    """Write ``{basename}.query.json`` and ``{basename}.final.parquet`` under *ch_dir*."""
    ch_dir.mkdir(parents=True, exist_ok=True)
    manifest = finalize_query_manifest(query_record)
    query_path = ch_dir / f"{basename}.query.json"
    query_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    parquet_path = ch_dir / f"{basename}.final.parquet"
    write_parquet_safe(parquet_path, frame if frame is not None else pd.DataFrame())
