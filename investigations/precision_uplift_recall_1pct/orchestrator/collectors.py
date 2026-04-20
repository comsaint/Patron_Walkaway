"""Collect backtest / R1-R6 / DB metrics into a unified dict (MVP T4)."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import evaluators
import slice_contract
import yaml

DEFAULT_BACKTEST_METRICS = "trainer/out_backtest/backtest_metrics.json"

_ORCHESTRATOR_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _ORCHESTRATOR_DIR.parents[2]

ERR_BACKTEST_METRICS = "E_COLLECT_BACKTEST_METRICS"
ERR_R1_PAYLOAD = "E_COLLECT_R1_PAYLOAD"
ERR_STATE_DB_STATS = "E_COLLECT_STATE_DB"
ERR_STATUS_HISTORY_REGISTRY = "E_COLLECT_STATUS_HISTORY_REGISTRY"
ERR_SLICE_CONTRACT = "E_COLLECT_SLICE_CONTRACT"
_MID_CP_STEM_RE = re.compile(r"^r1_r6_mid_cp(\d+)\.stdout\.log$")
_SLICE_AUTO_EVAL_ROWS_LIMIT_DEFAULT = 100_000

# STATUS.md can grow very large; Phase 1 crosscheck only scans the prefix (newest content first).
_STATUS_HISTORY_MD_SCAN_MAX_CHARS = 256_000
_STATUS_HISTORY_KEYWORDS: tuple[str, ...] = (
    "label noise",
    "delayed label",
    "label delay",
    "censored",
    "leakage",
    "lookahead",
    "point-in-time",
    "label contract",
    "contract drift",
    "標註噪音",
    "延遲標註",
    "標註延遲",
    "標籤噪音",
    "標籤延遲",
    "截尾",
    "時點對齊",
    "契約不一致",
)
_STATUS_HISTORY_PATTERN: re.Pattern[str] | None = None


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


def _phase2_training_metrics_pat_preview(obj: Mapping[str, Any]) -> float | None:
    """Parse PAT@1% from a ``training_metrics.json`` or backtest-shaped object (T10)."""
    return evaluators.extract_phase2_precision_at_recall_1pct_from_metrics_mapping(obj)


def validate_phase2_training_metrics_after_trainer_jobs(
    repo_root: Path,
    bundle: Mapping[str, Any],
) -> tuple[bool, str | None, str | None]:
    """Fail-fast after successful per-job trainer runs: metrics file exists and PAT@1% parseable.

    When ``trainer_jobs.executed`` is true and ``all_ok`` is true, each ``job_specs`` row
    that has a matching successful trainer result must resolve to an on-disk
    ``training_metrics.json`` (or YAML-hinted path) with a parseable PAT@1%
    (``model_default.<key>`` or trainer ``rated.<key>``). Missing file or
    invalid JSON → ``E_ARTIFACT_MISSING``; readable JSON without PAT → ``E_NO_DATA_WINDOW``.

    Returns:
        ``(True, None, None)`` when validation is skipped or passes; else
        ``(False, error_code, message)``.
    """
    tj = bundle.get("trainer_jobs")
    if not isinstance(tj, Mapping) or not tj.get("executed") or not tj.get("all_ok"):
        return True, None, None
    results = tj.get("results")
    if not isinstance(results, list):
        return True, None, None
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for r in results:
        if not isinstance(r, Mapping) or not r.get("ok"):
            continue
        tr = str(r.get("track") or "").strip()
        eid = str(r.get("exp_id") or "").strip()
        if tr and eid:
            by_key[(tr, eid)] = r
    specs = bundle.get("job_specs")
    if not isinstance(specs, list):
        return True, None, None
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        tr = str(spec.get("track") or "").strip()
        eid = str(spec.get("exp_id") or "").strip()
        if not tr or not eid:
            continue
        if (tr, eid) not in by_key:
            continue
        path, rel_disp, pre_err = _phase2_job_training_metrics_path(repo_root, spec)
        if pre_err is not None:
            return False, "E_ARTIFACT_MISSING", f"{tr}/{eid}: {pre_err}"
        assert path is not None
        if not path.is_file():
            return (
                False,
                "E_ARTIFACT_MISSING",
                f"{tr}/{eid}: training_metrics artifact missing at {rel_disp}",
            )
        obj, err = _load_json_object(path)
        if obj is None:
            return (
                False,
                "E_ARTIFACT_MISSING",
                f"{tr}/{eid}: {err or 'invalid training_metrics JSON'} ({rel_disp})",
            )
        if _phase2_training_metrics_pat_preview(obj) is None:
            prk = evaluators.PHASE2_BACKTEST_PR1_KEY
            return (
                False,
                "E_NO_DATA_WINDOW",
                f"{tr}/{eid}: training_metrics lacks parseable {prk} "
                f"(expected under model_default or rated) ({rel_disp})",
            )
    return True, None, None


def _index_phase2_rows_by_track_exp(
    rows: list[Any] | None,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Index list of dict rows by ``(track, exp_id)``."""
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        tr = str(r.get("track") or "").strip()
        eid = str(r.get("exp_id") or "").strip()
        if tr and eid:
            out[(tr, eid)] = r
    return out


def build_phase2_experiment_matrix(bundle: dict[str, Any]) -> None:
    """Populate ``bundle['phase2_experiment_matrix']`` — unified per-experiment summary (T10).

    Merges ``job_specs``, ``trainer_jobs.results``, ``job_training_harvest.rows``,
    ``per_job_backtest_jobs.results``, and optional shared ``backtest_metrics`` PAT@1%
    into one list for gate/report consumers.
    """
    specs = bundle.get("job_specs")
    if not isinstance(specs, list):
        return
    tj = bundle.get("trainer_jobs")
    tj_rows = tj.get("results") if isinstance(tj, Mapping) else []
    tj_map = _index_phase2_rows_by_track_exp(
        tj_rows if isinstance(tj_rows, list) else None
    )
    jh = bundle.get("job_training_harvest")
    jh_list = jh.get("rows") if isinstance(jh, Mapping) else None
    jh_map = _index_phase2_rows_by_track_exp(
        jh_list if isinstance(jh_list, list) else None
    )
    pjb = bundle.get("per_job_backtest_jobs")
    pj_list = pjb.get("results") if isinstance(pjb, Mapping) else None
    pj_map = _index_phase2_rows_by_track_exp(
        pj_list if isinstance(pj_list, list) else None
    )
    bm = bundle.get("backtest_metrics")
    shared_pat = evaluators.extract_phase2_shared_precision_at_recall_1pct(
        bm if isinstance(bm, Mapping) else None
    )
    out_rows: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        tr = str(spec.get("track") or "").strip()
        eid = str(spec.get("exp_id") or "").strip()
        if not tr or not eid:
            continue
        key = (tr, eid)
        tjr = tj_map.get(key)
        jhr = jh_map.get(key)
        pjr = pj_map.get(key)
        tm_obj = jhr.get("training_metrics") if isinstance(jhr, Mapping) else None
        train_preview = (
            _phase2_training_metrics_pat_preview(tm_obj)
            if isinstance(tm_obj, Mapping)
            else None
        )
        row: dict[str, Any] = {
            "track": tr,
            "exp_id": eid,
            "logs_subdir_relative": str(spec.get("logs_subdir_relative") or "").strip(),
            "training_metrics_repo_relative": str(
                spec.get("training_metrics_repo_relative") or ""
            ).strip(),
            "trainer_job_ok": bool(tjr.get("ok")) if isinstance(tjr, Mapping) else None,
            "trainer_argv_fingerprint": (
                str(tjr.get("argv_fingerprint") or "").strip()
                if isinstance(tjr, Mapping)
                else ""
            ),
            "job_training_harvest_found": (
                bool(jhr.get("found")) if isinstance(jhr, Mapping) else None
            ),
            "training_precision_at_recall_1pct": train_preview,
            "per_job_backtest_skipped": (
                bool(pjr.get("skipped")) if isinstance(pjr, Mapping) else None
            ),
            "per_job_backtest_ok": (
                bool(pjr.get("ok"))
                if isinstance(pjr, Mapping) and not bool(pjr.get("skipped"))
                else None
            ),
            "per_job_backtest_pat_preview": (
                pjr.get("shared_precision_at_recall_1pct_preview")
                if isinstance(pjr, Mapping)
                else None
            ),
        }
        out_rows.append(row)
    bundle["phase2_experiment_matrix"] = {
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "shared_precision_at_recall_1pct": shared_pat,
        "rows": out_rows,
    }


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


def collect_phase1_state_db_observe_counts(
    repo_root: Path,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """Lightweight ``validation_results`` aggregates for autonomous observe ticks.

    Reuses :func:`_collect_state_db_window_stats` (COUNT-only, read-only SQLite)
    without loading backtest metrics or R1 stdout logs.

    Args:
        repo_root: Repository root for resolving ``cfg['state_db_path']``.
        cfg: Phase 1 config with ``window`` and ``state_db_path``.

    Returns:
        JSON-serializable counts: ``finalized_alerts_count``,
        ``finalized_true_positives_count``, ``validation_results_rows_in_window``,
        optional ``state_db_note``, and ``collect_errors`` when aggregation fails.
    """
    errors: list[dict[str, str]] = []
    window_raw = cfg.get("window")
    window = window_raw if isinstance(window_raw, Mapping) else {}
    raw = cfg.get("state_db_path", "")
    state_path = (
        _resolve_under_root(repo_root, str(raw)) if str(raw).strip() else Path()
    )
    stats = _collect_state_db_window_stats(state_path, window, errors)
    out: dict[str, Any] = {
        "finalized_alerts_count": stats.get("finalized_alerts_count"),
        "finalized_true_positives_count": stats.get("finalized_true_positives_count"),
        "validation_results_rows_in_window": stats.get("validation_results_rows_in_window"),
        "state_db_note": stats.get("note"),
    }
    if errors:
        out["collect_errors"] = errors
    return out


def _safe_ratio(num: int, den: int) -> float | None:
    """Return ``num / den`` when denominator is positive; else ``None``."""
    if den <= 0:
        return None
    return float(num) / float(den)


def _capped_unit_ratio(num: int, den: int) -> float | None:
    """Return ``min(1.0, num/den)`` when ``den > 0``; else ``None``."""
    if den <= 0:
        return None
    return min(1.0, float(num) / float(den))


def collect_phase1_pit_parity(
    prediction_log_db: Path,
    state_db: Path,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    """Public entry for Phase 1 PIT parity diagnostics (delegates to internal collector)."""

    return _collect_phase1_pit_parity(prediction_log_db, state_db, window)


def _collect_phase1_pit_parity(
    prediction_log_db: Path,
    state_db: Path,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    """Collect Phase 1 PIT parity diagnostics (MVP auto metrics, non-blocking).

    ``scored_at_in_window_ratio`` compares the same ``[start_ts, end_ts)`` window as R2:
    ``prediction_log`` rows filtered by ``scored_at`` vs ``alerts`` rows filtered by ``ts``.
    Uses the R2 slice ``is_alert = 1 AND is_rated_obs = 1`` when those columns exist;
    otherwise falls back to all ``prediction_log`` rows in the ``scored_at`` window.
    The ratio is ``min(1, numerator/denominator)`` so duplicate-suppression (PL > alerts)
    does not read as a parity failure.
    """
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
    n_pl_all_in_window: int | None = None
    n_pl_alert_rated_in_window: int | None = None
    n_alerts_in_window: int | None = None

    if prediction_log_db.is_file():
        try:
            with sqlite3.connect(f"file:{prediction_log_db}?mode=ro", uri=True) as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(prediction_log)").fetchall()}
                if "scored_at" in cols:
                    win_params = (start_ts, end_ts)
                    if "is_alert" in cols and "is_rated_obs" in cols:
                        row_ct = conn.execute(
                            """
                            SELECT COUNT(*),
                                   SUM(CASE WHEN is_alert = 1 AND is_rated_obs = 1 THEN 1 ELSE 0 END)
                            FROM prediction_log
                            WHERE scored_at >= ? AND scored_at < ?
                            """,
                            win_params,
                        ).fetchone()
                        n_pl_all_in_window = int(row_ct[0])
                        rated_sum = row_ct[1]
                        n_pl_alert_rated_in_window = (
                            int(rated_sum) if rated_sum is not None else 0
                        )
                    else:
                        n_pl_all_in_window = int(
                            conn.execute(
                                "SELECT COUNT(*) FROM prediction_log WHERE scored_at >= ? AND scored_at < ?",
                                win_params,
                            ).fetchone()[0]
                        )
                        reasons.append("prediction_log_missing_is_alert_or_is_rated_obs_for_pit_ratio")
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
                    n_alerts_in_window = n_alerts
                    gap = out.get("alerts_vs_prediction_log_gap")
                    if isinstance(gap, int):
                        out["alerts_vs_prediction_log_gap"] = int(gap + n_alerts)
                else:
                    reasons.append("alerts_missing_ts")
        except sqlite3.Error:
            reasons.append("state_db_unavailable_for_pit")
    else:
        reasons.append("state_db_not_found_for_pit")

    if (
        n_alerts_in_window is not None
        and n_alerts_in_window > 0
        and n_pl_all_in_window is not None
    ):
        numer = (
            n_pl_alert_rated_in_window
            if n_pl_alert_rated_in_window is not None
            else n_pl_all_in_window
        )
        out["scored_at_in_window_ratio"] = _capped_unit_ratio(numer, n_alerts_in_window)
        out["scored_at_window_coverage_counts"] = {
            "prediction_log_rows_scored_at_window": n_pl_all_in_window,
            "prediction_log_alert_rated_rows_scored_at_window": n_pl_alert_rated_in_window,
            "alerts_rows_ts_window": n_alerts_in_window,
        }
    elif n_pl_all_in_window is not None and (n_alerts_in_window is None or n_alerts_in_window <= 0):
        out["scored_at_in_window_ratio"] = None
        if n_alerts_in_window is not None and n_alerts_in_window == 0:
            reasons.append("no_alerts_in_window_for_scored_at_coverage_ratio")

    out["reasons"] = reasons
    if reasons:
        out["status"] = "warn"
    return out


def _collect_slice_eval_rows_from_sqlite(
    prediction_log_db: Path,
    state_db: Path,
    window: Mapping[str, Any],
    *,
    max_rows: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build rated/labeled eval rows for ``slice_contract`` from SQLite artifacts.

    Returns ``(eval_rows, notes)`` and never raises; notes describe degraded paths.
    """
    notes: list[str] = []
    start_ts = str(window.get("start_ts", ""))
    end_ts = str(window.get("end_ts", ""))
    out: list[dict[str, Any]] = []
    if not prediction_log_db.is_file():
        return out, ["prediction_log_db_not_found"]
    try:
        with sqlite3.connect(f"file:{prediction_log_db}?mode=ro", uri=True) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(prediction_log)").fetchall()
            }
            required = {"bet_id", "canonical_id", "scored_at"}
            if not required.issubset(cols):
                missing = sorted(required - cols)
                return out, [f"prediction_log_missing_cols:{','.join(missing)}"]
            has_table_id = "table_id" in cols
            has_score = "score" in cols
            rated_clause = " AND is_rated_obs = 1" if "is_rated_obs" in cols else ""
            if not rated_clause:
                notes.append("prediction_log_missing_is_rated_obs")
            select_cols = "bet_id, canonical_id, scored_at"
            if has_table_id:
                select_cols += ", table_id"
            if has_score:
                select_cols += ", score"
            sql = (
                f"SELECT {select_cols} FROM prediction_log "
                "WHERE scored_at >= ? AND scored_at < ?"
                f"{rated_clause} ORDER BY scored_at ASC LIMIT ?"
            )
            rows = conn.execute(sql, (start_ts, end_ts, max_rows + 1)).fetchall()
    except sqlite3.Error:
        return out, ["prediction_log_db_unavailable_for_slice_auto_eval_rows"]

    if not rows:
        return out, ["prediction_log_no_rows_in_window"]
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        notes.append(f"slice_eval_rows_truncated_at:{max_rows}")

    pred_rows: list[dict[str, Any]] = []
    for row in rows:
        bet_id = str(row[0] or "").strip()
        cid = str(row[1] or "").strip()
        scored_at = str(row[2] or "").strip()
        if not bet_id or not cid or not scored_at:
            continue
        idx = 3
        table_id = row[idx] if has_table_id else None
        idx += 1 if has_table_id else 0
        score = row[idx] if has_score else None
        pred_rows.append(
            {
                "bet_id": bet_id,
                "canonical_id": cid,
                "decision_ts": scored_at,
                "table_id": table_id,
                "score": score,
            }
        )
    if not pred_rows:
        return out, notes + ["prediction_log_rows_invalid_for_slice_eval_rows"]

    labels: dict[str, int] = {}
    if state_db.is_file():
        try:
            with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as conn:
                vcols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(validation_results)").fetchall()
                }
                if "bet_id" in vcols and "result" in vcols:
                    fin_clause = ""
                    if "validated_at" in vcols:
                        fin_clause = (
                            " AND TRIM(COALESCE(validated_at, '')) != ''"
                        )
                    chunk = 800
                    for i in range(0, len(pred_rows), chunk):
                        bids = [r["bet_id"] for r in pred_rows[i : i + chunk]]
                        placeholders = ",".join(["?"] * len(bids))
                        sql = (
                            "SELECT bet_id, result FROM validation_results "
                            f"WHERE bet_id IN ({placeholders}){fin_clause}"
                        )
                        for bid_raw, res_raw in conn.execute(sql, tuple(bids)).fetchall():
                            bid = str(bid_raw or "").strip()
                            if not bid:
                                continue
                            try:
                                labels[bid] = int(res_raw)
                            except (TypeError, ValueError):
                                continue
                else:
                    notes.append("validation_results_missing_bet_id_or_result")
        except sqlite3.Error:
            notes.append("state_db_unavailable_for_slice_auto_eval_rows")
    else:
        notes.append("state_db_not_found_for_slice_auto_eval_rows")

    dropped_unlabeled = 0
    for r in pred_rows:
        lab = labels.get(r["bet_id"])
        if lab is None:
            dropped_unlabeled += 1
            continue
        out.append(
            {
                "bet_id": r["bet_id"],
                "canonical_id": r["canonical_id"],
                "decision_ts": r["decision_ts"],
                "table_id": r["table_id"],
                "score": r["score"],
                "label": int(lab),
            }
        )
    if dropped_unlabeled > 0:
        notes.append(f"slice_eval_rows_dropped_unlabeled:{dropped_unlabeled}")
    return out, notes


def _collect_slice_profiles_from_state_db(
    state_db: Path,
    canonical_ids: list[str],
    *,
    profile_table: str,
    t0_raw: Any = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load minimal profile fields for target canonical IDs from state DB table."""
    notes: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    t0_dt: datetime | None = None
    if t0_raw is not None:
        t0_s = str(t0_raw).strip()
        if t0_s:
            ts = t0_s[:-1] + "+00:00" if t0_s.endswith("Z") else t0_s
            try:
                t0_dt = datetime.fromisoformat(ts)
            except ValueError:
                notes.append("slice_profiles_t0_unparseable_fallback_no_asof")
    if not canonical_ids:
        return out, ["slice_profiles_no_canonical_ids"]
    if not state_db.is_file():
        return out, ["state_db_not_found_for_slice_profiles"]
    try:
        with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as conn:
            cols = {
                row[1]
                for row in conn.execute(f"PRAGMA table_info({profile_table})").fetchall()
            }
            required = {
                "canonical_id",
                "theo_win_sum_30d",
                "active_days_30d",
                "turnover_sum_30d",
                "days_since_first_session",
            }
            if not required.issubset(cols):
                miss = sorted(required - cols)
                return out, [f"player_profile_missing_cols:{','.join(miss)}"]
            has_asof = "as_of_ts" in cols
            if not has_asof:
                notes.append("player_profile_missing_as_of_ts_fallback_no_asof")
            elif t0_dt is None:
                notes.append("slice_profiles_missing_t0_or_unparseable_fallback_no_asof")
            uniq_ids = sorted({str(x).strip() for x in canonical_ids if str(x).strip()})
            chunk = 800
            asof_parse_err_count = 0
            asof_after_t0_drop_count = 0
            asof_no_candidate_count = 0
            chosen_asof: dict[str, datetime] = {}
            for i in range(0, len(uniq_ids), chunk):
                part = uniq_ids[i : i + chunk]
                placeholders = ",".join(["?"] * len(part))
                select_cols = (
                    "canonical_id, theo_win_sum_30d, active_days_30d, "
                    "turnover_sum_30d, days_since_first_session"
                )
                if has_asof:
                    select_cols += ", as_of_ts"
                sql = f"SELECT {select_cols} FROM {profile_table} WHERE canonical_id IN ({placeholders})"
                for row in conn.execute(sql, tuple(part)).fetchall():
                    cid = str(row[0] or "").strip()
                    if not cid:
                        continue
                    if has_asof and t0_dt is not None:
                        as_of_raw = row[5]
                        as_of_s = str(as_of_raw or "").strip()
                        if not as_of_s:
                            asof_no_candidate_count += 1
                            continue
                        as_of_norm = as_of_s[:-1] + "+00:00" if as_of_s.endswith("Z") else as_of_s
                        try:
                            as_of_dt = datetime.fromisoformat(as_of_norm)
                        except ValueError:
                            asof_parse_err_count += 1
                            continue
                        if as_of_dt > t0_dt:
                            asof_after_t0_drop_count += 1
                            continue
                        prev = chosen_asof.get(cid)
                        if prev is not None and as_of_dt <= prev:
                            continue
                        chosen_asof[cid] = as_of_dt
                    elif cid in out:
                        continue
                    out[cid] = {
                        "theo_win_sum_30d": row[1],
                        "active_days_30d": row[2],
                        "turnover_sum_30d": row[3],
                        "days_since_first_session": row[4],
                    }
            if has_asof and t0_dt is not None:
                missing_after_filter = sum(1 for cid in uniq_ids if cid not in out)
                if asof_parse_err_count > 0:
                    notes.append(f"slice_profiles_as_of_parse_errors:{asof_parse_err_count}")
                if asof_after_t0_drop_count > 0:
                    notes.append(f"slice_profiles_as_of_after_t0_dropped:{asof_after_t0_drop_count}")
                if asof_no_candidate_count > 0:
                    notes.append(f"slice_profiles_as_of_missing:{asof_no_candidate_count}")
                if missing_after_filter > 0:
                    notes.append(f"slice_profiles_no_asof_profile_at_or_before_t0:{missing_after_filter}")
    except sqlite3.Error:
        return out, ["state_db_unavailable_for_slice_profiles"]
    return out, notes


def _collect_slice_profiles_from_parquet(
    parquet_path: Path,
    canonical_ids: list[str],
    *,
    t0_raw: Any = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Best-effort Parquet fallback for slice profiles (small-ID set only)."""
    notes: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    if not canonical_ids:
        return out, ["slice_profiles_no_canonical_ids"]
    if not parquet_path.is_file():
        return out, ["slice_profiles_parquet_not_found"]

    t0_dt: datetime | None = None
    t0_s = str(t0_raw or "").strip()
    if t0_s:
        ts = t0_s[:-1] + "+00:00" if t0_s.endswith("Z") else t0_s
        try:
            t0_dt = datetime.fromisoformat(ts)
        except ValueError:
            notes.append("slice_profiles_t0_unparseable_fallback_no_asof")

    try:
        import duckdb  # type: ignore
    except Exception:
        return out, ["slice_profiles_parquet_duckdb_unavailable"]

    uniq_ids = sorted({str(x).strip() for x in canonical_ids if str(x).strip()})
    if not uniq_ids:
        return out, ["slice_profiles_no_canonical_ids"]
    try:
        con = duckdb.connect(":memory:")
        cols = {
            str(r[0])
            for r in con.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)",
                [str(parquet_path)],
            ).fetchall()
        }
        required = {
            "canonical_id",
            "theo_win_sum_30d",
            "active_days_30d",
            "turnover_sum_30d",
            "days_since_first_session",
        }
        if not required.issubset(cols):
            miss = sorted(required - cols)
            con.close()
            return out, [f"slice_profiles_parquet_missing_cols:{','.join(miss)}"]
        has_asof = "as_of_ts" in cols
        if not has_asof:
            notes.append("slice_profiles_parquet_missing_as_of_ts_fallback_no_asof")
        elif t0_dt is None:
            notes.append("slice_profiles_missing_t0_or_unparseable_fallback_no_asof")

        id_tuple = tuple(uniq_ids)
        base_sql = (
            "SELECT canonical_id, theo_win_sum_30d, active_days_30d, "
            "turnover_sum_30d, days_since_first_session"
        )
        if has_asof:
            base_sql += ", as_of_ts"
        base_sql += " FROM read_parquet(?) WHERE canonical_id IN ?"
        rows = con.execute(base_sql, [str(parquet_path), id_tuple]).fetchall()
        con.close()
    except Exception:
        return out, ["slice_profiles_parquet_query_failed"]

    chosen_asof: dict[str, datetime] = {}
    asof_drop = 0
    asof_parse_err = 0
    for row in rows:
        cid = str(row[0] or "").strip()
        if not cid:
            continue
        if has_asof and t0_dt is not None:
            as_of_s = str(row[5] or "").strip()
            if not as_of_s:
                continue
            as_of_norm = as_of_s[:-1] + "+00:00" if as_of_s.endswith("Z") else as_of_s
            try:
                as_of_dt = datetime.fromisoformat(as_of_norm)
            except ValueError:
                asof_parse_err += 1
                continue
            if as_of_dt > t0_dt:
                asof_drop += 1
                continue
            prev = chosen_asof.get(cid)
            if prev is not None and as_of_dt <= prev:
                continue
            chosen_asof[cid] = as_of_dt
        elif cid in out:
            continue
        out[cid] = {
            "theo_win_sum_30d": row[1],
            "active_days_30d": row[2],
            "turnover_sum_30d": row[3],
            "days_since_first_session": row[4],
        }
    if asof_drop > 0:
        notes.append(f"slice_profiles_parquet_as_of_after_t0_dropped:{asof_drop}")
    if asof_parse_err > 0:
        notes.append(f"slice_profiles_parquet_as_of_parse_errors:{asof_parse_err}")
    return out, notes


def _collect_slice_profiles_from_clickhouse(
    canonical_ids: list[str],
    *,
    t0_raw: Any = None,
    source_db: str = "",
    profile_table: str = "",
    max_ids: int = 5000,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Best-effort ClickHouse fallback for slice profiles (small-ID set only)."""
    notes: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    uniq_ids = sorted({str(x).strip() for x in canonical_ids if str(x).strip()})
    if not uniq_ids:
        return out, ["slice_profiles_no_canonical_ids"]
    if len(uniq_ids) > max_ids:
        return out, [f"slice_profiles_clickhouse_id_limit_exceeded:{len(uniq_ids)}>{max_ids}"]

    t0_dt: datetime | None = None
    t0_s = str(t0_raw or "").strip()
    if t0_s:
        ts = t0_s[:-1] + "+00:00" if t0_s.endswith("Z") else t0_s
        try:
            t0_dt = datetime.fromisoformat(ts)
        except ValueError:
            notes.append("slice_profiles_t0_unparseable_fallback_no_asof")
    try:
        from trainer import db_conn as _db_conn  # lazy import
        from trainer.core import config as _cfg  # lazy import
    except Exception:
        return out, ["slice_profiles_clickhouse_import_failed"]

    db = str(source_db or _cfg.SOURCE_DB).strip()
    table = str(profile_table or _cfg.TPROFILE).strip()
    if not db or not table:
        return out, ["slice_profiles_clickhouse_missing_source_table"]
    full_table = f"{db}.{table}"

    try:
        cli = _db_conn.get_clickhouse_client()
        ddf = cli.query_df(f"DESCRIBE TABLE {full_table}")
    except Exception:
        return out, ["slice_profiles_clickhouse_describe_failed"]
    if "name" not in getattr(ddf, "columns", []):
        return out, ["slice_profiles_clickhouse_describe_missing_name_col"]
    cols = {str(x) for x in ddf["name"].tolist()}
    required = {
        "canonical_id",
        "theo_win_sum_30d",
        "active_days_30d",
        "turnover_sum_30d",
        "days_since_first_session",
    }
    if not required.issubset(cols):
        miss = sorted(required - cols)
        return out, [f"slice_profiles_clickhouse_missing_cols:{','.join(miss)}"]
    has_asof = "as_of_ts" in cols
    if not has_asof:
        notes.append("slice_profiles_clickhouse_missing_as_of_ts_fallback_no_asof")
    elif t0_dt is None:
        notes.append("slice_profiles_missing_t0_or_unparseable_fallback_no_asof")

    chosen_asof: dict[str, datetime] = {}
    asof_drop = 0
    asof_parse_err = 0
    chunk = 800
    for i in range(0, len(uniq_ids), chunk):
        part = uniq_ids[i : i + chunk]
        sel = (
            "canonical_id, theo_win_sum_30d, active_days_30d, "
            "turnover_sum_30d, days_since_first_session"
        )
        if has_asof:
            sel += ", as_of_ts"
        sql = f"SELECT {sel} FROM {full_table} WHERE canonical_id IN %(cids)s"
        try:
            pdf = cli.query_df(sql, parameters={"cids": tuple(part)})
        except Exception:
            notes.append("slice_profiles_clickhouse_query_failed")
            continue
        if pdf is None or len(pdf) == 0:
            continue
        for row in pdf.itertuples(index=False):
            cid = str(getattr(row, "canonical_id", "") or "").strip()
            if not cid:
                continue
            if has_asof and t0_dt is not None:
                as_of_raw = getattr(row, "as_of_ts", None)
                as_of_s = str(as_of_raw or "").strip()
                if not as_of_s:
                    continue
                as_of_norm = as_of_s[:-1] + "+00:00" if as_of_s.endswith("Z") else as_of_s
                try:
                    as_of_dt = datetime.fromisoformat(as_of_norm)
                except ValueError:
                    asof_parse_err += 1
                    continue
                if as_of_dt > t0_dt:
                    asof_drop += 1
                    continue
                prev = chosen_asof.get(cid)
                if prev is not None and as_of_dt <= prev:
                    continue
                chosen_asof[cid] = as_of_dt
            elif cid in out:
                continue
            out[cid] = {
                "theo_win_sum_30d": getattr(row, "theo_win_sum_30d", None),
                "active_days_30d": getattr(row, "active_days_30d", None),
                "turnover_sum_30d": getattr(row, "turnover_sum_30d", None),
                "days_since_first_session": getattr(row, "days_since_first_session", None),
            }
    if asof_drop > 0:
        notes.append(f"slice_profiles_clickhouse_as_of_after_t0_dropped:{asof_drop}")
    if asof_parse_err > 0:
        notes.append(f"slice_profiles_clickhouse_as_of_parse_errors:{asof_parse_err}")
    return out, notes


def _slice_profiles_state_db_infeasible(notes: list[str]) -> bool:
    """Hard-failure notes that justify fallback away from state_db primary."""
    hard_prefixes = (
        "state_db_not_found_for_slice_profiles",
        "state_db_unavailable_for_slice_profiles",
        "player_profile_missing_cols:",
    )
    for n in notes:
        s = str(n)
        if s.startswith(hard_prefixes):
            return True
    return False


def _slice_profiles_asof_uncertain(notes: list[str]) -> bool:
    """True when notes indicate as-of evidence is unavailable or degraded."""
    prefixes = (
        "player_profile_missing_as_of_ts_fallback_no_asof",
        "slice_profiles_missing_t0_or_unparseable_fallback_no_asof",
        "slice_profiles_no_asof_profile_at_or_before_t0:",
        "slice_profiles_parquet_missing_as_of_ts_fallback_no_asof",
        "slice_profiles_clickhouse_missing_as_of_ts_fallback_no_asof",
    )
    for n in notes:
        s = str(n)
        if s.startswith(prefixes):
            return True
    return False


def _slice_contract_plan_section_hash(repo_root: Path) -> tuple[str | None, str | None]:
    """Return (sha256_hex, section_id) for PLAN sprint §7 text."""
    plan_path = repo_root / ".cursor" / "plans" / "PLAN_precision_uplift_sprint.md"
    if not plan_path.is_file():
        return None, None
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    m_start = re.search(
        r"^##\s+7\.\s+Phase 1 錯誤切片分析：分段定義（`slice_contract`）\s*$",
        text,
        re.MULTILINE,
    )
    if not m_start:
        return None, None
    start = m_start.start()
    tail = text[m_start.end() :]
    m_end = re.search(r"^##\s+8\.\s+", tail, re.MULTILINE)
    end = (m_start.end() + m_end.start()) if m_end else len(text)
    sec = text[start:end].strip()
    if not sec:
        return None, None
    h = hashlib.sha256(sec.encode("utf-8")).hexdigest()
    return h, "PLAN_precision_uplift_sprint.md#section-7"


def _status_history_keyword_pattern() -> re.Pattern[str]:
    """Compile lazily so module import stays light."""
    global _STATUS_HISTORY_PATTERN
    if _STATUS_HISTORY_PATTERN is None:
        escaped = [re.escape(w) for w in _STATUS_HISTORY_KEYWORDS]
        _STATUS_HISTORY_PATTERN = re.compile(
            rf"({'|'.join(escaped)})", re.IGNORECASE
        )
    return _STATUS_HISTORY_PATTERN


def _scan_status_md_keyword_hits(
    status_text: str, *, max_hits: int = 200
) -> list[dict[str, str]]:
    """Return section + evidence snippets (same semantics as build_status_history_crosscheck)."""
    pattern = _status_history_keyword_pattern()
    section = "Uncategorized"
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in status_text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            section = line.removeprefix("## ").strip()
            continue
        if not line or len(line) < 12:
            continue
        if pattern.search(line):
            evidence = line[:220]
            key = (section, evidence)
            if key not in seen:
                seen.add(key)
                candidates.append({"section": section, "evidence_snippet": evidence})
                if len(candidates) >= max_hits:
                    break
    return candidates


def _default_status_history_registry_path(repo_root: Path) -> Path:
    """Canonical registry path under the investigation ``phase1/`` tree."""
    return (
        repo_root
        / "investigations"
        / "precision_uplift_recall_1pct"
        / "phase1"
        / "status_history_registry.yaml"
    )


def _default_status_history_status_md_path(repo_root: Path) -> Path:
    return repo_root / ".cursor" / "plans" / "STATUS.md"


def collect_status_history_crosscheck(
    run_id: str,
    repo_root: Path,
    cfg: Mapping[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Load registry + optional STATUS keyword scan (W1-B1 machine-readable bundle).

    ``unresolved_blockers`` lists registry entries with ``blocks_phase1_decision`` and
    ``resolution_status`` in ``open`` / ``deferred`` (case-insensitive).
    """
    reg_rel = str(cfg.get("status_history_registry_path") or "").strip()
    reg_path = (
        _resolve_under_root(repo_root, reg_rel)
        if reg_rel
        else _default_status_history_registry_path(repo_root)
    )
    status_rel = str(cfg.get("status_history_status_md_path") or "").strip()
    status_path = (
        _resolve_under_root(repo_root, status_rel)
        if status_rel
        else _default_status_history_status_md_path(repo_root)
    )

    out: dict[str, Any] = {
        "run_id": run_id,
        "registry_path": str(reg_path),
        "status_md_path": str(status_path),
        "registry_schema_version": None,
        "registry_entries": [],
        "registry_load_error": None,
        "keyword_hits": [],
        "status_md_scan_truncated": False,
        "status_md_missing": False,
        "unresolved_blockers": [],
    }

    entries: list[dict[str, Any]] = []
    if reg_path.is_file():
        try:
            raw = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            msg = f"invalid YAML in status_history registry: {exc}"
            out["registry_load_error"] = msg
            _append_error(errors, ERR_STATUS_HISTORY_REGISTRY, msg, path=str(reg_path))
        else:
            if isinstance(raw, Mapping):
                out["registry_schema_version"] = raw.get("schema_version")
                raw_entries = raw.get("entries")
                if raw_entries is None:
                    raw_entries = []
                if not isinstance(raw_entries, list):
                    msg = "status_history registry 'entries' must be a list"
                    out["registry_load_error"] = msg
                    _append_error(
                        errors,
                        ERR_STATUS_HISTORY_REGISTRY,
                        msg,
                        path=str(reg_path),
                    )
                else:
                    for i, row in enumerate(raw_entries):
                        if not isinstance(row, Mapping):
                            continue
                        issue_id = str(row.get("issue_id") or "").strip()
                        if not issue_id:
                            continue
                        rs = str(row.get("resolution_status") or "").strip().lower()
                        entry = {
                            "issue_id": issue_id,
                            "title": str(row.get("title") or "").strip(),
                            "resolution_status": rs or "unknown",
                            "blocks_phase1_decision": bool(row.get("blocks_phase1_decision")),
                            "status_history_anchor": str(
                                row.get("status_history_anchor") or ""
                            ).strip(),
                            "owner": str(row.get("owner") or "").strip(),
                            "notes": str(row.get("notes") or "").strip(),
                        }
                        entries.append(entry)
            else:
                msg = "status_history registry root must be a mapping"
                out["registry_load_error"] = msg
                _append_error(
                    errors,
                    ERR_STATUS_HISTORY_REGISTRY,
                    msg,
                    path=str(reg_path),
                )
    else:
        out["registry_load_error"] = "registry file not found (optional path)"

    out["registry_entries"] = entries

    disable_kw = bool(cfg.get("status_history_disable_keyword_scan"))
    if not disable_kw and status_path.is_file():
        try:
            raw_text = status_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out["keyword_hits"] = []
            out["status_md_read_error"] = str(exc)
        else:
            scan_text = raw_text
            if len(scan_text) > _STATUS_HISTORY_MD_SCAN_MAX_CHARS:
                scan_text = scan_text[:_STATUS_HISTORY_MD_SCAN_MAX_CHARS]
                out["status_md_scan_truncated"] = True
            out["keyword_hits"] = _scan_status_md_keyword_hits(scan_text)
    elif not disable_kw:
        out["status_md_missing"] = True
        out["keyword_hits"] = []

    unresolved: list[dict[str, Any]] = []
    for e in entries:
        if not e.get("blocks_phase1_decision"):
            continue
        rs = str(e.get("resolution_status") or "").lower()
        if rs in {"open", "deferred"}:
            unresolved.append(
                {
                    "issue_id": e["issue_id"],
                    "title": e.get("title"),
                    "resolution_status": rs,
                }
            )
    out["unresolved_blockers"] = unresolved
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
    pit_parity = collect_phase1_pit_parity(
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
    bundle["status_history_crosscheck"] = collect_status_history_crosscheck(
        run_id, root, cfg_d, errors
    )
    sc_spec = cfg_d.get("slice_contract")
    if isinstance(sc_spec, Mapping):
        try:
            merged_sc = dict(sc_spec)
            profile_notes: list[str] = []
            eval_rows = merged_sc.get("eval_rows")
            auto_rows_enabled = bool(merged_sc.get("auto_eval_rows_from_prediction_log"))
            if (not isinstance(eval_rows, list)) and auto_rows_enabled:
                max_rows_raw = merged_sc.get(
                    "auto_eval_rows_limit", _SLICE_AUTO_EVAL_ROWS_LIMIT_DEFAULT
                )
                try:
                    max_rows = max(1, int(max_rows_raw))
                except (TypeError, ValueError):
                    max_rows = _SLICE_AUTO_EVAL_ROWS_LIMIT_DEFAULT
                auto_rows, auto_notes = _collect_slice_eval_rows_from_sqlite(
                    prediction_db_path,
                    state_db_path,
                    window if isinstance(window, Mapping) else {},
                    max_rows=max_rows,
                )
                merged_sc["eval_rows"] = auto_rows
                if auto_notes:
                    merged_sc["notes"] = "; ".join(auto_notes)
            profiles = merged_sc.get("profiles")
            auto_profiles_enabled = bool(merged_sc.get("auto_profiles_from_state_db"))
            if (not isinstance(profiles, Mapping) or not profiles) and auto_profiles_enabled:
                eval_rows_ready = merged_sc.get("eval_rows")
                cids: list[str] = []
                if isinstance(eval_rows_ready, list):
                    for r in eval_rows_ready:
                        if not isinstance(r, Mapping):
                            continue
                        cid = str(r.get("canonical_id") or "").strip()
                        if cid:
                            cids.append(cid)
                ptable = str(merged_sc.get("profile_table") or "player_profile").strip() or "player_profile"
                auto_profiles, profile_notes = _collect_slice_profiles_from_state_db(
                    state_db_path,
                    cids,
                    profile_table=ptable,
                    t0_raw=merged_sc.get("T0"),
                )
                if (not auto_profiles) and _slice_profiles_state_db_infeasible(profile_notes):
                    p_rel = str(merged_sc.get("profile_parquet_path") or "").strip()
                    if p_rel:
                        p_path = _resolve_under_root(root, p_rel)
                        parq_profiles, parq_notes = _collect_slice_profiles_from_parquet(
                            p_path,
                            cids,
                            t0_raw=merged_sc.get("T0"),
                        )
                        if parq_profiles:
                            auto_profiles = parq_profiles
                        profile_notes.extend(parq_notes)
                    if (not auto_profiles) and bool(merged_sc.get("auto_profiles_from_clickhouse")):
                        ch_profiles, ch_notes = _collect_slice_profiles_from_clickhouse(
                            cids,
                            t0_raw=merged_sc.get("T0"),
                            source_db=str(merged_sc.get("clickhouse_source_db") or "").strip(),
                            profile_table=str(merged_sc.get("clickhouse_profile_table") or "").strip(),
                        )
                        if ch_profiles:
                            auto_profiles = ch_profiles
                        profile_notes.extend(ch_notes)
                merged_sc["profiles"] = auto_profiles
                if profile_notes:
                    prior = str(merged_sc.get("notes") or "").strip()
                    extra = "; ".join(profile_notes)
                    merged_sc["notes"] = f"{prior}; {extra}".strip("; ").strip()
            if "recall_score_threshold" not in merged_sc:
                r1_thr = evaluators.extract_threshold_at_target_recall(r1_final)
                if r1_thr is not None:
                    merged_sc["recall_score_threshold"] = r1_thr
                else:
                    bt_thr = evaluators.extract_threshold_at_recall_0_01_from_backtest_metrics(
                        metrics_obj
                    )
                    if bt_thr is not None:
                        merged_sc["recall_score_threshold"] = bt_thr
            if isinstance(merged_sc.get("eval_rows"), list):
                sc_bundle = slice_contract.build_slice_contract_bundle(
                    merged_sc
                )
                notes_joined = str(merged_sc.get("notes") or "").strip()
                if notes_joined:
                    prior = str(sc_bundle.get("notes") or "").strip()
                    sc_bundle["notes"] = (
                        f"{prior}; {notes_joined}".strip("; ").strip()
                        if prior
                        else notes_joined
                    )
                asof_mode = str(merged_sc.get("asof_mode", "STRICT") or "STRICT").strip().upper()
                if asof_mode not in {"STRICT", "WARN_ONLY"}:
                    asof_mode = "STRICT"
                if asof_mode == "STRICT" and _slice_profiles_asof_uncertain(profile_notes):
                    sc_bundle["slice_data_incomplete"] = True
                    codes = sc_bundle.get("blocking_profile_codes")
                    out_codes = (
                        [str(c) for c in codes if str(c).strip()]
                        if isinstance(codes, (list, tuple))
                        else []
                    )
                    if "asof_contract_unavailable_strict" not in out_codes:
                        out_codes.append("asof_contract_unavailable_strict")
                    sc_bundle["blocking_profile_codes"] = sorted(set(out_codes))
                plan_h, plan_ref = _slice_contract_plan_section_hash(root)
                if plan_h is not None:
                    sc_bundle["slice_contract_plan_hash_sha256"] = plan_h
                    sc_bundle["slice_contract_plan_section"] = plan_ref
                if not str(sc_bundle.get("slice_contract_version") or "").strip():
                    if plan_h is not None:
                        sc_bundle["slice_contract_version"] = f"plan7-sha256:{plan_h[:16]}"
                    else:
                        sc_bundle["slice_contract_version"] = "inline-v1"
                bundle["slice_contract"] = sc_bundle
        except Exception as exc:  # noqa: BLE001 — surface as collect error + stub bundle
            _append_error(
                errors,
                ERR_SLICE_CONTRACT,
                str(exc),
                path="slice_contract:inline",
            )
            bundle["slice_contract"] = {
                "slice_contract_version": "inline-v1",
                "slice_data_incomplete": True,
                "slice_contract_violations": [],
                "blocking_profile_codes": ["slice_contract_exception"],
                "top_drag_slices": [],
                "row_annotations": [],
                "notes": str(exc),
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
    em = bundle.get("phase2_experiment_matrix")
    if isinstance(em, Mapping):
        er = em.get("rows")
        if isinstance(er, list):
            out["phase2_experiment_matrix_rows"] = len(er)
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
    sh = bundle.get("status_history_crosscheck")
    sh_n = 0
    if isinstance(sh, Mapping):
        ub = sh.get("unresolved_blockers")
        if isinstance(ub, list):
            sh_n = len(ub)
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
        "status_history_unresolved_blocker_count": sh_n,
    }
