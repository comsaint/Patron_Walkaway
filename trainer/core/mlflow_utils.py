"""
Phase 2 P0–P1: Shared MLflow helpers for trainer and export script.

- Reads MLFLOW_TRACKING_URI from environment.
- When URI is unset or unreachable: logs warning only, does not raise; all log helpers no-op.
- Trainer must not fail when GCP/MLflow is unavailable (training still succeeds).
- T11: On import, if local_state/mlflow.env exists (or MLFLOW_ENV_FILE points to a file),
  loads those vars into os.environ with override=False so process/shell env takes precedence.
- GCP Cloud Run: when GOOGLE_APPLICATION_CREDENTIALS is set and tracking URI is HTTPS,
  fetches a GCP ID token and registers a RequestHeaderProvider so MLflow requests include
  Authorization: Bearer <token> for Cloud Run auth.

See doc/phase2_provenance_schema.md for provenance key names.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

_log = logging.getLogger(__name__)

# Cache for GCP ID token: (token_str, expiry_ts). Refresh when now >= expiry_ts - 300.
_gcp_id_token_cache: Optional[tuple[str, float]] = None
_gcp_provider_registered = False

# T11: Load project-local MLflow env file so train/export get config without main .env.
# MLFLOW_ENV_FILE (optional): override path for tests; else repo_root/local_state/mlflow.env.
# Code Review §1: wrap in try/except so import never fails (zip/frozen or load_dotenv error).
# Code Review §2: empty or whitespace MLFLOW_ENV_FILE → treat as unset, use default path.
try:
    _repo_root = Path(__file__).resolve().parent.parent.parent
    _env_file_override = (os.environ.get("MLFLOW_ENV_FILE") or "").strip() or None
    _mlflow_env_path = Path(_env_file_override) if _env_file_override else (_repo_root / "local_state" / "mlflow.env")
    if _mlflow_env_path.is_file():
        load_dotenv(str(_mlflow_env_path), override=False)
except Exception as e:
    _log.warning("T11: could not load local_state/mlflow.env: %s", e)

# Cached after first check to avoid repeated connection attempts on hot path.
_mlflow_available: Optional[bool] = None

# ID token cache: (token, expiry_ts). Tokens typically valid 1h; refresh 5 min before.
_GCP_TOKEN_REFRESH_BUFFER_SEC = 300


def _get_gcp_id_token(audience: str) -> Optional[str]:
    """Fetch GCP ID token for the given audience (e.g. Cloud Run URL). Uses GOOGLE_APPLICATION_CREDENTIALS. Cached until ~5 min before expiry."""
    global _gcp_id_token_cache
    now = time.time()
    if _gcp_id_token_cache is not None:
        _token, expiry = _gcp_id_token_cache
        if now < expiry - _GCP_TOKEN_REFRESH_BUFFER_SEC:
            return _token
    try:
        import google.auth.transport.requests  # type: ignore[import-untyped]
        import google.oauth2.id_token  # type: ignore[import-untyped]
        request = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(request, audience)
        if token:
            # ID tokens typically expire in 3600s; use 3500 to be safe.
            _gcp_id_token_cache = (token, now + 3500)
            return token
    except Exception as e:
        _log.warning("Failed to fetch GCP ID token for MLflow: %s", e)
    return None


def _register_gcp_bearer_provider_if_needed() -> None:
    """Register a RequestHeaderProvider that adds Authorization: Bearer <id_token> when URI is HTTPS and GOOGLE_APPLICATION_CREDENTIALS is set."""
    global _gcp_provider_registered
    if _gcp_provider_registered:
        return
    uri = get_tracking_uri()
    creds = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not uri or not creds or not uri.lower().startswith("https://"):
        return
    try:
        from mlflow.tracking.request_header.abstract_request_header_provider import RequestHeaderProvider
        from mlflow.tracking.request_header.registry import _request_header_provider_registry  # type: ignore[attr-defined]

        class _GCPBearerRequestHeaderProvider(RequestHeaderProvider):
            def in_context(self) -> bool:
                u = get_tracking_uri()
                c = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
                return bool(u and c and u.lower().startswith("https://"))

            def request_headers(self) -> dict[str, str]:
                u = get_tracking_uri()
                if not u:
                    return {}
                token = _get_gcp_id_token(u)
                if not token:
                    return {}
                return {"Authorization": f"Bearer {token}"}

        _request_header_provider_registry.register(_GCPBearerRequestHeaderProvider)
        _gcp_provider_registered = True
    except Exception as e:
        _log.debug("Could not register GCP Bearer provider for MLflow: %s", e)


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

    # Register GCP Bearer provider before any MLflow request when using HTTPS + service account.
    _register_gcp_bearer_provider_if_needed()

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


def has_active_run() -> bool:
    """Return True if MLflow is available and there is an active run; otherwise False. Used to avoid nesting runs (e.g. T12)."""
    if not is_mlflow_available():
        return False
    try:
        import mlflow  # type: ignore[import-not-found]
        return mlflow.active_run() is not None
    except Exception:
        return False


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
