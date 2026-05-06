"""Unit tests for ``mapping_identity_health_from_meta`` (L0/L1/L2 audit fields)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from parallel_lda_mvp import eligible_builder


@pytest.fixture
def fp() -> str:
    return "a" * 64


def test_mapping_identity_health_unknown_meta(fp: str) -> None:
    h = eligible_builder.mapping_identity_health_from_meta(None, fingerprint=fp)
    assert h["mapping_snapshot_id"] == fp
    assert h["identity_mode"] == "unknown"
    assert h["degrade_level"] == 0
    assert h["mapping_age_min"] is None


def test_mapping_identity_health_fresh(fp: str) -> None:
    frozen = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    built = "2026-05-01T11:50:00Z"
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = frozen
    with (
        patch.object(eligible_builder, "MAPPING_MAX_STALENESS_MIN", 30),
        patch.object(eligible_builder, "MAPPING_HARD_STALE_LIMIT_MIN", 240),
        patch("parallel_lda_mvp.eligible_builder.datetime", mock_dt),
    ):
        h = eligible_builder.mapping_identity_health_from_meta(
            {"built_at": built}, fingerprint=fp
        )
    assert h["identity_mode"] == "fresh"
    assert h["degrade_level"] == 0
    assert h["mapping_age_min"] == pytest.approx(10.0, rel=0, abs=0.01)


def test_mapping_identity_health_stale_l1(fp: str) -> None:
    frozen = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    built = "2026-05-01T11:00:00Z"
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = frozen
    with (
        patch.object(eligible_builder, "MAPPING_MAX_STALENESS_MIN", 30),
        patch.object(eligible_builder, "MAPPING_HARD_STALE_LIMIT_MIN", 240),
        patch("parallel_lda_mvp.eligible_builder.datetime", mock_dt),
    ):
        h = eligible_builder.mapping_identity_health_from_meta(
            {"built_at": built}, fingerprint=fp
        )
    assert h["identity_mode"] == "stale_snapshot"
    assert h["degrade_level"] == 1
    assert h["l2_conservative_degrade"] == "not_implemented"


def test_mapping_identity_health_hard_l2(fp: str) -> None:
    frozen = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    built = "2026-05-01T06:00:00Z"
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = frozen
    with (
        patch.object(eligible_builder, "MAPPING_MAX_STALENESS_MIN", 30),
        patch.object(eligible_builder, "MAPPING_HARD_STALE_LIMIT_MIN", 240),
        patch("parallel_lda_mvp.eligible_builder.datetime", mock_dt),
    ):
        h = eligible_builder.mapping_identity_health_from_meta(
            {"built_at": built}, fingerprint=fp
        )
    assert h["identity_mode"] == "stale_snapshot"
    assert h["degrade_level"] == 2
    assert h["hard_stale_limit_min"] == 240


def test_hard_limit_clamped_below_max_staleness(fp: str) -> None:
    """Misconfigured HARD < MAX: effective hard is ``max(HARD, MAX)`` (never below L1 ceiling)."""
    frozen = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    built = "2026-05-01T10:00:00Z"
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = frozen
    with (
        patch.object(eligible_builder, "MAPPING_MAX_STALENESS_MIN", 60),
        patch.object(eligible_builder, "MAPPING_HARD_STALE_LIMIT_MIN", 30),
        patch("parallel_lda_mvp.eligible_builder.datetime", mock_dt),
    ):
        h = eligible_builder.mapping_identity_health_from_meta(
            {"built_at": built}, fingerprint=fp
        )
    assert h["hard_stale_limit_min"] == 60
    assert h["degrade_level"] == 2
