"""
Phase 2 T5: Export prediction_log to MLflow artifact by watermark.

- Reads last_exported_prediction_id from prediction_export_meta.
- Exports batch: prediction_id > last_exported_id, scored_at <= now - safety_lag.
- On success: updates watermark once; optionally records run in prediction_export_runs.
- On failure: does not move watermark; data is retained for retry.

Run from repo root: python -m trainer.scripts.export_predictions_to_mlflow [--dry-run]
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd  # type: ignore[import-untyped]

from trainer.core import config
from trainer.core.mlflow_utils import (
    end_run_safe,
    is_mlflow_available,
    log_params_safe,
    log_tags_safe,
    safe_start_run,
)

_log = logging.getLogger(__name__)

META_KEY_LAST_EXPORTED_ID = "last_exported_prediction_id"
MLFLOW_EXPERIMENT_EXPORT = (
    (os.environ.get("MLFLOW_EXPERIMENT_EXPORT") or "").strip()
    or "patron/patron_walkaway/prod/prediction_export"
)


def _ensure_export_meta_tables(conn: sqlite3.Connection) -> None:
    """Create export meta and audit tables if not exist (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_export_meta (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_export_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts TEXT NOT NULL,
            end_ts TEXT,
            min_prediction_id INTEGER,
            max_prediction_id INTEGER,
            row_count INTEGER,
            artifact_path TEXT,
            success INTEGER NOT NULL,
            error_message TEXT
        )
        """
    )


def _get_last_exported_id(conn: sqlite3.Connection) -> int:
    """Return last exported prediction_id; 0 if no row."""
    row = conn.execute(
        "SELECT value FROM prediction_export_meta WHERE key = ?",
        (META_KEY_LAST_EXPORTED_ID,),
    ).fetchone()
    return int(row[0]) if row else 0


def _set_last_exported_id(conn: sqlite3.Connection, prediction_id: int) -> None:
    """Set watermark to prediction_id (INSERT OR REPLACE)."""
    conn.execute(
        "INSERT OR REPLACE INTO prediction_export_meta (key, value) VALUES (?, ?)",
        (META_KEY_LAST_EXPORTED_ID, prediction_id),
    )


def _insert_export_run(
    conn: sqlite3.Connection,
    start_ts: str,
    end_ts: str | None,
    min_prediction_id: int | None,
    max_prediction_id: int | None,
    row_count: int,
    artifact_path: str | None,
    success: int,
    error_message: str | None = None,
) -> None:
    """Append one row to prediction_export_runs audit table."""
    conn.execute(
        """
        INSERT INTO prediction_export_runs (
            start_ts, end_ts, min_prediction_id, max_prediction_id,
            row_count, artifact_path, success, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            start_ts,
            end_ts,
            min_prediction_id,
            max_prediction_id,
            row_count,
            artifact_path,
            success,
            error_message,
        ),
    )


def _run_retention_cleanup(
    conn: sqlite3.Connection,
    watermark_id: int,
    retention_cutoff_ts: str,
    batch_size: int,
) -> int:
    """Delete rows where prediction_id <= watermark and scored_at < retention_cutoff, in batches. Returns total deleted."""
    total_deleted = 0
    while True:
        ids = conn.execute(
            """
            SELECT prediction_id FROM prediction_log
            WHERE prediction_id <= ? AND scored_at < ?
            LIMIT ?
            """,
            (watermark_id, retention_cutoff_ts, batch_size),
        ).fetchall()
        if not ids:
            break
        id_list = [r[0] for r in ids]
        placeholders = ",".join("?" * len(id_list))
        conn.execute(
            f"DELETE FROM prediction_log WHERE prediction_id IN ({placeholders})",
            id_list,
        )
        total_deleted += len(id_list)
        conn.commit()
    return total_deleted


def run_export(
    db_path: str,
    safety_lag_minutes: int,
    batch_rows: int,
    dry_run: bool = False,
    retention_days: int = 0,
    retention_delete_batch: int = 5000,
    run_cleanup: bool = True,
) -> int:
    """
    Export one batch of prediction_log to Parquet and upload to MLflow.

    Returns 0 if no rows to export or success; 1 on failure (watermark unchanged).
    """
    pl_path = (db_path or "").strip()
    if not pl_path:
        _log.warning("PREDICTION_LOG_DB_PATH is empty; export skipped.")
        return 0

    path = Path(pl_path)
    if not path.exists():
        _log.warning("Prediction log DB not found at %s; export skipped.", pl_path)
        return 0

    hk_tz = ZoneInfo(config.HK_TZ)
    now_hk = datetime.now(hk_tz)
    cutoff_hk = now_hk - timedelta(minutes=safety_lag_minutes)
    cutoff_ts = cutoff_hk.isoformat()

    conn = sqlite3.connect(pl_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        _ensure_export_meta_tables(conn)
        # If scorer has never run, prediction_log may not exist yet.
        cursor = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prediction_log'"
        )
        if cursor.fetchone() is None:
            _log.info("prediction_log table not present; export skipped.")
            return 0
        last_id = _get_last_exported_id(conn)

        # Batch: prediction_id > last_id, scored_at <= cutoff, ORDER BY prediction_id LIMIT
        df = pd.read_sql_query(
            """
            SELECT prediction_id, scored_at, bet_id, session_id, player_id,
                   canonical_id, casino_player_id, table_id, model_version,
                   score, margin, is_alert, is_rated_obs
            FROM prediction_log
            WHERE prediction_id > ? AND scored_at <= ?
            ORDER BY prediction_id
            LIMIT ?
            """,
            conn,
            params=(last_id, cutoff_ts, batch_rows),
        )
    finally:
        conn.close()

    if df.empty:
        _log.info("No rows to export (last_id=%s, cutoff_ts=%s).", last_id, cutoff_ts)
        return 0

    min_id = int(df["prediction_id"].min())
    max_id = int(df["prediction_id"].max())
    row_count = len(df)
    start_ts = now_hk.isoformat()

    if dry_run:
        _log.info(
            "Dry-run: would export prediction_id %s..%s (%s rows).",
            min_id,
            max_id,
            row_count,
        )
        return 0

    # Write Parquet (snappy)
    with tempfile.TemporaryDirectory(prefix="prediction_export_") as tmpdir:
        out_path = Path(tmpdir) / "predictions.parquet"
        df.to_parquet(out_path, index=False, compression="snappy")

        # Artifact path: predictions/date/hour/batch.parquet
        date_part = now_hk.strftime("%Y-%m-%d")
        hour_part = now_hk.strftime("%H")
        artifact_path = f"predictions/{date_part}/{hour_part}/batch.parquet"

        # On upload failure we must not advance watermark (plan: 上傳失敗 -> 不移動 watermark).
        # Use MLflow directly so exceptions propagate; log_artifact_safe no-ops and does not raise.
        with safe_start_run(
            experiment_name=MLFLOW_EXPERIMENT_EXPORT,
            run_name=f"export_{date_part}_{hour_part}_{min_id}_{max_id}",
            tags={"min_prediction_id": str(min_id), "max_prediction_id": str(max_id)},
        ):
            log_params_safe({"row_count": row_count, "min_prediction_id": min_id, "max_prediction_id": max_id})
            log_tags_safe({"export_batch": "1"})
            try:
                if is_mlflow_available():
                    import mlflow  # type: ignore[import-not-found]
                    mlflow.log_artifact(str(out_path), artifact_path=artifact_path)
                else:
                    _log.warning("MLflow not available; skipping upload (watermark unchanged).")
                    return 1
            except Exception as e:
                _log.exception("MLflow log_artifact failed: %s", e)
                end_run_safe()
                return 1
        end_run_safe()

    # Success: update watermark and audit
    conn = sqlite3.connect(pl_path)
    try:
        _set_last_exported_id(conn, max_id)
        _insert_export_run(
            conn,
            start_ts=start_ts,
            end_ts=datetime.now(hk_tz).isoformat(),
            min_prediction_id=min_id,
            max_prediction_id=max_id,
            row_count=row_count,
            artifact_path=artifact_path,
            success=1,
        )
        conn.commit()
        # T6: bounded retention cleanup (only rows already exported and older than retention)
        if run_cleanup and retention_days > 0:
            retention_cutoff = (now_hk - timedelta(days=retention_days)).isoformat()
            deleted = _run_retention_cleanup(conn, max_id, retention_cutoff, retention_delete_batch)
            if deleted:
                _log.info("Retention cleanup: deleted %s rows (prediction_id <= %s, scored_at < %s).", deleted, max_id, retention_cutoff)
    finally:
        conn.close()

    _log.info("Exported prediction_id %s..%s (%s rows) to %s.", min_id, max_id, row_count, artifact_path)
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Export prediction_log batch to MLflow artifact (T5).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would be exported; do not upload or move watermark.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override PREDICTION_LOG_DB_PATH (default: from config).",
    )
    parser.add_argument(
        "--batch-rows",
        type=int,
        default=None,
        help="Override batch size (default: config.PREDICTION_EXPORT_BATCH_ROWS).",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip retention cleanup after export (T6).",
    )
    args = parser.parse_args()

    db_path = args.db or config.PREDICTION_LOG_DB_PATH
    batch_rows = args.batch_rows if args.batch_rows is not None else config.PREDICTION_EXPORT_BATCH_ROWS
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not args.dry_run and not is_mlflow_available():
        _log.warning("MLflow not available; export would no-op. Set MLFLOW_TRACKING_URI to enable upload.")
        # Still allow dry-run when MLflow is down
        if not args.dry_run:
            return 1

    return run_export(
        db_path=db_path,
        safety_lag_minutes=config.PREDICTION_EXPORT_SAFETY_LAG_MINUTES,
        batch_rows=batch_rows,
        dry_run=args.dry_run,
        retention_days=config.PREDICTION_LOG_RETENTION_DAYS,
        retention_delete_batch=config.PREDICTION_LOG_RETENTION_DELETE_BATCH,
        run_cleanup=not args.no_cleanup,
    )


if __name__ == "__main__":
    raise SystemExit(main())
