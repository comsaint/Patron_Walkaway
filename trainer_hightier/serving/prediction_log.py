"""Append-only SQLite ``prediction_log`` (all scored rows; separate from ``state.db``)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


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
            is_rated_obs INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_scored_at ON prediction_log(scored_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_model_version ON prediction_log(model_version)"
    )


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
) -> None:
    """Batch-insert one scoring cycle into ``prediction_log`` (no-op if path disabled or frame empty).

    ``is_alert`` matches scorer alert rule: ``margin >= 0`` and ``is_rated_obs == 1``.
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

    nom = (
        staged["casino_player_id"]
        if "casino_player_id" in staged.columns
        else pd.Series(np.nan, index=staged.index)
    )
    is_rated_obs = (nom.notna() & (nom.astype(str).str.strip() != "")).astype(int)
    score = np.asarray(prob, dtype=np.float64)
    margin = score - float(threshold)
    is_alert = ((margin >= 0.0) & (is_rated_obs.to_numpy() == 1)).astype(int)

    rows: list[tuple[Any, ...]] = []
    for pos in range(len(staged)):
        row = staged.iloc[pos]
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
                is_alert, is_rated_obs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    logger.debug("[prediction_log] appended %d row(s) to %s", len(rows), p)
