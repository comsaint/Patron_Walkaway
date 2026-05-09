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
SCHEMA_TRAINING_METRICS_V3 = "training-metrics.v3"
SCHEMA_FEATURE_IMPORTANCE_V1 = "feature-importance.v1"
SCHEMA_COMPARISON_METRICS_V1 = "comparison-metrics.v1"

_SELECTION_METRIC_FIELD_TEST = "field_test_precision"
_SELECTION_MODE_SOURCE_V2 = "artifact_training_metrics.v2.json"
_SELECTION_MODE_SOURCE_V3 = "artifact_training_metrics.v3.json"

_SELECTION_SLIM_KEYS = frozenset({"label", "threshold", "best_hyperparams", "_uncalibrated"})


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
    blocked = {"feature_importance", "gbm_bakeoff", "stage1_datasets"}
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

    _datasets = build_datasets_section(rated)
    _stage1_ds = rated.get("stage1_datasets")
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_TRAINING_METRICS_V2,
        "model_version": model_version,
        "selection_metric": _SELECTION_METRIC_FIELD_TEST,
        "selection_mode": metrics_root.get("selection_mode"),
        "selection_mode_source": _SELECTION_MODE_SOURCE_V2,
        "production_neg_pos_ratio": metrics_root.get("production_neg_pos_ratio"),
        "datasets": _datasets,
        "selection": _selection_remainder(rated),
    }
    if isinstance(_stage1_ds, dict) and _stage1_ds:
        payload["stage1_datasets"] = _stage1_ds

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


def _finite_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int_nonneg(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        i = int(x)
    except (TypeError, ValueError):
        return None
    return i if i >= 0 else None


def _neg_pos_ratio_one_split(row: Mapping[str, Any], split: str) -> Dict[str, Any]:
    """One split row: ``neg_pos_ratio`` = ``n_neg / n_pos`` (trainer contract)."""
    ratio_key = f"{split}_neg_pos_ratio"
    samples_k = f"{split}_samples"
    pos_k = f"{split}_positives"
    direct = _finite_float(row.get(ratio_key))
    n_s = _int_nonneg(row.get(samples_k))
    n_p = _int_nonneg(row.get(pos_k))
    n_neg: Optional[int] = None
    if n_s is not None and n_p is not None:
        cand = n_s - n_p
        n_neg = cand if cand >= 0 else None

    out_ratio: Optional[float] = None
    source = "unavailable"
    if direct is not None:
        out_ratio = direct
        source = f"rated.{ratio_key}"
    elif n_neg is not None and n_p is not None and n_p > 0:
        r = float(n_neg) / float(n_p)
        if math.isfinite(r) and r > 0.0:
            out_ratio = r
            source = f"derived_from_{samples_k}_and_{pos_k}"

    return {
        "neg_pos_ratio": out_ratio,
        "n_samples": n_s,
        "n_pos": n_p,
        "n_neg": n_neg,
        "source": source,
    }


def _neg_pos_ratio_three_splits(row: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        "train": _neg_pos_ratio_one_split(row, "train"),
        "val": _neg_pos_ratio_one_split(row, "val"),
        "test": _neg_pos_ratio_one_split(row, "test"),
    }


def build_neg_pos_ratio_overview(
    metrics_root: Mapping[str, Any], rated: Mapping[str, Any]
) -> Dict[str, Any]:
    """V3 one-page table: primary rated model + Issue #8 segment rows (same three splits each)."""
    segments: list[Dict[str, Any]] = []
    for seg_label, root_key in (("high", "segment_high"), ("low", "segment_low")):
        blob = metrics_root.get(root_key)
        if isinstance(blob, dict) and blob:
            segments.append(
                {
                    "segment": seg_label,
                    "splits": _neg_pos_ratio_three_splits(blob),
                }
            )
    if not segments:
        hr = metrics_root.get("high_roller_segmentation")
        if isinstance(hr, dict):
            for seg_label, mk in (
                ("high", "high_segment_metrics"),
                ("low", "low_segment_metrics"),
            ):
                blob = hr.get(mk)
                if isinstance(blob, dict) and blob:
                    segments.append(
                        {
                            "segment": seg_label,
                            "splits": _neg_pos_ratio_three_splits(blob),
                        }
                    )
    return {
        "neg_pos_ratio_contract": "n_neg / n_pos",
        "primary_model": _neg_pos_ratio_three_splits(rated),
        "segments": segments,
    }


def _derive_selection_metric_id(rated: Mapping[str, Any]) -> str:
    """Identify the primary objective / HPO mode string used for this run (best-effort)."""
    raw = rated.get("optuna_hpo_objective_mode")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if rated.get("optuna_hpo_gate_blocked") is True:
        return "gate_blocked"
    eff = rated.get("optuna_hpo_effective_enabled")
    if eff is False:
        return "hpo_disabled_or_skipped"
    return "unknown"


def _val_field_test_block_v3(rated: Mapping[str, Any]) -> Dict[str, Any]:
    raw_p = _finite_float(rated.get("val_precision"))
    primary = _finite_float(rated.get("val_field_test_primary_score"))
    mode = rated.get("val_field_test_primary_score_mode")
    prod_adj: Optional[float] = None
    if mode == "precision_prod_adjusted" and primary is not None:
        prod_adj = primary
    used = primary if primary is not None else raw_p
    return {
        "precision_raw": raw_p,
        "precision_used_for_selection": used,
        "precision_prod_adjusted": prod_adj,
        "precision_type": _mode_to_precision_type(mode),
    }


def _test_field_test_block_v3(rated: Mapping[str, Any]) -> Dict[str, Any]:
    raw_p = _finite_float(rated.get("test_precision"))
    adj = _finite_float(rated.get("test_precision_prod_adjusted"))
    used = adj if adj is not None else raw_p
    ptype = "prod_adjusted" if adj is not None else "raw"
    return {
        "precision_raw": raw_p,
        "precision_used_for_selection": used,
        "precision_prod_adjusted": adj,
        "precision_type": ptype,
    }


def build_datasets_section_v3(rated: Mapping[str, Any]) -> Dict[str, Any]:
    """Same split prefixes as v2, with v3 ``field_test`` diagnostics (prod-adjust optional)."""
    train = _split_prefixed_metrics(rated, "train_")
    val = _split_prefixed_metrics(rated, "val_")
    test = _split_prefixed_metrics(rated, "test_")
    _strip_val_test_noise(val, prefix="val_")
    val["field_test"] = _val_field_test_block_v3(rated)
    test["field_test"] = _test_field_test_block_v3(rated)
    out: Dict[str, Any] = {}
    if train:
        out["train"] = train
    if val:
        out["val"] = val
    if test:
        out["test"] = test
    return out


def _objective_contract_block(
    metrics_root: Mapping[str, Any], rated: Mapping[str, Any]
) -> Dict[str, Any]:
    """DEC-026 / field-test objective provenance (config ratio vs observed split ratios)."""
    pn = metrics_root.get("production_neg_pos_ratio")
    gate_blocked = bool(rated.get("optuna_hpo_gate_blocked") is True)
    _tri = _neg_pos_ratio_three_splits(rated)
    return {
        "selection_metric_id": _derive_selection_metric_id(rated),
        "threshold": {
            "selected": _finite_float(rated.get("threshold")),
            "recall_floor": metrics_root.get("threshold_selected_at_recall_floor"),
        },
        "constraints": {
            "min_alerts_per_hour": _finite_float(
                rated.get("field_test_min_alerts_per_hour_objective")
            ),
            "val_dec026_pick_window_hours": _finite_float(
                rated.get("val_dec026_pick_window_hours")
            ),
            "val_dec026_pick_min_alerts_per_hour": _finite_float(
                rated.get("val_dec026_pick_min_alerts_per_hour")
            ),
        },
        "gate": {
            "constrained_optuna_objective_allowed": rated.get(
                "field_test_constrained_optuna_objective_allowed"
            ),
            "optuna_gate_blocked": gate_blocked,
            "optuna_gate_blocked_reason_code": rated.get("optuna_hpo_gate_blocked_reason_code"),
            "optuna_gate_blocked_details": rated.get("optuna_hpo_gate_blocked_details"),
        },
        "ratio_assumption": {
            "production_neg_pos_ratio": _finite_float(pn) if pn is not None else None,
            "source": "config" if pn is not None else None,
            "required_for_selection": False,
        },
        "observed_split_ratios": {
            "train_neg_pos_ratio": _tri["train"]["neg_pos_ratio"],
            "val_neg_pos_ratio": _tri["val"]["neg_pos_ratio"],
            "test_neg_pos_ratio": _tri["test"]["neg_pos_ratio"],
        },
    }


def _segmentation_block(metrics_root: Mapping[str, Any]) -> Dict[str, Any]:
    """Issue #8 high-roller multi-tier outputs when present on ``metrics_root``."""
    hr = metrics_root.get("high_roller_segmentation")
    sh = metrics_root.get("segment_high")
    sl = metrics_root.get("segment_low")
    enabled = isinstance(hr, dict) and bool(hr)
    out: Dict[str, Any] = {"enabled": enabled}
    if enabled:
        out["high_roller_segmentation"] = hr
    if isinstance(sh, dict) and sh:
        out["segment_high"] = sh
    if isinstance(sl, dict) and sl:
        out["segment_low"] = sl
    return out


def _execution_and_selection_v3(
    rated: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Split rated remainder into slim ``selection`` vs ``execution`` buckets."""
    remainder = _selection_remainder(rated)
    selection: Dict[str, Any] = {}
    execution: Dict[str, Any] = {}
    for k, v in remainder.items():
        if k in _SELECTION_SLIM_KEYS:
            selection[k] = v
        else:
            execution[k] = v
    return selection, execution


def build_training_metrics_v3_payload(
    *,
    model_version: str,
    metrics_root: Mapping[str, Any],
) -> Dict[str, Any]:
    """Contract-first training metrics (v3) aligned with current pipeline semantics."""
    rated = metrics_root.get("rated")
    if not isinstance(rated, dict):
        rated = {}

    _datasets = build_datasets_section_v3(rated)
    _stage1_ds = rated.get("stage1_datasets")
    selection, execution = _execution_and_selection_v3(rated)

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_TRAINING_METRICS_V3,
        "model_version": model_version,
        "selection_mode": metrics_root.get("selection_mode"),
        "selection_mode_source": _SELECTION_MODE_SOURCE_V3,
        "production_neg_pos_ratio": metrics_root.get("production_neg_pos_ratio"),
        "neg_pos_ratio_overview": build_neg_pos_ratio_overview(metrics_root, rated),
        "objective_contract": _objective_contract_block(metrics_root, rated),
        "datasets": _datasets,
        "segmentation": _segmentation_block(metrics_root),
        "selection": selection,
        "execution": execution,
    }
    if isinstance(_stage1_ds, dict) and _stage1_ds:
        payload["stage1_datasets"] = _stage1_ds

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
) -> Tuple[Path, Path, Path, Path]:
    """Write v3/v2 metrics, feature importance, and comparison JSON; update optional metadata pointers.

    Returns written paths ``(v3, v2, feature_importance, comparison)`` (resolved).
    """
    root = Path(bundle_dir).resolve()
    rated = metrics_root.get("rated")
    if not isinstance(rated, dict):
        rated = {}

    v3_path = root / "training_metrics.v3.json"
    v2_path = root / "training_metrics.v2.json"
    fi_path = root / "feature_importance.json"
    cm_path = root / "comparison_metrics.json"

    v3_path.write_text(
        _json_dump(
            build_training_metrics_v3_payload(
                model_version=model_version,
                metrics_root=metrics_root,
            )
        ),
        encoding="utf-8",
    )
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
        arts["training_metrics_v3_path"] = str(v3_path)
        arts["training_metrics_v2_path"] = str(v2_path)
        arts["feature_importance_path"] = str(fi_path)
        arts["comparison_metrics_path"] = str(cm_path)

    return v3_path, v2_path, fi_path, cm_path
