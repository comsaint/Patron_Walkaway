"""Phase-0 layered contracts: enumeration + validate_layered_contracts."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]


def _enumerate_mod():
    path = _REPO / "scripts" / "enumerate_deploy_features.py"
    spec = importlib.util.spec_from_file_location("_lda_enum_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_enumerate_features_sorted_and_stable() -> None:
    spec_path = _REPO / "package" / "deploy" / "models" / "feature_spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert isinstance(spec, dict)
    mod = _enumerate_mod()
    rows = mod.enumerate_features(spec)
    keys = [(r["track_section"], r["feature_id"]) for r in rows]
    assert keys == sorted(keys)
    path = _REPO / "artifacts" / "layered_data_assets" / "contracts" / "features_enumerated.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["feature_count"] == len(rows)


def test_validate_layered_contracts_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "validate_layered_contracts.py")],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
