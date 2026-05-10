"""Step 6 partition observability + carry-over persistence hooks.

Stores lightweight JSON under ``trainer/.data/step6_partition/``. Safe to call
from :func:`trainer.training.trainer.process_chunk` after a successful chunk
write. Full overlap reuse of prefeatures Parquets is future work; this module
provides index/carry-over filenames and stable keys aligned with the unified
reuse plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

STEP6_PARTITION_INDEX_FILE = "partition_index.json"


def step6_partition_data_dir() -> Path:
    """Directory for Step 6 partition sidecars (next to ``trainer`` package)."""
    return Path(__file__).resolve().parents[1] / ".data" / "step6_partition"


def read_partition_index() -> Dict[str, Any]:
    """Return the partition index dict (possibly empty)."""
    p = step6_partition_data_dir() / STEP6_PARTITION_INDEX_FILE
    if not p.is_file():
        return {"kind": "step6_partition_index_v1", "partitions": []}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"kind": "step6_partition_index_v1", "partitions": [], "error": "invalid_json"}
    return raw if isinstance(raw, dict) else {"kind": "step6_partition_index_v1", "partitions": []}


def record_prefeatures_partition_meta(
    *,
    gaming_day_min: str,
    gaming_day_max: str,
    chunk_fingerprint: str,
    source_snapshot_id: str,
) -> None:
    """Append partition coverage metadata for overlap planning (bounded list)."""
    d = step6_partition_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    idx = read_partition_index()
    parts: List[Dict[str, Any]] = list(idx.get("partitions") or [])
    parts.append(
        {
            "gaming_day_min": str(gaming_day_min),
            "gaming_day_max": str(gaming_day_max),
            "chunk_fingerprint": str(chunk_fingerprint),
            "source_snapshot_id": str(source_snapshot_id),
        },
    )
    idx["partitions"] = parts[-5000:]
    (d / STEP6_PARTITION_INDEX_FILE).write_text(
        json.dumps(idx, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def carry_over_state_path(gaming_day: str) -> Path:
    """JSON path for run_state_machine carry-over blob keyed by gaming day."""
    safe = str(gaming_day).replace("/", "_")[:64]
    return step6_partition_data_dir() / "carry_over" / f"{safe}.json"


def save_carry_over_state_blob(gaming_day: str, blob: Dict[str, Any]) -> None:
    """Persist carry-over state for the next partition (best-effort)."""
    p = carry_over_state_path(gaming_day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(blob, sort_keys=True) + "\n", encoding="utf-8")


def load_carry_over_state_blob(gaming_day: str) -> Optional[Dict[str, Any]]:
    """Load carry-over blob when present."""
    p = carry_over_state_path(gaming_day)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None
