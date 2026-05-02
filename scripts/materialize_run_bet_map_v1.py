#!/usr/bin/env python3
"""L1 ``run_bet_map`` membership (LDA-E1-04): ``run_id`` ↔ bet rows.

Uses the same gap / ``run_id`` rules as ``materialize_run_fact_v1.py``. Writes
``run_bet_map.parquet`` + ``manifest.json`` under
``data/l1_layered/<source_snapshot_id>/run_bet_map/run_end_gaming_day=.../``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from layered_data_assets.l1_paths import l1_run_bet_map_partition_dir  # noqa: E402
from layered_data_assets.run_bet_map_v1 import build_run_bet_map_manifest, materialize_run_bet_map_v1  # noqa: E402
from layered_data_assets.run_fact_v1 import (  # noqa: E402
    RUN_BREAK_MIN_DEFAULT,
    RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
    SOURCE_NAMESPACE_DEFAULT,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI for ``materialize_run_bet_map_v1``."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-root", type=Path, default=Path("data"), help="Repo data root (default: ./data)")
    p.add_argument("--source-snapshot-id", required=True, help="L0 batch id, e.g. snap_...")
    p.add_argument(
        "--run-end-gaming-day",
        required=True,
        help="Partition YYYY-MM-DD (same as run_fact): runs whose last bet gaming_day equals this",
    )
    p.add_argument(
        "--l1-preprocess-gaming-day",
        required=True,
        help="Upstream cleaned-bet partition for manifest (YYYY-MM-DD)",
    )
    p.add_argument(
        "--input",
        dest="inputs",
        action="append",
        type=Path,
        required=True,
        help="Cleaned bet Parquet (repeatable); same inputs as run_fact job",
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
        help=f"Minutes gap for run boundary (default: {RUN_BREAK_MIN_DEFAULT})",
    )
    p.add_argument(
        "--run-definition-version",
        default=RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
        help="Must match run_fact job for consistent run_id",
    )
    p.add_argument(
        "--source-namespace",
        default=SOURCE_NAMESPACE_DEFAULT,
        help="Must match run_fact job for consistent run_id",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override output directory (default: data/l1_layered/<snap>/run_bet_map/run_end_gaming_day=...)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Materialize one ``run_bet_map`` partition."""
    try:
        import duckdb
    except ImportError:
        print("duckdb is required (see requirements.txt).", file=sys.stderr)
        return 2

    args = _parse_args(argv)
    out_dir = args.output_dir
    if out_dir is None:
        out_dir = l1_run_bet_map_partition_dir(
            args.data_root.resolve(), args.source_snapshot_id, args.run_end_gaming_day
        )
    out_parquet = out_dir / "run_bet_map.parquet"
    out_manifest = out_dir / "manifest.json"

    con = duckdb.connect(database=":memory:")
    try:
        stats = materialize_run_bet_map_v1(
            con=con,
            input_paths=[p.resolve() for p in args.inputs],
            output_parquet=out_parquet,
            run_end_gaming_day=args.run_end_gaming_day,
            run_break_min=args.run_break_min,
            run_definition_version=args.run_definition_version,
            source_namespace=args.source_namespace,
        )
        manifest = build_run_bet_map_manifest(
            source_snapshot_id=args.source_snapshot_id,
            run_end_gaming_day=args.run_end_gaming_day,
            l0_fingerprint_path=args.l0_fingerprint_json,
            l1_preprocess_gaming_day=args.l1_preprocess_gaming_day,
            output_parquet=out_parquet,
            manifest_uri_anchor=_REPO_ROOT,
            stats=stats,
        )
    finally:
        con.close()

    out_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"OK wrote {out_parquet}")
    print(f"OK wrote {out_manifest}")
    print(f"OK row_count={stats['row_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
