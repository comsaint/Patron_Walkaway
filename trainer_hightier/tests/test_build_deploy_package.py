"""Tests for ``trainer_hightier.build_deploy_package`` (portable bundle layout)."""

from __future__ import annotations

import importlib
import json
import pickle
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from trainer_hightier.build_deploy_package import (
    _REPO_ROOT,
    _bump_patch_version,
    _pip_freeze_package_name,
    _read_pyproject_version,
    _temporary_pyproject_version,
    _walkaway_fields_from_training_metrics,
    _wheel_package_version,
    _write_bundle_info,
    _write_deploy_paths,
    _write_pyproject_version,
    build_deploy_package,
)
from trainer_hightier.config import FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME, DEPLOY_CONTRACT_FILENAME
from trainer_hightier.core.model_bundle_paths import DEPLOY_E2E_GATE_REPORT_FILENAME
from trainer_hightier.serving.adt_allowlist import sha256_file


def test_bump_patch_version_increments_numeric_patch() -> None:
    assert _bump_patch_version("0.3.0") == "0.3.1"
    assert _bump_patch_version("1.2.9") == "1.2.10"


def test_walkaway_fields_from_training_metrics_extracts_contract_fields() -> None:
    metrics = {
        "walkaway_gap_min": 60,
        "alert_horizon_min": 15,
        "walkaway_label_contract_id": "walkaway_v1_gap60",
        "unrelated": "ignored",
    }
    assert _walkaway_fields_from_training_metrics(metrics) == {
        "walkaway_gap_min": 60,
        "alert_horizon_min": 15,
        "walkaway_label_contract_id": "walkaway_v1_gap60",
    }


def test_walkaway_fields_from_training_metrics_omits_blank_contract_id() -> None:
    assert _walkaway_fields_from_training_metrics(
        {"walkaway_gap_min": 30, "walkaway_label_contract_id": "  "}
    ) == {"walkaway_gap_min": 30}


def test_write_bundle_info_stamps_walkaway_fields(tmp_path: Path) -> None:
    walkaway = {
        "walkaway_gap_min": 60,
        "alert_horizon_min": 15,
        "walkaway_label_contract_id": "walkaway_v1_gap60",
    }
    out = tmp_path / "bundle_info.json"
    _write_bundle_info(
        out,
        model_version="mv1",
        manifest_version="1",
        package_version="0.1.0",
        allowlist_sha="a" * 64,
        slow_patron_sha="b" * 64,
        canonical_mapping_sha="c" * 64,
        frozen_fingerprint_sha256="d" * 64,
        build_time_iso="2026-06-22T00:00:00+00:00",
        walkaway_fields=walkaway,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["walkaway_gap_min"] == 60
    assert payload["alert_horizon_min"] == 15
    assert payload["walkaway_label_contract_id"] == "walkaway_v1_gap60"


def test_write_deploy_paths_stamps_walkaway_fields(tmp_path: Path) -> None:
    walkaway = {"walkaway_gap_min": 30, "alert_horizon_min": 15}
    out = tmp_path / "deploy_paths.json"
    _write_deploy_paths(out, mapping_name="canonical_mapping.parquet", walkaway_fields=walkaway)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["walkaway_gap_min"] == 30
    assert payload["alert_horizon_min"] == 15
    assert payload["canonical_mapping_parquet"] == "mapping/canonical_mapping.parquet"


def test_bump_pyproject_patch_version_writes_toml(tmp_path: Path) -> None:
    pyproj = tmp_path / "pyproject.toml"
    pyproj.write_text('[project]\nname = "trainer-hightier"\nversion = "2.4.6"\n', encoding="utf-8")
    assert _read_pyproject_version(pyproj) == "2.4.6"
    _write_pyproject_version(pyproj, _bump_patch_version(_read_pyproject_version(pyproj)))
    assert _read_pyproject_version(pyproj) == "2.4.7"
    _write_pyproject_version(pyproj, _bump_patch_version(_read_pyproject_version(pyproj)))
    assert _read_pyproject_version(pyproj) == "2.4.8"
    _write_pyproject_version(pyproj, "9.0.0")
    assert _read_pyproject_version(pyproj) == "9.0.0"


def test_wheel_package_version_bump_is_transient_plan(tmp_path: Path) -> None:
    pyproj = tmp_path / "pyproject.toml"
    pyproj.write_text('[project]\nname = "trainer-hightier"\nversion = "1.0.3"\n', encoding="utf-8")
    assert _wheel_package_version(pyproj=pyproj, bump_version=False) == "1.0.3"
    assert _read_pyproject_version(pyproj) == "1.0.3"
    assert _wheel_package_version(pyproj=pyproj, bump_version=True) == "1.0.4"
    assert _read_pyproject_version(pyproj) == "1.0.3"


def test_temporary_pyproject_version_restores_original_bytes(tmp_path: Path) -> None:
    pyproj = tmp_path / "pyproject.toml"
    original = '[project]\nname = "trainer-hightier"\nversion = "3.1.0"\n'
    pyproj.write_text(original, encoding="utf-8")
    with _temporary_pyproject_version(pyproj, "3.1.1"):
        assert _read_pyproject_version(pyproj) == "3.1.1"
    assert pyproj.read_text(encoding="utf-8") == original


def _minimal_pack_argv(
    *,
    model_src: Path,
    snap_src: Path,
    mapping: Path,
    out: Path,
    extra: list[str] | None = None,
) -> list[str]:
    return [
        "--model-source",
        str(model_src),
        "--snapshot-manifest-source",
        str(snap_src),
        "--mapping-source",
        str(mapping),
        "--output-dir",
        str(out),
        "--skip-step6-gate",
        "--skip-deploy-e2e-gate",
        *(extra or []),
    ]


def test_pack_default_preserves_repo_pyproject_version(tmp_path: Path) -> None:
    pyproj = _REPO_ROOT / "trainer_hightier" / "pyproject.toml"
    before_text = pyproj.read_text(encoding="utf-8")
    before_version = _read_pyproject_version(pyproj)

    model_src = tmp_path / "model_pyproj_default"
    snap_src = tmp_path / "snap_pyproj_default"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-pyproj-default.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-pyproj-default"
    build_deploy_package(_minimal_pack_argv(model_src=model_src, snap_src=snap_src, mapping=mapping, out=out))

    assert pyproj.read_text(encoding="utf-8") == before_text
    assert _read_pyproject_version(pyproj) == before_version


def test_pack_writes_deploy_contract_json(tmp_path: Path) -> None:
    model_src = tmp_path / "model_contract"
    snap_src = tmp_path / "snap_contract"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-contract.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-contract"
    build_deploy_package(_minimal_pack_argv(model_src=model_src, snap_src=snap_src, mapping=mapping, out=out))
    contract_path = out / "models" / DEPLOY_CONTRACT_FILENAME
    assert contract_path.is_file()
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "deploy_contract_v1"
    assert payload["flags"]["deploy_requires_clickhouse"] is True
    assert any(r["supplier_id"] == "bundle_static_artifact" for r in payload["requirements"])


def test_pack_bump_version_restores_repo_pyproject(tmp_path: Path) -> None:
    pyproj = _REPO_ROOT / "trainer_hightier" / "pyproject.toml"
    before_text = pyproj.read_text(encoding="utf-8")
    before_version = _read_pyproject_version(pyproj)
    bumped_version = _bump_patch_version(before_version)

    model_src = tmp_path / "model_pyproj_bump"
    snap_src = tmp_path / "snap_pyproj_bump"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-pyproj-bump.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-pyproj-bump"
    build_deploy_package(
        _minimal_pack_argv(
            model_src=model_src,
            snap_src=snap_src,
            mapping=mapping,
            out=out,
            extra=["--bump-version"],
        )
    )

    assert pyproj.read_text(encoding="utf-8") == before_text
    assert _read_pyproject_version(pyproj) == before_version
    whls = list((out / "wheels").glob(f"trainer_hightier-{bumped_version}-*.whl"))
    assert whls, f"expected wheel with transient version {bumped_version}"


_PARQUET_MANIFEST_SUFFIX = "_parquet"


def _assert_feast_only_bundle(bundle_root: Path) -> dict[str, Any]:
    """Assert Feast-only bundle contract: no snapshot artifacts, metadata-only manifest."""
    art_dir = bundle_root / "snapshots" / "artifacts"
    if art_dir.is_dir():
        parquet_files = list(art_dir.glob("*.parquet"))
        assert not parquet_files, f"unexpected snapshot artifacts: {parquet_files}"
    payload = json.loads((bundle_root / "snapshots" / "active_manifest.json").read_text(encoding="utf-8"))
    parquet_keys = [k for k in payload if k.endswith(_PARQUET_MANIFEST_SUFFIX) or k.endswith("_parquet")]
    assert not parquet_keys, f"manifest must be metadata-only; found parquet keys={parquet_keys}"
    allow = bundle_root / "mapping" / "adt_allowed_players_q0p99.parquet"
    assert allow.is_file(), f"missing fixed allowlist at {allow}"
    return payload


def _write_minimal_model_bundle(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    clf = DummyClassifier(strategy="constant", constant=0)
    clf.fit([[0, 0, 0]] * 5, [0] * 5)
    payload = {
        "model": clf,
        "feature_columns": ["a", "b", "c"],
        "threshold": 0.5,
        "categorical_columns": [],
        "category_categories": {},
    }
    (dest / "model.pkl").write_bytes(pickle.dumps(payload))
    (dest / "model_version").write_text("test-ver\n", encoding="utf-8")


# Minimal frozen registry aligning with DummyClassifier pickle feature_columns=["a","b","c"]:
# baseline_model → a,b; feast_slow_180d → b (model col b); baseline_model → c
_REGISTRY_SNAPSHOT_BODY_ABC = """
registry_version: "pack-test-registry-v1"
updated_at: "2026-01-01"
features:
  - feature_id: "a"
    group_id: "group_pack_test"
    source: "baseline_model"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: none
  - feature_id: "b"
    group_id: "group_pack_test"
    source: "feast_slow_180d"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: long_term
    max_lookback: P180D
  - feature_id: "c"
    group_id: "group_pack_test"
    source: "baseline_model"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: none
"""

_REGISTRY_SNAPSHOT_BODY_WITH_FE = """
registry_version: "pack-test-registry-fe-v1"
updated_at: "2026-01-01"
features:
  - feature_id: "a"
    group_id: "group_pack_test"
    source: "baseline_model"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: none
  - feature_id: "b"
    group_id: "group_pack_test"
    source: "feast_slow_180d"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: long_term
    max_lookback: P180D
  - feature_id: "c"
    group_id: "group_pack_test"
    source: "fe_derived"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: short_term
    max_lookback: PT15M
"""


def _write_step6_deploy_e2e_pass_fixture(model_bundle: Path) -> None:
    """Write a minimal passing deploy E2E report for strict pack gate tests."""
    payload = {
        "schema_version": "deploy_e2e_gate_v1",
        "verdict": "pass",
        "failure_reason": None,
        "steps": [{"name": "startup_refresh", "ok": True}],
    }
    (Path(model_bundle) / DEPLOY_E2E_GATE_REPORT_FILENAME).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_step6_parity_pass_fixture(model_bundle: Path) -> None:
    """Write minimal passing Step 6 parity + deploy E2E reports for pack gate tests."""
    payload = {
        "schema_version": "feature_parity_verification_v2",
        "n_failed_slow_gate": 0,
        "n_failed_all_feature_gate": 0,
        "parity_gate": {
            "hard_fail_slow_gate": True,
            "hard_fail_all_feature_gate": False,
        },
        "models": [
            {
                "slow_gate": {"verdict": "pass"},
                "all_feature_gate": {"verdict": "pass"},
            },
        ],
    }
    (Path(model_bundle) / "feature_parity_verification.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    _write_step6_deploy_e2e_pass_fixture(model_bundle)


def _write_frozen_registry_abc_fixture(
    model_bundle: Path,
    metrics_extras: dict | None = None,
    *,
    with_fe_derived: bool = False,
) -> None:
    snap = Path(model_bundle) / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    body_yaml = _REGISTRY_SNAPSHOT_BODY_WITH_FE if with_fe_derived else _REGISTRY_SNAPSHOT_BODY_ABC
    snap.write_text(body_yaml.strip() + "\n", encoding="utf-8")
    body = dict(metrics_extras or {})
    body["feature_candidate_registry_sha256"] = sha256_file(snap)
    (model_bundle / "training_metrics.json").write_text(json.dumps(body), encoding="utf-8")
    _write_step6_parity_pass_fixture(model_bundle)


def _write_fe_derived_fixture(path: Path, *, include_registry_feat: bool = True) -> None:
    """Minimal bet-grain fe parquet aligned with ``_REGISTRY_SNAPSHOT_BODY_ABC`` feature ``c``."""

    data: dict[str, object] = {"bet_id": [101.0, 102.0]}
    if include_registry_feat:
        data["c"] = [0.1, 0.2]
    pd.DataFrame(data).to_parquet(path, index=False)


def _manifest_abc_layers(
    *,
    slow: Path,
    allow: Path,
    fe: Path | None,
    fe_short: Path | None = None,
    mid_term: Path | None = None,
    version: str = "mv",
) -> dict:
    man = {
        "version": version,
        "slow_patron_parquet": str(slow.resolve()),
        "adt_allowlist_parquet": str(allow.resolve()),
        "coverage_end_exclusive": datetime.now(timezone.utc).isoformat(),
    }
    short_path = fe_short if fe_short is not None else fe
    if short_path is not None:
        man["fe_short_term_parquet"] = str(short_path.resolve())
        man["fe_derived_source_kind"] = "production_clickhouse"
    if fe is not None:
        man["fe_derived_parquet"] = str(fe.resolve())
        man.setdefault("fe_derived_source_kind", "production_clickhouse")
    if mid_term is not None:
        from trainer_hightier.config import MID_TERM_GRAIN_CANONICAL_DAILY_ASOF

        man["mid_term_snapshot_parquet"] = str(mid_term.resolve())
        man["mid_term_grain"] = MID_TERM_GRAIN_CANONICAL_DAILY_ASOF
    return man


def _write_mid_term_production_fixture(path: Path, *, anchor: date | None = None) -> None:
    from trainer_hightier.config import MID_TERM_SNAPSHOT_SCOPE_PRODUCTION
    from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
        MID_TERM_SNAPSHOT_OUTPUT_COLUMNS,
    )
    from trainer_hightier.serving.snapshot_freshness import expected_mid_term_anchor

    anchor_day = anchor or expected_mid_term_anchor(date.today())
    row: dict[str, object] = {
        "canonical_id": ["c1"],
        "anchor_gaming_day_event": [anchor_day.isoformat()],
    }
    for col in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS:
        if col.startswith("fe__"):
            row[col] = [1.0]
    pd.DataFrame(row).to_parquet(path, index=False)
    meta = path.parent / f"{path.stem}.meta.json"
    meta.write_text(
        json.dumps({"snapshot_scope": MID_TERM_SNAPSHOT_SCOPE_PRODUCTION}),
        encoding="utf-8",
    )


def _write_slow_bet_fixture(path: Path, *, include_registry_slow_feat: bool = True) -> None:
    """Feast-aligned slow parquet: one row per bet_id (trainer materializer contract)."""

    pit = pd.to_datetime(
        pd.Series(["2025-01-15 10:00:00+08:00", "2025-02-15 11:30:00+08:00"]),
        errors="coerce",
    ).dt.tz_convert("UTC")
    syn = pit
    d: dict[str, object] = {
        "bet_id": [101.0, 102.0],
        "prediction_visible_ts_cf": pit,
        "__etl_insert_Dtm_synthetic": syn,
        "patron__theo_win_sum__w180d_m1snap": [10.0, 20.0],
        "patron__gaming_days_cnt__w180d_m1snap": [2, 3],
        "patron__adt__w180d_m1snap": [10.0, 20.0],
    }
    if include_registry_slow_feat:
        d["b"] = [0.1, 0.2]
    pd.DataFrame(d).to_parquet(path, index=False)


def _write_slow_player_fixture(
    path: Path,
    *,
    anchor_kind: str = "gaming_day_event",
    include_registry_slow_feat: bool = True,
) -> None:
    """Legacy player-grain snapshot for scorer ASOF path (tests / older parquet)."""

    data: dict[str, list[float | int]] = {"player_id": [1, 2]}
    if anchor_kind == "gaming_day_event":
        data["gaming_day_event"] = [20250101, 20250201]
    elif anchor_kind == "anchor_gaming_day_event":
        data["anchor_gaming_day_event"] = [20250101, 20250201]
    else:
        raise ValueError(f"unknown anchor_kind: {anchor_kind!r}")
    if include_registry_slow_feat:
        data["b"] = [0.1, 0.2]
    pd.DataFrame(data).to_parquet(path, index=False)


def _write_parquet(path: Path) -> None:
    """Generic layer/mapping parquet with structural keys used by packaging gates."""

    pd.DataFrame({"player_id": [1, 2], "canonical_id": [901, 902]}).to_parquet(path, index=False)


def test_strict_missing_deploy_e2e_report_fails(tmp_path: Path) -> None:
    """Strict pack requires passing deploy E2E report beside the model bundle."""

    model_src = tmp_path / "model_no_e2e"
    snap_src = tmp_path / "snap_no_e2e"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    _write_step6_parity_pass_fixture(model_src)
    (model_src / DEPLOY_E2E_GATE_REPORT_FILENAME).unlink(missing_ok=True)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-no-e2e.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-no-e2e"
    with pytest.raises(FileNotFoundError, match=DEPLOY_E2E_GATE_REPORT_FILENAME):
        build_deploy_package(
            [
                "--model-source",
                str(model_src),
                "--snapshot-manifest-source",
                str(snap_src),
                "--mapping-source",
                str(mapping),
                "--output-dir",
                str(out),
            ],
        )


def test_strict_deploy_e2e_fail_verdict_raises(tmp_path: Path) -> None:
    model_src = tmp_path / "model_e2e_fail"
    snap_src = tmp_path / "snap_e2e_fail"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    _write_step6_parity_pass_fixture(model_src)
    (model_src / DEPLOY_E2E_GATE_REPORT_FILENAME).write_text(
        json.dumps({"schema_version": "deploy_e2e_gate_v1", "verdict": "fail"}),
        encoding="utf-8",
    )
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-e2e-fail.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-e2e-fail"
    with pytest.raises(ValueError, match="deploy E2E gate failed"):
        build_deploy_package(
            [
                "--model-source",
                str(model_src),
                "--snapshot-manifest-source",
                str(snap_src),
                "--mapping-source",
                str(mapping),
                "--output-dir",
                str(out),
            ],
        )


def test_strict_missing_step6_parity_fails(tmp_path: Path) -> None:
    """Strict pack requires passing Step 6 parity artifact beside the model bundle."""

    model_src = tmp_path / "model_no_step6"
    snap_src = tmp_path / "snap_no_step6"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    (model_src / "feature_parity_verification.json").unlink(missing_ok=True)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-no-step6.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-no-step6"
    with pytest.raises(FileNotFoundError, match=r"feature_parity_verification\.json"):
        build_deploy_package(
            [
                "--model-source",
                str(model_src),
                "--snapshot-manifest-source",
                str(snap_src),
                "--mapping-source",
                str(mapping),
                "--output-dir",
                str(out),
            ]
        )


def test_overwrite_repacks_non_empty_output_dir(tmp_path: Path) -> None:
    """Default pack replaces an existing non-empty output directory."""

    model_src = tmp_path / "model_repack"
    snap_src = tmp_path / "snap_repack"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    (model_src / "feature_parity_verification.json").unlink(missing_ok=True)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-repack.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-repack"
    base_argv = [
        "--model-source",
        str(model_src),
        "--snapshot-manifest-source",
        str(snap_src),
        "--mapping-source",
        str(mapping),
        "--output-dir",
        str(out),
        "--skip-step6-gate",
        "--skip-deploy-e2e-gate",
    ]
    build_deploy_package(base_argv)
    (out / "stale_marker.txt").write_text("old", encoding="utf-8")
    build_deploy_package(base_argv)
    assert (out / "models" / "model.pkl").is_file()
    assert not (out / "stale_marker.txt").exists()


def test_no_overwrite_refuses_non_empty_output_dir(tmp_path: Path) -> None:
    model_src = tmp_path / "model_no_overwrite"
    snap_src = tmp_path / "snap_no_overwrite"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    (model_src / "feature_parity_verification.json").unlink(missing_ok=True)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-no-overwrite.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-no-overwrite"
    argv = [
        "--model-source",
        str(model_src),
        "--snapshot-manifest-source",
        str(snap_src),
        "--mapping-source",
        str(mapping),
        "--output-dir",
        str(out),
        "--skip-step6-gate",
        "--skip-deploy-e2e-gate",
    ]
    build_deploy_package(argv)
    with pytest.raises(FileExistsError, match="output dir must be empty or absent"):
        build_deploy_package([*argv, "--no-overwrite"])


def test_skip_step6_gate_allows_missing_parity(tmp_path: Path) -> None:
    model_src = tmp_path / "model_skip_step6"
    snap_src = tmp_path / "snap_skip_step6"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    (model_src / "feature_parity_verification.json").unlink(missing_ok=True)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-skip-step6.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-skip-step6"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
            "--skip-step6-gate",
            "--skip-deploy-e2e-gate",
        ]
    )
    assert (out / "models" / "model.pkl").is_file()


def test_schema_registry_sha256_mismatch_fails(tmp_path: Path) -> None:
    """training_metrics.registry sha must match frozen snapshot bytes."""

    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    snap_path = Path(model_src) / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    snap_path.write_text(_REGISTRY_SNAPSHOT_BODY_ABC.strip() + "\n", encoding="utf-8")
    (model_src / "training_metrics.json").write_text(
        json.dumps({"feature_candidate_registry_sha256": "0" * 64}),
        encoding="utf-8",
    )
    _write_step6_parity_pass_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-hash.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bad-reg-hash"
    with pytest.raises(ValueError, match=r"feature registry SHA mismatch"):
        build_deploy_package(
            ["--model-source", str(model_src), "--snapshot-manifest-source", str(snap_src),
             "--mapping-source", str(mapping), "--output-dir", str(out)]
        )


def test_strict_missing_frozen_registry_snapshot_fails(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    (model_src / "training_metrics.json").write_text("{}", encoding="utf-8")
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-noreg.parquet"
    _write_parquet(mapping)
    out = tmp_path / "no-reg-snapshot"
    with pytest.raises(FileNotFoundError, match=r"feature_candidate_registry\.snapshot\.yaml"):
        build_deploy_package(
            ["--model-source", str(model_src), "--snapshot-manifest-source", str(snap_src),
             "--mapping-source", str(mapping), "--output-dir", str(out),
             "--skip-step6-gate", "--skip-deploy-e2e-gate"]
        )


def test_no_strict_skips_gate_without_snapshot(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in_ns"
    snap_src = tmp_path / "snap_in_ns"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    (model_src / "training_metrics.json").write_text("{}", encoding="utf-8")
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-ns.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-no-strict-no-snap"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
            "--no-strict",
        ]
    )
    assert (out / "models" / "model.pkl").is_file()


def test_static_slow_missing_anchor_skipped_in_feast_only_bundle(tmp_path: Path) -> None:
    """Feast-only pack skips slow parquet structural gate (mid/long served via Feast online)."""

    model_src = tmp_path / "model_in_sa"
    snap_src = tmp_path / "snap_in_sa"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    pd.DataFrame({"player_id": [1, 2], "b": [0.0, 0.0]}).to_parquet(slow, index=False)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-slow-anchor.parquet"
    _write_parquet(mapping)
    out = tmp_path / "slow-no-anchor-feast-only"
    build_deploy_package(
        ["--model-source", str(model_src), "--snapshot-manifest-source", str(snap_src),
         "--mapping-source", str(mapping), "--output-dir", str(out)]
    )
    _assert_feast_only_bundle(out)


def test_dynamic_slow_missing_model_column_skipped_in_feast_only(tmp_path: Path) -> None:
    """feast_slow_180d Parquet gate is skipped when packing Feast-only bundle."""

    model_src = tmp_path / "model_in_dyn"
    snap_src = tmp_path / "snap_in_dyn"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow, include_registry_slow_feat=False)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-dyn.parquet"
    _write_parquet(mapping)
    out = tmp_path / "dyn-missing-b-feast-only"
    build_deploy_package(
        ["--model-source", str(model_src), "--snapshot-manifest-source", str(snap_src),
         "--mapping-source", str(mapping), "--output-dir", str(out)]
    )
    _assert_feast_only_bundle(out)


def test_static_slow_feast_missing_etl_skipped_in_feast_only(tmp_path: Path) -> None:
    """Bet-grain slow ETL column gate skipped for Feast-only production bundle."""

    model_src = tmp_path / "model_in_etl"
    snap_src = tmp_path / "snap_in_etl"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    pit = pd.to_datetime(pd.Series(["2025-01-01T00:00:00Z"]), utc=True)
    pd.DataFrame(
        {
            "bet_id": [1.0],
            "prediction_visible_ts_cf": pit,
            "patron__adt__w180d_m1snap": [0.42],
        },
    ).to_parquet(slow, index=False)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-etl.parquet"
    _write_parquet(mapping)
    out = tmp_path / "slow-no-etl-feast-only"
    build_deploy_package(
        ["--model-source", str(model_src), "--snapshot-manifest-source", str(snap_src),
         "--mapping-source", str(mapping), "--output-dir", str(out)]
    )
    _assert_feast_only_bundle(out)


def test_build_bundle_accepts_anchor_gaming_day_event_slow(tmp_path: Path) -> None:
    """Static gate accepts anchor_gaming_day_event as slow ASOF anchor."""

    model_src = tmp_path / "model_in_agd"
    snap_src = tmp_path / "snap_in_agd"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_player_fixture(slow, anchor_kind="anchor_gaming_day_event")
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-agd.parquet"
    _write_parquet(mapping)
    out = tmp_path / "ok-anchor-gaming-day"
    build_deploy_package(
        ["--model-source", str(model_src), "--snapshot-manifest-source", str(snap_src),
         "--mapping-source", str(mapping), "--output-dir", str(out)]
    )
    _assert_feast_only_bundle(out)


def test_static_canonical_mapping_missing_canonical_id_fails(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in_map"
    snap_src = tmp_path / "snap_in_map"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-bad-structure.parquet"
    pd.DataFrame({"player_id": [1, 2]}).to_parquet(mapping, index=False)
    out = tmp_path / "bad-map"
    with pytest.raises(ValueError, match=r"canonical_mapping.*canonical_id"):
        build_deploy_package(
            [
                "--model-source",
                str(model_src),
                "--snapshot-manifest-source",
                str(snap_src),
                "--mapping-source",
                str(mapping),
                "--output-dir",
                str(out),
            ]
        )


def test_static_allowlist_missing_player_id_fails(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in_al"
    snap_src = tmp_path / "snap_in_al"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    pd.DataFrame({"canonical_id": [1, 2]}).to_parquet(allow, index=False)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow.resolve()), "adt_allowlist_parquet": str(allow.resolve())}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-al.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bad-allowlist"
    with pytest.raises(ValueError, match=r"adt_allowlist.*player_id"):
        build_deploy_package(
            [
                "--model-source",
                str(model_src),
                "--snapshot-manifest-source",
                str(snap_src),
                "--mapping-source",
                str(mapping),
                "--output-dir",
                str(out),
            ]
        )


def test_package_allows_refresh_required_fe_suppliers(tmp_path: Path) -> None:
    """Package-time gate permits production-refreshable fe suppliers to be absent."""

    model_src = tmp_path / "model_fe_miss"
    snap_src = tmp_path / "snap_fe_miss"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src, with_fe_derived=True)
    man = _manifest_abc_layers(slow=slow, allow=allow, fe=None)
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-fe-miss.parquet"
    _write_parquet(mapping)
    out = tmp_path / "fe-supply-miss"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
        ]
    )
    _assert_feast_only_bundle(out)


def test_feast_only_bundle_has_no_snapshot_artifact_parquets(tmp_path: Path) -> None:
    """Feast-only pack must not copy snapshot feature parquet layers into the bundle."""

    model_src = tmp_path / "model_cadence_layers"
    snap_src = tmp_path / "snap_cadence_layers"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    fe_short = art / "fe_short.parquet"
    mid = art / "mid.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_fe_derived_fixture(fe_short)
    _write_mid_term_production_fixture(mid)
    _write_minimal_model_bundle(model_src)
    reg = Path(model_src) / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    reg.write_text(
        _REGISTRY_SNAPSHOT_BODY_WITH_FE.replace("short_term", "mid_term", 1)
        .replace("PT15M", "P1D", 1)
        .strip()
        + "\n",
        encoding="utf-8",
    )
    (model_src / "training_metrics.json").write_text(
        json.dumps({"feature_candidate_registry_sha256": sha256_file(reg)}),
        encoding="utf-8",
    )
    _write_step6_parity_pass_fixture(model_src)
    man = _manifest_abc_layers(
        slow=slow,
        allow=allow,
        fe=None,
        fe_short=fe_short,
        mid_term=mid,
    )
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-cadence-layers.parquet"
    _write_parquet(mapping)
    out = tmp_path / "cadence-layers-pack"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
        ]
    )
    payload = _assert_feast_only_bundle(out)
    assert payload["coverage_end_exclusive"] == man["coverage_end_exclusive"]
    assert "fe_short_term_parquet" not in payload
    assert "mid_term_snapshot_parquet" not in payload


def test_feature_supplyability_feast_only_metadata_manifest(tmp_path: Path) -> None:
    model_src = tmp_path / "model_fe_ok"
    snap_src = tmp_path / "snap_fe_ok"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    fe = art / "fe_derived.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_fe_derived_fixture(fe)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src, with_fe_derived=True)
    man = _manifest_abc_layers(slow=slow, allow=allow, fe=fe, fe_short=fe)
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-fe-ok.parquet"
    _write_parquet(mapping)
    out = tmp_path / "fe-supply-ok"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
        ]
    )
    payload = _assert_feast_only_bundle(out)
    assert payload["version"] == "mv"


def test_trial_parquet_dynamic_gate_when_present(tmp_path: Path) -> None:
    """feast_trial_1h feature must exist in bundled trial parquet when layer is packaged."""

    model_src = tmp_path / "model_trial_gate"
    snap_src = tmp_path / "snap_trial"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    trial = art / "trial.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    clf = DummyClassifier(strategy="constant", constant=0)
    clf.fit([[0, 0, 0, 0]] * 5, [0] * 5)
    payload = {
        "model": clf,
        "feature_columns": ["a", "b", "c", "trial_feat"],
        "threshold": 0.5,
        "categorical_columns": [],
        "category_categories": {},
    }
    (model_src / "model.pkl").write_bytes(pickle.dumps(payload))

    yaml_body = """
registry_version: "trial-gate-test"
updated_at: "2026-01-01"
features:
  - feature_id: "a"
    group_id: "g"
    source: "baseline_model"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: none
  - feature_id: "b"
    group_id: "g"
    source: "feast_slow_180d"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: long_term
    max_lookback: P180D
  - feature_id: "c"
    group_id: "g"
    source: "baseline_model"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: none
  - feature_id: "trial_feat"
    group_id: "g"
    source: "feast_trial_1h"
    status: "active"
    enabled_for: ["baseline"]
    time_horizon: short_term
    max_lookback: PT1H
"""
    snap_p = Path(model_src) / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    snap_p.write_text(yaml_body.strip() + "\n", encoding="utf-8")
    metrics = {"feature_candidate_registry_sha256": sha256_file(snap_p)}
    (model_src / "training_metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    _write_step6_parity_pass_fixture(model_src)

    pd.DataFrame({"player_id": [1], "trial_feat": [0.42]}).to_parquet(trial, index=False)
    man = {
        "version": "mv",
        "slow_patron_parquet": str(slow.resolve()),
        "adt_allowlist_parquet": str(allow.resolve()),
        "trial_bet_behavior_parquet": str(trial.resolve()),
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map-trial.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle-trial-ok"
    build_deploy_package(
        ["--model-source", str(model_src), "--snapshot-manifest-source", str(snap_src),
         "--mapping-source", str(mapping), "--output-dir", str(out)]
    )
    bio = json.loads((out / "bundle_info.json").read_text(encoding="utf-8"))
    assert bio.get("feature_candidate_registry_sha256") and len(bio["feature_candidate_registry_sha256"]) == 64
    _assert_feast_only_bundle(out)


def test_build_bundle_rewrites_manifest_relative_paths(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "staging"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {
        "version": "m1",
        "slow_patron_parquet": str(slow.resolve()),
        "adt_allowlist_parquet": str(allow.resolve()),
        "adt_allowlist_version": "v-al",
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")

    mapping = tmp_path / "map.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
        ]
    )
    payload = _assert_feast_only_bundle(out)
    assert payload["version"] == "m1"
    assert payload.get("adt_allowlist_version") == "v-al"
    assert (out / "bundle_info.json").is_file()
    assert (out / "deploy_bundle_paths.json").is_file()
    assert (out / "main.py").is_file()
    assert (out / ".env.example").is_file()
    assert (out / "requirements.txt").is_file()
    assert (out / "feast_repo" / "feature_store.yaml").is_file()
    assert (out / "artifacts" / "feast").is_dir()
    deploy_paths = json.loads((out / "deploy_bundle_paths.json").read_text(encoding="utf-8"))
    assert deploy_paths.get("schema_version") == 2
    assert deploy_paths.get("feast_repo_dir") == "feast_repo"
    assert deploy_paths.get("feast_readiness_path") == "artifacts/feast/feast_online_readiness.json"
    assert deploy_paths.get("adt_allowlist_parquet") == "mapping/adt_allowed_players_q0p99.parquet"
    assert (out / "mapping" / "adt_allowed_players_q0p99.parquet").is_file()
    feast_yaml = (out / "feast_repo" / "feature_store.yaml").read_text(encoding="utf-8")
    assert "artifacts/feast/duckdb_staging" in feast_yaml
    whls = list((out / "wheels").glob("trainer_hightier-*.whl"))
    assert whls, "expected trainer_hightier wheel under bundle wheels/"
    req_txt = (out / "requirements.txt").read_text(encoding="utf-8")
    assert whls[0].name in req_txt
    assert req_txt.strip().splitlines()[3] == f"wheels/{whls[0].name}"
    assert "file://" not in req_txt.lower()
    assert " @ " not in req_txt
    assert "six==" in req_txt


def test_pip_freeze_package_name_handles_direct_url() -> None:
    """``pip freeze`` after ``pip install /abs/path.whl`` uses ``@ file://``, not ``==``."""
    line = (
        "trainer-hightier @ file:///C:/Users/longp/Patron_Walkaway/out/deploy/wheels/"
        "trainer_hightier-0.2.8-py3-none-any.whl#sha256=abc"
    )
    assert _pip_freeze_package_name(line) == "trainer-hightier"
    assert _pip_freeze_package_name("Flask==3.1.3") == "flask"
    assert _pip_freeze_package_name("ibis-framework[duckdb]==11.0.0") == "ibis-framework"


def test_build_bundle_archive_zip(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {
        "version": "mv",
        "slow_patron_parquet": str(slow),
        "adt_allowlist_parquet": str(allow),
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map2.parquet"
    _write_parquet(mapping)
    out = tmp_path / "outzip"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
            "--archive",
        ]
    )
    z = tmp_path / "outzip.zip"
    assert z.is_file()
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
    assert "bundle_info.json" in names
    assert "snapshots/active_manifest.json" in names
    assert "main.py" in names
    assert "requirements.txt" in names
    assert ".env.example" in names
    wheel_members = [n for n in names if n.startswith("wheels/") and n.endswith(".whl")]
    assert wheel_members


def test_allowlist_hash_mismatch_fails(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    bad_sha = "a" * 64
    _write_frozen_registry_abc_fixture(model_src, metrics_extras={"adt_allowlist_sha256": bad_sha})
    man = {
        "version": "mv",
        "slow_patron_parquet": str(slow),
        "adt_allowlist_parquet": str(allow),
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map3.parquet"
    _write_parquet(mapping)
    out = tmp_path / "badbundle"
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        build_deploy_package(
            [
                "--model-source",
                str(model_src),
                "--snapshot-manifest-source",
                str(snap_src),
                "--mapping-source",
                str(mapping),
                "--output-dir",
                str(out),
            ]
        )


def test_allowlist_hash_match_succeeds(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    h = sha256_file(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src, metrics_extras={"adt_allowlist_sha256": h})
    man = {
        "version": "mv",
        "slow_patron_parquet": str(slow),
        "adt_allowlist_parquet": str(allow),
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map4.parquet"
    _write_parquet(mapping)
    out = tmp_path / "goodbundle"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
        ]
    )
    assert (out / "models" / "training_metrics.json").is_file()


def test_deploy_api_health_after_bundle_config(tmp_path: Path) -> None:
    """Serving modules may load before override; reload so ``STATE_DB_PATH`` matches bundle."""
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {"version": "mv", "slow_patron_parquet": str(slow), "adt_allowlist_parquet": str(allow)}
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map5.parquet"
    _write_parquet(mapping)
    bundle = tmp_path / "healthbundle"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(bundle),
        ]
    )
    from trainer_hightier.config import set_hightier_serving_deploy_override
    from trainer_hightier.deploy.main import _load_rel_paths, _serving_config_for_bundle
    from trainer_hightier.serving import api_server as api_mod
    from trainer_hightier.serving import runtime_config as rc

    rel = _load_rel_paths(bundle)
    cfg = _serving_config_for_bundle(bundle, rel)
    try:
        set_hightier_serving_deploy_override(cfg)
        importlib.reload(rc)
        importlib.reload(api_mod)
        api_mod._init_state_and_prediction_log_dbs()
        client = api_mod.app.test_client()
        rv = client.get("/health")
        assert rv.status_code == 200
    finally:
        set_hightier_serving_deploy_override(None)
        importlib.reload(rc)
        importlib.reload(api_mod)


def test_strict_trial_declared_but_missing_succeeds_feast_only(tmp_path: Path) -> None:
    """Feast-only pack ignores missing trial parquet even when declared in source manifest."""

    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    missing_trial = (art / "nope.parquet").resolve()
    man = {
        "version": "mv",
        "slow_patron_parquet": str(slow.resolve()),
        "adt_allowlist_parquet": str(allow.resolve()),
        "trial_bet_behavior_parquet": str(missing_trial),
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "maptrial.parquet"
    _write_parquet(mapping)
    out = tmp_path / "trial-missing-feast-only"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
        ]
    )
    _assert_feast_only_bundle(out)


def test_model_version_defaults_and_fingerprint_repeatable(monkeypatch, tmp_path: Path) -> None:
    """``--model-version`` with default snapshot/mapping/output roots (via monkeypatch)."""

    from dataclasses import replace

    import trainer_hightier.config as hcfg
    from trainer_hightier.config import default_hightier_serving_config

    vid = "test-run-mv-defaults"
    versions_root = tmp_path / "models_mvp_root"
    deploy_root = tmp_path / "deploy_root"
    bundle_dir = versions_root / vid
    bundle_dir.mkdir(parents=True)
    _write_minimal_model_bundle(bundle_dir)
    (bundle_dir / "model_version").write_text(vid + "\n", encoding="utf-8")
    _write_frozen_registry_abc_fixture(bundle_dir)

    snap_dir = tmp_path / "serving_snapshots"
    art = snap_dir / "staging"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    man = {
        "version": "mv-def",
        "slow_patron_parquet": str(slow.resolve()),
        "adt_allowlist_parquet": str(allow.resolve()),
    }
    (snap_dir / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")

    map_pq = tmp_path / "artifacts" / "mapping" / "canonical_player_mapping.parquet"
    map_pq.parent.mkdir(parents=True)
    _write_parquet(map_pq)

    monkeypatch.setattr(hcfg, "DEFAULT_MODEL_DIR", versions_root)
    monkeypatch.setattr(hcfg, "DEFAULT_DEPLOY_OUTPUT_ROOT", deploy_root)

    def _fake_serving_cfg():
        return replace(default_hightier_serving_config(), snapshot_manifest_dir=snap_dir)

    monkeypatch.setattr(
        "trainer_hightier.build_deploy_package.default_hightier_serving_config",
        _fake_serving_cfg,
    )
    monkeypatch.setattr(
        "trainer_hightier.build_deploy_package.default_canonical_mapping_parquet_path",
        lambda: map_pq,
    )

    out1 = build_deploy_package(["--model-version", vid])
    assert out1 == deploy_root / vid
    bio1 = json.loads((out1 / "bundle_info.json").read_text(encoding="utf-8"))
    fp1 = bio1.get("frozen_fingerprint_sha256")
    assert isinstance(fp1, str) and len(fp1) == 64

    out2_parent = deploy_root / f"{vid}-b2"
    out2_parent.mkdir(parents=True)
    out2 = out2_parent / "bundle2"
    build_deploy_package(["--model-version", vid, "--output-dir", str(out2)])
    bio2 = json.loads((out2 / "bundle_info.json").read_text(encoding="utf-8"))
    assert bio2["frozen_fingerprint_sha256"] == fp1


def test_deploy_inputs_autodiscovery_model_source_only(tmp_path: Path) -> None:
    """Frozen ``deploy_inputs/`` beside bundle lets packager omit snapshot/mapping paths."""

    model_src = tmp_path / "self_contained_bundle"
    di = model_src / "deploy_inputs"
    di.mkdir(parents=True)
    slow_name = "slow_patron_180d_monthly.parquet"
    allow_name = "adt_allowed_players_q0p99.parquet"
    slow = di / slow_name
    allow_f = di / allow_name
    _write_slow_bet_fixture(slow)
    _write_parquet(allow_f)
    cmap = di / "canonical_player_mapping.parquet"
    _write_parquet(cmap)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {
        "version": "frozen-di",
        "slow_patron_parquet": slow_name,
        "adt_allowlist_parquet": allow_name,
        "adt_allowlist_version": "x",
    }
    (di / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")

    out = tmp_path / "from_deploy_inputs_only"
    build_deploy_package(["--model-source", str(model_src), "--output-dir", str(out)])
    payload = _assert_feast_only_bundle(out)
    assert payload["version"] == "frozen-di"
    copied_map = out / "mapping" / cmap.name
    assert copied_map.is_file()


def test_deploy_inputs_mid_term_manifest_metadata_only(tmp_path: Path) -> None:
    """Production mid_term inputs are not copied; metadata-only manifest retains audit fields."""

    from trainer_hightier.config import MID_TERM_GRAIN_CANONICAL_DAILY_ASOF

    model_src = tmp_path / "self_contained_fe_bundle"
    di = model_src / "deploy_inputs"
    di.mkdir(parents=True)
    slow_name = "slow_patron_180d_monthly.parquet"
    allow_name = "adt_allowed_players_q0p99.parquet"
    fe_name = "fe_short_term_features.parquet"
    mid_name = "mid_term_daily_snapshot.parquet"
    slow = di / slow_name
    allow_f = di / allow_name
    fe = di / fe_name
    mid = di / mid_name
    _write_slow_bet_fixture(slow)
    _write_parquet(allow_f)
    _write_fe_derived_fixture(fe)
    _write_mid_term_production_fixture(mid)
    cmap = di / "canonical_player_mapping.parquet"
    _write_parquet(cmap)
    _write_minimal_model_bundle(model_src)
    reg_body = (
        _REGISTRY_SNAPSHOT_BODY_WITH_FE.replace("short_term", "mid_term", 1)
        .replace("PT15M", "P1D", 1)
        .strip()
        + "\n"
    )
    snap_reg = model_src / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    snap_reg.write_text(reg_body, encoding="utf-8")
    (di / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME).write_text(reg_body, encoding="utf-8")
    (model_src / "training_metrics.json").write_text(
        json.dumps({"feature_candidate_registry_sha256": sha256_file(snap_reg)}),
        encoding="utf-8",
    )
    _write_step6_parity_pass_fixture(model_src)
    man = {
        "version": "frozen-di-fe",
        "slow_patron_parquet": slow_name,
        "fe_short_term_parquet": fe_name,
        "fe_derived_source_kind": "production_clickhouse",
        "mid_term_snapshot_parquet": mid_name,
        "mid_term_grain": MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
        "adt_allowlist_parquet": allow_name,
        "adt_allowlist_version": "x",
        "coverage_end_exclusive": datetime.now(timezone.utc).isoformat(),
        "training_cutoff_iso": "2026-05-19T00:00:00+00:00",
        "model_version": "frozen-di-fe",
    }
    (di / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")

    out = tmp_path / "from_deploy_inputs_fe"
    build_deploy_package(["--model-source", str(model_src), "--output-dir", str(out)])
    payload = _assert_feast_only_bundle(out)
    assert payload["coverage_end_exclusive"] == man["coverage_end_exclusive"]
    assert payload.get("training_cutoff_iso") == man["training_cutoff_iso"]
    assert payload.get("model_version") == man["model_version"]


def test_deploy_inputs_fallback_when_absent(monkeypatch, tmp_path: Path) -> None:
    """No ``deploy_inputs/`` → defaults to patched serving snapshot dir + canonical mapping."""

    from dataclasses import replace

    model_src = tmp_path / "no_di_bundle"
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)

    snap_dir = tmp_path / "legacy_serv_snap"
    art = snap_dir / "staging"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    man = {
        "version": "legacy",
        "slow_patron_parquet": str(slow.resolve()),
        "adt_allowlist_parquet": str(allow.resolve()),
    }
    (snap_dir / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")

    map_pq = tmp_path / "legacy_map" / "canonical_player_mapping.parquet"
    map_pq.parent.mkdir(parents=True)
    _write_parquet(map_pq)

    from trainer_hightier.config import default_hightier_serving_config

    def _fake_serving_cfg():
        return replace(default_hightier_serving_config(), snapshot_manifest_dir=snap_dir)

    monkeypatch.setattr(
        "trainer_hightier.build_deploy_package.default_hightier_serving_config",
        _fake_serving_cfg,
    )
    monkeypatch.setattr(
        "trainer_hightier.build_deploy_package.default_canonical_mapping_parquet_path",
        lambda: map_pq,
    )

    out = tmp_path / "legacy_fallback_out"
    build_deploy_package(["--model-source", str(model_src), "--output-dir", str(out)])
    assert (out / "bundle_info.json").is_file()


def test_archive_zip_file_list_matches_folder(tmp_path: Path) -> None:
    """Folder and zip archive must list the same relative paths (standalone bundle)."""
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {
        "version": "mv",
        "slow_patron_parquet": str(slow),
        "adt_allowlist_parquet": str(allow),
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map_zip_eq.parquet"
    _write_parquet(mapping)
    out = tmp_path / "fold_zip"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
            "--archive",
        ]
    )
    zpath = tmp_path / "fold_zip.zip"
    assert zpath.is_file()
    folder_files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    with zipfile.ZipFile(zpath) as zf:
        zip_files = set(zf.namelist())
    assert folder_files == zip_files


def test_feature_supply_uses_serving_registry_loader() -> None:
    """Deploy preflight path must not import ``trainer_hightier.feature_experiment``."""

    import importlib

    import trainer_hightier.serving.feature_supply as fs

    importlib.reload(fs)
    assert fs.load_candidate_registry.__module__ == "trainer_hightier.serving.candidate_registry_loader"


def test_phase_b_wheel_excludes_junk_and_includes_serving(tmp_path: Path) -> None:
    """Phase B: wheel must not ship ``build/lib`` pollution or training-only subtrees."""
    model_src = tmp_path / "model_in_pb"
    snap_src = tmp_path / "snap_in_pb"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {
        "version": "mv",
        "slow_patron_parquet": str(slow),
        "adt_allowlist_parquet": str(allow),
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map_pb.parquet"
    _write_parquet(mapping)
    out = tmp_path / "bundle_phase_b"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(out),
        ]
    )
    whls = sorted((out / "wheels").glob("trainer_hightier-*.whl"))
    assert len(whls) == 1, whls
    with zipfile.ZipFile(whls[0]) as zf:
        names = zf.namelist()
    assert not any("/build/lib/" in n for n in names), names[:20]
    assert any(n.startswith("trainer_hightier/feature_experiment/") for n in names)
    assert not any(n.startswith("trainer_hightier/tests/") for n in names)
    assert not any(n.startswith("trainer_hightier/feast_repo/") for n in names)
    assert any(n.endswith("trainer_hightier/utils/__init__.py") for n in names)
    assert any(n.endswith("trainer_hightier/serving/scorer.py") for n in names)
    assert any(n.endswith("trainer_hightier/serving/candidate_registry_loader.py") for n in names)
    assert any(n.endswith("trainer_hightier/serving/feature_supply.py") for n in names)
    assert any(n.endswith("trainer_hightier/deploy/main.py") for n in names)
    assert any(n.endswith("trainer_hightier/config.py") for n in names)
    assert any(n.endswith("trainer_hightier/contracts/feature_candidate_registry.yaml") for n in names)


@pytest.mark.slow
def test_no_repo_smoke_venv_pip_install_imports(tmp_path: Path) -> None:
    """End-to-end: venv + pip install -r requirements.txt can import trainer_hightier (needs PyPI)."""
    import subprocess
    import sys

    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_slow_bet_fixture(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    _write_frozen_registry_abc_fixture(model_src)
    man = {
        "version": "mv",
        "slow_patron_parquet": str(slow),
        "adt_allowlist_parquet": str(allow),
    }
    (snap_src / "active_manifest.json").write_text(json.dumps(man), encoding="utf-8")
    mapping = tmp_path / "map_smoke.parquet"
    _write_parquet(mapping)
    bundle = tmp_path / "smokebundle"
    build_deploy_package(
        [
            "--model-source",
            str(model_src),
            "--snapshot-manifest-source",
            str(snap_src),
            "--mapping-source",
            str(mapping),
            "--output-dir",
            str(bundle),
        ]
    )

    venv = tmp_path / "smoke_venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    if sys.platform == "win32":
        pip_exe = venv / "Scripts" / "pip.exe"
        py_exe = venv / "Scripts" / "python.exe"
    else:
        pip_exe = venv / "bin" / "pip"
        py_exe = venv / "bin" / "python"
    subprocess.run([str(pip_exe), "install", "-r", "requirements.txt"], check=True, cwd=str(bundle))
    subprocess.run(
        [
            str(py_exe),
            "-c",
            "import trainer_hightier.utils; import trainer_hightier.serving.scorer; import trainer_hightier.serving.prediction_log; import trainer_hightier.deploy.main",
        ],
        check=True,
    )

