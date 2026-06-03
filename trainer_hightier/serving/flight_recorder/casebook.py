"""Casebook extracts from recording bundles for RCA."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def build_high_score_casebook(recording_root: Path, *, top_n: int = 500) -> pd.DataFrame:
    """Collect high-score rows from scorer ``stage_09_scores`` across cycles."""
    rows: list[pd.DataFrame] = []
    scorer_dir = recording_root / "cycles" / "scorer"
    if not scorer_dir.is_dir():
        return pd.DataFrame()
    for cycle_dir in sorted(scorer_dir.glob("cycle_*")):
        path = cycle_dir / "stages" / "stage_09_scores.parquet"
        if not path.is_file():
            continue
        frame = pd.read_parquet(path)
        if frame.empty or "score" not in frame.columns:
            continue
        work = frame.copy()
        work["cycle"] = cycle_dir.name
        rows.append(work)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    out["score"] = pd.to_numeric(out["score"], errors="coerce")
    return out.sort_values("score", ascending=False).head(top_n)


def build_false_positive_casebook(recording_root: Path, *, top_n: int = 500) -> pd.DataFrame:
    """Collect validator MISS rows (false positives when alert fired)."""
    rows: list[pd.DataFrame] = []
    val_dir = recording_root / "cycles" / "validator"
    if not val_dir.is_dir():
        return pd.DataFrame()
    for cycle_dir in sorted(val_dir.glob("cycle_*")):
        path = cycle_dir / "decisions" / "decision_trace.parquet"
        if not path.is_file():
            continue
        trace = pd.read_parquet(path)
        if trace.empty:
            continue
        fp = trace[trace["result"] == False].copy()  # noqa: E712
        if fp.empty:
            continue
        fp["cycle"] = cycle_dir.name
        rows.append(fp)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True).head(top_n)


def write_casebooks(output_dir: Path, recording_root: Path) -> dict[str, Any]:
    """Write casebook Parquet files under *output_dir*."""
    output_dir.mkdir(parents=True, exist_ok=True)
    high = build_high_score_casebook(recording_root)
    fp = build_false_positive_casebook(recording_root)
    paths: dict[str, Any] = {}
    if not high.empty:
        p = output_dir / "high_score_casebook.parquet"
        high.to_parquet(p, index=False)
        paths["high_score_casebook"] = str(p)
    if not fp.empty:
        p = output_dir / "false_positive_casebook.parquet"
        fp.to_parquet(p, index=False)
        paths["false_positive_casebook"] = str(p)
    return {"paths": paths, "n_high_score": len(high), "n_false_positive": len(fp)}
