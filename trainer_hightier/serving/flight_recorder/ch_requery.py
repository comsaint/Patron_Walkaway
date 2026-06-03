"""Rebuild and execute ClickHouse diagnostic queries for time-machine."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

from trainer_hightier.config import default_hightier_serving_config
from trainer_hightier.serving.ch_adapter import get_clickhouse_client
from trainer_hightier.serving.flight_recorder.ch_capture import (
    build_incremental_query_record,
    build_pool_chunk_sql,
    build_pool_query_record,
    build_validator_bet_id_query_record,
    build_validator_canonical_query_record,
    finalize_query_manifest,
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
        if key in ("start", "bet_avail", "ws", "we", "last_etl", "end"):
            out[key] = _parse_dt(val)
        else:
            out[key] = val
    return out


def _allowlist_external_data(player_ids: Sequence[int]) -> Any:
    """Build ClickHouse external input for allowlist join replay."""
    from clickhouse_connect.driver.external import ExternalData

    lines = "\n".join(str(int(x)) for x in sorted({int(x) for x in player_ids}))
    payload = (lines + "\n").encode("utf-8") if lines else b""
    return ExternalData(
        file_name="adt_allowlist.tsv",
        data=payload,
        fmt="TSV",
        structure=["player_id Int64"],
    )


def rebuild_query_record(fetch: str, query_meta: dict[str, Any]) -> dict[str, Any]:
    """Return executable manifest; legacy records are upgraded via ``finalize_query_manifest``."""
    meta = finalize_query_manifest({**dict(query_meta), "fetch": fetch or query_meta.get("fetch", "")})
    if meta.get("sql_final"):
        return meta
    params = _coerce_params(dict(meta.get("parameters") or {}))
    ext = dict(meta.get("external_inputs") or {})
    if fetch == "fetch_bets_incremental":
        mode = str(meta.get("mode", "global"))
        allowlist = None
        if mode.startswith("allowlist"):
            ids = ext.get("allowlist_player_ids") or []
            allowlist = frozenset(int(x) for x in ids)
        return build_incremental_query_record(
            last_etl=_parse_dt(params.get("last_etl")),
            lookback_hours=float(meta.get("lookback_hours", params.get("lookback_hours", 6.0))),
            limit_rows=int(meta.get("limit_rows", params.get("lim", 1000))),
            allowlist_player_ids=allowlist if mode != "global" else None,
            allowlist_join_mode="chunk" if mode == "allowlist_chunk" else "external_input",
        )
    if fetch == "fetch_bet_pool_window":
        return build_pool_query_record(
            player_ids=[int(x) for x in ext.get("player_ids") or []],
            window_start=_parse_dt(params["ws"]),
            window_end=_parse_dt(params["we"]),
        )
    if fetch == "fetch_bets_by_canonical_id":
        return build_validator_canonical_query_record(
            player_ids=[int(x) for x in ext.get("player_ids") or []],
            start=_parse_dt(params["start"]),
            end=_parse_dt(params["end"]),
        )
    if fetch == "fetch_bet_payout_times_by_bet_ids":
        return build_validator_bet_id_query_record(
            bet_ids=[int(x) for x in ext.get("bet_ids") or []],
        )
    return meta


def requery_skip_reason(query_meta: dict[str, Any]) -> str | None:
    """Return a skip reason when this window cannot be re-executed; ``None`` if OK."""
    meta = finalize_query_manifest(dict(query_meta))
    if meta.get("requeryable") is False:
        return str(meta.get("skip_reason") or "not_requeryable")
    if meta.get("requeryable") is True:
        return None
    return _infer_skip_reason_legacy(meta)


def _infer_skip_reason_legacy(query_meta: dict[str, Any]) -> str | None:
    """Backward-compatible skip heuristics for manifests without ``requeryable``."""
    from trainer_hightier.serving.flight_recorder.ch_capture import _infer_skip_reason

    return _infer_skip_reason(query_meta)


def _pick_sql(query_meta: dict[str, Any], *, use_final: bool) -> str:
    """Select FINAL or non-FINAL SQL from manifest."""
    if use_final:
        return str(query_meta.get("sql_final") or query_meta.get("sql") or "").strip()
    non_final = str(query_meta.get("sql_non_final") or "").strip()
    if non_final:
        return non_final
    return re.sub(r"\bFINAL\b", "", str(query_meta.get("sql_final") or query_meta.get("sql") or ""), flags=re.I)


def _execute_incremental(client: Any, meta: dict[str, Any], *, use_final: bool) -> pd.DataFrame:
    """Run incremental fetch replay (global, allowlist external, or chunk)."""
    params = _coerce_params(dict(meta.get("parameters") or {}))
    ext = dict(meta.get("external_inputs") or {})
    mode = str(meta.get("mode", "global"))
    sql = _pick_sql(meta, use_final=use_final)
    if mode == "allowlist_external_input":
        ids = ext.get("allowlist_player_ids") or []
        if not ids:
            return pd.DataFrame()
        return client.query_df(
            sql,
            parameters=params,
            external_data=_allowlist_external_data(ids),
        )
    if mode == "allowlist_chunk":
        ids = sorted(int(x) for x in ext.get("allowlist_player_ids") or [])
        cfg = default_hightier_serving_config()
        chunk_sz = int(cfg.hightier_scorer_player_id_chunk_size)
        frames: list[pd.DataFrame] = []
        placeholder = int(cfg.placeholder_player_id)
        etl_filter = ""
        if params.get("last_etl") is not None:
            etl_filter = "AND __etl_insert_Dtm > %(last_etl)s"
        from trainer_hightier.serving.flight_recorder.ch_capture import _incremental_select_cols

        select_cols = _incremental_select_cols()
        for i in range(0, len(ids), chunk_sz):
            chunk = ids[i : i + chunk_sz]
            in_list = ",".join(str(x) for x in chunk)
            final_kw = " FINAL" if use_final else ""
            q = f"""
            SELECT {select_cols}
            FROM {cfg.source_db}.{cfg.tbet}{final_kw}
            WHERE payout_complete_dtm >= %(start)s
              AND payout_complete_dtm <= %(bet_avail)s
              AND payout_complete_dtm IS NOT NULL
              AND wager > 0
              AND player_id IS NOT NULL
              AND player_id != {placeholder}
              AND player_id IN ({in_list})
              {etl_filter}
            ORDER BY __etl_insert_Dtm ASC, bet_id ASC
            LIMIT %(lim)s
            """
            frames.append(client.query_df(q, parameters=params))
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)
    return client.query_df(sql, parameters=params)


def _execute_pool_window(client: Any, meta: dict[str, Any], *, use_final: bool) -> pd.DataFrame:
    """Run pool-window replay with production chunking."""
    ext = dict(meta.get("external_inputs") or {})
    ids = sorted(int(x) for x in ext.get("player_ids") or [])
    if not ids:
        return pd.DataFrame()
    cfg = default_hightier_serving_config()
    params = _coerce_params(dict(meta.get("parameters") or {}))
    chunk_sz = int(cfg.hightier_scorer_player_id_chunk_size)
    frames: list[pd.DataFrame] = []
    for i in range(0, len(ids), chunk_sz):
        chunk = ids[i : i + chunk_sz]
        sql = build_pool_chunk_sql(chunk, use_final=use_final, cfg=cfg)
        frames.append(client.query_df(sql, parameters=params))
    nonempty = [f for f in frames if f is not None and not f.empty]
    if not nonempty:
        return pd.DataFrame()
    bets = pd.concat(nonempty, ignore_index=True)
    if "bet_id" in bets.columns:
        bets = bets.drop_duplicates(subset=["bet_id"], keep="first")
    return bets


def _execute_validator_canonical(client: Any, meta: dict[str, Any], *, use_final: bool) -> pd.DataFrame:
    """Replay validator canonical-id fetch in player-id chunks."""
    ext = dict(meta.get("external_inputs") or {})
    ids = sorted(int(x) for x in ext.get("player_ids") or [])
    if not ids:
        return pd.DataFrame()
    params = _coerce_params(dict(meta.get("parameters") or {}))
    chunk_size = 5000
    frames: list[pd.DataFrame] = []
    sql = _pick_sql(meta, use_final=use_final)
    for i in range(0, len(ids), chunk_size):
        chunk = tuple(ids[i : i + chunk_size])
        q_params = {**params, "players": chunk}
        frames.append(client.query_df(sql, parameters=q_params))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _execute_validator_bet_ids(client: Any, meta: dict[str, Any], *, use_final: bool) -> pd.DataFrame:
    """Replay validator bet-id lookup in chunks."""
    ext = dict(meta.get("external_inputs") or {})
    ids = sorted(int(x) for x in ext.get("bet_ids") or [])
    if not ids:
        return pd.DataFrame()
    cfg = default_hightier_serving_config()
    chunk_size = max(1, int(cfg.hightier_scorer_player_id_chunk_size))
    sql = _pick_sql(meta, use_final=use_final)
    frames: list[pd.DataFrame] = []
    for i in range(0, len(ids), chunk_size):
        chunk = tuple(ids[i : i + chunk_size])
        frames.append(client.query_df(sql, parameters={"ids": chunk}))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def execute_query(
    query_meta: dict[str, Any],
    *,
    use_final: bool,
) -> pd.DataFrame:
    """Run stored replay contract against ClickHouse; empty frame when skipped."""
    meta = finalize_query_manifest(dict(query_meta))
    if requery_skip_reason(meta) is not None:
        return pd.DataFrame()
    sql = _pick_sql(meta, use_final=use_final)
    if not sql:
        return pd.DataFrame()
    try:
        client = get_clickhouse_client()
    except Exception as exc:
        logger.warning("[ch_requery] ClickHouse client unavailable: %s", exc)
        return pd.DataFrame()
    fetch = str(meta.get("fetch", ""))
    try:
        if fetch == "fetch_bets_incremental":
            return _execute_incremental(client, meta, use_final=use_final)
        if fetch == "fetch_bet_pool_window":
            return _execute_pool_window(client, meta, use_final=use_final)
        if fetch == "fetch_bets_by_canonical_id":
            return _execute_validator_canonical(client, meta, use_final=use_final)
        if fetch == "fetch_bet_payout_times_by_bet_ids":
            return _execute_validator_bet_ids(client, meta, use_final=use_final)
        params = _coerce_params(dict(meta.get("parameters") or {}))
        return client.query_df(sql, parameters=params)
    except Exception as exc:
        logger.warning(
            "[ch_requery] query failed fetch=%s use_final=%s: %s",
            fetch,
            use_final,
            exc,
        )
        return pd.DataFrame()
