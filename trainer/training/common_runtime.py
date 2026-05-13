"""Shared paths, data-dir layout, and light time helpers for the training package.

Issue #33 / Phase A: split from ``trainer.training.trainer`` so downstream modules
(backtester, LDA bridges, CLI) do not need to import the full trainer mega-module
for constants and ``_to_hk`` only.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple, cast

import pandas as pd
from zoneinfo import ZoneInfo

try:
    import config as _cfg  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

from trainer.training.data_sources import (  # noqa: E402
    _BET_INGEST_READ_COLS_ORDERED,
    _BET_SELECT_COLS,
    _CANONICAL_MAP_SESSION_COLS,
    _OPTIONAL_BET_LDA_RUN_TRIP_COLS,
    _REQUIRED_BET_PARQUET_COLS,
    _SESSION_SELECT_COLS,
    LOCAL_PARQUET_DIR,
    local_parquet_session_path_for_trainer,
    trainer_local_parquet_bridge_manifest_path,
)

HK_TZ_STR: str = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
TRAINER_DAYS: int = int(getattr(_cfg, "TRAINER_DAYS", 30))
HISTORY_BUFFER_DAYS: int = int(getattr(_cfg, "HISTORY_BUFFER_DAYS", 2))
HK_TZ = ZoneInfo(HK_TZ_STR)

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / ".data"
CHUNK_DIR = DATA_DIR / "chunks"
CANONICAL_MAPPING_PARQUET = LOCAL_PARQUET_DIR / "canonical_mapping.parquet"
CANONICAL_MAPPING_CUTOFF_JSON = LOCAL_PARQUET_DIR / "canonical_mapping.cutoff.json"
FEATURE_CANDIDATES_PATH = BASE_DIR / "feature_spec" / "feature_candidates.yaml"
FEATURE_SPEC_PATH = FEATURE_CANDIDATES_PATH
MODEL_DIR: Path = cast(Path, getattr(_cfg, "DEFAULT_MODEL_DIR", BASE_DIR / "models"))
OUT_DIR = BASE_DIR / "out_trainer"

for _d in (DATA_DIR, CHUNK_DIR, LOCAL_PARQUET_DIR, MODEL_DIR, OUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _to_hk(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=HK_TZ)
    return dt.astimezone(HK_TZ)


def default_training_window(days: int = TRAINER_DAYS) -> Tuple[datetime, datetime]:
    now = datetime.now(HK_TZ)
    return now - timedelta(days=days), now - timedelta(minutes=30)


def parse_window(args) -> Tuple[datetime, datetime]:
    if args.start or args.end:
        if not (args.start and args.end):
            raise ValueError("Provide both --start and --end or neither")
        start = _to_hk(pd.to_datetime(args.start).to_pydatetime())
        end = _to_hk(pd.to_datetime(args.end).to_pydatetime())
        return start, end
    return default_training_window(getattr(args, "days", TRAINER_DAYS))


def get_model_version() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        git_hash = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=BASE_DIR,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        git_hash = "nogit"
    return f"{ts}-{git_hash}"


__all__ = [
    "_BET_INGEST_READ_COLS_ORDERED",
    "_BET_SELECT_COLS",
    "_CANONICAL_MAP_SESSION_COLS",
    "_OPTIONAL_BET_LDA_RUN_TRIP_COLS",
    "_REQUIRED_BET_PARQUET_COLS",
    "_SESSION_SELECT_COLS",
    "BASE_DIR",
    "CANONICAL_MAPPING_CUTOFF_JSON",
    "CANONICAL_MAPPING_PARQUET",
    "CHUNK_DIR",
    "DATA_DIR",
    "FEATURE_CANDIDATES_PATH",
    "FEATURE_SPEC_PATH",
    "HK_TZ",
    "HK_TZ_STR",
    "HISTORY_BUFFER_DAYS",
    "LOCAL_PARQUET_DIR",
    "MODEL_DIR",
    "OUT_DIR",
    "PROJECT_ROOT",
    "TRAINER_DAYS",
    "default_training_window",
    "get_model_version",
    "local_parquet_session_path_for_trainer",
    "parse_window",
    "trainer_local_parquet_bridge_manifest_path",
    "_to_hk",
]
