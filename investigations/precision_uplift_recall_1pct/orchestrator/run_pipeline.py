#!/usr/bin/env python3
"""Precision uplift orchestrator CLI (Phase 1 MVP + Phase 2 T9–T11 scaffold)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_ORCHESTRATOR_DIR = Path(__file__).resolve().parent
# orchestrator/ -> precision_uplift_recall_1pct/ -> investigations/ -> repo root
_REPO_ROOT = _ORCHESTRATOR_DIR.parents[2]

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_DIR))

import collectors  # noqa: E402
import config_loader  # noqa: E402
import evaluators  # noqa: E402
import phase2_exit_codes as phase2_exits  # noqa: E402
import report_builder  # noqa: E402
import runner  # noqa: E402


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso_ts(raw: str) -> datetime:
    """Parse ISO-8601 timestamp string (supports trailing ``Z``)."""
    ts = raw.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def phase1_mid_snapshot_window(cfg: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the first auto mid-snapshot window for Phase 1.

    Compatibility helper for tests/callers that expect one midpoint. Internally
    the pipeline now supports multiple checkpoints via
    ``phase1_mid_snapshot_windows``.
    """
    rows = phase1_mid_snapshot_windows(cfg)
    return rows[0] if rows else None


def phase1_mid_snapshot_windows(cfg: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return auto mid-snapshot windows for Phase 1.

    Config (all optional) under ``checkpoints``:
    - ``enable_mid_snapshot``: bool, default True.
    - ``midpoint_ratio``: float in (0, 1), default 0.5.
    - ``midpoint_ratios``: optional list[float] in (0, 1); when set, overrides
      the single ``midpoint_ratio``.
    """
    cp = cfg.get("checkpoints") if isinstance(cfg.get("checkpoints"), Mapping) else {}
    enabled_raw = cp.get("enable_mid_snapshot", True)
    enabled = bool(enabled_raw) if isinstance(enabled_raw, bool) else True
    if not enabled:
        return []

    ratios_raw = cp.get("midpoint_ratios")
    ratios: list[float] = []
    if isinstance(ratios_raw, list) and ratios_raw:
        for x in ratios_raw:
            try:
                v = float(x)
            except (TypeError, ValueError):
                continue
            if 0.0 < v < 1.0:
                ratios.append(v)
    if not ratios:
        ratio_raw = cp.get("midpoint_ratio", 0.5)
        try:
            ratio = float(ratio_raw)
        except (TypeError, ValueError):
            ratio = 0.5
        if not (0.0 < ratio < 1.0):
            ratio = 0.5
        ratios = [ratio]

    window = cfg.get("window") if isinstance(cfg.get("window"), Mapping) else {}
    start_raw = str(window.get("start_ts") or "").strip()
    end_raw = str(window.get("end_ts") or "").strip()
    if not start_raw or not end_raw:
        return []
    start_dt = _parse_iso_ts(start_raw)
    end_dt = _parse_iso_ts(end_raw)
    if end_dt <= start_dt:
        return []

    uniq_sorted = sorted(set(ratios))
    out: list[dict[str, str]] = []
    for r in uniq_sorted:
        mid_dt = start_dt + (end_dt - start_dt) * r
        if mid_dt <= start_dt or mid_dt >= end_dt:
            continue
        out.append({"start_ts": start_raw, "end_ts": mid_dt.isoformat()})
    return out


def _state_path(run_id: str) -> Path:
    """Return path to ``run_state.json`` for this run."""
    return _ORCHESTRATOR_DIR / "state" / run_id / "run_state.json"


def _logs_dir(run_id: str) -> Path:
    """Return directory for subprocess stdout/stderr logs."""
    return _ORCHESTRATOR_DIR / "state" / run_id / "logs"


def _load_run_state(path: Path) -> dict[str, Any] | None:
    """Load existing run state JSON, or None if missing."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_run_state(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write run state JSON (best-effort).

    Retries ``replace`` on ``PermissionError`` (observed on Windows when AV or
    concurrent readers briefly lock ``run_state.json``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    last_pe: PermissionError | None = None
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_pe = exc
            if attempt < 7:
                time.sleep(0.05 * (attempt + 1))
    assert last_pe is not None
    raise last_pe


def build_input_summary(cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Build resume-safe input summary (includes full thresholds) + sha256 fingerprint.

    Args:
        cfg: Validated Phase 1 config.
        config_path: Resolved path to the YAML file.

    Returns:
        JSON-serializable mapping stored under ``run_state["input_summary"]``.
    """
    summary: dict[str, Any] = {
        "config_path": str(config_path.resolve()),
        "model_dir": str(cfg.get("model_dir")),
        "state_db_path": str(cfg.get("state_db_path")),
        "prediction_log_db_path": str(cfg.get("prediction_log_db_path")),
        "window": dict(cfg.get("window") or {}),
        "thresholds": dict(cfg.get("thresholds") or {}),
    }
    for opt in (
        "r1_r6_script",
        "backtest_skip_optuna",
        "backtest_extra_args",
        "backtest_metrics_path",
        "gate_pat_abs_tolerance",
        "checkpoints",
    ):
        if opt in cfg:
            summary[opt] = cfg[opt]
    fp_payload = json.dumps(summary, sort_keys=True, default=str, separators=(",", ":"))
    summary["fingerprint"] = hashlib.sha256(fp_payload.encode("utf-8")).hexdigest()
    return summary


def phase2_common_to_preflight_cfg(common: Mapping[str, Any]) -> dict[str, Any]:
    """Map phase2 ``common`` block to keys expected by ``runner.run_preflight``."""
    return {
        "model_dir": common["model_dir"],
        "state_db_path": common["state_db_path"],
        "prediction_log_db_path": common["prediction_log_db_path"],
        "window": dict(common["window"]),
    }


def phase2_cfg_to_backtest_cfg(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Map phase2 config to keys expected by ``runner.run_phase1_backtest`` (T10)."""
    com = cfg["common"]
    res = cfg.get("resources")
    res_d = dict(res) if isinstance(res, Mapping) else {}
    out: dict[str, Any] = {
        "model_dir": com["model_dir"],
        "window": dict(com["window"]),
        "backtest_skip_optuna": bool(res_d.get("backtest_skip_optuna", True)),
    }
    extras = res_d.get("backtest_extra_args")
    if isinstance(extras, list):
        out["backtest_extra_args"] = [str(x) for x in extras]
    return out


def _phase2_backtest_timeout_sec(cfg: Mapping[str, Any]) -> float | None:
    """Optional ``resources.phase2_backtest_timeout_sec`` for shared backtest subprocess."""
    res = cfg.get("resources")
    if not isinstance(res, Mapping):
        return None
    raw = res.get("phase2_backtest_timeout_sec")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _append_phase2_errors_for_failed_per_job_backtests(
    p2_bundle: dict[str, Any],
    pjb_results: list[Mapping[str, Any]],
    *,
    repo_root: Path,
) -> None:
    """Record structured errors for each failed non-skipped per-job backtest (T10).

    Uses ``E_NO_DATA_WINDOW`` when the result row carries ``ingest_error_code`` (metrics
    file readable but no parseable PAT@1%, aligned with shared backtest ingest); else
    ``E_ARTIFACT_MISSING``. ``bundle['errors']`` feeds ``evaluate_phase2_gate`` → **FAIL**.
    """
    p2_bundle.setdefault("errors", [])
    if not isinstance(p2_bundle["errors"], list):
        p2_bundle["errors"] = []
    err_list = p2_bundle["errors"]
    for row in pjb_results:
        if not isinstance(row, Mapping):
            continue
        if row.get("skipped"):
            continue
        if row.get("ok") is True:
            continue
        tr = str(row.get("track") or "").strip()
        eid = str(row.get("exp_id") or "").strip()
        detail = (
            row.get("metrics_load_error")
            or row.get("message")
            or "per-job backtest failed"
        )
        detail_s = str(detail).strip() or "per-job backtest failed"
        mrel = str(row.get("metrics_repo_relative") or "").strip()
        path_s = ""
        if mrel:
            path_s = str((repo_root / mrel).resolve())
        else:
            hint = str(row.get("training_metrics_repo_relative") or "").strip()
            if hint:
                path_s = str((repo_root / hint).resolve())
        prefix = f"{tr}/{eid}: " if tr and eid else ""
        row_code = row.get("ingest_error_code")
        err_code = (
            "E_NO_DATA_WINDOW"
            if str(row_code or "").strip() == "E_NO_DATA_WINDOW"
            else "E_ARTIFACT_MISSING"
        )
        err_list.append(
            {
                "code": err_code,
                "message": prefix + detail_s,
                "path": path_s,
            }
        )


def phase2_gate_cli_exit_code(
    gate_result: Mapping[str, Any],
    *,
    fail_on_gate_fail: bool = False,
    fail_on_gate_blocked: bool = False,
) -> int | None:
    """Return non-zero exit code after gate reports when policy requires failing the process.

    Args:
        gate_result: Output of ``evaluators.evaluate_phase2_gate``.
        fail_on_gate_fail: When True and status is ``FAIL``, return
            ``phase2_exit_codes.EXIT_PHASE2_GATE_FAIL``.
        fail_on_gate_blocked: When True and status is ``BLOCKED``, return
            ``phase2_exit_codes.EXIT_PHASE2_GATE_BLOCKED``.

    Returns:
        Gate policy exit code or ``None`` for exit 0 (see ``phase2_exit_codes``).
        ``FAIL`` is checked before ``BLOCKED`` when both flags are set.
    """
    st = str(gate_result.get("status") or "")
    if fail_on_gate_fail and st == "FAIL":
        return phase2_exits.EXIT_PHASE2_GATE_FAIL
    if fail_on_gate_blocked and st == "BLOCKED":
        return phase2_exits.EXIT_PHASE2_GATE_BLOCKED
    return None


def build_phase2_input_summary(cfg: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Build resume-safe input summary + fingerprint for Phase 2 config."""
    common = cfg["common"]
    summary: dict[str, Any] = {
        "config_path": str(config_path.resolve()),
        "phase": "phase2",
        "common": {
            "model_dir": str(common["model_dir"]),
            "state_db_path": str(common["state_db_path"]),
            "prediction_log_db_path": str(common["prediction_log_db_path"]),
            "window": dict(common["window"]),
            "contract": dict(common["contract"]),
        },
        "resources": dict(cfg["resources"]),
        "tracks": json.loads(json.dumps(cfg["tracks"], default=str)),
        "gate": dict(cfg["gate"]),
    }
    rid = cfg.get("run_id")
    if rid is not None and str(rid).strip():
        summary["run_id_yaml"] = str(rid).strip()
    fp_payload = json.dumps(summary, sort_keys=True, default=str, separators=(",", ":"))
    summary["fingerprint"] = hashlib.sha256(fp_payload.encode("utf-8")).hexdigest()
    return summary


def _resolve_config_path(repo_root: Path, rel_or_abs: str) -> Path:
    """Resolve a config path string relative to repo root when not absolute."""
    p = Path(str(rel_or_abs).strip())
    return p.resolve() if p.is_absolute() else (repo_root / p).resolve()


def build_run_full_input_summary(
    cfg: dict[str, Any],
    config_path: Path,
    resolved_phase_paths: Mapping[str, Path],
) -> dict[str, Any]:
    """Build resume-safe input summary + fingerprint for ``run_full`` config."""
    summary: dict[str, Any] = {
        "config_path": str(config_path.resolve()),
        "phase": "all",
        "execution": dict(cfg["execution"]),
        "phase_config_paths": {k: str(v.resolve()) for k, v in resolved_phase_paths.items()},
        "dry_run": dict(cfg["dry_run"]),
    }
    rid = cfg.get("run_id")
    if rid is not None and str(rid).strip():
        summary["run_id_yaml"] = str(rid).strip()
    fp_payload = json.dumps(summary, sort_keys=True, default=str, separators=(",", ":"))
    summary["fingerprint"] = hashlib.sha256(fp_payload.encode("utf-8")).hexdigest()
    return summary


def _contract_triplet(contract: Mapping[str, Any]) -> tuple[str, str, bool]:
    """Normalize contract fields for cross-phase comparison."""
    return (
        str(contract["metric"]).strip(),
        str(contract["timezone"]).strip(),
        bool(contract["exclude_censored"]),
    )


def _append_check(
    checks: list[dict[str, Any]],
    blocking: list[str],
    *,
    name: str,
    ok: bool,
    message: str | None,
    block_code: str | None,
    flags: Mapping[str, bool],
) -> None:
    """Append one dry-run check; optionally record a blocking code."""
    checks.append({"name": name, "ok": ok, "message": message})
    if not ok and block_code and flags.get("fail_on_any_check", True):
        blocking.append(block_code)


def run_all_phases_dry_run_readiness(
    repo_root: Path,
    orchestrator_dir: Path,
    run_id: str,
    run_full_cfg: dict[str, Any],
    resolved_paths: Mapping[str, Path],
    *,
    skip_backtest_smoke: bool,
    skip_phase1_preflight: bool = False,
    skip_phase2_preflight: bool = False,
) -> dict[str, Any]:
    """Run all-phase readiness checks (T16A) without long-running steps.

    Args:
        repo_root: Repository root.
        orchestrator_dir: ``orchestrator/`` directory.
        run_id: CLI run id.
        run_full_cfg: Validated ``run_full`` config (includes ``dry_run`` flags).
        resolved_paths: ``phase1``..``phase4`` -> absolute config paths.
        skip_backtest_smoke: Skip backtester CLI smoke when True.
        skip_phase1_preflight: When True, omit embedded phase1 ``run_preflight`` (e.g. already
            merged into ``run_state`` on this invocation).
        skip_phase2_preflight: When True, omit embedded phase2 ``run_preflight``.

    Returns:
        Dict with ``status`` (READY/NOT_READY), ``checks``, ``blocking_reasons``,
        ``artifacts``, and ``dry_run`` flags echo.
    """
    flags: dict[str, bool] = dict(run_full_cfg["dry_run"])
    checks: list[dict[str, Any]] = []
    blocking: list[str] = []
    artifacts: dict[str, str] = {}

    cfg1: dict[str, Any] | None = None
    cfg2: dict[str, Any] | None = None
    raw3: dict[str, Any] | None = None
    raw4: dict[str, Any] | None = None

    if flags.get("validate_phase_configs_exist", True):
        for pk in ("phase1", "phase2", "phase3", "phase4"):
            pth = resolved_paths[pk]
            ok = pth.is_file()
            _append_check(
                checks,
                blocking,
                name=f"phase_config_exists_{pk}",
                ok=ok,
                message=str(pth) if ok else f"missing file: {pth}",
                block_code=f"{pk}_config_missing",
                flags=flags,
            )

    if flags.get("validate_phase_schemas", True):
        try:
            cfg1 = config_loader.load_phase1_config(resolved_paths["phase1"])
            _append_check(
                checks,
                blocking,
                name="phase1_schema",
                ok=True,
                message="ok",
                block_code=None,
                flags=flags,
            )
        except config_loader.ConfigValidationError as exc:
            _append_check(
                checks,
                blocking,
                name="phase1_schema",
                ok=False,
                message=str(exc),
                block_code="phase1_schema_invalid",
                flags=flags,
            )
        try:
            cfg2 = config_loader.load_phase2_config(
                resolved_paths["phase2"],
                cli_run_id=run_id,
            )
            _append_check(
                checks,
                blocking,
                name="phase2_schema",
                ok=True,
                message="ok",
                block_code=None,
                flags=flags,
            )
        except config_loader.ConfigValidationError as exc:
            _append_check(
                checks,
                blocking,
                name="phase2_schema",
                ok=False,
                message=str(exc),
                block_code="phase2_schema_invalid",
                flags=flags,
            )
        try:
            raw3 = config_loader.validate_phase3_config_minimal(
                config_loader.load_raw_config(resolved_paths["phase3"])
            )
            _append_check(
                checks,
                blocking,
                name="phase3_schema",
                ok=True,
                message="ok",
                block_code=None,
                flags=flags,
            )
        except config_loader.ConfigValidationError as exc:
            _append_check(
                checks,
                blocking,
                name="phase3_schema",
                ok=False,
                message=str(exc),
                block_code="phase3_schema_invalid",
                flags=flags,
            )
        try:
            raw4 = config_loader.validate_phase4_config_minimal(
                config_loader.load_raw_config(resolved_paths["phase4"])
            )
            _append_check(
                checks,
                blocking,
                name="phase4_schema",
                ok=True,
                message="ok",
                block_code=None,
                flags=flags,
            )
        except config_loader.ConfigValidationError as exc:
            _append_check(
                checks,
                blocking,
                name="phase4_schema",
                ok=False,
                message=str(exc),
                block_code="phase4_schema_invalid",
                flags=flags,
            )

    if flags.get("validate_phase_dependencies", True) and raw3 is not None and raw4 is not None:
        ok = bool(str(raw3["upstream"].get("phase2_run_id", "")).strip())
        _append_check(
            checks,
            blocking,
            name="phase3_upstream_phase2_run_id",
            ok=ok,
            message=None if ok else "upstream.phase2_run_id missing",
            block_code="phase3_upstream_invalid",
            flags=flags,
        )
        ok = bool(str(raw4["candidate"].get("source_phase3_run_id", "")).strip())
        _append_check(
            checks,
            blocking,
            name="phase4_candidate_source_phase3_run_id",
            ok=ok,
            message=None if ok else "candidate.source_phase3_run_id missing",
            block_code="phase4_upstream_invalid",
            flags=flags,
        )

    if (
        flags.get("validate_contract_consistency", True)
        and cfg2 is not None
        and raw3 is not None
        and raw4 is not None
    ):
        t2 = _contract_triplet(cfg2["common"]["contract"])
        t3 = _contract_triplet(raw3["common"]["contract"])
        t4 = _contract_triplet(raw4["evaluation"]["contract"])
        ok = t2 == t3 == t4
        _append_check(
            checks,
            blocking,
            name="contract_consistency_phase2_3_4",
            ok=ok,
            message=(
                f"phase2={t2}, phase3={t3}, phase4={t4}" if not ok else "all match"
            ),
            block_code="contract_mismatch_across_phases",
            flags=flags,
        )

    if flags.get("validate_resource_limits", True) and cfg2 is not None:
        for label, resources in (
            ("phase2", cfg2["resources"]),
            ("phase3", raw3["resources"] if raw3 else None),
            ("phase4", raw4["resources"] if raw4 else None),
        ):
            if resources is None:
                continue
            mpj = resources.get("max_parallel_jobs")
            ok = isinstance(mpj, int) and mpj >= 1
            _append_check(
                checks,
                blocking,
                name=f"{label}_max_parallel_jobs",
                ok=ok,
                message=(
                    None
                    if ok
                    else f"max_parallel_jobs must be int >= 1, got {mpj!r}"
                ),
                block_code=f"{label}_resource_limits_invalid",
                flags=flags,
            )

    if flags.get("validate_paths_readable", True) and cfg1 is not None:
        md = runner._resolve_path(repo_root, str(cfg1["model_dir"]))
        sdb = runner._resolve_path(repo_root, str(cfg1["state_db_path"]))
        pdb = runner._resolve_path(repo_root, str(cfg1["prediction_log_db_path"]))
        ok = md.exists()
        _append_check(
            checks,
            blocking,
            name="phase1_model_dir_exists",
            ok=ok,
            message=str(md),
            block_code="phase1_model_dir_missing",
            flags=flags,
        )
        ok = sdb.is_file()
        _append_check(
            checks,
            blocking,
            name="phase1_state_db_file",
            ok=ok,
            message=str(sdb),
            block_code="phase1_state_db_missing",
            flags=flags,
        )
        ok = pdb.is_file()
        _append_check(
            checks,
            blocking,
            name="phase1_prediction_db_file",
            ok=ok,
            message=str(pdb),
            block_code="phase1_prediction_db_missing",
            flags=flags,
        )

    if flags.get("validate_paths_readable", True) and cfg2 is not None:
        com = cfg2["common"]
        md = runner._resolve_path(repo_root, str(com["model_dir"]))
        sdb = runner._resolve_path(repo_root, str(com["state_db_path"]))
        pdb = runner._resolve_path(repo_root, str(com["prediction_log_db_path"]))
        ok = md.exists()
        _append_check(
            checks,
            blocking,
            name="phase2_model_dir_exists",
            ok=ok,
            message=str(md),
            block_code="phase2_model_dir_missing",
            flags=flags,
        )
        ok = sdb.is_file()
        _append_check(
            checks,
            blocking,
            name="phase2_state_db_file",
            ok=ok,
            message=str(sdb),
            block_code="phase2_state_db_missing",
            flags=flags,
        )
        ok = pdb.is_file()
        _append_check(
            checks,
            blocking,
            name="phase2_prediction_db_file",
            ok=ok,
            message=str(pdb),
            block_code="phase2_prediction_db_missing",
            flags=flags,
        )

    if cfg1 is not None and not skip_phase1_preflight:
        pf1 = runner.run_preflight(
            repo_root,
            cfg1,
            skip_backtest_smoke=skip_backtest_smoke,
        )
        for c in pf1.get("checks") or []:
            nm = str(c.get("name", "check"))
            checks.append(
                {
                    "name": f"phase1_preflight_{nm}",
                    "ok": bool(c.get("ok")),
                    "message": c.get("message"),
                }
            )
            if not c.get("ok"):
                blocking.append(f"phase1_preflight_{nm}_failed")
        if not pf1.get("ok"):
            _append_check(
                checks,
                blocking,
                name="phase1_preflight_aggregate",
                ok=False,
                message=str(pf1.get("message") or pf1.get("error_code")),
                block_code="phase1_preflight_failed",
                flags=flags,
            )

    if cfg2 is not None and not skip_phase2_preflight:
        pf2 = runner.run_preflight(
            repo_root,
            phase2_common_to_preflight_cfg(cfg2["common"]),
            skip_backtest_smoke=True,
        )
        for c in pf2.get("checks") or []:
            nm = str(c.get("name", "check"))
            checks.append(
                {
                    "name": f"phase2_preflight_{nm}",
                    "ok": bool(c.get("ok")),
                    "message": c.get("message"),
                }
            )
            if not c.get("ok"):
                blocking.append(f"phase2_preflight_{nm}_failed")
        if not pf2.get("ok"):
            _append_check(
                checks,
                blocking,
                name="phase2_preflight_aggregate",
                ok=False,
                message=str(pf2.get("message") or pf2.get("error_code")),
                block_code="phase2_preflight_failed",
                flags=flags,
            )

    if flags.get("validate_writable_targets", True) and cfg1 is not None:
        phase3_dir = orchestrator_dir.parent / "phase3"
        phase4_dir = orchestrator_dir.parent / "phase4"
        smoke_skip = (
            skip_backtest_smoke or not flags.get("validate_cli_smoke_per_phase", True)
        )
        inner = run_dry_run_readiness(
            repo_root,
            orchestrator_dir,
            run_id,
            cfg1,
            skip_backtest_smoke=smoke_skip,
            extra_writable={
                "phase2_dir": orchestrator_dir.parent / "phase2",
                "phase3_dir": phase3_dir,
                "phase4_dir": phase4_dir,
            },
        )
        for c in inner.get("checks") or []:
            checks.append(dict(c))
        blocking.extend(inner.get("blocking_reasons") or [])
        artifacts.update(inner.get("artifacts") or {})

    status = "READY" if not blocking else "NOT_READY"
    out: dict[str, Any] = {
        "status": status,
        "checks": checks,
        "blocking_reasons": blocking,
        "artifacts": artifacts,
        "dry_run": flags,
    }
    return out


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and parse CLI arguments."""
    p = argparse.ArgumentParser(
        description="Precision uplift investigation orchestrator (phase1, phase2 scaffold)."
    )
    p.add_argument(
        "--phase",
        required=True,
        help="phase1 | phase2 | all (all = run_full.yaml; dry-run only until autonomous mode)",
    )
    p.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to phase YAML or run_full.yaml (when --phase all)",
    )
    p.add_argument("--run-id", dest="run_id", required=True, help="Run id (state directory name)")
    p.add_argument(
        "--collect-only",
        action="store_true",
        help="Run preflight then collectors/evaluators/reports only (no R1/R6/backtest yet)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Load run_state if present; later tasks skip completed steps",
    )
    p.add_argument(
        "--skip-backtest-smoke",
        action="store_true",
        help="Skip trainer.backtester --help smoke check (for constrained envs)",
    )
    p.add_argument(
        "--skip-phase2-trainer-smoke",
        action="store_true",
        help=(
            "Phase 2 only: mkdir per-job log dirs after plan bundle but skip "
            "python -m trainer.trainer --help"
        ),
    )
    p.add_argument(
        "--phase2-run-trainer-jobs",
        action="store_true",
        help=(
            "Phase 2 only: after runner smoke, run python -m trainer.trainer once per "
            "job_specs (uses common.window and resources.backtest_skip_optuna); "
            "default is skip (records trainer_jobs.executed=false in bundle)"
        ),
    )
    p.add_argument(
        "--phase2-run-per-job-backtests",
        action="store_true",
        help=(
            "Phase 2 only: after job metrics harvest, run trainer.backtester once per "
            "job_spec that has training_metrics_repo_relative (logs under each job's "
            "_per_job_backtest); skipped rows do not run. Runs before the shared "
            "--phase2-run-backtest-jobs step; default is skip"
        ),
    )
    p.add_argument(
        "--phase2-run-backtest-jobs",
        action="store_true",
        help=(
            "Phase 2 only: after optional per-job backtests, run one shared "
            "trainer.backtester for common.window + model_dir; then ingest "
            "resources.backtest_metrics_path or default "
            "trainer/out_backtest/backtest_metrics.json (exit 8 if missing); "
            "default is skip"
        ),
    )
    p.add_argument(
        "--phase2-fail-on-gate-fail",
        action="store_true",
        help=(
            "Phase 2 only: after phase2_gate_report, if evaluate_phase2_gate status is "
            "FAIL, mark step failed (E_PHASE2_GATE_FAIL) and exit 9 (reports still "
            "written). BLOCKED/PASS unchanged. Default: gate outcome does not change exit code"
        ),
    )
    p.add_argument(
        "--phase2-fail-on-gate-blocked",
        action="store_true",
        help=(
            "Phase 2 only: after phase2_gate_report, if evaluate_phase2_gate status is "
            "BLOCKED, mark step failed (E_PHASE2_GATE_BLOCKED) and exit 10 (reports still "
            "written). FAIL uses exit 9 when --phase2-fail-on-gate-fail is also set. "
            "Use with care: plan_only runs are usually BLOCKED"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run readiness checks only; do not run long investigation steps",
    )
    return p.parse_args(argv)


def _merge_state(
    prev: dict[str, Any] | None,
    run_id: str,
    phase: str,
    input_summary: dict[str, Any],
    preflight: dict[str, Any],
) -> dict[str, Any]:
    """Merge previous run_state with new preflight outcome."""
    base: dict[str, Any] = dict(prev) if prev else {}
    base["run_id"] = run_id
    base["phase"] = phase
    base["updated_at"] = _utc_now_iso()
    base["input_summary"] = input_summary
    base.setdefault("steps", {})
    step_status = "success" if preflight.get("ok") else "failed"
    base["steps"]["preflight"] = {
        "status": step_status,
        "error_code": preflight.get("error_code"),
        "message": preflight.get("message"),
        "checks": preflight.get("checks", []),
        "finished_at": _utc_now_iso(),
    }
    return base


def _step_success(steps: Mapping[str, Any], name: str) -> bool:
    """Return True if ``steps[name]`` completed successfully."""
    ent = steps.get(name)
    return isinstance(ent, dict) and ent.get("status") == "success"


def _mark_step_running(merged: dict[str, Any], name: str) -> str:
    """Set step to ``running``; return ``started_at`` ISO timestamp."""
    merged.setdefault("steps", {})
    started = _utc_now_iso()
    merged["steps"][name] = {"status": "running", "started_at": started}
    merged["updated_at"] = started
    return started


def _attach_step(
    merged: dict[str, Any],
    name: str,
    result: Mapping[str, Any],
    *,
    started_at: str | None = None,
) -> None:
    """Write step outcome into ``merged['steps'][name]`` and bump ``updated_at``."""
    merged.setdefault("steps", {})
    msg = result.get("message")
    ent: dict[str, Any] = {
        "status": "success" if result.get("ok") else "failed",
        "error_code": result.get("error_code"),
        "message": (msg[:2000] if isinstance(msg, str) else msg),
        "returncode": result.get("returncode"),
        "stdout_log": str(result.get("stdout_path", "")),
        "stderr_log": str(result.get("stderr_path", "")),
        "finished_at": _utc_now_iso(),
    }
    if started_at:
        ent["started_at"] = started_at
    merged["steps"][name] = ent
    merged["updated_at"] = _utc_now_iso()


def _attach_terminal_step(
    merged: dict[str, Any],
    name: str,
    *,
    status: str,
    artifacts: dict[str, str] | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> None:
    """Record a terminal orchestrator step (collect / reports)."""
    merged.setdefault("steps", {})
    body: dict[str, Any] = {
        "status": status,
        "finished_at": _utc_now_iso(),
    }
    if artifacts:
        body["artifacts"] = dict(artifacts)
    if error_code:
        body["error_code"] = error_code
    if message:
        body["message"] = message
    merged["steps"][name] = body
    merged["updated_at"] = _utc_now_iso()


def _check_writable_target(path: Path) -> tuple[bool, str | None]:
    """Validate a directory can be created and written by current process."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".orchestrator_write_probe.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, None
    except OSError as exc:
        return False, f"{path}: {exc}"


def run_dry_run_readiness(
    repo_root: Path,
    orchestrator_dir: Path,
    run_id: str,
    cfg: Mapping[str, Any],
    *,
    skip_backtest_smoke: bool,
    extra_writable: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Run fast readiness checks without long-running analysis execution.

    Returns:
        Dict with ``status`` (READY/NOT_READY), ``checks`` list, and
        ``blocking_reasons``.
    """
    checks: list[dict[str, Any]] = []
    blocking: list[str] = []

    ok, msg = runner.run_r1_r6_cli_smoke(repo_root, cfg)
    checks.append({"name": "r1_r6_cli_smoke", "ok": ok, "message": msg})
    if not ok:
        blocking.append("r1_r6_cli_smoke_failed")

    if skip_backtest_smoke:
        checks.append(
            {
                "name": "backtester_cli_smoke",
                "ok": True,
                "message": "skipped by --skip-backtest-smoke",
            }
        )
    else:
        ok, msg = runner.run_backtest_cli_smoke(repo_root)
        checks.append({"name": "backtester_cli_smoke", "ok": ok, "message": msg})
        if not ok:
            blocking.append("backtester_cli_smoke_failed")

    writable_targets = {
        "state_dir": orchestrator_dir / "state" / run_id,
        "logs_dir": _logs_dir(run_id),
        "phase1_dir": orchestrator_dir.parent / "phase1",
    }
    writable_artifacts: dict[str, str] = {}
    for name, p in writable_targets.items():
        ok, msg = _check_writable_target(p)
        checks.append({"name": f"writable_{name}", "ok": ok, "message": msg})
        if ok:
            writable_artifacts[name] = str(p.resolve())
        else:
            blocking.append(f"writable_{name}_failed")

    if extra_writable:
        for name, p in extra_writable.items():
            ok, msg = _check_writable_target(p)
            checks.append({"name": f"writable_{name}", "ok": ok, "message": msg})
            if ok:
                writable_artifacts[name] = str(p.resolve())
            else:
                blocking.append(f"writable_{name}_failed")

    status = "READY" if not blocking else "NOT_READY"
    return {
        "status": status,
        "checks": checks,
        "blocking_reasons": blocking,
        "artifacts": writable_artifacts,
    }


def _main_phase1(args: argparse.Namespace, config_path: Path) -> int:
    """Run Phase 1 pipeline (R1/R6, backtest, collect, reports)."""
    try:
        cfg = config_loader.load_phase1_config(config_path)
    except config_loader.ConfigValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    state_file = _state_path(args.run_id)
    prev_state = _load_run_state(state_file) if args.resume else None

    input_summary = build_input_summary(cfg, config_path)
    prev_fp = (prev_state.get("input_summary") or {}).get("fingerprint") if prev_state else None
    fingerprint_mismatch = bool(
        args.resume
        and prev_state is not None
        and prev_fp != input_summary["fingerprint"]
    )
    if fingerprint_mismatch:
        print(
            "orchestrator: resume invalid — config fingerprint differs from run_state "
            "(or was missing); will re-run preflight and eligible pipeline steps.",
            file=sys.stderr,
        )

    skip_preflight = bool(
        args.resume
        and prev_state
        and not fingerprint_mismatch
        and (prev_state.get("steps") or {}).get("preflight", {}).get("status") == "success"
    )

    if skip_preflight:
        preflight = {
            "ok": True,
            "error_code": None,
            "message": "skipped (resume: preflight already success)",
            "checks": [],
        }
    else:
        preflight = runner.run_preflight(
            _REPO_ROOT,
            cfg,
            skip_backtest_smoke=args.skip_backtest_smoke,
        )

    merged = _merge_state(prev_state, args.run_id, args.phase, input_summary, preflight)
    if fingerprint_mismatch:
        merged["resume_invalidated"] = "config_fingerprint_mismatch"
    else:
        merged.pop("resume_invalidated", None)
    _write_run_state(state_file, merged)

    if not preflight.get("ok"):
        print(preflight.get("message") or "preflight failed", file=sys.stderr)
        return 3

    if args.dry_run:
        readiness = run_dry_run_readiness(
            _REPO_ROOT,
            _ORCHESTRATOR_DIR,
            args.run_id,
            cfg,
            skip_backtest_smoke=args.skip_backtest_smoke,
            extra_writable=None,
        )
        merged["mode"] = "dry_run"
        merged["readiness"] = readiness
        _attach_terminal_step(
            merged,
            "dry_run_readiness",
            status="success" if readiness["status"] == "READY" else "failed",
            artifacts=readiness.get("artifacts"),
            error_code=(None if readiness["status"] == "READY" else "E_DRY_RUN_NOT_READY"),
            message="; ".join(readiness.get("blocking_reasons", [])),
        )
        _write_run_state(state_file, merged)
        if readiness["status"] != "READY":
            print(
                "dry-run NOT_READY: " + ", ".join(readiness["blocking_reasons"]),
                file=sys.stderr,
            )
            return 6
        return 0

    prev_steps: dict[str, Any] = (
        (prev_state.get("steps") or {}) if isinstance(prev_state, dict) else {}
    )
    resume_ok = args.resume and not fingerprint_mismatch

    if not args.collect_only:
        log_dir = _logs_dir(args.run_id)
        skip_mid = resume_ok and _step_success(prev_steps, "r1_r6_mid_snapshot")
        skip_r1 = resume_ok and _step_success(prev_steps, "r1_r6_analysis")
        skip_bt = resume_ok and _step_success(prev_steps, "backtest")

        if not skip_mid:
            mid_windows = phase1_mid_snapshot_windows(cfg)
            if not mid_windows:
                _attach_terminal_step(
                    merged,
                    "r1_r6_mid_snapshot",
                    status="success",
                    message="mid snapshot skipped (disabled or invalid checkpoint window)",
                )
                _write_run_state(state_file, merged)
            else:
                t_mid = _mark_step_running(merged, "r1_r6_mid_snapshot")
                _write_run_state(state_file, merged)
                r1_mid_last: dict[str, Any] | None = None
                for idx, mid_window in enumerate(mid_windows):
                    stem = (
                        "r1_r6_mid"
                        if idx == len(mid_windows) - 1
                        else f"r1_r6_mid_cp{idx + 1}"
                    )
                    r1_mid = runner.run_phase1_r1_r6_all(
                        _REPO_ROOT,
                        cfg,
                        log_dir,
                        window_override=mid_window,
                        log_stem=stem,
                    )
                    r1_mid_last = r1_mid
                    if not r1_mid.get("ok"):
                        fail_msg = (
                            f"mid snapshot failed at checkpoint {idx + 1}/{len(mid_windows)} "
                            f"(end_ts={mid_window.get('end_ts')})"
                        )
                        raw_msg = r1_mid.get("message")
                        if isinstance(raw_msg, str) and raw_msg.strip():
                            r1_mid["message"] = f"{fail_msg}; {raw_msg}"
                        else:
                            r1_mid["message"] = fail_msg
                        _attach_step(
                            merged, "r1_r6_mid_snapshot", r1_mid, started_at=t_mid
                        )
                        _write_run_state(state_file, merged)
                        break
                assert r1_mid_last is not None
                if r1_mid_last.get("ok"):
                    msg = f"mid snapshot(s) completed: {len(mid_windows)} checkpoint(s)"
                    _attach_terminal_step(
                        merged,
                        "r1_r6_mid_snapshot",
                        status="success",
                        artifacts={
                            "r1_r6_mid_stdout_log": str(
                                log_dir / "r1_r6_mid.stdout.log"
                            )
                        },
                        message=msg,
                    )
                    merged["steps"]["r1_r6_mid_snapshot"]["started_at"] = t_mid
                    _write_run_state(state_file, merged)
                else:
                    print(
                        r1_mid_last.get("message")
                        or r1_mid_last.get("error_code")
                        or "r1_r6_mid_snapshot failed",
                        file=sys.stderr,
                    )
                    return 4

        if not skip_r1:
            t_r1 = _mark_step_running(merged, "r1_r6_analysis")
            _write_run_state(state_file, merged)
            r1 = runner.run_phase1_r1_r6_all(_REPO_ROOT, cfg, log_dir)
            _attach_step(merged, "r1_r6_analysis", r1, started_at=t_r1)
            _write_run_state(state_file, merged)
            if not r1.get("ok"):
                print(
                    r1.get("message") or r1.get("error_code") or "r1_r6_analysis failed",
                    file=sys.stderr,
                )
                return 4

        if not skip_bt:
            t_bt = _mark_step_running(merged, "backtest")
            _write_run_state(state_file, merged)
            bt = runner.run_phase1_backtest(_REPO_ROOT, cfg, log_dir)
            _attach_step(merged, "backtest", bt, started_at=t_bt)
            _write_run_state(state_file, merged)
            if not bt.get("ok"):
                print(
                    bt.get("message") or bt.get("error_code") or "backtest failed",
                    file=sys.stderr,
                )
                return 5

    bundle = collectors.collect_phase1_artifacts(
        args.run_id,
        cfg,
        repo_root=_REPO_ROOT,
        orchestrator_dir=_ORCHESTRATOR_DIR,
    )
    collect_path = _ORCHESTRATOR_DIR / "state" / args.run_id / "collect_bundle.json"
    collect_path.parent.mkdir(parents=True, exist_ok=True)
    collect_path.write_text(
        json.dumps(bundle, indent=2, default=str),
        encoding="utf-8",
    )
    merged["collect"] = collectors.collect_summary_for_run_state(bundle)
    merged["collect_bundle_path"] = str(collect_path)
    gate = evaluators.evaluate_phase1_gate(bundle)
    merged["gate_decision"] = {
        "status": gate.get("status"),
        "blocking_reasons": list(gate.get("blocking_reasons") or []),
        "evidence_summary": str(gate.get("evidence_summary") or ""),
    }
    log_dir_resolved = _logs_dir(args.run_id)
    phase1_dir = _ORCHESTRATOR_DIR.parent / "phase1"
    merged["artifacts"] = {
        "run_state": str(state_file.resolve()),
        "logs_dir": str(log_dir_resolved.resolve()),
        "collect_bundle": str(collect_path.resolve()),
        "phase1_dir": str(phase1_dir.resolve()),
        "config_path": str(config_path.resolve()),
    }
    _attach_terminal_step(
        merged,
        "collect",
        status="success",
        artifacts={"collect_bundle": str(collect_path.resolve())},
    )
    merged["updated_at"] = _utc_now_iso()
    _write_run_state(state_file, merged)

    report_builder.write_phase1_reports(phase1_dir, args.run_id, cfg, bundle, gate)
    _attach_terminal_step(
        merged,
        "reports",
        status="success",
        artifacts={"phase1_dir": str(phase1_dir.resolve())},
    )
    _write_run_state(state_file, merged)

    return 0


def _main_phase2(args: argparse.Namespace, config_path: Path) -> int:
    """Phase 2: validate config, preflight, scaffold, plan bundle, runner smoke, jobs (T9–T10).

    Per-job ``trainer.trainer`` runs are opt-in via ``--phase2-run-trainer-jobs`` (can be heavy).
    Shared ``trainer.backtester`` + metrics ingest is opt-in via ``--phase2-run-backtest-jobs``.
    Gate / track uplift remains T11 until per-track metrics exist.
    """
    try:
        cfg = config_loader.load_phase2_config(config_path, cli_run_id=args.run_id)
    except config_loader.ConfigValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    state_file = _state_path(args.run_id)
    prev_state = _load_run_state(state_file) if args.resume else None
    input_summary = build_phase2_input_summary(cfg, config_path)
    prev_fp = (prev_state.get("input_summary") or {}).get("fingerprint") if prev_state else None
    fingerprint_mismatch = bool(
        args.resume
        and prev_state is not None
        and prev_fp != input_summary["fingerprint"]
    )
    if fingerprint_mismatch:
        print(
            "orchestrator: resume invalid — config fingerprint differs from run_state "
            "(or was missing); will re-run preflight and eligible pipeline steps.",
            file=sys.stderr,
        )

    skip_preflight = bool(
        args.resume
        and prev_state
        and not fingerprint_mismatch
        and (prev_state.get("steps") or {}).get("preflight", {}).get("status") == "success"
    )
    preflight_cfg = phase2_common_to_preflight_cfg(cfg["common"])

    if skip_preflight:
        preflight = {
            "ok": True,
            "error_code": None,
            "message": "skipped (resume: preflight already success)",
            "checks": [],
        }
    else:
        preflight = runner.run_preflight(
            _REPO_ROOT,
            preflight_cfg,
            skip_backtest_smoke=args.skip_backtest_smoke,
        )

    merged = _merge_state(prev_state, args.run_id, args.phase, input_summary, preflight)
    if fingerprint_mismatch:
        merged["resume_invalidated"] = "config_fingerprint_mismatch"
    else:
        merged.pop("resume_invalidated", None)
    _write_run_state(state_file, merged)

    if not preflight.get("ok"):
        print(preflight.get("message") or "preflight failed", file=sys.stderr)
        return 3

    phase2_dir = _ORCHESTRATOR_DIR.parent / "phase2"

    if args.dry_run:
        readiness = run_dry_run_readiness(
            _REPO_ROOT,
            _ORCHESTRATOR_DIR,
            args.run_id,
            preflight_cfg,
            skip_backtest_smoke=args.skip_backtest_smoke,
            extra_writable={"phase2_dir": phase2_dir},
        )
        merged["mode"] = "dry_run"
        merged["readiness"] = readiness
        _attach_terminal_step(
            merged,
            "dry_run_readiness",
            status="success" if readiness["status"] == "READY" else "failed",
            artifacts=readiness.get("artifacts"),
            error_code=(None if readiness["status"] == "READY" else "E_DRY_RUN_NOT_READY"),
            message="; ".join(readiness.get("blocking_reasons", [])),
        )
        _write_run_state(state_file, merged)
        if readiness["status"] != "READY":
            print(
                "dry-run NOT_READY: " + ", ".join(readiness["blocking_reasons"]),
                file=sys.stderr,
            )
            return 6
        return 0

    if args.collect_only:
        merged["artifacts"] = {
            "run_state": str(state_file.resolve()),
            "config_path": str(config_path.resolve()),
        }
        merged["updated_at"] = _utc_now_iso()
        _write_run_state(state_file, merged)
        return 0

    prev_steps: dict[str, Any] = (
        (prev_state.get("steps") or {}) if isinstance(prev_state, dict) else {}
    )
    resume_ok = args.resume and not fingerprint_mismatch
    skip_scaffold = resume_ok and _step_success(prev_steps, "phase2_scaffold")

    if not skip_scaffold:
        t0 = _mark_step_running(merged, "phase2_scaffold")
        _write_run_state(state_file, merged)
        _attach_terminal_step(
            merged,
            "phase2_scaffold",
            status="success",
            artifacts={"phase2_dir": str(phase2_dir.resolve())},
            message=(
                "T9 scaffold: config validated and preflight ok; "
                "plan-only phase2_bundle written in phase2_plan_bundle step (T10)"
            ),
        )
        merged["steps"]["phase2_scaffold"]["started_at"] = t0
        _write_run_state(state_file, merged)

    bundle_path = _ORCHESTRATOR_DIR / "state" / args.run_id / "phase2_bundle.json"
    skip_plan_bundle = resume_ok and _step_success(prev_steps, "phase2_plan_bundle")
    if not skip_plan_bundle:
        t_pb = _mark_step_running(merged, "phase2_plan_bundle")
        _write_run_state(state_file, merged)
        p2_bundle = collectors.collect_phase2_plan_bundle(args.run_id, cfg)
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(
            json.dumps(p2_bundle, indent=2, default=str),
            encoding="utf-8",
        )
        merged["phase2_collect"] = collectors.collect_summary_phase2_plan_for_run_state(
            p2_bundle
        )
        merged["phase2_bundle_path"] = str(bundle_path.resolve())
        _attach_terminal_step(
            merged,
            "phase2_plan_bundle",
            status="success",
            artifacts={"phase2_bundle": str(bundle_path.resolve())},
            message="T10: plan-only phase2_bundle.json from config (trainer runs deferred)",
        )
        merged["steps"]["phase2_plan_bundle"]["started_at"] = t_pb
        _write_run_state(state_file, merged)
    else:
        try:
            p2_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"orchestrator: cannot load phase2_bundle.json for resume: {exc}",
                file=sys.stderr,
            )
            return 4

    skip_runner_smoke = resume_ok and _step_success(prev_steps, "phase2_runner_smoke")
    if not skip_runner_smoke:
        t_rs = _mark_step_running(merged, "phase2_runner_smoke")
        _write_run_state(state_file, merged)
        ok_ld, msg_ld = runner.ensure_phase2_job_log_dirs(
            _REPO_ROOT, p2_bundle.get("job_specs")
        )
        if not ok_ld:
            rs_fail: dict[str, Any] = {
                "log_dirs_ok": False,
                "log_dirs_error": msg_ld,
                "trainer_help_skipped": True,
                "trainer_help_ok": None,
                "finished_at": _utc_now_iso(),
            }
            p2_bundle["runner_smoke"] = rs_fail
            merged["phase2_collect"] = (
                collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
            )
            bundle_path.write_text(
                json.dumps(p2_bundle, indent=2, default=str),
                encoding="utf-8",
            )
            _attach_terminal_step(
                merged,
                "phase2_runner_smoke",
                status="failed",
                error_code="E_PHASE2_RUNNER_SMOKE",
                message=msg_ld,
            )
            merged["steps"]["phase2_runner_smoke"]["started_at"] = t_rs
            _write_run_state(state_file, merged)
            print(msg_ld or "phase2_runner_smoke failed", file=sys.stderr)
            return phase2_exits.EXIT_PHASE2_RUNNER_SMOKE_FAILED

        th_skip = bool(args.skip_phase2_trainer_smoke)
        if th_skip:
            ok_th, msg_th = True, None
        else:
            ok_th, msg_th = runner.run_trainer_trainer_help_smoke(_REPO_ROOT)

        rs_ok: dict[str, Any] = {
            "log_dirs_ok": True,
            "trainer_help_skipped": th_skip,
            "trainer_help_ok": None if th_skip else ok_th,
            "trainer_help_error": None if th_skip or ok_th else msg_th,
            "finished_at": _utc_now_iso(),
        }
        p2_bundle["runner_smoke"] = rs_ok
        merged["phase2_collect"] = collectors.collect_summary_phase2_plan_for_run_state(
            p2_bundle
        )
        bundle_path.write_text(
            json.dumps(p2_bundle, indent=2, default=str),
            encoding="utf-8",
        )
        ok_rs = True if th_skip else bool(ok_th)
        _attach_terminal_step(
            merged,
            "phase2_runner_smoke",
            status="success" if ok_rs else "failed",
            error_code=None if ok_rs else "E_PHASE2_RUNNER_SMOKE",
            message=(
                None
                if ok_rs
                else (msg_th or "trainer.trainer --help smoke failed")
            ),
        )
        merged["steps"]["phase2_runner_smoke"]["started_at"] = t_rs
        _write_run_state(state_file, merged)
        if not ok_rs:
            print(
                msg_th or "phase2_runner_smoke failed",
                file=sys.stderr,
            )
            return phase2_exits.EXIT_PHASE2_RUNNER_SMOKE_FAILED

    skip_trainer_jobs = resume_ok and _step_success(prev_steps, "phase2_trainer_jobs")
    if not skip_trainer_jobs:
        t_tj = _mark_step_running(merged, "phase2_trainer_jobs")
        _write_run_state(state_file, merged)
        if not bool(getattr(args, "phase2_run_trainer_jobs", False)):
            p2_bundle["trainer_jobs"] = {
                "executed": False,
                "skip_reason": (
                    "pass --phase2-run-trainer-jobs to run trainer.trainer per job_spec"
                ),
                "all_ok": None,
                "results": [],
                "finished_at": _utc_now_iso(),
            }
            merged["phase2_collect"] = (
                collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
            )
            bundle_path.write_text(
                json.dumps(p2_bundle, indent=2, default=str),
                encoding="utf-8",
            )
            _attach_terminal_step(
                merged,
                "phase2_trainer_jobs",
                status="success",
                message="T10: trainer jobs skipped (no --phase2-run-trainer-jobs)",
            )
            merged["steps"]["phase2_trainer_jobs"]["started_at"] = t_tj
            _write_run_state(state_file, merged)
        else:
            ok_tj, msg_tj, job_results = runner.run_phase2_trainer_jobs(
                _REPO_ROOT, p2_bundle, python_exe=sys.executable
            )
            p2_bundle["trainer_jobs"] = {
                "executed": True,
                "skip_reason": None,
                "all_ok": ok_tj,
                "results": job_results,
                "finished_at": _utc_now_iso(),
            }
            runner.merge_inferred_training_metrics_paths_into_phase2_bundle(
                p2_bundle, _REPO_ROOT
            )
            merged["phase2_collect"] = (
                collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
            )
            bundle_path.write_text(
                json.dumps(p2_bundle, indent=2, default=str),
                encoding="utf-8",
            )
            _attach_terminal_step(
                merged,
                "phase2_trainer_jobs",
                status="success" if ok_tj else "failed",
                error_code=None if ok_tj else "E_PHASE2_TRAINER_JOBS",
                message=None if ok_tj else (msg_tj or "phase2_trainer_jobs failed"),
            )
            merged["steps"]["phase2_trainer_jobs"]["started_at"] = t_tj
            _write_run_state(state_file, merged)
            if not ok_tj:
                print(msg_tj or "phase2_trainer_jobs failed", file=sys.stderr)
                return phase2_exits.EXIT_PHASE2_TRAINER_JOBS_FAILED

    skip_job_harvest = resume_ok and _step_success(prev_steps, "phase2_job_metrics_harvest")
    if not skip_job_harvest:
        t_jh = _mark_step_running(merged, "phase2_job_metrics_harvest")
        _write_run_state(state_file, merged)
        harvest_rows = collectors.harvest_phase2_job_training_metrics(_REPO_ROOT, p2_bundle)
        p2_bundle["job_training_harvest"] = {
            "finished_at": _utc_now_iso(),
            "metrics_filename": collectors.PHASE2_JOB_TRAINING_METRICS_NAME,
            "rows": harvest_rows,
        }
        merged["phase2_collect"] = (
            collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
        )
        bundle_path.write_text(
            json.dumps(p2_bundle, indent=2, default=str),
            encoding="utf-8",
        )
        _attach_terminal_step(
            merged,
            "phase2_job_metrics_harvest",
            status="success",
            message="T10: scan job log dirs for training_metrics.json",
        )
        merged["steps"]["phase2_job_metrics_harvest"]["started_at"] = t_jh
        _write_run_state(state_file, merged)

    skip_per_job_bt = resume_ok and _step_success(
        prev_steps, "phase2_per_job_backtest_jobs"
    )
    if not skip_per_job_bt:
        t_pjb = _mark_step_running(merged, "phase2_per_job_backtest_jobs")
        _write_run_state(state_file, merged)
        if not bool(getattr(args, "phase2_run_per_job_backtests", False)):
            p2_bundle["per_job_backtest_jobs"] = {
                "executed": False,
                "skip_reason": (
                    "pass --phase2-run-per-job-backtests to run trainer.backtester "
                    "per job_spec with training_metrics_repo_relative"
                ),
                "all_ok": None,
                "results": [],
                "finished_at": _utc_now_iso(),
            }
            merged["phase2_collect"] = (
                collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
            )
            bundle_path.write_text(
                json.dumps(p2_bundle, indent=2, default=str),
                encoding="utf-8",
            )
            _attach_terminal_step(
                merged,
                "phase2_per_job_backtest_jobs",
                status="success",
                message=(
                    "T10: per-job backtests skipped (no --phase2-run-per-job-backtests)"
                ),
            )
            merged["steps"]["phase2_per_job_backtest_jobs"]["started_at"] = t_pjb
            _write_run_state(state_file, merged)
        else:
            bt_cfg_t = phase2_cfg_to_backtest_cfg(cfg)
            to_pjb = _phase2_backtest_timeout_sec(cfg)
            ok_pjb, msg_pjb, pjb_results = runner.run_phase2_per_job_backtests(
                _REPO_ROOT,
                p2_bundle,
                bt_cfg_t,
                run_id=args.run_id,
                python_exe=sys.executable,
                timeout_sec=to_pjb,
            )
            if not ok_pjb:
                _append_phase2_errors_for_failed_per_job_backtests(
                    p2_bundle,
                    list(pjb_results),
                    repo_root=_REPO_ROOT,
                )
            p2_bundle["per_job_backtest_jobs"] = {
                "executed": True,
                "skip_reason": None,
                "all_ok": ok_pjb,
                "results": pjb_results,
                "finished_at": _utc_now_iso(),
            }
            merged["phase2_collect"] = (
                collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
            )
            bundle_path.write_text(
                json.dumps(p2_bundle, indent=2, default=str),
                encoding="utf-8",
            )
            _attach_terminal_step(
                merged,
                "phase2_per_job_backtest_jobs",
                status="success" if ok_pjb else "failed",
                error_code=None if ok_pjb else "E_PHASE2_PER_JOB_BACKTEST_JOBS",
                message=None if ok_pjb else (msg_pjb or "phase2_per_job_backtest_jobs failed"),
            )
            merged["steps"]["phase2_per_job_backtest_jobs"]["started_at"] = t_pjb
            _write_run_state(state_file, merged)
            if not ok_pjb:
                print(
                    msg_pjb or "phase2_per_job_backtest_jobs failed",
                    file=sys.stderr,
                )
                return phase2_exits.EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE

    skip_backtest_jobs = resume_ok and _step_success(prev_steps, "phase2_backtest_jobs")
    if not skip_backtest_jobs:
        t_bj = _mark_step_running(merged, "phase2_backtest_jobs")
        _write_run_state(state_file, merged)
        if not bool(getattr(args, "phase2_run_backtest_jobs", False)):
            p2_bundle["backtest_jobs"] = {
                "executed": False,
                "skip_reason": (
                    "pass --phase2-run-backtest-jobs to run trainer.backtester "
                    "and ingest backtest_metrics"
                ),
                "subprocess_ok": None,
                "metrics_loaded": None,
                "metrics_path": None,
                "finished_at": _utc_now_iso(),
            }
            merged["phase2_collect"] = (
                collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
            )
            bundle_path.write_text(
                json.dumps(p2_bundle, indent=2, default=str),
                encoding="utf-8",
            )
            _attach_terminal_step(
                merged,
                "phase2_backtest_jobs",
                status="success",
                message="T10: backtest jobs skipped (no --phase2-run-backtest-jobs)",
            )
            merged["steps"]["phase2_backtest_jobs"]["started_at"] = t_bj
            _write_run_state(state_file, merged)
        else:
            rel_bt = collectors.phase2_shared_backtest_logs_subdir_relative(args.run_id)
            log_bt = (_REPO_ROOT / rel_bt).resolve()
            bt_cfg = phase2_cfg_to_backtest_cfg(cfg)
            to_bt = _phase2_backtest_timeout_sec(cfg)
            res_bt = runner.run_phase1_backtest(
                _REPO_ROOT,
                bt_cfg,
                log_bt,
                timeout_sec=to_bt,
            )
            ok_sub = bool(res_bt.get("ok"))
            res_map = cfg.get("resources")
            mrel = collectors.DEFAULT_BACKTEST_METRICS
            if isinstance(res_map, Mapping):
                bmp = res_map.get("backtest_metrics_path")
                if bmp is not None and str(bmp).strip():
                    mrel = str(bmp).strip()
            mpath = collectors._resolve_under_root(_REPO_ROOT, mrel)
            if not ok_sub:
                p2_bundle["backtest_jobs"] = {
                    "executed": True,
                    "skip_reason": None,
                    "subprocess_ok": False,
                    "metrics_loaded": False,
                    "metrics_path": str(mpath),
                    "backtest_error_code": res_bt.get("error_code"),
                    "backtest_message": res_bt.get("message"),
                    "finished_at": _utc_now_iso(),
                }
                merged["phase2_collect"] = (
                    collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
                )
                bundle_path.write_text(
                    json.dumps(p2_bundle, indent=2, default=str),
                    encoding="utf-8",
                )
                _attach_terminal_step(
                    merged,
                    "phase2_backtest_jobs",
                    status="failed",
                    error_code="E_PHASE2_BACKTEST_JOBS",
                    message=res_bt.get("message") or "phase2 backtest subprocess failed",
                )
                merged["steps"]["phase2_backtest_jobs"]["started_at"] = t_bj
                _write_run_state(state_file, merged)
                print(
                    res_bt.get("message") or "phase2_backtest_jobs failed",
                    file=sys.stderr,
                )
                return phase2_exits.EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE
            metrics_obj, metrics_err = collectors.load_json_under_repo(_REPO_ROOT, mrel)
            if metrics_obj is None or metrics_err:
                p2_bundle.setdefault("errors", [])
                if isinstance(p2_bundle["errors"], list):
                    err_item: dict[str, str] = {
                        "code": "E_ARTIFACT_MISSING",
                        "message": metrics_err
                        or f"backtest_metrics not loaded after subprocess ok: {mpath}",
                        "path": str(mpath),
                    }
                    p2_bundle["errors"].append(err_item)
                p2_bundle["backtest_jobs"] = {
                    "executed": True,
                    "skip_reason": None,
                    "subprocess_ok": True,
                    "metrics_loaded": False,
                    "metrics_path": str(mpath),
                    "ingest_error": metrics_err,
                    "finished_at": _utc_now_iso(),
                }
                merged["phase2_collect"] = (
                    collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
                )
                bundle_path.write_text(
                    json.dumps(p2_bundle, indent=2, default=str),
                    encoding="utf-8",
                )
                _attach_terminal_step(
                    merged,
                    "phase2_backtest_jobs",
                    status="failed",
                    error_code="E_ARTIFACT_MISSING",
                    message=metrics_err or f"missing or invalid metrics at {mpath}",
                )
                merged["steps"]["phase2_backtest_jobs"]["started_at"] = t_bj
                _write_run_state(state_file, merged)
                print(
                    metrics_err or f"phase2_backtest_jobs: missing metrics at {mpath}",
                    file=sys.stderr,
                )
                return phase2_exits.EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE
            pr1_shared = evaluators.extract_phase2_shared_precision_at_recall_1pct(
                metrics_obj
                if isinstance(metrics_obj, Mapping)
                else None
            )
            if pr1_shared is None:
                pr_key = evaluators.PHASE2_BACKTEST_PR1_KEY
                p2_bundle.setdefault("errors", [])
                if isinstance(p2_bundle["errors"], list):
                    p2_bundle["errors"].append(
                        {
                            "code": "E_NO_DATA_WINDOW",
                            "message": (
                                f"shared backtest_metrics at {mpath} lacks parseable "
                                f"model_default.{pr_key} (PAT@1% for observation window)"
                            ),
                            "path": str(mpath),
                        }
                    )
                p2_bundle["backtest_jobs"] = {
                    "executed": True,
                    "skip_reason": None,
                    "subprocess_ok": True,
                    "metrics_loaded": True,
                    "metrics_path": str(mpath),
                    "shared_pat_extractable": False,
                    "finished_at": _utc_now_iso(),
                }
                merged["phase2_collect"] = (
                    collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
                )
                bundle_path.write_text(
                    json.dumps(p2_bundle, indent=2, default=str),
                    encoding="utf-8",
                )
                _attach_terminal_step(
                    merged,
                    "phase2_backtest_jobs",
                    status="failed",
                    error_code="E_NO_DATA_WINDOW",
                    message=(
                        f"backtest_metrics missing PAT@1% field model_default.{pr_key} "
                        f"at {mpath}"
                    ),
                )
                merged["steps"]["phase2_backtest_jobs"]["started_at"] = t_bj
                _write_run_state(state_file, merged)
                print(
                    f"phase2_backtest_jobs: E_NO_DATA_WINDOW — missing "
                    f"model_default.{pr_key} at {mpath}",
                    file=sys.stderr,
                )
                return phase2_exits.EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE
            p2_bundle["backtest_metrics"] = metrics_obj
            p2_bundle["backtest_metrics_path"] = str(mpath)
            p2_bundle["status"] = "metrics_ingested"
            p2_bundle["backtest_jobs"] = {
                "executed": True,
                "skip_reason": None,
                "subprocess_ok": True,
                "metrics_loaded": True,
                "metrics_path": str(mpath),
                "finished_at": _utc_now_iso(),
            }
            merged["phase2_collect"] = (
                collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
            )
            bundle_path.write_text(
                json.dumps(p2_bundle, indent=2, default=str),
                encoding="utf-8",
            )
            _attach_terminal_step(
                merged,
                "phase2_backtest_jobs",
                status="success",
                message="T10: shared backtest + backtest_metrics ingest",
            )
            merged["steps"]["phase2_backtest_jobs"]["started_at"] = t_bj
            _write_run_state(state_file, merged)

    skip_gate_report = resume_ok and _step_success(prev_steps, "phase2_gate_report")
    if not skip_gate_report:
        t_gr = _mark_step_running(merged, "phase2_gate_report")
        _write_run_state(state_file, merged)
        merged_pat = collectors.merge_phase2_pat_series_from_shared_and_per_job(p2_bundle)
        if merged_pat:
            bundle_path.write_text(
                json.dumps(p2_bundle, indent=2, default=str),
                encoding="utf-8",
            )
            merged["phase2_collect"] = (
                collectors.collect_summary_phase2_plan_for_run_state(p2_bundle)
            )
            _write_run_state(state_file, merged)
        gate_p2 = evaluators.evaluate_phase2_gate(p2_bundle)
        gmet = (
            gate_p2.get("metrics")
            if isinstance(gate_p2.get("metrics"), Mapping)
            else {}
        )
        merged["phase2_gate_decision"] = {
            "status": gate_p2.get("status"),
            "blocking_reasons": list(gate_p2.get("blocking_reasons") or []),
            "evidence_summary": str(gate_p2.get("evidence_summary") or ""),
            "conclusion_strength": gate_p2.get("conclusion_strength"),
            "phase2_strategy_effective": gmet.get("phase2_strategy_effective"),
            "phase2_trainer_jobs_executed": gmet.get("phase2_trainer_jobs_executed"),
            "phase2_winner_track": gmet.get("phase2_winner_track"),
            "phase2_winner_exp_id": gmet.get("phase2_winner_exp_id"),
            "phase2_winner_baseline_exp_id": gmet.get("phase2_winner_baseline_exp_id"),
            "phase2_winner_uplift_pp_vs_baseline": gmet.get(
                "phase2_winner_uplift_pp_vs_baseline"
            ),
            "phase2_elimination_row_count": (
                len(gmet["phase2_elimination_rows"])
                if isinstance(gmet.get("phase2_elimination_rows"), list)
                else None
            ),
        }
        gate_md = phase2_dir / "phase2_gate_decision.md"
        report_builder.write_phase2_gate_decision(
            gate_md, args.run_id, cfg, p2_bundle, gate_p2
        )
        track_mds = report_builder.write_phase2_track_results(
            phase2_dir, args.run_id, cfg, p2_bundle, gate_p2
        )
        tr_art: dict[str, str] = {
            f"phase2_{p.stem}": str(p.resolve()) for p in track_mds
        }
        tr_art["phase2_gate_decision"] = str(gate_md.resolve())
        fail_on_gf = bool(getattr(args, "phase2_fail_on_gate_fail", False))
        fail_on_gb = bool(getattr(args, "phase2_fail_on_gate_blocked", False))
        gate_exit = phase2_gate_cli_exit_code(
            gate_p2,
            fail_on_gate_fail=fail_on_gf,
            fail_on_gate_blocked=fail_on_gb,
        )
        if gate_exit is not None:
            ev_msg = str(gate_p2.get("evidence_summary") or "")
            gst = str(gate_p2.get("status") or "")
            err_c = (
                "E_PHASE2_GATE_FAIL"
                if gst == "FAIL"
                else "E_PHASE2_GATE_BLOCKED"
            )
            msg_def = (
                "evaluate_phase2_gate returned FAIL"
                if gst == "FAIL"
                else "evaluate_phase2_gate returned BLOCKED"
            )
            _attach_terminal_step(
                merged,
                "phase2_gate_report",
                status="failed",
                artifacts=tr_art,
                error_code=err_c,
                message=(ev_msg[:2000] if ev_msg else msg_def),
            )
        else:
            _attach_terminal_step(
                merged,
                "phase2_gate_report",
                status="success",
                artifacts=tr_art,
                message=(
                    "T11: evaluate_phase2_gate + phase2_gate_decision.md + "
                    "track_*_results.md"
                ),
            )
        merged["steps"]["phase2_gate_report"]["started_at"] = t_gr
        _write_run_state(state_file, merged)
        if gate_exit is not None:
            art_fail: dict[str, str] = {
                "run_state": str(state_file.resolve()),
                "config_path": str(config_path.resolve()),
                "phase2_dir": str(phase2_dir.resolve()),
            }
            p2_bpf = merged.get("phase2_bundle_path")
            if isinstance(p2_bpf, str) and p2_bpf.strip():
                art_fail["phase2_bundle"] = p2_bpf.strip()
            gate_md_fail = phase2_dir / "phase2_gate_decision.md"
            if gate_md_fail.is_file():
                art_fail["phase2_gate_decision"] = str(gate_md_fail.resolve())
            for stem in ("track_a_results", "track_b_results", "track_c_results"):
                trp_f = phase2_dir / f"{stem}.md"
                if trp_f.is_file():
                    art_fail[f"phase2_{stem}"] = str(trp_f.resolve())
            merged["artifacts"] = art_fail
            merged["updated_at"] = _utc_now_iso()
            _write_run_state(state_file, merged)
            print(
                gate_p2.get("evidence_summary")
                or (
                    "phase2 gate FAIL"
                    if str(gate_p2.get("status") or "") == "FAIL"
                    else "phase2 gate BLOCKED"
                ),
                file=sys.stderr,
            )
            return gate_exit

    art: dict[str, str] = {
        "run_state": str(state_file.resolve()),
        "config_path": str(config_path.resolve()),
        "phase2_dir": str(phase2_dir.resolve()),
    }
    p2_bp = merged.get("phase2_bundle_path")
    if isinstance(p2_bp, str) and p2_bp.strip():
        art["phase2_bundle"] = p2_bp.strip()
    gate_md_path = phase2_dir / "phase2_gate_decision.md"
    if gate_md_path.is_file():
        art["phase2_gate_decision"] = str(gate_md_path.resolve())
    for stem in ("track_a_results", "track_b_results", "track_c_results"):
        trp = phase2_dir / f"{stem}.md"
        if trp.is_file():
            art[f"phase2_{stem}"] = str(trp.resolve())
    merged["artifacts"] = art
    merged["updated_at"] = _utc_now_iso()
    _write_run_state(state_file, merged)
    return 0


def _main_all(args: argparse.Namespace, config_path: Path) -> int:
    """All-phase root config (``run_full.yaml``): dry-run readiness only (T16A)."""
    if not args.dry_run:
        print(
            "orchestrator: --phase all requires --dry-run "
            "(full autonomous execution is not implemented yet)",
            file=sys.stderr,
        )
        return 2

    if args.collect_only:
        print(
            "orchestrator: --phase all does not support --collect-only",
            file=sys.stderr,
        )
        return 2

    try:
        rf_cfg = config_loader.load_run_full_config(
            config_path, cli_run_id=args.run_id
        )
    except config_loader.ConfigValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    pc = rf_cfg["phase_configs"]
    resolved_paths: dict[str, Path] = {
        k: _resolve_config_path(_REPO_ROOT, str(pc[k]))
        for k in ("phase1", "phase2", "phase3", "phase4")
    }

    state_file = _state_path(args.run_id)
    prev_state = _load_run_state(state_file) if args.resume else None
    input_summary = build_run_full_input_summary(
        rf_cfg, config_path, resolved_paths
    )
    prev_fp = (prev_state.get("input_summary") or {}).get("fingerprint") if prev_state else None
    fingerprint_mismatch = bool(
        args.resume
        and prev_state is not None
        and prev_fp != input_summary["fingerprint"]
    )
    if fingerprint_mismatch:
        print(
            "orchestrator: resume invalid — config fingerprint differs from run_state "
            "(or was missing); will re-run preflight and eligible pipeline steps.",
            file=sys.stderr,
        )

    skip_preflight = bool(
        args.resume
        and prev_state
        and not fingerprint_mismatch
        and str(prev_state.get("phase") or "") == "all"
        and (prev_state.get("steps") or {}).get("preflight", {}).get("status")
        == "success"
    )

    if skip_preflight:
        preflight = {
            "ok": True,
            "error_code": None,
            "message": "skipped (resume: preflight already success)",
            "checks": [],
        }
    else:
        try:
            cfg1 = config_loader.load_phase1_config(resolved_paths["phase1"])
        except config_loader.ConfigValidationError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        preflight = runner.run_preflight(
            _REPO_ROOT,
            cfg1,
            skip_backtest_smoke=args.skip_backtest_smoke,
        )

    merged = _merge_state(prev_state, args.run_id, "all", input_summary, preflight)
    if fingerprint_mismatch:
        merged["resume_invalidated"] = "config_fingerprint_mismatch"
    else:
        merged.pop("resume_invalidated", None)
    _write_run_state(state_file, merged)

    if not preflight.get("ok"):
        print(preflight.get("message") or "preflight failed", file=sys.stderr)
        return 3

    readiness = run_all_phases_dry_run_readiness(
        _REPO_ROOT,
        _ORCHESTRATOR_DIR,
        args.run_id,
        rf_cfg,
        resolved_paths,
        skip_backtest_smoke=args.skip_backtest_smoke,
        skip_phase1_preflight=True,
        skip_phase2_preflight=skip_preflight,
    )
    merged["mode"] = "dry_run"
    merged["readiness"] = readiness
    merged["artifacts"] = {
        "run_state": str(state_file.resolve()),
        "config_path": str(config_path.resolve()),
        **readiness.get("artifacts", {}),
    }
    _attach_terminal_step(
        merged,
        "dry_run_readiness",
        status="success" if readiness["status"] == "READY" else "failed",
        artifacts=readiness.get("artifacts"),
        error_code=(
            None if readiness["status"] == "READY" else "E_DRY_RUN_NOT_READY"
        ),
        message="; ".join(readiness.get("blocking_reasons", [])),
    )
    merged["updated_at"] = _utc_now_iso()
    _write_run_state(state_file, merged)
    if readiness["status"] != "READY":
        print(
            "dry-run NOT_READY: " + ", ".join(readiness["blocking_reasons"]),
            file=sys.stderr,
        )
        return 6
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns process exit code."""
    args = _parse_args(argv)
    config_path = args.config
    if not config_path.is_absolute():
        config_path = (_REPO_ROOT / config_path).resolve()

    if args.phase == "phase1":
        return _main_phase1(args, config_path)
    if args.phase == "phase2":
        return _main_phase2(args, config_path)
    if args.phase == "all":
        return _main_all(args, config_path)

    print(
        f"E_CONFIG_INVALID: unsupported --phase {args.phase!r} "
        "(supported: phase1, phase2, all)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
