"""CLI for training config recommender (PLAN § training-config-recommender).

Usage:
  python -m trainer.scripts.recommend_training_config --data-source parquet --chunk-dir .data/chunks --session-parquet ../data/gmwds_t_session.parquet --days 30
  python -m trainer.scripts.recommend_training_config --data-source clickhouse --days 30
  python -m trainer.scripts.recommend_training_config --data-source clickhouse --days 30 --no-ch-query --estimated-bytes-per-chunk 209715200

Output: Resources summary, data profile, per-step estimates, and suggested parameters.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as script or as module
_REPO = Path(__file__).resolve().parent.parent.parent
if _REPO.name == "trainer":
    _REPO = _REPO.parent
sys.path.insert(0, str(_REPO))

from trainer.training_config_recommender import (  # noqa: E402
    build_data_profile_clickhouse,
    build_data_profile_parquet,
    estimate_per_step,
    get_system_resources,
    suggest_config,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Recommend training config from system resources and data profile (Parquet or ClickHouse)."
    )
    p.add_argument(
        "--data-source",
        choices=("parquet", "clickhouse"),
        required=True,
        help="Data source: parquet (local chunk dir + session) or clickhouse (connect and query).",
    )
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Training window in days (default 30).",
    )
    # Parquet
    p.add_argument(
        "--chunk-dir",
        type=Path,
        default=None,
        help="[Parquet] Directory containing chunk_*.parquet files (default trainer/.data/chunks).",
    )
    p.add_argument(
        "--session-parquet",
        type=Path,
        default=None,
        help="[Parquet] Path to session Parquet for session_data_bytes (optional).",
    )
    # ClickHouse fallback when no query
    p.add_argument(
        "--no-ch-query",
        action="store_true",
        help="[ClickHouse] Skip connecting to CH; use --estimated-* or defaults.",
    )
    p.add_argument(
        "--estimated-bytes-per-chunk",
        type=int,
        default=None,
        help="[ClickHouse fallback] Assumed bytes per chunk when CH query unavailable.",
    )
    p.add_argument(
        "--estimated-rows-per-day",
        type=int,
        default=None,
        help="[ClickHouse fallback] Assumed rows/day for session estimate when CH query unavailable.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(message)s",
    )

    if args.days < 1:
        logging.error("--days must be >= 1")
        return 2

    # Resource detection
    chunk_dir = args.chunk_dir
    if chunk_dir is not None and not chunk_dir.is_absolute():
        chunk_dir = (_REPO / "trainer" / chunk_dir).resolve()
    elif chunk_dir is None and args.data_source == "parquet":
        chunk_dir = _REPO / "trainer" / ".data" / "chunks"
    disk_path = chunk_dir if isinstance(chunk_dir, Path) else _REPO / "trainer" / ".data"

    try:
        import trainer.config as _config  # type: ignore[import]
        step7_temp = getattr(_config, "STEP7_DUCKDB_TEMP_DIR", None)
    except Exception:
        step7_temp = None
    resources = get_system_resources(disk_path=disk_path, step7_temp_dir=step7_temp)

    print("=== Resources ===")
    print("  RAM total:     %.1f GB" % resources.get("ram_total_gb", 0))
    print("  RAM available: %.1f GB" % resources.get("ram_available_gb", 0))
    print("  CPU count:    %s" % resources.get("cpu_count", 0))
    print("  Disk free:    %.1f GB" % resources.get("disk_available_gb", 0))

    if args.data_source == "parquet":
        session_path = args.session_parquet
        if session_path is not None and not session_path.is_absolute():
            session_path = _REPO / session_path
        profile = build_data_profile_parquet(
            chunk_dir or Path("."),
            args.days,
            session_parquet_path=session_path,
        )
    else:
        profile = build_data_profile_clickhouse(
            args.days,
            skip_ch_connect=args.no_ch_query,
            estimated_bytes_per_chunk=args.estimated_bytes_per_chunk,
            estimated_rows_per_day=args.estimated_rows_per_day,
        )

    print("\n=== Data profile (%s) ===" % profile.get("data_source", "?"))
    print("  training_days: %s" % profile.get("training_days"))
    print("  chunk_count: %s" % profile.get("chunk_count"))
    print("  total_chunk_bytes_estimate: %s" % profile.get("total_chunk_bytes_estimate"))
    print("  session_data_bytes: %s" % profile.get("session_data_bytes"))
    print("  has_existing_chunks: %s" % profile.get("has_existing_chunks"))

    estimates = estimate_per_step(profile, resources)
    print("\n=== Per-step estimates (approx., see doc/training_oom_and_runtime_audit.md) ===")
    for k, v in sorted(estimates.items()):
        if isinstance(v, float):
            print("  %s: %.2f" % (k, v))
        else:
            print("  %s: %s" % (k, v))

    suggestions = suggest_config(profile, resources, estimates)
    print("\n=== Suggestions ===")
    for param, reason in suggestions:
        print("  %s — %s" % (param, reason))

    return 0


if __name__ == "__main__":
    sys.exit(main())
