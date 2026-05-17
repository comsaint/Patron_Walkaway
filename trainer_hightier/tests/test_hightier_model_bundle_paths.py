"""Versioned bundle + manifest parity with ``trainer.core.model_bundle_paths``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from trainer.core.model_bundle_paths import (
    LATEST_MODEL_MANIFEST_NAME,
    resolve_model_bundle_dir,
)
from trainer_hightier.config import DuckDbRuntimeConfig, Step5TrainConfig
from trainer_hightier.trainer import HighTierTrainArgs, run_training


@pytest.fixture()
def neutral_mlflow_spy() -> Iterator[None]:
    """Disable MLflow I/O during :func:`~trainer_hightier.trainer.run_training`."""

    with patch("trainer_hightier.trainer.safe_start_run", side_effect=lambda **_kwargs: nullcontext()):
        with patch("trainer_hightier.trainer.warm_up_mlflow_run_safe"):
            with patch("trainer_hightier.trainer.log_tags_safe"):
                with patch("trainer_hightier.trainer.log_params_safe"):
                    with patch("trainer_hightier.trainer.log_metrics_safe"):
                        with patch("trainer_hightier.trainer.log_artifact_safe"):
                            yield


_FIXED_MV = "20990101-000000-cafebff"


def _minimal_success_execute(args: HighTierTrainArgs, metrics: dict) -> None:
    """Mimic successful Step 5 outputs under ``step5_bundle_dir``."""

    bd = args.step5_bundle_dir
    assert bd is not None
    tpm = bd / "training_metrics.json"
    mp = bd / "model.pkl"
    tpm.write_text("{}", encoding="utf-8")
    mp.write_bytes(b"h")
    metrics["training_metrics_path"] = str(tpm.resolve())
    metrics["model_path"] = str(mp.resolve())


def test_run_training_success_writes_latest_manifest_resolve(
    tmp_path: Path,
    neutral_mlflow_spy: None,
) -> None:
    """After SUCCESS, *_latest_model_manifest.json* resolves to the new ``model.pkl``."""

    vr = tmp_path / "versions"
    args = HighTierTrainArgs(
        output_dir=vr,
        start_from_features=True,
        run_step4=False,
        build_training_dataset=False,
        duckdb_runtime=DuckDbRuntimeConfig(),
        step5=Step5TrainConfig(run_step5=True),
    )
    with patch(
        "trainer_hightier.trainer._mlflow_hightier_run_name",
        return_value=_FIXED_MV,
    ), patch(
        "trainer_hightier.trainer._run_training_execute_steps",
        side_effect=_minimal_success_execute,
    ):
        run_training(args)

    mf = vr / LATEST_MODEL_MANIFEST_NAME
    assert mf.is_file()
    blob = json.loads(mf.read_text(encoding="utf-8"))
    assert blob.get("bundle_relative") == _FIXED_MV

    resolved = resolve_model_bundle_dir(vr)
    assert (resolved / "model.pkl").is_file()
    assert (resolved / "run_report.json").is_file()
    assert (resolved / "run_summary.json").is_file()
    assert (resolved / "metrics_detailed.json").is_file()
    assert (resolved / "pipeline_debug.json").is_file()


def test_run_training_raises_when_existing_model_under_version(
    tmp_path: Path,
    neutral_mlflow_spy: None,
) -> None:
    """``model_version`` subdirectory with ``model.pkl`` blocks a second train (no overwrite)."""

    vr = tmp_path / "versions"
    pre = vr / _FIXED_MV / "model.pkl"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(b"x")

    args = HighTierTrainArgs(
        output_dir=vr,
        start_from_features=True,
        run_step4=False,
        build_training_dataset=False,
        duckdb_runtime=DuckDbRuntimeConfig(),
        step5=Step5TrainConfig(run_step5=True),
    )
    with patch(
        "trainer_hightier.trainer._mlflow_hightier_run_name",
        return_value=_FIXED_MV,
    ):
        with pytest.raises(FileExistsError, match="existing high-tier Step 5"):
            run_training(args)
