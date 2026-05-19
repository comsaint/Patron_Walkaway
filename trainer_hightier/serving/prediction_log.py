"""Append-only SQLite ``prediction_log`` (all scored rows; separate from ``state.db``)."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_PREDICTION_LOG_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("threshold", "REAL"),
    ("features_json", "TEXT"),
    ("fe_features_missing", "INTEGER"),
    ("snapshot_version", "TEXT"),
    ("mid_term_freshness_status", "TEXT"),
    ("slow_freshness_status", "TEXT"),
    ("snapshot_scoring_degraded", "INTEGER"),
)


def init_prediction_log_db(db_path: Path | str | None) -> Path | None:
    """Create empty ``prediction_log`` database and schema if enabled; idempotent.

    Call alongside :func:`trainer_hightier.serving.state_db.init_state_db` so the
    file exists at serving startup (not only after the first non-empty score cycle).

    Returns
    -------
    Path | None
        Resolved path when enabled, else ``None``.
    """
    if db_path is None or not str(db_path).strip():
        return None
    p = Path(db_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(p)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        ensure_prediction_log_table(conn)
    logger.info("[prediction_log] initialized %s", p)
    return p


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows}


def _migrate_prediction_log_columns(conn: sqlite3.Connection) -> None:
    """Add audit columns to existing DBs (idempotent)."""
    have = _existing_columns(conn, "prediction_log")
    for col_name, col_type in _PREDICTION_LOG_MIGRATION_COLUMNS:
        if col_name not in have:
            conn.execute(f"ALTER TABLE prediction_log ADD COLUMN {col_name} {col_type}")


def ensure_prediction_log_table(conn: sqlite3.Connection) -> None:
    """Create ``prediction_log`` and indexes if missing."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_log (
            prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scored_at TEXT NOT NULL,
            bet_id TEXT,
            session_id TEXT,
            player_id TEXT,
            canonical_id TEXT,
            casino_player_id TEXT,
            table_id TEXT,
            model_version TEXT NOT NULL,
            score REAL NOT NULL,
            margin REAL NOT NULL,
            is_alert INTEGER NOT NULL,
            is_rated_obs INTEGER NOT NULL,
            threshold REAL,
            features_json TEXT,
            fe_features_missing INTEGER
        )
        """
    )
    _migrate_prediction_log_columns(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_scored_at ON prediction_log(scored_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_model_version ON prediction_log(model_version)"
    )


def _json_safe_scalar(v: Any) -> Any:
    """Convert a single cell to a JSON-serializable value."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (np.floating, float)):
        fv = float(v)
        return None if not np.isfinite(fv) else fv
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if hasattr(v, "item"):
        try:
            return _json_safe_scalar(v.item())
        except (TypeError, ValueError):
            pass
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _feature_row_dict(row: pd.Series, feature_columns: tuple[str, ...]) -> dict[str, Any]:
    """Build feature name → value map for one scored row (model input values)."""
    return {str(c): _json_safe_scalar(row.get(c)) for c in feature_columns}


def _count_fe_features_missing(feat: dict[str, Any]) -> int:
    """Count ``fe__*`` keys with null/missing values."""
    n = 0
    for k, v in feat.items():
        if not str(k).startswith("fe__"):
            continue
        if v is None:
            n += 1
    return n


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (float, np.floating)) and np.isnan(float(v)):
        return None
    s = str(v).strip()
    return s if s else None


def append_hightier_prediction_log(
    db_path: Path | str | None,
    *,
    scored_at: str,
    model_version: str,
    staged: pd.DataFrame,
    prob: np.ndarray,
    threshold: float,
    features: pd.DataFrame | None = None,
    feature_columns: tuple[str, ...] | None = None,
    snapshot_version: str | None = None,
    mid_term_freshness_status: str | None = None,
    slow_freshness_status: str | None = None,
    snapshot_scoring_degraded: bool = False,
) -> None:
    """Batch-insert one scoring cycle into ``prediction_log`` (no-op if path disabled or frame empty).

    When ``features`` and ``feature_columns`` are provided, each row stores a JSON object of
    model input values in ``features_json`` plus ``fe_features_missing`` (count of null ``fe__*``).

    ``is_alert`` here means ``margin >= 0`` and ``is_rated_obs == 1`` (audit flag; scorer
    ``state.db`` alerts use ``score >= threshold`` on all rows).
    """
    if db_path is None or not str(db_path).strip():
        return
    if staged is None or staged.empty:
        return
    p = Path(db_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    if len(prob) != len(staged):
        raise ValueError(
            f"prob length {len(prob)} != staged length {len(staged)}; cannot write prediction_log"
        )
    feat_cols: tuple[str, ...] = ()
    feat_frame: pd.DataFrame | None = None
    if features is not None and feature_columns:
        feat_cols = tuple(str(c) for c in feature_columns)
        if len(features) != len(staged):
            raise ValueError(
                f"features length {len(features)} != staged length {len(staged)}; cannot write prediction_log"
            )
        miss = [c for c in feat_cols if c not in features.columns]
        if miss:
            raise ValueError(f"features frame missing model columns: {miss}")
        feat_frame = features.reset_index(drop=True)

    nom = (
        staged["casino_player_id"]
        if "casino_player_id" in staged.columns
        else pd.Series(np.nan, index=staged.index)
    )
    is_rated_obs = (nom.notna() & (nom.astype(str).str.strip() != "")).astype(int)
    score = np.asarray(prob, dtype=np.float64)
    thr = float(threshold)
    margin = score - thr
    is_alert = ((margin >= 0.0) & (is_rated_obs.to_numpy() == 1)).astype(int)

    staged_reset = staged.reset_index(drop=True)
    rows: list[tuple[Any, ...]] = []
    for pos in range(len(staged_reset)):
        row = staged_reset.iloc[pos]
        feat_json: str | None = None
        fe_miss: int | None = None
        if feat_frame is not None and feat_cols:
            feat_map = _feature_row_dict(feat_frame.iloc[pos], feat_cols)
            feat_json = json.dumps(feat_map, separators=(",", ":"), ensure_ascii=False)
            fe_miss = _count_fe_features_missing(feat_map)
        rows.append(
            (
                scored_at,
                _str_or_none(row.get("bet_id")),
                _str_or_none(row.get("session_id")),
                _str_or_none(row.get("player_id")),
                _str_or_none(row.get("canonical_id")),
                _str_or_none(row.get("casino_player_id")),
                _str_or_none(row.get("table_id")),
                str(model_version),
                float(score[pos]),
                float(margin[pos]),
                int(is_alert[pos]),
                int(is_rated_obs.iloc[pos]),
                thr,
                feat_json,
                fe_miss,
                _str_or_none(snapshot_version),
                _str_or_none(mid_term_freshness_status),
                _str_or_none(slow_freshness_status),
                1 if snapshot_scoring_degraded else 0,
            )
        )

    conn = sqlite3.connect(str(p))
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        ensure_prediction_log_table(conn)
        conn.executemany(
            """
            INSERT INTO prediction_log (
                scored_at, bet_id, session_id, player_id, canonical_id,
                casino_player_id, table_id, model_version, score, margin,
                is_alert, is_rated_obs, threshold, features_json, fe_features_missing,
                snapshot_version, mid_term_freshness_status, slow_freshness_status,
                snapshot_scoring_degraded
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    logger.debug("[prediction_log] appended %d row(s) to %s", len(rows), p)
