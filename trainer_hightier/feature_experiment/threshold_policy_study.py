"""Threshold policy study for hot-patron retrain artifacts."""

from __future__ import annotations

import argparse
import importlib
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trainer_hightier.evaluation.metrics_blocks import split_metrics_block
from trainer_hightier.evaluation.player_alert_policy import operational_simulated_metrics_block
from trainer_hightier.serving.feature_builder import prepare_lgbm_feature_matrix

_B5 = importlib.import_module("trainer_hightier.05_lgbm_train")
aggregate_bets_to_player_game = _B5.aggregate_bets_to_player_game

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MODEL_DIR = _REPO_ROOT / "out" / "models_high_tier_mvp" / "20260610-003854-7f676f4"
_DEFAULT_SPLITS_DIR = _REPO_ROOT / "trainer_hightier" / "artifacts" / "training_data" / "splits"
_DEFAULT_OUTPUT = _REPO_ROOT / "out" / "hot_patron_mission" / "threshold_policy_study.md"
_META_COLUMNS = ("player_id", "game_id", "gaming_day_event", "walkaway_label", "payout_complete_dtm", "bet_id")


@dataclass(frozen=True)
class SplitScore:
    """Scores and player-game aggregation for one split."""

    name: str
    frame: pd.DataFrame
    scores: np.ndarray
    player_game: Any
    window_hours: float | None


def _load_bundle(model_dir: Path) -> dict[str, Any]:
    """Load a model bundle from a Step 5 artifact directory."""

    model_path = Path(model_dir) / "model.pkl"
    if not model_path.is_file():
        raise FileNotFoundError(f"model.pkl not found: {model_path}")
    return pickle.loads(model_path.read_bytes())


def _window_hours(frame: pd.DataFrame) -> float | None:
    """Return split span in hours from payout timestamps."""

    ts = pd.to_datetime(frame["payout_complete_dtm"], errors="coerce")
    if not ts.notna().any():
        return None
    span = float((ts.max() - ts.min()).total_seconds()) / 3600.0
    return span if np.isfinite(span) and span > 0 else None


def _score_split(bundle: dict[str, Any], split_path: Path, split_name: str) -> SplitScore:
    """Load, score, and aggregate one split to player-game grain."""

    feature_columns = tuple(bundle["feature_columns"])
    columns = list(dict.fromkeys([*_META_COLUMNS, *feature_columns]))
    frame = pd.read_parquet(split_path, columns=columns)
    x_mat = prepare_lgbm_feature_matrix(
        frame,
        feature_columns=feature_columns,
        categorical_columns=tuple(bundle.get("categorical_columns", ())),
        category_categories=dict(bundle.get("category_categories", {})),
    )
    scores = bundle["model"].predict_proba(x_mat)[:, 1]
    player_game = aggregate_bets_to_player_game(frame, scores, split_name=split_name)
    return SplitScore(split_name, frame, scores, player_game, _window_hours(frame))


def _threshold_table(y_true: np.ndarray, scores: np.ndarray, window_hours: float | None) -> pd.DataFrame:
    """Enumerate score-boundary thresholds and player-game metrics."""

    y_arr = np.asarray(y_true, dtype=np.int8).reshape(-1)
    s_arr = np.asarray(scores, dtype=np.float64).reshape(-1)
    order = np.argsort(-s_arr, kind="mergesort")
    ys = y_arr[order].astype(np.int64)
    scs = s_arr[order]
    boundaries = np.flatnonzero(np.r_[True, scs[1:] != scs[:-1]])
    ends = np.r_[boundaries[1:], len(scs)]
    tp = np.cumsum(ys)[ends - 1]
    alerts = ends.astype(np.int64)
    positives = int(np.sum(y_arr == 1))
    precision = tp / alerts
    recall = tp / float(positives) if positives > 0 else np.zeros_like(tp, dtype=np.float64)
    out = pd.DataFrame({"threshold": scs[boundaries], "alerts": alerts, "precision": precision, "recall": recall})
    out["alerts_per_hour"] = np.nan if window_hours is None else out["alerts"] / float(window_hours)
    return out


def _pick_at_precision(table: pd.DataFrame, min_precision: float) -> pd.Series | None:
    """Pick max-recall row under a precision floor."""

    feasible = table.loc[table["precision"] >= float(min_precision) - 1e-15]
    if feasible.empty:
        return None
    return feasible.sort_values(["recall", "precision", "threshold"], ascending=[False, False, False]).iloc[0]


def _metrics_at_threshold(split: SplitScore, threshold: float) -> dict[str, Any]:
    """Return player-game and operational metrics at one threshold."""

    pg = split_metrics_block(
        split.name,
        split.player_game.y_true,
        split.player_game.scores,
        threshold,
        window_hours=split.window_hours,
    )
    op = operational_simulated_metrics_block(
        split.name,
        split.player_game.candidates,
        threshold,
        window_hours=split.window_hours,
    )
    return {**pg, **op}


def _format_pct(value: float | None) -> str:
    """Format a proportion as a percentage string."""

    if value is None or not np.isfinite(float(value)):
        return "NA"
    return f"{100.0 * float(value):.2f}%"


def _format_num(value: float | int | None, digits: int = 3) -> str:
    """Format nullable numeric output."""

    if value is None or not np.isfinite(float(value)):
        return "NA"
    return f"{float(value):.{digits}f}"


def _markdown_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    """Render a compact markdown table."""

    header = "| " + " | ".join(label for label, _ in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(str(row.get(key, "")) for _, key in columns) + " |" for row in rows]
    return "\n".join([header, sep, *body])


def _floor_rows(val_table: pd.DataFrame, val_split: SplitScore, test_split: SplitScore) -> list[dict[str, Any]]:
    """Build sensitivity rows for several validation precision floors."""

    rows: list[dict[str, Any]] = []
    for floor in (0.60, 0.55, 0.50, 0.45, 0.40):
        pick = _pick_at_precision(val_table, floor)
        if pick is None:
            rows.append({"floor": f"{floor:.2f}", "status": "infeasible"})
            continue
        threshold = float(pick["threshold"])
        val_metrics = _metrics_at_threshold(val_split, threshold)
        test_metrics = _metrics_at_threshold(test_split, threshold)
        rows.append(_build_floor_row(floor, threshold, val_metrics, test_metrics))
    return rows


def _build_floor_row(
    floor: float,
    threshold: float,
    val_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
) -> dict[str, Any]:
    """Build one markdown row for a validation precision floor."""

    return {
        "floor": f"{floor:.2f}",
        "threshold": _format_num(threshold, 6),
        "val_pg_precision": _format_pct(val_metrics.get("val_precision")),
        "val_pg_alerts_hr": _format_num(val_metrics.get("val_alerts_per_hour"), 3),
        "val_op_precision": _format_pct(val_metrics.get("val_operational_simulated_precision")),
        "val_op_alerts_hr": _format_num(val_metrics.get("val_operational_simulated_alerts_per_hour"), 3),
        "test_op_precision": _format_pct(test_metrics.get("test_operational_simulated_precision")),
        "test_op_alerts_hr": _format_num(test_metrics.get("test_operational_simulated_alerts_per_hour"), 3),
    }


def _fixed_floor_rows(val_table: pd.DataFrame, val_split: SplitScore, test_split: SplitScore) -> list[dict[str, Any]]:
    """Return representative rows for the fixed min_precision=0.6 frontier."""

    feasible = val_table.loc[val_table["precision"] >= 0.6 - 1e-15].copy()
    if feasible.empty:
        return []
    targets = [0.10, 0.25, 0.50, 1.00, 2.00, 3.00]
    rows: list[dict[str, Any]] = []
    seen: set[float] = set()
    for target in targets:
        under = feasible.loc[feasible["alerts_per_hour"] <= target + 1e-15]
        if under.empty:
            continue
        pick = under.sort_values("recall", ascending=False).iloc[0]
        threshold = float(pick["threshold"])
        if threshold in seen:
            continue
        seen.add(threshold)
        rows.append(_build_frontier_row(target, threshold, val_split, test_split))
    return rows


def _build_frontier_row(
    target: float,
    threshold: float,
    val_split: SplitScore,
    test_split: SplitScore,
) -> dict[str, Any]:
    """Build one fixed-floor frontier row with operational test metrics."""

    val_metrics = _metrics_at_threshold(val_split, threshold)
    test_metrics = _metrics_at_threshold(test_split, threshold)
    return {
        "target": _format_num(target, 2),
        "threshold": _format_num(threshold, 6),
        "val_pg_precision": _format_pct(val_metrics.get("val_precision")),
        "val_pg_alerts_hr": _format_num(val_metrics.get("val_alerts_per_hour"), 3),
        "val_op_precision": _format_pct(val_metrics.get("val_operational_simulated_precision")),
        "val_op_alerts_hr": _format_num(val_metrics.get("val_operational_simulated_alerts_per_hour"), 3),
        "test_op_precision": _format_pct(test_metrics.get("test_operational_simulated_precision")),
        "test_op_alerts_hr": _format_num(test_metrics.get("test_operational_simulated_alerts_per_hour"), 3),
    }


def _build_report(bundle: dict[str, Any], val_split: SplitScore, test_split: SplitScore) -> str:
    """Build the markdown threshold policy study."""

    val_table = _threshold_table(val_split.player_game.y_true, val_split.player_game.scores, val_split.window_hours)
    feasible = val_table.loc[val_table["precision"] >= 0.6 - 1e-15]
    max_fixed = _pick_at_precision(val_table, 0.6)
    floor_rows = _floor_rows(val_table, val_split, test_split)
    fixed_rows = _fixed_floor_rows(val_table, val_split, test_split)
    max_alerts_hr = None if max_fixed is None else float(max_fixed["alerts_per_hour"])
    conclusion = "No val-feasible 1 alert/hr point exists at min_precision=0.6."
    if max_alerts_hr is not None and max_alerts_hr >= 1.0:
        conclusion = "A val-feasible 1 alert/hr point exists at min_precision=0.6."
    return _render_report(bundle, feasible, max_alerts_hr, floor_rows, fixed_rows, conclusion)


def _render_report(
    bundle: dict[str, Any],
    feasible: pd.DataFrame,
    max_alerts_hr: float | None,
    floor_rows: list[dict[str, Any]],
    fixed_rows: list[dict[str, Any]],
    conclusion: str,
) -> str:
    """Render final markdown from computed tables."""

    floor_cols = [
        ("val min_prec", "floor"),
        ("threshold", "threshold"),
        ("val pg prec", "val_pg_precision"),
        ("val pg alerts/hr", "val_pg_alerts_hr"),
        ("val op prec", "val_op_precision"),
        ("val op alerts/hr", "val_op_alerts_hr"),
        ("test op prec", "test_op_precision"),
        ("test op alerts/hr", "test_op_alerts_hr"),
    ]
    frontier_cols = [("target val alerts/hr", "target"), *floor_cols[1:]]
    return "\n\n".join(
        [
            "# Threshold Policy Study",
            f"Model: `{_DEFAULT_MODEL_DIR}`",
            f"Bundle threshold: `{float(bundle['threshold']):.6f}`",
            f"Fixed `min_precision=0.6` feasible thresholds: `{len(feasible)}`",
            f"Max val player-game alerts/hr at `min_precision=0.6`: `{_format_num(max_alerts_hr, 3)}`",
            f"Conclusion: **{conclusion}**",
            "## Fixed 0.6 Frontier",
            _markdown_table(fixed_rows, frontier_cols) if fixed_rows else "No feasible rows.",
            "## Precision Floor Sensitivity",
            _markdown_table(floor_rows, floor_cols),
        ]
    ) + "\n"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=_DEFAULT_MODEL_DIR)
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS_DIR)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    """Run the threshold policy study."""

    args = parse_args()
    bundle = _load_bundle(args.model_dir)
    val_split = _score_split(bundle, Path(args.splits_dir) / "val.parquet", "val")
    test_split = _score_split(bundle, Path(args.splits_dir) / "test.parquet", "test")
    report = _build_report(bundle, val_split, test_split)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
