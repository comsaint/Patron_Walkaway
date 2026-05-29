"""ClickHouse client for ``trainer_hightier`` serving (credentials from :mod:`trainer_hightier.config`)."""

from __future__ import annotations

import threading
from typing import Any, Dict, Final, Optional

try:
    import clickhouse_connect
except ImportError:
    clickhouse_connect = None

from trainer_hightier.config import default_hightier_serving_config

# GDP_GMWDS_Raw.t_bet money columns are Decimal(19,4) with occasional NULLs.
# Bare CAST(... AS Float64) fails on NULL; toFloat64OrNull only accepts String.
CH_TBET_WAGER_SELECT: Final[str] = "CAST(wager AS Nullable(Float64)) AS wager"
CH_TBET_CASINO_WIN_SELECT: Final[str] = "CAST(casino_win AS Nullable(Float64)) AS casino_win"
CH_TBET_PAYOUT_ODDS_SELECT: Final[str] = "CAST(payout_odds AS Nullable(Float64)) AS payout_odds"
CH_TBET_WAGER_POSITIVE_PRED: Final[str] = (
    "wager IS NOT NULL AND CAST(wager AS Nullable(Float64)) > 0"
)
# Day semantics are defined in schema/time_semantics_registry.yaml: derive from payout_complete_dtm in HK.
CH_TBET_GAMING_DAY_EVENT_EXPR: Final[str] = "toDate(toTimeZone(payout_complete_dtm, 'Asia/Hong_Kong'))"

# Day semantics for t_session: prefer session_end_dtm falling back to lud_dtm (last user/device activity)
# Normalize to HK date to match other day semantics.
CH_TSESSION_GAMING_DAY_EVENT_EXPR: Final[str] = "toDate(toTimeZone(COALESCE(session_end_dtm, lud_dtm), 'Asia/Hong_Kong'))"

_thread_local = threading.local()


def get_clickhouse_client():
    """Return a per-thread ClickHouse client (same concurrency model as ``trainer.db_conn``)."""
    if clickhouse_connect is None:
        raise RuntimeError("clickhouse_connect is required for serving; install clickhouse-connect.")
    cfg = default_hightier_serving_config()
    if not hasattr(_thread_local, "client") or _thread_local.client is None:
        _thread_local.client = clickhouse_connect.get_client(
            host=cfg.ch_host,
            port=int(cfg.ch_port),
            username=cfg.ch_user,
            password=cfg.ch_password,
            secure=bool(cfg.ch_secure),
            database=cfg.source_db,
        )
    return _thread_local.client


def _clear_clickhouse_client_cache() -> None:
    if hasattr(_thread_local, "client"):
        _thread_local.client = None


get_clickhouse_client.cache_clear = _clear_clickhouse_client_cache  # type: ignore[attr-defined]


def query_df(sql: str, parameters: Optional[Dict[str, Any]] = None):
    """Run ``query_df`` on the shared thread-local client."""
    client = get_clickhouse_client()
    return client.query_df(sql, parameters=parameters or {})
