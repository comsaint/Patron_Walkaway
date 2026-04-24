"""Build W1 freeze evidence package for DEC-043.

This script is lightweight by design:
- It reads existing artifacts only (no model training/backtest execution).
- It emits machine-readable + markdown evidence bundles.
- It tolerates missing optional artifacts so laptop/offline workflows still work.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from trainer.core.training_metrics_bundle import load_training_metrics_merged


DEC043_ALLOWED_REASON_CODES: tuple[str, ...] = (
    "empty_subset",
    "single_class",
    "invalid_input_nan",
    "infeasible_constraint",
    "missing_required_column",
)


@dataclass(frozen=True)
class RunEvidenceRow:
    run_id: str
    run_dir: str
    has_training_metrics: bool
    has_backtest_metrics: bool
    selection_mode_training: Optional[str]
    selection_mode_backtest: Optional[str]
    objective_mode: Optional[str]
    gate_blocked_reason_code: Optional[str]


def _safe_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _load_json_object(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return payload


def _get_nested_map(blob: dict[str, Any], key: str) -> dict[str, Any]:
    v = blob.get(key)
    return v if isinstance(v, dict) else {}


def _collect_reason_codes_from_metrics(metrics_obj: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key, value in metrics_obj.items():
        if key.endswith("_reason_code"):
            code = _safe_text(value)
            if code is not None:
                out.append(code)
    return out


def _row_from_run_dir(run_dir: Path) -> tuple[RunEvidenceRow, list[str]]:
    tm_path = run_dir / "training_metrics.json"
    v2_path = run_dir / "training_metrics.v2.json"
    bt_path = run_dir / "backtest_metrics.json"
    if tm_path.is_file() or v2_path.is_file():
        _src, tm = load_training_metrics_merged(run_dir)
    else:
        tm = {}
    bt = _load_json_object(bt_path) if bt_path.is_file() else {}
    run_id = _safe_text(tm.get("run_id")) or _safe_text(tm.get("model_version")) or run_dir.name
    reason_codes: list[str] = []

    gate_blocked_reason_code = _safe_text(tm.get("optuna_hpo_gate_blocked_reason_code"))
    if gate_blocked_reason_code is not None:
        reason_codes.append(gate_blocked_reason_code)

    for section_name in ("model_default", "optuna"):
        reason_codes.extend(_collect_reason_codes_from_metrics(_get_nested_map(bt, section_name)))

    return (
        RunEvidenceRow(
            run_id=run_id,
            run_dir=str(run_dir.resolve()),
            has_training_metrics=bool(tm_path.is_file() or v2_path.is_file()),
            has_backtest_metrics=bt_path.is_file(),
            selection_mode_training=_safe_text(tm.get("selection_mode")),
            selection_mode_backtest=_safe_text(bt.get("selection_mode")),
            objective_mode=_safe_text(tm.get("optuna_hpo_objective_mode")),
            gate_blocked_reason_code=gate_blocked_reason_code,
        ),
        reason_codes,
    )


def _resolve_run_dirs(args: argparse.Namespace) -> list[Path]:
    run_dirs: list[Path] = []
    for raw in args.run_dir:
        s = str(raw).strip()
        if s:
            run_dirs.append(Path(s))
    for raw in args.run_dir_glob:
        s = str(raw).strip()
        if not s:
            continue
        for p in sorted(Path(".").glob(s)):
            if p.is_dir():
                run_dirs.append(p)
    uniq: list[Path] = []
    seen: set[str] = set()
    for p in run_dirs:
        resolved = str(p.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        uniq.append(p)
    return uniq


def _build_contract_checks(
    *,
    rows: Sequence[RunEvidenceRow],
    precondition_payload: Optional[dict[str, Any]],
    observed_reason_codes: Sequence[str],
) -> list[dict[str, Any]]:
    statuses_selection_train = {
        row.selection_mode_training for row in rows if row.selection_mode_training is not None
    }
    statuses_selection_bt = {
        row.selection_mode_backtest for row in rows if row.selection_mode_backtest is not None
    }
    objective_modes = {
        row.objective_mode for row in rows if row.objective_mode is not None
    }
    precondition_decision = None
    precondition_allowed_reason_codes: list[str] = []
    if isinstance(precondition_payload, dict):
        precondition_decision = _safe_text(precondition_payload.get("objective_decision"))
        payload_codes = precondition_payload.get("allowed_reason_codes")
        if isinstance(payload_codes, list):
            precondition_allowed_reason_codes = [
                c for c in (_safe_text(v) for v in payload_codes) if c is not None
            ]

    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "check_id": "dec043_selection_mode_field_test",
            "expected": "selection_mode=field_test",
            "observed": {
                "training_modes": sorted(statuses_selection_train),
                "backtest_modes": sorted(statuses_selection_bt),
            },
            "status": (
                "pass"
                if (not statuses_selection_train or statuses_selection_train == {"field_test"})
                and (not statuses_selection_bt or statuses_selection_bt == {"field_test"})
                else "warn"
            ),
        }
    )
    checks.append(
        {
            "check_id": "dec043_precondition_blocked_semantics",
            "expected": "objective_decision in precondition should be BLOCKED or single_constrained",
            "observed": {"precondition_objective_decision": precondition_decision},
            "status": (
                "pass"
                if precondition_decision in (None, "BLOCKED", "single_constrained")
                else "warn"
            ),
        }
    )
    checks.append(
        {
            "check_id": "dec043_no_ap_fallback_signal",
            "expected": "objective mode should not silently fallback to AP",
            "observed": {"objective_modes": sorted(objective_modes)},
            "status": (
                "pass"
                if not any(m == "validation_ap" for m in objective_modes)
                else "warn"
            ),
        }
    )
    checks.append(
        {
            "check_id": "dec043_reason_code_enum_freeze",
            "expected": list(DEC043_ALLOWED_REASON_CODES),
            "observed": {
                "precondition_allowed_reason_codes": precondition_allowed_reason_codes,
                "observed_reason_codes": sorted(set(observed_reason_codes)),
            },
            "status": (
                "pass"
                if all(c in DEC043_ALLOWED_REASON_CODES for c in observed_reason_codes)
                and (
                    not precondition_allowed_reason_codes
                    or set(precondition_allowed_reason_codes) == set(DEC043_ALLOWED_REASON_CODES)
                )
                else "warn"
            ),
        }
    )
    return checks


def _build_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# W1 Freeze Evidence Package")
    lines.append("")
    lines.append(f"- decision_id: `{payload['decision_id']}`")
    lines.append(f"- generated_at_utc: `{payload['generated_at_utc']}`")
    lines.append(f"- run_rows: `{payload['summary']['run_row_count']}`")
    lines.append("")

    lines.append("## Contract Checks")
    lines.append("")
    lines.append("| check_id | status | expected | observed |")
    lines.append("| :--- | :--- | :--- | :--- |")
    for check in payload["contract_checks"]:
        lines.append(
            f"| `{check['check_id']}` | `{check['status']}` | "
            f"{json.dumps(check['expected'], ensure_ascii=False)} | "
            f"{json.dumps(check['observed'], ensure_ascii=False)} |"
        )
    lines.append("")

    lines.append("## Reason Code Evidence")
    lines.append("")
    lines.append(f"- allowed_reason_codes: `{payload['reason_code_evidence']['allowed_reason_codes']}`")
    lines.append(
        f"- observed_reason_codes: `{payload['reason_code_evidence']['observed_reason_codes']}`"
    )
    lines.append(
        f"- unknown_reason_codes: `{payload['reason_code_evidence']['unknown_reason_codes']}`"
    )
    lines.append("")

    lines.append("## Run Rows")
    lines.append("")
    lines.append(
        "| run_id | has_training_metrics | has_backtest_metrics | selection_mode_training | "
        "selection_mode_backtest | objective_mode | gate_blocked_reason_code |"
    )
    lines.append("| :--- | :---: | :---: | :--- | :--- | :--- | :--- |")
    for row in payload["run_rows"]:
        lines.append(
            f"| `{row['run_id']}` | `{row['has_training_metrics']}` | `{row['has_backtest_metrics']}` | "
            f"`{row['selection_mode_training']}` | `{row['selection_mode_backtest']}` | "
            f"`{row['objective_mode']}` | `{row['gate_blocked_reason_code']}` |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build W1 freeze evidence package for DEC-043."
    )
    parser.add_argument(
        "--decision-id",
        default="DEC-043",
        help="Decision id label for this evidence package (default: DEC-043).",
    )
    parser.add_argument(
        "--precondition-json",
        default="out/precision_uplift_field_test_objective/field_test_objective_precondition_check.json",
        help="Path to W1 precondition JSON (optional).",
    )
    parser.add_argument(
        "--run-dir",
        action="append",
        default=[],
        help="Run directory containing training_metrics.v2.json and/or training_metrics.json plus backtest_metrics.json. Repeatable.",
    )
    parser.add_argument(
        "--run-dir-glob",
        action="append",
        default=[],
        help="Glob pattern for run directories (e.g. out/models/*). Repeatable.",
    )
    parser.add_argument(
        "--output-json",
        default="out/precision_uplift_field_test_objective/w1_freeze_evidence.json",
        help="Output path for machine-readable evidence JSON.",
    )
    parser.add_argument(
        "--output-md",
        default="trainer/precision_improvement_plan/w1_freeze_evidence.md",
        help="Output path for human-readable evidence markdown.",
    )
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    run_dirs = _resolve_run_dirs(args)
    precondition_path = Path(str(args.precondition_json))
    precondition_payload: Optional[dict[str, Any]] = None
    if precondition_path.is_file():
        precondition_payload = _load_json_object(precondition_path)

    rows: list[RunEvidenceRow] = []
    observed_codes: list[str] = []
    for run_dir in run_dirs:
        row, reason_codes = _row_from_run_dir(run_dir)
        rows.append(row)
        observed_codes.extend(reason_codes)
    code_counter = Counter(observed_codes)
    unknown_codes = sorted(code for code in code_counter if code not in DEC043_ALLOWED_REASON_CODES)
    checks = _build_contract_checks(
        rows=rows,
        precondition_payload=precondition_payload,
        observed_reason_codes=observed_codes,
    )

    payload: dict[str, Any] = {
        "schema_version": "w1-freeze-evidence-v1",
        "decision_id": str(args.decision_id),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "precondition_json": str(precondition_path.resolve()),
            "run_dirs": [str(p.resolve()) for p in run_dirs],
        },
        "summary": {
            "run_row_count": len(rows),
            "runs_with_training_metrics": sum(1 for r in rows if r.has_training_metrics),
            "runs_with_backtest_metrics": sum(1 for r in rows if r.has_backtest_metrics),
        },
        "contract_checks": checks,
        "reason_code_evidence": {
            "allowed_reason_codes": list(DEC043_ALLOWED_REASON_CODES),
            "observed_reason_codes": sorted(code_counter.keys()),
            "observed_reason_code_counts": dict(sorted(code_counter.items(), key=lambda x: x[0])),
            "unknown_reason_codes": unknown_codes,
        },
        "run_rows": [row.__dict__ for row in rows],
    }

    out_json = Path(str(args.output_json))
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    out_md = Path(str(args.output_md))
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(_build_markdown(payload), encoding="utf-8")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
