"""Lightweight datasource checks before serving-heavy loops."""

from __future__ import annotations

import logging

from trainer_hightier.serving import ch_adapter


def run_cross_entry_data_preflight(
    *,
    entry: str,
    use_local_parquet: bool,
    logger: logging.Logger,
) -> None:
    """Run ClickHouse ping for online ``trainer_hightier`` validators/scorers."""

    del use_local_parquet  # parity signature with legacy trainer; high-tier MVP is warehouse-only here.
    start = f"DataPreflight[{entry}]: verifying ClickHouse connectivity ..."
    logger.info(start)
    try:
        _ = ch_adapter.query_df("SELECT 1 AS _ok")
    except Exception as exc:
        raise RuntimeError(
            f"DataPreflight[{entry}]: ClickHouse check failed: {exc!r}. "
            "Verify trainer_hightier.config HightierServingConfig credentials."
        ) from exc
    logger.info("DataPreflight[%s]: ClickHouse OK", entry)
