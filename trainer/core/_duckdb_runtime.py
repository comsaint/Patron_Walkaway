from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Optional

from trainer.core._config_duckdb_memory import (
    CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB,
    CANONICAL_MAP_DUCKDB_THREADS,
    DUCKDB_MEMORY_LIMIT_MAX_GB,
    DUCKDB_MEMORY_LIMIT_MIN_GB,
    DUCKDB_PRESERVE_INSERTION_ORDER,
    DUCKDB_RAM_FRACTION,
    DUCKDB_RAM_MAX_FRACTION,
    DUCKDB_THREADS,
    LIBSVM_EXPORT_DUCKDB_MEMORY_LIMIT_MAX_GB,
    LIBSVM_EXPORT_DUCKDB_THREADS,
    PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB,
    SCREENING_DUCKDB_MEMORY_LIMIT_MAX_GB,
    SCREENING_DUCKDB_THREADS,
    STEP7_DUCKDB_TEMP_DIR,
    STEP7_DUCKDB_THREADS,
    TRACK_LLM_DUCKDB_MEMORY_LIMIT_MAX_GB,
    TRACK_LLM_DUCKDB_THREADS,
)
from trainer.core._config_env_paths import _REPO_ROOT

_log = logging.getLogger(__name__)

DuckDBRuntimeStage = Literal[
    "profile",
    "step7",
    "canonical_map",
    "track_llm",
    "screening",
    "libsvm_export",
]

_TRAINER_DATA_DIR = (_REPO_ROOT / "trainer" / ".data").resolve()
_DEFAULT_DUCKDB_TMP = (_TRAINER_DATA_DIR / "duckdb_tmp").resolve()


def _sanitize_temp_directory(temp_dir: Optional[str]) -> str:
    raw = str(temp_dir or _DEFAULT_DUCKDB_TMP)
    if "'" in raw:
        return str(_DEFAULT_DUCKDB_TMP)
    try:
        resolved = Path(raw).resolve()
        if resolved != _DEFAULT_DUCKDB_TMP:
            resolved.relative_to(_TRAINER_DATA_DIR)
    except (OSError, ValueError):
        return str(_DEFAULT_DUCKDB_TMP)
    return raw


def _stage_defaults(stage: DuckDBRuntimeStage) -> tuple[int, float, Optional[str]]:
    threads = DUCKDB_THREADS
    max_gb = DUCKDB_MEMORY_LIMIT_MAX_GB
    temp_dir: Optional[str] = None
    if stage == "profile":
        max_gb = PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB
    elif stage == "step7":
        threads = STEP7_DUCKDB_THREADS
        temp_dir = STEP7_DUCKDB_TEMP_DIR
    elif stage == "canonical_map":
        threads = CANONICAL_MAP_DUCKDB_THREADS
        max_gb = CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB
    elif stage == "track_llm":
        threads = TRACK_LLM_DUCKDB_THREADS
        max_gb = TRACK_LLM_DUCKDB_MEMORY_LIMIT_MAX_GB
    elif stage == "screening":
        threads = SCREENING_DUCKDB_THREADS
        max_gb = SCREENING_DUCKDB_MEMORY_LIMIT_MAX_GB
    elif stage == "libsvm_export":
        threads = LIBSVM_EXPORT_DUCKDB_THREADS
        max_gb = LIBSVM_EXPORT_DUCKDB_MEMORY_LIMIT_MAX_GB
    return (max(1, int(threads)), float(max_gb), temp_dir)


def resolve_duckdb_runtime_policy(
    stage: DuckDBRuntimeStage,
    available_bytes: Optional[int],
    input_bytes: Optional[int] = None,
) -> dict[str, Any]:
    if stage not in (
        "profile",
        "step7",
        "canonical_map",
        "track_llm",
        "screening",
        "libsvm_export",
    ):
        raise ValueError(f"Unsupported DuckDB runtime stage: {stage!r}")

    frac = DUCKDB_RAM_FRACTION if (0.0 < DUCKDB_RAM_FRACTION <= 1.0) else 0.5
    min_gb = DUCKDB_MEMORY_LIMIT_MIN_GB
    max_gb = DUCKDB_MEMORY_LIMIT_MAX_GB
    ram_max_frac = DUCKDB_RAM_MAX_FRACTION
    threads, stage_max_gb, stage_temp_dir = _stage_defaults(stage)
    max_gb = min(max_gb, stage_max_gb)
    min_bytes = max(1, int(max(0.1, float(min_gb)) * 1024**3))
    max_bytes = max(min_bytes, int(max(0.1, float(max_gb)) * 1024**3))
    cap_1tb = 1024 * 1024**3
    max_bytes = min(max_bytes, cap_1tb)
    if available_bytes is None or available_bytes <= 0:
        budget_bytes = min_bytes
    else:
        budget_bytes = int(available_bytes * frac)
        effective_max = max_bytes
        if ram_max_frac is not None and 0.0 < ram_max_frac <= 1.0:
            effective_max = min(max(effective_max, int(available_bytes * ram_max_frac)), cap_1tb)
        budget_bytes = max(min_bytes, min(effective_max, budget_bytes))
        if stage in ("step7", "screening") and input_bytes is not None and input_bytes > 0:
            if input_bytes > available_bytes * 0.35:
                threads = min(threads, 1)
            elif input_bytes > available_bytes * 0.20:
                threads = min(threads, 2)
    temp_directory = _sanitize_temp_directory(stage_temp_dir)
    Path(temp_directory).mkdir(parents=True, exist_ok=True)
    return {
        "stage": stage,
        "memory_limit_bytes": int(budget_bytes),
        "threads": max(1, int(threads)),
        "temp_directory": temp_directory,
        "preserve_insertion_order": bool(DUCKDB_PRESERVE_INSERTION_ORDER),
    }


def apply_duckdb_runtime(con: Any, policy: dict[str, Any]) -> None:
    budget_gb = float(policy["memory_limit_bytes"]) / 1024**3
    threads = max(1, int(policy["threads"]))
    temp_directory = str(policy["temp_directory"])
    temp_dir_sql = temp_directory.replace("'", "''")
    for stmt, label in (
        (f"SET memory_limit='{budget_gb:.2f}GB'", "memory_limit"),
        (f"SET threads={threads}", "threads"),
        (f"SET temp_directory='{temp_dir_sql}'", "temp_directory"),
    ):
        try:
            con.execute(stmt)
        except Exception as exc:
            _log.warning("DuckDB runtime SET %s failed (non-fatal): %s", label, exc)
    if not bool(policy.get("preserve_insertion_order", True)):
        try:
            con.execute("SET preserve_insertion_order=false")
        except Exception as exc:
            _log.warning("DuckDB runtime SET preserve_insertion_order failed (non-fatal): %s", exc)
