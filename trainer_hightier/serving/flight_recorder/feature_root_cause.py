"""Rank feature null reasons from provenance artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def rank_feature_root_causes(recording_root: Path, *, top_n: int = 30) -> dict[str, Any]:
    """Aggregate ``feature_missing_provenance.parquet`` across scorer cycles."""
    frames: list[pd.DataFrame] = []
    scorer_dir = recording_root / "cycles" / "scorer"
    if scorer_dir.is_dir():
        for cycle_dir in sorted(scorer_dir.glob("cycle_*")):
            path = cycle_dir / "audits" / "feature_missing_provenance.parquet"
            if path.is_file():
                frames.append(pd.read_parquet(path))
    if not frames:
        return {"ranked": [], "n_rows": 0}
    prov = pd.concat(frames, ignore_index=True)
    nulls = prov[prov["is_null"] == True]  # noqa: E712
    if nulls.empty:
        return {"ranked": [], "n_rows": int(len(prov))}
    grouped = (
        nulls.groupby(["null_reason", "source_layer", "feature_id"])
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
        .head(top_n)
    )
    ranked = grouped.to_dict(orient="records")
    return {"ranked": ranked, "n_rows": int(len(prov)), "n_null_rows": int(len(nulls))}


def write_feature_root_cause_report(output_dir: Path, recording_root: Path) -> Path:
    """Write ``feature_root_cause_rank.json``."""
    payload = rank_feature_root_causes(recording_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "feature_root_cause_rank.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
