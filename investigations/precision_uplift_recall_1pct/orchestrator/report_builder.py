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


def write_phase2_gate_decision(
    path: Path,
    run_id: str,
    cfg: Mapping[str, Any],
    bundle: Mapping[str, Any],
    gate: Mapping[str, Any],
) -> Path:
    """Write ``phase2_gate_decision.md`` under the investigation phase2 directory."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    common = cfg.get("common") if isinstance(cfg.get("common"), Mapping) else {}
    rel_bundle = (
        f"investigations/precision_uplift_recall_1pct/orchestrator/state/{run_id}/"
        "phase2_bundle.json"
    )
    lines = [
        "# phase2_gate_decision",
        "",
        "## Run metadata",
        "",
        f"- **run_id**: `{run_id}`",
        f"- **phase2_bundle**: `{rel_bundle}`",
        f"- **bundle status**: `{bundle.get('status')}`",
        f"- **model_dir**: `{common.get('model_dir')}`",
        "",
        "## Gate outcome",
        "",
        f"- **status**: **{gate.get('status')}**",
        "",
        "### Blocking reasons",
        "",
    ]
    br = gate.get("blocking_reasons") or []
    if isinstance(br, list) and br:
        for item in br:
            lines.append(f"- `{item}`")
    else:
        lines.append("- *(none)*")
    lines.extend(
        [
            "",
            "### Evidence summary",
            "",
            str(gate.get("evidence_summary") or ""),
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _phase2_harvest_markdown_for_track(bundle: Mapping[str, Any], tname: str) -> str:
    """Markdown list lines for ``job_training_harvest`` rows belonging to ``tname``."""
    jh = bundle.get("job_training_harvest")
    if not isinstance(jh, Mapping):
        return "- *(no ``job_training_harvest`` in bundle)*\n"
    rows = jh.get("rows")
    if not isinstance(rows, list) or not rows:
        return "- *(no harvest rows)*\n"
    sub = [
        r
        for r in rows
        if isinstance(r, Mapping) and str(r.get("track") or "").strip() == tname
    ]
    if not sub:
        return "- *(no rows for this track)*\n"
    lines: list[str] = []
    for r in sub:
        eid = str(r.get("exp_id") or "").strip() or "(unknown)"
        rel = str(r.get("metrics_relative") or "").strip()
        if r.get("found"):
            lines.append(f"- `{eid}`: **found** (`{rel}`)")
        else:
            err = str(r.get("load_error") or "missing")
            lines.append(f"- `{eid}`: **not found** ({err})")
    return "\n".join(lines) + "\n"


def _phase2_per_job_backtest_markdown_for_track(
    bundle: Mapping[str, Any], tname: str
) -> str:
    """Markdown list lines for ``per_job_backtest_jobs.results`` on ``tname``."""
    pjb = bundle.get("per_job_backtest_jobs")
    if not isinstance(pjb, Mapping):
        return "- *(no ``per_job_backtest_jobs`` in bundle)*\n"
    if not pjb.get("executed"):
        return (
            "- *(per-job backtests not run; pass ``--phase2-run-per-job-backtests``)*\n"
        )
    res = pjb.get("results")
    if not isinstance(res, list) or not res:
        return "- *(no per-job results)*\n"
    sub = [
        r
        for r in res
        if isinstance(r, Mapping) and str(r.get("track") or "").strip() == tname
    ]
    if not sub:
        return "- *(no rows for this track)*\n"
    lines: list[str] = []
    for r in sub:
        eid = str(r.get("exp_id") or "").strip() or "(unknown)"
        if r.get("skipped"):
            sr = str(r.get("skip_reason") or "skipped")
            lines.append(f"- `{eid}`: **skipped** ({sr})")
            continue
        ok = r.get("ok")
        pv_raw = r.get("shared_precision_at_recall_1pct_preview")
        if ok is True and pv_raw is not None:
            try:
                fv = float(pv_raw)
                lines.append(
                    f"- `{eid}`: PAT@1% preview **{fv:.4f}** (per-job backtest)"
                )
            except (TypeError, ValueError):
                lines.append(f"- `{eid}`: **ok** (preview unparsable)")
        elif ok is True:
            lines.append(f"- `{eid}`: **ok** (no PAT@1% preview in metrics file)")
        else:
            msg = str(r.get("message") or r.get("error_code") or "failed")
            lines.append(f"- `{eid}`: **failed** ({msg})")
    return "\n".join(lines) + "\n"


def _phase2_uplift_rows_markdown_for_track(gate: Mapping[str, Any], tname: str) -> str:
    """Markdown lines from ``gate['metrics']['phase2_uplift_rows']`` for one track."""
    gm = gate.get("metrics")
    if not isinstance(gm, Mapping) or not gm.get("phase2_uplift_gate_evaluated"):
        return (
            "- *(uplift gate not evaluated — needs ``metrics_ingested`` plus per-job "
            "backtests with previews)*\n"
        )
    rows = gm.get("phase2_uplift_rows")
    if not isinstance(rows, list):
        return "- *(no uplift rows)*\n"
    sub = [
        r
        for r in rows
        if isinstance(r, Mapping) and str(r.get("track") or "").strip() == tname
    ]
    if not sub:
        return "- *(no uplift rows for this track)*\n"
    lines: list[str] = []
    for r in sub:
        eid = str(r.get("exp_id") or "").strip()
        role = str(r.get("role") or "")
        if role == "baseline":
            pv = r.get("preview")
            try:
                fv = float(pv)
            except (TypeError, ValueError):
                fv = float("nan")
            lines.append(f"- `{eid}`: **baseline** PAT@1% preview={fv:.4f}")
            continue
        u = r.get("uplift_pp_vs_baseline")
        m = r.get("meets_min_uplift")
        be = str(r.get("baseline_exp_id") or "")
        try:
            fu = float(u)
        except (TypeError, ValueError):
            fu = float("nan")
        tag = "meets min" if m else "below min"
        lines.append(f"- `{eid}`: vs `{be}` uplift **{fu:+.2f} pp** ({tag})")
    return "\n".join(lines) + "\n"


def _format_phase2_pat_series_values(values: list[Any], *, max_elems: int = 8) -> str:
    """Format PAT@1% value lists for markdown; truncates long sequences."""
    parts: list[str] = []
    for x in values:
        try:
            parts.append(f"{float(x):.4f}")
        except (TypeError, ValueError):
            parts.append(str(x))
    if len(parts) <= max_elems:
        return "[" + ", ".join(parts) + "]"
    head = ", ".join(parts[:max_elems])
    return f"[{head}, … ({len(parts)} values)]"


def _phase2_std_and_pat_series_markdown_for_track(
    bundle: Mapping[str, Any],
    gate: Mapping[str, Any],
    tname: str,
) -> str:
    """Markdown body: bundle PAT series for this track + std gate metrics slice."""
    lines: list[str] = []
    root = bundle.get("phase2_pat_series_by_experiment")
    exp_map = root.get(tname) if isinstance(root, Mapping) else None
    lines.append("### Bundle series (this track)")
    lines.append("")
    if isinstance(exp_map, Mapping) and exp_map:
        for eid_raw in sorted(exp_map.keys(), key=lambda k: str(k)):
            eid = str(eid_raw).strip()
            raw_list = exp_map.get(eid_raw)
            if not isinstance(raw_list, list) or not raw_list:
                lines.append(f"- `{eid}`: *(non-list or empty)*")
                continue
            lines.append(
                f"- `{eid}`: n={len(raw_list)} {_format_phase2_pat_series_values(raw_list)}"
            )
    else:
        lines.append(
            "- *(no `phase2_pat_series_by_experiment` entries for this track)*"
        )
    lines.extend(["", "### Std gate (from evaluate_phase2_gate)", ""])
    gm = gate.get("metrics") if isinstance(gate.get("metrics"), Mapping) else {}
    note = gm.get("phase2_std_gate_note")
    if note:
        lines.append(f"- **note**: {note}")
    if gm.get("phase2_std_gate_evaluated") is True:
        mx = gm.get("phase2_std_max_pp_across_experiments")
        lim = gm.get("phase2_std_pp_limit")
        try:
            mx_f = float(mx) if mx is not None else float("nan")
            li_f = float(lim) if lim is not None else float("nan")
            lines.append(
                f"- **evaluated**: yes; **max sample stdev (pp, gate-wide)**: {mx_f:.4f}; "
                f"**limit (pp)**: {li_f:.4f}"
            )
        except (TypeError, ValueError):
            lines.append(
                f"- **evaluated**: yes; **max sample stdev (pp)**: {mx!r}; "
                f"**limit (pp)**: {lim!r}"
            )
        ps = gm.get("phase2_std_per_series")
        if isinstance(ps, list):
            sub = [
                x
                for x in ps
                if isinstance(x, Mapping)
                and str(x.get("track") or "").strip() == tname
            ]
            if sub:
                lines.append("- **This track (per-series)**:")
                for row in sub:
                    te = str(row.get("exp_id") or "")
                    nw = row.get("n_windows")
                    sp = row.get("std_pp")
                    lines.append(f"  - `{te}`: n_windows={nw}, std_pp={sp}")
            else:
                lines.append(
                    "- *(no `phase2_std_per_series` rows for this track)*"
                )
    else:
        lines.append(
            "- *(std gate not evaluated — e.g. `plan_only`, or uplift/std prerequisites "
            "missing)*"
        )
    lines.append("")
    return "\n".join(lines)


def write_phase2_track_results(
    phase2_dir: Path,
    run_id: str,
    cfg: Mapping[str, Any],
    bundle: Mapping[str, Any],
    gate: Mapping[str, Any],
) -> list[Path]:
    """Write ``phase2/track_{a,b,c}_results.md`` (T11; shared-backtest stub).

    Each file lists the track's declared experiments. Numeric rows duplicate the **shared**
    backtest PAT@1% until per-experiment artifacts are wired (T10+).

    Args:
        phase2_dir: Investigation ``phase2`` directory.
        run_id: Orchestrator run id.
        cfg: Validated phase2 config.
        bundle: Phase2 bundle (may include ``backtest_metrics``).
        gate: Output of ``evaluate_phase2_gate``.

    Returns:
        Paths written (existing files overwritten).
    """
    import evaluators as _evaluators

    phase2_dir = phase2_dir.resolve()
    phase2_dir.mkdir(parents=True, exist_ok=True)
    tracks = cfg.get("tracks") if isinstance(cfg.get("tracks"), Mapping) else {}
    pr1 = _evaluators.extract_phase2_shared_precision_at_recall_1pct(
        bundle.get("backtest_metrics")
        if isinstance(bundle.get("backtest_metrics"), Mapping)
        else None
    )
    gm = gate.get("metrics") if isinstance(gate.get("metrics"), Mapping) else {}
    pr1_gate = gm.get("shared_precision_at_recall_1pct")
    if pr1 is None and pr1_gate is not None:
        try:
            pr1 = float(pr1_gate)
        except (TypeError, ValueError):
            pr1 = None

    written: list[Path] = []
    for tname in ("track_a", "track_b", "track_c"):
        tnode = tracks.get(tname) if isinstance(tracks, Mapping) else None
        enabled = bool(tnode.get("enabled")) if isinstance(tnode, Mapping) else False
        exps = tnode.get("experiments") if isinstance(tnode, Mapping) else []
        exp_lines: list[str] = []
        if isinstance(exps, list):
            for ex in exps:
                if not isinstance(ex, Mapping):
                    continue
                eid = str(ex.get("exp_id") or "").strip()
                if eid:
                    exp_lines.append(f"- `{eid}`")
        exp_block = "\n".join(exp_lines) if exp_lines else "- *(none)*"
        pr_line = (
            f"{pr1:.4f}"
            if pr1 is not None
            else "*(not available in ingested backtest_metrics)*"
        )
        lines = [
            f"# Phase 2 — {tname} results",
            "",
            "## Run metadata",
            "",
            f"- **run_id**: `{run_id}`",
            f"- **bundle status**: `{bundle.get('status')}`",
            f"- **track enabled**: `{enabled}`",
            "",
            "## Experiments (YAML)",
            "",
            exp_block,
            "",
            "## Per-job training_metrics harvest",
            "",
            "> Harvest uses each job's optional ``training_metrics_repo_relative`` (YAML) "
            "when set, else ``{logs_subdir_relative}/training_metrics.json``.",
            "",
            _phase2_harvest_markdown_for_track(bundle, tname),
            "## Per-job backtest preview",
            "",
            "> One ``trainer.backtester`` run per ``job_spec`` with "
            "``training_metrics_repo_relative``; each job uses ``--output-dir`` under "
            "``…/logs/phase2/<track>/<exp_id>/_per_job_backtest/`` so "
            "``backtest_metrics.json`` is not overwritten by the next job (shared "
            "backtest still uses ``resources.backtest_metrics_path`` / default).",
            "",
            _phase2_per_job_backtest_markdown_for_track(bundle, tname),
            "## Uplift vs baseline (gate)",
            "",
            "> First experiment with a PAT@1% preview in **YAML order** is the track "
            "baseline; challengers are later experiments with previews. Values come from "
            "``evaluate_phase2_gate`` (``gate.min_uplift_pp_vs_baseline`` in percentage points).",
            "",
            _phase2_uplift_rows_markdown_for_track(gate, tname),
            "## PAT@1% series & std (gate)",
            "",
            "> Optional ``bundle['phase2_pat_series_by_experiment']``; std lines come from "
            "``gate['metrics']`` when the uplift/std path ran (limit = "
            "``gate.max_std_pp_across_windows`` in pp).",
            "",
            _phase2_std_and_pat_series_markdown_for_track(bundle, gate, tname),
            "## Metrics (shared backtest)",
            "",
            "> **Note**: Values below come from a **single** `trainer.backtester` run over "
            "`common.model_dir`, not per-experiment outputs. Per-track differentiation is T10+.",
            "",
            f"- **Precision @ recall 1% (shared)**: {pr_line}",
            "",
            "## Gate snapshot",
            "",
            f"- **gate status**: `{gate.get('status')}`",
            "",
            "### Blocking reasons",
            "",
        ]
        br = gate.get("blocking_reasons") or []
        if isinstance(br, list) and br:
            for item in br:
                lines.append(f"- `{item}`")
        else:
            lines.append("- *(none)*")
        lines.extend(["", "### Evidence summary", "", str(gate.get("evidence_summary") or ""), ""])
        out = phase2_dir / f"{tname}_results.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        written.append(out)
    return written


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
