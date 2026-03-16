from __future__ import annotations

import threading
from typing import Any, Dict, Optional

try:
    import clickhouse_connect
except ImportError:
    clickhouse_connect = None

from . import config  # type: ignore[import, no-redef]

_thread_local = threading.local()


def get_clickhouse_client():
    """Return a per-thread ClickHouse client configured from config.py.

    Each thread gets its own client instance to avoid concurrent queries
    on the same session (see PLAN: ClickHouse Client Concurrency).
    """
    if clickhouse_connect is None:
        raise RuntimeError(
            "clickhouse_connect not available; install clickhouse-connect and ensure .env is loaded"
        )
    if not hasattr(_thread_local, "client") or _thread_local.client is None:
        _thread_local.client = clickhouse_connect.get_client(
            host=config.CH_HOST,
            port=config.CH_PORT,
            username=config.CH_USER,
            password=config.CH_PASS,
            secure=config.CH_SECURE,
            database=config.SOURCE_DB,
        )
    return _thread_local.client


def _clear_clickhouse_client_cache() -> None:
    """Clear the current thread's cached client (for tests)."""
    if hasattr(_thread_local, "client"):
        _thread_local.client = None


get_clickhouse_client.cache_clear = _clear_clickhouse_client_cache  # type: ignore[attr-defined]


def query_df(sql: str, parameters: Optional[Dict[str, Any]] = None):
    """Shortcut to run a DataFrame query using the shared client."""
    client = get_clickhouse_client()
    return client.query_df(sql, parameters=parameters or {})
