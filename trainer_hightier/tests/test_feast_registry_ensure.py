"""Tests for Feast schema gate ``ensure_feast_schema_ready`` and Step 3 wiring."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

b3 = importlib.import_module("trainer_hightier.03_build_training_data")
from trainer_hightier.serving import feast_online_adapter as adapter


def _touch_registry(feast_repo: Path) -> Path:
    dr = feast_repo / "data"
    dr.mkdir(parents=True, exist_ok=True)
    reg = dr / "registry.db"
    reg.write_bytes(b"x")
    return reg


def test_ensure_existing_registry_skips_apply(tmp_path: Path) -> None:
    """No drift → never spawn ``feast apply``."""

    fr = tmp_path / "feast_repo"
    _touch_registry(fr)
    with patch.object(adapter, "feast_schema_drift_issues", return_value=[]), patch(
        "trainer_hightier.serving.feast_online_refresh.run_feast_apply",
    ) as mock_apply:
        r = b3.ensure_feast_registry_ready(fr, auto_apply=True)
    mock_apply.assert_not_called()
    assert r.feast_registry_ready is True
    assert r.feast_auto_apply_attempted is False
    assert r.feast_schema_drift_issues == ()


def test_ensure_missing_registry_runs_apply_and_succeeds(tmp_path: Path) -> None:
    """Missing registry (drift) + auto_apply runs feast apply then sees registry file."""

    fr = tmp_path / "feast_repo"
    fr.mkdir(parents=True, exist_ok=True)
    reg = fr / "data" / "registry.db"

    def apply_side_effect(repo: Path, *, reset_runtime: bool = False) -> float:
        _touch_registry(repo)
        return 1.23

    with patch.object(
        adapter,
        "feast_schema_drift_issues",
        side_effect=[["Feast registry missing"], []],
    ), patch(
        "trainer_hightier.serving.feast_online_refresh.run_feast_apply",
        side_effect=apply_side_effect,
    ) as mock_apply:
        r = b3.ensure_feast_registry_ready(fr, auto_apply=True)

    mock_apply.assert_called_once()
    assert reg.is_file()
    assert r.feast_auto_apply_attempted is True
    assert r.feast_auto_apply_succeeded is True
    assert r.feast_registry_ready is True


def test_ensure_schema_drift_on_existing_registry_triggers_apply(tmp_path: Path) -> None:
    """Registry file present but schema drift → conditional apply."""

    fr = tmp_path / "feast_repo"
    _touch_registry(fr)
    drift_msg = (
        "feature view 'mid_term_daily_spike_features' missing 1 column(s): "
        "[anchor_gaming_day_event]"
    )
    with patch.object(
        adapter,
        "feast_schema_drift_issues",
        side_effect=[[drift_msg], []],
    ), patch(
        "trainer_hightier.serving.feast_online_refresh.run_feast_apply",
        return_value=0.42,
    ) as mock_apply:
        r = b3.ensure_feast_registry_ready(fr, auto_apply=True)

    mock_apply.assert_called_once()
    assert r.feast_auto_apply_attempted is True
    assert drift_msg in r.feast_schema_drift_issues


def test_ensure_apply_failure_raises_with_context(tmp_path: Path) -> None:
    """Failed feast apply → RuntimeError propagated."""

    fr = tmp_path / "feast_repo"
    fr.mkdir(parents=True, exist_ok=True)
    with patch.object(adapter, "feast_schema_drift_issues", return_value=["missing registry"]), patch(
        "trainer_hightier.serving.feast_online_refresh.run_feast_apply",
        side_effect=RuntimeError("feast apply failed (exit 2): boom"),
    ):
        with pytest.raises(RuntimeError, match="feast apply failed"):
            b3.ensure_feast_registry_ready(fr, auto_apply=True)


def test_ensure_drift_remains_after_apply_raises(tmp_path: Path) -> None:
    fr = tmp_path / "feast_repo"
    _touch_registry(fr)
    drift = ["feature view 'mid_term_daily_spike_features' missing 1 column(s): [anchor_gaming_day_event]"]
    with patch.object(adapter, "feast_schema_drift_issues", return_value=drift), patch(
        "trainer_hightier.serving.feast_online_refresh.run_feast_apply",
        return_value=0.1,
    ):
        with pytest.raises(RuntimeError, match="drift remains after apply"):
            b3.ensure_feast_registry_ready(fr, auto_apply=True)


def test_ensure_no_cli_raises(tmp_path: Path) -> None:
    fr = tmp_path / "feast_repo"
    fr.mkdir(parents=True, exist_ok=True)
    with patch.object(adapter, "feast_schema_drift_issues", return_value=["missing registry"]), patch(
        "trainer_hightier.serving.feast_online_refresh.run_feast_apply",
        side_effect=RuntimeError("feast CLI not found on PATH"),
    ):
        with pytest.raises(RuntimeError, match="feast CLI not found"):
            b3.ensure_feast_registry_ready(fr, auto_apply=True)


def test_ensure_disabled_raises_file_not_found(tmp_path: Path) -> None:
    fr = tmp_path / "feast_repo"
    fr.mkdir(parents=True, exist_ok=True)
    with patch.object(adapter, "feast_schema_drift_issues", return_value=["Feast registry missing at x"]):
        with pytest.raises(FileNotFoundError, match="Feast schema not ready"):
            b3.ensure_feast_registry_ready(fr, auto_apply=False)


def test_feast_schema_drift_issues_missing_registry(tmp_path: Path) -> None:
    repo = tmp_path / "feast_repo"
    issues = adapter.feast_schema_drift_issues(repo)
    assert len(issues) == 1
    assert "registry missing" in issues[0].lower()


def test_feast_schema_drift_issues_missing_columns(tmp_path: Path) -> None:
    fr = tmp_path / "feast_repo"
    _touch_registry(fr)
    with patch.object(
        adapter,
        "feast_registry_feature_view_columns",
        side_effect=[
            frozenset({"canonical_id", "event_timestamp"}),
            frozenset({"canonical_id", "event_timestamp", "slow_col_a"}),
        ],
    ):
        issues = adapter.feast_schema_drift_issues(fr)
    assert any("mid_term_daily_spike_features" in i for i in issues)
    assert any("anchor_gaming_day_event" in i for i in issues)


def test_feast_registry_ensure_result_to_metrics_roundtrip() -> None:
    rp = Path("/tmp/feast_fake")
    reg = rp / "data" / "registry.db"
    r = b3.FeastRegistryEnsureResult(
        feast_repo=rp,
        registry_path=reg,
        feast_registry_ready=True,
        feast_auto_apply_requested=True,
        feast_auto_apply_attempted=True,
        feast_auto_apply_succeeded=True,
        feast_apply_wall_sec=1.23,
        feast_schema_drift_issues=("missing anchor_gaming_day_event",),
    )
    d = b3.feast_registry_ensure_result_to_metrics(r)
    assert d["feast_auto_apply_attempted"] is True
    assert d["feast_auto_apply_succeeded"] is True
    assert abs(float(d["feast_apply_wall_sec"] or 0) - 1.23) < 1e-6
    assert d["feast_schema_drift_issues"] == ["missing anchor_gaming_day_event"]


@patch("trainer_hightier.trainer._b3.ensure_feast_registry_ready")
@patch("trainer_hightier.trainer._b3.build_training_data")
@patch(
    "trainer_hightier.trainer._hbet.cleaned_bet_dataset_has_any_parquet",
    return_value=True,
)
def test_maybe_build_training_dataset_logs_feast_echo(
    _mock_cb: MagicMock,
    mock_build: MagicMock,
    mock_ensure: MagicMock,
    tmp_path: Path,
) -> None:
    """trainer Step 3 path records ``metrics['feast_auto_apply']`` after ensure."""

    from trainer_hightier.trainer import HighTierTrainArgs, _maybe_build_training_dataset

    labels = tmp_path / "walkaway_labels.parquet"
    labels.write_bytes(b"pq")
    trainer_root = Path(__file__).resolve().parents[1]
    called_repo = (trainer_root / "feast_repo").resolve()
    reg_path = called_repo / "data" / "registry.db"
    mock_ensure.return_value = b3.FeastRegistryEnsureResult(
        feast_repo=called_repo,
        registry_path=reg_path,
        feast_registry_ready=True,
        feast_auto_apply_requested=True,
        feast_auto_apply_attempted=False,
        feast_auto_apply_succeeded=None,
        feast_apply_wall_sec=None,
    )
    with patch(
        "trainer_hightier.trainer._hpre.default_cleaned_bet_parquet_path",
        return_value=tmp_path / "cleaned",
    ), patch(
        "trainer_hightier.trainer.default_walkaway_labels_parquet_path",
        return_value=labels,
    ):
        args = HighTierTrainArgs(
            output_dir=tmp_path / "out",
            auto_feast_apply=True,
            build_training_dataset=True,
        )
        metrics: dict[str, object] = {}
        _maybe_build_training_dataset(args, metrics=metrics)

    mock_ensure.assert_called_once()
    assert mock_ensure.call_args.kwargs["auto_apply"] is True
    feast_repo_arg = Path(mock_ensure.call_args.args[0])
    assert feast_repo_arg.name == "feast_repo"
    assert feast_repo_arg.is_relative_to(trainer_root.resolve())

    assert "feast_auto_apply" in metrics
    assert metrics["feast_auto_apply"]["feast_registry_ready"] is True
    mock_build.assert_called_once()


def test_trainer_argparser_disable_auto_feast_apply() -> None:
    """``--disable-auto-feast-apply`` is wired on the main trainer CLI."""

    from trainer_hightier.trainer import _build_argparser

    ns = _build_argparser().parse_args(["--disable-auto-feast-apply"])
    assert ns.disable_auto_feast_apply is True


def test_trainer_cli_defaults_match_config_ssot() -> None:
    """Main trainer CLI defaults must follow ``trainer_hightier.config`` dataclass SSOT."""

    from trainer_hightier.config import (
        DEFAULT_RANDOM_SEED,
        DEFAULT_RUN_PROFILE_NAME,
        DEFAULT_TRAINING_FEATURE_SERVICE,
        PartitionIngressConfig,
        Step5TrainConfig,
    )
    from trainer_hightier.trainer import HighTierTrainArgs, _build_argparser, _train_args_from_cli_namespace

    ns = _build_argparser().parse_args([])
    assert ns.run_profile == DEFAULT_RUN_PROFILE_NAME
    assert ns.optuna_timeout_sec == Step5TrainConfig().optuna_timeout_sec
    assert ns.partition_backfill_count == PartitionIngressConfig().backfill_month_count

    args = _train_args_from_cli_namespace(ns)
    assert args.random_seed == DEFAULT_RANDOM_SEED
    assert args.training_feature_service == DEFAULT_TRAINING_FEATURE_SERVICE
    assert args.partition_backfill_month_count == PartitionIngressConfig().backfill_month_count
    assert args.step5.optuna_timeout_sec == Step5TrainConfig().optuna_timeout_sec
    assert args.step5.run_step5 is True
    assert args.step5.skip_optuna is False

    bare = HighTierTrainArgs(output_dir=Path("/tmp/out"))
    assert bare.random_seed == DEFAULT_RANDOM_SEED
    assert bare.training_feature_service == DEFAULT_TRAINING_FEATURE_SERVICE


def test_feature_experiment_parse_disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["fe_run", "--disable-auto-feast-apply"])
    from trainer_hightier.feature_experiment import run_pipeline as rp

    ns = rp._parse_args()
    assert getattr(ns, "disable_auto_feast_apply") is True
