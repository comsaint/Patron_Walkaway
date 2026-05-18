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
    assert (out / "main.py").is_file()
    assert (out / ".env.example").is_file()
    assert (out / "requirements.txt").is_file()
    whls = list((out / "wheels").glob("trainer_hightier-*.whl"))
    assert whls, "expected trainer_hightier wheel under bundle wheels/"
    req_txt = (out / "requirements.txt").read_text(encoding="utf-8")
    assert whls[0].name in req_txt


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


def test_strict_trial_declared_but_missing_raises(tmp_path: Path) -> None:
    """Manifest declares trial_bet_behavior_parquet but source file absent -> strict fails."""

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
    out = tmp_path / "trialbad"
    with pytest.raises(FileNotFoundError):
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
    (bundle_dir / "training_metrics.json").write_text("{}", encoding="utf-8")

    snap_dir = tmp_path / "serving_snapshots"
    art = snap_dir / "staging"
    art.mkdir(parents=True)
    slow = art / "slow.parquet"
    allow = art / "allow.parquet"
    _write_parquet(slow)
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


def test_archive_zip_file_list_matches_folder(tmp_path: Path) -> None:
    """Folder and zip archive must list the same relative paths (standalone bundle)."""
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


def test_phase_b_wheel_excludes_junk_and_includes_serving(tmp_path: Path) -> None:
    """Phase B: wheel must not ship ``build/lib`` pollution or training-only subtrees."""
    model_src = tmp_path / "model_in_pb"
    snap_src = tmp_path / "snap_in_pb"
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
    assert not any(n.startswith("trainer_hightier/feature_experiment/") for n in names)
    assert not any(n.startswith("trainer_hightier/tests/") for n in names)
    assert not any(n.startswith("trainer_hightier/feast_repo/") for n in names)
    assert any(n.endswith("trainer_hightier/utils/__init__.py") for n in names)
    assert any(n.endswith("trainer_hightier/serving/scorer.py") for n in names)
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

