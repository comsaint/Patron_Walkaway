"""
MLflow helpers (vendored from legacy ``trainer.core.mlflow_utils``).

``_repo_root`` points two levels above this file (repository root containing
``trainer_hightier/``).
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

_gcp_id_token_cache: dict[str, tuple[str, float]] = {}
_gcp_provider_registered = False

try:
    _repo_root = Path(__file__).resolve().parents[2]
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

_mlflow_available: Optional[bool] = None
_GCP_TOKEN_REFRESH_BUFFER_SEC = 300
_MLFLOW_RETRY_MAX_RETRIES = 3
_MLFLOW_RETRY_INITIAL_DELAY_SEC = 30
_MLFLOW_RETRY_BACKOFF_MULTIPLIER = 2


def _is_transient_mlflow_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like transient HTTP errors."""

    msg = str(exc).lower()
    return "502" in msg or "503" in msg or "504" in msg or "too many 503" in msg


def _get_gcp_id_token(audience: str) -> Optional[str]:
    """Fetch a GCP ID token (cached); used for MLflow Bearer auth."""

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
            _gcp_id_token_cache[audience] = (token, now + 3500)
            return token
    except Exception as e:
        _log.warning("Failed to fetch GCP ID token for MLflow: %s", e)
    return None


def _register_gcp_bearer_provider_if_needed() -> None:
    """Register Bearer header provider when HTTPS tracking + service account."""

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
    """Reset cached MLflow URI reachability."""

    global _mlflow_available
    _mlflow_available = None


def get_tracking_uri() -> Optional[str]:
    """Return MLFLOW_TRACKING_URI if set."""

    return os.environ.get("MLFLOW_TRACKING_URI") or None


def is_mlflow_available() -> bool:
    """Return True only when MLflow tracking URI is reachable (cached per process)."""

    global _mlflow_available
    if _mlflow_available is not None:
        return _mlflow_available

    uri = get_tracking_uri()
    if not uri:
        _log.warning("MLFLOW_TRACKING_URI is not set; MLflow logging will be skipped.")
        _mlflow_available = False
        return False

    _register_gcp_bearer_provider_if_needed()

    try:
        import mlflow  # type: ignore[import-not-found]

        mlflow.set_tracking_uri(uri)
        _ = mlflow.get_experiment_by_name("Default")
        _mlflow_available = True
        return True
    except Exception as e:
        _log.warning("MLflow tracking URI is set but unreachable (%s); skipping.", e)
        _mlflow_available = False
        return False


def has_active_run() -> bool:
    """Return True when an MLflow active run exists."""

    if not is_mlflow_available():
        return False
    try:
        import mlflow  # type: ignore[import-not-found]

        return mlflow.active_run() is not None
    except Exception as e:
        _log.warning(
            "has_active_run: mlflow.active_run() failed; assuming no active run: %s",
            e,
        )
        return False


def warm_up_mlflow_run_safe() -> None:
    """Prime MLflow by fetching the active ``run_id`` (Cloud Run throttle mitigation)."""

    if not is_mlflow_available():
        return
    import mlflow  # type: ignore[import-not-found]

    run = mlflow.active_run()
    if run is None:
        return
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
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break
    _log.warning(
        "MLflow warm-up failed after %d attempts: %s",
        _MLFLOW_RETRY_MAX_RETRIES + 1,
        type(last_exc).__name__ if last_exc is not None else "Unknown",
    )


def safe_start_run(
    experiment_name: Optional[str] = None,
    run_name: Optional[str] = None,
    tags: Optional[dict[str, str]] = None,
):
    """Context manager wrapping ``mlflow.start_run`` when tracking is reachable."""

    if not is_mlflow_available():
        from contextlib import nullcontext

        return nullcontext()
    import mlflow  # type: ignore[import-not-found]

    if experiment_name is not None:
        mlflow.set_experiment(experiment_name)
    return mlflow.start_run(run_name=run_name, tags=tags)


def log_params_safe(params: dict[str, Any]) -> None:
    """Log params with transient retry semantics."""

    if not is_mlflow_available() or not params:
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
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break
    _log.warning(
        "MLflow log_params failed after retries: %s",
        type(last_exc).__name__ if last_exc is not None else "Unknown",
    )


def log_tags_safe(tags: dict[str, str]) -> None:
    """Set tags with transient retries."""

    if not is_mlflow_available() or not tags:
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
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break


def _log_metrics_sanitized_with_step_fallback(
    mlflow_mod: Any,
    sanitized: dict[str, float],
    step: Optional[int],
) -> None:
    """Call ``log_metrics``, retrying once without ``step`` on TypeError."""

    if step is None:
        mlflow_mod.log_metrics(sanitized)
        return
    try:
        mlflow_mod.log_metrics(sanitized, step=step)
    except TypeError as e:
        msg = str(e).lower()
        if "unexpected keyword" in msg and "step" in msg:
            _log.warning("MLflow rejected step parameter; retrying without.")
            mlflow_mod.log_metrics(sanitized)
            return
        raise


def log_metrics_safe(metrics: dict[str, Any], step: Optional[int] = None) -> None:
    """Log sanitized numeric metrics."""

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
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break


def log_artifact_safe(local_path: str | Path, artifact_path: Optional[str] = None) -> None:
    """Log artifact without raising."""

    if not is_mlflow_available():
        return
    try:
        import mlflow  # type: ignore[import-not-found]

        mlflow.log_artifact(str(local_path), artifact_path=artifact_path)
    except Exception as e:
        _log.warning("MLflow log_artifact failed for %s: %s", local_path, e)


def log_artifacts_safe(local_dir: str | Path, artifact_path: Optional[str] = None) -> None:
    """Log directory tree."""

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
                time.sleep(delay_sec)
                delay_sec *= _MLFLOW_RETRY_BACKOFF_MULTIPLIER
            else:
                break


def end_run_safe() -> None:
    """End MLflow active run."""

    if not is_mlflow_available():
        return
    try:
        import mlflow  # type: ignore[import-not-found]

        if mlflow.active_run():
            mlflow.end_run()
    except Exception as e:
        _log.warning("MLflow end_run failed: %s", e)
