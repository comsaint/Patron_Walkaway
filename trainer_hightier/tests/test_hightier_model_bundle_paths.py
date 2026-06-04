"""Versioned bundle + manifest (``trainer_hightier.core.model_bundle_paths``)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from trainer_hightier.core.model_bundle_paths import (
    DEPLOY_E2E_GATE_REPORT_FILENAME,
    LATEST_MODEL_MANIFEST_NAME,
    model_bundle_report_path,
    resolve_model_bundle_dir,
    resolve_model_bundle_for_reports,
)
from trainer_hightier.config import DuckDbRuntimeConfig, Step5TrainConfig, Step6ParityConfig
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
    split_src = bd.parent / "_step4_artifacts" / "split_report.json"
    split_src.parent.mkdir(parents=True, exist_ok=True)
    split_src.write_text('{"splits": [{"split": "train"}]}', encoding="utf-8")
    metrics["step4_split_report"] = str(split_src.resolve())


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
        step6=Step6ParityConfig(run_step6=False),
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
    assert not (resolved / "run_summary.json").exists()
    assert (resolved / "split_report.json").is_file()
    report = json.loads((resolved / "run_report.json").read_text(encoding="utf-8"))
    assert report.get("schema") == "trainer_hightier.run_report.v1"
    assert report.get("status") == "SUCCESS"
    assert isinstance(report.get("summary"), dict)
    assert isinstance(report.get("evaluation_detail"), dict)
    assert isinstance(report.get("pipeline_debug"), dict)
    assert report.get("artifacts", {}).get("split_report_path") == str((resolved / "split_report.json").resolve())


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


def test_resolve_model_bundle_for_reports_prefers_canonical_training_dir(tmp_path: Path) -> None:
    """Deploy gate reports target ``versions_root/<model_version>`` when it exists."""

    versions_root = tmp_path / "out" / "models_high_tier_mvp"
    mv = "20260522-124003-245bd1f"
    canonical = versions_root / mv
    canonical.mkdir(parents=True)
    (canonical / "model.pkl").write_bytes(b"m")
    (canonical / "model_version").write_text(mv, encoding="utf-8")

    deploy = tmp_path / "deploy"
    embedded = deploy / "models"
    embedded.mkdir(parents=True)
    (embedded / "model.pkl").write_bytes(b"m")
    (embedded / "model_version").write_text(mv, encoding="utf-8")
    (deploy / "deploy_bundle_paths.json").write_text(
        json.dumps({"model_bundle_dir": "models"}),
        encoding="utf-8",
    )

    resolved = resolve_model_bundle_for_reports(
        deploy_bundle_dir=deploy,
        versions_root=versions_root,
    )
    assert resolved == canonical.resolve()
    out = model_bundle_report_path(resolved, DEPLOY_E2E_GATE_REPORT_FILENAME)
    assert out.parent == canonical.resolve()
    assert out.name == DEPLOY_E2E_GATE_REPORT_FILENAME


def test_resolve_model_bundle_for_reports_explicit_model_dir(tmp_path: Path) -> None:
    """Explicit ``--model-dir`` wins over deploy bundle layout."""

    explicit = tmp_path / "bundle"
    explicit.mkdir()
    (explicit / "model.pkl").write_bytes(b"m")
    resolved = resolve_model_bundle_for_reports(model_dir=explicit)
    assert resolved == explicit.resolve()
