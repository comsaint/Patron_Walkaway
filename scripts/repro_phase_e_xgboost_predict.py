#!/usr/bin/env python3
"""Reproduce Phase E dense val predict (XGBoost sklearn path) without the full trainer.

Why a new file: there is no single CLI entry that runs only
``_phase_e_dense_positive_scores`` after a representative fit; this script does.

Typical pipeline log uses val ~1,168,555 rows, 50 cols, ``batch_rows=100_000``.
This XGBoost sklearn API exposes ``booster``, not ``booster_``, so production
hits the chunked ``predict_proba`` path (``batch_begin`` / ``batch_end``).

Memory: ``--preset full`` holds a full-size val ``DataFrame`` in RAM (~hundreds
of MB for float32 columns, more if float64). Use ``--preset smoke`` first.

Examples::

    python scripts/repro_phase_e_xgboost_predict.py --preset smoke
    python scripts/repro_phase_e_xgboost_predict.py --preset prodish
    python scripts/repro_phase_e_xgboost_predict.py --preset full --n-estimators 200
    python scripts/repro_phase_e_xgboost_predict.py --preset prodish --mimic-pipeline-fit
    python scripts/repro_phase_e_xgboost_predict.py --preset prodish --mimic-pipeline-fit --train-rows 800000
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
import pandas as pd

# Rated[xgboost] Optuna best from L2 bundle log (Step 9/11, Patron_Walkaway).
MIMIC_PIPELINE_OPTUNA_XGB: dict[str, Any] = {
    "n_estimators": 500,
    "learning_rate": 0.01875220945578641,
    "max_depth": 10,
    "reg_lambda": 0.7510418138777541,
    "reg_alpha": 4.9830438374949075,
    "subsample": 0.9474136752138245,
    "colsample_bytree": 0.7989499894055425,
    "min_child_weight": 15.826541904647563,
    "objective": "binary:logistic",
    "tree_method": "hist",
    "random_state": 42,
    "n_jobs": -1,
    "verbosity": 0,
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--preset",
        choices=("smoke", "prodish", "full"),
        default="smoke",
        help="smoke=small; prodish=~prod val rows scaled down trees; full=1168555 val rows",
    )
    p.add_argument("--train-rows", type=int, default=None, help="override train row count")
    p.add_argument("--val-rows", type=int, default=None, help="override val row count")
    p.add_argument("--n-features", type=int, default=50)
    p.add_argument("--n-estimators", type=int, default=None)
    p.add_argument("--batch-rows", type=int, default=100_000)
    p.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="override tree depth (default 10 when not using --mimic-pipeline-fit)",
    )
    p.add_argument(
        "--tree-method",
        type=str,
        default=None,
        help="override tree_method (default hist when not using --mimic-pipeline-fit)",
    )
    p.add_argument(
        "--mimic-pipeline-fit",
        action="store_true",
        help="use frozen Optuna-rated[xgboost] hyperparams from pipeline log "
        "(optional --n-estimators / --max-depth / --tree-method still override)",
    )
    p.add_argument(
        "--xgb-n-jobs",
        type=int,
        default=None,
        help="override XGBClassifier n_jobs (e.g. 1 to test single-thread predict)",
    )
    p.add_argument(
        "--bloat-gib",
        type=float,
        default=0.0,
        help="after fit, retain this many GiB of float32 zeros to simulate peak RAM",
    )
    p.add_argument(
        "--diag-memory-snapshot",
        action="store_true",
        help="enable trainer A3_PHASE_E_DIAG_MEMORY_SNAPSHOT for Phase E RSS logs",
    )
    p.add_argument("--first-batch-only", action="store_true", help="only run batch 1 predict")
    return p.parse_args(argv)


def _dims_for_preset(preset: str) -> tuple[int, int, int]:
    """Return (train_rows, val_rows, n_estimators) defaults for each preset."""
    if preset == "smoke":
        return 25_000, 120_000, 80
    if preset == "prodish":
        return 400_000, 1_168_555, 120
    return 500_000, 1_168_555, 200


def _make_xy(n: int, n_features: int, seed: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Build random binary classification data as float32 columns (lighter RAM)."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n, n_features)).astype(np.float32, copy=False)
    # Mildly separable label
    logits = x[:, 0] * 0.4 + x[:, 1] * 0.15 + rng.standard_normal(n).astype(np.float32) * 0.2
    y = (logits > 0.0).astype(np.int64)
    cols = [f"f{i}" for i in range(n_features)]
    return pd.DataFrame(x, columns=cols), y


def _xgb_classifier_kwargs(
    args: argparse.Namespace,
    *,
    n_estimators_default: int,
) -> dict[str, Any]:
    """Build keyword args for ``XGBClassifier`` (mimic preset vs quick defaults)."""
    if args.mimic_pipeline_fit:
        kw: dict[str, Any] = dict(MIMIC_PIPELINE_OPTUNA_XGB)
        if args.n_estimators is not None:
            kw["n_estimators"] = int(args.n_estimators)
        if args.max_depth is not None:
            kw["max_depth"] = int(args.max_depth)
        if args.tree_method is not None:
            kw["tree_method"] = str(args.tree_method)
        if getattr(args, "xgb_n_jobs", None) is not None:
            kw["n_jobs"] = int(args.xgb_n_jobs)
        return kw
    out = {
        "n_estimators": int(n_estimators_default),
        "max_depth": int(args.max_depth) if args.max_depth is not None else 10,
        "learning_rate": 0.05,
        "tree_method": str(args.tree_method) if args.tree_method is not None else "hist",
        "n_jobs": -1,
        "random_state": 42,
        "verbosity": 1,
    }
    if getattr(args, "xgb_n_jobs", None) is not None:
        out["n_jobs"] = int(args.xgb_n_jobs)
    return out


def _log_xgboost_sklearn_path(model: Any) -> None:
    """Log whether production Phase E will use booster_ branch or sklearn chunks."""
    b = getattr(model, "booster_", None)
    logging.info(
        "repro: getattr(model, 'booster_', None) is %s (None => sklearn chunk path)",
        "set" if b is not None else "None",
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns process exit code."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    tr_default, vr_default, ne_default = _dims_for_preset(args.preset)
    train_rows = int(args.train_rows if args.train_rows is not None else tr_default)
    val_rows = int(args.val_rows if args.val_rows is not None else vr_default)
    n_features = int(args.n_features)
    n_estimators = int(args.n_estimators if args.n_estimators is not None else ne_default)
    clf_kw = _xgb_classifier_kwargs(args, n_estimators_default=n_estimators)

    logging.warning(
        "repro: preset=%s mimic_pipeline_fit=%s train_rows=%d val_rows=%d n_features=%d "
        "clf_kwargs=%s batch_rows=%d (high val_rows / n_estimators => high RAM & runtime)",
        args.preset,
        args.mimic_pipeline_fit,
        train_rows,
        val_rows,
        n_features,
        {k: clf_kw[k] for k in ("n_estimators", "max_depth", "tree_method", "learning_rate") if k in clf_kw},
        args.batch_rows,
    )

    from xgboost import XGBClassifier

    X_tr, y_tr = _make_xy(train_rows, n_features, seed=1)
    X_va, y_va = _make_xy(val_rows, n_features, seed=2)

    model = XGBClassifier(**clf_kw)
    t_fit = time.perf_counter()
    model.fit(X_tr, y_tr)
    logging.info("repro: fit done in %.1fs", time.perf_counter() - t_fit)
    del X_tr, y_tr
    _log_xgboost_sklearn_path(model)

    _bloat_hold: list[np.ndarray] = []
    if float(getattr(args, "bloat_gib", 0.0) or 0.0) > 0.0:
        gib = float(args.bloat_gib)
        chunk_bytes = 256 * 1024 * 1024
        elems = chunk_bytes // 4
        target = int(gib * (1024**3))
        used = 0
        while used < target:
            _bloat_hold.append(np.zeros(elems, dtype=np.float32))
            used += chunk_bytes
        logging.warning(
            "repro: retained bloat arrays ~%.2f GiB (simulate pipeline RSS pressure)",
            used / (1024**3),
        )
        # Touch pages so RSS tracks reserved zeros (Windows may otherwise defer fault-in).
        for _arr in _bloat_hold:
            _arr[0] = 0.0
            _arr[-1] = 0.0

    if args.first_batch_only:
        br = min(int(args.batch_rows), 100_000)
        chunk = X_va.iloc[:br].astype(np.float32, copy=False)
        logging.info("repro: first_batch_only predict_proba rows=%d cols=%d", len(chunk), n_features)
        t0 = time.perf_counter()
        _ = model.predict_proba(chunk)[:, 1]
        logging.info("repro: first batch predict_proba wall_s=%.3f", time.perf_counter() - t0)
        logging.info("repro: done (first-batch-only)")
        return 0

    if getattr(args, "diag_memory_snapshot", False):
        import trainer.core.config as _cfg_mod

        _cfg_mod.A3_PHASE_E_DIAG_MEMORY_SNAPSHOT = True

    from trainer.training.gbm_bakeoff import _phase_e_dense_positive_scores

    t1 = time.perf_counter()
    scores, ds = _phase_e_dense_positive_scores(
        model,
        X_va,
        int(args.batch_rows),
        backend="xgboost",
        role="repro_val",
    )
    logging.info(
        "repro: _phase_e_dense_positive_scores done len=%d ds=%s wall_s=%.1f",
        len(scores),
        ds,
        time.perf_counter() - t1,
    )
    _ = y_va  # keep reference if later metrics wanted
    logging.info("repro: finished OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
