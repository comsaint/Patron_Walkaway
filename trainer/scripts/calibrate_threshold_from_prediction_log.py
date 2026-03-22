"""T-OnlineCalibration (MVP): SQLite schema + manual runtime threshold write.

This script does **not** query ClickHouse or run full PR calibration yet; it provides:
- ``--init-schema``: create ``prediction_ground_truth`` / ``calibration_runs`` on the
  prediction log DB, and ``runtime_rated_threshold`` on the state DB.
- ``--set-runtime-threshold``: upsert row ``id=1`` in ``runtime_rated_threshold`` for
  ops/testing (scorer reads this when valid / not stale).

Full batch calibration from CH + ``pick_threshold_dec026`` can extend this CLI later.

Usage (repo root)::

    python -m trainer.scripts.calibrate_threshold_from_prediction_log --init-schema
    python -m trainer.scripts.calibrate_threshold_from_prediction_log --set-runtime-threshold 0.62 --source cli_test
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from trainer.core import config
from trainer.serving.scorer import (
    STATE_DB_PATH,
    ensure_prediction_calibration_schema,
    ensure_runtime_rated_threshold_schema,
    upsert_runtime_rated_threshold,
)


def _prediction_log_path() -> Path:
    """Default path from config; empty env → empty Path (caller must validate for --init-schema)."""
    return Path(str(getattr(config, "PREDICTION_LOG_DB_PATH", "") or "").strip())


def _state_db_path(cli: Path | None) -> Path:
    if cli is not None:
        return cli
    return Path(STATE_DB_PATH)


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
        st_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(st_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            upsert_runtime_rated_threshold(conn, t, source=args.source)
            conn.commit()
        print(f"set-runtime-threshold: {t} written to {st_path.resolve()} (source={args.source})")

    if not args.init_schema and args.set_runtime_threshold is None:
        parser.print_help()
        raise SystemExit(2)


if __name__ == "__main__":
    main()
