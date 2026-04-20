"""
Single-script Phase 2 Track A experiment runner.

This script provides a minimal, auditable workflow for:
1) Baseline evaluation (existing model or freshly trained baseline),
2) A1 focal-like reweight training,
3) A2 focal-like + hard-negative mining training,
4) Forward/Purged time-series validation (Track C compliance).

How to run
==========

1) Simplest run (trainer local parquet + existing baseline model):
    python investigations/precision_uplift_recall_1pct/phase2/run_track_a_experiment.py \
      --use-local-parquet \
      --holdout-start 2026-03-01T00:00:00+08:00 \
      --baseline-mode existing

2) Train baseline from scratch (A0/A1/A2 all retrained):
    python investigations/precision_uplift_recall_1pct/phase2/run_track_a_experiment.py \
      --use-local-parquet \
      --holdout-start 2026-03-01T00:00:00+08:00 \
      --baseline-mode train

3) Use a custom parquet path (instead of trainer/.data/chunks):
    python investigations/precision_uplift_recall_1pct/phase2/run_track_a_experiment.py \
      --parquet-path data/labeled_samples.parquet \
      --holdout-start 2026-03-01T00:00:00+08:00 \
      --baseline-mode existing

4) Optional common overrides:
    - --holdout-end <ISO-8601>              (default: auto to data max timestamp)
    - --existing-model-path <path/to/model.pkl> (default: auto resolve from out/models)
    - --output-json <path> / --output-md <path> (default: phase2/track_a_single_script_results.*)
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import re
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve

try:
    import lightgbm as lgb
except Exception as exc:  # pragma: no cover - runtime dependency guard.
    raise SystemExit(
        "lightgbm import failed. Please install LightGBM in this environment before running Track A script."
    ) from exc


TrainFn = Callable[[pd.DataFrame, np.ndarray], Any]


@dataclass
class VariantResult:
    name: str
    holdout_pat1pct: float | None
    holdout_pr_auc: float | None
    holdout_uplift_pp_vs_baseline: float | None
    cv_pat1pct_series: list[float]
    cv_pat1pct_mean: float | None
    cv_pat1pct_std: float | None
    cv_fold_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Phase 2 Track A experiments with optional existing baseline model and "
            "Forward/Purged validation."
        )
    )
    parser.add_argument(
        "--parquet-path",
        required=False,
        default=None,
        help=(
            "Parquet file or directory path. Optional when --use-local-parquet is set."
        ),
    )
    parser.add_argument(
        "--use-local-parquet",
        action="store_true",
        help=(
            "Use trainer local parquet convention. For this Track A script it loads "
            "trainer/.data/chunks as the labeled-feature source."
        ),
    )
    parser.add_argument(
        "--label-col",
        default="label",
        help="Binary label column (default follows trainer output: label).",
    )
    parser.add_argument(
        "--time-col",
        default="decision_ts",
        help="Timestamp column (default follows trainer/parity contract: decision_ts).",
    )
    parser.add_argument(
        "--feature-cols",
        required=False,
        default=None,
        help=(
            "Optional comma-separated feature columns. If omitted, script infers from existing model "
            "feature names (preferred) or numeric parquet columns excluding reserved fields."
        ),
    )
    parser.add_argument(
        "--holdout-start",
        required=True,
        help="Holdout start timestamp (ISO-8601). Train uses rows strictly earlier than this timestamp.",
    )
    parser.add_argument(
        "--holdout-end",
        required=False,
        default=None,
        help=(
            "Holdout end timestamp (ISO-8601, exclusive). If omitted, script uses data max timestamp "
            "(effectively all rows >= holdout-start)."
        ),
    )
    parser.add_argument(
        "--baseline-mode",
        choices=("existing", "train"),
        default="existing",
        help="Baseline source: existing model file or train-from-scratch baseline.",
    )
    parser.add_argument(
        "--existing-model-path",
        default=None,
        help=(
            "Path to existing model pickle. If omitted in existing mode, script auto-resolves from "
            "out/models/_latest_model_manifest.json or latest out/models/*/model.pkl."
        ),
    )
    parser.add_argument(
        "--target-recall",
        type=float,
        default=0.01,
        help="Target recall for precision@recall metric (default: 0.01).",
    )
    parser.add_argument(
        "--cv-splits",
        type=int,
        default=3,
        help="Number of forward CV folds on pre-holdout train data.",
    )
    parser.add_argument(
        "--purge-hours",
        type=float,
        default=0.0,
        help="Purge gap (hours) before each validation window.",
    )
    parser.add_argument(
        "--min-fold-size",
        type=int,
        default=300,
        help="Minimum row count for both train and validation in a fold.",
    )
    parser.add_argument(
        "--lgbm-n-estimators",
        type=int,
        default=300,
        help="LightGBM n_estimators for script retraining.",
    )
    parser.add_argument(
        "--lgbm-learning-rate",
        type=float,
        default=0.05,
        help="LightGBM learning rate for script retraining.",
    )
    parser.add_argument(
        "--lgbm-num-leaves",
        type=int,
        default=63,
        help="LightGBM num_leaves for script retraining.",
    )
    parser.add_argument(
        "--focal-alpha",
        type=float,
        default=0.25,
        help="Alpha for focal-like reweighting.",
    )
    parser.add_argument(
        "--focal-gamma",
        type=float,
        default=2.0,
        help="Gamma for focal-like reweighting.",
    )
    parser.add_argument(
        "--hard-negative-top-rate",
        type=float,
        default=0.01,
        help="Top-rate threshold among negative samples to mark hard negatives.",
    )
    parser.add_argument(
        "--hard-negative-weight",
        type=float,
        default=3.0,
        help="Extra weight multiplier applied to hard negatives in A2.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help=(
            "Optional safety cap for laptop memory. If >0, keeps only the latest N rows by time_col."
        ),
    )
    parser.add_argument(
        "--output-json",
        required=False,
        default="investigations/precision_uplift_recall_1pct/phase2/track_a_single_script_results.json",
        help="Output JSON path (default: phase2/track_a_single_script_results.json).",
    )
    parser.add_argument(
        "--output-md",
        required=False,
        default="investigations/precision_uplift_recall_1pct/phase2/track_a_single_script_results.md",
        help="Output markdown summary path (default: phase2/track_a_single_script_results.md).",
    )
    return parser.parse_args()


def precision_at_recall_target(
    y_true: np.ndarray,
    y_score: np.ndarray,
    target_recall: float,
) -> float | None:
    if y_true.size == 0:
        return None
    positives = int(np.sum(y_true == 1))
    negatives = int(np.sum(y_true == 0))
    if positives == 0 or negatives == 0:
        return None
    precision, recall, _ = precision_recall_curve(y_true, y_score)
    mask = recall >= target_recall
    if not np.any(mask):
        return None
    return float(np.max(precision[mask]))


def calc_pr_auc(y_true: np.ndarray, y_score: np.ndarray) -> float | None:
    positives = int(np.sum(y_true == 1))
    negatives = int(np.sum(y_true == 0))
    if positives == 0 or negatives == 0:
        return None
    return float(average_precision_score(y_true, y_score))


def build_lgbm_classifier(args: argparse.Namespace) -> lgb.LGBMClassifier:
    return lgb.LGBMClassifier(
        objective="binary",
        random_state=42,
        class_weight="balanced",
        n_estimators=args.lgbm_n_estimators,
        learning_rate=args.lgbm_learning_rate,
        num_leaves=args.lgbm_num_leaves,
        n_jobs=-1,
        verbose=-1,
    )


def compute_base_sample_weights(y: np.ndarray) -> np.ndarray:
    # Keep behavior explicit and stable across script runs.
    pos_count = float(np.sum(y == 1))
    neg_count = float(np.sum(y == 0))
    if pos_count == 0.0 or neg_count == 0.0:
        return np.ones_like(y, dtype=float)
    pos_weight = neg_count / pos_count
    weights = np.ones_like(y, dtype=float)
    weights[y == 1] = pos_weight
    return weights


def train_baseline_model(X: pd.DataFrame, y: np.ndarray, args: argparse.Namespace) -> Any:
    model = build_lgbm_classifier(args)
    model.fit(X, y)
    return model


def train_focal_like_model(X: pd.DataFrame, y: np.ndarray, args: argparse.Namespace) -> tuple[Any, np.ndarray]:
    base_weights = compute_base_sample_weights(y)
    warm_model = build_lgbm_classifier(args)
    warm_model.fit(X, y, sample_weight=base_weights)
    p = warm_model.predict_proba(X)[:, 1]
    eps = 1e-6
    p = np.clip(p, eps, 1.0 - eps)

    alpha = float(args.focal_alpha)
    gamma = float(args.focal_gamma)
    focal_factor = np.where(
        y == 1,
        alpha * np.power(1.0 - p, gamma),
        (1.0 - alpha) * np.power(p, gamma),
    )
    focal_weights = base_weights * focal_factor

    final_model = build_lgbm_classifier(args)
    final_model.fit(X, y, sample_weight=focal_weights)
    return final_model, focal_weights


def train_hard_negative_model(X: pd.DataFrame, y: np.ndarray, args: argparse.Namespace) -> Any:
    focal_model, focal_weights = train_focal_like_model(X, y, args)
    p = focal_model.predict_proba(X)[:, 1]
    neg_mask = y == 0
    if not np.any(neg_mask):
        model = build_lgbm_classifier(args)
        model.fit(X, y, sample_weight=focal_weights)
        return model

    top_rate = float(args.hard_negative_top_rate)
    top_rate = min(max(top_rate, 1e-6), 0.5)
    neg_scores = p[neg_mask]
    threshold = float(np.quantile(neg_scores, 1.0 - top_rate))
    hard_negative_mask = neg_mask & (p >= threshold)

    weights = focal_weights.copy()
    weights[hard_negative_mask] *= float(args.hard_negative_weight)

    model = build_lgbm_classifier(args)
    model.fit(X, y, sample_weight=weights)
    return model


def model_predict_proba_1(model: Any, X: pd.DataFrame) -> np.ndarray:
    if hasattr(model, "predict_proba"):
        out = model.predict_proba(X)
        if isinstance(out, np.ndarray) and out.ndim == 2 and out.shape[1] >= 2:
            return out[:, 1]
        if isinstance(out, np.ndarray) and out.ndim == 1:
            return out
    if hasattr(model, "predict"):
        out = model.predict(X)
        arr = np.asarray(out, dtype=float)
        if arr.ndim == 1:
            return arr
    raise ValueError("Model does not provide a usable predict_proba/predict output.")


def forward_purged_splits(
    ts: pd.Series,
    n_splits: int,
    purge_hours: float,
    min_fold_size: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    if n_splits < 2:
        raise ValueError("cv_splits must be >= 2 for robust time validation.")
    order = np.argsort(ts.values.astype("datetime64[ns]"))
    n = len(order)
    chunk = n // (n_splits + 1)
    if chunk < min_fold_size:
        raise ValueError(
            f"Not enough rows for requested cv_splits={n_splits} and min_fold_size={min_fold_size}. "
            f"rows={n}, chunk={chunk}."
        )

    purge_delta = pd.to_timedelta(timedelta(hours=float(purge_hours)))
    folds: list[tuple[np.ndarray, np.ndarray]] = []
    ts_np = ts.values.astype("datetime64[ns]")

    for i in range(n_splits):
        train_end = (i + 1) * chunk
        valid_end = (i + 2) * chunk if (i + 2) * chunk < n else n
        train_idx = order[:train_end]
        valid_idx = order[train_end:valid_end]
        if train_idx.size < min_fold_size or valid_idx.size < min_fold_size:
            continue
        if purge_delta > pd.Timedelta(0):
            valid_start_ts = ts_np[valid_idx[0]]
            cutoff = np.datetime64(valid_start_ts) - np.timedelta64(int(purge_delta.value), "ns")
            train_idx = train_idx[ts_np[train_idx] < cutoff]
        if train_idx.size < min_fold_size:
            continue
        folds.append((train_idx, valid_idx))
    return folds


def safe_mean_std(series: list[float]) -> tuple[float | None, float | None]:
    if not series:
        return None, None
    if len(series) == 1:
        return float(series[0]), None
    return float(np.mean(series)), float(np.std(series, ddof=1))


def load_existing_model(path: Path) -> Any:
    with path.open("rb") as f:
        return pickle.load(f)


def parse_feature_cols(raw: str) -> list[str]:
    cols = [x.strip() for x in raw.split(",")]
    out = [x for x in cols if x]
    if not out:
        raise ValueError("feature columns are empty.")
    return out


def resolve_input_parquet_path(args: argparse.Namespace) -> Path:
    if args.use_local_parquet:
        chunk_dir = Path("trainer/.data/chunks")
        if not chunk_dir.exists():
            raise FileNotFoundError(
                "trainer/.data/chunks does not exist. Run trainer first or pass --parquet-path."
            )
        chunk_files = sorted(chunk_dir.glob("chunk_*.parquet"))
        if not chunk_files:
            raise FileNotFoundError(
                "No chunk_*.parquet found under trainer/.data/chunks. "
                "Run trainer to generate labeled chunks or pass --parquet-path."
            )
        return chunk_dir

    if args.parquet_path:
        p = Path(args.parquet_path)
        if not p.exists():
            raise FileNotFoundError(f"parquet path not found: {p}")
        return p

    raise FileNotFoundError(
        "No input parquet source. Use --parquet-path or set --use-local-parquet."
    )


def _try_load_latest_model_manifest_path() -> Path | None:
    manifest = Path("out/models/_latest_model_manifest.json")
    if not manifest.exists():
        return None
    try:
        obj = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    candidates = [
        obj.get("model_path"),
        obj.get("model_file"),
        obj.get("bundle_model_path"),
    ]
    for c in candidates:
        if not isinstance(c, str) or not c.strip():
            continue
        p = Path(c.strip())
        if p.exists() and p.is_file():
            return p
        p2 = Path("out/models") / c.strip()
        if p2.exists() and p2.is_file():
            return p2
    ver = obj.get("model_version")
    if isinstance(ver, str) and ver.strip():
        p3 = Path("out/models") / ver.strip() / "model.pkl"
        if p3.exists():
            return p3
    return None


def resolve_existing_model_path(raw_path: str | None) -> Path:
    if raw_path:
        p = Path(raw_path)
        if not p.exists():
            raise FileNotFoundError(f"existing model path not found: {p}")
        return p

    from_manifest = _try_load_latest_model_manifest_path()
    if from_manifest is not None:
        return from_manifest

    root = Path("out/models")
    if not root.exists():
        raise FileNotFoundError(
            "existing baseline mode requires model.pkl. out/models does not exist and no --existing-model-path provided."
        )
    candidates = sorted(root.glob("*/model.pkl"), key=lambda x: x.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    raise FileNotFoundError(
        "existing baseline mode requires model.pkl, but none found under out/models/*/model.pkl."
    )


def read_dataset(
    parquet_path: Path,
    use_cols: list[str],
    time_col: str,
    max_rows: int,
) -> pd.DataFrame:
    df = pd.read_parquet(parquet_path, columns=use_cols)
    if df.empty:
        raise ValueError("Loaded parquet is empty.")
    df[time_col] = pd.to_datetime(df[time_col], errors="coerce", utc=False)
    df = df.dropna(subset=[time_col])
    df = df.sort_values(time_col).reset_index(drop=True)
    if max_rows > 0 and len(df) > max_rows:
        # Keep latest rows to preserve recent-time behavior while limiting RAM.
        df = df.iloc[-max_rows:].copy().reset_index(drop=True)
    return df


def ensure_binary_labels(df: pd.DataFrame, label_col: str) -> np.ndarray:
    raw = pd.to_numeric(df[label_col], errors="coerce")
    mask = raw.isin([0, 1])
    if not bool(mask.all()):
        raise ValueError(f"label column {label_col!r} must contain only 0/1 values after coercion.")
    return raw.astype(int).to_numpy()


def infer_feature_cols_from_model(model: Any, available_cols: list[str]) -> list[str]:
    names: list[str] = []
    for attr in ("feature_name_", "feature_names_in_"):
        val = getattr(model, attr, None)
        if val is None:
            continue
        try:
            names = [str(x) for x in list(val)]
        except Exception:
            continue
        if names:
            break
    if not names:
        return []
    avail = set(available_cols)
    out = [c for c in names if c in avail]
    return out


def infer_feature_cols_from_parquet_schema(
    parquet_path: Path,
    excluded: set[str],
) -> list[str]:
    # Read one row only for type-safe schema sniffing with low memory overhead.
    df0 = pd.read_parquet(parquet_path).head(1)
    candidates: list[str] = []
    for col in df0.columns:
        c = str(col)
        if c in excluded:
            continue
        if c.startswith("label_") or c.endswith("_label"):
            continue
        if re.search(r"(ts|time|date)$", c):
            continue
        ser = df0[c]
        if pd.api.types.is_numeric_dtype(ser):
            candidates.append(c)
    return candidates


def parquet_columns_fast(parquet_path: Path) -> list[str]:
    try:
        import pyarrow.parquet as pq  # local optional dependency

        schema = pq.read_schema(parquet_path)
        return [str(x) for x in schema.names]
    except Exception:
        # Fallback path; can be heavier than metadata-only, but keeps script usable.
        return [str(x) for x in pd.read_parquet(parquet_path).columns]


def run_variant_holdout(
    train_X: pd.DataFrame,
    train_y: np.ndarray,
    holdout_X: pd.DataFrame,
    holdout_y: np.ndarray,
    train_fn: TrainFn,
    target_recall: float,
) -> tuple[float | None, float | None]:
    model = train_fn(train_X, train_y)
    holdout_scores = model_predict_proba_1(model, holdout_X)
    pat = precision_at_recall_target(holdout_y, holdout_scores, target_recall)
    pr_auc = calc_pr_auc(holdout_y, holdout_scores)
    return pat, pr_auc


def run_cv_series(
    X: pd.DataFrame,
    y: np.ndarray,
    ts: pd.Series,
    train_fn: TrainFn,
    n_splits: int,
    purge_hours: float,
    min_fold_size: int,
    target_recall: float,
) -> list[float]:
    folds = forward_purged_splits(ts, n_splits, purge_hours, min_fold_size)
    scores: list[float] = []
    for train_idx, valid_idx in folds:
        X_tr = X.iloc[train_idx]
        y_tr = y[train_idx]
        X_va = X.iloc[valid_idx]
        y_va = y[valid_idx]
        if int(np.sum(y_tr == 1)) == 0 or int(np.sum(y_va == 1)) == 0:
            continue
        model = train_fn(X_tr, y_tr)
        pred = model_predict_proba_1(model, X_va)
        pat = precision_at_recall_target(y_va, pred, target_recall)
        if pat is not None:
            scores.append(float(pat))
    return scores


def variant_to_dict(v: VariantResult) -> dict[str, Any]:
    return asdict(v)


def write_markdown_report(
    out_path: Path,
    baseline_mode: str,
    target_recall: float,
    cv_splits: int,
    purge_hours: float,
    uplift_gate_pp: float,
    std_gate_pp: float,
    variants: list[VariantResult],
) -> None:
    def fmt(x: float | None, pct: bool = False) -> str:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "n/a"
        if pct:
            return f"{x * 100:.2f}%"
        return f"{x:.6f}"

    lines: list[str] = []
    lines.append("# Track A Single-Script Results")
    lines.append("")
    lines.append("## Contract")
    lines.append("")
    lines.append(f"- baseline mode: `{baseline_mode}`")
    lines.append(f"- target recall: `{target_recall}`")
    lines.append(f"- Forward/Purged CV: splits=`{cv_splits}`, purge_hours=`{purge_hours}`")
    lines.append(f"- uplift gate suggestion: `{uplift_gate_pp:.2f} pp`")
    lines.append(f"- std gate suggestion: `{std_gate_pp:.2f} pp`")
    lines.append("")
    lines.append("## Variant Summary")
    lines.append("")
    lines.append("| variant | holdout PAT@1% | uplift vs baseline (pp) | cv mean PAT@1% | cv std (pp) | folds |")
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: |")
    for v in variants:
        uplift_pp = v.holdout_uplift_pp_vs_baseline
        std_pp = None if v.cv_pat1pct_std is None else v.cv_pat1pct_std * 100.0
        lines.append(
            "| "
            f"{v.name} | "
            f"{fmt(v.holdout_pat1pct, pct=True)} | "
            f"{'n/a' if uplift_pp is None else f'{uplift_pp:.2f}'} | "
            f"{fmt(v.cv_pat1pct_mean, pct=True)} | "
            f"{'n/a' if std_pp is None else f'{std_pp:.2f}'} | "
            f"{v.cv_fold_count} |"
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Track C compliance in this script = Forward/Purged CV with PAT@1% series and mean/std."
    )
    lines.append(
        "- If `baseline_mode=existing`, baseline CV may be unavailable because an external fitted model is not fold-specific."
    )
    lines.append(
        "- A1/A2 are retrained variants by design; this is required because weighting/mining act at training time."
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    parquet_path = resolve_input_parquet_path(args)
    existing_model: Any | None = None
    existing_model_path_resolved: Path | None = None
    if args.baseline_mode == "existing":
        existing_model_path_resolved = resolve_existing_model_path(args.existing_model_path)
        existing_model = load_existing_model(existing_model_path_resolved)

    if args.feature_cols:
        feature_cols = parse_feature_cols(args.feature_cols)
    else:
        schema_cols = parquet_columns_fast(parquet_path)
        inferred: list[str] = []
        if existing_model is not None:
            inferred = infer_feature_cols_from_model(existing_model, schema_cols)
        if not inferred:
            excluded = {
                args.label_col,
                args.time_col,
                "canonical_id",
                "is_rated",
                "run_id",
                "gaming_day",
                "score",
                "is_alert",
                "censored",
            }
            inferred = infer_feature_cols_from_parquet_schema(parquet_path, excluded)
        if not inferred:
            raise SystemExit(
                "Unable to infer feature columns. Please pass --feature-cols explicitly."
            )
        feature_cols = inferred

    required_cols = [args.label_col, args.time_col] + feature_cols

    df = read_dataset(parquet_path, required_cols, args.time_col, args.max_rows)
    y_all = ensure_binary_labels(df, args.label_col)
    X_all = df[feature_cols].copy()
    ts_all = df[args.time_col]

    holdout_start = pd.to_datetime(args.holdout_start)
    holdout_end = pd.to_datetime(args.holdout_end) if args.holdout_end else None
    holdout_end_effective = holdout_end if holdout_end is not None else pd.to_datetime(ts_all.max())
    train_mask = ts_all < holdout_start
    holdout_mask = ts_all >= holdout_start
    if holdout_end is not None:
        holdout_mask = holdout_mask & (ts_all < holdout_end)
    if int(np.sum(train_mask)) == 0 or int(np.sum(holdout_mask)) == 0:
        raise SystemExit("train/holdout split is empty. Please check holdout-start/holdout-end.")

    train_X = X_all.loc[train_mask].reset_index(drop=True)
    train_y = y_all[train_mask.to_numpy()]
    train_ts = ts_all.loc[train_mask].reset_index(drop=True)
    holdout_X = X_all.loc[holdout_mask].reset_index(drop=True)
    holdout_y = y_all[holdout_mask.to_numpy()]

    if int(np.sum(train_y == 1)) == 0 or int(np.sum(holdout_y == 1)) == 0:
        raise SystemExit("Positive labels missing in train or holdout split.")

    baseline_pat: float | None = None
    baseline_pr_auc: float | None = None

    variants: dict[str, VariantResult] = {}

    if args.baseline_mode == "existing":
        assert existing_model is not None
        pred = model_predict_proba_1(existing_model, holdout_X)
        baseline_pat = precision_at_recall_target(holdout_y, pred, args.target_recall)
        baseline_pr_auc = calc_pr_auc(holdout_y, pred)
        baseline_cv: list[float] = []
        b_mean, b_std = safe_mean_std(baseline_cv)
        variants["baseline"] = VariantResult(
            name="baseline",
            holdout_pat1pct=baseline_pat,
            holdout_pr_auc=baseline_pr_auc,
            holdout_uplift_pp_vs_baseline=0.0 if baseline_pat is not None else None,
            cv_pat1pct_series=baseline_cv,
            cv_pat1pct_mean=b_mean,
            cv_pat1pct_std=b_std,
            cv_fold_count=len(baseline_cv),
        )
    else:
        baseline_train_fn: TrainFn = lambda X, y: train_baseline_model(X, y, args)
        pat, pr_auc = run_variant_holdout(
            train_X, train_y, holdout_X, holdout_y, baseline_train_fn, args.target_recall
        )
        baseline_pat, baseline_pr_auc = pat, pr_auc
        baseline_cv = run_cv_series(
            train_X,
            train_y,
            train_ts,
            baseline_train_fn,
            args.cv_splits,
            args.purge_hours,
            args.min_fold_size,
            args.target_recall,
        )
        b_mean, b_std = safe_mean_std(baseline_cv)
        variants["baseline"] = VariantResult(
            name="baseline",
            holdout_pat1pct=baseline_pat,
            holdout_pr_auc=baseline_pr_auc,
            holdout_uplift_pp_vs_baseline=0.0 if baseline_pat is not None else None,
            cv_pat1pct_series=baseline_cv,
            cv_pat1pct_mean=b_mean,
            cv_pat1pct_std=b_std,
            cv_fold_count=len(baseline_cv),
        )

    a1_train_fn: TrainFn = lambda X, y: train_focal_like_model(X, y, args)[0]
    a2_train_fn: TrainFn = lambda X, y: train_hard_negative_model(X, y, args)

    for name, train_fn in (("a1_focal_like", a1_train_fn), ("a2_focal_like_hard_negative", a2_train_fn)):
        pat, pr_auc = run_variant_holdout(
            train_X,
            train_y,
            holdout_X,
            holdout_y,
            train_fn,
            args.target_recall,
        )
        cv_series = run_cv_series(
            train_X,
            train_y,
            train_ts,
            train_fn,
            args.cv_splits,
            args.purge_hours,
            args.min_fold_size,
            args.target_recall,
        )
        c_mean, c_std = safe_mean_std(cv_series)
        uplift_pp: float | None = None
        if baseline_pat is not None and pat is not None:
            uplift_pp = (pat - baseline_pat) * 100.0
        variants[name] = VariantResult(
            name=name,
            holdout_pat1pct=pat,
            holdout_pr_auc=pr_auc,
            holdout_uplift_pp_vs_baseline=uplift_pp,
            cv_pat1pct_series=cv_series,
            cv_pat1pct_mean=c_mean,
            cv_pat1pct_std=c_std,
            cv_fold_count=len(cv_series),
        )

    uplift_gate_pp = 3.0
    std_gate_pp = 2.5

    summary: dict[str, Any] = {
        "contract": {
            "metric": "precision@recall",
            "target_recall": float(args.target_recall),
            "baseline_mode": args.baseline_mode,
            "existing_model_path": (
                str(existing_model_path_resolved) if existing_model_path_resolved is not None else None
            ),
            "label_col": args.label_col,
            "time_col": args.time_col,
            "feature_count": len(feature_cols),
            "feature_cols_preview": feature_cols[:30],
            "track_c_validation": {
                "method": "forward_purged_cv",
                "cv_splits": int(args.cv_splits),
                "purge_hours": float(args.purge_hours),
            },
            "holdout_window": {
                "start": str(holdout_start),
                "end_exclusive": str(holdout_end_effective),
                "end_auto_from_data_max": holdout_end is None,
            },
            "gate_reference": {
                "min_uplift_pp_vs_baseline": uplift_gate_pp,
                "max_std_pp_across_windows": std_gate_pp,
            },
        },
        "data_snapshot": {
            "rows_total": int(len(df)),
            "rows_train": int(np.sum(train_mask)),
            "rows_holdout": int(np.sum(holdout_mask)),
            "positives_train": int(np.sum(train_y == 1)),
            "positives_holdout": int(np.sum(holdout_y == 1)),
            "memory_safety_note": (
                "Script reads selected columns only; use --max-rows for laptop RAM safety when needed."
            ),
        },
        "variants": [variant_to_dict(v) for v in variants.values()],
    }

    out_json = Path(args.output_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    write_markdown_report(
        Path(args.output_md),
        args.baseline_mode,
        float(args.target_recall),
        int(args.cv_splits),
        float(args.purge_hours),
        uplift_gate_pp,
        std_gate_pp,
        list(variants.values()),
    )

    print(f"Done. JSON: {out_json}")
    print(f"Done. MD: {args.output_md}")


if __name__ == "__main__":
    main()
