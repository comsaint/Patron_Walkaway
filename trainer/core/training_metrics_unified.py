"""Single-file training metrics contract (``training_metrics.unified.v1``).

Replaces separate ``training_metrics.v2.json`` / ``training_metrics.v3.json`` /
``feature_importance.json`` / ``comparison_metrics.json`` with one JSON object
embedded under optional nested keys for backward-compatible tooling.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional, Tuple

SCHEMA_TRAINING_METRICS_UNIFIED = "training-metrics.unified.v1"


def primary_rated_metrics_row(metrics_root: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the primary rated / overall metrics row for v2/v3 builders and FI export."""
    om = metrics_root.get("overall_model")
    if isinstance(om, dict) and om:
        return dict(om)
    r = metrics_root.get("rated")
    return dict(r) if isinstance(r, dict) else {}


def _int_nonneg(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        i = int(x)
    except (TypeError, ValueError):
        return None
    return i if i >= 0 else None


def _finite_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def label_distribution_three_splits(m: Mapping[str, Any]) -> Dict[str, Any]:
    """Build ``label_distribution`` train/val/test from segment ``*_samples`` / ``*_positives``."""
    out: Dict[str, Any] = {}
    for split in ("train", "val", "test"):
        sk = f"{split}_samples"
        pk = f"{split}_positives"
        samples = _int_nonneg(m.get(sk))
        pos = _int_nonneg(m.get(pk))
        if samples is None and pos is None:
            continue
        s = int(samples or 0)
        p = int(pos or 0)
        neg = max(0, s - p)
        ratio: Optional[float] = None
        if p > 0:
            ratio = float(neg) / float(p)
        out[split] = {
            "positives": p,
            "negatives": neg,
            "neg_pos_ratio": ratio,
        }
    return out


def alerts_three_splits(m: Mapping[str, Any]) -> Dict[str, Any]:
    """Build ``alerts`` train/val/test from ``{split}_alerts`` / ``{split}_window_hours``."""
    out: Dict[str, Any] = {}
    for split in ("train", "val", "test"):
        ak = f"{split}_alerts"
        wk = f"{split}_window_hours"
        cnt = _int_nonneg(m.get(ak))
        wh = _finite_float(m.get(wk))
        if cnt is None and wh is None:
            continue
        aph: Optional[float] = None
        if cnt is not None and wh is not None and wh > 0:
            aph = float(cnt) / float(wh)
        blob: Dict[str, Any] = {}
        if cnt is not None:
            blob["count"] = cnt
        if wh is not None:
            blob["window_hours"] = float(wh)
        if aph is not None:
            blob["alerts_per_hour"] = aph
        if blob:
            out[split] = blob
    return out


def merge_label_distribution_sum(
    a: Mapping[str, Any], b: Mapping[str, Any]
) -> Dict[str, Any]:
    """Sum positives/negatives per split from two ``label_distribution`` blobs."""
    splits = set(a.keys()) | set(b.keys())
    out: Dict[str, Any] = {}
    for sp in splits:
        da = a.get(sp) if isinstance(a.get(sp), dict) else {}
        db = b.get(sp) if isinstance(b.get(sp), dict) else {}
        pa = _int_nonneg(da.get("positives")) or 0
        pb = _int_nonneg(db.get("positives")) or 0
        na = _int_nonneg(da.get("negatives")) or 0
        nb = _int_nonneg(db.get("negatives")) or 0
        p = pa + pb
        n = na + nb
        if p == 0 and n == 0:
            continue
        ratio = float(n) / float(p) if p > 0 else None
        out[sp] = {"positives": p, "negatives": n, "neg_pos_ratio": ratio}
    return out


def merge_rated_observation_count(
    a: Mapping[str, int], b: Mapping[str, int]
) -> Dict[str, int]:
    """Sum per-split observation counts from two segment maps."""
    keys = set(a.keys()) | set(b.keys())
    return {k: int(a.get(k, 0)) + int(b.get(k, 0)) for k in keys}


def rated_observation_count_from_metrics(m: Mapping[str, Any]) -> Dict[str, int]:
    """``train``/``val``/``test`` rated row counts from ``*_samples`` when present."""
    out: Dict[str, int] = {}
    for split in ("train", "val", "test"):
        sk = f"{split}_samples"
        s = _int_nonneg(m.get(sk))
        if s is not None:
            out[split] = int(s)
    return out


def build_segment_models_issue8(
    *,
    tail_key: str,
    low_key: str,
    thr_tail: float,
    thr_low: float,
    m_tail: Mapping[str, Any],
    m_low: Mapping[str, Any],
    unique_canonical_tail: Mapping[str, Optional[int]],
    unique_canonical_low: Mapping[str, Optional[int]],
) -> Dict[str, Any]:
    """Rich per-segment view for unified ``training_metrics.json``."""
    tail_ld = label_distribution_three_splits(m_tail)
    low_ld = label_distribution_three_splits(m_low)
    tail_obs = rated_observation_count_from_metrics(m_tail)
    low_obs = rated_observation_count_from_metrics(m_low)
    return {
        tail_key: {
            "bucket_index": 1,
            "role": "upper_tail",
            "threshold": float(thr_tail),
            "rated_observation_count": tail_obs,
            "unique_canonical_rated": {k: v for k, v in dict(unique_canonical_tail).items() if v is not None},
            "label_distribution": tail_ld,
            "alerts": alerts_three_splits(m_tail),
            "metrics": dict(m_tail),
        },
        low_key: {
            "bucket_index": 0,
            "role": "complement",
            "threshold": float(thr_low),
            "rated_observation_count": low_obs,
            "unique_canonical_rated": {k: v for k, v in dict(unique_canonical_low).items() if v is not None},
            "label_distribution": low_ld,
            "alerts": alerts_three_splits(m_low),
            "metrics": dict(m_low),
        },
    }


def build_segmentation_spec_issue8(
    *,
    theo_col: str,
    quantile: float,
    cutoff: float,
    tail_key: str,
    low_key: str,
    serving_root: str,
) -> Dict[str, Any]:
    """Percentile bucket spec for current two-segment Issue #8."""
    tail_pct = max(1, min(99, int(round((1.0 - float(quantile)) * 100.0))))
    low_pct_label = 100 - tail_pct
    return {
        "schema_version": "percentile_buckets_v1",
        "routing_feature": str(theo_col),
        "rated_train_quantile_method": "quantile_cont",
        "bucket_edges_quantile": [0.0, float(quantile), 1.0],
        "bucket_model_keys": [low_key, tail_key],
        "bucket_labels": [f"P0_P{low_pct_label}", f"P{low_pct_label}_P100"],
        "serving_root_model_key": str(serving_root),
        "high_roller_cutoff_theo": float(cutoff),
        "counts_contract": {
            "rated_observation_count": "positives+negatives from train_*_samples / *_positives on segment metrics row",
            "unique_canonical_rated": "COUNT(DISTINCT canonical_id) on rated rows per split (segment parquet)",
            "neg_pos_ratio": "negatives/positives per split",
        },
    }


def build_overall_model_issue8(
    *,
    primary_metrics: Mapping[str, Any],
    m_tail: Mapping[str, Any],
    m_low: Mapping[str, Any],
    tail_key: str,
    low_key: str,
    serving_root: str,
    serving_threshold: float,
    routed_test: Mapping[str, Any],
    unique_canonical_overall: Mapping[str, Optional[int]],
) -> Dict[str, Any]:
    """Pooled cohort + routed test summary; non-split keys copied from serving-root metrics."""
    tail_ld = label_distribution_three_splits(m_tail)
    low_ld = label_distribution_three_splits(m_low)
    merged_ld = merge_label_distribution_sum(tail_ld, low_ld)
    tail_obs = rated_observation_count_from_metrics(m_tail)
    low_obs = rated_observation_count_from_metrics(m_low)
    rated_obs = merge_rated_observation_count(
        {k: int(v) for k, v in tail_obs.items()},
        {k: int(v) for k, v in low_obs.items()},
    )
    tp = _int_nonneg(routed_test.get("overall_routed_test_tp"))
    fp = _int_nonneg(routed_test.get("overall_routed_test_fp"))
    test_alerts: Optional[int] = None
    if tp is not None and fp is not None:
        test_alerts = int(tp + fp)
    test_wh = _finite_float(primary_metrics.get("test_window_hours"))
    test_aph: Optional[float] = None
    if test_alerts is not None and test_wh is not None and test_wh > 0:
        test_aph = float(test_alerts) / float(test_wh)
    blend: Dict[str, Any] = {
        "method": "theo_threshold_router",
        "tail_model_key": tail_key,
        "low_value_model_key": low_key,
        "serving_root_segment": str(serving_root),
        "threshold_note": (
            "threshold is the serving-root segment threshold only; routed test uses "
            "per-segment thresholds inside the router."
        ),
        "test_routed": {
            "rows": routed_test.get("overall_routed_test_rows"),
            "tp": tp,
            "fp": fp,
            "fn": routed_test.get("overall_routed_test_fn"),
            "precision": routed_test.get("overall_routed_test_precision"),
            "recall": routed_test.get("overall_routed_test_recall"),
        },
        "train_val_alerts": {
            "skipped": "not_routed_pooled_use_segment_models_for_per_split_alerts",
        },
    }
    out: Dict[str, Any] = {
        "overall_model_kind": "issue8_segment_routed",
        "overall_blend": blend,
        "threshold": float(serving_threshold),
        "threshold_scope": "serving_root_only",
        "rated_observation_count": rated_obs,
        "unique_canonical_rated": {k: v for k, v in unique_canonical_overall.items() if v is not None},
        "label_distribution": merged_ld,
        "alerts": {},
    }
    alerts: Dict[str, Any] = {}
    for split in ("train", "val", "test"):
        ak = f"{split}_alerts"
        wk = f"{split}_window_hours"
        mt = _int_nonneg(m_tail.get(ak))
        ml = _int_nonneg(m_low.get(ak))
        wht = _finite_float(m_tail.get(wk))
        whl = _finite_float(m_low.get(wk))
        if split == "test" and test_alerts is not None:
            alerts[split] = {
                "count": test_alerts,
                "window_hours": test_wh,
                "alerts_per_hour": test_aph,
                "definition": "routed_test_tp_plus_fp_at_segment_thresholds",
            }
            continue
        if mt is None and ml is None:
            continue
        cnt = (mt or 0) + (ml or 0)
        wh = wht if wht is not None else whl
        aph = float(cnt) / float(wh) if wh is not None and wh and wh > 0 else None
        alerts[split] = {
            "count": int(cnt),
            "window_hours": float(wh) if wh is not None else None,
            "alerts_per_hour": aph,
            "definition": "sum_of_segment_models_at_own_thresholds_not_routed_merge",
        }
    out["alerts"] = alerts

    for k, v in primary_metrics.items():
        if not isinstance(k, str):
            continue
        if k.startswith(("train_", "val_", "test_")):
            continue
        out[k] = v
    out["val_precision"] = primary_metrics.get("val_precision")
    out["test_precision"] = primary_metrics.get("test_precision")
    out["val_recall"] = primary_metrics.get("val_recall")
    out["test_recall"] = primary_metrics.get("test_recall")
    return out


def build_unified_training_metrics_document(
    *,
    metrics_root: Mapping[str, Any],
    model_version: str,
    contract_v2: Mapping[str, Any],
    contract_v3: Mapping[str, Any],
    feature_importance: Mapping[str, Any],
    comparison_metrics: Mapping[str, Any],
) -> Dict[str, Any]:
    """Assemble the single ``training_metrics.json`` payload."""
    base: Dict[str, Any] = dict(metrics_root)
    base["schema_version"] = SCHEMA_TRAINING_METRICS_UNIFIED
    base["model_version"] = str(model_version)
    base["contract_v2"] = dict(contract_v2)
    base["contract_v3"] = dict(contract_v3)
    base["feature_importance"] = dict(feature_importance)
    base["comparison_metrics"] = dict(comparison_metrics)
    return base


def write_unified_training_metrics_json(
    bundle_dir: Path,
    *,
    model_version: str,
    metrics_root: Mapping[str, Any],
    contract_v2: Mapping[str, Any],
    contract_v3: Mapping[str, Any],
    feature_importance: Mapping[str, Any],
    comparison_metrics: Mapping[str, Any],
    model_metadata: Optional[MutableMapping[str, Any]] = None,
) -> Path:
    """Write ``training_metrics.json`` only; remove legacy sidecar files if present."""
    root = Path(bundle_dir).resolve()
    unified = build_unified_training_metrics_document(
        metrics_root=metrics_root,
        model_version=model_version,
        contract_v2=contract_v2,
        contract_v3=contract_v3,
        feature_importance=feature_importance,
        comparison_metrics=comparison_metrics,
    )
    out_path = root / "training_metrics.json"
    out_path.write_text(
        json.dumps(unified, indent=2, default=str, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    for stale in (
        "training_metrics.v2.json",
        "training_metrics.v3.json",
        "feature_importance.json",
        "comparison_metrics.json",
    ):
        p = root / stale
        if p.is_file():
            p.unlink()
    if model_metadata is not None:
        arts = model_metadata.setdefault("artifacts", {})
        arts["training_metrics_path"] = str(out_path)
        for k in (
            "training_metrics_v2_path",
            "training_metrics_v3_path",
            "feature_importance_path",
            "comparison_metrics_path",
        ):
            arts.pop(k, None)
    return out_path
