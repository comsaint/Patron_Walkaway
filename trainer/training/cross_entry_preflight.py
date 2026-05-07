"""WS4 (v2): Shared data-source preflight for trainer-like CLI entrypoints.

Local Parquet: delegates to ``ensure_local_bridge_ready_for_training`` (Issue #14,
Workstream A) which emits ``AutoBuild[...]`` lines.

ClickHouse: lightweight ``SELECT 1`` via ``query_df``; logs ``DataPreflight[<entry>]:``.
Scorer/validator loops should call this **once at process start** (``main()``), not per
poll iteration, to avoid connection churn.
"""

from __future__ import annotations

import logging


def run_cross_entry_data_preflight(
    *,
    entry: str,
    use_local_parquet: bool,
    logger: logging.Logger,
) -> None:
    """Run data-source checks before heavy loads (bridge manifest or ClickHouse).

    Parameters
    ----------
    entry
        Short entry id for logs, e.g. ``\"trainer\"``, ``\"backtester\"``, ``\"scorer\"``, ``\"validator\"``.
    use_local_parquet
        When True, ensure Workstream A bridge ingress (may auto-build).
    logger
        Logger for the calling entrypoint.
    """
    if use_local_parquet:
        from trainer.training.local_bridge_preflight import ensure_local_bridge_ready_for_training

        ensure_local_bridge_ready_for_training(logger=logger)
        return
    _preflight_clickhouse(entry=entry, logger=logger)


def _preflight_clickhouse(*, entry: str, logger: logging.Logger) -> None:
    """Verify ClickHouse connectivity with a trivial query."""
    start = f"DataPreflight[{entry}]: verifying ClickHouse connectivity …"
    logger.info(start)
    print(start, flush=True)
    try:
        from trainer.core.db_conn import query_df

        query_df("SELECT 1 AS _ok")
    except Exception as exc:
        raise RuntimeError(
            f"DataPreflight[{entry}]: ClickHouse check failed: {exc!r}. "
            "Verify CH_HOST/CH_PORT, credentials, and SOURCE_DB in config."
        ) from exc
    ok = f"DataPreflight[{entry}]: ClickHouse OK"
    logger.info(ok)
    print(ok, flush=True)
