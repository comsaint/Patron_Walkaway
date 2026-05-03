"""CLI: write ``published_snapshot.json`` + ``l1_layered/published/current.json`` (LDA-E2-04)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pipelines.layered_data_assets.io.published_snapshot_v1 import publish_layered_snapshot_v1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish L1 layered snapshot pointer (offline MVP).")
    p.add_argument("--data-root", type=Path, default=Path("data"), help="Data root (default: data)")
    p.add_argument("--published-snapshot-id", required=True, help="e.g. pub_2026-05-03_smoke")
    p.add_argument("--source-snapshot-id", required=True, help="L1 batch id, e.g. snap_abcdef")
    p.add_argument(
        "--previous-published-snapshot-id",
        default=None,
        help="Override chain previous (default: read from current.json if exists)",
    )
    p.add_argument(
        "--no-inherit-previous",
        action="store_true",
        help="Do not read previous from current.json (first chain link)",
    )
    p.add_argument("--notes", default=None, help="Optional note stored in published_snapshot.json")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    snap, cur = publish_layered_snapshot_v1(
        data_root=args.data_root,
        published_snapshot_id=args.published_snapshot_id,
        source_snapshot_id=args.source_snapshot_id,
        previous_published_snapshot_id=args.previous_published_snapshot_id,
        inherit_previous_from_pointer=not args.no_inherit_previous,
        notes=args.notes,
    )
    print(json.dumps({"published_snapshot": str(snap), "current": str(cur)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
