"""ClickHouse client for ``trainer_hightier`` serving (credentials from :mod:`trainer_hightier.config`)."""

from __future__ import annotations

import logging
import threading
import warnings
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
def ch_tbet_gaming_day_event_sql(*, table_alias: str = "") -> str:
    """Return ClickHouse SQL for HK ``gaming_day_event`` from raw ``t_bet``."""
    col = f"{table_alias}.payout_complete_dtm" if table_alias else "payout_complete_dtm"
    return f"toDate(toTimeZone({col}, 'Asia/Hong_Kong'))"


CH_TBET_GAMING_DAY_EVENT_EXPR: Final[str] = ch_tbet_gaming_day_event_sql()
CH_TBET_GAMING_DAY_EVENT_SELECT: Final[str] = f"{CH_TBET_GAMING_DAY_EVENT_EXPR} AS gaming_day_event"
CH_TBET_GAMING_DAY_EVENT_NOT_NULL_PRED: Final[str] = f"{CH_TBET_GAMING_DAY_EVENT_EXPR} IS NOT NULL"

# Day semantics for t_session: prefer session_end_dtm falling back to lud_dtm (last user/device activity)
# Normalize to HK date to match other day semantics.
CH_TSESSION_GAMING_DAY_EVENT_EXPR: Final[str] = "toDate(toTimeZone(COALESCE(session_end_dtm, lud_dtm), 'Asia/Hong_Kong'))"

_thread_local = threading.local()
_LOG_NOISE_FILTERS_APPLIED = False


def apply_serving_log_noise_filters() -> None:
    """Suppress noisy third-party warnings (pandas 3 ``Pandas4Warning`` / clickhouse_connect).

    Idempotent; safe to call from deploy bootstrap and on ``ch_adapter`` import.
    """
    global _LOG_NOISE_FILTERS_APPLIED
    if _LOG_NOISE_FILTERS_APPLIED:
        return
    _LOG_NOISE_FILTERS_APPLIED = True

    warnings.filterwarnings(
        "ignore",
        message=r"The copy keyword is deprecated.*",
    )
    warnings.filterwarnings(
        "ignore",
        module=r"clickhouse_connect\.driver\.npquery",
    )
    pandas4: type[Warning] | None = None
    try:
        from pandas.errors import Pandas4Warning as _p4  # type: ignore[attr-defined]

        pandas4 = _p4
    except ImportError:
        try:
            from pandas import Pandas4Warning as _p4  # type: ignore[attr-defined]

            pandas4 = _p4
        except ImportError:
            pandas4 = None
    if pandas4 is not None:
        warnings.filterwarnings("ignore", category=pandas4)

    for name in ("feast", "feast.infra", "feast.infra.registry"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


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


apply_serving_log_noise_filters()
