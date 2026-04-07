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
- IAP in front of Cloud Run: set MLFLOW_IAP_AUDIENCE to the IAP OAuth client ID
  (…apps.googleusercontent.com). Using the tracking URI as audience will fail with
  "Invalid JWT audience".

See doc/phase2_provenance_schema.md for provenance key names.
"""

from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

_log = logging.getLogger(__name__)

# Cache for GCP ID token per audience: audience -> (token_str, expiry_ts).
_gcp_id_token_cache: dict[str, tuple[str, float]] = {}
_gcp_provider_registered = False

# T11: Load project-local MLflow env file so train/export get config without main .env.
# MLFLOW_ENV_FILE (optional): override path for tests; else credential/mlflow.env, then local_state/mlflow.env (PLAN Credential folder).
# Code Review §1: wrap in try/except so import never fails (zip/frozen or load_dotenv error).
# Code Review §2: empty or whitespace MLFLOW_ENV_FILE → treat as unset, use default path.
try:
    _repo_root = Path(__file__).resolve().parent.parent.parent
    _env_file_override = (os.environ.get("MLFLOW_ENV_FILE") or "").strip() or None
    if _env_file_override:
        _mlflow_env_path = Path(_env_file_override)
    else:
        _candidate = _repo_root / "credential" / "mlflow.env"
        if not _candidate.is_file():
            _candidate = _repo_root / "local_state" / "mlflow.env"
        _mlflow_env_path = _candidate
    if _mlflow_env_path.is_file():
        load_dotenv(str(_mlflow_env_path), override=False)
except Exception as e:
    _log.warning("T11: could not load mlflow.env (credential/ or local_state/): %s", type(e).__name__)

# Cached after first check to avoid repeated connection attempts on hot path.
_mlflow_available: Optional[bool] = None

# ID token cache: (token, expiry_ts). Tokens typically valid 1h; refresh 5 min before.
_GCP_TOKEN_REFRESH_BUFFER_SEC = 300

# T13: Retry on transient server errors (e.g. Cloud Run cold start 503).
# max_retries=3 → 4 attempts total; delays 30s, 60s, 120s → ~3.5 min max wait.
_MLFLOW_RETRY_MAX_RETRIES = 3
_MLFLOW_RETRY_INITIAL_DELAY_SEC = 30
_MLFLOW_RETRY_BACKOFF_MULTIPLIER = 2


def _is_transient_mlflow_error(exc: BaseException) -> bool:
    """True if the exception indicates a transient server error (502/503/504, cold start)."""
    msg = str(exc).lower()
    return (
        "502" in msg
        or "503" in msg
        or "504" in msg
        or "too many 503" in msg
    )


def _get_gcp_id_token(audience: str) -> Optional[str]:
    """Fetch GCP ID token for the given audience (Cloud Run URL, or IAP OAuth client ID). Uses GOOGLE_APPLICATION_CREDENTIALS. Cached per audience."""
    global _gcp_id_token_cache
    now = time.time()
    cached = _gcp_id_token_cache.get(audience)
    if cached is not None:
        _token, expiry = cached
        if now < expiry - _GCP_TOKEN_REFRESH_BUFFER_SEC:
            return _token
    try:
        import google.auth.transport.requests  # type: ignore[import-untyped]
        import google.oauth2.id_token  # type: ignore[import-untyped]
        request = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(request, audience)
        if token:
            # ID tokens typically expire in 3600s; use 3500 to be safe.
            _gcp_id_token_cache[audience] = (token, now + 3500)
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
                iap = (os.environ.get("MLFLOW_IAP_AUDIENCE") or "").strip()
                audience = iap or u
                token = _get_gcp_id_token(audience)
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
    except Exception as e:
        # Code Review §1: log when active_run() fails so failures are debuggable.
        _log.warning(
            "has_active_run: mlflow.active_run() failed; assuming no active run: %s",
            e,
        )
        return False


def warm_up_mlflow_run_safe() -> None:
    """T13: Lightweight MLflow API call to wake Cloud Run before first log-batch.
    No-op if MLflow unavailable or no active run. Uses same 502/503/504 retry+backoff."""
    if not is_mlflow_available():
        return
    import mlflow  # type: ignore[import-not-found]
    run = mlflow.active_run()
    if run is None:
        return
    # T13 Review #4: avoid get_run(None) when run_id is missing.
    if not getattr(run, "info", None) or getattr(run.info, "run_id", None) is None:
        _log.warning("MLflow warm-up skipped: no run_id")
        return
    delay_sec = float(_MLFLOW_RETRY_INITIAL_DELAY_SEC)
    last_exc: Optional[Exception] = None
    for attempt in range(_MLFLOW_RETRY_MAX_RETRIES + 1):
        try:
            mlflow.get_run(run.info.run_id)
            return
        except Exception as e:
            last_exc = e
            if attempt < _MLFLOW_RETRY_MAX_RETRIES and _is_transient_mlflow_error(e):
                _log.info(
                    "MLflow warm-up transient error (attempt %d/%d), retry in %.0fs: %s",
                    attempt + 1,
                    _MLFLOW_RETRY_MAX_RETRIES + 1,
                    delay_sec,
                    type(e).__name__,
                )
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break
    _log.warning(
        "MLflow warm-up failed after %d attempts (provenance/metrics may still succeed): %s",
        _MLFLOW_RETRY_MAX_RETRIES + 1,
        type(last_exc).__name__ if last_exc is not None else "Unknown",
    )


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
    if experiment_name is not None:
        mlflow.set_experiment(experiment_name)
    return mlflow.start_run(run_name=run_name, tags=tags)


def log_params_safe(params: dict[str, Any]) -> None:
    """Log params to current run if MLflow is available; otherwise no-op.
    T13: On 502/503/504 (e.g. cold start), retries with exponential backoff."""
    if not is_mlflow_available():
        return
    if not params:
        return
    delay_sec = float(_MLFLOW_RETRY_INITIAL_DELAY_SEC)
    last_exc: Optional[Exception] = None
    for attempt in range(_MLFLOW_RETRY_MAX_RETRIES + 1):
        try:
            import mlflow  # type: ignore[import-not-found]
            mlflow.log_params(params)
            return
        except Exception as e:
            last_exc = e
            if attempt < _MLFLOW_RETRY_MAX_RETRIES and _is_transient_mlflow_error(e):
                _log.info(
                    "MLflow log_params transient error (attempt %d/%d), retry in %.0fs: %s",
                    attempt + 1,
                    _MLFLOW_RETRY_MAX_RETRIES + 1,
                    delay_sec,
                    type(e).__name__,
                )
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break
    _log.warning(
        "MLflow log_params failed after %d attempts: %s",
        _MLFLOW_RETRY_MAX_RETRIES + 1,
        type(last_exc).__name__ if last_exc is not None else "Unknown",
    )


def log_tags_safe(tags: dict[str, str]) -> None:
    """Log tags to current run if MLflow is available; otherwise no-op.
    T13: On 502/503/504 (e.g. cold start), retries with exponential backoff."""
    if not is_mlflow_available():
        return
    if not tags:
        return
    delay_sec = float(_MLFLOW_RETRY_INITIAL_DELAY_SEC)
    last_exc: Optional[Exception] = None
    for attempt in range(_MLFLOW_RETRY_MAX_RETRIES + 1):
        try:
            import mlflow  # type: ignore[import-not-found]
            mlflow.set_tags(tags)
            return
        except Exception as e:
            last_exc = e
            if attempt < _MLFLOW_RETRY_MAX_RETRIES and _is_transient_mlflow_error(e):
                _log.info(
                    "MLflow set_tags transient error (attempt %d/%d), retry in %.0fs: %s",
                    attempt + 1,
                    _MLFLOW_RETRY_MAX_RETRIES + 1,
                    delay_sec,
                    type(e).__name__,
                )
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break
    _log.warning(
        "MLflow set_tags failed after %d attempts: %s",
        _MLFLOW_RETRY_MAX_RETRIES + 1,
        type(last_exc).__name__ if last_exc is not None else "Unknown",
    )


def _log_metrics_sanitized_with_step_fallback(
    mlflow_mod: Any, sanitized: dict[str, float], step: Optional[int]
) -> None:
    """Call mlflow.log_metrics; if client rejects ``step=`` (TypeError), retry once without step."""
    if step is None:
        mlflow_mod.log_metrics(sanitized)
        return
    try:
        mlflow_mod.log_metrics(sanitized, step=step)
    except TypeError as e:
        msg = str(e).lower()
        if "unexpected keyword" in msg and "step" in msg:
            _log.warning(
                "MLflow log_metrics rejected step=; logging without step (%s)",
                type(e).__name__,
            )
            mlflow_mod.log_metrics(sanitized)
            return
        raise


def log_metrics_safe(metrics: dict[str, Any], step: Optional[int] = None) -> None:
    """
    Log numeric metrics to current run if MLflow is available; otherwise no-op.

    Contract-style behavior:
    - Skip keys whose value is None.
    - Best-effort coerce values via float(v); on failure skip that key.
    - Optional ``step`` is forwarded to ``mlflow.log_metrics`` so the same keys can form
      a time series in the UI instead of a single overwritten point.
    - If the MLflow client does not support ``step=`` (TypeError), logs once without ``step``.
    - Never raise (log warning only) so training pipeline is not impacted.
    T13: On 502/503/504 (e.g. cold start), retries with exponential backoff.
    """
    if not is_mlflow_available():
        return
    import mlflow  # type: ignore[import-not-found]
    sanitized: dict[str, float] = {}
    for k, v in metrics.items():
        if v is None:
            continue
        try:
            fv = float(v)
            if not math.isfinite(fv):
                continue
            sanitized[k] = fv
        except Exception:
            continue
    if not sanitized:
        return
    delay_sec = float(_MLFLOW_RETRY_INITIAL_DELAY_SEC)
    last_exc: Optional[Exception] = None
    for attempt in range(_MLFLOW_RETRY_MAX_RETRIES + 1):
        try:
            _log_metrics_sanitized_with_step_fallback(mlflow, sanitized, step)
            return
        except Exception as e:
            last_exc = e
            if attempt < _MLFLOW_RETRY_MAX_RETRIES and _is_transient_mlflow_error(e):
                _log.info(
                    "MLflow log_metrics transient error (attempt %d/%d), retry in %.0fs: %s",
                    attempt + 1,
                    _MLFLOW_RETRY_MAX_RETRIES + 1,
                    delay_sec,
                    type(e).__name__,
                )
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break
    _log.warning(
        "MLflow log_metrics failed after %d attempts: %s",
        _MLFLOW_RETRY_MAX_RETRIES + 1,
        type(last_exc).__name__ if last_exc is not None else "Unknown",
    )


def log_artifact_safe(local_path: str | Path, artifact_path: Optional[str] = None) -> None:
    """Log a file/dir as artifact if MLflow is available; otherwise no-op."""
    if not is_mlflow_available():
        return
    try:
        import mlflow  # type: ignore[import-not-found]
        mlflow.log_artifact(str(local_path), artifact_path=artifact_path)
    except Exception as e:
        _log.warning("MLflow log_artifact failed for %s: %s", local_path, e)


def log_artifacts_safe(local_dir: str | Path, artifact_path: Optional[str] = None) -> None:
    """Log a directory tree as MLflow artifacts; no-op when MLflow is unavailable.

    Retries on transient 502/503/504 (same policy as ``log_metrics_safe``).
    Training must not fail when upload fails; warnings only.
    """
    if not is_mlflow_available():
        return
    import mlflow  # type: ignore[import-not-found]

    delay_sec = float(_MLFLOW_RETRY_INITIAL_DELAY_SEC)
    last_exc: Optional[Exception] = None
    for attempt in range(_MLFLOW_RETRY_MAX_RETRIES + 1):
        try:
            mlflow.log_artifacts(str(local_dir), artifact_path=artifact_path)
            return
        except Exception as e:
            last_exc = e
            if attempt < _MLFLOW_RETRY_MAX_RETRIES and _is_transient_mlflow_error(e):
                _log.info(
                    "MLflow log_artifacts transient error (attempt %d/%d), retry in %.0fs: %s",
                    attempt + 1,
                    _MLFLOW_RETRY_MAX_RETRIES + 1,
                    delay_sec,
                    type(e).__name__,
                )
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break
    _log.warning(
        "MLflow log_artifacts failed for %s after %d attempts: %s",
        local_dir,
        _MLFLOW_RETRY_MAX_RETRIES + 1,
        type(last_exc).__name__ if last_exc is not None else "Unknown",
    )


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
