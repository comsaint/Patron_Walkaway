"""W1 field-test objective precondition helpers (trainer-side).

Reads ``field_test_objective_precondition_check.json`` produced by
``trainer.scripts.build_field_test_objective_precondition`` and surfaces a
compact overlay into ``training_metrics.json`` without loading large blobs.

Environment variable (optional):
    FIELD_TEST_OBJECTIVE_PRECONDITION_JSON — absolute or relative path to JSON.
"""

from __future__ import annotations

import glob as glob_module
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("trainer")

FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV = "FIELD_TEST_OBJECTIVE_PRECONDITION_JSON"

# Guardrail: precondition JSON is expected to be a small summary (R2 / laptop-friendly).
MAX_PRECONDITION_JSON_BYTES = 2 * 1024 * 1024

DEFAULT_MAX_FIELD_TEST_FOLD_METRICS_FILES = 32


def expand_repo_relative_json_globs(
    repo_root: Path,
    glob_exprs: Sequence[str],
    *,
    max_files: int = DEFAULT_MAX_FIELD_TEST_FOLD_METRICS_FILES,
) -> tuple[list[Path], dict[str, Any]]:
    """Expand repo-relative glob patterns to unique ``*.json`` paths under *repo_root*.

    Patterns are resolved from the repository root. Only files whose resolved path
    stays under *repo_root* are kept. Intended for orchestrator auto-discovery of
    fold-level metrics JSON without hand-maintaining long path lists.
    """
    root = repo_root.resolve()
    found: list[Path] = []
    seen: set[str] = set()
    meta: dict[str, Any] = {
        "glob_exprs_used": [str(x) for x in glob_exprs],
        "hits_per_glob": [],
        "matched_unique_count": 0,
        "truncated": False,
    }
    for raw in glob_exprs:
        pat = str(raw).strip().replace("\\", "/")
        if not pat:
            continue
        full_pattern = pat if Path(pat).is_absolute() else str((root / pat).as_posix())
        hits = sorted(glob_module.glob(full_pattern, recursive=True))
        meta["hits_per_glob"].append({"pattern": pat, "count": len(hits)})
        for p_str in hits:
            pp = Path(p_str).resolve()
            if not pp.is_file():
                continue
            if pp.suffix.lower() != ".json":
                continue
            try:
                rel = pp.relative_to(root)
            except ValueError:
                continue
            key = rel.as_posix()
            if key in seen:
                continue
            seen.add(key)
            found.append(pp)
    meta["matched_unique_count"] = len(found)
    if len(found) > max_files:
        meta["truncated"] = True
        meta["truncated_to"] = max_files
        found = found[: int(max_files)]
    return found, meta


def try_load_precondition_json(path: Path) -> Optional[dict[str, Any]]:
    """Load precondition JSON; return ``None`` if missing, unreadable, or not an object."""
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("Field-test precondition JSON stat failed (%s): %s", path, exc)
        return None
    if size > MAX_PRECONDITION_JSON_BYTES:
        logger.warning(
            "Field-test precondition JSON too large (%d bytes > %d): %s",
            size,
            MAX_PRECONDITION_JSON_BYTES,
            path,
        )
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Field-test precondition JSON unreadable (%s): %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Field-test precondition JSON must be an object: %s", path)
        return None
    return data


def precondition_constrained_optuna_allowed(
    doc: Optional[Mapping[str, Any]],
) -> bool:
    """Whether W1 precondition permits a future field-test *single constrained* Optuna path (W2).

    Returns ``True`` when there is no document. Mirrors the gate used for
    ``field_test_constrained_optuna_objective_allowed`` in
    :func:`training_metrics_overlay_from_precondition` without emitting log lines
    (callers may already have logged load warnings).
    """
    if doc is None:
        return True
    raw_block = doc.get("blocking_reasons")
    if "blocking_reasons" in doc and not isinstance(raw_block, list):
        return False
    blocking: list[Any] = raw_block if isinstance(raw_block, list) else []
    if blocking:
        return False
    sa = doc.get("single_objective_allowed")
    if isinstance(sa, bool):
        return sa
    return True


def training_metrics_overlay_from_precondition(
    doc: Mapping[str, Any],
    *,
    source_path: str,
    max_blocking_list: int = 12,
) -> dict[str, Any]:
    """Return flat keys to merge into rated ``training_metrics`` (keep JSON small)."""
    blocking_reasons_schema_ok = True
    raw_block = doc.get("blocking_reasons")
    if "blocking_reasons" in doc and not isinstance(raw_block, list):
        blocking_reasons_schema_ok = False
        logger.warning(
            "Field-test precondition JSON has non-list blocking_reasons (%s); "
            "treating as gate-invalid for constrained objective.",
            type(raw_block).__name__,
        )
        blocking: list[Any] = []
    elif isinstance(raw_block, list):
        blocking = raw_block
    else:
        blocking = []

    decision = doc.get("objective_decision")
    if not isinstance(decision, str):
        decision = "unknown"
    single_allowed = doc.get("single_objective_allowed")
    if not isinstance(single_allowed, bool):
        single_allowed = len(blocking) == 0 and blocking_reasons_schema_ok
    if not blocking_reasons_schema_ok:
        single_allowed = False

    head = blocking[: int(max_blocking_list)]
    return {
        "field_test_objective_precondition_json": source_path,
        "field_test_objective_decision": decision,
        "field_test_single_objective_allowed": single_allowed,
        "field_test_precondition_blocking_reason_count": len(blocking),
        "field_test_precondition_blocking_reasons_head": "; ".join(str(x) for x in head),
        "field_test_precondition_blocking_reasons_schema_ok": blocking_reasons_schema_ok,
        # Explicit contract for W2: constrained field-test objective must not run when False.
        "field_test_constrained_optuna_objective_allowed": single_allowed,
    }


def log_precondition_block_warning(doc: Mapping[str, Any]) -> None:
    """Emit a single WARNING when ``blocking_reasons`` is non-empty."""
    blocking = doc.get("blocking_reasons")
    if not isinstance(blocking, list) or not blocking:
        return
    logger.warning(
        "Field-test objective precondition: %d blocking reason(s); "
        "single constrained objective is not allowed for this run.",
        len(blocking),
    )


def log_optuna_precondition_context(doc: Optional[Mapping[str, Any]]) -> None:
    """Log once before Optuna when a precondition doc is loaded (AP objective unchanged)."""
    if doc is None:
        return
    blocking = doc.get("blocking_reasons")
    if not isinstance(blocking, list):
        blocking = []
    if blocking:
        logger.info(
            "Optuna HPO: still optimising validation AP (baseline). "
            "Precondition blocks single constrained field-test objective (%d reason(s)).",
            len(blocking),
        )
    else:
        logger.info(
            "Optuna HPO: precondition reports no blockers; "
            "single constrained field-test objective remains subject to W2 wiring."
        )


def _sanitize_run_id_segment(run_id: str) -> str:
    s = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (run_id or "").strip())
    return (s[:200] if s else "unknown_run")


def build_field_test_precondition_for_orchestration(
    repo_root: Path,
    *,
    run_id: str,
    start_ts: str,
    end_ts: str,
    fold_metrics_abs_paths: Sequence[Path],
    production_neg_pos_ratio: float,
    selection_mode: str = "field_test",
) -> dict[str, Any]:
    """Materialize precondition JSON/MD under ``out/precision_uplift_field_test_objective/``.

    Intended for Phase 2 orchestration before ``trainer.trainer`` subprocesses.
    """
    if not fold_metrics_abs_paths:
        return {"applied": False, "reason": "no_fold_metrics_paths"}
    missing = [str(p) for p in fold_metrics_abs_paths if not p.is_file()]
    if missing:
        return {"applied": False, "reason": "missing_fold_metrics_files", "missing": missing}

    try:
        from trainer.scripts.build_field_test_objective_precondition import (
            run as _run_precondition_build,
        )
    except ImportError as exc:
        logger.warning("Cannot import precondition build script: %s", exc)
        return {"applied": False, "reason": "import_error", "message": str(exc)}

    root = repo_root.resolve()
    out_dir = root / "out" / "precision_uplift_field_test_objective"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = _sanitize_run_id_segment(run_id)
    out_json = out_dir / f"phase2_{safe}_field_test_objective_precondition_check.json"
    out_md = out_dir / f"phase2_{safe}_field_test_objective_precondition_check.md"

    argv: list[str] = []
    for p in fold_metrics_abs_paths:
        argv.extend(["--fold-metrics-json", str(Path(p).resolve())])
    argv.extend(
        [
            "--run-id",
            str(run_id).strip(),
            "--start-ts",
            str(start_ts).strip(),
            "--end-ts",
            str(end_ts).strip(),
            "--production-neg-pos-ratio",
            str(float(production_neg_pos_ratio)),
            "--selection-mode",
            str(selection_mode or "field_test").strip(),
            "--output-json",
            str(out_json),
            "--output-md",
            str(out_md),
        ]
    )
    rc = int(_run_precondition_build(argv))
    if rc != 0:
        return {"applied": False, "reason": "build_script_failed", "returncode": rc}
    return {
        "applied": True,
        "output_json": str(out_json.resolve()),
        "output_md": str(out_md.resolve()),
        "returncode": rc,
        "fold_metrics_paths": [str(Path(p).resolve()) for p in fold_metrics_abs_paths],
    }


def trainer_env_updates_from_precondition_manifest(
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    """Map manifest from :func:`build_field_test_precondition_for_orchestration` to env."""
    if manifest.get("applied") and manifest.get("output_json"):
        return {
            FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV: str(
                Path(str(manifest["output_json"])).resolve()
            )
        }
    return {}
