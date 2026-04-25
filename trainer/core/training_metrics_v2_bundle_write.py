"""Phase A dual-write for training_metrics v2 artifact split.

Writes alongside legacy ``training_metrics.json``:

- ``training_metrics.v2.json`` — nested datasets + selection remainder (no long importance list, no gbm_bakeoff blob).
- ``feature_importance.json`` — winner feature importance list + method.
- ``comparison_metrics.json`` — ``families.gbm_bakeoff`` when A3 report exists.

Legacy v1 payload is unchanged by these helpers; see ``doc/training_metrics_v2_artifact_split_implementation_plan.md``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional, Tuple

SCHEMA_TRAINING_METRICS_V2 = "training-metrics.v2"
SCHEMA_FEATURE_IMPORTANCE_V1 = "feature-importance.v1"
SCHEMA_COMPARISON_METRICS_V1 = "comparison-metrics.v1"

_SELECTION_METRIC_FIELD_TEST = "field_test_precision"
_SELECTION_MODE_SOURCE_V2 = "artifact_training_metrics.v2.json"


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str) + "\n"


def _mode_to_precision_type(mode: Any) -> str:
    if mode == "precision_prod_adjusted":
        return "prod_adjusted"
    if mode == "precision_raw":
        return "raw"
    return "raw"


def _val_field_test_block(rated: Mapping[str, Any]) -> Dict[str, Any]:
    mode = rated.get("val_field_test_primary_score_mode")
    ptype = _mode_to_precision_type(mode)
    prec = rated.get("val_field_test_primary_score")
    if prec is None or (isinstance(prec, float) and not math.isfinite(prec)):
        prec = rated.get("val_precision")
    if prec is not None and isinstance(prec, float) and not math.isfinite(prec):
        prec = None
    return {"precision": prec, "precision_type": ptype}


def _test_field_test_block(rated: Mapping[str, Any]) -> Dict[str, Any]:
    adj = rated.get("test_precision_prod_adjusted")
    if adj is not None and isinstance(adj, float) and math.isfinite(adj):
        return {"precision": float(adj), "precision_type": "prod_adjusted"}
    raw = rated.get("test_precision")
    if raw is not None and isinstance(raw, float) and not math.isfinite(raw):
        raw = None
    return {"precision": raw, "precision_type": "raw"}


def _split_prefixed_metrics(
    rated: Mapping[str, Any], prefix: str
) -> Dict[str, Any]:
    plen = len(prefix)
    out: Dict[str, Any] = {}
    for k, v in rated.items():
        if isinstance(k, str) and k.startswith(prefix):
            out[k[plen:]] = v
    return out


def _strip_val_test_noise(d: MutableMapping[str, Any], *, prefix: str) -> None:
    if prefix == "val_":
        for noisy in ("field_test_primary_score", "field_test_primary_score_mode"):
            d.pop(noisy, None)
    # test side uses prod_adjusted keys only for field_test block; keep flat test_precision* for convenience.


def build_datasets_section(rated: Mapping[str, Any]) -> Dict[str, Any]:
    train = _split_prefixed_metrics(rated, "train_")
    val = _split_prefixed_metrics(rated, "val_")
    test = _split_prefixed_metrics(rated, "test_")
    _strip_val_test_noise(val, prefix="val_")
    val["field_test"] = _val_field_test_block(rated)
    test["field_test"] = _test_field_test_block(rated)
    out: Dict[str, Any] = {}
    if train:
        out["train"] = train
    if val:
        out["val"] = val
    if test:
        out["test"] = test
    return out


def _selection_remainder(rated: Mapping[str, Any]) -> Dict[str, Any]:
    skip_prefixes = ("train_", "val_", "test_")
    blocked = {"feature_importance", "gbm_bakeoff"}
    out: Dict[str, Any] = {}
    for k, v in rated.items():
        if not isinstance(k, str):
            continue
        if k in blocked:
            continue
        if any(k.startswith(p) for p in skip_prefixes):
            continue
        out[k] = v
    return out


def build_training_metrics_v2_payload(
    *,
    model_version: str,
    metrics_root: Mapping[str, Any],
) -> Dict[str, Any]:
    rated = metrics_root.get("rated")
    if not isinstance(rated, dict):
        rated = {}

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_TRAINING_METRICS_V2,
        "model_version": model_version,
        "selection_metric": _SELECTION_METRIC_FIELD_TEST,
        "selection_mode": metrics_root.get("selection_mode"),
        "selection_mode_source": _SELECTION_MODE_SOURCE_V2,
        "production_neg_pos_ratio": metrics_root.get("production_neg_pos_ratio"),
        "datasets": build_datasets_section(rated),
        "selection": _selection_remainder(rated),
    }

    for k in (
        "sample_rated_n",
        "neg_sample_frac",
        "threshold_selected_at_recall_floor",
        "spec_hash",
        "uncalibrated_threshold",
        "baseline_data_alignment",
    ):
        if k in metrics_root:
            payload[k] = metrics_root[k]

    return payload


def build_feature_importance_payload(
    *,
    model_version: str,
    rated: Mapping[str, Any],
) -> Dict[str, Any]:
    items = rated.get("feature_importance")
    if not isinstance(items, list):
        items = []
    method = rated.get("importance_method") or "gain"
    return {
        "schema_version": SCHEMA_FEATURE_IMPORTANCE_V1,
        "model_version": model_version,
        "importance_method": method,
        "items": items,
    }


def _metrics_row_to_datasets(row: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(row, Mapping):
        return {}
    return build_datasets_section(row)


def _gbm_bakeoff_family(report: Mapping[str, Any]) -> Dict[str, Any]:
    per = report.get("per_backend") or {}
    candidates: Dict[str, Any] = {}
    if isinstance(per, dict):
        for cid, row in per.items():
            if isinstance(row, dict):
                candidate: Dict[str, Any] = {
                    "candidate_id": str(cid),
                    "datasets": _metrics_row_to_datasets(row),
                }
                if "error" in row:
                    candidate["error"] = row.get("error")
                if "bakeoff_disposition" in row:
                    candidate["bakeoff_disposition"] = row.get("bakeoff_disposition")
                candidates[str(cid)] = candidate
            else:
                candidates[str(cid)] = {"candidate_id": str(cid), "error": repr(row)}
    winner = report.get("winner_id")
    if winner is None:
        winner = report.get("winner_backend")
    return {
        "comparison_family": "gbm_bakeoff",
        "selection_rule": report.get("selection_rule"),
        "selection_metric": _SELECTION_METRIC_FIELD_TEST,
        "winner_id": winner,
        "schema_version": report.get("schema_version"),
        "candidates": candidates,
        "ensemble_bridge": report.get("ensemble_bridge"),
    }


def build_comparison_metrics_payload(
    *,
    model_version: str,
    rated: Mapping[str, Any],
) -> Dict[str, Any]:
    families: Dict[str, Any] = {}
    gb = rated.get("gbm_bakeoff")
    if isinstance(gb, dict) and (
        gb.get("per_backend") is not None or gb.get("winner_backend") is not None
    ):
        families["gbm_bakeoff"] = _gbm_bakeoff_family(gb)
    return {
        "schema_version": SCHEMA_COMPARISON_METRICS_V1,
        "model_version": model_version,
        "families": families,
    }


def write_training_metrics_v2_sidecars(
    bundle_dir: Path,
    *,
    model_version: str,
    metrics_root: Mapping[str, Any],
    model_metadata: Optional[MutableMapping[str, Any]] = None,
) -> Tuple[Path, Path, Path]:
    """Write v2 metrics, feature importance, and comparison JSON; update optional metadata pointers.

    Returns written paths (resolved).
    """
    root = Path(bundle_dir).resolve()
    rated = metrics_root.get("rated")
    if not isinstance(rated, dict):
        rated = {}

    v2_path = root / "training_metrics.v2.json"
    fi_path = root / "feature_importance.json"
    cm_path = root / "comparison_metrics.json"

    v2_path.write_text(
        _json_dump(
            build_training_metrics_v2_payload(
                model_version=model_version,
                metrics_root=metrics_root,
            )
        ),
        encoding="utf-8",
    )
    fi_path.write_text(
        _json_dump(
            build_feature_importance_payload(
                model_version=model_version,
                rated=rated,
            )
        ),
        encoding="utf-8",
    )
    cm_path.write_text(
        _json_dump(
            build_comparison_metrics_payload(
                model_version=model_version,
                rated=rated,
            )
        ),
        encoding="utf-8",
    )

    if model_metadata is not None:
        arts = model_metadata.setdefault("artifacts", {})
        arts["training_metrics_v2_path"] = str(v2_path)
        arts["feature_importance_path"] = str(fi_path)
        arts["comparison_metrics_path"] = str(cm_path)

    return v2_path, fi_path, cm_path
