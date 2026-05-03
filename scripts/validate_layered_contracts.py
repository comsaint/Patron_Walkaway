#!/usr/bin/env python3
"""Local Phase-0 gate: registry, JSON schemas, feature enumeration artifacts."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

try:
    import jsonschema
    from jsonschema import Draft7Validator
except ImportError as exc:  # pragma: no cover
    print("jsonschema is required (see requirements.txt).", file=sys.stderr)
    raise SystemExit(2) from exc

_REPO = Path(__file__).resolve().parent.parent


def _load_enumerate_module() -> ModuleType:
    """Load ``enumerate_deploy_features`` as a module (no package import path)."""
    path = _REPO / "scripts" / "enumerate_deploy_features.py"
    spec = importlib.util.spec_from_file_location("_lda_enumerate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(cmd: list[str]) -> None:
    """Run ``cmd`` from repo root; raise ``RuntimeError`` if exit code is non-zero."""
    proc = subprocess.run(cmd, cwd=str(_REPO), check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}")


def _load_json(p: Path) -> object:
    """Parse JSON from ``p`` (UTF-8)."""
    return json.loads(p.read_text(encoding="utf-8"))


def _validate_json_schema(instance: object, schema_path: Path) -> None:
    """Validate ``instance`` against the JSON Schema at ``schema_path`` (Draft 7)."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft7Validator(schema).validate(instance)


def _validate_correction_file(path: Path, schema_path: Path) -> None:
    """Validate each row of a JSON array correction log against ``schema_path``."""
    data = _load_json(path)
    if not isinstance(data, list):
        raise TypeError(f"{path}: expected JSON array")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema)
    for row in data:
        validator.validate(row)


def _validate_enumeration_artifacts() -> None:
    """Check enumerated features JSON and dependency CSV match live ``feature_spec``."""
    import csv

    import yaml

    mod = _load_enumerate_module()
    spec_path = _REPO / "package" / "deploy" / "models" / "feature_spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise TypeError("feature_spec root must be a mapping")
    rows = mod.enumerate_features(spec)
    fresh = mod.build_enumerated_payload(spec_path, rows, repo_root=_REPO)
    path = _REPO / "artifacts" / "layered_data_assets" / "contracts" / "features_enumerated.json"
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path}; run: python scripts/enumerate_deploy_features.py")
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    if on_disk.get("source_spec_path") != fresh.get("source_spec_path"):
        raise ValueError(
            f"features_enumerated.json stale source_spec_path: disk={on_disk.get('source_spec_path')!r} "
            f"live={fresh.get('source_spec_path')!r}; re-run enumerate_deploy_features.py"
        )
    if on_disk.get("feature_count") != fresh.get("feature_count"):
        raise ValueError(
            f"features_enumerated.json stale: disk count={on_disk.get('feature_count')} "
            f"live={fresh.get('feature_count')}; re-run enumerate_deploy_features.py"
        )
    if on_disk.get("features") != fresh.get("features"):
        raise ValueError(
            "features_enumerated.json differs from live enumeration; re-run enumerate_deploy_features.py"
        )
    csv_path = _REPO / "artifacts" / "layered_data_assets" / "contracts" / "feature_dependency_registry.csv"
    if not csv_path.is_file():
        raise FileNotFoundError(f"Missing {csv_path}; run: python scripts/enumerate_deploy_features.py")
    with csv_path.open(newline="", encoding="utf-8") as f:
        nrows = sum(1 for _ in csv.DictReader(f))
    if nrows != int(fresh.get("feature_count", 0)):
        raise ValueError(
            f"feature_dependency_registry.csv row count {nrows} != feature_count {fresh.get('feature_count')}"
        )


def main() -> int:
    """Run layered Phase-0 contract checks; return 0 on success, 1 on failure."""
    try:
        _run([sys.executable, str(_REPO / "scripts" / "validate_time_semantics_registry.py")])
        manifest_ex = _REPO / "schema" / "examples" / "manifest_l1_example.json"
        manifest_sc = _REPO / "schema" / "manifest_layered_data_assets.schema.json"
        _validate_json_schema(_load_json(manifest_ex), manifest_sc)
        manifest_bet = _REPO / "schema" / "examples" / "manifest_preprocess_bet_l1_example.json"
        _validate_json_schema(_load_json(manifest_bet), manifest_sc)
        manifest_run = _REPO / "schema" / "examples" / "manifest_run_fact_l1_example.json"
        _validate_json_schema(_load_json(manifest_run), manifest_sc)
        manifest_rbm = _REPO / "schema" / "examples" / "manifest_run_bet_map_l1_example.json"
        _validate_json_schema(_load_json(manifest_rbm), manifest_sc)
        manifest_rdb = _REPO / "schema" / "examples" / "manifest_run_day_bridge_l1_example.json"
        _validate_json_schema(_load_json(manifest_rdb), manifest_sc)
        manifest_tf = _REPO / "schema" / "examples" / "manifest_trip_fact_l1_example.json"
        _validate_json_schema(_load_json(manifest_tf), manifest_sc)
        manifest_trm = _REPO / "schema" / "examples" / "manifest_trip_run_map_l1_example.json"
        _validate_json_schema(_load_json(manifest_trm), manifest_sc)
        corr_ex = _REPO / "schema" / "examples" / "late_arrival_correction_log.example.json"
        corr_sc = _REPO / "schema" / "late_arrival_correction_log.schema.json"
        _validate_correction_file(corr_ex, corr_sc)
        _validate_enumeration_artifacts()
    except (FileNotFoundError, TypeError, ValueError, jsonschema.ValidationError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print("validate_layered_contracts: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
