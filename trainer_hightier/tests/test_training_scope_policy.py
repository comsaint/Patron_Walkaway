"""Tests for training acceleration scope / sampling policy helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import (
    DATA_COMPLETENESS_MODE_STRICT,
    DATA_COMPLETENESS_MODE_WARN,
    DuckDbRuntimeConfig,
    FeatureScreeningPolicy,
    SamplePolicy,
    TrainingScopePolicy,
    TRAINING_RUN_KIND_RELEASE,
    TRAINING_RUN_KIND_SPEED,
    feature_selection_policy_fingerprint,
    resolve_training_scope,
    sample_policy_fingerprint,
    training_scope_policy_fingerprint,
    validate_sample_policy_for_run,
)
from trainer_hightier.trainer import (
    HighTierTrainArgs,
    _apply_training_scope_horizon_to_parquet,
    _audit_training_scope_completeness,
    _init_training_acceleration_metrics,
)


def test_resolve_target_months_example_from_ssot() -> None:
    """SSOT example: as_of 2026-06-08, recent_full_months=3."""
    policy = TrainingScopePolicy(
        recent_full_months=3,
        include_current_partial_month=True,
        as_of_date=date(2026, 6, 8),
    )
    resolved = resolve_training_scope(policy)
    assert resolved.horizon_enabled is True
    assert resolved.target_months == ("202603", "202604", "202605", "202606")
    assert resolved.partial_target_months == frozenset({"202606"})
    assert resolved.target_start_date == date(2026, 3, 1)
    assert resolved.target_end_date == date(2026, 6, 8)


def test_horizon_disabled_when_recent_full_months_none() -> None:
    """Legacy path keeps all rows until explicit horizon is configured."""
    resolved = resolve_training_scope(TrainingScopePolicy(recent_full_months=None))
    assert resolved.horizon_enabled is False
    assert resolved.target_months == ()


def test_policy_fingerprints_are_stable() -> None:
    """Fingerprints must be deterministic for identical policy inputs."""
    policy = TrainingScopePolicy(recent_full_months=3, as_of_date=date(2026, 6, 8))
    resolved = resolve_training_scope(policy)
    fp_a = training_scope_policy_fingerprint(resolved)
    fp_b = training_scope_policy_fingerprint(resolve_training_scope(policy))
    assert fp_a == fp_b
    assert len(fp_a) == 64

    sample = SamplePolicy(neg_sample_frac=0.3, neg_sample_seed=7)
    assert sample_policy_fingerprint(sample) == sample_policy_fingerprint(sample)
    screening = FeatureScreeningPolicy(enabled=False)
    assert feature_selection_policy_fingerprint(screening) == feature_selection_policy_fingerprint(screening)


def test_release_run_rejects_downsampling() -> None:
    """Release / promoted runs must keep neg_sample_frac=1.0."""
    with pytest.raises(ValueError, match="release_promoted"):
        validate_sample_policy_for_run(
            SamplePolicy(neg_sample_frac=0.3),
            run_kind=TRAINING_RUN_KIND_RELEASE,
        )


def test_init_training_acceleration_metrics_seeds_report_block() -> None:
    """Run start should populate acceleration policy artifact skeleton."""
    args = HighTierTrainArgs(
        output_dir=Path("/tmp/out"),
        training_scope_policy=TrainingScopePolicy(
            recent_full_months=3,
            as_of_date=date(2026, 6, 8),
        ),
        sample_policy=SamplePolicy(neg_sample_frac=1.0),
        training_run_kind=TRAINING_RUN_KIND_SPEED,
    )
    metrics: dict[str, object] = {}
    _init_training_acceleration_metrics(args, metrics)
    block = metrics["training_acceleration_policy"]
    assert isinstance(block, dict)
    assert block["training_scope_policy_fingerprint"]
    assert block["sample_policy_fingerprint"]
    assert block["feature_selection_policy_fingerprint"]
    assert block["step35_indexed_replay_gate_summary"] is None


def test_horizon_filter_keeps_only_target_month_rows(tmp_path: Path) -> None:
    """Horizon filter should drop rows outside selected target months."""
    path = tmp_path / "training.parquet"
    table = pa.table(
        {
            "gaming_day_event": [
                date(2026, 3, 1),
                date(2026, 5, 1),
                date(2026, 6, 8),
                date(2025, 12, 31),
            ],
            "walkaway_censored": [False, False, False, False],
        },
    )
    pq.write_table(table, path)
    resolved = resolve_training_scope(
        TrainingScopePolicy(
            recent_full_months=1,
            include_current_partial_month=True,
            as_of_date=date(2026, 6, 8),
        ),
    )
    out, summary = _apply_training_scope_horizon_to_parquet(
        path,
        resolved=resolved,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    assert summary["horizon_filter_applied"] is True
    assert summary["rows_before"] == 4
    assert summary["rows_after"] == 2
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT CAST(gaming_day_event AS DATE) FROM read_parquet('{out.as_posix()}') ORDER BY 1",
        ).fetchall()
    finally:
        con.close()
    assert [r[0] for r in rows] == [date(2026, 5, 1), date(2026, 6, 8)]


def test_completeness_strict_fails_on_empty_full_month(tmp_path: Path) -> None:
    """Strict mode should fail when a full target month has zero rows."""
    path = tmp_path / "training.parquet"
    pq.write_table(
        pa.table(
            {
                "gaming_day_event": [date(2026, 6, 8)],
                "walkaway_censored": [False],
            },
        ),
        path,
    )
    resolved = resolve_training_scope(
        TrainingScopePolicy(
            recent_full_months=2,
            include_current_partial_month=True,
            as_of_date=date(2026, 6, 8),
            data_completeness_mode=DATA_COMPLETENESS_MODE_STRICT,
        ),
    )
    with pytest.raises(ValueError, match="empty full target month"):
        _audit_training_scope_completeness(
            path,
            resolved=resolved,
            duckdb_runtime=DuckDbRuntimeConfig(),
        )


def test_completeness_warn_allows_empty_full_month(tmp_path: Path) -> None:
    """Warn mode should continue when a full target month is empty."""
    path = tmp_path / "training.parquet"
    pq.write_table(
        pa.table(
            {
                "gaming_day_event": [date(2026, 6, 8)],
                "walkaway_censored": [False],
            },
        ),
        path,
    )
    resolved = resolve_training_scope(
        TrainingScopePolicy(
            recent_full_months=2,
            include_current_partial_month=True,
            as_of_date=date(2026, 6, 8),
            data_completeness_mode=DATA_COMPLETENESS_MODE_WARN,
        ),
    )
    report = _audit_training_scope_completeness(
        path,
        resolved=resolved,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    assert report["empty_full_target_months"]
    assert "completeness_warnings" in report
