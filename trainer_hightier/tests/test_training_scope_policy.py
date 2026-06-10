"""Tests for training acceleration scope / sampling policy helpers."""

from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_resolve_training_scope_rejects_non_positive_recent_full_months() -> None:
    """Horizon policy must reject zero or negative month counts."""
    with pytest.raises(ValueError, match="recent_full_months must be positive"):
        resolve_training_scope(TrainingScopePolicy(recent_full_months=0))
    with pytest.raises(ValueError, match="recent_full_months must be positive"):
        resolve_training_scope(TrainingScopePolicy(recent_full_months=-2))


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


_bt3 = importlib.import_module("trainer_hightier.03_build_training_data")


def test_filter_month_starts_for_target_scope_prunes_to_target_months() -> None:
    """Step 3 must iterate only resolved target months, not all prediction-visible months."""
    all_months = [date(2025, month, 1) for month in range(1, 13)] + [
        date(2026, month, 1) for month in range(1, 7)
    ]
    resolved = resolve_training_scope(
        TrainingScopePolicy(
            recent_full_months=3,
            include_current_partial_month=True,
            as_of_date=date(2026, 6, 8),
        ),
    )
    filtered = _bt3._filter_month_starts_for_target_scope(all_months, resolved)
    assert [_bt3._month_yyyymm(month_start) for month_start in filtered] == [
        "202603",
        "202604",
        "202605",
        "202606",
    ]


def test_filter_month_starts_legacy_returns_all_months() -> None:
    """When horizon is disabled, Step 3 keeps the full prediction-visible month list."""
    months = [date(2025, 1, 1), date(2025, 2, 1)]
    resolved = resolve_training_scope(TrainingScopePolicy(recent_full_months=None))
    assert _bt3._filter_month_starts_for_target_scope(months, resolved) == months


def test_entity_month_end_exclusive_caps_partial_target_month() -> None:
    """Partial target month entity rows must stop at ``target_end_date``, not calendar month end."""
    resolved = resolve_training_scope(
        TrainingScopePolicy(
            recent_full_months=3,
            include_current_partial_month=True,
            as_of_date=date(2026, 6, 8),
        ),
    )
    june_start = date(2026, 6, 1)
    assert _bt3._entity_month_end_exclusive(june_start, resolved=resolved) == date(2026, 6, 9)
    assert _bt3._entity_month_end_exclusive(date(2026, 5, 1), resolved=resolved) == date(2026, 6, 1)


def test_build_training_data_month_batch_prunes_before_entity_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: month-batch assembly must not call entity writes for non-target months."""
    all_months = [date(2025, month, 1) for month in range(1, 13)] + [
        date(2026, month, 1) for month in range(1, 7)
    ]
    resolved = resolve_training_scope(
        TrainingScopePolicy(
            recent_full_months=3,
            include_current_partial_month=True,
            as_of_date=date(2026, 6, 8),
        ),
    )
    entity_calls: list[tuple[date | None, date | None]] = []

    def _spy_write_entity(
        _cleaned_bet: Path,
        _entity_out: Path,
        *,
        duckdb_runtime: DuckDbRuntimeConfig,
        max_rows: int | None,
        month_start: date | None = None,
        month_end_exclusive: date | None = None,
    ) -> int:
        entity_calls.append((month_start, month_end_exclusive))
        return 0

    monkeypatch.setattr(_bt3, "ensure_feast_registry_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(_bt3, "_validate_prereqs", lambda **kwargs: None)
    monkeypatch.setattr(_bt3, "_maybe_materialize_derived", lambda _cfg: None)
    monkeypatch.setattr(
        _bt3,
        "cleaned_bet_artifact_fingerprint_block",
        lambda _path: {"shard_list_sha256_hex": "cc" * 32},
    )
    monkeypatch.setattr(_bt3, "_prediction_visible_month_starts", lambda *args, **kwargs: list(all_months))
    monkeypatch.setattr(_bt3, "_write_entity_parquet", _spy_write_entity)
    monkeypatch.setattr(
        _bt3,
        "default_slow_patron_180d_monthly_parquet_path",
        lambda **kwargs: tmp_path / "missing_slow.parquet",
    )

    cfg = _bt3.BuildTrainingDataArgs(
        feast_repo=tmp_path / "feast_repo",
        cleaned_bet_parquet=tmp_path / "cleaned_bet",
        labels_parquet=tmp_path / "labels.parquet",
        output_parquet=tmp_path / "training_data" / "training_set.parquet",
        feature_service_name=_bt3.DEFAULT_FEATURE_SERVICE,
        materialize_derived_features=False,
        max_entity_rows=None,
        duckdb_runtime=DuckDbRuntimeConfig(),
        feast_entity_batch_by_calendar_month=True,
        feast_retrieval_cache_enabled=True,
        auto_feast_apply=False,
        target_scope=resolved,
    )
    with pytest.raises(ValueError, match="Feast month batches produced no non-empty slices"):
        _bt3.build_training_data(cfg)

    assert len(entity_calls) == 4
    assert [_bt3._month_yyyymm(month_start) for month_start, _ in entity_calls if month_start is not None] == [
        "202603",
        "202604",
        "202605",
        "202606",
    ]
    june_start, june_end = entity_calls[-1]
    assert june_start == date(2026, 6, 1)
    assert june_end == date(2026, 6, 9)


@patch("trainer_hightier.trainer._prepare_training_features_parquet")
@patch("trainer_hightier.trainer._b3.ensure_feast_registry_ready")
@patch("trainer_hightier.trainer._b3.build_training_data")
@patch(
    "trainer_hightier.trainer._hbet.cleaned_bet_dataset_has_any_parquet",
    return_value=True,
)
def test_maybe_build_training_dataset_passes_target_scope(
    _mock_cb: MagicMock,
    mock_build: MagicMock,
    mock_ensure: MagicMock,
    _mock_prepare: MagicMock,
    tmp_path: Path,
) -> None:
    """Trainer Step 3 must wire ``ResolvedTrainingScope`` into ``BuildTrainingDataArgs``."""
    from trainer_hightier.trainer import HighTierTrainArgs, _maybe_build_training_dataset

    labels = tmp_path / "walkaway_labels.parquet"
    labels.write_bytes(b"pq")
    trainer_root = Path(__file__).resolve().parents[1]
    called_repo = (trainer_root / "feast_repo").resolve()
    mock_ensure.return_value = _bt3.FeastRegistryEnsureResult(
        feast_repo=called_repo,
        registry_path=called_repo / "data" / "registry.db",
        feast_registry_ready=True,
        feast_auto_apply_requested=False,
        feast_auto_apply_attempted=False,
        feast_auto_apply_succeeded=None,
        feast_apply_wall_sec=None,
    )
    mock_build.return_value = tmp_path / "training_set.parquet"

    with patch(
        "trainer_hightier.trainer._hpre.default_cleaned_bet_parquet_path",
        return_value=tmp_path / "cleaned",
    ), patch(
        "trainer_hightier.trainer.default_walkaway_labels_parquet_path",
        return_value=labels,
    ):
        args = HighTierTrainArgs(
            output_dir=tmp_path / "out",
            build_training_dataset=True,
            training_scope_policy=TrainingScopePolicy(
                recent_full_months=3,
                include_current_partial_month=True,
                as_of_date=date(2026, 6, 8),
            ),
        )
        _maybe_build_training_dataset(args, metrics={})

    mock_build.assert_called_once()
    cfg = mock_build.call_args.args[0]
    assert cfg.target_scope is not None
    assert cfg.target_scope.horizon_enabled is True
    assert cfg.target_scope.target_months == ("202603", "202604", "202605", "202606")


def test_training_manifest_includes_target_scope_audit_block() -> None:
    """Training-set manifest should record target scope for run audit only."""
    resolved = resolve_training_scope(
        TrainingScopePolicy(recent_full_months=3, as_of_date=date(2026, 6, 8)),
    )
    blob = _bt3._training_manifest(
        output_parquet=Path("/tmp/out/training_set.parquet"),
        feast_repo=Path("/tmp/feast_repo"),
        feature_service="walkaway_bet_trial_v1",
        row_count=10,
        labels_parquet=Path("/tmp/labels.parquet"),
        versioned_parquet=Path("/tmp/out/versions/training_set_x.parquet"),
        target_scope=resolved,
    )
    assert "target_scope" in blob
    assert blob["target_scope"]["target_months"] == list(resolved.target_months)
