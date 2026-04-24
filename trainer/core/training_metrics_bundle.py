"""Load training metrics from a model bundle with v2-first merge semantics.

Phase B contract: prefer ``training_metrics.v2.json`` when present, fall back to
legacy ``training_metrics.json``, and expose a **flat** dict compatible with
scripts that historically read top-level ``test_*`` / ``optuna_hpo_*`` keys.

``read_bundle_run_contract_block`` uses the same resolution order for
``selection_mode`` only; full merges are for reporting scripts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Tuple

V2_SCHEMA = "training-metrics.v2"


def _load_json_object(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _flatten_v1_training_metrics(tm: dict[str, Any]) -> dict[str, Any]:
    """Promote ``rated`` scalars to top-level (setdefault) for legacy nested bundles."""
    out = dict(tm)
    rated = out.pop("rated", None)
    if isinstance(rated, dict):
        for k, v in rated.items():
            out.setdefault(k, v)
    return out


def _flatten_v2_training_metrics(tm: dict[str, Any]) -> dict[str, Any]:
    """Expand v2 ``datasets`` + ``selection`` into legacy-style flat keys."""
    out: dict[str, Any] = {}
    for k, v in tm.items():
        if k in ("datasets", "selection"):
            continue
        out[k] = v

    sel = tm.get("selection")
    if isinstance(sel, dict):
        for k, v in sel.items():
            out.setdefault(k, v)

    ds = tm.get("datasets")
    if not isinstance(ds, dict):
        return out

    for split, prefix in (("train", "train_"), ("val", "val_"), ("test", "test_")):
        blob = ds.get(split)
        if not isinstance(blob, dict):
            continue
        for k, v in blob.items():
            if k == "field_test":
                continue
            key = k if isinstance(k, str) and k.startswith(prefix) else f"{prefix}{k}"
            out[key] = v
    return out


def load_training_metrics_merged(bundle_root: Path) -> Tuple[Optional[str], dict[str, Any]]:
    """Return ``(source_label, flat_metrics)`` for scripts / parity tooling.

    *source_label* is a short provenance hint:
    - ``training_metrics.v2.json`` when v2 was loaded (possibly merged with v1),
    - ``training_metrics.json`` when only v1 exists,
    - ``None`` when neither file exists or both are unreadable.
    """
    root = Path(bundle_root).resolve()
    p_v2 = root / "training_metrics.v2.json"
    p_v1 = root / "training_metrics.json"
    v2_raw = _load_json_object(p_v2)
    v1_raw = _load_json_object(p_v1)

    if not v1_raw and not v2_raw:
        return None, {}

    v1_flat = _flatten_v1_training_metrics(v1_raw) if v1_raw else {}
    if v2_raw and str(v2_raw.get("schema_version") or "") == V2_SCHEMA:
        v2_flat = _flatten_v2_training_metrics(v2_raw)
        merged = {**v1_flat, **v2_flat}
        return str(p_v2.name), merged

    if v2_raw:
        # Non-contract file present but unknown schema: treat like supplemental dict
        merged = {**v1_flat, **v2_raw}
        return str(p_v2.name), merged

    return str(p_v1.name), v1_flat


def load_training_metrics_for_contract(bundle_root: Path) -> Tuple[Optional[dict[str, Any]], str]:
    """Return ``(artifact_dict_or_none, path_label)`` for run-contract reads.

    Prefer v2 when it carries a non-empty ``selection_mode``; else v1 with a
    non-empty mode; else whichever file exists (for forward compatibility).
    *path_label* matches ``selection_mode_source`` strings used by scorer/backtester.
    """
    root = Path(bundle_root).resolve()
    p_v2 = root / "training_metrics.v2.json"
    p_v1 = root / "training_metrics.json"
    v2 = _load_json_object(p_v2)
    v1 = _load_json_object(p_v1)

    def _nonempty_mode(blob: dict[str, Any]) -> bool:
        m = blob.get("selection_mode")
        return m is not None and str(m).strip() != ""

    if v2 is not None and _nonempty_mode(v2):
        return v2, "artifact_training_metrics.v2.json"
    if v1 is not None and _nonempty_mode(v1):
        return v1, "artifact_training_metrics.json"
    if v2 is not None:
        return v2, "artifact_training_metrics.v2.json"
    if v1 is not None:
        return v1, "artifact_training_metrics.json"
    return None, "config"
