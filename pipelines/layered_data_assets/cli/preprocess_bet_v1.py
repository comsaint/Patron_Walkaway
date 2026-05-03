#!/usr/bin/env python3
"""L1 preprocess for ``t_bet``: ``preprocess_bet_v1`` via DuckDB + manifest (LDA-E1-02).

Reads one or more Parquet files (L0 ``part-*.parquet`` or raw ``t_bet`` export with at least
``player_id``, ``bet_id``, ``gaming_day``), applies BET-PK / BET-DQ-01
filters, ``bet_id`` dedup (latest ``__etl_insert_Dtm``), optional dummy / eligible anti-joins,
writes ``cleaned.parquet`` and ``manifest.json`` under ``data/l1_layered/<source_snapshot_id>/t_bet/gaming_day=.../``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .repo_root import discover_repo_root

_REPO_ROOT = discover_repo_root()

from ..core.preprocess_bet_v1 import (
    build_preprocess_manifest,
    run_preprocess_bet_v1,
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
from ..io.l1_paths import l1_bet_partition_dir
from ..io.manifest_lineage_v1 import merge_source_hashes_into_manifest
from ..orchestration.oom_runner_v1 import add_duckdb_oom_cli_args, run_duckdb_job_with_oom_retries


def _add_preprocess_bet_required_args(p: argparse.ArgumentParser) -> None:
    """Register required preprocess CLI arguments."""
    p.add_argument("--data-root", type=Path, default=Path("data"), help="Repo data root (default: ./data)")
    p.add_argument("--source-snapshot-id", required=True, help="L0 batch id, e.g. snap_...")
    p.add_argument("--gaming-day", required=True, help="Partition value YYYY-MM-DD")
    p.add_argument(
        "--input",
        dest="inputs",
        action="append",
        type=Path,
        required=True,
        help="Input Parquet (repeatable); must include player_id, bet_id, gaming_day (L0 t_bet shape)",
    )


def _add_preprocess_bet_optional_args(p: argparse.ArgumentParser) -> None:
    """Register optional fingerprint, sidecar, and output overrides."""
    p.add_argument(
        "--l0-fingerprint-json",
        type=Path,
        default=None,
        help="Optional ``snapshot_fingerprint.json`` to copy source_hashes into manifest",
    )
    p.add_argument(
        "--dummy-player-ids-parquet",
        type=Path,
        default=None,
        help="Optional single-column ``player_id`` dummy set (BET-DQ-02)",
    )
    p.add_argument(
        "--eligible-player-ids-parquet",
        type=Path,
        default=None,
        help="Optional single-column ``player_id`` rated-eligible set (BET-DQ-03)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override output directory (default: data/l1_layered/<snap>/t_bet/gaming_day=...)",
    )
    p.add_argument(
        "--late-threshold-sec",
        type=float,
        default=DEFAULT_LATE_THRESHOLD_SEC,
        help=f"Late-arrival threshold on ingest delay seconds (default: {DEFAULT_LATE_THRESHOLD_SEC})",
    )
    p.add_argument(
        "--ingestion-fix-registry-yaml",
        type=Path,
        default=None,
        help="Optional YAML registry; when set, applies BET-INGEST-FIX-004 synthetic observed-at cap before dedup",
    )
    p.add_argument(
        "--ingestion-fix-registry-version-expected",
        type=str,
        default=None,
        help="Optional fail-fast check: registry top-level registry_version must match this string",
    )
    add_duckdb_oom_cli_args(p)


def _build_preprocess_bet_arg_parser() -> argparse.ArgumentParser:
    """Construct the preprocess_bet_v1 argument parser."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    _add_preprocess_bet_required_args(p)
    _add_preprocess_bet_optional_args(p)
    return p


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI for preprocess_bet_v1."""
    return _build_preprocess_bet_arg_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run preprocess and write ``cleaned.parquet`` + ``manifest.json``."""
    try:
        import duckdb
    except ImportError:
        print("duckdb is required (see requirements.txt).", file=sys.stderr)
        return 2

    args = _parse_args(argv)
    out_dir = args.output_dir
    if out_dir is None:
        out_dir = l1_bet_partition_dir(args.data_root.resolve(), args.source_snapshot_id, args.gaming_day)
    out_parquet = out_dir / "cleaned.parquet"
    out_manifest = out_dir / "manifest.json"
    staged_parquet = staged_parquet_path(out_parquet)
    staged_m = staged_manifest_path(out_manifest)
    remove_staged_outputs(staged_parquet, staged_m)

    inputs = [p.resolve() for p in args.inputs]

    reg_path = args.ingestion_fix_registry_yaml
    if reg_path is not None:
        reg_path = reg_path.resolve()

    def _work(con: object):
        stats = run_preprocess_bet_v1(
            con=con,
            input_paths=inputs,
            output_parquet=staged_parquet,
            gaming_day=args.gaming_day,
            dummy_player_ids_parquet=args.dummy_player_ids_parquet,
            eligible_player_ids_parquet=args.eligible_player_ids_parquet,
            ingestion_fix_registry_path=reg_path,
            ingestion_fix_registry_version_expected=args.ingestion_fix_registry_version_expected,
        )
        observed_col = (
            "__etl_insert_Dtm_synthetic"
            if stats.get("ingest_delay_cap_sec_applied") is not None
            else "__etl_insert_Dtm"
        )
        id_summary = compute_ingestion_delay_summary_preview(
            con,
            staged_parquet,
            late_threshold_sec=args.late_threshold_sec,
            observed_at_col=observed_col,
        )
        return stats, id_summary

    try:
        stats, id_summary = run_duckdb_job_with_oom_retries(
            connect=lambda: duckdb.connect(database=":memory:"),
            work=_work,
            input_paths=inputs,
            job_name="preprocess_bet_v1",
            run_log_path=args.duckdb_run_log,
            failure_context_path=args.duckdb_oom_failure_context,
            max_attempts=args.duckdb_oom_max_attempts,
            initial_memory_limit_mb=args.duckdb_initial_memory_limit_mb,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    manifest = build_preprocess_manifest(
        source_snapshot_id=args.source_snapshot_id,
        gaming_day=args.gaming_day,
        l0_fingerprint_path=args.l0_fingerprint_json,
        output_parquet=out_parquet,
        manifest_uri_anchor=_REPO_ROOT,
        stats=stats,
        ingestion_delay_summary=id_summary,
    )
    manifest = merge_source_hashes_into_manifest(manifest, args.l0_fingerprint_json)

    commit_parquet_and_manifest(
        staged_parquet=staged_parquet,
        final_parquet=out_parquet,
        manifest_text=json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        final_manifest=out_manifest,
    )
    print(f"OK wrote {out_parquet}")
    print(f"OK wrote {out_manifest}")
    print(f"OK row_count={stats['row_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
