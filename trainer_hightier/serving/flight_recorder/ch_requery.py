"""Rebuild and execute ClickHouse diagnostic queries for time-machine."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from trainer_hightier.serving.ch_adapter import get_clickhouse_client
from trainer_hightier.serving.flight_recorder.ch_capture import (
    build_incremental_query_record,
    build_pool_query_record,
    build_validator_bet_id_query_record,
    build_validator_canonical_query_record,
)


def _parse_dt(value: Any) -> Any:
    """Parse ISO datetime strings for ClickHouse parameters."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if not text:
        return value
    try:
        return pd.Timestamp(text).to_pydatetime()
    except (TypeError, ValueError):
        return value


def _coerce_params(params: dict[str, Any]) -> dict[str, Any]:
    """Coerce stored parameter strings back to runtime types."""
    out: dict[str, Any] = {}
    for key, val in params.items():
        if key in ("start", "bet_avail", "ws", "we", "last_etl"):
            out[key] = _parse_dt(val)
        else:
            out[key] = val
    return out


def rebuild_query_record(fetch: str, query_meta: dict[str, Any]) -> dict[str, Any]:
    """Rebuild query metadata from stored ``fetch`` + ``parameters``."""
    params = _coerce_params(dict(query_meta.get("parameters") or {}))
    if fetch == "fetch_bets_incremental":
        mode = str(query_meta.get("mode", "global"))
        allowlist = None
        if mode.startswith("allowlist"):
            size = int(params.get("allowlist_size", 0))
            allowlist = frozenset(range(size)) if size > 0 else frozenset()
        lookback = float(query_meta.get("lookback_hours", params.get("lookback_hours", 6.0)))
        limit_rows = int(query_meta.get("limit_rows", params.get("lim", 1000)))
        return build_incremental_query_record(
            last_etl=_parse_dt(params.get("last_etl")),
            lookback_hours=lookback,
            limit_rows=limit_rows,
            allowlist_player_ids=allowlist if mode != "global" else None,
        )
    if fetch == "fetch_bet_pool_window":
        return build_pool_query_record(
            player_ids=list(range(int(params.get("n_players", 0)))),
            window_start=_parse_dt(params["ws"]),
            window_end=_parse_dt(params["we"]),
        )
    if fetch == "fetch_bets_by_canonical_id":
        return build_validator_canonical_query_record(
            n_players=int(params.get("n_players", 0)),
            start=_parse_dt(params["start"]),
            end=_parse_dt(params["end"]),
        )
    if fetch == "fetch_bet_payout_times_by_bet_ids":
        return build_validator_bet_id_query_record(
            n_bet_ids=int(params.get("n_bet_ids", 0)),
        )
    return {"fetch": fetch, "error": "unknown_fetch", "final": True, "sql": "", "parameters": params}


def _apply_final_modifier(sql: str, *, use_final: bool) -> str:
    """Toggle ``FINAL`` modifier on ``t_bet`` reads."""
    if use_final:
        return sql
    return re.sub(r"\bFINAL\b", "", sql, flags=re.IGNORECASE)


def execute_query(
    query_meta: dict[str, Any],
    *,
    use_final: bool,
) -> pd.DataFrame:
    """Run rebuilt SQL against ClickHouse; empty frame when client unavailable."""
    client = get_clickhouse_client()
    sql = str(query_meta.get("sql") or "").strip()
    if not sql:
        return pd.DataFrame()
    sql = _apply_final_modifier(sql, use_final=use_final)
    params = _coerce_params(dict(query_meta.get("parameters") or {}))
    mode = str(query_meta.get("mode", ""))
    if mode == "allowlist_external_input":
        return pd.DataFrame()
    try:
        return client.query_df(sql, parameters=params)
    except Exception:
        return pd.DataFrame()
