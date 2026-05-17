"""MLflow integration smoke tests for :mod:`trainer_hightier.trainer`."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from trainer_hightier.config import DuckDbRuntimeConfig, Step5TrainConfig
from trainer_hightier.trainer import HighTierTrainArgs, run_training


@pytest.fixture
def minimal_mlflow_patches() -> tuple:
    """Patch MLflow helpers to nullcontext + noop mocks."""

    with patch("trainer_hightier.trainer.safe_start_run", side_effect=lambda **kwargs: nullcontext()):
        with patch("trainer_hightier.trainer.warm_up_mlflow_run_safe") as warm_m:
            with patch("trainer_hightier.trainer.log_tags_safe") as tags_m:
                with patch("trainer_hightier.trainer.log_params_safe") as params_m:
                    with patch("trainer_hightier.trainer.log_metrics_safe") as metrics_m:
                        with patch("trainer_hightier.trainer.log_artifact_safe") as art_m:
                            yield warm_m, tags_m, params_m, metrics_m, art_m


def test_run_training_mlflow_success_path_logs_success_tag_and_run_report_artifact(
    tmp_path: Path,
    minimal_mlflow_patches: tuple,
) -> None:
    """SUCCESS tag and whitelist artifact logging occur after successful pipeline."""

    _warm_m, tags_m, params_m, metrics_m, art_m = minimal_mlflow_patches

    def _inject_metrics(args: HighTierTrainArgs, metrics: dict) -> None:
        metrics["step5_seconds"] = 12.5
        metrics["val_precision"] = 0.61
        metrics["candidate_registry"] = {
            "registry_version": "t_registry_vtest",
            "resolved_path": str(tmp_path / "registry.yaml"),
            "n_baseline_features": 4,
        }
        metrics["training_metrics_path"] = str(tmp_path / "out" / "training_metrics.json")
        metrics["model_path"] = str(tmp_path / "out" / "model.pkl")
        metrics["step5_training_metrics_path"] = metrics["training_metrics_path"]
        metrics["step5_model_path"] = metrics["model_path"]
        metrics["step4_split_report"] = str(tmp_path / "split_report.json")

    def fake_execute(args: HighTierTrainArgs, metrics: dict) -> None:
        _inject_metrics(args, metrics)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        Path(metrics["step5_training_metrics_path"]).write_text("{}", encoding="utf-8")
        Path(metrics["step5_model_path"]).write_bytes(b"x")
        Path(metrics["step4_split_report"]).write_text("{}", encoding="utf-8")

    args = HighTierTrainArgs(
        output_dir=tmp_path / "out",
        start_from_features=True,
        run_step4=False,
        step5=Step5TrainConfig(run_step5=False),
        duckdb_runtime=DuckDbRuntimeConfig(),
        run_profile_name="default",
    )
    with patch("trainer_hightier.trainer._run_training_execute_steps", side_effect=fake_execute):
        run_training(args)

    statuses = [c.args[0].get("status") for c in tags_m.call_args_list if c.args and isinstance(c.args[0], dict)]
    assert "RUNNING" in statuses
    assert "SUCCESS" in statuses

    rp = tmp_path / "out" / "run_report.json"
    assert rp.is_file()
    assert (tmp_path / "out" / "run_summary.json").is_file()
    assert (tmp_path / "out" / "metrics_detailed.json").is_file()
    assert (tmp_path / "out" / "pipeline_debug.json").is_file()

    artifact_paths = [c.args[0] for c in art_m.call_args_list]
    assert any(Path(p).resolve() == rp.resolve() for p in artifact_paths)
    rs_logged = tmp_path / "out" / "run_summary.json"
    assert any(Path(p).resolve() == rs_logged.resolve() for p in artifact_paths)

    metrics_m.assert_called_once()
    logged_metrics = metrics_m.call_args[0][0]
    assert logged_metrics.get("step5_seconds") == 12.5
    assert logged_metrics.get("val_precision") == 0.61


def test_run_training_mlflow_failure_path_logs_failed_tag(
    tmp_path: Path,
    minimal_mlflow_patches: tuple,
) -> None:
    """FAILED tag is recorded before re-raising."""

    _warm_m, tags_m, _params_m, _metrics_m, _art_m = minimal_mlflow_patches

    args = HighTierTrainArgs(
        output_dir=tmp_path / "out",
        start_from_features=True,
        run_step4=False,
        step5=Step5TrainConfig(run_step5=False),
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    with patch(
        "trainer_hightier.trainer._run_training_execute_steps",
        side_effect=RuntimeError("planned_failure"),
    ):
        with pytest.raises(RuntimeError, match="planned_failure"):
            run_training(args)

    failed_calls = [
        c.args[0]
        for c in tags_m.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("status") == "FAILED"
    ]
    assert len(failed_calls) >= 1
    assert "planned_failure" in failed_calls[0].get("error", "")
