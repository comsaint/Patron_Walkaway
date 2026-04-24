from __future__ import annotations

from typing import Optional

"""Internal DuckDB memory / runtime config shard.

This file is an internal implementation detail. The stable public config
surface remains ``trainer.core.config`` (and the compatibility facade
``trainer.config``), which re-export these names.

Exposure classes in this shard:
- pipeline mode defaults: per-stage thread / temp-dir choices
- internal guards: shared DuckDB RAM budget constants and compatibility aliases
"""

# --- DuckDB shared memory budget (internal guards) ---
DUCKDB_RAM_FRACTION: float = 0.5
DUCKDB_MEMORY_LIMIT_MIN_GB: float = 1.0
DUCKDB_MEMORY_LIMIT_MAX_GB: float = 24.0
DUCKDB_RAM_MAX_FRACTION: Optional[float] = 0.45
DUCKDB_THREADS: int = 2
DUCKDB_PRESERVE_INSERTION_ORDER: bool = False

# --- Profile ETL override (pipeline mode defaults) ---
PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB: float = 8.0

# Backward-compat aliases (DEC-027): tests / ETL may still read these names.
PROFILE_DUCKDB_RAM_FRACTION: float = DUCKDB_RAM_FRACTION
PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB: float = DUCKDB_MEMORY_LIMIT_MIN_GB
PROFILE_DUCKDB_RAM_MAX_FRACTION: Optional[float] = DUCKDB_RAM_MAX_FRACTION
PROFILE_DUCKDB_THREADS: int = DUCKDB_THREADS
PROFILE_DUCKDB_PRESERVE_INSERTION_ORDER: bool = DUCKDB_PRESERVE_INSERTION_ORDER

# --- Step 7 DuckDB runtime (pipeline mode defaults) ---
STEP7_DUCKDB_THREADS: int = 4
STEP7_DUCKDB_TEMP_DIR: Optional[str] = None

# Backward-compat aliases (DEC-027): tests may still expect these on config.
STEP7_DUCKDB_RAM_FRACTION: float = DUCKDB_RAM_FRACTION
STEP7_DUCKDB_RAM_MIN_GB: float = DUCKDB_MEMORY_LIMIT_MIN_GB
STEP7_DUCKDB_RAM_MAX_GB: float = DUCKDB_MEMORY_LIMIT_MAX_GB
STEP7_DUCKDB_PRESERVE_INSERTION_ORDER: bool = DUCKDB_PRESERVE_INSERTION_ORDER

# --- Canonical mapping DuckDB runtime (pipeline mode defaults) ---
CANONICAL_MAP_DUCKDB_THREADS: int = 1

# --- Track LLM DuckDB runtime (pipeline mode defaults) ---
TRACK_LLM_DUCKDB_THREADS: int = 2
TRACK_LLM_DUCKDB_MEMORY_LIMIT_MAX_GB: float = 8.0

# --- Step 8 screening DuckDB runtime (pipeline mode defaults) ---
SCREENING_DUCKDB_THREADS: int = 1
SCREENING_DUCKDB_MEMORY_LIMIT_MAX_GB: float = 4.0

# --- LibSVM export DuckDB runtime (pipeline mode defaults) ---
LIBSVM_EXPORT_DUCKDB_THREADS: int = 1
LIBSVM_EXPORT_DUCKDB_MEMORY_LIMIT_MAX_GB: float = 8.0

# Backward-compat aliases (DEC-027): tests may still expect these on config.
CANONICAL_MAP_DUCKDB_RAM_FRACTION: float = DUCKDB_RAM_FRACTION
CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MIN_GB: float = DUCKDB_MEMORY_LIMIT_MIN_GB
CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB: float = DUCKDB_MEMORY_LIMIT_MAX_GB

