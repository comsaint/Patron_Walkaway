"""Tests for ``trainer_hightier.build_deploy_package`` (portable bundle layout)."""

from __future__ import annotations

import importlib
import json
import pickle
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from trainer_hightier.build_deploy_package import build_deploy_package
from trainer_hightier.serving.adt_allowlist import sha256_file
from trainer_hightier.serving.feature_state_store import ActiveSnapshotManifest


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


def _write_parquet(path: Path) -> None:
    pd.DataFrame({"player_id": [1, 2]}).to_parquet(path, index=False)


def test_build_bundle_rewrites_manifest_relative_paths(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "staging"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_parquet(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    (model_src / "training_metrics.json").write_text("{}", encoding="utf-8")
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
    mpath = out / "snapshots" / "active_manifest.json"
    payload = json.loads(mpath.read_text(encoding="utf-8"))
    am = ActiveSnapshotManifest.from_dict(payload, manifest_dir=mpath.parent)
    assert am.slow_patron_parquet.is_file()
    assert am.adt_allowlist_parquet is not None and am.adt_allowlist_parquet.is_file()
    assert (out / "bundle_info.json").is_file()
    assert (out / "deploy_bundle_paths.json").is_file()


def test_build_bundle_archive_zip(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_parquet(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    (model_src / "training_metrics.json").write_text("{}", encoding="utf-8")
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


def test_allowlist_hash_mismatch_fails(tmp_path: Path) -> None:
    model_src = tmp_path / "model_in"
    snap_src = tmp_path / "snap_in"
    art = snap_src / "x"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_parquet(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    bad_sha = "a" * 64
    metrics = {"adt_allowlist_sha256": bad_sha}
    (model_src / "training_metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
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
    _write_parquet(slow)
    _write_parquet(allow)
    h = sha256_file(allow)
    _write_minimal_model_bundle(model_src)
    (model_src / "training_metrics.json").write_text(
        json.dumps({"adt_allowlist_sha256": h}),
        encoding="utf-8",
    )
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
    _write_parquet(slow)
    _write_parquet(allow)
    _write_minimal_model_bundle(model_src)
    (model_src / "training_metrics.json").write_text("{}", encoding="utf-8")
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
    from trainer_hightier.serving.state_db import init_state_db

    rel = _load_rel_paths(bundle)
    cfg = _serving_config_for_bundle(bundle, rel)
    try:
        set_hightier_serving_deploy_override(cfg)
        importlib.reload(rc)
        importlib.reload(api_mod)
        init_state_db(api_mod.STATE_DB_PATH)
        client = api_mod.app.test_client()
        rv = client.get("/health")
        assert rv.status_code == 200
    finally:
        set_hightier_serving_deploy_override(None)
        importlib.reload(rc)
        importlib.reload(api_mod)
