"""Offline L0 preprocessing facade (``t_session`` + ``t_bet``).

Implementation lives in :mod:`trainer_hightier.utils.session_l0_preprocess` and
:mod:`trainer_hightier.utils.bet_l0_preprocess` so session vs bet caches stay independent.
"""

from __future__ import annotations

from trainer_hightier.utils.bet_l0_preprocess import (
    bet_clean_cache_is_hit,
    bet_clean_cache_manifest_path,
    build_bet_clean_cache_record,
    bulk_bet_episode_calendar_tags,
    default_cleaned_bet_parquet_path,
    default_preprocess_registry_yaml_path,
    preprocess_bets_from_parquet_streaming,
    write_bet_clean_cache_manifest,
)
from trainer_hightier.utils.session_l0_preprocess import (
    SESSION_PREPROCESS_READ_COLS_ORDERED,
    apply_session_dq_keep_manual,
    apply_session_l0_registry_cleanup,
    build_session_clean_cache_record,
    default_cleaned_session_parquet_path,
    load_sessions_via_duckdb_local_parquet_contract,
    load_bets_sessions_from_parquet_skeleton,
    preprocess_sessions_from_parquet,
    preprocess_sessions_from_parquet_streaming,
    session_clean_cache_is_hit,
    session_clean_cache_manifest_path,
    write_cleaned_session_parquet,
    write_session_clean_cache_manifest,
)

__all__ = [
    "SESSION_PREPROCESS_READ_COLS_ORDERED",
    "apply_session_dq_keep_manual",
    "apply_session_l0_registry_cleanup",
    "bet_clean_cache_is_hit",
    "bet_clean_cache_manifest_path",
    "build_bet_clean_cache_record",
    "build_session_clean_cache_record",
    "bulk_bet_episode_calendar_tags",
    "default_cleaned_bet_parquet_path",
    "default_cleaned_session_parquet_path",
    "default_preprocess_registry_yaml_path",
    "preprocess_sessions_from_parquet_streaming",
    "preprocess_sessions_from_parquet",
    "preprocess_bets_from_parquet_streaming",
    "load_sessions_via_duckdb_local_parquet_contract",
    "session_clean_cache_is_hit",
    "session_clean_cache_manifest_path",
    "write_session_clean_cache_manifest",
    "write_bet_clean_cache_manifest",
    "write_cleaned_session_parquet",
    "load_bets_sessions_from_parquet_skeleton",
]
