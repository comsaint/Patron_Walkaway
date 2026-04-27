from __future__ import annotations

import logging
import math
import os
from typing import Optional

"""Internal serving / runtime / status-server config shard."""

_log = logging.getLogger(__name__)

SCORER_ALERT_RETENTION_DAYS = 30
SCORER_STATE_RETENTION_HOURS = 24
VALIDATION_RESULTS_RETENTION_DAYS = 180
VALIDATOR_CACHE_PRUNE_INTERVAL_SECONDS = 300

try:
    _max_lb = int(float(os.getenv("SCORER_LOOKBACK_HOURS_MAX", "8760")))
    SCORER_LOOKBACK_HOURS_MAX: int = _max_lb if _max_lb > 0 else 8760
except (TypeError, ValueError, OverflowError):
    SCORER_LOOKBACK_HOURS_MAX = 8760

_raw_scorer_lb = os.getenv("SCORER_LOOKBACK_HOURS", "8")
try:
    _scorer_lb_parsed = int(float(_raw_scorer_lb))
    SCORER_LOOKBACK_HOURS = _scorer_lb_parsed if _scorer_lb_parsed > 0 else 8
except (TypeError, ValueError, OverflowError):
    _log.warning("SCORER_LOOKBACK_HOURS invalid (%r); using 8", _raw_scorer_lb)
    SCORER_LOOKBACK_HOURS = 8
if SCORER_LOOKBACK_HOURS > SCORER_LOOKBACK_HOURS_MAX:
    _log.warning(
        "SCORER_LOOKBACK_HOURS=%d exceeds SCORER_LOOKBACK_HOURS_MAX=%d; capping",
        SCORER_LOOKBACK_HOURS,
        SCORER_LOOKBACK_HOURS_MAX,
    )
    SCORER_LOOKBACK_HOURS = SCORER_LOOKBACK_HOURS_MAX

SCORER_POLL_INTERVAL_SECONDS = 45

CHUNK_TWO_STAGE_CACHE_DEFAULT: bool = True


def chunk_two_stage_cache_enabled(default: bool = CHUNK_TWO_STAGE_CACHE_DEFAULT) -> bool:
    """Return whether the step-6 two-stage prefeatures cache is enabled."""
    raw = (os.getenv("CHUNK_TWO_STAGE_CACHE") or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    _log.warning(
        "CHUNK_TWO_STAGE_CACHE=%r invalid; using default %s",
        os.getenv("CHUNK_TWO_STAGE_CACHE"),
        default,
    )
    return default


_csw_raw = os.getenv("SCORER_COLD_START_WINDOW_HOURS", "").strip()
SCORER_COLD_START_WINDOW_HOURS: Optional[float]
if not _csw_raw:
    SCORER_COLD_START_WINDOW_HOURS = 2.0
else:
    try:
        _csw_p = float(_csw_raw)
        if not math.isfinite(_csw_p) or _csw_p <= 0:
            _log.warning(
                "SCORER_COLD_START_WINDOW_HOURS invalid (%r); scoring age filter disabled",
                _csw_raw,
            )
            SCORER_COLD_START_WINDOW_HOURS = None
        elif _csw_p > float(SCORER_LOOKBACK_HOURS_MAX):
            _log.warning(
                "SCORER_COLD_START_WINDOW_HOURS=%s exceeds SCORER_LOOKBACK_HOURS_MAX=%d; capping",
                _csw_raw,
                SCORER_LOOKBACK_HOURS_MAX,
            )
            SCORER_COLD_START_WINDOW_HOURS = float(SCORER_LOOKBACK_HOURS_MAX)
        else:
            SCORER_COLD_START_WINDOW_HOURS = _csw_p
    except (TypeError, ValueError, OverflowError):
        _log.warning(
            "SCORER_COLD_START_WINDOW_HOURS invalid (%r); scoring age filter disabled",
            _csw_raw,
        )
        SCORER_COLD_START_WINDOW_HOURS = None

_rtt_age = os.getenv("RUNTIME_THRESHOLD_MAX_AGE_HOURS", "").strip()
RUNTIME_THRESHOLD_MAX_AGE_HOURS: Optional[float]
if not _rtt_age:
    RUNTIME_THRESHOLD_MAX_AGE_HOURS = None
else:
    try:
        _rtt_f = float(_rtt_age)
        RUNTIME_THRESHOLD_MAX_AGE_HOURS = _rtt_f if math.isfinite(_rtt_f) and _rtt_f > 0 else None
    except (TypeError, ValueError):
        RUNTIME_THRESHOLD_MAX_AGE_HOURS = None

DISABLE_PROGRESS_BAR = False

TABLE_STATUS_REFRESH_SECONDS = 45
TABLE_STATUS_LOOKBACK_HOURS = 12
TABLE_STATUS_RETENTION_HOURS = 24
TABLE_STATUS_HC_RETENTION_DAYS = 30

SCORER_ENABLE_SHAP_REASON_CODES = os.getenv(
    "SCORER_ENABLE_SHAP_REASON_CODES", "0"
).strip().lower() in ("1", "true", "t", "yes", "y")

# Feature parity audit (serving): optional writes to PREDICTION_LOG_DB_PATH SQLite.
SCORER_FEATURE_AUDIT_ENABLE = os.getenv(
    "SCORER_FEATURE_AUDIT_ENABLE", "0"
).strip().lower() in ("1", "true", "t", "yes", "y")

_raw_audit_sample = os.getenv("SCORER_FEATURE_AUDIT_SAMPLE_ROWS", "1000")
try:
    _audit_sample_parsed = int(float(_raw_audit_sample))
    SCORER_FEATURE_AUDIT_SAMPLE_ROWS = _audit_sample_parsed if _audit_sample_parsed >= 0 else 1000
except (TypeError, ValueError, OverflowError):
    _log.warning(
        "SCORER_FEATURE_AUDIT_SAMPLE_ROWS invalid (%r); using 1000",
        _raw_audit_sample,
    )
    SCORER_FEATURE_AUDIT_SAMPLE_ROWS = 1000

_raw_audit_every = os.getenv("SCORER_FEATURE_AUDIT_EVERY_N_CYCLES", "1")
try:
    _audit_every_parsed = int(float(_raw_audit_every))
    SCORER_FEATURE_AUDIT_EVERY_N_CYCLES = _audit_every_parsed if _audit_every_parsed > 0 else 1
except (TypeError, ValueError, OverflowError):
    _log.warning(
        "SCORER_FEATURE_AUDIT_EVERY_N_CYCLES invalid (%r); using 1",
        _raw_audit_every,
    )
    SCORER_FEATURE_AUDIT_EVERY_N_CYCLES = 1

_raw_audit_ret = os.getenv("SCORER_FEATURE_AUDIT_RETENTION_HOURS", "24")
try:
    _audit_ret_parsed = float(_raw_audit_ret)
    SCORER_FEATURE_AUDIT_RETENTION_HOURS = (
        _audit_ret_parsed if math.isfinite(_audit_ret_parsed) and _audit_ret_parsed > 0 else 24.0
    )
except (TypeError, ValueError, OverflowError):
    _log.warning(
        "SCORER_FEATURE_AUDIT_RETENTION_HOURS invalid (%r); using 24",
        _raw_audit_ret,
    )
    SCORER_FEATURE_AUDIT_RETENTION_HOURS = 24.0

SCORER_FEATURE_AUDIT_STORE_VALUES = os.getenv(
    "SCORER_FEATURE_AUDIT_STORE_VALUES", "1"
).strip().lower() in ("1", "true", "t", "yes", "y")

