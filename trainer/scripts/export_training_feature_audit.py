"""Export training chunk Parquet into the same ``feature_audit_*`` SQLite schema as serving.

Run from repo root::

    python -m trainer.scripts.export_training_feature_audit \\
        --parquet out/chunks/chunk_xxx.parquet \\
        --feature-list-json out/models/feature_list.json \\
        --out-db investigations/feature_audit_training.sqlite \\
        [--feature-spec-yaml out/models/feature_spec.yaml] \\
        [--model-version v123] [--threshold 0.35] [--max-rows 50000]

The output DB can be compared with production ``prediction_log.db`` using
``python -m trainer.scripts.compare_feature_audit_summaries``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple, Any

import pandas as pd

from trainer.serving.feature_audit import write_training_feature_audit_run


def _parse_feature_list_json(path: Path) -> Tuple[List[str], Optional[List[Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not raw:
        return [], None
    if isinstance(raw[0], dict) and "name" in raw[0]:
        names = [str(e["name"]) for e in raw if isinstance(e, dict) and "name" in e]
        return names, raw  # type: ignore[return-value]
    return [str(x) for x in raw], None


def main() -> int:
    p = argparse.ArgumentParser(description="Write training feature_audit tables for parity checks.")
    p.add_argument("--parquet", type=Path, required=True)
    p.add_argument("--feature-list-json", type=Path, required=True)
    p.add_argument("--out-db", type=Path, required=True)
    p.add_argument("--feature-spec-yaml", type=Path, default=None)
    p.add_argument("--model-version", type=str, default="training-export")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--max-rows", type=int, default=0, help="If >0, only load first N rows (memory cap).")
    p.add_argument("--retention-hours", type=float, default=8760.0)
    args = p.parse_args()

    if not args.parquet.is_file():
        print(f"Parquet not found: {args.parquet}", file=sys.stderr)
        return 1
    if not args.feature_list_json.is_file():
        print(f"feature_list.json not found: {args.feature_list_json}", file=sys.stderr)
        return 1

    max_rows = int(args.max_rows)
    df = pd.read_parquet(args.parquet) if max_rows <= 0 else pd.read_parquet(args.parquet).head(max_rows)
    if df.empty:
        print("Parquet is empty.", file=sys.stderr)
        return 1

    if "is_rated" not in df.columns:
        df = df.copy()
        df["is_rated"] = True

    names, meta = _parse_feature_list_json(args.feature_list_json)
    if not names:
        print("feature_list.json produced no feature names.", file=sys.stderr)
        return 1

    spec = None
    if args.feature_spec_yaml is not None:
        if not args.feature_spec_yaml.is_file():
            print(f"feature_spec.yaml not found: {args.feature_spec_yaml}", file=sys.stderr)
            return 1
        try:
            from trainer.features import load_feature_spec
        except ImportError as exc:
            print(f"Cannot import load_feature_spec: {exc}", file=sys.stderr)
            return 1
        spec = load_feature_spec(args.feature_spec_yaml)

    args.out_db.parent.mkdir(parents=True, exist_ok=True)
    write_training_feature_audit_run(
        out_db_path=str(args.out_db.resolve()),
        df=df,
        model_features=names,
        feature_list=names,
        feature_list_meta=meta,
        feature_spec=spec,
        model_version=str(args.model_version),
        bundle_threshold=float(args.threshold),
        effective_threshold=float(args.threshold),
        retention_hours=float(args.retention_hours),
    )
    print(f"Wrote training feature audit to {args.out_db.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
