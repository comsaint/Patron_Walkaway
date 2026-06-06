"""Tests for L0 preprocess ``gaming_day_event`` scope config and SQL helper."""

from __future__ import annotations

from datetime import date

from trainer_hightier.config import (
    DEFAULT_L0_PREPROCESS_GAMING_DAY_EVENT_MIN,
    DEFAULT_TRAINING_GAMING_DAY_EVENT_MIN,
    BetPreprocessConfig,
    L0PreprocessDataScopeConfig,
    SessionPreprocessConfig,
    TrainingDataScopeConfig,
)
from trainer_hightier.utils.hk_time_semantics import duckdb_gaming_day_event_scope_and_sql


def test_default_l0_preprocess_scope_starts_2025() -> None:
    """Production defaults exclude pre-2025 L0 shards."""
    scope = SessionPreprocessConfig().data_scope
    assert scope.gaming_day_event_min == DEFAULT_L0_PREPROCESS_GAMING_DAY_EVENT_MIN
    assert scope.gaming_day_event_min == date(2025, 1, 1)
    assert scope.gaming_day_event_max is None
    assert BetPreprocessConfig().data_scope == scope


def test_default_training_data_scope_start_date() -> None:
    """Training scope default keeps the full L0-preprocessed population."""
    scope = TrainingDataScopeConfig()
    assert scope.gaming_day_event_min == DEFAULT_TRAINING_GAMING_DAY_EVENT_MIN
    assert scope.gaming_day_event_min is None
    assert scope.gaming_day_event_max is None


def test_duckdb_gaming_day_event_scope_and_sql_min_only() -> None:
    """Scope helper emits inclusive lower-bound predicate."""
    sql = duckdb_gaming_day_event_scope_and_sql(min_day=date(2025, 1, 1), max_day=None)
    assert sql.startswith(" AND ")
    assert ">= DATE '2025-01-01'" in sql


def test_duckdb_gaming_day_event_scope_and_sql_empty_when_unbounded() -> None:
    """No bounds → empty suffix (no filter)."""
    assert duckdb_gaming_day_event_scope_and_sql(min_day=None, max_day=None) == ""


def test_l0_preprocess_data_scope_manifest_block() -> None:
    """Manifest fragment is JSON-friendly ISO dates."""
    block = L0PreprocessDataScopeConfig(
        gaming_day_event_min=date(2025, 1, 1),
        gaming_day_event_max=date(2026, 6, 3),
    ).manifest_block()
    assert block == {
        "gaming_day_event_min": "2025-01-01",
        "gaming_day_event_max": "2026-06-03",
    }
