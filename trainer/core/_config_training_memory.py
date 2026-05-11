"""Internal training-memory / OOM config shard.

This file groups the knobs most directly tied to training-path memory pressure.
The stable public surface remains ``trainer.core.config`` / ``trainer.config``.

Exposure classes in this shard:
- user policy knobs: settings a user may intentionally tune for data volume
- pipeline mode defaults: file-based / on-disk paths that should normally stay fixed
- internal guards: heuristics and RAM safety constants
"""

from __future__ import annotations

import os
from typing import Literal, Optional

# --- Step 6 / run_state_machine: compute engine (Phase 1 pandas vs Phase 2 DuckDB) ---
# SSOT: switch here (avoid env-var toggles for training semantics).
# Rollout gate (suggested):
# - On a fixed training window, ``run_state_machine_compute`` wall time improves vs pandas baseline.
# - Peak RSS unchanged or lower on the same machine.
# - Spot-check with RUN_BOUNDARY_PARITY_CHECK=True while len(bets) <= RUN_BOUNDARY_PARITY_MAX_ROWS.
# Then set RUN_BOUNDARY_ENGINE = "duckdb".
# Default DuckDB: lower peak RAM / wall time on large windows (pandas fallback remains in feature_pipeline).
RUN_BOUNDARY_ENGINE: Literal["pandas", "duckdb"] = "duckdb"
# When True and engine is duckdb, diff against pandas if len(bets) <= RUN_BOUNDARY_PARITY_MAX_ROWS.
RUN_BOUNDARY_PARITY_CHECK: bool = False
RUN_BOUNDARY_PARITY_MAX_ROWS: int = 500_000

# --- Step 6 diagnostics (process_chunk hot path) ---
STEP6_PROFILE_ENABLED: bool = True
# Log one compact health line after label window filter (rows / positives) for A/B parity checks.
STEP6_CHUNK_HEALTH_LOG: bool = True
# Manual Step 6 phase gates: compare the same training window after each change —
# Step6 profile phases (load_raw, normalize_dq, identity_admission, run_state_machine, …),
# ``chunk health`` log line (rows / label_pos / rss_mb), and final chunk Parquet row count.

def _truthy_env(name: str, default: str) -> bool:
    """Return True for common truthy env strings (1/true/t/yes/y, case-insensitive)."""
    raw = (os.getenv(name) or default).strip().lower()
    return raw in ("1", "true", "t", "yes", "y")

# --- Step 7 on-disk footprint estimate (internal guards) ---
CHUNK_CONCAT_MEMORY_WARN_BYTES = int(1 * (1024**3))  # 1 GB on-disk total
CHUNK_CONCAT_RAM_FACTOR = 15  # on-disk size × this × (1 + TRAIN_SPLIT_FRAC) ≈ Step 7 peak RAM
# Pandas fallback is reserved for tiny test/dev-sized chunk sets only.
STEP7_PANDAS_FALLBACK_MAX_BYTES = 256 * 1024 * 1024

# --- Negative sampling / OOM pre-check ---
# User policy knob: keep all positives, optionally reduce negatives.
NEG_SAMPLE_FRAC: float = 0.3

# Internal guards for auto-reduction logic.
# Default off: chunk-path RAM heuristics / auto-neg-frac live under GitHub #10 (#16 defers OOM).
# Set True locally if you still want pre-Step-6 OOM pre-check + chunk-1 probe + auto frac.
NEG_SAMPLE_FRAC_AUTO: bool = False
NEG_SAMPLE_FRAC_MIN: float = 0.05
NEG_SAMPLE_FRAC_ASSUMED_POS_RATE: float = 0.15
NEG_SAMPLE_RAM_SAFETY: float = 0.75
NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT: int = 200 * 1024 * 1024

# --- Row-level split contract ---
# Test share is implicit: 1 - TRAIN_SPLIT_FRAC - VALID_SPLIT_FRAC (0.15 with 0.65/0.20).
# For large N, 15% temporal holdout is a common default; adequacy of *row counts* for
# metrics is warned in trainer Step 7 when valid or test falls below MIN_VALID_TEST_ROWS.
TRAIN_SPLIT_FRAC = 0.70
VALID_SPLIT_FRAC = 0.15
MIN_VALID_TEST_ROWS = 50

# --- parallel_lda_mvp → trainer bridge (DuckDB in-memory engine cap) ---
# Gigabytes for DuckDB ``PRAGMA memory_limit`` in
# ``parallel_lda_mvp.trainer_bridge_mvp.emit_trainer_local_parquet`` (emitted as ``"{n}GB"``).
PARALLEL_LDA_BRIDGE_DUCKDB_MEMORY_LIMIT: int = 8

# --- Profile ETL memory path ---
PROFILE_USE_DUCKDB: bool = True
PROFILE_PRELOAD_MAX_BYTES: int = int(1.5 * 1024**3)

# --- Step 7/8/9 pipeline mode defaults ---
STEP7_USE_DUCKDB: bool = True
STEP7_KEEP_TRAIN_ON_DISK: bool = True
STEP9_EXPORT_LIBSVM: bool = True
# Retired: Plan B CSV training removed; pipeline is LibSVM-only. Kept as False for import/facade tests.
STEP9_TRAIN_FROM_FILE: bool = False
STEP9_COMPARE_ALL_GBMS: bool = True
STEP9_SAVE_LGB_BINARY: bool = True

# --- Step 8 / Step 9 memory-sensitive knobs ---
# Keep this as a plain assignment for now; no getenv override contract yet.
TRAIN_METRICS_PREDICT_BATCH_ROWS: int = 500_000
# A3 Phase E dense predict: emit a progress log every N batch iterations (0 disables).
A3_PHASE_E_PREDICT_HEARTBEAT_EVERY_N_BATCHES: int = 10
# When True, Phase E dense predict logs RSS / available RAM at key points (investigation).
A3_PHASE_E_DIAG_MEMORY_SNAPSHOT: bool = False

# --- A3 optional backends (CatBoost / XGBoost): LibSVM-disk final fit (OOM mitigation) ---
# When True and Plan B+ LibSVM paths exist, final full-data fits use on-disk data instead
# of dense in-memory frames. Set GBM_BAKEOFF_FROM_FILE=0 to force legacy in-memory path.
GBM_BAKEOFF_FROM_FILE: bool = _truthy_env("GBM_BAKEOFF_FROM_FILE", "1")
# XGBoost external-memory URI (hist only); off by default on mixed Windows paths / GPU.
GBM_BAKEOFF_XGBOOST_EXTERNAL_MEMORY: bool = _truthy_env("GBM_BAKEOFF_XGBOOST_EXTERNAL_MEMORY", "0")
# CatBoost quantize() before fit (large-data path); optional RAM trade-off.
GBM_BAKEOFF_CATBOOST_QUANTIZE: bool = _truthy_env("GBM_BAKEOFF_CATBOOST_QUANTIZE", "0")
# None = no extra sampling cap; if set, the integer must be > 0.
STEP8_SCREEN_SAMPLE_ROWS: Optional[int] = None
# Which rows of the train split feed Step 8 screening sample: head | tail | head_tail.
STEP8_SCREEN_SAMPLE_STRATEGY: str = "head"

# --- Canonical mapping fallback path ---
CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS: bool = False

