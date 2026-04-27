from __future__ import annotations

from typing import Optional

"""Internal training-memory / OOM config shard.

This file groups the knobs most directly tied to training-path memory pressure.
The stable public surface remains ``trainer.core.config`` / ``trainer.config``.

Exposure classes in this shard:
- user policy knobs: settings a user may intentionally tune for data volume
- pipeline mode defaults: file-based / on-disk paths that should normally stay fixed
- internal guards: heuristics and RAM safety constants
"""

# --- Step 7 on-disk footprint estimate (internal guards) ---
CHUNK_CONCAT_MEMORY_WARN_BYTES = int(1 * (1024**3))  # 1 GB on-disk total
CHUNK_CONCAT_RAM_FACTOR = 15  # on-disk size × this × (1 + TRAIN_SPLIT_FRAC) ≈ Step 7 peak RAM
# Pandas fallback is reserved for tiny test/dev-sized chunk sets only.
STEP7_PANDAS_FALLBACK_MAX_BYTES = 256 * 1024 * 1024

# --- Negative sampling / OOM pre-check ---
# User policy knob: keep all positives, optionally reduce negatives.
NEG_SAMPLE_FRAC: float = 0.20

# Internal guards for auto-reduction logic.
NEG_SAMPLE_FRAC_AUTO: bool = False
NEG_SAMPLE_FRAC_MIN: float = 0.05
NEG_SAMPLE_FRAC_ASSUMED_POS_RATE: float = 0.15
NEG_SAMPLE_RAM_SAFETY: float = 0.75
NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT: int = 200 * 1024 * 1024

# --- Row-level split contract ---
TRAIN_SPLIT_FRAC = 0.70
VALID_SPLIT_FRAC = 0.15
MIN_VALID_TEST_ROWS = 50

# --- Profile ETL memory path ---
PROFILE_USE_DUCKDB: bool = True
PROFILE_PRELOAD_MAX_BYTES: int = int(1.5 * 1024**3)

# --- Step 7/8/9 pipeline mode defaults ---
STEP7_USE_DUCKDB: bool = True
STEP7_KEEP_TRAIN_ON_DISK: bool = True
STEP9_EXPORT_LIBSVM: bool = True
STEP9_TRAIN_FROM_FILE: bool = True
STEP9_COMPARE_ALL_GBMS: bool = True
STEP9_SAVE_LGB_BINARY: bool = True

# --- Step 8 / Step 9 memory-sensitive knobs ---
# Keep this as a plain assignment for now; no getenv override contract yet.
TRAIN_METRICS_PREDICT_BATCH_ROWS: int = 500_000
# None = no extra sampling cap; if set, the integer must be > 0.
STEP8_SCREEN_SAMPLE_ROWS: Optional[int] = None
# Which rows of the train split feed Step 8 screening sample: head | tail | head_tail.
STEP8_SCREEN_SAMPLE_STRATEGY: str = "head"

# --- Canonical mapping fallback path ---
CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS: bool = False

