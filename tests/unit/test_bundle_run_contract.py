"""trainer.core.bundle_run_contract — W2 SSOT for backtest / scorer contract block."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from trainer.core import bundle_run_contract as brc
from trainer.core.bundle_run_contract import read_bundle_run_contract_block


def test_read_block_prefers_training_metrics_selection_mode(tmp_path: Path) -> None:
    root = tmp_path / "b"
    root.mkdir()
    (root / "training_metrics.json").write_text(
        json.dumps({"selection_mode": "field_test"}),
        encoding="utf-8",
    )
    out = read_bundle_run_contract_block(root)
    assert out["selection_mode"] == "field_test"
    assert out["selection_mode_source"] == "artifact_training_metrics.json"


def test_read_block_production_ratio_override(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    out = read_bundle_run_contract_block(root, production_neg_pos_ratio=99.0)
    assert out["production_neg_pos_ratio"] == 99.0


def test_read_block_none_root_uses_config_defaults() -> None:
    with mock.patch.object(brc.config, "SELECTION_MODE", "legacy"):
        out = read_bundle_run_contract_block(None)
    assert out["selection_mode"] == "legacy"
    assert out["selection_mode_source"] == "config"


def test_read_block_invalid_json(tmp_path: Path) -> None:
    root = tmp_path / "x"
    root.mkdir()
    (root / "training_metrics.json").write_text("{", encoding="utf-8")
    with mock.patch.object(brc.config, "SELECTION_MODE", "legacy"):
        out = read_bundle_run_contract_block(root)
    assert out["selection_mode"] == "legacy"
    assert out["selection_mode_source"] == "config"
