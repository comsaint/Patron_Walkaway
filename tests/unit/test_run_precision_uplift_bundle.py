from __future__ import annotations

from pathlib import Path
from unittest import mock

from trainer.scripts import run_precision_uplift_bundle as mod


def _cp(returncode: int = 0) -> mock.Mock:
    cp = mock.Mock()
    cp.returncode = returncode
    return cp


def test_run_calls_trainer_with_precondition_env() -> None:
    with mock.patch("trainer.scripts.run_precision_uplift_bundle.subprocess.run", return_value=_cp(0)) as run_mock:
        rc = mod.run(
            [
                "--precondition-json",
                "out/p.json",
                "--skip-train",
            ]
        )
    assert rc == 0
    run_mock.assert_not_called()

    with mock.patch("trainer.scripts.run_precision_uplift_bundle.subprocess.run", return_value=_cp(0)) as run_mock:
        rc = mod.run(
            [
                "--precondition-json",
                "out/p.json",
                "--",
                "--max-optuna-trials",
                "1",
            ]
        )
    assert rc == 0
    call = run_mock.call_args
    cmd = call.args[0]
    env = call.kwargs["env"]
    assert len(cmd) >= 3
    assert cmd[1:3] == ["-m", "trainer.training.trainer"]
    assert cmd[-2:] == ["--max-optuna-trials", "1"]
    assert env["FIELD_TEST_OBJECTIVE_PRECONDITION_JSON"] == "out/p.json"


def test_run_full_pipeline_with_auto_discovery(tmp_path: Path) -> None:
    run_dir = tmp_path / "out" / "models" / "r1"
    run_dir.mkdir(parents=True)

    with mock.patch("trainer.scripts.run_precision_uplift_bundle.subprocess.run", return_value=_cp(0)) as run_mock:
        rc = mod.run(
            [
                "--skip-train",
                "--auto-discover-latest-run-dir-glob",
                str(tmp_path / "out" / "models" / "*"),
                "--emit-w1-freeze-evidence",
                "--emit-w2-parity",
            ]
        )

    assert rc == 0
    assert run_mock.call_count == 2
    w1_cmd = run_mock.call_args_list[0].args[0]
    w2_cmd = run_mock.call_args_list[1].args[0]
    assert "trainer.scripts.build_w1_freeze_evidence" in w1_cmd
    assert "--run-dir" in w1_cmd
    assert str(run_dir) in w1_cmd
    assert "trainer.scripts.report_w2_objective_parity" in w2_cmd
    assert "--run-dir" in w2_cmd
    assert str(run_dir) in w2_cmd


def test_run_stops_when_trainer_fails() -> None:
    with mock.patch("trainer.scripts.run_precision_uplift_bundle.subprocess.run", return_value=_cp(2)) as run_mock:
        rc = mod.run(["--", "--max-optuna-trials", "1"])
    assert rc == 2
    assert run_mock.call_count == 1
