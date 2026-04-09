"""Collect backtest / R1-R6 / DB metrics into a unified dict (MVP T4)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

DEFAULT_BACKTEST_METRICS = "trainer/out_backtest/backtest_metrics.json"

ERR_BACKTEST_METRICS = "E_COLLECT_BACKTEST_METRICS"
ERR_R1_PAYLOAD = "E_COLLECT_R1_PAYLOAD"
ERR_STATE_DB_STATS = "E_COLLECT_STATE_DB"


def _resolve_under_root(repo_root: Path, p: str | Path) -> Path:
    """Resolve ``p`` under ``repo_root`` when relative."""
    path = Path(p)
    return path if path.is_absolute() else (repo_root / path)


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

    r1_mid_payload: dict[str, Any] | None = None
    r1_mid_parse_err: str | None = None
    if r1_mid_path.is_file():
        r1_mid_payload, r1_mid_parse_err = _parse_r1_stdout_file(r1_mid_path)
        if r1_mid_parse_err:
            _append_error(
                errors,
                ERR_R1_PAYLOAD,
                f"mid: {r1_mid_parse_err}",
                path=str(r1_mid_path),
            )

    window = cfg_d.get("window") or {}
    state_db_raw = cfg_d.get("state_db_path", "")
    state_db_path = _resolve_under_root(root, str(state_db_raw)) if state_db_raw else Path()
    state_stats = _collect_state_db_window_stats(state_db_path, window, errors)

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
        "state_db_stats": state_stats,
        "thresholds": dict(cfg_d.get("thresholds") or {}),
        "window": dict(window) if isinstance(window, Mapping) else {},
    }
    return bundle


def collect_summary_for_run_state(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Smaller JSON-safe view for embedding inside ``run_state.json``."""
    r1f = bundle.get("r1_r6_final") if isinstance(bundle.get("r1_r6_final"), dict) else {}
    r1m = bundle.get("r1_r6_mid") if isinstance(bundle.get("r1_r6_mid"), dict) else {}
    stats = bundle.get("state_db_stats") if isinstance(bundle.get("state_db_stats"), dict) else {}
    return {
        "error_count": len(bundle.get("errors") or []),
        "error_codes": [e.get("code", "") for e in (bundle.get("errors") or [])],
        "has_backtest_metrics": bundle.get("backtest_metrics") is not None,
        "has_r1_final_payload": r1f.get("payload") is not None,
        "has_r1_mid_payload": r1m.get("payload") is not None,
        "finalized_alerts_count": stats.get("finalized_alerts_count"),
        "finalized_true_positives_count": stats.get("finalized_true_positives_count"),
    }
