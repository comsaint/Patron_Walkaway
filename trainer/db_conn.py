from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Optional

import clickhouse_connect

import config


@lru_cache(maxsize=1)
def get_clickhouse_client():
    """Return a cached ClickHouse client configured from config.py."""
    return clickhouse_connect.get_client(
        host=config.CH_HOST,
        port=config.CH_PORT,
        username=config.CH_USER,
        password=config.CH_PASS,
        secure=config.CH_SECURE,
        database=config.SOURCE_DB,
    )


def query_df(sql: str, parameters: Optional[Dict[str, Any]] = None):
    """Shortcut to run a DataFrame query using the shared client."""
    client = get_clickhouse_client()
    return client.query_df(sql, parameters=parameters or {})
