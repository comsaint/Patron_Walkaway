#!/usr/bin/env python3
"""L1 ``run_fact`` materialization (LDA-E1-03): gap-based runs + deterministic ``run_id``.

Reads cleaned ``t_bet`` Parquet (``preprocess_bet_v1`` output), assigns runs by
``RUN_BREAK_MIN`` (default 30, aligned with trainer), hashes ``run_id`` per
implementation plan §4.1, writes ``run_fact.parquet`` + ``manifest.json`` under
``data/l1_layered/<source_snapshot_id>/run_fact/run_end_gaming_day=.../``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from layered_data_assets.ingestion_delay_summary_v1 import (  # noqa: E402
    DEFAULT_LATE_THRESHOLD_SEC,
    compute_ingestion_delay_summary_preview,
)
from layered_data_assets.l1_paths import l1_run_fact_partition_dir  # noqa: E402
from layered_data_assets.manifest_lineage_v1 import merge_source_hashes_into_manifest  # noqa: E402
from layered_data_assets.oom_runner_v1 import add_duckdb_oom_cli_args, run_duckdb_job_with_oom_retries  # noqa: E402
from layered_data_assets.run_fact_v1 import (  # noqa: E402
    RUN_BREAK_MIN_DEFAULT,
    RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
    SOURCE_NAMESPACE_DEFAULT,
    build_run_fact_manifest,
    materialize_run_fact_v1,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI for ``materialize_run_fact_v1``."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-root", type=Path, default=Path("data"), help="Repo data root (default: ./data)")
    p.add_argument("--source-snapshot-id", required=True, help="L0 batch id, e.g. snap_...")
    p.add_argument(
        "--run-end-gaming-day",
        required=True,
        help="Partition YYYY-MM-DD: emit runs whose last bet's gaming_day equals this",
    )
    p.add_argument(
        "--l1-preprocess-gaming-day",
        required=True,
        help="Upstream cleaned-bet partition label for manifest source_partitions (YYYY-MM-DD)",
    )
    p.add_argument(
        "--input",
        dest="inputs",
        action="append",
        type=Path,
        required=True,
        help="Cleaned bet Parquet (repeatable); must include player_id, bet_id, gaming_day, payout_complete_dtm",
    )
    p.add_argument(
        "--l0-fingerprint-json",
        type=Path,
        default=None,
        help="Optional snapshot_fingerprint.json for manifest source_hashes",
    )
    p.add_argument(
        "--run-break-min",
        type=float,
        default=RUN_BREAK_MIN_DEFAULT,
        help=f"Minutes gap to start a new run (default: {RUN_BREAK_MIN_DEFAULT})",
    )
    p.add_argument(
        "--run-definition-version",
        default=RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
        help="Version string embedded in run_id hash (default: run_boundary_v1)",
    )
    p.add_argument(
        "--source-namespace",
        default=SOURCE_NAMESPACE_DEFAULT,
        help="Namespace string embedded in run_id hash",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override output directory (default: data/l1_layered/<snap>/run_fact/run_end_gaming_day=...)",
    )
    p.add_argument(
        "--ingestion-delay-parquet",
        type=Path,
        default=None,
        help="Parquet for ingest-delay preview (default: first --input cleaned bet file)",
    )
    p.add_argument(
        "--late-threshold-sec",
        type=float,
        default=DEFAULT_LATE_THRESHOLD_SEC,
        help=f"Late threshold in seconds for preview (default: {DEFAULT_LATE_THRESHOLD_SEC})",
    )
    add_duckdb_oom_cli_args(p)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Materialize one ``run_fact`` partition."""
    try:
        import duckdb
    except ImportError:
        print("duckdb is required (see requirements.txt).", file=sys.stderr)
        return 2

    args = _parse_args(argv)
    out_dir = args.output_dir
    if out_dir is None:
        out_dir = l1_run_fact_partition_dir(
            args.data_root.resolve(), args.source_snapshot_id, args.run_end_gaming_day
        )
    out_parquet = out_dir / "run_fact.parquet"
    out_manifest = out_dir / "manifest.json"

    inputs = [p.resolve() for p in args.inputs]

    def _work(con: object):
        stats = materialize_run_fact_v1(
            con=con,
            input_paths=inputs,
            output_parquet=out_parquet,
            run_end_gaming_day=args.run_end_gaming_day,
            run_break_min=args.run_break_min,
            run_definition_version=args.run_definition_version,
            source_namespace=args.source_namespace,
        )
        delay_src = args.ingestion_delay_parquet or args.inputs[0]
        id_summary = compute_ingestion_delay_summary_preview(
            con, delay_src.resolve(), late_threshold_sec=args.late_threshold_sec
        )
        return stats, id_summary

    stats, id_summary = run_duckdb_job_with_oom_retries(
        connect=lambda: duckdb.connect(database=":memory:"),
        work=_work,
        input_paths=inputs,
        job_name="materialize_run_fact_v1",
        run_log_path=args.duckdb_run_log,
        failure_context_path=args.duckdb_oom_failure_context,
        max_attempts=args.duckdb_oom_max_attempts,
        initial_memory_limit_mb=args.duckdb_initial_memory_limit_mb,
    )
    manifest = build_run_fact_manifest(
        source_snapshot_id=args.source_snapshot_id,
        run_end_gaming_day=args.run_end_gaming_day,
        l0_fingerprint_path=args.l0_fingerprint_json,
        l1_preprocess_gaming_day=args.l1_preprocess_gaming_day,
        output_parquet=out_parquet,
        manifest_uri_anchor=_REPO_ROOT,
        stats=stats,
        ingestion_delay_summary=id_summary,
    )
    manifest = merge_source_hashes_into_manifest(manifest, args.l0_fingerprint_json)

    out_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"OK wrote {out_parquet}")
    print(f"OK wrote {out_manifest}")
    print(f"OK row_count={stats['row_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
