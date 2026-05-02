#!/usr/bin/env python3
"""L0 ingest: fingerprint sources, derive ``source_snapshot_id``, materialize Hive-style paths.

Streaming SHA-256 keeps RAM bounded for large Parquet. Default snapshot id is deterministic
from ``snapshot_fingerprint.json`` canonical JSON (see ``doc/l0_ingest_governance_decisions.md``).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from layered_data_assets.l0_fingerprint import (  # noqa: E402
    INGEST_RECIPE_VERSION_DEFAULT,
    build_fingerprint_document,
    derive_source_snapshot_id,
    fingerprint_canonical_json,
    normalized_sorted_sources,
)
from layered_data_assets.l0_paths import l0_partition_dir, validate_source_snapshot_id  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for ``materialize`` (L0 layout + fingerprint file)."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="Root containing l0_layered/ (default: ./data)",
    )
    p.add_argument(
        "--anchor-path",
        type=Path,
        default=_REPO_ROOT,
        help="All --source files must resolve under this directory; fingerprint stores paths relative to it only (default: repo root)",
    )
    p.add_argument("--table", required=True, help="Table directory name, e.g. t_bet")
    p.add_argument("--partition-key", required=True, help="Hive partition key, e.g. gaming_day")
    p.add_argument("--partition-value", required=True, help="Hive partition value, e.g. 2026-04-01")
    p.add_argument(
        "--source",
        dest="sources",
        action="append",
        required=True,
        help="Source file to copy (repeatable). Order does not affect snapshot id.",
    )
    p.add_argument(
        "--snapshot-id",
        default=None,
        help="Override source_snapshot_id; must match derived id unless --allow-snapshot-id-mismatch",
    )
    p.add_argument(
        "--allow-snapshot-id-mismatch",
        action="store_true",
        help="Allow --snapshot-id that differs from fingerprint-derived id (audit use)",
    )
    p.add_argument(
        "--ingest-recipe-version",
        default=INGEST_RECIPE_VERSION_DEFAULT,
        help="Recipe version string embedded in fingerprint",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned snapshot id and paths; do not write files",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="If snapshot root exists with conflicting fingerprint, replace fingerprint (unsafe)",
    )
    return p.parse_args(argv)


def _load_existing_fingerprint(snapshot_root: Path) -> str | None:
    """Return canonical JSON string of existing fingerprint, or None if missing."""
    fp = snapshot_root / "snapshot_fingerprint.json"
    if not fp.is_file():
        return None
    data = json.loads(fp.read_text(encoding="utf-8"))
    return fingerprint_canonical_json(data)


def materialize(argv: list[str] | None = None) -> int:
    """Compute fingerprint, derive id, write ``snapshot_fingerprint.json``, copy sources."""
    args = _parse_args(argv)
    sources = normalized_sorted_sources([Path(s) for s in args.sources])
    doc = build_fingerprint_document(
        source_paths=sources,
        anchor=args.anchor_path,
        table=args.table,
        partition_key=args.partition_key,
        partition_value=args.partition_value,
        ingest_recipe_version=args.ingest_recipe_version,
    )
    canonical = fingerprint_canonical_json(doc)
    derived_id = derive_source_snapshot_id(canonical)
    snapshot_id = args.snapshot_id.strip() if args.snapshot_id else derived_id
    if args.snapshot_id:
        validate_source_snapshot_id(snapshot_id)
        if snapshot_id != derived_id and not args.allow_snapshot_id_mismatch:
            print(
                f"ERROR: --snapshot-id {snapshot_id!r} != derived {derived_id!r}. "
                "Use default (omit --snapshot-id) or pass --allow-snapshot-id-mismatch.",
                file=sys.stderr,
            )
            return 2
    else:
        validate_source_snapshot_id(snapshot_id)

    data_root = args.data_root.resolve()
    snapshot_root = data_root / "l0_layered" / snapshot_id
    part_dir = l0_partition_dir(Path(data_root), snapshot_id, args.table, args.partition_key, args.partition_value)

    if args.dry_run:
        print("dry_run: true")
        print(f"snapshot_id: {snapshot_id}")
        print(f"snapshot_root: {snapshot_root}")
        print(f"partition_dir: {part_dir}")
        print("canonical_fingerprint_json:", canonical[:200] + ("..." if len(canonical) > 200 else ""))
        return 0

    existing = _load_existing_fingerprint(snapshot_root)
    if existing is not None and existing != canonical:
        if not args.force:
            print(
                f"ERROR: snapshot {snapshot_id!r} exists with different fingerprint. "
                "Refuse to overwrite (use new inputs or --force).",
                file=sys.stderr,
            )
            return 3

    snapshot_root.mkdir(parents=True, exist_ok=True)
    part_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_root / "snapshot_fingerprint.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    for i, src in enumerate(sources):
        dest_name = "part-000.parquet" if len(sources) == 1 else f"part-{i:03d}.parquet"
        dest = part_dir / dest_name
        shutil.copy2(src.resolve(), dest)
    print(f"OK snapshot_id={snapshot_id}")
    print(f"OK wrote {snapshot_root / 'snapshot_fingerprint.json'}")
    print(f"OK materialized {len(sources)} file(s) under {part_dir}")
    return 0


def main() -> int:
    """CLI entry for L0 ingest."""
    return materialize()


if __name__ == "__main__":
    raise SystemExit(main())
