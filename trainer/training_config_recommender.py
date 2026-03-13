"""Training config recommender — 依資源與資料來源產出參數建議（PLAN § training-config-recommender）.

獨立工具：偵測可用資源、估計各步驟時間與記憶體、產出建議參數。
支援 Parquet 與 ClickHouse 兩種資料來源。參考 doc/training_oom_and_runtime_audit.md。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Literal, Optional, TypedDict

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]

try:
    import config as _config  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as _config  # type: ignore[import, no-redef]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data profile (shared structure per PLAN)
# ---------------------------------------------------------------------------


class DataProfile(TypedDict, total=False):
    """Unified data profile for estimation and suggestions (Parquet or ClickHouse)."""
    data_source: Literal["parquet", "clickhouse"]
    training_days: int
    chunk_count: int
    total_chunk_bytes_estimate: int
    session_data_bytes: int
    has_existing_chunks: bool
    rows_per_chunk_estimate: int
    bets_rows_per_day: int
    sessions_rows_per_day: int


# ---------------------------------------------------------------------------
# Resource detection
# ---------------------------------------------------------------------------


def get_system_resources(
    disk_path: Optional[Path] = None,
    *,
    step7_temp_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Detect available system resources for training.

    Returns dict with: ram_total_gb, ram_available_gb, cpu_count,
    disk_available_gb (for disk_path or step7_temp_dir). Optional override
    from env TRAINING_AVAILABLE_RAM_GB (container / override).
    """
    out: dict[str, Any] = {}

    # RAM: optional env override (e.g. container)
    env_ram = os.getenv("TRAINING_AVAILABLE_RAM_GB")
    if env_ram is not None:
        try:
            out["ram_available_gb"] = float(env_ram)
            out["ram_total_gb"] = out["ram_available_gb"]  # assume same when overridden
        except ValueError:
            logger.warning("TRAINING_AVAILABLE_RAM_GB=%r invalid; using psutil", env_ram)
            env_ram = None
    if env_ram is None and psutil is not None:
        v = psutil.virtual_memory()
        out["ram_total_gb"] = v.total / (1024 ** 3)
        out["ram_available_gb"] = v.available / (1024 ** 3)
    elif env_ram is None:
        out["ram_total_gb"] = 8.0
        out["ram_available_gb"] = 6.0
        logger.warning("psutil not available; assuming 8 GB total / 6 GB available")

    # CPU
    out["cpu_count"] = os.cpu_count() or 1
    if psutil is not None:
        out["cpu_count"] = psutil.cpu_count() or out["cpu_count"]

    # Disk: for Step 7 spill and chunk writes
    path_for_disk = None
    if disk_path is not None and disk_path.exists():
        path_for_disk = disk_path
    if path_for_disk is None and step7_temp_dir:
        path_for_disk = Path(step7_temp_dir)
    if path_for_disk is None:
        path_for_disk = Path.cwd()
    try:
        usage = shutil.disk_usage(path_for_disk)
        out["disk_available_gb"] = usage.free / (1024 ** 3)
    except OSError as e:
        logger.warning("disk_usage(%s) failed: %s; assuming 50 GB", path_for_disk, e)
        out["disk_available_gb"] = 50.0

    return out


# ---------------------------------------------------------------------------
# Parquet data profile
# ---------------------------------------------------------------------------

def build_data_profile_parquet(
    chunk_dir: Path,
    training_days: int,
    session_parquet_path: Optional[Path] = None,
    *,
    bytes_per_chunk_fallback: Optional[int] = None,
) -> DataProfile:
    """Build data profile from local Parquet layout (chunk dir + optional session file).

    Discovery: scan chunk_dir for *.parquet -> chunk_count, total_chunk_bytes_estimate.
    session_parquet_path -> file size for session_data_bytes.
    Fallback when no chunks: chunk_count from training_days (~1 month per chunk),
    total_chunk_bytes_estimate = chunk_count * bytes_per_chunk_fallback (or config default).
    """
    profile: DataProfile = {
        "data_source": "parquet",
        "training_days": training_days,
        "chunk_count": 0,
        "total_chunk_bytes_estimate": 0,
        "session_data_bytes": 0,
        "has_existing_chunks": False,
    }

    # Session file size (Step 3/4) and chunk discovery — catch OSError (e.g. permission) and keep fallback
    try:
        if session_parquet_path is not None and session_parquet_path.is_file():
            profile["session_data_bytes"] = session_parquet_path.stat().st_size

        if chunk_dir.is_dir():
            parquets = list(chunk_dir.glob("*.parquet"))
            if parquets:
                profile["has_existing_chunks"] = True
                profile["chunk_count"] = len(parquets)
                profile["total_chunk_bytes_estimate"] = sum(p.stat().st_size for p in parquets)
    except OSError as e:
        logger.warning("Parquet discovery failed (e.g. permission): %s; using fallback.", e)

    # Fallback when no chunks
    if profile["chunk_count"] == 0:
        # ~1 chunk per month
        profile["chunk_count"] = max(1, (training_days + 29) // 30)
        default_bytes = bytes_per_chunk_fallback
        if default_bytes is None:
            default_bytes = getattr(
                _config, "NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT", 200 * 1024 * 1024
            )
        profile["total_chunk_bytes_estimate"] = profile["chunk_count"] * default_bytes

    return profile


# ---------------------------------------------------------------------------
# ClickHouse data profile (Phase 2: connect + query)
# ---------------------------------------------------------------------------

def build_data_profile_clickhouse(
    training_days: int,
    *,
    get_client: Optional[Any] = None,
    source_db: Optional[str] = None,
    estimated_bytes_per_chunk: Optional[int] = None,
    estimated_rows_per_day: Optional[int] = None,
    skip_ch_connect: bool = False,
) -> DataProfile:
    """Build data profile from ClickHouse: connect and query system.parts or COUNT(*).

    When skip_ch_connect=True (e.g. --no-ch-query), skip connection and use estimated_*
    or defaults. On connection/query failure, falls back to estimated_* or conservative
    defaults and sets profile so report can note "CH estimate not obtained".
    """
    profile: DataProfile = {
        "data_source": "clickhouse",
        "training_days": training_days,
        "chunk_count": max(1, (training_days + 29) // 30),
        "total_chunk_bytes_estimate": 0,
        "session_data_bytes": 0,
        "has_existing_chunks": False,
    }

    source_db = source_db or getattr(_config, "SOURCE_DB", "GDP_GMWDS_Raw")
    default_bytes = getattr(_config, "NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT", 200 * 1024 * 1024)

    if not skip_ch_connect and get_client is None:
        try:
            from .db_conn import get_clickhouse_client  # type: ignore[import]
            get_client = get_clickhouse_client
        except RuntimeError:
            raise  # do not silently swallow config/dependency errors from db_conn
        except (ImportError, ModuleNotFoundError, AttributeError):
            get_client = None  # lazy import may fail when not run as trainer package

    if not skip_ch_connect and get_client is not None:
        try:
            client = get_client()
            # system.parts: total bytes and rows for t_bet, t_session in SOURCE_DB
            q = """
            SELECT
                table,
                sum(bytes_on_disk) AS bytes,
                sum(rows) AS rows
            FROM system.parts
            WHERE database = %(db)s AND table IN ('t_bet', 't_session') AND active
            GROUP BY table
            """
            rows = client.query(q, parameters={"db": source_db})
            result_set = getattr(rows, "result_set", None) or getattr(rows, "result_rows", None)
            if result_set:
                total_bytes = 0
                total_rows_bet = 0
                total_rows_session = 0
                for row in result_set:
                    name = row[0] if len(row) > 0 else None
                    b = row[1] if len(row) > 1 else 0
                    r = row[2] if len(row) > 2 else 0
                    total_bytes += int(b) if b else 0
                    if name == "t_bet":
                        total_rows_bet = int(r) if r else 0
                    elif name == "t_session":
                        total_rows_session = int(r) if r else 0
                if total_bytes > 0:
                    # Clamp frac to [0, 1] so negative or zero training_days do not yield negative estimate
                    frac = (
                        max(0.0, min(1.0, training_days / 365.0))
                        if training_days and training_days > 0
                        else 0.0
                    )
                    profile["total_chunk_bytes_estimate"] = int(total_bytes * frac)
                    profile["has_existing_chunks"] = True
                    if total_rows_bet:
                        profile["bets_rows_per_day"] = max(1, total_rows_bet // 365)
                    if total_rows_session:
                        profile["sessions_rows_per_day"] = max(1, total_rows_session // 365)
        except Exception as e:
            logger.warning("ClickHouse query for data profile failed: %s", e)

    if profile["total_chunk_bytes_estimate"] == 0:
        profile["total_chunk_bytes_estimate"] = (
            (estimated_bytes_per_chunk or default_bytes) * profile["chunk_count"]
        )
    if profile["session_data_bytes"] == 0 and estimated_rows_per_day:
        # Very rough: assume ~200 bytes/row for session
        profile["session_data_bytes"] = estimated_rows_per_day * 365 * 200

    return profile


# ---------------------------------------------------------------------------
# Per-step estimate and suggest (Phase 3 stubs; expand in same file)
# ---------------------------------------------------------------------------

def estimate_per_step(
    profile: DataProfile,
    resources: dict[str, Any],
) -> dict[str, Any]:
    """Estimate peak RAM and time per step (3/4/6/7/8/9). Formulas from audit doc."""
    estimates: dict[str, Any] = {}
    avail_gb = resources.get("ram_available_gb", 8.0)
    total_chunk = profile.get("total_chunk_bytes_estimate", 0)
    chunk_count = max(1, profile.get("chunk_count", 1))
    session_bytes = profile.get("session_data_bytes", 0)

    # Step 3: session materialize (A02)
    estimates["step3_peak_ram_gb"] = min(avail_gb, session_bytes / (1024 ** 3) * 1.2)
    estimates["step3_time_min"] = max(0.1, session_bytes / (1024 ** 2) / 500.0)  # rough MB/s

    # Step 4: profile backfill (A05/A06)
    estimates["step4_peak_ram_gb"] = min(avail_gb, session_bytes / (1024 ** 3) * 1.1)
    estimates["step4_time_min"] = estimates["step3_time_min"] * 2.0  # per snapshot

    # Step 6: per chunk (A08)
    per_chunk_bytes = total_chunk // chunk_count
    estimates["step6_per_chunk_ram_gb"] = min(avail_gb, per_chunk_bytes / (1024 ** 3) * 2.0)
    estimates["step6_per_chunk_time_min"] = max(0.5, per_chunk_bytes / (1024 ** 3) * 2.0)
    estimates["step6_total_time_min"] = estimates["step6_per_chunk_time_min"] * chunk_count

    # Step 7 (A19/A20): concat + split
    factor = getattr(_config, "CHUNK_CONCAT_RAM_FACTOR", 15.0)
    train_frac = getattr(_config, "TRAIN_SPLIT_FRAC", 0.7)
    step7_peak = (total_chunk / (1024 ** 3)) * factor * (1 + train_frac)
    estimates["step7_peak_ram_gb"] = min(avail_gb * 1.5, step7_peak)
    estimates["step7_time_min"] = max(1.0, total_chunk / (1024 ** 3) * 0.5)

    # Step 8/9 (A23–A27): screening + Optuna
    estimates["step8_peak_ram_gb"] = min(avail_gb, 4.0)
    estimates["step8_time_min"] = 5.0
    estimates["step9_peak_ram_gb"] = min(avail_gb, 6.0)
    estimates["step9_time_min"] = 15.0

    return estimates


def suggest_config(
    profile: DataProfile,
    resources: dict[str, Any],
    estimates: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return list of (parameter, reason) suggestions (OOM → time → data volume)."""
    suggestions: list[tuple[str, str]] = []
    avail_gb = resources.get("ram_available_gb", 8.0)
    step7_peak = estimates.get("step7_peak_ram_gb", 0)
    safety = getattr(_config, "NEG_SAMPLE_RAM_SAFETY", 0.75)
    budget = avail_gb * safety

    if step7_peak > budget and step7_peak > 0:
        suggestions.append((
            "NEG_SAMPLE_FRAC / --days",
            f"Step 7 est. peak {step7_peak:.1f} GB > budget {budget:.1f} GB; lower NEG_SAMPLE_FRAC or --days",
        ))
    suggestions.append(("STEP7_USE_DUCKDB=True", "Avoid pandas fallback (A19)."))
    suggestions.append(("STEP8_SCREEN_SAMPLE_ROWS=2000000", "Cap screening memory (A23)."))
    suggestions.append(("SCREEN_FEATURES_METHOD=lgbm", "Faster than MI (A24)."))
    suggestions.append(("TRAINER_USE_LOOKBACK=False", "Phase 1 default; enable after Phase 2 vectorization (A12)."))

    session_bytes = profile.get("session_data_bytes", 0)
    if session_bytes > (avail_gb * 0.4) * (1024 ** 3) and profile.get("data_source") == "parquet":
        suggestions.append(("--no-preload or PROFILE_PRELOAD_MAX_BYTES", "Session file large vs RAM (A05)."))

    if profile.get("data_source") == "clickhouse":
        suggestions.append(("CH query+network time", "Total time includes ClickHouse query and transfer."))

    return suggestions
