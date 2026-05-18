"""Load high-tier ``model.pkl`` bundle (Step 5 pickle layout)."""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from trainer_hightier.config import DEFAULT_MODEL_DIR
from trainer_hightier.core.model_bundle_paths import resolve_model_bundle_dir


@dataclass(frozen=True)
class HightierModelBundle:
    """Resolved artifacts next to ``model.pkl``."""

    bundle_dir: Path
    model: Any
    threshold: float
    feature_columns: tuple[str, ...]
    categorical_columns: tuple[str, ...]
    category_categories: Mapping[str, list[Any]]
    model_version: str
    training_metrics: dict[str, Any]


def _read_model_version(bundle_dir: Path) -> str:
    p = bundle_dir / "model_version"
    if p.is_file():
        return p.read_text(encoding="utf-8").strip() or "unknown"
    return "unknown"


def _read_training_metrics(bundle_dir: Path) -> dict[str, Any]:
    p = bundle_dir / "training_metrics.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def load_hightier_model_bundle(
    versions_root: Path | None = None,
    *,
    bundle_dir: Path | None = None,
) -> HightierModelBundle:
    """Resolve bundle directory and load ``model.pkl`` (pickle dict with sklearn model).

    Raises
    ------
    FileNotFoundError
        If ``model.pkl`` is missing.
    ValueError
        If payload is malformed.
    """
    if bundle_dir is not None:
        d = Path(bundle_dir).expanduser().resolve()
    else:
        root = Path(versions_root or DEFAULT_MODEL_DIR).resolve()
        d = resolve_model_bundle_dir(root.resolve())
    model_path = d / "model.pkl"
    if not model_path.is_file():
        raise FileNotFoundError(f"model.pkl not found under {d}")
    raw = pickle.loads(model_path.read_bytes())
    if not isinstance(raw, dict):
        raise ValueError(f"model.pkl must be a dict payload; got {type(raw)}")
    model = raw.get("model")
    feat = raw.get("feature_columns") or raw.get("feature_cols")
    if model is None or not feat:
        raise ValueError("model.pkl dict must contain 'model' and 'feature_columns'")
    cols = tuple(str(x) for x in list(feat))
    thr = float(raw.get("threshold", 0.5))
    cats = tuple(str(x) for x in list(raw.get("categorical_columns") or ()))
    cc = raw.get("category_categories") or {}
    if not isinstance(cc, dict):
        cc = {}
    mv = _read_model_version(d)
    metrics = _read_training_metrics(d)
    return HightierModelBundle(
        bundle_dir=d,
        model=model,
        threshold=thr,
        feature_columns=cols,
        categorical_columns=cats,
        category_categories=cc,
        model_version=mv,
        training_metrics=metrics,
    )


def infer_training_cutoff_iso(metrics: dict[str, Any]) -> Optional[str]:
    """Best-effort training cutoff ISO timestamp for snapshot gap fill.

    Prefer explicit keys; otherwise return ``None`` (callers use DB watermarks only).
    """
    for k in ("training_cutoff_iso", "step5_training_cutoff_iso", "data_cutoff_iso"):
        v = metrics.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None
