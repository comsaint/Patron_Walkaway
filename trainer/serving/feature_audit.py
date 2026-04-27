"""SQLite feature audit for serving vs training parity (optional, env-gated).

Writes summary statistics and optional long-format samples to the same DB as
``prediction_log`` (``PREDICTION_LOG_DB_PATH``). Designed for bounded memory:
per-feature stats are computed one column at a time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_HK = ZoneInfo("Asia/Hong_Kong")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def feature_list_fingerprint(
    feature_list: Sequence[str],
    feature_list_meta: Optional[Sequence[Mapping[str, Any]]],
) -> str:
    """Stable hash for artifact feature list + optional Step-6 metadata."""
    if feature_list_meta:
        payload = json.dumps(list(feature_list_meta), sort_keys=True, default=str)
    else:
        payload = json.dumps(list(feature_list), sort_keys=True, default=str)
    return _sha256_text(payload)


def feature_spec_fingerprint(feature_spec: Optional[Mapping[str, Any]]) -> str:
    """Hash parsed feature spec dict (frozen YAML snapshot in bundle)."""
    if not feature_spec:
        return ""
    return _sha256_text(json.dumps(dict(feature_spec), sort_keys=True, default=str))


def track_by_feature_name(
    feature_list: Sequence[str],
    feature_list_meta: Optional[Sequence[Mapping[str, Any]]],
) -> Dict[str, str]:
    out: Dict[str, str] = {str(n): "unknown" for n in feature_list}
    if not feature_list_meta:
        return out
    for e in feature_list_meta:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if name is None:
            continue
        tr = e.get("track")
        out[str(name)] = str(tr) if tr is not None else "unknown"
    return out


def ensure_feature_audit_schema(conn: sqlite3.Connection) -> None:
    """Create feature audit tables if missing (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scored_at TEXT NOT NULL,
            model_version TEXT NOT NULL,
            feature_list_hash TEXT NOT NULL,
            feature_spec_hash TEXT NOT NULL,
            effective_threshold REAL NOT NULL,
            bundle_threshold REAL NOT NULL,
            row_count INTEGER NOT NULL,
            rated_count INTEGER NOT NULL,
            sample_count INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'serving',
            model_features_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_audit_row_sample (
            audit_run_id INTEGER NOT NULL,
            bet_id TEXT,
            player_id TEXT,
            canonical_id TEXT,
            session_id TEXT,
            casino_player_id TEXT,
            score REAL,
            margin REAL,
            is_alert INTEGER NOT NULL,
            is_rated_obs INTEGER NOT NULL,
            payout_complete_dtm TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feature_audit_row_run
        ON feature_audit_row_sample(audit_run_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_audit_feature_summary (
            audit_run_id INTEGER NOT NULL,
            feature_name TEXT NOT NULL,
            track TEXT NOT NULL,
            count_n INTEGER NOT NULL,
            null_count INTEGER NOT NULL,
            zero_count INTEGER NOT NULL,
            mean_v REAL,
            std_v REAL,
            min_v REAL,
            p01 REAL,
            p05 REAL,
            p50 REAL,
            p95 REAL,
            p99 REAL,
            max_v REAL,
            PRIMARY KEY (audit_run_id, feature_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_audit_feature_sample_long (
            audit_run_id INTEGER NOT NULL,
            bet_id TEXT NOT NULL,
            feature_name TEXT NOT NULL,
            feature_value REAL,
            PRIMARY KEY (audit_run_id, bet_id, feature_name)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feature_audit_long_run
        ON feature_audit_feature_sample_long(audit_run_id)
        """
    )


def prune_feature_audit_old(conn: sqlite3.Connection, retention_hours: float) -> None:
    """Delete audit rows older than *retention_hours* (best-effort, HK timeline)."""
    if not math.isfinite(retention_hours) or retention_hours <= 0:
        return
    try:
        cutoff = pd.Timestamp.now(tz=_HK) - pd.Timedelta(hours=float(retention_hours))
    except Exception:
        return
    try:
        rows = conn.execute("SELECT id, scored_at FROM feature_audit_runs").fetchall()
    except sqlite3.Error as exc:
        logger.warning("[feature_audit] retention scan failed: %s", exc)
        return
    to_del: List[int] = []
    for rid, ts_raw in rows:
        try:
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize(_HK)
            else:
                ts = ts.tz_convert(_HK)
        except Exception:
            continue
        if ts < cutoff:
            to_del.append(int(rid))
    if not to_del:
        return
    ph = ",".join("?" for _ in to_del)
    try:
        conn.execute(
            f"DELETE FROM feature_audit_feature_sample_long WHERE audit_run_id IN ({ph})",
            to_del,
        )
        conn.execute(
            f"DELETE FROM feature_audit_feature_summary WHERE audit_run_id IN ({ph})",
            to_del,
        )
        conn.execute(
            f"DELETE FROM feature_audit_row_sample WHERE audit_run_id IN ({ph})",
            to_del,
        )
        conn.execute(f"DELETE FROM feature_audit_runs WHERE id IN ({ph})", to_del)
    except sqlite3.Error as exc:
        logger.warning("[feature_audit] retention prune failed: %s", exc)


def _numeric_series_stats(
    arr: np.ndarray,
) -> Tuple[int, int, Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    """null_count, zero_count, mean, std, min, p01..p99, max for float array (len = row_count)."""
    null_count = int(np.sum(~np.isfinite(arr)))
    fin = arr[np.isfinite(arr)]
    count_fin = int(fin.size)
    if count_fin == 0:
        return null_count, 0, None, None, None, None, None, None, None, None, None
    zero_count = int(np.sum(fin == 0.0))
    mean_v = float(np.mean(fin))
    std_v = float(np.std(fin, ddof=0)) if count_fin > 1 else 0.0
    min_v = float(np.min(fin))
    max_v = float(np.max(fin))
    qs = np.nanpercentile(fin, [1.0, 5.0, 50.0, 95.0, 99.0])
    p01, p05, p50, p95, p99 = (float(qs[0]), float(qs[1]), float(qs[2]), float(qs[3]), float(qs[4]))
    return null_count, zero_count, mean_v, std_v, min_v, p01, p05, p50, p95, p99, max_v


def _deterministic_sample_positions(df: pd.DataFrame, k: int) -> List[int]:
    """Return up to *k* iloc positions, stable order by MD5(bet_id)."""
    if df.empty or k <= 0:
        return []
    n = len(df)
    if "bet_id" not in df.columns:
        return list(range(min(k, n)))
    bids = df["bet_id"].astype(str).tolist()
    scored = [(hashlib.md5(b.encode("utf-8")).hexdigest(), i) for i, b in enumerate(bids)]
    scored.sort(key=lambda x: x[0])
    return [i for _, i in scored[: min(k, n)]]


def write_serving_feature_audit(
    *,
    pred_log_path: str,
    df: pd.DataFrame,
    model_features: List[str],
    artifacts: Dict[str, Any],
    feature_list: List[str],
    scored_at: str,
    model_version: str,
    effective_threshold: float,
    bundle_threshold: float,
    store_long_values: bool,
    retention_hours: float,
    sample_rows: int,
    source: str = "serving",
) -> None:
    """Persist one audit run + per-feature summaries + optional long samples."""
    path = str(pred_log_path).strip()
    if not path or df.empty or not model_features:
        return

    fl_hash = feature_list_fingerprint(feature_list, artifacts.get("feature_list_meta"))
    fs_hash = feature_spec_fingerprint(artifacts.get("feature_spec"))
    tracks = track_by_feature_name(feature_list, artifacts.get("feature_list_meta"))

    rated_count = int(df["is_rated"].sum()) if "is_rated" in df.columns else len(df)
    row_count = len(df)
    sample_cap = max(0, int(sample_rows))
    sample_positions = _deterministic_sample_positions(df, sample_cap)
    sample_count = len(sample_positions)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        ensure_feature_audit_schema(conn)
        prune_feature_audit_old(conn, retention_hours)

        mf_json = json.dumps(list(model_features), ensure_ascii=False)
        cur = conn.execute(
            """
            INSERT INTO feature_audit_runs (
                scored_at, model_version, feature_list_hash, feature_spec_hash,
                effective_threshold, bundle_threshold, row_count, rated_count,
                sample_count, source, model_features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scored_at,
                model_version,
                fl_hash,
                fs_hash,
                float(effective_threshold),
                float(bundle_threshold),
                row_count,
                rated_count,
                sample_count,
                str(source)[:64],
                mf_json,
            ),
        )
        audit_run_id = int(cur.lastrowid)

        summary_rows: List[Tuple[Any, ...]] = []
        for fname in model_features:
            if fname not in df.columns:
                summary_rows.append(
                    (
                        audit_run_id,
                        str(fname),
                        tracks.get(str(fname), "unknown"),
                        row_count,
                        row_count,
                        0,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                )
                continue
            s = pd.to_numeric(df[fname], errors="coerce").to_numpy(dtype=np.float64, copy=False)
            nc, zc, mean_v, std_v, min_v, p01, p05, p50, p95, p99, max_v = _numeric_series_stats(s)
            summary_rows.append(
                (
                    audit_run_id,
                    str(fname),
                    tracks.get(str(fname), "unknown"),
                    row_count,
                    nc,
                    zc,
                    mean_v,
                    std_v,
                    min_v,
                    p01,
                    p05,
                    p50,
                    p95,
                    p99,
                    max_v,
                )
            )

        conn.executemany(
            """
            INSERT INTO feature_audit_feature_summary (
                audit_run_id, feature_name, track, count_n, null_count, zero_count,
                mean_v, std_v, min_v, p01, p05, p50, p95, p99, max_v
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            summary_rows,
        )

        def _cell(row: pd.Series, col: str) -> Optional[str]:
            if col not in row.index:
                return None
            v = row[col]
            if v is None or (isinstance(v, float) and not math.isfinite(v)):
                return None
            if pd.isna(v):
                return None
            return str(v)

        def _cell_ts(row: pd.Series, col: str) -> Optional[str]:
            if col not in row.index:
                return None
            v = row[col]
            if v is None or pd.isna(v):
                return None
            try:
                return pd.Timestamp(v).isoformat()
            except Exception:
                return str(v)

        row_inserts: List[Tuple[Any, ...]] = []
        for pos in sample_positions:
            r = df.iloc[pos]
            score_v = float(r["score"]) if "score" in r.index and pd.notna(r.get("score")) else None
            margin_v = float(r["margin"]) if "margin" in r.index and pd.notna(r.get("margin")) else None
            is_ro = int(r["is_rated_obs"]) if "is_rated_obs" in r.index and pd.notna(r.get("is_rated_obs")) else 0
            ialert = 1 if (margin_v is not None and margin_v >= 0.0 and is_ro == 1) else 0
            row_inserts.append(
                (
                    audit_run_id,
                    _cell(r, "bet_id"),
                    _cell(r, "player_id"),
                    _cell(r, "canonical_id"),
                    _cell(r, "session_id"),
                    _cell(r, "casino_player_id"),
                    score_v,
                    margin_v,
                    ialert,
                    is_ro,
                    _cell_ts(r, "payout_complete_dtm"),
                )
            )
        if row_inserts:
            conn.executemany(
                """
                INSERT INTO feature_audit_row_sample (
                    audit_run_id, bet_id, player_id, canonical_id, session_id,
                    casino_player_id, score, margin, is_alert, is_rated_obs,
                    payout_complete_dtm
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row_inserts,
            )

        if store_long_values and sample_positions:
            long_rows: List[Tuple[Any, ...]] = []
            bid_col = "bet_id" if "bet_id" in df.columns else None
            for pos in sample_positions:
                r = df.iloc[pos]
                bid = _cell(r, "bet_id") if bid_col else str(pos)
                if bid is None:
                    continue
                for fname in model_features:
                    if fname not in df.columns:
                        long_rows.append((audit_run_id, bid, str(fname), None))
                        continue
                    raw = r[fname]
                    if raw is None or pd.isna(raw):
                        long_rows.append((audit_run_id, bid, str(fname), None))
                        continue
                    try:
                        fv = float(raw)
                    except (TypeError, ValueError):
                        long_rows.append((audit_run_id, bid, str(fname), None))
                        continue
                    if not math.isfinite(fv):
                        long_rows.append((audit_run_id, bid, str(fname), None))
                        continue
                    long_rows.append((audit_run_id, bid, str(fname), fv))
            batch = 5000
            for i in range(0, len(long_rows), batch):
                conn.executemany(
                    """
                    INSERT INTO feature_audit_feature_sample_long (
                        audit_run_id, bet_id, feature_name, feature_value
                    ) VALUES (?, ?, ?, ?)
                    """,
                    long_rows[i : i + batch],
                )

        conn.commit()
    finally:
        conn.close()


def write_training_feature_audit_run(
    *,
    out_db_path: str,
    df: pd.DataFrame,
    model_features: List[str],
    feature_list: List[str],
    feature_list_meta: Optional[Sequence[Mapping[str, Any]]],
    feature_spec: Optional[Mapping[str, Any]],
    model_version: str,
    bundle_threshold: float,
    effective_threshold: Optional[float] = None,
    scored_at_iso: Optional[str] = None,
    retention_hours: float = 8760.0,
) -> None:
    """Write summary-only audit rows (``source='training'``) for offline parity checks."""
    artifacts: Dict[str, Any] = {
        "feature_list_meta": feature_list_meta,
        "feature_spec": feature_spec,
    }
    eff = float(effective_threshold) if effective_threshold is not None else float(bundle_threshold)
    ts = scored_at_iso or pd.Timestamp.now(tz=_HK).isoformat()
    write_serving_feature_audit(
        pred_log_path=out_db_path,
        df=df,
        model_features=model_features,
        artifacts=artifacts,
        feature_list=feature_list,
        scored_at=ts,
        model_version=model_version,
        effective_threshold=eff,
        bundle_threshold=float(bundle_threshold),
        store_long_values=False,
        retention_hours=float(retention_hours),
        sample_rows=0,
        source="training",
    )
