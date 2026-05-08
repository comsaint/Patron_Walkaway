"""WS4 (v2): Shared data-source preflight for trainer-like CLI entrypoints.

Local Parquet: delegates to ``ensure_local_bridge_ready_for_training`` (Issue #14,
Workstream A) which emits ``AutoBuild[...]`` lines.

ClickHouse: lightweight ``SELECT 1`` via ``query_df``; logs ``DataPreflight[<entry>]:``.
Optional Issue #19 production freshness: compares bundle ``model_metadata.json``
``global_window.end`` vs ``max(__etl_insert_Dtm)`` on ``SOURCE_DB.TBET`` (disable with
``DISABLE_PRODUCTION_FRESHNESS_PREFLIGHT=1``; strict with ``PRODUCTION_FRESHNESS_STRICT=1``).

Scorer/validator loops should call this **once at process start** (``main()``), not per
poll iteration, to avoid connection churn.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd


def _env_truthy(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


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
    _maybe_run_production_freshness_gate(entry=entry, logger=logger)


def _resolve_model_bundle_dir_for_metadata(model_dir_raw: str) -> Optional[Path]:
    """Return bundle dir containing ``model_metadata.json`` when resolvable."""
    from trainer.core.model_bundle_paths import resolve_model_bundle_dir

    raw = Path(model_dir_raw).expanduser().resolve()
    if not raw.exists():
        return None
    if (raw / "model.pkl").is_file():
        try:
            bd = resolve_model_bundle_dir(raw, explicit_dir=raw)
        except FileNotFoundError:
            bd = raw
        return bd if (bd / "model_metadata.json").is_file() else None
    try:
        bd = resolve_model_bundle_dir(raw)
    except FileNotFoundError:
        return None
    return bd if (bd / "model_metadata.json").is_file() else None


def _parse_global_window_end_utc_naive(meta_path: Path) -> Optional[pd.Timestamp]:
    """Parse ``global_window.end`` from bundle metadata as naive UTC."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    gw = meta.get("global_window") or {}
    end_raw = gw.get("end")
    if not end_raw:
        return None
    ts = pd.Timestamp(end_raw)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def _query_tbet_max_etl_utc_naive() -> Optional[pd.Timestamp]:
    """Return warehouse max ETL timestamp or None when missing."""
    from trainer.core import config
    from trainer.core.db_conn import query_df

    db = (getattr(config, "SOURCE_DB", None) or "").strip()
    tb = (getattr(config, "TBET", None) or "").strip()
    if not db or not tb:
        raise RuntimeError("SOURCE_DB or TBET unset; cannot query warehouse freshness")
    sql = f"SELECT max(__etl_insert_Dtm) AS mx FROM {db}.{tb} FINAL"
    df = query_df(sql)
    if df.empty or "mx" not in df.columns:
        return None
    v = df["mx"].iloc[0]
    if v is None or pd.isna(v):
        return None
    ts = pd.Timestamp(v)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def _log_freshness_mismatch(
    *,
    entry: str,
    logger: logging.Logger,
    strict: bool,
    train_end: pd.Timestamp,
    mx: pd.Timestamp,
) -> None:
    """Log or raise on warehouse vs training-window mismatch."""
    msg = (
        f"warehouse_freshness_mismatch: max(__etl_insert_Dtm)={mx} < "
        f"model global_window.end={train_end}"
    )
    if strict:
        raise RuntimeError(f"PRODUCTION_FRESHNESS_STRICT: {msg}") from None
    logger.warning("DataPreflight[%s]: %s", entry, msg)


def _warehouse_max_etl_or_none(
    *,
    entry: str,
    logger: logging.Logger,
    strict: bool,
) -> Optional[pd.Timestamp]:
    """Run max(__etl_insert_Dtm) query; on strict failure raise, else warn and return None."""
    try:
        mx = _query_tbet_max_etl_utc_naive()
    except Exception as exc:
        if strict:
            raise RuntimeError(
                f"PRODUCTION_FRESHNESS_STRICT: ClickHouse max(__etl_insert_Dtm) query failed: "
                f"{exc!r}"
            ) from exc
        logger.warning("DataPreflight[%s]: freshness query failed — %s", entry, exc)
        return None
    if mx is None:
        msg = "max(__etl_insert_Dtm) is NULL (warehouse empty or column unavailable)"
        if strict:
            raise RuntimeError(f"PRODUCTION_FRESHNESS_STRICT: {msg}")
        logger.warning("DataPreflight[%s]: %s", entry, msg)
    return mx


def _maybe_run_production_freshness_gate(*, entry: str, logger: logging.Logger) -> None:
    """Optionally compare ClickHouse ETL watermark to bundle training window end."""
    if _env_truthy("DISABLE_PRODUCTION_FRESHNESS_PREFLIGHT"):
        logger.info(
            "DataPreflight[%s]: production freshness preflight disabled "
            "(DISABLE_PRODUCTION_FRESHNESS_PREFLIGHT)",
            entry,
        )
        return
    strict = _env_truthy("PRODUCTION_FRESHNESS_STRICT")
    model_dir = (os.environ.get("MODEL_DIR") or "").strip()
    if not model_dir:
        if strict:
            raise RuntimeError(
                "PRODUCTION_FRESHNESS_STRICT: MODEL_DIR must be set for bundle vs warehouse "
                "freshness check"
            )
        return
    bundle = _resolve_model_bundle_dir_for_metadata(model_dir)
    if bundle is None:
        msg = f"no bundle with model_metadata.json under MODEL_DIR={model_dir!r}"
        if strict:
            raise FileNotFoundError(f"PRODUCTION_FRESHNESS_STRICT: {msg}")
        logger.warning("DataPreflight[%s]: %s — skipping freshness check", entry, msg)
        return
    train_end = _parse_global_window_end_utc_naive(bundle / "model_metadata.json")
    if train_end is None:
        logger.warning(
            "DataPreflight[%s]: model_metadata.json missing global_window.end — "
            "skipping freshness check",
            entry,
        )
        return
    mx = _warehouse_max_etl_or_none(entry=entry, logger=logger, strict=strict)
    if mx is None:
        return
    if mx < train_end:
        _log_freshness_mismatch(
            entry=entry, logger=logger, strict=strict, train_end=train_end, mx=mx
        )
        return
    logger.info(
        "DataPreflight[%s]: production freshness OK (max_etl=%s >= train_window_end=%s)",
        entry,
        mx,
        train_end,
    )
