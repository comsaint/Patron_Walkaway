#!/usr/bin/env python3
"""Refresh ``manifest.json`` lineage fields (LDA-E1-06): ``source_hashes`` + ``ingestion_delay_summary``.

Reads an existing manifest, optionally merges ``source_hashes`` from ``snapshot_fingerprint.json``,
optionally recomputes ``ingestion_delay_summary`` from a bet Parquet, writes JSON back.
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
from layered_data_assets.manifest_lineage_v1 import (  # noqa: E402
    merge_ingestion_delay_summary,
    merge_source_hashes_into_manifest,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI for manifest lineage preview."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest", type=Path, required=True, help="Existing manifest.json to update")
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write path (default: overwrite --manifest)",
    )
    p.add_argument(
        "--l0-fingerprint-json",
        type=Path,
        default=None,
        help="snapshot_fingerprint.json to refresh source_hashes",
    )
    p.add_argument(
        "--ingestion-delay-parquet",
        type=Path,
        default=None,
        help="Bet Parquet (cleaned) for ingestion_delay_summary preview",
    )
    p.add_argument(
        "--late-threshold-sec",
        type=float,
        default=DEFAULT_LATE_THRESHOLD_SEC,
        help=f"Late threshold seconds (default: {DEFAULT_LATE_THRESHOLD_SEC})",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Update manifest lineage fields."""
    args = _parse_args(argv)
    path = args.manifest.resolve()
    out = args.output.resolve() if args.output else path
    manifest = json.loads(path.read_text(encoding="utf-8"))

    if args.l0_fingerprint_json is not None:
        manifest = merge_source_hashes_into_manifest(manifest, args.l0_fingerprint_json.resolve())

    if args.ingestion_delay_parquet is not None:
        try:
            import duckdb
        except ImportError:
            print("duckdb is required when --ingestion-delay-parquet is set.", file=sys.stderr)
            return 2
        con = duckdb.connect(database=":memory:")
        try:
            summary = compute_ingestion_delay_summary_preview(
                con,
                args.ingestion_delay_parquet.resolve(),
                late_threshold_sec=args.late_threshold_sec,
            )
        finally:
            con.close()
        manifest = merge_ingestion_delay_summary(manifest, summary)

    out.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"OK wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
