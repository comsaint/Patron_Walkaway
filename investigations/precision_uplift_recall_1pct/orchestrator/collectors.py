"""Collect backtest / R1-R6 / DB metrics into a unified dict (MVP T4)."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import evaluators

DEFAULT_BACKTEST_METRICS = "trainer/out_backtest/backtest_metrics.json"

_ORCHESTRATOR_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ORCHESTRATOR_DIR.parents[2]

ERR_BACKTEST_METRICS = "E_COLLECT_BACKTEST_METRICS"
ERR_R1_PAYLOAD = "E_COLLECT_R1_PAYLOAD"
ERR_STATE_DB_STATS = "E_COLLECT_STATE_DB"
_MID_CP_STEM_RE = re.compile(r"^r1_r6_mid_cp(\d+)\.stdout\.log$")


def _resolve_under_root(repo_root: Path, p: str | Path) -> Path:
    """Resolve ``p`` under ``repo_root`` when relative."""
    path = Path(p)
    return path if path.is_absolute() else (repo_root / path)


def _phase2_job_logs_subdir_relative(run_id: str, track: str, exp_id: str) -> str:
    """Repo-relative POSIX path for Phase 2 job logs (same tree as run_state / bundle)."""
    rel_orch = _ORCHESTRATOR_DIR.relative_to(_REPO_ROOT)
    leaf = rel_orch / "state" / run_id / "logs" / "phase2" / track / exp_id
    return leaf.as_posix()


def phase2_shared_backtest_logs_subdir_relative(run_id: str) -> str:
    """Repo-relative log directory for the Phase 2 shared backtest subprocess (T10)."""
    rel_orch = _ORCHESTRATOR_DIR.relative_to(_REPO_ROOT)
    leaf = rel_orch / "state" / run_id / "logs" / "phase2" / "_shared_backtest"
    return leaf.as_posix()


def phase2_per_job_backtest_logs_subdir_relative(
    run_id: str,
    track: str,
    exp_id: str,
) -> str:
    """Repo-relative log directory for one job's ``trainer.backtester`` subprocess (T10)."""
    rel_orch = _ORCHESTRATOR_DIR.relative_to(_REPO_ROOT)
    leaf = (
        rel_orch
        / "state"
        / run_id
        / "logs"
        / "phase2"
        / track
        / exp_id
        / "_per_job_backtest"
    )
    return leaf.as_posix()


def phase2_per_job_backtest_metrics_repo_relative(
    run_id: str,
    track: str,
    exp_id: str,
) -> str:
    """Repo-relative path to ``backtest_metrics.json`` for one per-job backtest (T10).

    Written by ``trainer.backtester --output-dir`` set to the job's
    ``phase2_per_job_backtest_logs_subdir_relative`` directory.
    """
    base = phase2_per_job_backtest_logs_subdir_relative(run_id, track, exp_id)
    return f"{base}/backtest_metrics.json"


def phase2_backtest_metrics_repo_relative(bundle: Mapping[str, Any]) -> str:
    """Path (repo-relative) where ``trainer.backtester`` writes ``backtest_metrics.json``."""
    res = bundle.get("resources")
    mrel = DEFAULT_BACKTEST_METRICS
    if isinstance(res, Mapping):
        bmp = res.get("backtest_metrics_path")
        if bmp is not None and str(bmp).strip():
            mrel = str(bmp).strip()
    return mrel


def model_bundle_dir_from_training_metrics_hint(
    repo_root: Path,
    hint: str,
) -> tuple[Path | None, str | None]:
    """Resolve ``training_metrics_repo_relative`` to the on-disk model bundle directory.

    Accepts a path to ``training_metrics.json`` (parent dir = bundle) or to the bundle
    directory itself.

    Returns:
        ``(resolved_dir, None)`` or ``(None, error_message)``.
    """
    s = str(hint or "").strip()
    if not s:
        return None, "empty training_metrics_repo_relative"
    base, err = _safe_resolve_under_repo_root(repo_root, s)
    if err:
        return None, err
    assert base is not None
    if not base.exists():
        return None, f"path not found: {base}"
    d = base.parent if base.is_file() else base
    d = d.resolve()
    if not d.is_dir():
        return None, f"model bundle path is not a directory: {d}"
    return d, None


PHASE2_JOB_TRAINING_METRICS_NAME = "training_metrics.json"


def _safe_resolve_under_repo_root(repo_root: Path, rel: str) -> tuple[Path | None, str | None]:
    """Resolve a repo-relative path; reject absolute paths and ``..`` escapes.

    Args:
        repo_root: Repository root (directory).
        rel: Relative POSIX-ish path from config (forward slashes ok on Windows).

    Returns:
        ``(resolved_path, None)`` on success, or ``(None, error_message)``.
    """
    s = str(rel).strip()
    if not s:
        return None, "empty training_metrics_repo_relative"
    raw = Path(s)
    if raw.is_absolute():
        return None, "absolute training_metrics_repo_relative is not allowed"
    root = repo_root.resolve()
    try:
        cand = (root / s).resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"cannot resolve path: {exc}"
    try:
        cand.relative_to(root)
    except ValueError:
        return None, "training_metrics_repo_relative escapes repository root"
    return cand, None


def _repo_relative_posix(repo_root: Path, path: Path) -> str:
    """Best-effort repo-relative path for display (POSIX)."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _phase2_job_training_metrics_path(
    repo_root: Path,
    spec: Mapping[str, Any],
) -> tuple[Path | None, str, str | None]:
    """Pick JSON path for one job_spec and optional pre-load error (bad hint).

    Precedence: optional ``training_metrics_repo_relative`` (file or directory +
    ``training_metrics.json``), else ``{logs_subdir_relative}/training_metrics.json``.

    Returns:
        ``(json_path, metrics_relative_display, pre_error)`` — ``pre_error`` set when
        the hint is invalid (escape, absolute); ``json_path`` may still be missing on disk.
    """
    hint = str(spec.get("training_metrics_repo_relative") or "").strip()
    if hint:
        base, serr = _safe_resolve_under_repo_root(repo_root, hint)
        if serr:
            return None, hint, serr
        assert base is not None
        if base.is_file():
            json_path = base
        elif base.is_dir():
            json_path = base / PHASE2_JOB_TRAINING_METRICS_NAME
        else:
            json_path = (
                base
                if base.suffix.lower() == ".json"
                else base / PHASE2_JOB_TRAINING_METRICS_NAME
            )
        rel_disp = _repo_relative_posix(repo_root, json_path)
        return json_path, rel_disp, None

    rel_logs = str(spec.get("logs_subdir_relative") or "").strip()
    if not rel_logs:
        return None, "", "job_spec missing logs_subdir_relative"
    rel_json = f"{rel_logs.rstrip('/')}/{PHASE2_JOB_TRAINING_METRICS_NAME}"
    path = _resolve_under_root(repo_root, rel_json)
    return path, rel_json, None


def harvest_phase2_job_training_metrics(
    repo_root: Path,
    bundle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Best-effort load training metrics per ``job_specs`` entry (T10).

    Each row tries (in order): optional YAML/bundle field
    ``training_metrics_repo_relative`` (repo-relative path to a ``.json`` file or to a
    directory containing ``training_metrics.json``), else
    ``{logs_subdir_relative}/training_metrics.json``.

    Args:
        repo_root: Repository root.
        bundle: Phase 2 bundle with ``job_specs``.

    Returns:
        One dict per job_spec with paths, ``found``, optional ``training_metrics`` payload.
    """
    specs = bundle.get("job_specs")
    if not isinstance(specs, list):
        return []
    out: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        track = str(spec.get("track") or "").strip()
        eid = str(spec.get("exp_id") or "").strip()
        path, rel_disp, pre_err = _phase2_job_training_metrics_path(repo_root, spec)
        if pre_err is not None:
            out.append(
                {
                    "track": track,
                    "exp_id": eid,
                    "metrics_relative": str(
                        spec.get("training_metrics_repo_relative") or ""
                    ).strip(),
                    "metrics_absolute": "",
                    "found": False,
                    "load_error": pre_err,
                    "training_metrics": None,
                }
            )
            continue
        assert path is not None
        obj, err = _load_json_object(path)
        row: dict[str, Any] = {
            "track": track,
            "exp_id": eid,
            "metrics_relative": rel_disp,
            "metrics_absolute": str(path),
            "found": obj is not None,
            "load_error": None if obj is not None else err,
            "training_metrics": obj,
        }
        out.append(row)
    return out


def _append_error(
    errors: list[dict[str, str]],
    code: str,
    message: str,
    *,
    path: str | None = None,
) -> None:
    """Record a collection error for callers (non-silent fail)."""
    item: dict[str, str] = {"code": code, "message": message}
    if path is not None:
        item["path"] = path
    errors.append(item)


def _load_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a JSON object from file; return (obj, None) or (None, err)."""
    if not path.is_file():
        return None, f"file not found: {path}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(raw, dict):
        return None, f"expected JSON object at root, got {type(raw).__name__}"
    return raw, None


def load_json_under_repo(repo_root: Path, rel_path: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load a JSON object from ``rel_path`` resolved under ``repo_root``."""
    path = _resolve_under_root(repo_root, str(rel_path).strip())
    return _load_json_object(path)


def _parse_r1_stdout_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse R1/R6 ``--pretty`` stdout log (single JSON object)."""
    if not path.is_file():
        return None, f"r1_r6 log not found: {path}"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None, f"empty r1_r6 log: {path}"
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"r1_r6 log is not valid JSON ({path}): {exc}"
    if not isinstance(raw, dict):
        return None, f"r1_r6 JSON root must be object, got {type(raw).__name__}"
    return raw, None


def _collect_mid_snapshot_payloads(
    logs_dir: Path,
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Parse all discovered mid snapshot logs in deterministic checkpoint order.

    Ordering:
    1) ``r1_r6_mid_cpN.stdout.log`` by ascending N
    2) ``r1_r6_mid.stdout.log`` (canonical last-mid alias)
    """
    rows: list[dict[str, Any]] = []
    cp_paths: list[tuple[int, Path]] = []
    for p in logs_dir.glob("r1_r6_mid_cp*.stdout.log"):
        m = _MID_CP_STEM_RE.match(p.name)
        if not m:
            continue
        cp_paths.append((int(m.group(1)), p))
    cp_paths.sort(key=lambda x: x[0])
    for idx, p in cp_paths:
        payload, perr = _parse_r1_stdout_file(p)
        if perr:
            _append_error(
                errors,
                ERR_R1_PAYLOAD,
                f"mid_cp{idx}: {perr}",
                path=str(p),
            )
        rows.append(
            {
                "checkpoint_index": idx,
                "stdout_log": str(p),
                "payload": payload,
                "parse_error": perr,
            }
        )
    alias = logs_dir / "r1_r6_mid.stdout.log"
    if alias.is_file():
        payload, perr = _parse_r1_stdout_file(alias)
        if perr:
            _append_error(
                errors,
                ERR_R1_PAYLOAD,
                f"mid: {perr}",
                path=str(alias),
            )
        rows.append(
            {
                "checkpoint_index": None,
                "stdout_log": str(alias),
                "payload": payload,
                "parse_error": perr,
                "is_canonical_mid_alias": True,
            }
        )
    return rows


def _collect_state_db_window_stats(
    state_db: Path,
    window: Mapping[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Count finalized validations in ``validation_results`` inside the window."""
    start_ts = str(window.get("start_ts", ""))
    end_ts = str(window.get("end_ts", ""))
    empty: dict[str, Any] = {
        "state_db_path": str(state_db),
        "window_start_ts": start_ts,
        "window_end_ts": end_ts,
        "validation_results_rows_in_window": None,
        "finalized_alerts_count": None,
        "finalized_true_positives_count": None,
        "note": None,
    }
    if not state_db.is_file():
        _append_error(
            errors,
            ERR_STATE_DB_STATS,
            "state DB path is not a file",
            path=str(state_db),
        )
        return empty

    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        _append_error(
            errors,
            ERR_STATE_DB_STATS,
            f"cannot open state DB: {exc}",
            path=str(state_db),
        )
        return empty

    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(validation_results)").fetchall()
        }
        if "alert_ts" not in cols:
            empty["note"] = "validation_results missing alert_ts; counts are global"
            where_time = ""
            params: tuple[Any, ...] = ()
        else:
            where_time = "alert_ts >= ? AND alert_ts < ? AND "
            params = (start_ts, end_ts)

        if "validated_at" not in cols:
            empty["note"] = "validation_results missing validated_at; using row counts only"
            sql_all = f"SELECT COUNT(*) FROM validation_results WHERE {where_time} 1=1" if where_time else "SELECT COUNT(*) FROM validation_results"
            n_all = int(conn.execute(sql_all, params).fetchone()[0])
            empty["validation_results_rows_in_window"] = n_all
            empty["finalized_alerts_count"] = n_all
            empty["finalized_true_positives_count"] = 0
            return empty

        fin_clause = "validated_at IS NOT NULL AND TRIM(COALESCE(validated_at, '')) != ''"
        sql_fin = (
            f"SELECT COUNT(*) FROM validation_results WHERE {where_time}{fin_clause}"
        )
        n_fin = int(conn.execute(sql_fin, params).fetchone()[0])

        if "result" not in cols:
            empty["validation_results_rows_in_window"] = n_fin
            empty["finalized_alerts_count"] = n_fin
            empty["finalized_true_positives_count"] = None
            empty["note"] = "validation_results missing result column"
            return empty

        sql_tp = (
            f"SELECT COUNT(*) FROM validation_results WHERE {where_time}{fin_clause}"
            f" AND result = 1"
        )
        n_tp = int(conn.execute(sql_tp, params).fetchone()[0])

        sql_rows = (
            f"SELECT COUNT(*) FROM validation_results WHERE {where_time} 1=1"
            if where_time
            else "SELECT COUNT(*) FROM validation_results"
        )
        n_rows = int(conn.execute(sql_rows, params).fetchone()[0])

        empty["validation_results_rows_in_window"] = n_rows
        empty["finalized_alerts_count"] = n_fin
        empty["finalized_true_positives_count"] = n_tp
        return empty
    except sqlite3.Error as exc:
        _append_error(
            errors,
            ERR_STATE_DB_STATS,
            f"SQL error while aggregating validation_results: {exc}",
            path=str(state_db),
        )
        return empty
    finally:
        conn.close()


def _safe_ratio(num: int, den: int) -> float | None:
    """Return ``num / den`` when denominator is positive; else ``None``."""
    if den <= 0:
        return None
    return float(num) / float(den)


def _collect_phase1_pit_parity(
    prediction_log_db: Path,
    state_db: Path,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    """Collect Phase 1 PIT parity diagnostics (MVP auto metrics, non-blocking)."""
    start_ts = str(window.get("start_ts", ""))
    end_ts = str(window.get("end_ts", ""))
    out: dict[str, Any] = {
        "status": "ok",
        "window_start_ts": start_ts,
        "window_end_ts": end_ts,
        "scored_at_in_window_ratio": None,
        "validated_at_non_null_ratio": None,
        "alerts_vs_prediction_log_gap": None,
        "window_timezone_mismatch_count": None,
        "reasons": [],
        "note": "MVP auto diagnostics; timezone mismatch count unavailable without explicit tz column contract.",
    }

    reasons: list[str] = []
    if prediction_log_db.is_file():
        try:
            with sqlite3.connect(f"file:{prediction_log_db}?mode=ro", uri=True) as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(prediction_log)").fetchall()}
                if "scored_at" in cols:
                    n_window_total = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM prediction_log WHERE scored_at >= ? AND scored_at < ?",
                            (start_ts, end_ts),
                        ).fetchone()[0]
                    )
                    n_scored_in = int(conn.execute("SELECT COUNT(*) FROM prediction_log WHERE scored_at >= ? AND scored_at < ? AND TRIM(COALESCE(scored_at, '')) != ''", (start_ts, end_ts)).fetchone()[0])
                    out["scored_at_in_window_ratio"] = _safe_ratio(n_scored_in, n_window_total)
                else:
                    reasons.append("prediction_log_missing_scored_at")
                if "scored_at" in cols and "is_alert" in cols:
                    n_pl_alerts = int(conn.execute("SELECT COUNT(*) FROM prediction_log WHERE scored_at >= ? AND scored_at < ? AND is_alert = 1", (start_ts, end_ts)).fetchone()[0])
                    out["alerts_vs_prediction_log_gap"] = -n_pl_alerts
                else:
                    reasons.append("prediction_log_missing_is_alert_or_scored_at")
        except sqlite3.Error:
            reasons.append("prediction_log_db_unavailable")
    else:
        reasons.append("prediction_log_db_not_found")

    if state_db.is_file():
        try:
            with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as conn:
                vcols = {row[1] for row in conn.execute("PRAGMA table_info(validation_results)").fetchall()}
                if "validated_at" in vcols:
                    n_rows = int(conn.execute("SELECT COUNT(*) FROM validation_results").fetchone()[0])
                    n_valid = int(conn.execute("SELECT COUNT(*) FROM validation_results WHERE TRIM(COALESCE(validated_at, '')) != ''").fetchone()[0])
                    out["validated_at_non_null_ratio"] = _safe_ratio(n_valid, n_rows)
                else:
                    reasons.append("validation_results_missing_validated_at")
                acols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
                if "ts" in acols:
                    n_alerts = int(conn.execute("SELECT COUNT(*) FROM alerts WHERE ts >= ? AND ts < ?", (start_ts, end_ts)).fetchone()[0])
                    gap = out.get("alerts_vs_prediction_log_gap")
                    if isinstance(gap, int):
                        out["alerts_vs_prediction_log_gap"] = int(gap + n_alerts)
                else:
                    reasons.append("alerts_missing_ts")
        except sqlite3.Error:
            reasons.append("state_db_unavailable_for_pit")
    else:
        reasons.append("state_db_not_found_for_pit")

    out["reasons"] = reasons
    if reasons:
        out["status"] = "warn"
    return out


def collect_phase1_artifacts(
    run_id: str,
    cfg: Mapping[str, Any] | None = None,
    *,
    repo_root: Path | None = None,
    orchestrator_dir: Path | None = None,
) -> dict[str, Any]:
    """Gather paths and metrics needed for Gate and reports.

    Reads:
    - ``trainer/out_backtest/backtest_metrics.json`` (override via ``cfg['backtest_metrics_path']``)
    - R1/R6 JSON from ``orchestrator/state/<run_id>/logs/r1_r6.stdout.log`` and optional
      ``r1_r6_mid.stdout.log`` when present
    - Basic stats from ``cfg['state_db_path']`` / ``validation_results``

    Args:
        run_id: Orchestrator run id (state subdirectory name).
        cfg: Validated Phase 1 config (window, paths).
        repo_root: Repository root for resolving relative artifact paths.
        orchestrator_dir: ``.../orchestrator`` directory (for per-run logs).

    Returns:
        Unified dict with ``errors`` (explicit missing/parse failures), parsed payloads,
        and ``state_db_stats`` suitable for evaluators.
    """
    errors: list[dict[str, str]] = []
    cfg_d = dict(cfg or {})
    root = repo_root or Path.cwd()
    orch = orchestrator_dir or Path.cwd()

    rel_metrics = str(cfg_d.get("backtest_metrics_path") or DEFAULT_BACKTEST_METRICS)
    metrics_path = _resolve_under_root(root, rel_metrics)
    metrics_obj, m_err = _load_json_object(metrics_path)
    if m_err:
        _append_error(
            errors,
            ERR_BACKTEST_METRICS,
            m_err,
            path=str(metrics_path),
        )

    logs_dir = orch / "state" / run_id / "logs"
    r1_final_path = logs_dir / "r1_r6.stdout.log"
    r1_mid_path = logs_dir / "r1_r6_mid.stdout.log"

    r1_final, r1_err = _parse_r1_stdout_file(r1_final_path)
    if r1_err:
        _append_error(
            errors,
            ERR_R1_PAYLOAD,
            r1_err,
            path=str(r1_final_path),
        )

    mid_rows = _collect_mid_snapshot_payloads(logs_dir, errors)
    r1_mid_payload: dict[str, Any] | None = None
    r1_mid_parse_err: str | None = None
    if mid_rows:
        last = mid_rows[-1]
        p = last.get("payload")
        r1_mid_payload = p if isinstance(p, dict) else None
        pe = last.get("parse_error")
        r1_mid_parse_err = str(pe) if isinstance(pe, str) else None

    window = cfg_d.get("window") or {}
    state_db_raw = cfg_d.get("state_db_path", "")
    state_db_path = _resolve_under_root(root, str(state_db_raw)) if state_db_raw else Path()
    prediction_db_raw = cfg_d.get("prediction_log_db_path", "")
    prediction_db_path = (
        _resolve_under_root(root, str(prediction_db_raw))
        if prediction_db_raw
        else Path()
    )
    state_stats = _collect_state_db_window_stats(state_db_path, window, errors)
    pit_parity = _collect_phase1_pit_parity(
        prediction_db_path,
        state_db_path,
        window if isinstance(window, Mapping) else {},
    )

    bundle: dict[str, Any] = {
        "run_id": run_id,
        "errors": errors,
        "backtest_metrics": metrics_obj,
        "backtest_metrics_path": str(metrics_path),
        "r1_r6_final": {
            "payload": r1_final,
            "stdout_log": str(r1_final_path),
            "parse_error": r1_err,
        },
        "r1_r6_mid": {
            "payload": r1_mid_payload,
            "stdout_log": str(r1_mid_path),
            "parse_error": r1_mid_parse_err,
        },
        "r1_r6_mid_snapshots": mid_rows,
        "state_db_stats": state_stats,
        "pit_parity": pit_parity,
        "thresholds": dict(cfg_d.get("thresholds") or {}),
        "window": dict(window) if isinstance(window, Mapping) else {},
    }
    return bundle


_PHASE2_TRACK_ORDER: tuple[str, ...] = ("track_a", "track_b", "track_c")


def count_phase2_yaml_pat_matrix_experiments(tracks: Any) -> int:
    """Count experiments under ``track_*`` with non-empty ``precision_at_recall_1pct_by_window``.

    Used for ``run_state.phase2_collect`` observability (T10 multi-window matrix intent).

    Args:
        tracks: ``bundle['tracks']`` from a phase2 plan or enriched bundle.

    Returns:
        Number of experiment rows that declare at least one window PAT value in YAML.
    """
    if not isinstance(tracks, Mapping):
        return 0
    n = 0
    for tname, tnode in tracks.items():
        tr = str(tname).strip()
        if not tr.startswith("track_"):
            continue
        if not isinstance(tnode, Mapping):
            continue
        exps = tnode.get("experiments")
        if not isinstance(exps, list):
            continue
        for exp in exps:
            if not isinstance(exp, Mapping):
                continue
            raw = exp.get("precision_at_recall_1pct_by_window")
            if isinstance(raw, list) and len(raw) > 0:
                n += 1
    return n


def build_phase2_pat_series_from_plan_tracks(tracks_out: Mapping[str, Any]) -> dict[str, dict[str, list[float]]]:
    """Aggregate optional per-experiment ``precision_at_recall_1pct_by_window`` into gate shape.

    Reads validated plan snapshot ``tracks_out`` (``track_*`` → ``experiments`` list with
    optional ``precision_at_recall_1pct_by_window``). Produces
    ``track_name -> {exp_id: [PAT@1% per window]}`` in the same scale as previews (0–1).

    Args:
        tracks_out: ``collect_phase2_plan_bundle`` internal ``tracks_out`` mapping.

    Returns:
        Nested dict suitable for ``bundle['phase2_pat_series_by_experiment']``; may be empty.
    """
    out: dict[str, dict[str, list[float]]] = {}
    for tname, tnode in tracks_out.items():
        tr = str(tname).strip()
        if not tr.startswith("track_"):
            continue
        if not isinstance(tnode, Mapping):
            continue
        exps = tnode.get("experiments")
        if not isinstance(exps, list):
            continue
        for exp in exps:
            if not isinstance(exp, Mapping):
                continue
            eid = str(exp.get("exp_id") or "").strip()
            raw = exp.get("precision_at_recall_1pct_by_window")
            if not isinstance(raw, list) or not raw or not eid:
                continue
            nums: list[float] = []
            for x in raw:
                try:
                    nums.append(float(x))
                except (TypeError, ValueError):
                    continue
            if nums:
                out.setdefault(tr, {})[eid] = nums
    return out


def collect_phase2_plan_bundle(run_id: str, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Build a reproducible Phase 2 bundle from validated YAML only (T10 plan-only).

    Does not run trainer jobs. ``status`` is ``plan_only`` until the track runner
    writes real metrics artifacts.

    Args:
        run_id: Orchestrator run identifier.
        cfg: Output of ``config_loader.load_phase2_config`` (validated mapping).

    Returns:
        JSON-serializable bundle dict including flattened ``experiments_index`` and
        ``job_specs`` (enabled experiments with suggested per-job log paths for T10 runner).

    Raises:
        TypeError: If ``cfg`` is not a mapping.
        ValueError: If ``tracks`` is missing or not a mapping.
    """
    if not isinstance(cfg, Mapping):
        raise TypeError(f"cfg must be a mapping, got {type(cfg).__name__}")
    tracks_raw = cfg.get("tracks")
    if not isinstance(tracks_raw, Mapping):
        raise ValueError("phase2 cfg.tracks must be a mapping")

    tracks_out: dict[str, Any] = {}
    experiments_index: list[dict[str, Any]] = []
    for name in _PHASE2_TRACK_ORDER:
        t_ent = tracks_raw.get(name)
        if not isinstance(t_ent, Mapping):
            continue
        enabled = bool(t_ent.get("enabled", False))
        exps_in = t_ent.get("experiments") or []
        exps_out: list[dict[str, Any]] = []
        if isinstance(exps_in, list):
            for exp in exps_in:
                if not isinstance(exp, Mapping):
                    continue
                eid = str(exp.get("exp_id", "") or "").strip()
                overrides = exp.get("overrides")
                ov_dict: dict[str, Any] = (
                    dict(overrides) if isinstance(overrides, Mapping) else {}
                )
                exp_row: dict[str, Any] = {"exp_id": eid, "overrides": ov_dict}
                tp_raw = exp.get("trainer_params")
                if isinstance(tp_raw, Mapping) and tp_raw:
                    exp_row["trainer_params"] = {
                        str(k): tp_raw[k] for k in sorted(tp_raw, key=str)
                    }
                idx_row: dict[str, Any] = {
                    "track": name,
                    "exp_id": eid,
                    "track_enabled": enabled,
                }
                tm_raw = exp.get("training_metrics_repo_relative")
                if isinstance(tm_raw, str) and tm_raw.strip():
                    ts = tm_raw.strip()
                    exp_row["training_metrics_repo_relative"] = ts
                    idx_row["training_metrics_repo_relative"] = ts
                pab = exp.get("precision_at_recall_1pct_by_window")
                if isinstance(pab, list) and pab:
                    exp_row["precision_at_recall_1pct_by_window"] = [float(x) for x in pab]
                exps_out.append(exp_row)
                experiments_index.append(idx_row)
        tracks_out[name] = {"enabled": enabled, "experiments": exps_out}

    common = cfg.get("common")
    common_snap: dict[str, Any]
    if isinstance(common, Mapping):
        common_snap = json.loads(json.dumps(common, default=str))
    else:
        common_snap = {}

    resources = cfg.get("resources")
    gate = cfg.get("gate")
    rid = str(run_id).strip()
    job_specs: list[dict[str, Any]] = []
    for e in experiments_index:
        if not isinstance(e, Mapping):
            continue
        if not e.get("track_enabled"):
            continue
        eid = str(e.get("exp_id") or "").strip()
        if not eid:
            continue
        tr = str(e.get("track") or "").strip()
        spec_row: dict[str, Any] = {
            "track": tr,
            "exp_id": eid,
            "logs_subdir_relative": _phase2_job_logs_subdir_relative(rid, tr, eid),
        }
        tm = e.get("training_metrics_repo_relative")
        if isinstance(tm, str) and tm.strip():
            spec_row["training_metrics_repo_relative"] = tm.strip()
        job_specs.append(spec_row)

    pat_from_yaml = build_phase2_pat_series_from_plan_tracks(tracks_out)
    bundle: dict[str, Any] = {
        "run_id": rid,
        "bundle_kind": "phase2_plan_v1",
        "status": "plan_only",
        "note": (
            "Experiment plan derived from phase2 YAML only; "
            "optional per-experiment training_metrics_repo_relative points harvest at "
            "trainer output (else logs_subdir_relative/training_metrics.json); "
            "optional precision_at_recall_1pct_by_window lists populate "
            "phase2_pat_series_by_experiment for std gate."
        ),
        "phase": str(cfg.get("phase", "")),
        "common": common_snap,
        "resources": dict(resources) if isinstance(resources, Mapping) else {},
        "gate": dict(gate) if isinstance(gate, Mapping) else {},
        "tracks": tracks_out,
        "experiments_index": experiments_index,
        "job_specs": job_specs,
        "errors": [],
    }
    if pat_from_yaml:
        bundle["phase2_pat_series_by_experiment"] = pat_from_yaml
    return bundle


def collect_summary_phase2_plan_for_run_state(
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Compact Phase 2 plan bundle summary for ``run_state.json``."""
    idx = (
        bundle.get("experiments_index")
        if isinstance(bundle.get("experiments_index"), list)
        else []
    )
    n_active = 0
    for e in idx:
        if not isinstance(e, Mapping):
            continue
        if not e.get("track_enabled"):
            continue
        if not str(e.get("exp_id", "") or "").strip():
            continue
        n_active += 1
    tracks = bundle.get("tracks") if isinstance(bundle.get("tracks"), dict) else {}
    tracks_enabled = [
        k
        for k, v in tracks.items()
        if isinstance(v, dict) and bool(v.get("enabled"))
    ]
    js = bundle.get("job_specs")
    n_jobs = len(js) if isinstance(js, list) else 0
    n_tm_hint = 0
    if isinstance(js, list):
        for spec in js:
            if not isinstance(spec, Mapping):
                continue
            if str(spec.get("training_metrics_repo_relative") or "").strip():
                n_tm_hint += 1
    out: dict[str, Any] = {
        "bundle_kind": bundle.get("bundle_kind"),
        "status": bundle.get("status"),
        "plan_experiment_slots": len(idx),
        "plan_experiments_active": n_active,
        "job_specs_count": n_jobs,
        "job_specs_training_metrics_hint_count": n_tm_hint,
        "tracks_enabled": sorted(tracks_enabled),
    }
    n_pat_yaml = count_phase2_yaml_pat_matrix_experiments(tracks)
    if n_pat_yaml > 0:
        out["phase2_pat_matrix_yaml_experiment_count"] = n_pat_yaml
    rs = bundle.get("runner_smoke")
    if isinstance(rs, Mapping):
        out["runner_log_dirs_ok"] = rs.get("log_dirs_ok")
        th_skip = bool(rs.get("trainer_help_skipped"))
        out["runner_trainer_help_skipped"] = th_skip
        out["runner_trainer_help_ok"] = (
            None if th_skip else rs.get("trainer_help_ok")
        )
    tj = bundle.get("trainer_jobs")
    if isinstance(tj, Mapping):
        out["trainer_jobs_executed"] = tj.get("executed")
        out["trainer_jobs_all_ok"] = tj.get("all_ok")
        res = tj.get("results")
        out["trainer_jobs_count"] = len(res) if isinstance(res, list) else 0
    bj = bundle.get("backtest_jobs")
    if isinstance(bj, Mapping):
        out["backtest_jobs_executed"] = bj.get("executed")
        out["backtest_subprocess_ok"] = bj.get("subprocess_ok")
        out["backtest_metrics_loaded"] = bj.get("metrics_loaded")
    jh = bundle.get("job_training_harvest")
    if isinstance(jh, Mapping):
        rows = jh.get("rows")
        n_rows = len(rows) if isinstance(rows, list) else 0
        n_found = 0
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, Mapping) and r.get("found"):
                    n_found += 1
        out["job_training_harvest_rows"] = n_rows
        out["job_training_harvest_found"] = n_found
    pjb = bundle.get("per_job_backtest_jobs")
    if isinstance(pjb, Mapping):
        out["per_job_backtest_jobs_executed"] = pjb.get("executed")
        out["per_job_backtest_jobs_all_ok"] = pjb.get("all_ok")
        res = pjb.get("results")
        out["per_job_backtest_jobs_count"] = len(res) if isinstance(res, list) else 0
    out["phase2_has_backtest_metrics"] = bundle.get("backtest_metrics") is not None
    bm_sum = bundle.get("backtest_metrics")
    ser_shared = evaluators.extract_phase2_shared_pat_series_from_backtest_metrics(
        bm_sum if isinstance(bm_sum, Mapping) else None
    )
    if ser_shared is not None:
        out["phase2_shared_backtest_pat_series_len"] = len(ser_shared)
    wid_shared = evaluators.extract_phase2_shared_pat_window_ids_from_backtest_metrics(
        bm_sum if isinstance(bm_sum, Mapping) else None
    )
    if wid_shared is not None:
        out["phase2_shared_backtest_pat_window_ids_len"] = len(wid_shared)
    if (
        ser_shared is not None
        and wid_shared is not None
        and len(ser_shared) != len(wid_shared)
    ):
        out["phase2_shared_backtest_pat_series_ids_mismatch"] = True
    root_ps = bundle.get("phase2_pat_series_by_experiment")
    pjb_sum = bundle.get("per_job_backtest_jobs")
    if phase2_pat_series_mapping_has_evaluable_series(root_ps):
        out["phase2_pat_series_auto_merge_skipped"] = True
    elif (
        isinstance(pjb_sum, Mapping)
        and pjb_sum.get("executed") is True
        and isinstance(bundle.get("backtest_metrics"), Mapping)
    ):
        shared_hint = evaluators.extract_phase2_shared_precision_at_recall_1pct(
            bundle.get("backtest_metrics")
        )
        out["phase2_pat_series_auto_merge_eligible"] = shared_hint is not None
    ps_root = bundle.get("phase2_pat_series_by_experiment")
    n_keys = 0
    max_len = 0
    n_ge2 = 0
    if isinstance(ps_root, Mapping):
        for tkey, exp_map in ps_root.items():
            tr = str(tkey).strip()
            if not tr.startswith("track_"):
                continue
            if not isinstance(exp_map, Mapping):
                continue
            for raw_list in exp_map.values():
                if not isinstance(raw_list, list):
                    continue
                n_keys += 1
                ell = len(raw_list)
                if ell > max_len:
                    max_len = ell
                if ell >= 2:
                    n_ge2 += 1
    if n_keys > 0:
        out["phase2_pat_series_key_count"] = n_keys
        out["phase2_pat_series_max_len"] = max_len
        out["phase2_pat_series_len_ge_2_count"] = n_ge2
    return out


def phase2_pat_series_mapping_has_evaluable_series(series_root: Any) -> bool:
    """True when any ``track_*`` experiment has a PAT@1% list usable by the std gate (len >= 2)."""
    if not isinstance(series_root, Mapping):
        return False
    for tkey, exp_map in series_root.items():
        tr = str(tkey).strip()
        if not tr.startswith("track_"):
            continue
        if not isinstance(exp_map, Mapping):
            continue
        for raw_list in exp_map.values():
            if isinstance(raw_list, list) and len(raw_list) >= 2:
                return True
    return False


def merge_phase2_pat_series_from_shared_and_per_job(bundle: dict[str, Any]) -> bool:
    """Fill ``phase2_pat_series_by_experiment`` from shared ingest + per-job previews (MVP).

    For each successful per-job backtest row, prefers optional
    ``precision_at_recall_1pct_by_window_preview`` (list[float]) when available and
    length >= 2. Otherwise falls back to numeric
    ``shared_precision_at_recall_1pct_preview`` with a two-sample bridge
    ``[shared_pat, preview]`` where ``shared_pat`` is parsed from ingested
    ``backtest_metrics`` (same path as the Phase 2 gate shared PAT extractor).

    This is a **bridge** for ``max_std_pp_across_windows``: two points yield a sample
    stdev (often interpreted as shared vs per-job delta), not a full multi-window
    matrix. Skips when the bundle already contains any evaluable manual series
    (``phase2_pat_series_mapping_has_evaluable_series``).
    When merging into an existing ``phase2_pat_series_by_experiment`` (e.g. from
    YAML ``precision_at_recall_1pct_by_window``), only fills missing or empty
    ``(track, exp_id)`` entries; never overwrites a non-empty list.

    Args:
        bundle: Mutable Phase 2 bundle.

    Also writes/merges optional provenance map
    ``phase2_pat_series_source_by_experiment`` with per-series source tags:
    ``per_job_window_series`` or ``shared_bridge`` and optional ``window_ids``.

    Returns:
        True if ``bundle`` gained a non-empty ``phase2_pat_series_by_experiment``.
    """
    if phase2_pat_series_mapping_has_evaluable_series(
        bundle.get("phase2_pat_series_by_experiment")
    ):
        return False
    pjb = bundle.get("per_job_backtest_jobs")
    if not isinstance(pjb, Mapping) or pjb.get("executed") is not True:
        return False
    bm = bundle.get("backtest_metrics")
    shared = evaluators.extract_phase2_shared_precision_at_recall_1pct(
        bm if isinstance(bm, Mapping) else None
    )
    if shared is None:
        return False
    results = pjb.get("results")
    if not isinstance(results, list):
        return False
    out_root: dict[str, dict[str, list[float]]] = {}
    src_root: dict[str, dict[str, dict[str, Any]]] = {}
    for row in results:
        if not isinstance(row, Mapping) or row.get("skipped"):
            continue
        if row.get("ok") is not True:
            continue
        tr = str(row.get("track") or "").strip()
        eid = str(row.get("exp_id") or "").strip()
        seq_preview = row.get("precision_at_recall_1pct_by_window_preview")
        if (
            isinstance(seq_preview, list)
            and len(seq_preview) >= 2
            and tr
            and eid
            and tr.startswith("track_")
        ):
            seq_vals: list[float] = []
            bad_seq = False
            for x in seq_preview:
                try:
                    seq_vals.append(float(x))
                except (TypeError, ValueError):
                    bad_seq = True
                    break
            if not bad_seq and seq_vals:
                out_root.setdefault(tr, {})[eid] = seq_vals
                ids_raw = row.get("precision_at_recall_1pct_window_ids_preview")
                ids: list[str] | None = None
                if isinstance(ids_raw, list) and ids_raw:
                    ids = [str(x) for x in ids_raw]
                src_row: dict[str, Any] = {"source": "per_job_window_series"}
                if ids is not None:
                    src_row["window_ids"] = ids
                src_root.setdefault(tr, {})[eid] = src_row
                continue
        prev = row.get("shared_precision_at_recall_1pct_preview")
        if not tr or not eid or prev is None:
            continue
        try:
            pv = float(prev)
            sv = float(shared)
        except (TypeError, ValueError):
            continue
        if not tr.startswith("track_"):
            continue
        exp_map = out_root.setdefault(tr, {})
        exp_map[eid] = [sv, pv]
        src_root.setdefault(tr, {})[eid] = {"source": "shared_bridge"}
    if not out_root:
        return False
    existing = bundle.get("phase2_pat_series_by_experiment")
    merged: dict[str, Any] = (
        copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
    )
    changed = False
    src_existing = bundle.get("phase2_pat_series_source_by_experiment")
    src_merged: dict[str, Any] = (
        copy.deepcopy(dict(src_existing)) if isinstance(src_existing, Mapping) else {}
    )
    src_changed = False
    for tr, em in out_root.items():
        cur = merged.setdefault(tr, {})
        if not isinstance(cur, dict):
            cur = {}
            merged[tr] = cur
        for eid, seq in em.items():
            prev_list = cur.get(eid)
            if isinstance(prev_list, list) and len(prev_list) > 0:
                continue
            cur[eid] = list(seq)
            changed = True
            tr_src = src_root.get(tr, {})
            src_row = tr_src.get(eid)
            if isinstance(src_row, Mapping):
                cur_src = src_merged.setdefault(tr, {})
                if not isinstance(cur_src, dict):
                    cur_src = {}
                    src_merged[tr] = cur_src
                cur_src[eid] = dict(src_row)
                src_changed = True
    if not changed:
        return False
    bundle["phase2_pat_series_by_experiment"] = merged
    if src_changed:
        bundle["phase2_pat_series_source_by_experiment"] = src_merged
    return True


def collect_summary_for_run_state(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Smaller JSON-safe view for embedding inside ``run_state.json``."""
    r1f = bundle.get("r1_r6_final") if isinstance(bundle.get("r1_r6_final"), dict) else {}
    r1m = bundle.get("r1_r6_mid") if isinstance(bundle.get("r1_r6_mid"), dict) else {}
    stats = bundle.get("state_db_stats") if isinstance(bundle.get("state_db_stats"), dict) else {}
    pit = bundle.get("pit_parity") if isinstance(bundle.get("pit_parity"), dict) else {}
    return {
        "error_count": len(bundle.get("errors") or []),
        "error_codes": [e.get("code", "") for e in (bundle.get("errors") or [])],
        "has_backtest_metrics": bundle.get("backtest_metrics") is not None,
        "has_r1_final_payload": r1f.get("payload") is not None,
        "has_r1_mid_payload": r1m.get("payload") is not None,
        "finalized_alerts_count": stats.get("finalized_alerts_count"),
        "finalized_true_positives_count": stats.get("finalized_true_positives_count"),
        "pit_parity_status": pit.get("status"),
        "pit_scored_at_in_window_ratio": pit.get("scored_at_in_window_ratio"),
        "pit_validated_at_non_null_ratio": pit.get("validated_at_non_null_ratio"),
    }
