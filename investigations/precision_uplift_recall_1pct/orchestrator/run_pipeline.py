#!/usr/bin/env python3
"""Phase 1 orchestrator CLI (MVP T1–T2: config, preflight, run_state)."""

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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and parse CLI arguments."""
    p = argparse.ArgumentParser(description="Precision uplift Phase 1 orchestrator (MVP).")
    p.add_argument("--phase", required=True, help="Only phase1 is supported in MVP")
    p.add_argument("--config", type=Path, required=True, help="Path to Phase 1 YAML config")
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

    status = "READY" if not blocking else "NOT_READY"
    return {
        "status": status,
        "checks": checks,
        "blocking_reasons": blocking,
        "artifacts": writable_artifacts,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns process exit code."""
    args = _parse_args(argv)
    if args.phase != "phase1":
        print(
            f"E_CONFIG_INVALID: unsupported --phase {args.phase!r} (MVP supports phase1 only)",
            file=sys.stderr,
        )
        return 2

    config_path = args.config
    if not config_path.is_absolute():
        config_path = (_REPO_ROOT / config_path).resolve()

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


if __name__ == "__main__":
    raise SystemExit(main())
