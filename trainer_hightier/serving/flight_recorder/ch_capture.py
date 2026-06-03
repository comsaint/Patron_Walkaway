"""ClickHouse query metadata + result capture for flight recorder."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

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
from trainer_hightier.serving.flight_recorder.redact import redact_sql, redact_value
from trainer_hightier.serving.flight_recorder.parquet_io import write_parquet_safe

_TBET_CASINO_PLAYER_ID_SELECT = "casino_player_id"


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


def build_incremental_query_record(
    *,
    last_etl: Optional[pd.Timestamp],
    lookback_hours: float,
    limit_rows: int,
    allowlist_player_ids: Optional[frozenset[int]] = None,
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
        return {"fetch": "fetch_bets_incremental", "mode": "empty_allowlist", "final": True}
    else:
        etl_filter_t = etl_filter.replace("__etl_insert_Dtm", "t.__etl_insert_Dtm") if etl_filter else ""
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
        params["allowlist_size"] = len(allowlist_player_ids)
    return {
        "fetch": "fetch_bets_incremental",
        "mode": mode,
        "final": True,
        "sql": redact_sql(sql),
        "parameters": redact_value(params),
        "lookback_hours": float(lookback_hours),
        "limit_rows": int(limit_rows),
    }


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
    placeholder = int(cfg.placeholder_player_id)
    in_list = ",".join(str(x) for x in unique_ids[: min(len(unique_ids), 32)])
    if len(unique_ids) > 32:
        in_list = f"{in_list},...({len(unique_ids)} players total)"
    sql = f"""
            SELECT bet_id, player_id, game_id, ...
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE payout_complete_dtm >= %(ws)s
              AND payout_complete_dtm <= %(we)s
              AND player_id IN ({in_list})
        """
    return {
        "fetch": "fetch_bet_pool_window",
        "final": True,
        "sql": redact_sql(sql),
        "parameters": redact_value(
            {
                "ws": window_start.isoformat(),
                "we": window_end.isoformat(),
                "n_players": len(unique_ids),
                "n_chunks": max(1, (len(unique_ids) + cfg.hightier_scorer_player_id_chunk_size - 1)
                                // cfg.hightier_scorer_player_id_chunk_size),
            }
        ),
    }


def build_validator_canonical_query_record(
    *,
    n_players: int,
    start: datetime,
    end: datetime,
    cfg: HightierServingConfig | None = None,
) -> dict[str, Any]:
    """Build metadata for ``fetch_bets_by_canonical_id`` (TBET FINAL)."""
    cfg = cfg or default_hightier_serving_config()
    placeholder = int(cfg.placeholder_player_id)
    sql = f"""
            SELECT player_id, payout_complete_dtm
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
    return {
        "fetch": "fetch_bets_by_canonical_id",
        "final": True,
        "sql": redact_sql(sql),
        "parameters": redact_value(
            {
                "n_players": n_players,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        ),
    }


def build_validator_bet_id_query_record(*, n_bet_ids: int, cfg: HightierServingConfig | None = None) -> dict[str, Any]:
    """Build metadata for ``fetch_bet_payout_times_by_bet_ids`` (no-bet retry path)."""
    cfg = cfg or default_hightier_serving_config()
    sql = f"""
            SELECT bet_id, payout_complete_dtm, player_id
            FROM {cfg.source_db}.{cfg.tbet} FINAL
            WHERE bet_id IN %(ids)s
              AND payout_complete_dtm IS NOT NULL
              AND wager > 0
        """
    return {
        "fetch": "fetch_bet_payout_times_by_bet_ids",
        "lookup": "VALIDATOR_NO_BET_BET_ID_LOOKUP",
        "final": True,
        "sql": redact_sql(sql),
        "parameters": redact_value({"n_bet_ids": n_bet_ids}),
    }


def save_clickhouse_capture(
    ch_dir: Path,
    basename: str,
    frame: pd.DataFrame,
    query_record: dict[str, Any],
) -> None:
    """Write ``{basename}.query.json`` and ``{basename}.final.parquet`` under *ch_dir*."""
    ch_dir.mkdir(parents=True, exist_ok=True)
    query_path = ch_dir / f"{basename}.query.json"
    query_path.write_text(json.dumps(query_record, indent=2, default=str), encoding="utf-8")
    parquet_path = ch_dir / f"{basename}.final.parquet"
    write_parquet_safe(parquet_path, frame if frame is not None else pd.DataFrame())
