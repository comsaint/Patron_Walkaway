"""T-OnlineCalibration (MVP): SQLite schema + manual runtime threshold write.

This script does **not** query ClickHouse or run full PR calibration yet; it provides:
- ``--init-schema``: create ``prediction_ground_truth`` / ``calibration_runs`` on the
  prediction log DB, and ``runtime_rated_threshold`` on the state DB.
- ``--set-runtime-threshold``: upsert row ``id=1`` in ``runtime_rated_threshold`` for
  ops/testing (scorer reads this when valid / not stale).
- ``--log-calibration-run`` (with ``--set-runtime-threshold``): append ``calibration_runs``
  on the prediction log DB with W2 ``summary_json`` (contract + CLI provenance).

Full batch calibration from CH + ``pick_threshold_dec026`` can extend this CLI later.

Usage (repo root)::

    python -m trainer.scripts.calibrate_threshold_from_prediction_log --init-schema
    python -m trainer.scripts.calibrate_threshold_from_prediction_log --set-runtime-threshold 0.62 --source cli_test
    python -m trainer.scripts.calibrate_threshold_from_prediction_log --set-runtime-threshold 0.62 --log-calibration-run --prediction-log-db path/to/prediction_log.db
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from trainer.core import config
from trainer.serving.scorer import (
    HK_TZ,
    STATE_DB_PATH,
    ensure_prediction_calibration_schema,
    ensure_runtime_rated_threshold_schema,
    insert_calibration_run_row,
    upsert_runtime_rated_threshold,
)
from trainer.training.threshold_selection import pick_threshold_dec026


def _prediction_log_path() -> Path:
    """Default path from config; empty env → empty Path (caller must validate for --init-schema)."""
    return Path(str(getattr(config, "PREDICTION_LOG_DB_PATH", "") or "").strip())


def _state_db_path(cli: Path | None) -> Path:
    if cli is not None:
        return cli
    return Path(STATE_DB_PATH)


def _prediction_log_path_for_calibration(args: argparse.Namespace) -> Path:
    if args.prediction_log_db is not None:
        raw = str(args.prediction_log_db).strip()
        if not raw:
            raise SystemExit("--prediction-log-db must not be empty")
        return Path(raw)
    raw = str(getattr(config, "PREDICTION_LOG_DB_PATH", "") or "").strip()
    if not raw:
        raise SystemExit("PREDICTION_LOG_DB_PATH is empty; set env var or pass --prediction-log-db")
    return Path(raw)


def _load_batch_calibration_rows(
    conn: sqlite3.Connection,
    *,
    window_hours: float,
    status_filter: Optional[str] = None,
) -> pd.DataFrame:
    end_ts = datetime.now(HK_TZ)
    start_ts = end_ts - timedelta(hours=float(window_hours))
    sql = """
        SELECT p.score AS score, g.label AS label, g.status AS gt_status, p.scored_at AS scored_at
        FROM prediction_log p
        INNER JOIN prediction_ground_truth g ON g.bet_id = p.bet_id
        WHERE p.is_rated_obs = 1
          AND p.scored_at >= ?
          AND p.scored_at < ?
    """
    params: list = [start_ts.isoformat(), end_ts.isoformat()]
    if status_filter:
        sql += " AND g.status = ?"
        params.append(status_filter)
    return pd.read_sql_query(sql, conn, params=params)


def _coerce_binary_label(v: object) -> Optional[int]:
    if v is None or pd.isna(v):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return 1 if f >= 0.5 else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize calibration schema and/or set runtime rated threshold (state DB)."
    )
    parser.add_argument(
        "--prediction-log-db",
        type=Path,
        default=None,
        help="Override prediction_log SQLite path (default: config PREDICTION_LOG_DB_PATH)",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="Override state SQLite path (default: scorer STATE_DB_PATH)",
    )
    parser.add_argument(
        "--init-schema",
        action="store_true",
        help="Create prediction_ground_truth / calibration_runs / runtime_rated_threshold if missing",
    )
    parser.add_argument(
        "--set-runtime-threshold",
        type=float,
        default=None,
        metavar="T",
        help="Upsert runtime_rated_threshold id=1 (must be strictly between 0 and 1)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="calibrate_cli",
        help="Provenance string stored with the runtime threshold row",
    )
    parser.add_argument(
        "--selection-mode",
        type=str,
        default=None,
        metavar="MODE",
        help="Optional W2 contract tag (e.g. legacy, field_test) stored in runtime_rated_threshold.selection_mode",
    )
    parser.add_argument(
        "--log-calibration-run",
        action="store_true",
        help="Append calibration_runs on prediction log DB (requires --set-runtime-threshold and a non-empty prediction log path)",
    )
    parser.add_argument(
        "--run-batch-calibration",
        action="store_true",
        help="Run batch threshold calibration from prediction_log + prediction_ground_truth, then append calibration_runs",
    )
    parser.add_argument(
        "--calibration-window-hours",
        type=float,
        default=24.0,
        metavar="H",
        help="Lookback hours for --run-batch-calibration (default: 24)",
    )
    parser.add_argument(
        "--ground-truth-status",
        type=str,
        default=None,
        metavar="STATUS",
        help="Optional exact status filter on prediction_ground_truth.status during batch calibration",
    )
    parser.add_argument(
        "--apply-batch-threshold-to-state",
        action="store_true",
        help="With --run-batch-calibration, upsert suggested threshold into state DB when non-fallback",
    )
    args = parser.parse_args()

    pl_path = args.prediction_log_db or _prediction_log_path()
    st_path = _state_db_path(args.state_db)

    if args.init_schema:
        # Do not use str(Path) alone: Path('') may normalize to '.' on Windows.
        if args.prediction_log_db is not None:
            if not str(args.prediction_log_db).strip():
                raise SystemExit(
                    "--prediction-log-db must not be empty when using --init-schema"
                )
        else:
            raw_pl = str(getattr(config, "PREDICTION_LOG_DB_PATH", "") or "").strip()
            if not raw_pl:
                raise SystemExit(
                    "PREDICTION_LOG_DB_PATH is empty; set the env var or pass --prediction-log-db"
                )
        pl_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(pl_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            ensure_prediction_calibration_schema(conn)
            conn.commit()
        print(f"init-schema: prediction log DB OK -> {pl_path.resolve()}")

        st_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(st_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            ensure_runtime_rated_threshold_schema(conn)
            conn.commit()
        print(f"init-schema: state DB OK -> {st_path.resolve()}")

    if args.set_runtime_threshold is not None:
        t = float(args.set_runtime_threshold)
        if not (0.0 < t < 1.0):
            raise SystemExit("--set-runtime-threshold must be strictly between 0 and 1")
        pl_log_path: Path | None = None
        if args.log_calibration_run:
            # Avoid Path("") -> "." ambiguity; validate raw source like --init-schema branch.
            if args.prediction_log_db is not None:
                _raw = str(args.prediction_log_db).strip()
                if not _raw:
                    raise SystemExit(
                        "--prediction-log-db must not be empty when using --log-calibration-run"
                    )
                pl_log_path = Path(_raw)
            else:
                _raw = str(getattr(config, "PREDICTION_LOG_DB_PATH", "") or "").strip()
                if not _raw:
                    raise SystemExit(
                        "--log-calibration-run requires --prediction-log-db or config PREDICTION_LOG_DB_PATH"
                    )
                pl_log_path = Path(_raw)
        st_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(st_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            upsert_runtime_rated_threshold(
                conn,
                t,
                source=args.source,
                selection_mode=args.selection_mode,
            )
            conn.commit()
        print(f"set-runtime-threshold: {t} written to {st_path.resolve()} (source={args.source})")
        if args.log_calibration_run:
            assert pl_log_path is not None
            pl_log_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(pl_log_path) as pl_conn:
                pl_conn.execute("PRAGMA journal_mode=WAL;")
                _rid = insert_calibration_run_row(
                    pl_conn,
                    suggested_threshold=t,
                    applied_to_state=True,
                    summary_extras={
                        "operation": "set_runtime_threshold",
                        "runtime_threshold_source": args.source,
                        "selection_mode_written_to_state": (
                            str(args.selection_mode).strip() if args.selection_mode else None
                        ),
                        "state_db_path": str(st_path.resolve()),
                    },
                )
                pl_conn.commit()
            print(
                f"log-calibration-run: calibration_runs id={_rid} -> {pl_log_path.resolve()}"
            )

    if args.run_batch_calibration:
        wh = float(args.calibration_window_hours)
        if not np.isfinite(wh) or wh <= 0.0:
            raise SystemExit("--calibration-window-hours must be > 0")
        plb = _prediction_log_path_for_calibration(args)
        plb.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(plb) as pl_conn:
            pl_conn.execute("PRAGMA journal_mode=WAL;")
            ensure_prediction_calibration_schema(pl_conn)
            df = _load_batch_calibration_rows(
                pl_conn,
                window_hours=wh,
                status_filter=(str(args.ground_truth_status).strip() if args.ground_truth_status else None),
            )
            if df.empty:
                rid = insert_calibration_run_row(
                    pl_conn,
                    suggested_threshold=0.5,
                    applied_to_state=False,
                    window_hours=wh,
                    n_rows_used=0,
                    n_pos=0,
                    skipped_reason="no_rows_in_window",
                    summary_extras={
                        "operation": "batch_calibration",
                        "ground_truth_status_filter": args.ground_truth_status,
                    },
                )
                pl_conn.commit()
                print(f"batch-calibration: no rows in window; logged calibration_runs id={rid}")
            else:
                labels = df["label"].map(_coerce_binary_label)
                score_s = pd.to_numeric(df["score"], errors="coerce")
                ok = labels.notna() & score_s.notna()
                y_true = labels[ok].astype(int).to_numpy(dtype=np.int64)
                y_score = score_s[ok].astype(float).to_numpy(dtype=np.float64)
                n_rows = int(len(y_true))
                n_pos = int(np.sum(y_true == 1))
                recall_floor = getattr(config, "THRESHOLD_MIN_RECALL", 0.01)
                min_alert_count = int(getattr(config, "THRESHOLD_MIN_ALERT_COUNT", 5))
                min_alerts_per_hour = getattr(config, "THRESHOLD_MIN_ALERTS_PER_HOUR", None)
                pick = pick_threshold_dec026(
                    y_true,
                    y_score,
                    recall_floor=recall_floor,
                    min_alert_count=min_alert_count,
                    min_alerts_per_hour=min_alerts_per_hour,
                    window_hours=wh,
                )
                apply_ok = bool(args.apply_batch_threshold_to_state) and not bool(pick.is_fallback)
                if apply_ok:
                    st_path.parent.mkdir(parents=True, exist_ok=True)
                    with sqlite3.connect(st_path) as st_conn:
                        st_conn.execute("PRAGMA journal_mode=WAL;")
                        upsert_runtime_rated_threshold(
                            st_conn,
                            float(pick.threshold),
                            source=f"{args.source}:batch_calibration",
                            n_mature=n_rows,
                            n_pos=n_pos,
                            window_hours=wh,
                            recall_at_threshold=float(pick.recall),
                            precision_at_threshold=float(pick.precision),
                            selection_mode=args.selection_mode,
                        )
                        st_conn.commit()
                rid = insert_calibration_run_row(
                    pl_conn,
                    suggested_threshold=float(pick.threshold),
                    applied_to_state=apply_ok,
                    window_hours=wh,
                    n_rows_used=n_rows,
                    n_pos=n_pos,
                    skipped_reason=("fallback_threshold" if pick.is_fallback else None),
                    summary_extras={
                        "operation": "batch_calibration",
                        "source": args.source,
                        "ground_truth_status_filter": args.ground_truth_status,
                        "selection_mode_written_to_state": (
                            str(args.selection_mode).strip() if (args.selection_mode and apply_ok) else None
                        ),
                        "batch_pick": {
                            "threshold": float(pick.threshold),
                            "precision": float(pick.precision),
                            "recall": float(pick.recall),
                            "f1": float(pick.f1),
                            "fbeta": float(pick.fbeta),
                            "is_fallback": bool(pick.is_fallback),
                            "recall_floor": recall_floor,
                            "min_alert_count": min_alert_count,
                            "min_alerts_per_hour": min_alerts_per_hour,
                            "window_hours": wh,
                        },
                    },
                )
                pl_conn.commit()
                print(
                    f"batch-calibration: suggested_threshold={float(pick.threshold):.6f} "
                    f"(fallback={bool(pick.is_fallback)}) logged id={rid}; "
                    f"applied_to_state={apply_ok}"
                )

    if args.log_calibration_run and args.set_runtime_threshold is None:
        raise SystemExit("--log-calibration-run requires --set-runtime-threshold")

    if (
        not args.init_schema
        and args.set_runtime_threshold is None
        and not args.run_batch_calibration
    ):
        parser.print_help()
        raise SystemExit(2)


if __name__ == "__main__":
    main()
