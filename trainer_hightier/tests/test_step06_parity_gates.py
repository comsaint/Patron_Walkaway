"""Tests for Step 06 layered parity gates."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from trainer_hightier.config import Step6ParityConfig


def _load_step06_module():
    step06_path = Path(__file__).resolve().parents[1] / "06_verify_training_serving_parity.py"
    spec = importlib.util.spec_from_file_location("step06_test", step06_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_model_exit_code_slow_gate_only() -> None:
    mod = _load_step06_module()
    report_fail_slow = {
        "slow_gate": {"verdict": "fail", "issues": ["x"]},
        "all_feature_gate": {"verdict": "pass", "issues": []},
    }
    cfg = Step6ParityConfig(hard_fail_slow_gate=True, hard_fail_all_feature_gate=False)
    assert mod.model_exit_code(report_fail_slow, parity_cfg=cfg) == 1

    cfg_warn = Step6ParityConfig(hard_fail_slow_gate=False, hard_fail_all_feature_gate=False)
    assert mod.model_exit_code(report_fail_slow, parity_cfg=cfg_warn) == 0
