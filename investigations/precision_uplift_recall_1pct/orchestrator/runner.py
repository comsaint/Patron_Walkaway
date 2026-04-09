"""Preflight checks and subprocess runners for Phase 1."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PREDICTION_DB_REQUIRED_TABLES: tuple[str, ...] = ("prediction_log",)
STATE_DB_REQUIRED_TABLES: tuple[str, ...] = ("alerts", "validation_results")

DEFAULT_R1_R6_SCRIPT = "investigations/test_vs_production/checks/run_r1_r6_analysis.py"
_LOG_TAIL_CHARS = 4000


def resolve_r1_r6_script(repo_root: Path, cfg: Mapping[str, Any]) -> Path:
    """Return absolute path to ``run_r1_r6_analysis.py`` from config or default."""
    raw = str(cfg.get("r1_r6_script") or DEFAULT_R1_R6_SCRIPT).strip()
    return _resolve_path(repo_root, raw)


def classify_r1_r6_failure(combined_text: str, returncode: int) -> tuple[str, str]:
    """Map R1/R6 stderr/stdout to orchestrator error codes (non-zero exit only)."""
    text = (combined_text or "").lower()
    msg = (combined_text or "").strip()
    if len(msg) > _LOG_TAIL_CHARS:
        msg = msg[-_LOG_TAIL_CHARS:]
    if not msg:
        msg = f"r1_r6 process exited with code {returncode}"
    if "prediction_log table not found" in text:
        return "E_ARTIFACT_MISSING", msg
    if "filenotfounderror" in text or "no such file or directory" in text:
        return "E_ARTIFACT_MISSING", msg
    if "no bets fetched from clickhouse" in text:
        return "E_NO_DATA_WINDOW", msg
    if "sample csv contains no bet_id rows" in text or "no labeled rows matched" in text:
        return "E_EMPTY_SAMPLE", msg
    if "fetched bets do not map to canonical_id" in text:
        return "E_EMPTY_SAMPLE", msg
    if "no player/canonical mapping found" in text:
        return "E_EMPTY_SAMPLE", msg
    return "E_EMPTY_SAMPLE", msg


def classify_backtest_failure(combined_text: str, returncode: int) -> tuple[str, str]:
    """Map backtester stderr/stdout to orchestrator error codes (non-zero exit only)."""
    text = (combined_text or "").lower()
    msg = (combined_text or "").strip()
    if len(msg) > _LOG_TAIL_CHARS:
        msg = msg[-_LOG_TAIL_CHARS:]
    if not msg:
        msg = f"backtest process exited with code {returncode}"
    if "no bets for the requested window" in text:
        return "E_NO_DATA_WINDOW", msg
    if "filenotfounderror" in text or "no such file or directory" in text:
        return "E_ARTIFACT_MISSING", msg
    if "model.pkl" in text and ("missing" in text or "not found" in text):
        return "E_ARTIFACT_MISSING", msg
    return "E_NO_DATA_WINDOW", msg


def run_logged_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    log_dir: Path,
    log_stem: str,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Run a subprocess with stdout/stderr captured to files under ``log_dir``.

    Args:
        argv: Command and arguments (no shell).
        cwd: Working directory (typically repo root).
        log_dir: Directory for ``{log_stem}.stdout.log`` and ``.stderr.log``.
        log_stem: Filename stem for log files.
        timeout_sec: Optional timeout in seconds (``None`` = none).

    Returns:
        Dict with returncode, paths, and captured text fields.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{log_stem}.stdout.log"
    stderr_path = log_dir / f"{log_stem}.stderr.log"
    try:
        with open(stdout_path, "w", encoding="utf-8", newline="") as fo, open(
            stderr_path, "w", encoding="utf-8", newline=""
        ) as fe:
            proc = subprocess.run(
                list(argv),
                cwd=cwd,
                stdout=fo,
                stderr=fe,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        tail = f"command timeout after {timeout_sec}s: {argv[0]!r}"
        return {
            "ok": False,
            "returncode": -1,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "stdout_text": "",
            "stderr_text": tail,
            "combined_text": tail,
            "error_code": "E_NO_DATA_WINDOW",
            "message": tail,
        }
    except OSError as exc:
        tail = f"failed to run subprocess: {exc}"
        return {
            "ok": False,
            "returncode": -1,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "stdout_text": "",
            "stderr_text": tail,
            "combined_text": tail,
            "error_code": "E_ARTIFACT_MISSING",
            "message": tail,
        }
    out_txt = stdout_path.read_text(encoding="utf-8", errors="replace")
    err_txt = stderr_path.read_text(encoding="utf-8", errors="replace")
    combined = f"{out_txt}\n{err_txt}"
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_text": out_txt,
        "stderr_text": err_txt,
        "combined_text": combined,
        "error_code": None,
        "message": None,
    }


def _wrap_classified(
    base: dict[str, Any],
    classify_fn: Callable[[str, int], tuple[str, str]],
) -> dict[str, Any]:
    """If command failed, set error_code and message using ``classify_fn``."""
    if base.get("ok"):
        return base
    code, msg = classify_fn(base.get("combined_text", ""), int(base.get("returncode", -1)))
    out = dict(base)
    out["error_code"] = code
    out["message"] = msg
    return out


def run_phase1_r1_r6_all(
    repo_root: Path,
    cfg: Mapping[str, Any],
    log_dir: Path,
    *,
    python_exe: str | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Run ``run_r1_r6_analysis.py --mode all --pretty`` with DB paths from config.

    Args:
        repo_root: Repository root (cwd).
        cfg: Validated Phase 1 config.
        log_dir: Directory for stdout/stderr logs.
        python_exe: Python interpreter; default ``sys.executable``.
        timeout_sec: Optional wall-clock timeout.

    Returns:
        Result dict with ok, error_code, message, log paths, subprocess returncode.
    """
    exe = python_exe or sys.executable
    script = resolve_r1_r6_script(repo_root, cfg)
    if not script.is_file():
        msg = f"r1_r6 script not found: {script}"
        return {
            "ok": False,
            "returncode": -1,
            "stdout_path": log_dir / "r1_r6.stdout.log",
            "stderr_path": log_dir / "r1_r6.stderr.log",
            "error_code": "E_ARTIFACT_MISSING",
            "message": msg,
        }

    model_dir = _resolve_path(repo_root, str(cfg["model_dir"]))
    state_db = _resolve_path(repo_root, str(cfg["state_db_path"]))
    pred_db = _resolve_path(repo_root, str(cfg["prediction_log_db_path"]))
    w = cfg["window"]

    argv: list[str] = [
        exe,
        str(script),
        "--mode",
        "all",
        "--pretty",
        "--start-ts",
        str(w["start_ts"]),
        "--end-ts",
        str(w["end_ts"]),
        "--pred-db-path",
        str(pred_db),
        "--state-db-path",
        str(state_db),
        "--model-dir",
        str(model_dir),
    ]
    base = run_logged_command(
        argv,
        cwd=repo_root,
        log_dir=log_dir,
        log_stem="r1_r6",
        timeout_sec=timeout_sec,
    )
    if not base.get("ok") and base.get("error_code"):
        return base
    return _wrap_classified(base, classify_r1_r6_failure)


def run_phase1_backtest(
    repo_root: Path,
    cfg: Mapping[str, Any],
    log_dir: Path,
    *,
    python_exe: str | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Run ``python -m trainer.backtester`` using window and model_dir from config.

    Args:
        repo_root: Repository root (cwd).
        cfg: Validated Phase 1 config.
        log_dir: Log directory.
        python_exe: Python interpreter.
        timeout_sec: Optional timeout.

    Returns:
        Result dict with ok, error_code, message, and log paths.
    """
    exe = python_exe or sys.executable
    model_dir = _resolve_path(repo_root, str(cfg["model_dir"]))
    w = cfg["window"]
    argv: list[str] = [
        exe,
        "-m",
        "trainer.backtester",
        "--start",
        str(w["start_ts"]),
        "--end",
        str(w["end_ts"]),
        "--model-dir",
        str(model_dir),
    ]
    if bool(cfg.get("backtest_skip_optuna", True)):
        argv.append("--skip-optuna")
    extras = cfg.get("backtest_extra_args")
    if isinstance(extras, list):
        argv.extend(str(x) for x in extras)

    base = run_logged_command(
        argv,
        cwd=repo_root,
        log_dir=log_dir,
        log_stem="backtest",
        timeout_sec=timeout_sec,
    )
    if not base.get("ok") and base.get("error_code"):
        return base
    return _wrap_classified(base, classify_backtest_failure)


def _resolve_path(repo_root: Path, p: str | Path) -> Path:
    """Resolve a config path relative to repo root when not absolute."""
    path = Path(p)
    return path if path.is_absolute() else (repo_root / path)


def _sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    """Return base table names in sqlite_master (rows where type='table')."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _check_paths_exist(
    repo_root: Path, cfg: Mapping[str, Any]
) -> tuple[bool, str | None]:
    """Verify model_dir exists and both DB paths exist."""
    model_dir = _resolve_path(repo_root, str(cfg["model_dir"]))
    if not model_dir.is_dir():
        return False, f"model_dir is not a directory: {model_dir}"
    state_db = _resolve_path(repo_root, str(cfg["state_db_path"]))
    if not state_db.is_file():
        return False, f"state_db_path not found: {state_db}"
    pred_db = _resolve_path(repo_root, str(cfg["prediction_log_db_path"]))
    if not pred_db.is_file():
        return False, f"prediction_log_db_path not found: {pred_db}"
    return True, None


def _check_prediction_db(pred_db: Path) -> tuple[bool, str | None]:
    """Open prediction_log DB and assert required tables exist."""
    try:
        conn = sqlite3.connect(f"file:{pred_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"cannot open prediction DB {pred_db}: {exc}"
    try:
        tables = _sqlite_tables(conn)
        missing = [t for t in PREDICTION_DB_REQUIRED_TABLES if t not in tables]
        if missing:
            return False, f"prediction DB missing tables {missing}; have {sorted(tables)}"
    finally:
        conn.close()
    return True, None


def _check_state_db(state_db: Path) -> tuple[bool, str | None]:
    """Open state DB and assert alerts + validation_results exist."""
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"cannot open state DB {state_db}: {exc}"
    try:
        tables = _sqlite_tables(conn)
        missing = [t for t in STATE_DB_REQUIRED_TABLES if t not in tables]
        if missing:
            return False, f"state DB missing tables {missing}; have {sorted(tables)}"
    finally:
        conn.close()
    return True, None


def run_backtest_cli_smoke(repo_root: Path, python_exe: str | None = None) -> tuple[bool, str | None]:
    """Run `python -m trainer.backtester --help` to ensure the CLI is importable.

    Args:
        repo_root: Repository root (cwd for subprocess).
        python_exe: Interpreter; defaults to ``sys.executable``.

    Returns:
        (ok, error_message).
    """
    exe = python_exe or sys.executable
    try:
        proc = subprocess.run(
            [exe, "-m", "trainer.backtester", "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "backtester smoke: subprocess timeout"
    except OSError as exc:
        return False, f"backtester smoke: failed to spawn process: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return False, f"backtester smoke exit {proc.returncode}: {tail}"
    return True, None


def run_r1_r6_cli_smoke(
    repo_root: Path,
    cfg: Mapping[str, Any],
    python_exe: str | None = None,
) -> tuple[bool, str | None]:
    """Run ``run_r1_r6_analysis.py --help`` to ensure command bootstraps.

    Args:
        repo_root: Repository root (cwd for subprocess).
        cfg: Validated config, optionally containing ``r1_r6_script``.
        python_exe: Interpreter; defaults to ``sys.executable``.

    Returns:
        (ok, error_message).
    """
    exe = python_exe or sys.executable
    script = resolve_r1_r6_script(repo_root, cfg)
    if not script.is_file():
        return False, f"r1_r6 script not found: {script}"
    try:
        proc = subprocess.run(
            [exe, str(script), "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "r1_r6 smoke: subprocess timeout"
    except OSError as exc:
        return False, f"r1_r6 smoke: failed to spawn process: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return False, f"r1_r6 smoke exit {proc.returncode}: {tail}"
    return True, None


def run_preflight(
    repo_root: Path,
    cfg: Mapping[str, Any],
    *,
    skip_backtest_smoke: bool = False,
    python_exe: str | None = None,
) -> dict[str, Any]:
    """Run all Phase 1 preflight checks.

    Args:
        repo_root: Repository root for resolving relative paths and subprocess cwd.
        cfg: Validated Phase 1 config mapping.
        skip_backtest_smoke: If True, skip ``trainer.backtester`` help invocation.
        python_exe: Python executable for smoke test.

    Returns:
        Dict with keys ok (bool), error_code (str | None), message (str | None),
        checks (list of per-step records).
    """
    checks: list[dict[str, Any]] = []

    ok, msg = _check_paths_exist(repo_root, cfg)
    checks.append({"name": "paths", "ok": ok, "message": msg})
    if not ok:
        return {
            "ok": False,
            "error_code": "E_ARTIFACT_MISSING",
            "message": msg,
            "checks": checks,
        }

    state_db = _resolve_path(repo_root, str(cfg["state_db_path"]))
    pred_db = _resolve_path(repo_root, str(cfg["prediction_log_db_path"]))

    ok, msg = _check_prediction_db(pred_db)
    checks.append({"name": "prediction_db_tables", "ok": ok, "message": msg})
    if not ok:
        return {
            "ok": False,
            "error_code": "E_DB_UNAVAILABLE",
            "message": msg,
            "checks": checks,
        }

    ok, msg = _check_state_db(state_db)
    checks.append({"name": "state_db_tables", "ok": ok, "message": msg})
    if not ok:
        return {
            "ok": False,
            "error_code": "E_DB_UNAVAILABLE",
            "message": msg,
            "checks": checks,
        }

    if not skip_backtest_smoke:
        ok, msg = run_backtest_cli_smoke(repo_root, python_exe=python_exe)
        checks.append({"name": "backtester_cli_smoke", "ok": ok, "message": msg})
        if not ok:
            return {
                "ok": False,
                "error_code": "E_BACKTEST_CLI",
                "message": msg,
                "checks": checks,
            }

    return {"ok": True, "error_code": None, "message": None, "checks": checks}
