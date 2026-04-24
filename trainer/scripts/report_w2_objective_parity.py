"""Build W2 multi-window objective parity report from per-run artifacts.

Inputs are run directories that contain:
- ``training_metrics.v2.json`` (preferred) and/or ``training_metrics.json`` (required if v2 absent)
- ``backtest_metrics.json`` (optional; row kept with null backtest fields)

Outputs:
- CSV (machine-readable row table)
- Markdown (human summary + frozen field mapping snapshot)
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Optional

from trainer.core.training_metrics_bundle import load_training_metrics_merged


@dataclass(frozen=True)
class RunRow:
    run_id: str
    run_dir: str
    selection_mode_train: Optional[str]
    selection_mode_backtest: Optional[str]
    objective_mode: Optional[str]
    hpo_best_value: Optional[float]
    train_test_precision: Optional[float]
    train_test_precision_prod_adjusted: Optional[float]
    train_test_recall: Optional[float]
    bt_model_default_test_ap: Optional[float]
    bt_model_default_test_precision: Optional[float]
    bt_model_default_test_precision_prod_adjusted: Optional[float]
    bt_model_default_test_recall: Optional[float]
    bt_model_default_alerts_per_hour: Optional[float]
    bt_model_default_test_precision_at_recall_0_01: Optional[float]
    bt_model_default_test_precision_at_recall_0_01_prod_adjusted: Optional[float]
    bt_optuna_test_ap: Optional[float]
    bt_optuna_test_precision: Optional[float]
    bt_optuna_test_precision_prod_adjusted: Optional[float]
    bt_optuna_test_recall: Optional[float]
    bt_optuna_alerts_per_hour: Optional[float]
    bt_optuna_test_precision_at_recall_0_01: Optional[float]
    bt_optuna_test_precision_at_recall_0_01_prod_adjusted: Optional[float]


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN guard without numpy
        return None
    return f


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return payload


def _section(d: dict[str, Any], key: str) -> dict[str, Any]:
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def row_from_run_dir(run_dir: Path) -> RunRow:
    tm_path = run_dir / "training_metrics.json"
    v2_path = run_dir / "training_metrics.v2.json"
    bt_path = run_dir / "backtest_metrics.json"
    if not tm_path.is_file() and not v2_path.is_file():
        raise FileNotFoundError(
            f"Missing training_metrics.v2.json and training_metrics.json under {run_dir}"
        )
    _src, tm = load_training_metrics_merged(run_dir)
    if not tm:
        raise FileNotFoundError(f"Unreadable or empty training metrics under {run_dir}")
    bt = _read_json(bt_path) if bt_path.is_file() else {}
    bt_md = _section(bt, "model_default")
    bt_opt = _section(bt, "optuna")
    run_id = str(tm.get("run_id") or tm.get("model_version") or run_dir.name)

    return RunRow(
        run_id=run_id,
        run_dir=str(run_dir.resolve()),
        selection_mode_train=(str(tm.get("selection_mode")).strip() if tm.get("selection_mode") is not None else None),
        selection_mode_backtest=(str(bt.get("selection_mode")).strip() if bt.get("selection_mode") is not None else None),
        objective_mode=(str(tm.get("optuna_hpo_objective_mode")).strip() if tm.get("optuna_hpo_objective_mode") is not None else None),
        hpo_best_value=_safe_float(tm.get("optuna_hpo_study_best_trial_value")),
        train_test_precision=_safe_float(tm.get("test_precision")),
        train_test_precision_prod_adjusted=_safe_float(tm.get("test_precision_prod_adjusted")),
        train_test_recall=_safe_float(tm.get("test_recall")),
        bt_model_default_test_ap=_safe_float(bt_md.get("test_ap")),
        bt_model_default_test_precision=_safe_float(bt_md.get("test_precision")),
        bt_model_default_test_precision_prod_adjusted=_safe_float(bt_md.get("test_precision_prod_adjusted")),
        bt_model_default_test_recall=_safe_float(bt_md.get("test_recall")),
        bt_model_default_alerts_per_hour=_safe_float(bt_md.get("alerts_per_hour")),
        bt_model_default_test_precision_at_recall_0_01=_safe_float(bt_md.get("test_precision_at_recall_0.01")),
        bt_model_default_test_precision_at_recall_0_01_prod_adjusted=_safe_float(
            bt_md.get("test_precision_at_recall_0.01_prod_adjusted")
        ),
        bt_optuna_test_ap=_safe_float(bt_opt.get("test_ap")),
        bt_optuna_test_precision=_safe_float(bt_opt.get("test_precision")),
        bt_optuna_test_precision_prod_adjusted=_safe_float(bt_opt.get("test_precision_prod_adjusted")),
        bt_optuna_test_recall=_safe_float(bt_opt.get("test_recall")),
        bt_optuna_alerts_per_hour=_safe_float(bt_opt.get("alerts_per_hour")),
        bt_optuna_test_precision_at_recall_0_01=_safe_float(bt_opt.get("test_precision_at_recall_0.01")),
        bt_optuna_test_precision_at_recall_0_01_prod_adjusted=_safe_float(
            bt_opt.get("test_precision_at_recall_0.01_prod_adjusted")
        ),
    )


def _csv_headers() -> list[str]:
    return list(RunRow.__annotations__.keys())


def write_csv(rows: list[RunRow], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_csv_headers())
        w.writeheader()
        for r in rows:
            w.writerow(r.__dict__)


def _mean_or_none(vals: list[Optional[float]]) -> Optional[float]:
    xs = [v for v in vals if v is not None]
    return mean(xs) if xs else None


def build_markdown(rows: list[RunRow]) -> str:
    lines: list[str] = ["# W2 Objective Parity Report", ""]
    lines.append(f"- windows (rows): `{len(rows)}`")
    lines.append("")
    modes: dict[str, list[RunRow]] = {}
    for r in rows:
        k = r.objective_mode or "unknown"
        modes.setdefault(k, []).append(r)

    lines.append("## Objective Group Summary")
    lines.append("")
    lines.append("| objective_mode | rows | mean_bt_optuna_precision_prod_adjusted | mean_bt_optuna_recall |")
    lines.append("| :--- | ---: | ---: | ---: |")
    for mode, rs in sorted(modes.items(), key=lambda x: x[0]):
        m_prec = _mean_or_none([x.bt_optuna_test_precision_prod_adjusted for x in rs])
        m_rec = _mean_or_none([x.bt_optuna_test_recall for x in rs])
        lines.append(
            f"| `{mode}` | {len(rs)} | "
            f"{('%.6f' % m_prec) if m_prec is not None else 'NA'} | "
            f"{('%.6f' % m_rec) if m_rec is not None else 'NA'} |"
        )
    lines.append("")
    lines.append("## Frozen Field Mapping Snapshot")
    lines.append("")
    lines.append("| contract field | training_metrics key | backtest_metrics key |")
    lines.append("| :--- | :--- | :--- |")
    lines.append("| selection_mode | `selection_mode` | top-level `selection_mode` |")
    lines.append("| objective mode | `optuna_hpo_objective_mode` | N/A |")
    lines.append("| precision_raw | `test_precision` | `model_default.test_precision` / `optuna.test_precision` |")
    lines.append(
        "| precision_prod_adjusted | `test_precision_prod_adjusted` | "
        "`model_default.test_precision_prod_adjusted` / `optuna.test_precision_prod_adjusted` |"
    )
    lines.append("| recall | `test_recall` | `model_default.test_recall` / `optuna.test_recall` |")
    lines.append("| alerts_per_hour | N/A | `model_default.alerts_per_hour` / `optuna.alerts_per_hour` |")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_markdown(rows: list[RunRow], out_md: Path) -> None:
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(build_markdown(rows), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build W2 AP vs field-test parity report across multiple windows.")
    p.add_argument(
        "--run-dir",
        action="append",
        default=[],
        help="Run directory containing training_metrics.v2.json and/or training_metrics.json (required) and backtest_metrics.json (optional). Repeatable.",
    )
    p.add_argument(
        "--run-dir-glob",
        action="append",
        default=[],
        help="Glob pattern for run directories (e.g. out/phase2/*/). Repeatable.",
    )
    p.add_argument("--output-csv", required=True, help="Output CSV path.")
    p.add_argument("--output-md", required=True, help="Output Markdown path.")
    return p.parse_args()


def _resolve_run_dirs(args: argparse.Namespace) -> list[Path]:
    out: list[Path] = []
    for raw in args.run_dir:
        s = str(raw).strip()
        if s:
            out.append(Path(s))
    for pat in args.run_dir_glob:
        s = str(pat).strip()
        if not s:
            continue
        for p in sorted(Path(".").glob(s)):
            if p.is_dir():
                out.append(p)
    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        k = str(p.resolve())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def main() -> int:
    args = parse_args()
    run_dirs = _resolve_run_dirs(args)
    if not run_dirs:
        raise SystemExit("No run directories. Use --run-dir and/or --run-dir-glob.")
    rows: list[RunRow] = []
    for d in run_dirs:
        rows.append(row_from_run_dir(d))
    write_csv(rows, Path(args.output_csv))
    write_markdown(rows, Path(args.output_md))
    print(f"wrote {len(rows)} rows -> {Path(args.output_csv).resolve()}")
    print(f"wrote markdown -> {Path(args.output_md).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
