"""
Phase 2 P0–P1: Shared MLflow helpers for trainer and export script.

- Reads MLFLOW_TRACKING_URI from environment.
- When URI is unset or unreachable: logs warning only, does not raise; all log helpers no-op.
- Trainer must not fail when GCP/MLflow is unavailable (training still succeeds).

See doc/phase2_provenance_schema.md for provenance key names.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

# Cached after first check to avoid repeated connection attempts on hot path.
_mlflow_available: Optional[bool] = None


def reset_availability_cache() -> None:
    """Reset the cached availability check. For testing only."""
    global _mlflow_available
    _mlflow_available = None


def get_tracking_uri() -> Optional[str]:
    """Return MLFLOW_TRACKING_URI if set, else None."""
    return os.environ.get("MLFLOW_TRACKING_URI") or None


def is_mlflow_available() -> bool:
    """
    Return True if MLflow tracking is configured and reachable.
    When URI is unset: log warning once and return False.
    When URI is set but unreachable: log warning once and return False.
    Result is cached for the process lifetime.
    """
    global _mlflow_available
    if _mlflow_available is not None:
        return _mlflow_available

    uri = get_tracking_uri()
    if not uri:
        _log.warning(
            "MLFLOW_TRACKING_URI is not set; MLflow logging (provenance, export) will be skipped."
        )
        _mlflow_available = False
        return False

    try:
        import mlflow  # type: ignore[import-not-found]
        mlflow.set_tracking_uri(uri)
        # Minimal reachability check: get experiment list or create default (lightweight).
        _ = mlflow.get_experiment_by_name("Default")
        _mlflow_available = True
        return True
    except Exception as e:
        _log.warning(
            "MLflow tracking URI is set but unreachable (%s); MLflow logging will be skipped.",
            e,
        )
        _mlflow_available = False
        return False


def _noop(*args: Any, **kwargs: Any) -> None:
    pass


def safe_start_run(
    experiment_name: Optional[str] = None,
    run_name: Optional[str] = None,
    tags: Optional[dict[str, str]] = None,
):
    """
    Start an MLflow run if tracking is available; otherwise no-op.
    Returns a context that can be used with 'with' (or no-op context if unavailable).
    """
    if not is_mlflow_available():
        from contextlib import nullcontext
        return nullcontext()
    import mlflow  # type: ignore[import-not-found]
    return mlflow.start_run(experiment_name=experiment_name, run_name=run_name, tags=tags)


def log_params_safe(params: dict[str, Any]) -> None:
    """Log params to current run if MLflow is available; otherwise no-op."""
    if not is_mlflow_available():
        return
    try:
        import mlflow  # type: ignore[import-not-found]
        mlflow.log_params(params)
    except Exception as e:
        _log.warning("MLflow log_params failed: %s", e)


def log_tags_safe(tags: dict[str, str]) -> None:
    """Log tags to current run if MLflow is available; otherwise no-op."""
    if not is_mlflow_available():
        return
    try:
        import mlflow  # type: ignore[import-not-found]
        mlflow.set_tags(tags)
    except Exception as e:
        _log.warning("MLflow set_tags failed: %s", e)


def log_artifact_safe(local_path: str | Path, artifact_path: Optional[str] = None) -> None:
    """Log a file/dir as artifact if MLflow is available; otherwise no-op."""
    if not is_mlflow_available():
        return
    try:
        import mlflow  # type: ignore[import-not-found]
        mlflow.log_artifact(str(local_path), artifact_path=artifact_path)
    except Exception as e:
        _log.warning("MLflow log_artifact failed for %s: %s", local_path, e)


def end_run_safe() -> None:
    """End the current run if active and MLflow is available; otherwise no-op."""
    if not is_mlflow_available():
        return
    try:
        import mlflow  # type: ignore[import-not-found]
        if mlflow.active_run():
            mlflow.end_run()
    except Exception as e:
        _log.warning("MLflow end_run failed: %s", e)
