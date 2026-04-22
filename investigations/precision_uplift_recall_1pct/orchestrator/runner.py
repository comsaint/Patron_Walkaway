"""Preflight checks and subprocess runners for Phase 1 and Phase 2 (T10)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import collectors
import evaluators

PREDICTION_DB_REQUIRED_TABLES: tuple[str, ...] = ("prediction_log",)
STATE_DB_REQUIRED_TABLES: tuple[str, ...] = ("alerts", "validation_results")

DEFAULT_R1_R6_SCRIPT = "investigations/test_vs_production/checks/run_r1_r6_analysis.py"
_LOG_TAIL_CHARS = 4000
_TRAINER_ARTIFACTS_SAVED_RE = re.compile(
    r"Artifacts saved to\s+(.+?)\s+\(version=",
    re.MULTILINE,
)
# Contract: must stay aligned with ``logger.info`` in
# ``trainer.training.trainer.save_artifact_bundle`` (see that file).
TRAINER_ARTIFACTS_SAVED_LOGGER_INFO_FORMAT = (
    'logger.info("Artifacts saved to %s  (version=%s)", _out, model_version)'
)
_TRAINER_LOG_TAIL_BYTES = 512_000


def _tail_file_text(path: Path, max_bytes: int) -> str:
    """Read a text file, or only the last ``max_bytes`` (UTF-8, replace errors)."""
    if not path.is_file():
        return ""
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def infer_training_metrics_repo_relative_from_trainer_logs(
    repo_root: Path,
    *,
    stdout_path: str | Path,
    stderr_path: str | Path,
    max_scan_bytes: int = _TRAINER_LOG_TAIL_BYTES,
) -> str | None:
    """Parse trainer stdout/stderr tail for ``save_artifact_bundle`` log line.

    Matches ``trainer.training.trainer`` logger line
    ``Artifacts saved to <dir>  (version=...)``.

    Args:
        repo_root: Repository root (artifact path must resolve under it).
        stdout_path: Path to captured trainer stdout log.
        stderr_path: Path to captured trainer stderr log (trainer often logs here).
        max_scan_bytes: Max bytes read from the end of each file.

    Returns:
        POSIX path relative to ``repo_root`` for the bundle directory containing
        ``training_metrics.json``, or None if not found or outside the repo.
    """
    text = "\n".join(
        (
            _tail_file_text(Path(stdout_path), max_scan_bytes),
            _tail_file_text(Path(stderr_path), max_scan_bytes),
        )
    )
    matches = list(_TRAINER_ARTIFACTS_SAVED_RE.finditer(text))
    if not matches:
        return None
    raw = matches[-1].group(1).strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = (repo_root / p).resolve()
    else:
        p = p.resolve()
    root = repo_root.resolve()
    try:
        rel = p.relative_to(root).as_posix()
    except ValueError:
        return None
    return rel


def merge_inferred_training_metrics_paths_into_phase2_bundle(
    bundle: dict[str, Any],
    repo_root: Path,
) -> None:
    """Set ``job_specs[].training_metrics_repo_relative`` from successful trainer jobs.

    Only fills specs that do not already define ``training_metrics_repo_relative``.
    Inference uses per-job ``trainer_jobs.results[].inferred_training_metrics_repo_relative``.
    """
    tj = bundle.get("trainer_jobs")
    if not isinstance(tj, Mapping):
        return
    results = tj.get("results")
    if not isinstance(results, list):
        return
    specs = bundle.get("job_specs")
    if not isinstance(specs, list):
        return
    by_key: dict[tuple[str, str], str] = {}
    for r in results:
        if not isinstance(r, Mapping) or not r.get("ok"):
            continue
        tr = str(r.get("track") or "").strip()
        eid = str(r.get("exp_id") or "").strip()
        inf = r.get("inferred_training_metrics_repo_relative")
        if not tr or not eid:
            continue
        if isinstance(inf, str) and inf.strip():
            # Re-validate under repo (defense in depth).
            safe, _err = _safe_training_metrics_hint(repo_root, inf.strip())
            if safe:
                by_key[(tr, eid)] = safe
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        if str(spec.get("training_metrics_repo_relative") or "").strip():
            continue
        tr = str(spec.get("track") or "").strip()
        eid = str(spec.get("exp_id") or "").strip()
        got = by_key.get((tr, eid))
        if got:
            spec["training_metrics_repo_relative"] = got


def _safe_training_metrics_hint(repo_root: Path, rel: str) -> tuple[str | None, str | None]:
    """Return canonical repo-relative POSIX path or ``(None, error)`` if invalid."""
    base, err = collectors._safe_resolve_under_repo_root(repo_root, rel)
    if err:
        return None, err
    assert base is not None
    return base.relative_to(repo_root.resolve()).as_posix(), None


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
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run a subprocess with stdout/stderr captured to files under ``log_dir``.

    Args:
        argv: Command and arguments (no shell).
        cwd: Working directory (typically repo root).
        log_dir: Directory for ``{log_stem}.stdout.log`` and ``.stderr.log``.
        log_stem: Filename stem for log files.
        timeout_sec: Optional timeout in seconds (``None`` = none).
        env: Optional full environment for the child (e.g. aligned ``MODEL_DIR``);
            when ``None``, the child inherits the current process environment.

    Returns:
        Dict with returncode, paths, and captured text fields. On
        ``subprocess.TimeoutExpired``, ``error_code`` is ``E_SUBPROCESS_TIMEOUT``
        (distinct from backtest ``E_NO_DATA_WINDOW`` / empty-window cases).
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{log_stem}.stdout.log"
    stderr_path = log_dir / f"{log_stem}.stderr.log"
    argv_preview = list(argv)
    if len(argv_preview) > 12:
        preview = " ".join(shlex.quote(str(a)) for a in argv_preview[:12]) + " ..."
    else:
        preview = " ".join(shlex.quote(str(a)) for a in argv_preview)
    to_msg = f" timeout={timeout_sec}s" if timeout_sec is not None else ""
    print(
        f"[precision_uplift_orchestrator] subprocess: cwd={cwd} "
        f"stdout={stdout_path.name} stderr={stderr_path.name}{to_msg}\n"
        f"[precision_uplift_orchestrator]   cmd: {preview}",
        file=sys.stderr,
        flush=True,
    )
    run_kw: dict[str, Any] = {
        "cwd": cwd,
        "stdout": None,
        "stderr": None,
        "text": True,
        "timeout": timeout_sec,
        "check": False,
    }
    if env is not None:
        run_kw["env"] = dict(env)
    try:
        with open(stdout_path, "w", encoding="utf-8", newline="") as fo, open(
            stderr_path, "w", encoding="utf-8", newline=""
        ) as fe:
            run_kw["stdout"] = fo
            run_kw["stderr"] = fe
            proc = subprocess.run(list(argv), **run_kw)
    except subprocess.TimeoutExpired:
        tail = f"command timeout after {timeout_sec}s: {argv[0]!r}"
        print(
            f"[precision_uplift_orchestrator] subprocess TIMEOUT: {tail} log_dir={log_dir}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "ok": False,
            "returncode": -1,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "stdout_text": "",
            "stderr_text": tail,
            "combined_text": tail,
            "error_code": "E_SUBPROCESS_TIMEOUT",
            "message": tail,
        }
    except OSError as exc:
        tail = f"failed to run subprocess: {exc}"
        print(
            f"[precision_uplift_orchestrator] subprocess OS error: {tail} log_dir={log_dir}",
            file=sys.stderr,
            flush=True,
        )
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
    status = "ok" if ok else "FAILED"
    print(
        f"[precision_uplift_orchestrator] subprocess {status}: "
        f"returncode={proc.returncode} log_dir={log_dir}",
        file=sys.stderr,
        flush=True,
    )
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
    window_override: Mapping[str, Any] | None = None,
    log_stem: str = "r1_r6",
) -> dict[str, Any]:
    """Run ``run_r1_r6_analysis.py --mode all --pretty`` with DB paths from config.

    Args:
        repo_root: Repository root (cwd).
        cfg: Validated Phase 1 config.
        log_dir: Directory for stdout/stderr logs.
        python_exe: Python interpreter; default ``sys.executable``.
        timeout_sec: Optional wall-clock timeout.
        window_override: Optional ``{"start_ts","end_ts"}`` mapping used instead of
            ``cfg["window"]``. Useful for mid-snapshot checkpoints.
        log_stem: Filename stem for subprocess logs (default ``r1_r6``).

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
    w_raw = window_override if isinstance(window_override, Mapping) else cfg["window"]
    w = {
        "start_ts": str(w_raw["start_ts"]),
        "end_ts": str(w_raw["end_ts"]),
    }

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
    child_env = _subprocess_env_align_model_dir_bundle(
        repo_root, config_model_dir=str(cfg["model_dir"]).strip()
    )
    base = run_logged_command(
        argv,
        cwd=repo_root,
        log_dir=log_dir,
        log_stem=log_stem,
        timeout_sec=timeout_sec,
        env=child_env,
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
        cfg: Validated Phase 1 config. Optional ``backtest_output_dir`` adds
            ``--output-dir`` so metrics land outside the default trainer out dir.
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
    bod = cfg.get("backtest_output_dir")
    if bod is not None and str(bod).strip():
        out_p = Path(str(bod).strip())
        out_p = out_p if out_p.is_absolute() else (repo_root / out_p)
        argv.extend(["--output-dir", str(out_p.resolve())])

    child_env = _subprocess_env_align_model_dir_bundle(
        repo_root, config_model_dir=str(cfg["model_dir"]).strip()
    )
    base = run_logged_command(
        argv,
        cwd=repo_root,
        log_dir=log_dir,
        log_stem="backtest",
        timeout_sec=timeout_sec,
        env=child_env,
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


def _subprocess_env_align_model_dir_bundle(
    repo_root: Path, *, config_model_dir: str | None
) -> dict[str, str]:
    """Build a child env where ``MODEL_DIR`` matches a versioned model bundle when possible.

    When *config_model_dir* is set (repo-relative path from orchestrator YAML, e.g.
    ``out/models/<version>``), sets ``MODEL_DIR`` to that directory's absolute path so
    ``trainer.features`` finds ``feature_spec.yaml`` inside the bundle.

    When *config_model_dir* is unset, if the parent ``MODEL_DIR`` points at a directory
    without ``feature_spec.yaml`` (typical when the shell sets ``MODEL_DIR=out/models``
    at the versions root), drops ``MODEL_DIR`` so the child uses repo
    ``features_candidates.yaml``.

    Args:
        repo_root: Repository root for resolving relative paths.
        config_model_dir: Bundle directory string from validated config, or ``None``.

    Returns:
        Full environment mapping for ``subprocess.run`` / ``run_logged_command``.
    """
    env: dict[str, str] = dict(os.environ)
    if isinstance(config_model_dir, str) and config_model_dir.strip():
        bd = _resolve_path(repo_root, config_model_dir.strip())
        env["MODEL_DIR"] = str(bd.resolve())
    else:
        raw = env.get("MODEL_DIR")
        if raw and str(raw).strip():
            rp = Path(str(raw).strip())
            if not rp.is_absolute():
                rp = (repo_root / rp).resolve()
            else:
                rp = rp.resolve()
            if not (rp / "feature_spec.yaml").is_file():
                env.pop("MODEL_DIR", None)
    if sys.platform == "win32":
        env.setdefault("PYTHONUTF8", "1")
    return env


def _subprocess_env_for_cli_help_smoke(
    repo_root: Path, *, config_model_dir: str | None
) -> dict[str, str]:
    """Build env for ``trainer.* --help`` subprocesses (aligned ``MODEL_DIR``, Windows UTF-8)."""
    env = _subprocess_env_align_model_dir_bundle(repo_root, config_model_dir=config_model_dir)
    if sys.platform == "win32":
        env.setdefault("PYTHONUTF8", "1")
    return env


def run_backtest_cli_smoke(
    repo_root: Path,
    python_exe: str | None = None,
    *,
    config_model_dir: str | None = None,
) -> tuple[bool, str | None]:
    """Run `python -m trainer.backtester --help` to ensure the CLI is importable.

    Args:
        repo_root: Repository root (cwd for subprocess).
        python_exe: Interpreter; defaults to ``sys.executable``.
        config_model_dir: When set, subprocess ``MODEL_DIR`` is forced to this bundle
            (repo-relative path as in orchestrator YAML). Mitigates a parent shell
            ``MODEL_DIR`` pointing at ``out/models`` without a root ``feature_spec.yaml``.

    Returns:
        (ok, error_message).
    """
    exe = python_exe or sys.executable
    smoke_env = _subprocess_env_for_cli_help_smoke(
        repo_root, config_model_dir=config_model_dir
    )
    try:
        proc = subprocess.run(
            [exe, "-m", "trainer.backtester", "--help"],
            cwd=repo_root,
            env=smoke_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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


def ensure_phase2_job_log_dirs(
    repo_root: Path,
    job_specs: object,
) -> tuple[bool, str | None]:
    """Create log directories for each Phase 2 ``job_specs`` entry (T10).

    Args:
        repo_root: Repository root; each ``logs_subdir_relative`` is resolved under it.
        job_specs: List of mappings with ``logs_subdir_relative`` (repo-relative POSIX
            path from ``collect_phase2_plan_bundle``, under the investigation
            ``orchestrator/state/<run_id>/logs/phase2/...`` tree).

    Returns:
        ``(True, None)`` on success, or ``(False, message)`` on first failure.
    """
    if not isinstance(job_specs, list):
        return True, None
    for i, spec in enumerate(job_specs):
        if not isinstance(spec, Mapping):
            continue
        rel = str(spec.get("logs_subdir_relative") or "").strip()
        if not rel:
            return False, f"job_specs[{i}] missing logs_subdir_relative"
        dest = (repo_root / rel).resolve()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"cannot mkdir {dest}: {exc}"
    return True, None


def run_trainer_trainer_help_smoke(
    repo_root: Path,
    python_exe: str | None = None,
    *,
    config_model_dir: str | None = None,
) -> tuple[bool, str | None]:
    """Run ``python -m trainer.trainer --help`` to verify training CLI imports (T10).

    Args:
        repo_root: Repository root (subprocess working directory).
        python_exe: Interpreter; defaults to ``sys.executable``.
        config_model_dir: When set, subprocess ``MODEL_DIR`` matches the phase2 bundle
            (repo-relative path from orchestrator YAML), avoiding a parent shell
            ``MODEL_DIR=out/models`` without ``feature_spec.yaml``.

    Returns:
        ``(True, None)`` on success, or ``(False, message)`` on failure.
    """
    exe = python_exe or sys.executable
    smoke_env = _subprocess_env_for_cli_help_smoke(
        repo_root, config_model_dir=config_model_dir
    )
    try:
        proc = subprocess.run(
            [exe, "-m", "trainer.trainer", "--help"],
            cwd=repo_root,
            env=smoke_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "trainer.trainer smoke: subprocess timeout"
    except OSError as exc:
        return False, f"trainer.trainer smoke: failed to spawn process: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return False, f"trainer.trainer smoke exit {proc.returncode}: {tail}"
    return True, None


def phase2_experiment_overrides(
    bundle: Mapping[str, Any],
    track: str,
    exp_id: str,
) -> dict[str, Any]:
    """Return experiment ``overrides`` dict for ``track`` / ``exp_id`` from a phase2 bundle."""
    tracks = bundle.get("tracks")
    if not isinstance(tracks, Mapping):
        return {}
    tnode = tracks.get(track)
    if not isinstance(tnode, Mapping):
        return {}
    exps = tnode.get("experiments")
    if not isinstance(exps, list):
        return {}
    for ex in exps:
        if not isinstance(ex, Mapping):
            continue
        if str(ex.get("exp_id") or "").strip() != exp_id:
            continue
        ov = ex.get("overrides")
        return dict(ov) if isinstance(ov, Mapping) else {}
    return {}


def phase2_experiment_trainer_params(
    bundle: Mapping[str, Any],
    track: str,
    exp_id: str,
) -> dict[str, Any]:
    """Return validated ``trainer_params`` for ``track`` / ``exp_id`` from a phase2 bundle (T10A)."""
    tracks = bundle.get("tracks")
    if not isinstance(tracks, Mapping):
        return {}
    tnode = tracks.get(track)
    if not isinstance(tnode, Mapping):
        return {}
    exps = tnode.get("experiments")
    if not isinstance(exps, list):
        return {}
    for ex in exps:
        if not isinstance(ex, Mapping):
            continue
        if str(ex.get("exp_id") or "").strip() != exp_id:
            continue
        tp = ex.get("trainer_params")
        return dict(tp) if isinstance(tp, Mapping) else {}
    return {}


def phase2_trainer_argv_fingerprint(argv: Sequence[str]) -> str:
    """Stable short fingerprint of trainer argv (from ``-m`` onward) for audit (T10A)."""
    lst = list(argv)
    try:
        mi = lst.index("-m")
        seq = lst[mi:]
    except ValueError:
        seq = lst
    blob = json.dumps(seq, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def build_phase2_trainer_argv(
    bundle: Mapping[str, Any],
    *,
    track: str,
    exp_id: str,
    python_exe: str | None = None,
) -> tuple[list[str], list[str]]:
    """Build ``python -m trainer.trainer`` argv for one Phase 2 job (T10 / T10A).

    Applies ``resources`` defaults, then per-experiment ``trainer_params`` (whitelist only;
    validated at config load). Non-empty legacy ``overrides`` in the bundle raise
    ``ValueError`` (stale plan artifact).

    Args:
        bundle: Phase 2 plan bundle (``common``, ``resources``, ``tracks``).
        track: Track name (e.g. ``track_a``).
        exp_id: Experiment id from config.
        python_exe: Interpreter; defaults to ``sys.executable``.

    Returns:
        ``(argv, unapplied)`` — ``unapplied`` is always ``[]`` (retained for call-site compat).
    """
    exe = python_exe or sys.executable
    common = bundle.get("common")
    if not isinstance(common, Mapping):
        raise ValueError("bundle['common'] must be a mapping")
    win = common.get("window")
    if not isinstance(win, Mapping):
        raise ValueError("bundle['common']['window'] must be a mapping")
    start = str(win.get("start_ts") or "").strip()
    end = str(win.get("end_ts") or "").strip()
    if not start or not end:
        raise ValueError("bundle common.window requires non-empty start_ts and end_ts")

    resources = (
        bundle["resources"] if isinstance(bundle.get("resources"), Mapping) else {}
    )
    ov = phase2_experiment_overrides(bundle, track, exp_id)
    if ov:
        bad = sorted(str(k) for k in ov.keys())
        raise ValueError(
            f"bundle has non-empty overrides for {track}/{exp_id}: {bad}; "
            "rebuild phase2_bundle from validated YAML (T10A disallows legacy overrides)"
        )

    tp = phase2_experiment_trainer_params(bundle, track, exp_id)

    argv: list[str] = [exe, "-m", "trainer.trainer", "--start", start, "--end", end]

    if "skip_optuna" in tp:
        skip_opt = bool(tp["skip_optuna"])
    else:
        skip_opt = bool(resources.get("backtest_skip_optuna"))
    if skip_opt:
        argv.append("--skip-optuna")

    if "use_local_parquet" in tp:
        use_lp = bool(tp["use_local_parquet"])
    else:
        use_lp = bool(resources.get("trainer_use_local_parquet"))
    if use_lp:
        argv.append("--use-local-parquet")

    if "recent_chunks" in tp:
        argv.extend(["--recent-chunks", str(int(tp["recent_chunks"]))])
    if "sample_rated" in tp:
        argv.extend(["--sample-rated", str(int(tp["sample_rated"]))])
    if "lgbm_device" in tp:
        dev = str(tp["lgbm_device"]).strip()
        argv.extend(["--lgbm-device", dev])

    return argv, []


def merge_phase2_field_test_precondition_into_trainer_env(
    repo_root: Path,
    bundle: Mapping[str, Any],
    child_env: dict[str, str],
) -> dict[str, Any]:
    """When YAML lists fold metrics (or globs), build precondition JSON and set trainer env (W1-C4).

    Precedence:
    1. ``resources.field_test_objective_fold_metrics_json`` — non-empty list of
       repo-relative paths (explicit).
    2. Else ``resources.field_test_objective_fold_metrics_globs`` — non-empty list of
       glob strings relative to repo root (e.g. ``investigations/**/fold_*.json``);
       expands to unique ``*.json`` under the repo (cap 32 by default, see trainer module).

    Optional:
    ``resources.field_test_precondition_production_neg_pos_ratio`` (default 20),
    ``resources.field_test_precondition_selection_mode`` (default ``field_test``).

    On failure or misconfiguration, logs to stderr and returns a manifest with
    ``applied: false`` without aborting trainer jobs.
    """
    from trainer.training.field_test_objective_precondition import (
        build_field_test_precondition_for_orchestration,
        expand_repo_relative_json_globs,
        trainer_env_updates_from_precondition_manifest,
    )

    manifest: dict[str, Any] = {"applied": False, "reason": "not_configured"}
    resources = bundle.get("resources")
    if not isinstance(resources, Mapping):
        return manifest

    common = bundle.get("common")
    if not isinstance(common, Mapping):
        manifest = {"applied": False, "reason": "bundle_common_missing"}
        return manifest
    win = common.get("window")
    if not isinstance(win, Mapping):
        manifest = {"applied": False, "reason": "bundle_common_window_missing"}
        return manifest
    start_ts = str(win.get("start_ts") or "").strip()
    end_ts = str(win.get("end_ts") or "").strip()
    if not start_ts or not end_ts:
        manifest = {"applied": False, "reason": "window_start_end_required"}
        print(
            "[precision_uplift_orchestrator] field_test precondition skipped: "
            "common.window start_ts/end_ts required",
            file=sys.stderr,
            flush=True,
        )
        return manifest

    rid = str(bundle.get("run_id") or "").strip()
    if not rid:
        manifest = {"applied": False, "reason": "bundle_run_id_missing"}
        return manifest

    raw_list = resources.get("field_test_objective_fold_metrics_json")
    rels: list[str] = []
    if raw_list is None:
        pass
    elif isinstance(raw_list, list):
        rels = [str(x).strip() for x in raw_list if str(x).strip()]
    else:
        manifest = {
            "applied": False,
            "reason": "field_test_objective_fold_metrics_json_must_be_list",
        }
        print(
            "[precision_uplift_orchestrator] field_test precondition skipped: "
            "resources.field_test_objective_fold_metrics_json must be a list when set",
            file=sys.stderr,
            flush=True,
        )
        return manifest

    abs_paths: list[Path] = []
    root = repo_root.resolve()
    glob_expand_meta: dict[str, Any] | None = None

    if rels:
        for rel in rels:
            p = _resolve_path(repo_root, rel).resolve()
            try:
                p.relative_to(root)
            except ValueError:
                manifest = {
                    "applied": False,
                    "reason": "fold_metric_path_escapes_repo",
                    "bad_path": rel,
                }
                print(
                    f"[precision_uplift_orchestrator] field_test precondition skipped: "
                    f"path outside repo ({rel!r})",
                    file=sys.stderr,
                    flush=True,
                )
                return manifest
            abs_paths.append(p)
    else:
        globs_raw = resources.get("field_test_objective_fold_metrics_globs")
        if not isinstance(globs_raw, list) or not globs_raw:
            return manifest
        globs_list = [str(x).strip() for x in globs_raw if str(x).strip()]
        if not globs_list:
            return manifest
        abs_paths, glob_expand_meta = expand_repo_relative_json_globs(
            repo_root, globs_list
        )
        if not abs_paths:
            manifest = {
                "applied": False,
                "reason": "empty_glob_match",
                "glob_expand": glob_expand_meta,
            }
            print(
                "[precision_uplift_orchestrator] field_test precondition skipped: "
                "field_test_objective_fold_metrics_globs matched no json files under repo",
                file=sys.stderr,
                flush=True,
            )
            return manifest

    ratio_raw = resources.get("field_test_precondition_production_neg_pos_ratio", 20.0)
    try:
        ratio = float(ratio_raw)
    except (TypeError, ValueError):
        ratio = 20.0

    sel_raw = resources.get("field_test_precondition_selection_mode", "field_test")
    sel_s = str(sel_raw).strip() if sel_raw is not None else "field_test"

    manifest = build_field_test_precondition_for_orchestration(
        repo_root,
        run_id=rid,
        start_ts=start_ts,
        end_ts=end_ts,
        fold_metrics_abs_paths=abs_paths,
        production_neg_pos_ratio=ratio,
        selection_mode=sel_s,
    )
    if glob_expand_meta is not None:
        manifest = {**manifest, "glob_expand": glob_expand_meta}
    updates = trainer_env_updates_from_precondition_manifest(manifest)
    if updates:
        child_env.update(updates)
        print(
            "[precision_uplift_orchestrator] field_test precondition: "
            f"wrote {manifest.get('output_json')} ({len(abs_paths)} fold files); "
            "FIELD_TEST_OBJECTIVE_PRECONDITION_JSON set for trainer jobs.",
            file=sys.stderr,
            flush=True,
        )
    elif not manifest.get("applied"):
        print(
            f"[precision_uplift_orchestrator] field_test precondition skipped: "
            f"{manifest.get('reason')}",
            file=sys.stderr,
            flush=True,
        )
    return manifest


def _phase2_trainer_job_timeout_sec(bundle: Mapping[str, Any]) -> float | None:
    """Optional per-job timeout from ``bundle['resources']['phase2_trainer_job_timeout_sec']``."""
    res = bundle.get("resources")
    if not isinstance(res, Mapping):
        return None
    raw = res.get("phase2_trainer_job_timeout_sec")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def run_phase2_trainer_jobs(
    repo_root: Path,
    bundle: dict[str, Any],
    *,
    python_exe: str | None = None,
    timeout_sec: float | None = None,
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    """Run ``trainer.trainer`` once per ``job_specs`` entry with logging under each job log dir.

    Args:
        repo_root: Repository root (subprocess cwd).
        bundle: Phase 2 bundle (mutable dict) with ``common``, ``resources``, ``job_specs``;
            updated in-place with ``field_test_objective_precondition`` after precondition merge.
        python_exe: Interpreter; defaults to ``sys.executable``.
        timeout_sec: Subprocess timeout per job; ``None`` uses bundle resource when set,
            else no timeout.

    Returns:
        ``(all_ok, first_error_message_or_none, per_job_records)``.
    """
    specs_obj = bundle.get("job_specs")
    if not isinstance(specs_obj, list):
        return True, None, []
    timeout = timeout_sec
    if timeout is None:
        timeout = _phase2_trainer_job_timeout_sec(bundle)

    common = bundle.get("common")
    md_opt: str | None = None
    if isinstance(common, Mapping):
        md_raw = common.get("model_dir")
        if isinstance(md_raw, str) and md_raw.strip():
            md_opt = md_raw.strip()
    child_env = _subprocess_env_align_model_dir_bundle(
        repo_root, config_model_dir=md_opt
    )
    pre_manifest = merge_phase2_field_test_precondition_into_trainer_env(
        repo_root, bundle, child_env
    )
    bundle["field_test_objective_precondition"] = pre_manifest

    results: list[dict[str, Any]] = []
    all_ok = True
    first_err: str | None = None
    exe = python_exe or sys.executable

    for spec in specs_obj:
        if not isinstance(spec, Mapping):
            continue
        track = str(spec.get("track") or "").strip()
        eid = str(spec.get("exp_id") or "").strip()
        rel = str(spec.get("logs_subdir_relative") or "").strip()
        if not track or not eid or not rel:
            msg = f"invalid job_spec entry (need track, exp_id, logs_subdir_relative): {spec!r}"
            if all_ok:
                all_ok = False
                first_err = msg
            results.append(
                {
                    "track": track or None,
                    "exp_id": eid or None,
                    "ok": False,
                    "message": msg,
                    "inferred_training_metrics_repo_relative": None,
                }
            )
            continue

        log_dir = (repo_root / rel).resolve()
        try:
            argv, unapplied = build_phase2_trainer_argv(
                bundle, track=track, exp_id=eid, python_exe=exe
            )
        except ValueError as exc:
            msg = str(exc)
            if all_ok:
                all_ok = False
                first_err = msg
            results.append(
                {
                    "track": track,
                    "exp_id": eid,
                    "ok": False,
                    "message": msg,
                    "unapplied_overrides": [],
                    "inferred_training_metrics_repo_relative": None,
                }
            )
            continue

        log_stem = f"trainer_job_{track}_{eid}".replace("/", "_").replace("\\", "_")
        print(
            f"[precision_uplift_orchestrator] phase2 trainer job: {track}/{eid} "
            f"(log under {rel})",
            file=sys.stderr,
            flush=True,
        )
        base = run_logged_command(
            argv,
            cwd=repo_root,
            log_dir=log_dir,
            log_stem=log_stem,
            timeout_sec=timeout,
            env=child_env,
        )
        ok_job = bool(base.get("ok"))
        if not ok_job and all_ok:
            all_ok = False
            first_err = (
                base.get("message")
                or f"trainer job {track}/{eid} exit {base.get('returncode')}"
            )
        out_path = base.get("stdout_path")
        err_path = base.get("stderr_path")
        inferred: str | None = None
        if ok_job:
            inferred = infer_training_metrics_repo_relative_from_trainer_logs(
                repo_root,
                stdout_path=str(out_path or ""),
                stderr_path=str(err_path or ""),
            )
        results.append(
            {
                "track": track,
                "exp_id": eid,
                "ok": ok_job,
                "returncode": base.get("returncode"),
                "argv": list(argv),
                "resolved_trainer_argv": list(argv),
                "argv_fingerprint": phase2_trainer_argv_fingerprint(argv),
                "unapplied_overrides": unapplied,
                "stdout_path": str(out_path) if out_path is not None else "",
                "stderr_path": str(err_path) if err_path is not None else "",
                "error_code": base.get("error_code"),
                "message": base.get("message"),
                "inferred_training_metrics_repo_relative": inferred,
            }
        )

    return all_ok, first_err, results


def _preview_precision_at_recall_1pct_from_metrics(
    metrics: Mapping[str, Any],
) -> float | None:
    """PAT@1% from ``backtest_metrics`` (``model_default``) or trainer metrics (``rated``)."""
    return evaluators.extract_phase2_precision_at_recall_1pct_from_metrics_mapping(
        metrics
    )


def _preview_precision_at_recall_1pct_series_from_metrics(
    metrics: Mapping[str, Any],
) -> list[float] | None:
    """Delegate to ``evaluators.extract_phase2_shared_pat_series_from_backtest_metrics``."""
    return evaluators.extract_phase2_shared_pat_series_from_backtest_metrics(metrics)


def _preview_precision_at_recall_1pct_window_ids_from_metrics(
    metrics: Mapping[str, Any],
) -> list[str] | None:
    """Delegate to ``evaluators.extract_phase2_shared_pat_window_ids_from_backtest_metrics``."""
    return evaluators.extract_phase2_shared_pat_window_ids_from_backtest_metrics(metrics)


def run_phase2_per_job_backtests(
    repo_root: Path,
    bundle: Mapping[str, Any],
    backtest_cfg_template: Mapping[str, Any],
    *,
    run_id: str,
    python_exe: str | None = None,
    timeout_sec: float | None = None,
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    """Run ``trainer.backtester`` once per ``job_specs`` row that has a training-metrics hint.

    Uses ``job_specs[].training_metrics_repo_relative`` to resolve the model bundle directory
    (see ``collectors.model_bundle_dir_from_training_metrics_hint``). Jobs without that
    field are skipped.     After each successful subprocess, reads ``backtest_metrics.json`` from
    ``collectors.phase2_per_job_backtest_metrics_repo_relative`` under that job's
    ``_per_job_backtest`` log directory (isolated from the shared backtest path).

    Args:
        repo_root: Repository root (subprocess cwd).
        bundle: Phase 2 bundle with ``job_specs`` and optional ``resources``.
        backtest_cfg_template: Mapping suitable for ``run_phase1_backtest`` (window, etc.);
            ``model_dir`` is overridden per job.
        run_id: Orchestrator run id (for log paths).
        python_exe: Interpreter; defaults to ``sys.executable``.
        timeout_sec: Per-job subprocess timeout.

    Returns:
        ``(all_ok, first_error_message_or_none, per_job_records)``.
    """
    specs_obj = bundle.get("job_specs")
    if not isinstance(specs_obj, list):
        return True, None, []

    results: list[dict[str, Any]] = []
    all_ok = True
    first_err: str | None = None
    exe = python_exe or sys.executable

    for spec in specs_obj:
        if not isinstance(spec, Mapping):
            continue
        track = str(spec.get("track") or "").strip()
        eid = str(spec.get("exp_id") or "").strip()
        hint = str(spec.get("training_metrics_repo_relative") or "").strip()
        if not track or not eid:
            msg = (
                "invalid job_spec entry (need track, exp_id): "
                f"{spec!r}"
            )
            if all_ok:
                first_err = msg
            all_ok = False
            results.append(
                {
                    "track": track or None,
                    "exp_id": eid or None,
                    "skipped": False,
                    "ok": False,
                    "message": msg,
                }
            )
            continue

        if not hint:
            print(
                f"[precision_uplift_orchestrator] phase2 per-job backtest SKIP: {track}/{eid} "
                "(no training_metrics_repo_relative)",
                file=sys.stderr,
                flush=True,
            )
            results.append(
                {
                    "track": track,
                    "exp_id": eid,
                    "skipped": True,
                    "skip_reason": "no training_metrics_repo_relative",
                    "ok": True,
                }
            )
            continue

        bundle_dir, berr = collectors.model_bundle_dir_from_training_metrics_hint(
            repo_root, hint
        )
        if bundle_dir is None:
            msg = berr or "cannot resolve model bundle from training_metrics_repo_relative"
            if all_ok:
                first_err = msg
            all_ok = False
            results.append(
                {
                    "track": track,
                    "exp_id": eid,
                    "skipped": False,
                    "training_metrics_repo_relative": hint,
                    "ok": False,
                    "message": msg,
                }
            )
            continue

        cfg_job = dict(backtest_cfg_template)
        cfg_job["model_dir"] = str(bundle_dir.resolve())
        rel_log = collectors.phase2_per_job_backtest_logs_subdir_relative(
            run_id, track, eid
        )
        log_dir = (repo_root / rel_log).resolve()
        cfg_job["backtest_output_dir"] = str(log_dir.resolve())
        mrel_job = collectors.phase2_per_job_backtest_metrics_repo_relative(
            run_id, track, eid
        )
        print(
            f"[precision_uplift_orchestrator] phase2 per-job backtest: {track}/{eid} "
            f"model_bundle={bundle_dir}",
            file=sys.stderr,
            flush=True,
        )
        res_bt = run_phase1_backtest(
            repo_root,
            cfg_job,
            log_dir,
            python_exe=exe,
            timeout_sec=timeout_sec,
        )
        ok_sub = bool(res_bt.get("ok"))
        preview: float | None = None
        load_err: str | None = None
        ingest_error_code: str | None = None
        if ok_sub:
            mobj, load_err = collectors.load_json_under_repo(repo_root, mrel_job)
            if mobj is not None and not load_err:
                preview = _preview_precision_at_recall_1pct_from_metrics(mobj)
                if preview is None:
                    ok_sub = False
                    pr_key = evaluators.PHASE2_BACKTEST_PR1_KEY
                    load_err = (
                        f"backtest_metrics lacks parseable {pr_key} "
                        "(expected under model_default or rated; PAT@1% for observation window)"
                    )
                    ingest_error_code = "E_NO_DATA_WINDOW"
                    preview_series = None
                    preview_window_ids = None
                else:
                    preview_series = _preview_precision_at_recall_1pct_series_from_metrics(
                        mobj
                    )
                    preview_window_ids = (
                        _preview_precision_at_recall_1pct_window_ids_from_metrics(mobj)
                    )
            else:
                ok_sub = False
                preview_series = None
                preview_window_ids = None
        else:
            preview_series = None
            preview_window_ids = None

        if not ok_sub:
            if all_ok:
                first_err = (
                    load_err
                    or res_bt.get("message")
                    or f"per-job backtest failed for {track}/{eid}"
                )
            all_ok = False

        results.append(
            {
                "track": track,
                "exp_id": eid,
                "skipped": False,
                "training_metrics_repo_relative": hint,
                "model_bundle_dir": str(bundle_dir.resolve()),
                "metrics_repo_relative": mrel_job,
                "ok": ok_sub,
                "returncode": res_bt.get("returncode"),
                "stdout_path": str(res_bt.get("stdout_path") or ""),
                "stderr_path": str(res_bt.get("stderr_path") or ""),
                "error_code": res_bt.get("error_code"),
                "message": res_bt.get("message"),
                "metrics_load_error": None if ok_sub else load_err,
                "ingest_error_code": ingest_error_code,
                "shared_precision_at_recall_1pct_preview": preview,
                "precision_at_recall_1pct_by_window_preview": preview_series,
                "precision_at_recall_1pct_window_ids_preview": preview_window_ids,
            }
        )

    return all_ok, first_err, results


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
    print(
        "[precision_uplift_orchestrator] preflight: checking model_dir, sqlite DBs, "
        f"backtest_smoke={not skip_backtest_smoke}",
        file=sys.stderr,
        flush=True,
    )
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
        md_raw = cfg.get("model_dir")
        md_opt = str(md_raw).strip() if isinstance(md_raw, str) and str(md_raw).strip() else None
        ok, msg = run_backtest_cli_smoke(
            repo_root, python_exe=python_exe, config_model_dir=md_opt
        )
        checks.append({"name": "backtester_cli_smoke", "ok": ok, "message": msg})
        if not ok:
            return {
                "ok": False,
                "error_code": "E_BACKTEST_CLI",
                "message": msg,
                "checks": checks,
            }

    print(
        "[precision_uplift_orchestrator] preflight: all checks passed",
        file=sys.stderr,
        flush=True,
    )
    return {"ok": True, "error_code": None, "message": None, "checks": checks}
