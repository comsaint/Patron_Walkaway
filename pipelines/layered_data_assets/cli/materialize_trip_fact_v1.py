#!/usr/bin/env python3
"""L1 ``trip_fact`` + ``trip_run_map`` (Phase 2 LDA-E2-01/02): full-snapshot trip from ``run_fact``.

Reads **all** supplied ``run_fact`` Parquet files (repeatable ``--input-run-fact``), derives trips
(SSOT: 3 empty ``gaming_day``; MVP uses run calendar gaps), writes one ``trip_start_gaming_day``
partition for both artifacts.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from ..core.trip_fact_v1 import (
    SOURCE_NAMESPACE_DEFAULT,
    TRIP_DEFINITION_VERSION_DEFAULT,
    build_trip_fact_manifest,
    build_trip_run_map_manifest,
    materialize_trip_partition_parquets,
)
from ..io.atomic_parquet_manifest_v1 import (
    commit_parquet_and_manifest,
    remove_staged_outputs,
    staged_manifest_path,
    staged_parquet_path,
)
from ..io.ingestion_delay_summary_v1 import (
    DEFAULT_LATE_THRESHOLD_SEC,
    compute_ingestion_delay_summary_preview,
)
from ..io.l1_paths import l1_trip_fact_partition_dir, l1_trip_run_map_partition_dir
from ..io.manifest_lineage_v1 import merge_source_hashes_into_manifest
from ..orchestration.oom_runner_v1 import add_duckdb_oom_cli_args, run_duckdb_job_with_oom_retries
from .repo_root import discover_repo_root


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI for ``materialize_trip_fact_v1``."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-root", type=Path, default=Path("data"), help="Repo data root (default: ./data)")
    p.add_argument("--source-snapshot-id", required=True, help="L0 batch id, e.g. snap_...")
    p.add_argument(
        "--trip-start-gaming-day",
        required=True,
        help="Partition YYYY-MM-DD: emit trips whose first run starts on this gaming_day",
    )
    p.add_argument(
        "--input-run-fact",
        dest="run_fact_inputs",
        action="append",
        type=Path,
        required=True,
        help="run_fact.parquet path (repeatable); should cover snapshot for correct trip boundaries",
    )
    p.add_argument(
        "--coverage-end",
        type=str,
        default=None,
        help="Optional YYYY-MM-DD upper bound for open-trip tail (default: max run gaming_day in inputs)",
    )
    p.add_argument(
        "--l0-fingerprint-json",
        type=Path,
        default=None,
        help="Optional snapshot_fingerprint.json for manifest source_hashes",
    )
    p.add_argument(
        "--ingestion-delay-parquet",
        type=Path,
        default=None,
        help="Parquet for ingest-delay preview (default: omit → placeholder in manifest)",
    )
    p.add_argument(
        "--late-threshold-sec",
        type=float,
        default=DEFAULT_LATE_THRESHOLD_SEC,
        help=f"Late threshold in seconds for preview (default: {DEFAULT_LATE_THRESHOLD_SEC})",
    )
    p.add_argument(
        "--trip-definition-version",
        default=TRIP_DEFINITION_VERSION_DEFAULT,
        help="Version string embedded in trip_id hash",
    )
    p.add_argument(
        "--source-namespace",
        default=SOURCE_NAMESPACE_DEFAULT,
        help="Namespace string embedded in trip_id hash",
    )
    p.add_argument(
        "--trip-fact-output-dir",
        type=Path,
        default=None,
        help="Override trip_fact directory (default: data/l1_layered/<snap>/trip_fact/trip_start_gaming_day=...)",
    )
    p.add_argument(
        "--trip-run-map-output-dir",
        type=Path,
        default=None,
        help="Override trip_run_map directory (default: .../trip_run_map/trip_start_gaming_day=...)",
    )
    add_duckdb_oom_cli_args(p)
    return p.parse_args(argv)


def _optional_coverage_end(s: str | None) -> date | None:
    if s is None or not str(s).strip():
        return None
    return date.fromisoformat(str(s).strip())


def main(argv: list[str] | None = None) -> int:
    """Materialize one ``trip_start_gaming_day`` partition (``trip_fact`` + ``trip_run_map``)."""
    try:
        import duckdb
    except ImportError:
        print("duckdb is required (see requirements.txt).", file=sys.stderr)
        return 2

    args = _parse_args(argv)
    repo_root = discover_repo_root()
    trip_dir = args.trip_fact_output_dir
    if trip_dir is None:
        trip_dir = l1_trip_fact_partition_dir(
            args.data_root.resolve(), args.source_snapshot_id, args.trip_start_gaming_day
        )
    map_dir = args.trip_run_map_output_dir
    if map_dir is None:
        map_dir = l1_trip_run_map_partition_dir(
            args.data_root.resolve(), args.source_snapshot_id, args.trip_start_gaming_day
        )
    trip_parquet = trip_dir / "trip_fact.parquet"
    trip_manifest = trip_dir / "manifest.json"
    map_parquet = map_dir / "trip_run_map.parquet"
    map_manifest = map_dir / "manifest.json"
    st_trip_pq = staged_parquet_path(trip_parquet)
    st_trip_m = staged_manifest_path(trip_manifest)
    st_map_pq = staged_parquet_path(map_parquet)
    st_map_m = staged_manifest_path(map_manifest)
    remove_staged_outputs(st_trip_pq, st_trip_m, st_map_pq, st_map_m)

    inputs = [p.resolve() for p in args.run_fact_inputs]
    cov = _optional_coverage_end(args.coverage_end)

    def _work(con: object):
        stats = materialize_trip_partition_parquets(
            con=con,
            run_fact_paths=inputs,
            trip_start_gaming_day=args.trip_start_gaming_day,
            trip_fact_out=st_trip_pq,
            trip_run_map_out=st_map_pq,
            source_snapshot_id=args.source_snapshot_id,
            trip_definition_version=args.trip_definition_version,
            source_namespace=args.source_namespace,
            coverage_end=cov,
        )
        id_summary = None
        if args.ingestion_delay_parquet is not None:
            id_summary = compute_ingestion_delay_summary_preview(
                con, args.ingestion_delay_parquet.resolve(), late_threshold_sec=args.late_threshold_sec
            )
        return stats, id_summary

    stats, id_summary = run_duckdb_job_with_oom_retries(
        connect=lambda: duckdb.connect(database=":memory:"),
        work=_work,
        input_paths=inputs,
        job_name="materialize_trip_fact_v1",
        run_log_path=args.duckdb_run_log,
        failure_context_path=args.duckdb_oom_failure_context,
        max_attempts=args.duckdb_oom_max_attempts,
        initial_memory_limit_mb=args.duckdb_initial_memory_limit_mb,
    )
    sparts = list(stats["source_partitions"])
    mf_trip = build_trip_fact_manifest(
        source_snapshot_id=args.source_snapshot_id,
        trip_start_gaming_day=args.trip_start_gaming_day,
        l0_fingerprint_path=args.l0_fingerprint_json,
        output_parquet=trip_parquet,
        manifest_uri_anchor=repo_root,
        stats=stats,
        source_partitions=sparts,
        ingestion_delay_summary=id_summary,
    )
    mf_trip = merge_source_hashes_into_manifest(mf_trip, args.l0_fingerprint_json)
    mf_map = build_trip_run_map_manifest(
        source_snapshot_id=args.source_snapshot_id,
        trip_start_gaming_day=args.trip_start_gaming_day,
        l0_fingerprint_path=args.l0_fingerprint_json,
        output_parquet=map_parquet,
        manifest_uri_anchor=repo_root,
        stats=stats,
        source_partitions=sparts,
        ingestion_delay_summary=id_summary,
    )
    mf_map = merge_source_hashes_into_manifest(mf_map, args.l0_fingerprint_json)

    commit_parquet_and_manifest(
        staged_parquet=st_trip_pq,
        final_parquet=trip_parquet,
        manifest_text=json.dumps(mf_trip, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        final_manifest=trip_manifest,
    )
    commit_parquet_and_manifest(
        staged_parquet=st_map_pq,
        final_parquet=map_parquet,
        manifest_text=json.dumps(mf_map, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        final_manifest=map_manifest,
    )
    print(f"OK wrote {trip_parquet}")
    print(f"OK wrote {trip_manifest}")
    print(f"OK wrote {map_parquet}")
    print(f"OK wrote {map_manifest}")
    print(f"OK trip_fact row_count={stats['row_count_trip_fact']} trip_run_map row_count={stats['row_count_trip_run_map']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
