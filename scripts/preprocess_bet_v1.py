#!/usr/bin/env python3
"""L1 preprocess for ``t_bet``: ``preprocess_bet_v1`` via DuckDB + manifest (LDA-E1-02).

Reads one or more Parquet files (L0 ``part-*.parquet`` or raw export), applies BET-PK / BET-DQ-01
filters, ``bet_id`` dedup (latest ``__etl_insert_Dtm``), optional dummy / eligible anti-joins,
writes ``cleaned.parquet`` and ``manifest.json`` under ``data/l1_layered/<source_snapshot_id>/t_bet/gaming_day=.../``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from layered_data_assets.l1_paths import l1_bet_partition_dir  # noqa: E402
from layered_data_assets.preprocess_bet_v1 import (  # noqa: E402
    build_preprocess_manifest,
    run_preprocess_bet_v1,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI for preprocess_bet_v1."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-root", type=Path, default=Path("data"), help="Repo data root (default: ./data)")
    p.add_argument("--source-snapshot-id", required=True, help="L0 batch id, e.g. snap_...")
    p.add_argument("--gaming-day", required=True, help="Partition value YYYY-MM-DD")
    p.add_argument(
        "--input",
        dest="inputs",
        action="append",
        type=Path,
        required=True,
        help="Input Parquet (repeatable); must include columns for filters used",
    )
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
    return p.parse_args(argv)


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

    con = duckdb.connect(database=":memory:")
    try:
        stats = run_preprocess_bet_v1(
            con=con,
            input_paths=[p.resolve() for p in args.inputs],
            output_parquet=out_parquet,
            gaming_day=args.gaming_day,
            dummy_player_ids_parquet=args.dummy_player_ids_parquet,
            eligible_player_ids_parquet=args.eligible_player_ids_parquet,
        )
        manifest = build_preprocess_manifest(
            source_snapshot_id=args.source_snapshot_id,
            gaming_day=args.gaming_day,
            l0_fingerprint_path=args.l0_fingerprint_json,
            output_parquet=out_parquet,
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
