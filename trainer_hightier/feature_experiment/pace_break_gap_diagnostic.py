"""Second-round diagnostics for pace-break gap trajectory features."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.metrics import roc_auc_score

from trainer_hightier.feature_experiment.trajectory_feature_audit import (
    BET_ID_COLUMN,
    PLAYER_ID_COLUMN,
    SCORE_COLUMN,
    _BASE_COLS,
    _DEFAULT_MODEL_DIR,
    _DEFAULT_SPLITS_DIR,
    _build_alert_cohorts,
    _build_near_threshold_cohort,
    _build_split_context,
    _compute_player_session_baselines,
    _load_bundle,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEFAULT_OUT_DIR: Final[Path] = (
    _REPO_ROOT / "out" / "trajectory_feature_audit" / "pace_break_gap_diagnostic"
)

PACE_FEATURES: Final[tuple[str, ...]] = (
    "fe__traj__gap_since_prev_bet_sec",
    "fe__traj__gap_slope_last5",
    "fe__traj__gap_ratio_to_session_median_so_far",
    "fe__traj__gap_ratio_to_own_median_w30d",
    "fe__traj__pace_bets_cnt_w5m",
)


def _auc_direction(frame: pd.DataFrame, feature: str) -> dict[str, Any]:
    """Return univariate TP/FP separation metrics for one feature."""

    if frame.empty:
        return {
            "n": 0,
            "tp": 0,
            "fp": 0,
            "missing_rate": None,
            "auc": None,
            "ks": None,
            "direction": None,
            "tp_median": None,
            "fp_median": None,
        }
    vals = pd.to_numeric(frame[feature], errors="coerce")
    y = frame["is_tp"].astype(int)
    finite = vals.notna() & np.isfinite(vals)
    out: dict[str, Any] = {
        "n": int(len(frame)),
        "tp": int(y.sum()),
        "fp": int(len(y) - y.sum()),
        "missing_rate": float(1.0 - finite.mean()),
    }
    if finite.sum() == 0 or y[finite].nunique() < 2:
        out.update({"auc": None, "ks": None, "direction": None})
        return out

    x = vals[finite].to_numpy(dtype=np.float64)
    yy = y[finite].to_numpy(dtype=np.int8)
    tp_vals = vals[finite & frame["is_tp"]].to_numpy(dtype=np.float64)
    fp_vals = vals[finite & ~frame["is_tp"]].to_numpy(dtype=np.float64)
    out["auc"] = float(roc_auc_score(yy, x))
    out["ks"] = float(stats.ks_2samp(tp_vals, fp_vals).statistic)
    out["tp_median"] = float(np.median(tp_vals)) if len(tp_vals) else None
    out["fp_median"] = float(np.median(fp_vals)) if len(fp_vals) else None
    if out["tp_median"] is not None and out["fp_median"] is not None:
        out["direction"] = "higher_tp" if out["tp_median"] > out["fp_median"] else "higher_fp"
    else:
        out["direction"] = None
    return out


def _load_frames(
    model_dir: Path,
    splits_dir: Path,
) -> tuple[float, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Build raised-alert and near-threshold cohorts for val/test."""

    bundle = _load_bundle(model_dir)
    threshold = float(bundle["threshold"])
    train = pd.read_parquet(splits_dir / "train.parquet", columns=list(_BASE_COLS))
    baselines = _compute_player_session_baselines(train)

    alerts: dict[str, pd.DataFrame] = {}
    near: dict[str, pd.DataFrame] = {}
    for split in ("val", "test"):
        ctx = _build_split_context(
            bundle,
            split,
            splits_dir / f"{split}.parquet",
            baselines=baselines,
            threshold=threshold,
        )
        alerts[split] = _build_alert_cohorts(ctx)
        near[split] = _build_near_threshold_cohort(ctx)
        day_map = ctx.bets[[BET_ID_COLUMN, "gaming_day_event"]].drop_duplicates()
        if "gaming_day_event" not in alerts[split].columns:
            alerts[split] = alerts[split].merge(day_map, on=BET_ID_COLUMN, how="left")
        if not near[split].empty and "gaming_day_event" not in near[split].columns:
            near[split] = near[split].merge(day_map, on=BET_ID_COLUMN, how="left")
    return threshold, alerts, near


def _summarize_features(
    alerts: dict[str, pd.DataFrame],
    near: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Build feature-level separation summary across cohorts."""

    rows: list[dict[str, Any]] = []
    for split in ("val", "test"):
        for cohort_name, frame in (("raised", alerts[split]), ("near_threshold", near[split])):
            for feature in PACE_FEATURES:
                row = {"split": split, "cohort": cohort_name, "feature": feature}
                row.update(_auc_direction(frame, feature))
                rows.append(row)
    return pd.DataFrame(rows)


def _score_band_diagnostics(
    threshold: float,
    alerts: dict[str, pd.DataFrame],
    near: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Measure feature separation by score bands around the threshold."""

    bins = [
        -np.inf,
        threshold - 0.04,
        threshold - 0.02,
        threshold,
        threshold + 0.02,
        threshold + 0.05,
        np.inf,
    ]
    labels = [
        "below_-0.04",
        "-0.04_to_-0.02",
        "-0.02_to_0",
        "0_to_+0.02",
        "+0.02_to_+0.05",
        "above_+0.05",
    ]
    rows: list[dict[str, Any]] = []
    for split in ("val", "test"):
        frame = pd.concat([alerts[split], near[split]], ignore_index=True, sort=False)
        if frame.empty:
            continue
        scores = pd.to_numeric(frame[SCORE_COLUMN], errors="coerce")
        frame = frame.copy()
        frame["score_band_detail"] = pd.cut(scores, bins=bins, labels=labels)
        for band, band_slice in frame.groupby("score_band_detail", observed=True):
            for feature in PACE_FEATURES:
                row = {"split": split, "score_band": str(band), "feature": feature}
                row.update(_auc_direction(band_slice, feature))
                rows.append(row)
    return pd.DataFrame(rows)


def _top_fp_slice_diagnostics(alerts: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Check whether signal is driven by top FP-heavy player-days."""

    rows: list[dict[str, Any]] = []
    for split in ("val", "test"):
        frame = alerts[split].copy()
        if frame.empty:
            continue
        frame["player_day"] = (
            frame[PLAYER_ID_COLUMN].astype(str) + "@" + frame["gaming_day_event"].astype(str)
        )
        top_days = frame.loc[~frame["is_tp"], "player_day"].value_counts().head(10).index
        frame["slice"] = np.where(frame["player_day"].isin(top_days), "top10_fp_days", "other_days")
        for slice_name, slice_df in frame.groupby("slice"):
            for feature in PACE_FEATURES:
                row = {"split": split, "slice": slice_name, "feature": feature}
                row.update(_auc_direction(slice_df, feature))
                rows.append(row)
    return pd.DataFrame(rows)


def _coverage_diagnostics(
    alerts: dict[str, pd.DataFrame],
    near: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Coverage for train-baseline own-median gap ratio."""

    rows: list[dict[str, Any]] = []
    feature = "fe__traj__gap_ratio_to_own_median_w30d"
    for split in ("val", "test"):
        for cohort_name, frame in (("raised", alerts[split]), ("near_threshold", near[split])):
            if frame.empty:
                continue
            masks = {
                "all": pd.Series(True, index=frame.index),
                "tp": frame["is_tp"],
                "fp": ~frame["is_tp"],
            }
            for label_name, mask in masks.items():
                sub = frame.loc[mask]
                if sub.empty:
                    continue
                values = pd.to_numeric(sub[feature], errors="coerce")
                rows.append(
                    {
                        "split": split,
                        "cohort": cohort_name,
                        "label": label_name,
                        "n": int(len(sub)),
                        "coverage": float(values.notna().mean()),
                        "median": float(values.median()) if values.notna().any() else None,
                        "p75": float(values.quantile(0.75)) if values.notna().any() else None,
                    },
                )
    return pd.DataFrame(rows)


def _val_fit_filters(alerts: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Fit simple feature filters on val and apply to both splits."""

    rows: list[dict[str, Any]] = []
    for feature in PACE_FEATURES:
        val_frame = alerts["val"]
        val_values = pd.to_numeric(val_frame[feature], errors="coerce")
        if val_values.notna().sum() == 0:
            continue
        tp_median = val_values[val_frame["is_tp"]].median()
        fp_median = val_values[~val_frame["is_tp"]].median()
        direction = "higher_tp" if tp_median > fp_median else "higher_fp"
        cutoff_quantile = 0.2 if direction == "higher_tp" else 0.8
        cutoff = float(val_values.dropna().quantile(cutoff_quantile))
        for split in ("val", "test"):
            frame = alerts[split]
            values = pd.to_numeric(frame[feature], errors="coerce")
            kept = frame.loc[values >= cutoff] if direction == "higher_tp" else frame.loc[values <= cutoff]
            rows.append(
                {
                    "feature": feature,
                    "val_fit_direction": direction,
                    "val_fit_cutoff": cutoff,
                    "split": split,
                    "baseline_alerts": int(len(frame)),
                    "baseline_precision": float(frame["is_tp"].mean()) if len(frame) else None,
                    "retained_alerts": int(len(kept)),
                    "retained_precision": float(kept["is_tp"].mean()) if len(kept) else None,
                    "retained_tp": int(kept["is_tp"].sum()),
                    "retained_fp": int((~kept["is_tp"]).sum()),
                },
            )
    return pd.DataFrame(rows)


def _write_report(
    out_dir: Path,
    alerts: dict[str, pd.DataFrame],
    summary: pd.DataFrame,
    coverage: pd.DataFrame,
    filters: pd.DataFrame,
) -> None:
    """Write a concise markdown report."""

    lines = ["# Pace Break Gap Diagnostic Report", "", "## Baseline"]
    for split in ("val", "test"):
        frame = alerts[split]
        lines.append(
            f"- {split}: {frame['is_tp'].mean():.1%} precision, {len(frame)} raised alerts, "
            f"{int(frame['is_tp'].sum())} TP / {int((~frame['is_tp']).sum())} FP",
        )
    lines.extend(
        [
            "",
            "## Main Finding",
            "",
            "Gap features do not pass a validation-stable gate. The strongest test signals are "
            "gap-based, but the same features are weak or direction-reversed on val. Treat test "
            "uplift as exploratory, not retrain-ready.",
            "",
            "## Feature Summary",
        ],
    )
    for feature in PACE_FEATURES:
        parts: list[str] = []
        for split in ("val", "test"):
            row = summary[
                (summary["split"] == split)
                & (summary["cohort"] == "raised")
                & (summary["feature"] == feature)
            ].iloc[0]
            auc = "NA" if pd.isna(row["auc"]) else f"{row['auc']:.3f}"
            missing = "NA" if pd.isna(row["missing_rate"]) else f"{row['missing_rate']:.1%}"
            parts.append(f"{split} AUC={auc}, dir={row['direction']}, missing={missing}")
        lines.append(f"- `{feature}`: " + "; ".join(parts))

    lines.extend(["", "## Own-Median Coverage"])
    own = coverage[(coverage["cohort"] == "raised") & (coverage["label"] == "all")]
    for _, row in own.iterrows():
        lines.append(
            f"- {row['split']} raised coverage for `gap_ratio_to_own_median_w30d`: "
            f"{row['coverage']:.1%} ({int(row['n'])} alerts)",
        )

    lines.extend(
        [
            "",
            "## Val-Fit Filter Check",
            "",
            "Filters are fit only from val medians/cutoffs, then applied to test.",
        ],
    )
    for feature in PACE_FEATURES:
        sub = filters[filters["feature"] == feature]
        if sub.empty:
            continue
        test = sub[sub["split"] == "test"].iloc[0]
        lines.append(
            f"- `{feature}` ({test['val_fit_direction']}, cutoff={test['val_fit_cutoff']:.4g}): "
            f"test retained {int(test['retained_alerts'])}/{int(test['baseline_alerts'])} alerts, "
            f"precision {test['retained_precision']:.1%} vs baseline {test['baseline_precision']:.1%}",
        )

    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            "Do not retrain on the current gap features. If we continue, first fix or replace the "
            "train-only player own-median baseline and test a more robust definition: gap expansion "
            "relative to same-session prior gaps. Then require val/test direction consistency before promotion.",
        ],
    )
    (out_dir / "pace_break_gap_diagnostic_report.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def run_diagnostic(model_dir: Path, splits_dir: Path, out_dir: Path) -> None:
    """Run the second-round pace-break gap diagnostic."""

    threshold, alerts, near = _load_frames(model_dir, splits_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = _summarize_features(alerts, near)
    score_bands = _score_band_diagnostics(threshold, alerts, near)
    top_fp = _top_fp_slice_diagnostics(alerts)
    coverage = _coverage_diagnostics(alerts, near)
    filters = _val_fit_filters(alerts)

    summary.to_csv(out_dir / "pace_break_gap_summary.csv", index=False)
    score_bands.to_csv(out_dir / "pace_break_gap_score_band.csv", index=False)
    top_fp.to_csv(out_dir / "pace_break_gap_top_fp_slices.csv", index=False)
    coverage.to_csv(out_dir / "pace_break_gap_coverage.csv", index=False)
    filters.to_csv(out_dir / "pace_break_gap_valfit_filters.csv", index=False)
    _write_report(out_dir, alerts, summary, coverage, filters)

    metadata = {
        "threshold": threshold,
        "outputs": [
            "pace_break_gap_summary.csv",
            "pace_break_gap_score_band.csv",
            "pace_break_gap_top_fp_slices.csv",
            "pace_break_gap_coverage.csv",
            "pace_break_gap_valfit_filters.csv",
            "pace_break_gap_diagnostic_report.md",
        ],
    }
    (out_dir / "pace_break_gap_diagnostic.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(metadata, indent=2))


def main() -> None:
    """CLI entrypoint."""

    parser = argparse.ArgumentParser(description="Second-round pace-break gap diagnostics")
    parser.add_argument("--model-dir", type=Path, default=_DEFAULT_MODEL_DIR)
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS_DIR)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    args = parser.parse_args()
    run_diagnostic(args.model_dir, args.splits_dir, args.out_dir)


if __name__ == "__main__":
    main()
