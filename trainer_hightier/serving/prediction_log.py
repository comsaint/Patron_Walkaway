"""Append-only SQLite ``prediction_log`` (all scored rows; separate from ``state.db``)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from trainer_hightier.config import HK_TZ
from trainer_hightier.serving.feast_online_adapter import RowMissingAudit

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo  # type: ignore

_HK_TZ_INFO = ZoneInfo(HK_TZ)

_PREDICTION_LOG_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("game_id", "TEXT"),
    ("bet_ts", "TEXT"),
    ("threshold", "REAL"),
    ("features_json", "TEXT"),
    ("fe_features_missing", "INTEGER"),
    ("snapshot_version", "TEXT"),
    ("mid_term_freshness_status", "TEXT"),
    ("slow_freshness_status", "TEXT"),
    ("snapshot_scoring_degraded", "INTEGER"),
    ("scoring_status", "TEXT"),
    ("model_features_missing", "INTEGER"),
    ("missing_family_json", "TEXT"),
    ("mid_term_anchor_gaming_day_event_max", "TEXT"),
    ("mid_term_snapshot_age_days", "INTEGER"),
    ("mid_null_top_features_json", "TEXT"),
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
        ensure_prediction_validation_tables(conn)
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
            bet_ts TEXT,
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_bet_ts ON prediction_log(bet_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_player_game "
        "ON prediction_log(player_id, game_id)"
    )


def ensure_prediction_validation_tables(conn: sqlite3.Connection) -> None:
    """Create ground-truth validation tables in ``prediction_log.db`` (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_validation_results (
            bet_id TEXT PRIMARY KEY,
            scored_at TEXT,
            bet_ts TEXT,
            validated_at TEXT,
            player_id INTEGER,
            canonical_id TEXT,
            casino_player_id TEXT,
            model_version TEXT,
            score REAL,
            is_alert INTEGER,
            result INTEGER,
            gap_start TEXT,
            gap_minutes REAL,
            reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_predictions (
            bet_id TEXT PRIMARY KEY,
            processed_ts TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pvr_bet_ts ON prediction_validation_results(bet_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pvr_result ON prediction_validation_results(result)"
    )


def format_bet_ts_iso(value: Any) -> str | None:
    """Serialize ``payout_complete_dtm`` as HK isoformat for ``prediction_log.bet_ts``."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize(_HK_TZ_INFO)
    else:
        ts = ts.tz_convert(_HK_TZ_INFO)
    return ts.isoformat()


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
    row_audits: list[RowMissingAudit] | None = None,
    scoring_status: str = "scored",
    snapshot_version: str | None = None,
    mid_term_freshness_status: str | None = None,
    slow_freshness_status: str | None = None,
    snapshot_scoring_degraded: bool = False,
    mid_term_anchor_gaming_day_event_max: str | None = None,
    mid_term_snapshot_age_days: int | None = None,
    mid_null_top_features_json: str | None = None,
) -> None:
    """Batch-insert one scoring cycle into ``prediction_log`` (no-op if path disabled or frame empty).

    When ``features`` and ``feature_columns`` are provided, each row stores a JSON object of
    model input values in ``features_json`` plus per-family missing counts in ``missing_family_json``.

    ``scoring_status`` is ``scored`` for rows that entered ``predict_proba``, or
    ``skipped_entity_missing`` when Feast entity rows were absent.

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
    status = str(scoring_status).strip() or "scored"
    rows: list[tuple[Any, ...]] = []
    for pos in range(len(staged_reset)):
        row = staged_reset.iloc[pos]
        feat_json: str | None = None
        fe_miss: int | None = None
        model_miss: int | None = None
        family_json: str | None = None
        if row_audits is not None and pos < len(row_audits):
            audit = row_audits[pos]
            fe_miss = audit.fe_features_missing
            model_miss = audit.model_features_missing
            family_json = json.dumps(audit.family_summary(), separators=(",", ":"), ensure_ascii=False)
        if feat_frame is not None and feat_cols:
            feat_map = _feature_row_dict(feat_frame.iloc[pos], feat_cols)
            feat_json = json.dumps(feat_map, separators=(",", ":"), ensure_ascii=False)
            if fe_miss is None:
                fe_miss = _count_fe_features_missing(feat_map)
            if model_miss is None:
                model_miss = sum(1 for c in feat_cols if feat_map.get(c) is None)
        rows.append(
            (
                scored_at,
                format_bet_ts_iso(row.get("payout_complete_dtm")),
                _str_or_none(row.get("bet_id")),
                _str_or_none(row.get("session_id")),
                _str_or_none(row.get("player_id")),
                _str_or_none(row.get("game_id")),
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
                status,
                model_miss,
                family_json,
                _str_or_none(mid_term_anchor_gaming_day_event_max),
                int(mid_term_snapshot_age_days)
                if mid_term_snapshot_age_days is not None
                else None,
                mid_null_top_features_json,
            )
        )

    conn = sqlite3.connect(str(p))
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        ensure_prediction_log_table(conn)
        ensure_prediction_validation_tables(conn)
        conn.executemany(
            """
            INSERT INTO prediction_log (
                scored_at, bet_ts, bet_id, session_id, player_id, game_id, canonical_id,
                casino_player_id, table_id, model_version, score, margin,
                is_alert, is_rated_obs, threshold, features_json, fe_features_missing,
                snapshot_version, mid_term_freshness_status, slow_freshness_status,
                snapshot_scoring_degraded, scoring_status, model_features_missing,
                missing_family_json, mid_term_anchor_gaming_day_event_max,
                mid_term_snapshot_age_days, mid_null_top_features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    logger.debug("[prediction_log] appended %d row(s) status=%s to %s", len(rows), status, p)


def append_skipped_entity_missing_log(
    db_path: Path | str | None,
    *,
    scored_at: str,
    model_version: str,
    skipped: pd.DataFrame,
    feature_columns: tuple[str, ...],
    threshold: float,
    feast_mid_cols: tuple[str, ...] = (),
    feast_slow_cols: tuple[str, ...] = (),
    short_term_cols: tuple[str, ...] = (),
    snapshot_version: str | None = None,
) -> None:
    """Audit Feast entity-missing rows that did not enter ``predict_proba``."""
    if skipped is None or skipped.empty:
        return
    n_feat = len(feature_columns)
    fe_n = sum(1 for c in feature_columns if str(c).startswith("fe__"))
    short_n = len(short_term_cols)
    audits = [
        RowMissingAudit(
            model_features_missing=n_feat,
            fe_features_missing=fe_n,
            feast_mid_missing=len(feast_mid_cols),
            feast_slow_missing=len(feast_slow_cols),
            short_term_missing=short_n,
        )
        for _ in range(len(skipped))
    ]
    append_hightier_prediction_log(
        db_path,
        scored_at=scored_at,
        model_version=model_version,
        staged=skipped,
        prob=np.zeros(len(skipped), dtype=np.float64),
        threshold=threshold,
        feature_columns=feature_columns,
        row_audits=audits,
        scoring_status="skipped_entity_missing",
        snapshot_version=snapshot_version,
    )


def load_processed_predictions(conn: sqlite3.Connection) -> set[str]:
    """Return ``bet_id`` values already finalized in ``processed_predictions``."""
    try:
        rows = conn.execute("SELECT bet_id FROM processed_predictions").fetchall()
        return {str(r[0]) for r in rows if r[0] is not None}
    except Exception:
        return set()


def mark_processed_predictions(conn: sqlite3.Connection, bet_ids: list[Any]) -> None:
    """Mark prediction rows as processed (idempotent upsert)."""
    if not bet_ids:
        return
    ts = datetime.now(_HK_TZ_INFO).isoformat()
    rows = [(str(bid), ts) for bid in bet_ids if bid is not None and not pd.isna(bid)]
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO processed_predictions(bet_id, processed_ts)
        VALUES (?, ?)
        ON CONFLICT(bet_id) DO UPDATE SET processed_ts=excluded.processed_ts
        """,
        rows,
    )
    conn.commit()


def save_prediction_validation_results(conn: sqlite3.Connection, final_df: pd.DataFrame) -> None:
    """Upsert rows into ``prediction_validation_results``."""
    if final_df.empty:
        return

    def _s(v: object) -> str | None:
        try:
            return None if pd.isna(v) else str(v)
        except (TypeError, ValueError):
            return str(v) if v is not None else None

    rows = [
        (
            _s(r.bet_id),
            getattr(r, "scored_at", None),
            getattr(r, "bet_ts", None),
            getattr(r, "validated_at", None),
            None if pd.isna(getattr(r, "player_id", None)) else int(r.player_id),
            _s(getattr(r, "canonical_id", None)),
            _s(getattr(r, "casino_player_id", None)),
            _s(getattr(r, "model_version", None)),
            getattr(r, "score", None),
            None if pd.isna(getattr(r, "is_alert", None)) else int(r.is_alert),
            None if pd.isna(getattr(r, "result", None)) else int(bool(r.result)),
            getattr(r, "gap_start", None),
            getattr(r, "gap_minutes", None),
            getattr(r, "reason", None),
        )
        for r in final_df.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO prediction_validation_results(
            bet_id, scored_at, bet_ts, validated_at, player_id, canonical_id,
            casino_player_id, model_version, score, is_alert, result,
            gap_start, gap_minutes, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bet_id) DO UPDATE SET
            scored_at=excluded.scored_at,
            bet_ts=excluded.bet_ts,
            validated_at=excluded.validated_at,
            player_id=excluded.player_id,
            canonical_id=excluded.canonical_id,
            casino_player_id=excluded.casino_player_id,
            model_version=excluded.model_version,
            score=excluded.score,
            is_alert=excluded.is_alert,
            result=excluded.result,
            gap_start=excluded.gap_start,
            gap_minutes=excluded.gap_minutes,
            reason=excluded.reason
        """,
        rows,
    )
    conn.commit()


def prune_prediction_validation_retention(
    conn: sqlite3.Connection,
    now_hk: datetime,
    *,
    retention_days: int,
) -> None:
    """Delete old ``prediction_validation_results`` / ``processed_predictions`` rows."""
    if retention_days <= 0:
        return
    cutoff = now_hk - timedelta(days=retention_days)
    cut_s = cutoff.isoformat()
    conn.execute(
        """
        DELETE FROM prediction_validation_results
        WHERE (
            COALESCE(validated_at, bet_ts, scored_at) < ?
        )
        """,
        (cut_s,),
    )
    conn.execute("DELETE FROM processed_predictions WHERE processed_ts < ?", (cut_s,))
    conn.commit()


def record_missing_bet_ts_poison(
    conn: sqlite3.Connection,
    *,
    bet_id: str,
    scored_at: str | None,
    now_hk: datetime,
) -> None:
    """Mark a prediction row whose ``bet_ts`` cannot be resolved (no retry)."""
    validated_at = now_hk.isoformat()
    conn.execute(
        """
        INSERT INTO prediction_validation_results(
            bet_id, scored_at, bet_ts, validated_at, result, reason
        ) VALUES (?, ?, NULL, ?, NULL, 'missing_bet_ts')
        ON CONFLICT(bet_id) DO UPDATE SET
            validated_at=excluded.validated_at,
            reason=excluded.reason
        """,
        (str(bet_id), scored_at, validated_at),
    )
    mark_processed_predictions(conn, [bet_id])


def backfill_prediction_log_bet_ts(
    db_path: Path | str,
    *,
    chunk_size: int = 500,
    fetch_by_bet_ids: Callable[..., Any] | None = None,
) -> dict[str, int]:
    """Backfill ``prediction_log.bet_ts`` from ClickHouse by ``bet_id``.

    Rows still missing after lookup are poison-pilled (``missing_bet_ts``).
    """
    p = Path(db_path).resolve()
    if fetch_by_bet_ids is None:
        from trainer_hightier.serving.validator import fetch_bet_payout_times_by_bet_ids

        fetch_by_bet_ids = fetch_bet_payout_times_by_bet_ids

    stats = {"candidates": 0, "updated": 0, "missing": 0}
    with sqlite3.connect(str(p)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        ensure_prediction_log_table(conn)
        ensure_prediction_validation_tables(conn)
        rows = conn.execute(
            """
            SELECT bet_id, scored_at
            FROM prediction_log
            WHERE bet_ts IS NULL AND bet_id IS NOT NULL
            ORDER BY prediction_id ASC
            """
        ).fetchall()
    if not rows:
        return stats

    stats["candidates"] = len(rows)
    now_hk = datetime.now(_HK_TZ_INFO)
    chunk_size = max(1, int(chunk_size))

    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        bet_ids: list[int] = []
        meta: dict[str, str | None] = {}
        unparseable: list[tuple[str, str | None]] = []
        for bid_raw, scored_at in chunk:
            try:
                bid_int = int(str(bid_raw).strip())
            except (TypeError, ValueError):
                unparseable.append((str(bid_raw), scored_at))
                continue
            bet_ids.append(bid_int)
            meta[str(bid_int)] = scored_at

        bid_map: dict[str, Any] = {}
        if bet_ids:
            bid_map, _, _, _ = fetch_by_bet_ids(bet_ids, chunk_size=chunk_size)
        with sqlite3.connect(str(p)) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            ensure_prediction_validation_tables(conn)
            for bid_str, scored_at in unparseable:
                record_missing_bet_ts_poison(
                    conn,
                    bet_id=bid_str,
                    scored_at=scored_at,
                    now_hk=now_hk,
                )
                stats["missing"] += 1
            for bid_str, scored_at in meta.items():
                hit = bid_map.get(bid_str)
                if hit is not None:
                    payout_hk, _pid = hit
                    conn.execute(
                        "UPDATE prediction_log SET bet_ts = ? WHERE bet_id = ?",
                        (payout_hk.isoformat(), bid_str),
                    )
                    stats["updated"] += 1
                else:
                    record_missing_bet_ts_poison(
                        conn,
                        bet_id=bid_str,
                        scored_at=scored_at,
                        now_hk=now_hk,
                    )
                    stats["missing"] += 1
            conn.commit()

    logger.info(
        "[prediction_log] backfill bet_ts: candidates=%s updated=%s missing=%s path=%s",
        stats["candidates"],
        stats["updated"],
        stats["missing"],
        p,
    )
    return stats
