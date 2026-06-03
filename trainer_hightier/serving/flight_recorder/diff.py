"""DataFrame diff engine for ClickHouse time-machine captures."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

import numpy as np
import pandas as pd

_DEFAULT_VERSION_COLS: tuple[str, ...] = (
    "__ts_ms",
    "__op",
    "__deleted",
    "__etl_insert_Dtm",
    "lud_dtm",
)


def _row_fingerprint(row: pd.Series, columns: Sequence[str]) -> str:
    """Stable hash for one row over *columns*."""
    parts: list[str] = []
    for col in columns:
        val = row.get(col)
        if pd.isna(val):
            parts.append(f"{col}=<NA>")
        else:
            parts.append(f"{col}={val!r}")
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_dataframes(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    business_key: str = "bet_id",
    version_columns: Sequence[str] | None = None,
    max_changed_samples: int = 20,
) -> dict[str, Any]:
    """Compare *left* (baseline) vs *right* by *business_key*."""
    version_columns = tuple(version_columns or _DEFAULT_VERSION_COLS)
    if business_key not in left.columns and business_key not in right.columns:
        return {
            "business_key": business_key,
            "error": f"business_key {business_key!r} missing in both frames",
        }
    left = left.copy()
    right = right.copy()
    left[business_key] = left[business_key].astype(str)
    right[business_key] = right[business_key].astype(str)
    left_keys = set(left[business_key].dropna().astype(str))
    right_keys = set(right[business_key].dropna().astype(str))
    added = sorted(right_keys - left_keys)
    removed = sorted(left_keys - right_keys)
    common = left_keys & right_keys
    compare_cols = [
        c
        for c in sorted(set(left.columns) | set(right.columns))
        if c != business_key
    ]
    fp_cols = [c for c in version_columns if c in compare_cols] or compare_cols[:8]
    changed_keys: list[str] = []
    changed_samples: list[dict[str, Any]] = []
    for key in sorted(common):
        lrow = left[left[business_key] == key].iloc[-1]
        rrow = right[right[business_key] == key].iloc[-1]
        if _row_fingerprint(lrow, fp_cols) == _row_fingerprint(rrow, fp_cols):
            continue
        changed_keys.append(key)
        if len(changed_samples) < max_changed_samples:
            sample: dict[str, Any] = {"business_key": key}
            for col in fp_cols:
                if col in lrow.index or col in rrow.index:
                    sample[f"left_{col}"] = lrow.get(col)
                    sample[f"right_{col}"] = rrow.get(col)
            changed_samples.append(sample)
    return {
        "business_key": business_key,
        "left_rows": int(len(left)),
        "right_rows": int(len(right)),
        "left_unique_keys": len(left_keys),
        "right_unique_keys": len(right_keys),
        "duplicate_keys_left": int(left[business_key].duplicated().sum()),
        "duplicate_keys_right": int(right[business_key].duplicated().sum()),
        "added_keys_count": len(added),
        "removed_keys_count": len(removed),
        "changed_keys_count": len(changed_keys),
        "added_keys_sample": added[:max_changed_samples],
        "removed_keys_sample": removed[:max_changed_samples],
        "changed_keys_sample": changed_samples,
        "version_columns_used": list(fp_cols),
    }


def write_diff_report(path: Any, payload: dict[str, Any]) -> None:
    """Write diff JSON to *path*."""
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
