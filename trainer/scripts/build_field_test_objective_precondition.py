"""Build W1 precondition artifacts for field-test objective gating.

This script aggregates per-fold metrics JSON files into:
1) machine-readable precondition JSON
2) human-readable markdown summary

It is intentionally lightweight so DS can run it on a laptop after existing
trainer/backtester runs, without introducing heavy recomputation.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ALLOWED_REASON_CODES: Sequence[str] = (
    "missing_field:<field_name>",
    "t_feasible_insufficient:<fold_id>",
    "t_feasible_empty:<fold_id>",
    "tail_support_insufficient:<fold_id>",
    "prod_ratio_unstable",
    "data_contract_mismatch",
)


@dataclass(frozen=True)
class FoldEvidence:
    """Normalized fold evidence required by W1-C2."""

    fold_id: str
    positive_count: Optional[int]
    finalized_tp_count: Optional[int]
    rated_bet_count: Optional[int]
    fold_duration_hours: Optional[float]
    t_feasible_size: int
    tail_support_sufficient: bool
    reason_codes: List[str]
    test_neg_pos_ratio: Optional[float]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate fold metrics into W1 precondition artifacts "
            "(field_test_objective_precondition_check.{json,md})."
        )
    )
    parser.add_argument(
        "--fold-metrics-json",
        action="append",
        dest="fold_metrics_jsons",
        required=True,
        help="Path to a fold-level metrics JSON. Repeat for multiple folds.",
    )
    parser.add_argument("--run-id", required=True, help="Run id for this precondition check.")
    parser.add_argument("--start-ts", required=True, help="Evaluation window start timestamp.")
    parser.add_argument("--end-ts", required=True, help="Evaluation window end timestamp.")
    parser.add_argument(
        "--timezone",
        default="Asia/Hong_Kong",
        help="Timezone label for the window (default: Asia/Hong_Kong).",
    )
    parser.add_argument(
        "--selection-mode",
        default="field_test",
        help="Selection mode label (default: field_test).",
    )
    parser.add_argument(
        "--production-neg-pos-ratio",
        type=float,
        default=None,
        help="Assumed production neg/pos ratio used by prod-adjusted precision.",
    )
    parser.add_argument(
        "--production-neg-pos-ratio-source",
        default="",
        help="Short source description for production neg/pos ratio assumption.",
    )
    parser.add_argument(
        "--production-neg-pos-ratio-sensitivity",
        default="",
        help="Short sensitivity summary for the production neg/pos ratio assumption.",
    )
    parser.add_argument(
        "--min-t-feasible-size",
        type=int,
        default=3,
        help="Minimum feasible threshold count per fold (default: 3).",
    )
    parser.add_argument(
        "--min-tail-positive-count",
        type=int,
        default=30,
        help="Minimum positives considered sufficient for tail support (default: 30).",
    )
    parser.add_argument(
        "--objective-decision-override",
        choices=("single_constrained", "composite"),
        default=None,
        help="Override auto objective decision.",
    )
    parser.add_argument(
        "--output-json",
        default="out/precision_uplift_field_test_objective/field_test_objective_precondition_check.json",
        help="Output path for machine-readable precondition JSON.",
    )
    parser.add_argument(
        "--output-md",
        default="trainer/precision_improvement_plan/field_test_objective_precondition_check.md",
        help="Output path for human-readable precondition markdown summary.",
    )
    return parser


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}, got {type(data).__name__}.")
    return data


def _walk_dict_candidates(data: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield plausible nested metrics dicts for key extraction."""
    yield data
    for key in ("model_default", "rated", "metrics", "rated_metrics"):
        child = data.get(key)
        if isinstance(child, dict):
            yield child


def _extract_first(data: Dict[str, Any], keys: Sequence[str]) -> Any:
    for blob in _walk_dict_candidates(data):
        for key in keys:
            if key in blob:
                return blob[key]
    return None


def _extract_t_feasible_size(data: Dict[str, Any]) -> int:
    """Approximate feasible threshold support from available threshold keys.

    If explicit `t_feasible_size` exists, use it.
    Else count non-null threshold candidates from `threshold_at_recall_*`.
    """
    explicit = _extract_first(data, ("t_feasible_size", "T_feasible_size"))
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            return 0

    candidate_count = 0
    for blob in _walk_dict_candidates(data):
        for key, value in blob.items():
            if key.startswith("threshold_at_recall_") and value is not None:
                candidate_count += 1
    return candidate_count


def _to_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_fold_duration_hours(data: Dict[str, Any]) -> Optional[float]:
    direct = _to_float_or_none(
        _extract_first(data, ("fold_duration_hours", "window_hours", "eval_window_hours"))
    )
    if direct is not None:
        return direct
    start_ts = _extract_first(data, ("start_ts", "window_start_ts", "window_start"))
    end_ts = _extract_first(data, ("end_ts", "window_end_ts", "window_end"))
    if not isinstance(start_ts, str) or not isinstance(end_ts, str):
        return None
    try:
        start = datetime.fromisoformat(start_ts)
        end = datetime.fromisoformat(end_ts)
        delta_seconds = (end - start).total_seconds()
        if delta_seconds <= 0:
            return None
        return delta_seconds / 3600.0
    except ValueError:
        return None


def _extract_fold_evidence(
    *,
    fold_id: str,
    data: Dict[str, Any],
    min_t_feasible_size: int,
    min_tail_positive_count: int,
) -> FoldEvidence:
    positive_count = _to_int_or_none(
        _extract_first(data, ("test_positives", "val_positives", "positive_count"))
    )
    finalized_tp_count = _to_int_or_none(
        _extract_first(data, ("finalized_tp_count", "tp", "true_positives", "test_tp"))
    )
    rated_bet_count = _to_int_or_none(
        _extract_first(data, ("test_samples", "val_samples", "rated_bet_count"))
    )
    fold_duration_hours = _extract_fold_duration_hours(data)
    t_feasible_size = _extract_t_feasible_size(data)
    test_neg_pos_ratio = _to_float_or_none(_extract_first(data, ("test_neg_pos_ratio",)))

    reason_codes: List[str] = []
    if positive_count is None:
        reason_codes.append("missing_field:positive_count")
    if finalized_tp_count is None:
        reason_codes.append("missing_field:finalized_tp_count")
    if rated_bet_count is None:
        reason_codes.append("missing_field:rated_bet_count")
    if fold_duration_hours is None:
        reason_codes.append("missing_field:fold_duration_hours")
    if test_neg_pos_ratio is None:
        reason_codes.append("missing_field:test_neg_pos_ratio")

    if t_feasible_size <= 0:
        reason_codes.append(f"t_feasible_empty:{fold_id}")
    elif t_feasible_size < min_t_feasible_size:
        reason_codes.append(f"t_feasible_insufficient:{fold_id}")

    tail_support_sufficient = (
        positive_count is not None and positive_count >= int(min_tail_positive_count)
    )
    if not tail_support_sufficient:
        reason_codes.append(f"tail_support_insufficient:{fold_id}")

    return FoldEvidence(
        fold_id=fold_id,
        positive_count=positive_count,
        finalized_tp_count=finalized_tp_count,
        rated_bet_count=rated_bet_count,
        fold_duration_hours=fold_duration_hours,
        t_feasible_size=t_feasible_size,
        tail_support_sufficient=tail_support_sufficient,
        reason_codes=reason_codes,
        test_neg_pos_ratio=test_neg_pos_ratio,
    )


def _aggregate_test_neg_pos_ratio(folds: Sequence[FoldEvidence]) -> Optional[float]:
    weighted_sum = 0.0
    total_weight = 0.0
    for fold in folds:
        if fold.test_neg_pos_ratio is None:
            continue
        if fold.rated_bet_count is not None and fold.rated_bet_count > 0:
            weight = float(fold.rated_bet_count)
        else:
            weight = 1.0
        weighted_sum += float(fold.test_neg_pos_ratio) * weight
        total_weight += weight
    if total_weight <= 0.0:
        return None
    return float(weighted_sum / total_weight)


def _aggregate_blocking_reasons(
    folds: Sequence[FoldEvidence],
    production_neg_pos_ratio: Optional[float],
) -> List[str]:
    reasons: List[str] = []
    for fold in folds:
        reasons.extend(fold.reason_codes)

    if production_neg_pos_ratio is None or production_neg_pos_ratio <= 0:
        reasons.append("prod_ratio_unstable")

    # stable order + dedup
    deduped: List[str] = []
    seen = set()
    for reason in reasons:
        if reason not in seen:
            deduped.append(reason)
            seen.add(reason)
    return deduped


def _decide_objective(
    *,
    blocking_reasons: Sequence[str],
    override: Optional[str],
) -> Tuple[str, bool]:
    if override is not None and override == "single_constrained" and blocking_reasons:
        objective_decision = "composite"
    elif override is not None:
        objective_decision = override
    else:
        objective_decision = (
            "single_constrained" if len(blocking_reasons) == 0 else "composite"
        )
    single_allowed = objective_decision == "single_constrained" and len(blocking_reasons) == 0
    return objective_decision, single_allowed


def _build_output_json(
    *,
    run_id: str,
    start_ts: str,
    end_ts: str,
    timezone: str,
    selection_mode: str,
    production_neg_pos_ratio: Optional[float],
    production_neg_pos_ratio_source: str,
    production_neg_pos_ratio_sensitivity: str,
    folds: Sequence[FoldEvidence],
    min_t_feasible_size: int,
    objective_decision: str,
    single_objective_allowed: bool,
    blocking_reasons: Sequence[str],
) -> Dict[str, Any]:
    t_feasible_sizes = [f.t_feasible_size for f in folds]
    insufficient_fold_count = int(
        sum(1 for s in t_feasible_sizes if s < int(min_t_feasible_size) and s > 0)
    )
    empty_fold_count = int(sum(1 for s in t_feasible_sizes if s <= 0))

    fold_stats: List[Dict[str, Any]] = []
    for fold in folds:
        fold_stats.append(
            {
                "fold_id": fold.fold_id,
                "positive_count": fold.positive_count,
                "finalized_tp_count": fold.finalized_tp_count,
                "rated_bet_count": fold.rated_bet_count,
                "fold_duration_hours": fold.fold_duration_hours,
                "t_feasible_size": fold.t_feasible_size,
                "tail_support_sufficient": fold.tail_support_sufficient,
                "reason_codes": fold.reason_codes,
            }
        )

    return {
        "schema_version": "field-test-objective-precondition-v1",
        "run_id": run_id,
        "selection_mode": selection_mode,
        "objective_decision": objective_decision,
        "single_objective_allowed": single_objective_allowed,
        "window": {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "timezone": timezone,
        },
        "test_neg_pos_ratio": _aggregate_test_neg_pos_ratio(folds),
        "production_neg_pos_ratio_assumption": {
            "value": production_neg_pos_ratio,
            "source": production_neg_pos_ratio_source,
            "sensitivity_summary": production_neg_pos_ratio_sensitivity,
        },
        "t_feasible_stats": {
            "fold_count": len(folds),
            "insufficient_fold_count": insufficient_fold_count,
            "empty_fold_count": empty_fold_count,
        },
        "fold_stats": fold_stats,
        "blocking_reasons": list(blocking_reasons),
        "allowed_reason_codes": list(ALLOWED_REASON_CODES),
    }


def _build_markdown(data: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Field-test objective precondition check")
    lines.append("")
    lines.append(f"- run_id: `{data['run_id']}`")
    lines.append(f"- selection_mode: `{data['selection_mode']}`")
    lines.append(f"- objective_decision: `{data['objective_decision']}`")
    lines.append(f"- single_objective_allowed: `{data['single_objective_allowed']}`")
    lines.append("")
    lines.append("## Window")
    lines.append("")
    window = data["window"]
    lines.append(f"- start_ts: `{window['start_ts']}`")
    lines.append(f"- end_ts: `{window['end_ts']}`")
    lines.append(f"- timezone: `{window['timezone']}`")
    lines.append("")
    lines.append("## t_feasible summary")
    lines.append("")
    tfs = data["t_feasible_stats"]
    lines.append(f"- fold_count: `{tfs['fold_count']}`")
    lines.append(f"- insufficient_fold_count: `{tfs['insufficient_fold_count']}`")
    lines.append(f"- empty_fold_count: `{tfs['empty_fold_count']}`")
    lines.append("")
    lines.append("## Fold evidence")
    lines.append("")
    lines.append("| fold_id | positive_count | finalized_tp_count | rated_bet_count | fold_duration_hours | t_feasible_size | tail_support_sufficient | reason_codes |")
    lines.append("|---|---:|---:|---:|---:|---:|:---:|---|")
    for fold in data["fold_stats"]:
        reason_text = ", ".join(fold["reason_codes"]) if fold["reason_codes"] else "-"
        lines.append(
            "| {fold_id} | {positive_count} | {finalized_tp_count} | {rated_bet_count} | "
            "{fold_duration_hours} | {t_feasible_size} | {tail_support_sufficient} | {reason_text} |".format(
                fold_id=fold["fold_id"],
                positive_count=fold["positive_count"],
                finalized_tp_count=fold["finalized_tp_count"],
                rated_bet_count=fold["rated_bet_count"],
                fold_duration_hours=fold["fold_duration_hours"],
                t_feasible_size=fold["t_feasible_size"],
                tail_support_sufficient=str(fold["tail_support_sufficient"]).lower(),
                reason_text=reason_text,
            )
        )
    lines.append("")
    lines.append("## Blocking reasons")
    lines.append("")
    if data["blocking_reasons"]:
        for reason in data["blocking_reasons"]:
            lines.append(f"- `{reason}`")
    else:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    fold_paths = [Path(p) for p in args.fold_metrics_jsons]
    folds: List[FoldEvidence] = []
    for i, path in enumerate(fold_paths, start=1):
        blob = _read_json(path)
        fold_id = f"fold_{i}"
        fold = _extract_fold_evidence(
            fold_id=fold_id,
            data=blob,
            min_t_feasible_size=int(args.min_t_feasible_size),
            min_tail_positive_count=int(args.min_tail_positive_count),
        )
        folds.append(fold)

    blocking_reasons = _aggregate_blocking_reasons(
        folds, production_neg_pos_ratio=args.production_neg_pos_ratio
    )
    objective_decision, single_objective_allowed = _decide_objective(
        blocking_reasons=blocking_reasons,
        override=args.objective_decision_override,
    )

    output = _build_output_json(
        run_id=args.run_id,
        start_ts=args.start_ts,
        end_ts=args.end_ts,
        timezone=args.timezone,
        selection_mode=args.selection_mode,
        production_neg_pos_ratio=args.production_neg_pos_ratio,
        production_neg_pos_ratio_source=args.production_neg_pos_ratio_source,
        production_neg_pos_ratio_sensitivity=args.production_neg_pos_ratio_sensitivity,
        folds=folds,
        min_t_feasible_size=int(args.min_t_feasible_size),
        objective_decision=objective_decision,
        single_objective_allowed=single_objective_allowed,
        blocking_reasons=blocking_reasons,
    )

    output_json_path = Path(args.output_json)
    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    output_json_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    output_md_path = Path(args.output_md)
    output_md_path.parent.mkdir(parents=True, exist_ok=True)
    output_md_path.write_text(_build_markdown(output), encoding="utf-8")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

