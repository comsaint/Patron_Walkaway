#!/usr/bin/env python3
"""Enumerate deploy feature_spec candidates per implementation plan §6.1.1."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    print("PyYAML is required.", file=sys.stderr)
    raise SystemExit(2) from exc

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SPEC = _REPO_ROOT / "package" / "deploy" / "models" / "feature_spec.yaml"
_DEFAULT_JSON = (
    _REPO_ROOT
    / "artifacts"
    / "layered_data_assets"
    / "contracts"
    / "features_enumerated.json"
)
_DEFAULT_CSV = (
    _REPO_ROOT
    / "artifacts"
    / "layered_data_assets"
    / "contracts"
    / "feature_dependency_registry.csv"
)

_SQL_TOKENS = frozenset(
    """
    COUNT SUM AVG MIN MAX NULLIF CAST ROW PARTITION ORDER BY ASC DESC LAG OVER RANGE
    BETWEEN INTERVAL MINUTE PRECEDING CURRENT AND OR CASE WHEN THEN ELSE END EPOCH
    cos sin pi date_part hour minute double varchar bigint int
    """.split()
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--spec", type=Path, default=_DEFAULT_SPEC)
    p.add_argument("--out-json", type=Path, default=_DEFAULT_JSON)
    p.add_argument("--out-csv", type=Path, default=_DEFAULT_CSV)
    return p.parse_args()


def _allowed_bet_columns(spec: Mapping[str, Any]) -> set[str]:
    tl = spec.get("track_llm")
    if not isinstance(tl, Mapping):
        return set()
    gr = tl.get("guardrails")
    if not isinstance(gr, Mapping):
        return set()
    raw = gr.get("track_llm_allowed_columns")
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw if isinstance(x, str)}


def enumerate_features(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, val in spec.items():
        if not isinstance(key, str) or not key.startswith("track_"):
            continue
        if not isinstance(val, Mapping):
            continue
        cands = val.get("candidates")
        if not isinstance(cands, list):
            continue
        for item in cands:
            if isinstance(item, Mapping) and "feature_id" in item:
                fid = item["feature_id"]
                if not isinstance(fid, str) or not fid.strip():
                    raise ValueError(f"{key}: empty feature_id in candidates")
                row = {"track_section": key, "feature_id": fid.strip()}
                row["spec"] = dict(item)
                rows.append(row)
    rows.sort(key=lambda r: (r["track_section"], r["feature_id"]))
    return rows


def _required_l1_placeholder(item: Mapping[str, Any], allowed: set[str]) -> str:
    ic = item.get("input_columns")
    if isinstance(ic, list) and ic:
        return ";".join(sorted({str(x) for x in ic if isinstance(x, str) and x}))
    sc = item.get("source_column")
    if isinstance(sc, str) and sc.strip():
        return f"player_profile.{sc.strip()}"
    expr = item.get("expression")
    if isinstance(expr, str) and expr.strip() and allowed:
        tokens = set(re.findall(r"\b[A-Za-z_][\w]*\b", expr))
        hits = sorted(t for t in tokens if t in allowed and t.upper() not in _SQL_TOKENS)
        if hits:
            return ";".join(hits)
    return "TBD"


def _allow_bet_rescan(item: Mapping[str, Any]) -> str:
    st = item.get("type")
    if st == "profile_column":
        return "no"
    return "TBD"


def _computation_source(item: Mapping[str, Any]) -> str:
    st = item.get("type", "")
    if st == "python_vectorized":
        fn = item.get("function_name")
        if isinstance(fn, str) and fn:
            return f"python_vectorized:{fn}"
        return "python_vectorized"
    if st == "profile_column":
        sc = item.get("source_column", "")
        return f"profile_column:{sc}"
    expr = item.get("expression", "")
    if isinstance(expr, str) and expr:
        one = expr.replace("\n", " ").strip()
        return (st + ":" + one)[:200]
    return str(st) if st else "TBD"


def build_enumerated_payload(
    spec_path: Path,
    rows: list[dict[str, Any]],
    *,
    repo_root: Path,
) -> dict[str, Any]:
    """Embed ``source_spec_path`` relative to ``repo_root`` when possible (stable across machines)."""
    spec_resolved = spec_path.resolve()
    root_resolved = repo_root.resolve()
    try:
        source_spec_path = spec_resolved.relative_to(root_resolved).as_posix()
    except ValueError:
        source_spec_path = spec_resolved.as_posix()
    return {
        "enumerator_version": "1",
        "source_spec_path": source_spec_path,
        "feature_count": len(rows),
        "features": [
            {"track_section": r["track_section"], "feature_id": r["feature_id"]} for r in rows
        ],
    }


def write_registry_csv(path: Path, rows: list[dict[str, Any]], allowed: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "track_section",
                "feature_id",
                "spec_type",
                "required_l1_fields",
                "allow_bet_rescan",
                "computation_source",
            ],
        )
        w.writeheader()
        for r in rows:
            item = r["spec"]
            w.writerow(
                {
                    "track_section": r["track_section"],
                    "feature_id": r["feature_id"],
                    "spec_type": str(item.get("type", "")),
                    "required_l1_fields": _required_l1_placeholder(item, allowed),
                    "allow_bet_rescan": _allow_bet_rescan(item),
                    "computation_source": _computation_source(item),
                }
            )


def main() -> int:
    args = _parse_args()
    spec_path = args.spec.resolve()
    raw = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        print("feature_spec root must be a mapping.", file=sys.stderr)
        return 1
    allowed = _allowed_bet_columns(raw)
    rows = enumerate_features(raw)
    payload = build_enumerated_payload(spec_path, rows, repo_root=_REPO_ROOT)
    out_json = args.out_json.resolve()
    out_csv = args.out_csv.resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_registry_csv(out_csv, rows, allowed)
    print(f"Wrote {len(rows)} features to {out_json} and {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
