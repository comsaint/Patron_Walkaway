"""CLI for training config recommender (PLAN § training-config-recommender).

Usage:
  python -m trainer.scripts.recommend_training_config --data-source parquet --days 30
  python -m trainer.scripts.recommend_training_config --data-source clickhouse --days 30
  python -m trainer.scripts.recommend_training_config --data-source clickhouse --days 30 --no-ch-query --estimated-bytes-per-chunk 209715200

Parquet mode uses the same paths as the trainer (CHUNK_DIR, LOCAL_PARQUET_DIR) so no path args are needed.
Optional --chunk-dir / --session-parquet override for testing. Output: resources, data profile, estimates, suggestions.
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
    # Parquet (defaults mirror trainer: CHUNK_DIR, LOCAL_PARQUET_DIR/gmwds_t_session.parquet)
    p.add_argument(
        "--chunk-dir",
        type=Path,
        default=None,
        help="[Parquet] Override chunk directory (default: same as trainer CHUNK_DIR).",
    )
    p.add_argument(
        "--session-parquet",
        type=Path,
        default=None,
        help="[Parquet] Override session Parquet path (default: same as trainer LOCAL_PARQUET_DIR/gmwds_t_session.parquet).",
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

    # Parquet: use same paths as trainer (single source of truth) unless overridden
    if args.data_source == "parquet":
        from trainer.trainer import CHUNK_DIR, LOCAL_PARQUET_DIR  # noqa: E402

        chunk_dir = args.chunk_dir
        if chunk_dir is not None:
            if not chunk_dir.is_absolute():
                chunk_dir = (_REPO / "trainer" / chunk_dir).resolve()
        else:
            chunk_dir = CHUNK_DIR
        session_path = args.session_parquet
        if session_path is not None:
            if not session_path.is_absolute():
                session_path = (_REPO / session_path).resolve()
        else:
            session_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    else:
        chunk_dir = None

    # Resource detection
    disk_path = chunk_dir if (chunk_dir is not None and isinstance(chunk_dir, Path)) else _REPO / "trainer" / ".data"

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
        profile = build_data_profile_parquet(
            chunk_dir,
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

    # Total time (sum of step times) and peak RAM (max of step peaks)
    time_keys = ["step3_time_min", "step4_time_min", "step6_total_time_min", "step7_time_min", "step8_time_min", "step9_time_min"]
    ram_keys = ["step3_peak_ram_gb", "step4_peak_ram_gb", "step6_per_chunk_ram_gb", "step7_peak_ram_gb", "step8_peak_ram_gb", "step9_peak_ram_gb"]
    total_time_min = sum(estimates.get(k, 0) for k in time_keys if isinstance(estimates.get(k), (int, float)))
    peak_ram_gb = max((estimates.get(k, 0) for k in ram_keys if isinstance(estimates.get(k), (int, float))), default=0.0)
    print("  total_time_min: %.2f" % total_time_min)
    print("  peak_ram_gb: %.2f" % peak_ram_gb)

    suggestions = suggest_config(profile, resources, estimates)
    print("\n=== Suggestions ===")
    for param, reason in suggestions:
        print("  %s — %s" % (param, reason))

    return 0


if __name__ == "__main__":
    sys.exit(main())
