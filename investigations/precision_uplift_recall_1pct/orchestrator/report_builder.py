"""Render phase1/*.md reports — MVP T6."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

_ORCH_NOTE_START = "<!-- ORCHESTRATOR_RUN_NOTE_START -->\n"
_ORCH_NOTE_END = "\n<!-- ORCHESTRATOR_RUN_NOTE_END -->"
_JSON_FENCE_MAX = 12000


def _r1_final_payload(bundle: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the R1/R6 final JSON object when present."""
    blk = bundle.get("r1_r6_final")
    if not isinstance(blk, Mapping):
        return None
    p = blk.get("payload")
    return dict(p) if isinstance(p, Mapping) else None


def _meta_section(run_id: str, cfg: Mapping[str, Any], bundle: Mapping[str, Any]) -> str:
    """Shared metadata block for all phase1 artifacts."""
    window = bundle.get("window") if isinstance(bundle.get("window"), Mapping) else {}
    rel_bundle = (
        f"investigations/precision_uplift_recall_1pct/orchestrator/state/{run_id}/collect_bundle.json"
    )
    return (
        "## Run metadata (orchestrator)\n\n"
        f"- **run_id**: `{run_id}`\n"
        f"- **Window**: `{window.get('start_ts')}` → `{window.get('end_ts')}`\n"
        f"- **model_dir**: `{cfg.get('model_dir')}`\n"
        f"- **state_db_path**: `{cfg.get('state_db_path')}`\n"
        f"- **prediction_log_db_path**: `{cfg.get('prediction_log_db_path')}`\n"
        f"- **collect_bundle**: `{rel_bundle}`\n\n"
    )


def _json_fence(data: Any, *, max_chars: int = _JSON_FENCE_MAX) -> str:
    """Serialize data inside a fenced json code block, truncating if huge."""
    raw = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "\n... (truncated for file size)"
    return f"```json\n{raw}\n```\n"


def _write_upper_bound_repro(path: Path, run_id: str, cfg: Mapping[str, Any], bundle: Mapping[str, Any]) -> Path:
    """Write ``upper_bound_repro.md`` from backtest metrics + training baseline hints."""
    metrics = bundle.get("backtest_metrics")
    r1 = _r1_final_payload(bundle)
    baseline = r1.get("training_artifact_baseline") if r1 else None
    lines = [
        "# upper_bound_repro",
        "",
        _meta_section(run_id, cfg, bundle),
        "## Offline / backtest snapshot",
        "",
    ]
    if metrics is None:
        lines.append("*No `backtest_metrics` in collect bundle (see collector errors).*")
    else:
        lines.append(_json_fence(metrics))
    lines.extend(["", "## Training artifact baseline (from R1 payload)", ""])
    if baseline is None:
        lines.append("*No `training_artifact_baseline` in R1 final payload.*")
    else:
        lines.append(_json_fence(baseline, max_chars=6000))
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_label_noise_audit(path: Path, run_id: str, cfg: Mapping[str, Any], bundle: Mapping[str, Any]) -> Path:
    """Write ``label_noise_audit.md`` from R1 unified evaluation + branches."""
    r1 = _r1_final_payload(bundle)
    uni = r1.get("unified_sample_evaluation") if r1 else None
    lines = ["# label_noise_audit", "", _meta_section(run_id, cfg, bundle), "## Unified sample (R1/R6)", ""]
    if not isinstance(uni, Mapping):
        lines.append("*No `unified_sample_evaluation` in R1 final payload.*")
    else:
        lines.append(_json_fence({k: uni[k] for k in uni if k != "by_model_version"}, max_chars=8000))
        bv = uni.get("by_model_version")
        if isinstance(bv, Mapping) and bv:
            lines.extend(["", "## By model_version (head)", "", _json_fence(bv, max_chars=4000)])
    lines.extend(["", "## Full R1 final payload (reference)", ""])
    lines.append(_json_fence(r1 if r1 else {}, max_chars=4000))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_slice_performance_report(path: Path, run_id: str, cfg: Mapping[str, Any], bundle: Mapping[str, Any]) -> Path:
    """Write ``slice_performance_report.md`` from DB stats + R2 cross-check."""
    stats = bundle.get("state_db_stats") if isinstance(bundle.get("state_db_stats"), Mapping) else {}
    errs = bundle.get("errors") or []
    r1 = _r1_final_payload(bundle)
    r2 = r1.get("r2_prediction_log_vs_alerts") if r1 else None
    lines = [
        "# slice_performance_report",
        "",
        _meta_section(run_id, cfg, bundle),
        "## validation_results aggregates (state DB)",
        "",
        _json_fence(dict(stats)),
        "",
        "## R2 prediction_log vs alerts",
        "",
    ]
    if r2 is None:
        lines.append("*No `r2_prediction_log_vs_alerts` in R1 final payload.*")
    else:
        lines.append(_json_fence(r2))
    lines.extend(["", "## Collector errors (if any)", ""])
    lines.append(_json_fence(list(errs)) if errs else "*None.*")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_point_in_time_parity(path: Path, run_id: str, cfg: Mapping[str, Any], bundle: Mapping[str, Any]) -> Path:
    """Write ``point_in_time_parity_check.md`` (MVP: data-source + manual checklist)."""
    lines = [
        "# point_in_time_parity_check",
        "",
        _meta_section(run_id, cfg, bundle),
        "## MVP 範圍（scaffold）",
        "",
        "本段由 orchestrator 產生；請人工核對：",
        "",
        "- Scorer `scored_at` 與 bet 延遲 / 桌台政策",
        "- Validator `validated_at` 與標籤成熟 / censored 規則",
        "- 與 R1/R6 觀測窗同一時區契約（runbook：HKT）",
        "",
        "## 資料來源路徑（供 reviewer）",
        "",
        f"- Prediction log DB: `{cfg.get('prediction_log_db_path')}`",
        f"- State DB: `{cfg.get('state_db_path')}`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_phase1_gate_decision(
    path: Path, run_id: str, cfg: Mapping[str, Any], bundle: Mapping[str, Any], gate: Mapping[str, Any]
) -> Path:
    """Write ``phase1_gate_decision.md`` from gate evaluator output."""
    status = str(gate.get("status", "UNKNOWN"))
    reasons = gate.get("blocking_reasons") or []
    evidence = str(gate.get("evidence_summary", ""))
    metrics = gate.get("metrics")
    lines = [
        "# phase1_gate_decision",
        "",
        _meta_section(run_id, cfg, bundle),
        "## Gate 結論 (orchestrator)",
        "",
        f"- **status**: `{status}`",
        "",
        "### blocking_reasons",
        "",
    ]
    if reasons:
        lines.extend([f"- `{r}`" for r in reasons])
    else:
        lines.append("- *(none)*")
    lines.extend(["", "### evidence_summary", "", evidence or "*empty*", ""])
    lines.extend(["### metrics", "", _json_fence(metrics if metrics is not None else {})])
    lines.extend(
        [
            "",
            "## 人工維護區（下方可續寫）",
            "",
            "- 與 `slice_performance_report.md`、`label_noise_audit.md` 等交叉比對後補主因與行動項。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _merge_status_history_crosscheck(path: Path, body: str) -> None:
    """Append or replace orchestrator-delimited note without clobbering manual text."""
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
    else:
        existing = (
            "# status_history_crosscheck\n\n"
            "> 人工維護主體；orchestrator 僅更新標記區塊。\n\n"
        )
    replacement = f"{_ORCH_NOTE_START}{body}{_ORCH_NOTE_END}"
    if _ORCH_NOTE_START in existing and _ORCH_NOTE_END in existing:
        head, _, rest = existing.partition(_ORCH_NOTE_START)
        _, _, tail = rest.partition(_ORCH_NOTE_END)
        merged = head + replacement + tail
    else:
        merged = existing.rstrip() + "\n\n" + replacement + "\n"
    path.write_text(merged, encoding="utf-8")


def _write_status_history_crosscheck(path: Path, run_id: str, cfg: Mapping[str, Any], gate: Mapping[str, Any]) -> Path:
    """Update ``status_history_crosscheck.md`` orchestrator block only."""
    body = (
        f"**Last orchestrator run**: `{run_id}`\n\n"
        f"- **Gate status**: `{gate.get('status')}`\n"
        f"- **blocking_reasons**: `{gate.get('blocking_reasons')}`\n"
        "- Please keep narrative cross-check above this block; edit conflicts rare.\n"
    )
    _merge_status_history_crosscheck(path, body)
    return path


def write_phase1_reports(
    phase1_dir: Path,
    run_id: str,
    config: dict[str, Any],
    bundle: dict[str, Any],
    gate: dict[str, Any],
) -> list[Path]:
    """Write or update Phase 1 markdown artifacts under ``phase1_dir``.

    Args:
        phase1_dir: Directory for investigation phase1 markdown files.
        run_id: Current run id.
        config: Validated orchestrator config.
        bundle: Collector output.
        gate: Gate evaluator output.

    Returns:
        List of written artifact paths.
    """
    phase1_dir = phase1_dir.resolve()
    phase1_dir.mkdir(parents=True, exist_ok=True)
    cfg_m: Mapping[str, Any] = config
    bundle_m: Mapping[str, Any] = bundle
    gate_m: Mapping[str, Any] = gate
    written: list[Path] = []
    written.append(_write_upper_bound_repro(phase1_dir / "upper_bound_repro.md", run_id, cfg_m, bundle_m))
    written.append(_write_label_noise_audit(phase1_dir / "label_noise_audit.md", run_id, cfg_m, bundle_m))
    written.append(_write_slice_performance_report(phase1_dir / "slice_performance_report.md", run_id, cfg_m, bundle_m))
    written.append(_write_point_in_time_parity(phase1_dir / "point_in_time_parity_check.md", run_id, cfg_m, bundle_m))
    written.append(_write_phase1_gate_decision(phase1_dir / "phase1_gate_decision.md", run_id, cfg_m, bundle_m, gate_m))
    written.append(_write_status_history_crosscheck(phase1_dir / "status_history_crosscheck.md", run_id, cfg_m, gate_m))
    return written


# Fix typo: _ORNS should not exist - I made a bug in partition
