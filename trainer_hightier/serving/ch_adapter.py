"""ClickHouse client for ``trainer_hightier`` serving (credentials from :mod:`trainer_hightier.config`)."""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

try:
    import clickhouse_connect
except ImportError:
    clickhouse_connect = None

from trainer_hightier.config import default_hightier_serving_config

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
