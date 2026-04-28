#!/usr/bin/env python3
"""Patch A4 ``model.pkl`` for train–serve contract alignment.

1. ``--patch-stage1-only`` (default when no parquet): backup ``model.pkl``, set
   ``a4_stage1_threshold_before_final_calibration`` to the current ``threshold``
   (the stage-1 DEC-026 value from the original training run). Does **not** change
   ``threshold`` (fused-surface DEC-026 requires validation rows).

2. ``--valid-parquet PATH``: load rated validation features + ``label``, reproduce
   fused scores with the bundle's stage-1 model, stage-2 model, and
   ``a4_candidate_cutoff``, then run the same DEC-026 pick as training (kwargs read
   from ``training_metrics.json`` when present, else ``trainer.core.config``).

Examples (run from repo root)::

    python trainer/scripts/recalibrate_a4_model_pkl_fused_threshold.py \\
        out/models/20260426-220557-9a19582 --patch-stage1-only

    python trainer/scripts/recalibrate_a4_model_pkl_fused_threshold.py \\
        out/models/MYRUN --valid-parquet path/to/rated_valid.parquet
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import joblib
import numpy as np

from trainer.core import config as cfg
from trainer.training import trainer as trainer_mod
from trainer.training.two_stage import (
    candidate_mask_from_scores,
    fuse_product_scores,
)


def _read_training_metrics_rated(bundle_dir: Path) -> Dict[str, Any]:
    p = bundle_dir / "training_metrics.json"
    if not p.is_file():
        return {}
    root = json.loads(p.read_text(encoding="utf-8"))
    r = root.get("rated")
    return dict(r) if isinstance(r, dict) else {}


def _dec026_kwargs_from_bundle(bundle_dir: Path) -> Tuple[float, float, Optional[float], Optional[float]]:
    """Return (recall_floor, min_alert_count, min_alerts_per_hour, window_hours)."""
    rated = _read_training_metrics_rated(bundle_dir)
    meta_path = bundle_dir / "model_metadata.json"
    recall = float(rated.get("threshold_min_recall") or cfg.THRESHOLD_MIN_RECALL or 0.01)
    min_cnt = int(rated.get("threshold_min_alert_count") or cfg.THRESHOLD_MIN_ALERT_COUNT or 1)
    mah = rated.get("val_dec026_pick_min_alerts_per_hour")
    wh = rated.get("val_dec026_pick_window_hours")
    if mah is not None and wh is not None:
        return recall, min_cnt, float(mah), float(wh)
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        tp = meta.get("training_params") or {}
        recall = float(tp.get("threshold_min_recall", recall))
        min_cnt = int(tp.get("threshold_min_alert_count", min_cnt))
    mah_f = float(mah) if mah is not None else None
    wh_f = float(wh) if wh is not None else None
    return recall, min_cnt, mah_f, wh_f


def patch_stage1_only(bundle_dir: Path) -> Path:
    """Backup ``model.pkl`` and set ``a4_stage1_threshold_before_final_calibration``."""
    pkl = bundle_dir / "model.pkl"
    if not pkl.is_file():
        raise FileNotFoundError(f"Missing model.pkl: {pkl}")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = bundle_dir / f"model.pkl.bak-before-a4-stage1-field-{ts}"
    shutil.copy2(pkl, bak)
    d: Dict[str, Any] = joblib.load(pkl)
    if not bool(d.get("a4_enabled")):
        raise ValueError("Bundle is not A4-enabled; nothing to patch.")
    t_old = float(d["threshold"])
    d["a4_stage1_threshold_before_final_calibration"] = t_old
    joblib.dump(d, pkl)
    return bak


def recalibrate_threshold_from_valid_parquet(bundle_dir: Path, valid_parquet: Path) -> float:
    """Pick fused-surface DEC-026 threshold and update ``model.pkl``."""
    pkl = bundle_dir / "model.pkl"
    d: Dict[str, Any] = joblib.load(pkl)
    if not bool(d.get("a4_enabled")):
        raise ValueError("Bundle is not A4-enabled.")
    stage1_thr = float(
        d.get("a4_stage1_threshold_before_final_calibration") or d["threshold"]
    )
    features = list(d.get("features") or [])
    model = d["model"]
    stage2 = d.get("stage2_model")
    if stage2 is None:
        raise ValueError("Bundle missing stage2_model.")
    cutoff = d.get("a4_candidate_cutoff")
    if cutoff is None:
        from trainer.training.two_stage import candidate_cutoff_from_threshold

        s1_for_cut = float(d.get("a4_stage1_threshold_before_final_calibration") or stage1_thr)
        cutoff = candidate_cutoff_from_threshold(
            s1_for_cut,
            float(getattr(cfg, "A4_TWO_STAGE_CANDIDATE_MULTIPLIER", 0.9)),
        )
    cutoff_f = float(cutoff)
    rated_df = trainer_mod._load_rated_eval_split_from_parquet(valid_parquet, features)
    if rated_df.empty or "label" not in rated_df.columns:
        raise ValueError("valid_parquet must be non-empty rated rows with a label column.")
    x_s1 = trainer_mod._dataframe_for_lgb_predict(model, rated_df, features)
    batch = int(max(1, getattr(cfg, "A4_TWO_STAGE_PREDICT_BATCH_ROWS", 250_000)))
    s1 = trainer_mod._batched_model_positive_class_scores(model, x_s1, batch)
    cand = candidate_mask_from_scores(s1, cutoff=cutoff_f)
    s2 = np.ones(len(s1), dtype=np.float64)
    if int(np.sum(cand)) > 0:
        x2 = x_s1.loc[cand, :]
        s2[cand] = trainer_mod._batched_model_positive_class_scores(stage2, x2, batch)
    fused = fuse_product_scores(s1, s2)
    y = rated_df["label"].to_numpy(dtype=float)
    recall_floor, min_alert_count, mah, wh = _dec026_kwargs_from_bundle(bundle_dir)
    pick = trainer_mod.pick_dec026_threshold_from_binary_scores(
        y,
        fused,
        recall_floor=recall_floor,
        min_alert_count=min_alert_count,
        min_alerts_per_hour=mah,
        window_hours=wh,
        fbeta_beta=float(cfg.THRESHOLD_FBETA),
    )
    if pick.is_fallback:
        raise RuntimeError(
            "DEC-026 pick on fused validation returned fallback; refusing to write "
            f"(bundle_dir={bundle_dir})."
        )
    t_fused = float(pick.threshold)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = bundle_dir / f"model.pkl.bak-before-a4-fused-threshold-recal-{ts}"
    shutil.copy2(pkl, bak)
    d["a4_stage1_threshold_before_final_calibration"] = stage1_thr
    d["threshold"] = t_fused
    joblib.dump(d, pkl)
    return t_fused


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "bundle_dir",
        type=Path,
        help="Model bundle directory containing model.pkl",
    )
    ap.add_argument(
        "--valid-parquet",
        type=Path,
        default=None,
        help="Rated validation parquet (features + label); triggers fused DEC-026.",
    )
    ap.add_argument(
        "--patch-stage1-only",
        action="store_true",
        help="Only set a4_stage1_threshold_before_final_calibration (backup first).",
    )
    args = ap.parse_args()
    bd = args.bundle_dir.resolve()
    if not bd.is_dir():
        raise SystemExit(f"Not a directory: {bd}")
    if args.valid_parquet is not None:
        t = recalibrate_threshold_from_valid_parquet(bd, args.valid_parquet.resolve())
        print(f"Wrote fused threshold={t} to {bd / 'model.pkl'}")
        return
    if not args.patch_stage1_only:
        args.patch_stage1_only = True
    bak = patch_stage1_only(bd)
    print(f"Backed up to {bak}")
    print(
        f"Patched {bd / 'model.pkl'}: "
        "a4_stage1_threshold_before_final_calibration = previous threshold. "
        "Fused-surface threshold unchanged (pass --valid-parquet to recalibrate)."
    )


if __name__ == "__main__":
    main()
