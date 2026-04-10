#!/usr/bin/env python3
"""Precision uplift orchestrator CLI (Phase 1 MVP + Phase 2 scaffold T9)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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
import report_builder  # noqa: E402
import runner  # noqa: E402


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    """Atomically write run state JSON (best-effort)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


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
        skip_r1 = resume_ok and _step_success(prev_steps, "r1_r6_analysis")
        skip_bt = resume_ok and _step_success(prev_steps, "backtest")

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
    """Phase 2 scaffold (T9): validate config, preflight paths/DBs, write run_state.

    Track runner / collectors / gate / reports are deferred to T10–T11.
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
                "track runner / phase2_bundle / reports deferred to T10–T11"
            ),
        )
        merged["steps"]["phase2_scaffold"]["started_at"] = t0
        _write_run_state(state_file, merged)

    merged["artifacts"] = {
        "run_state": str(state_file.resolve()),
        "config_path": str(config_path.resolve()),
        "phase2_dir": str(phase2_dir.resolve()),
    }
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
