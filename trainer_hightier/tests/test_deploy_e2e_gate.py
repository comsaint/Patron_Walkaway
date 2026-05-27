"""Tests for production-like deploy E2E gate orchestration."""

from __future__ import annotations

import json
import pickle
import shutil
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from trainer_hightier.config import FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME, default_hightier_serving_config
from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.serving.deploy_e2e_gate import (
    REPORT_SCHEMA_VERSION,
    DeployE2EGateOptions,
    DeployE2EGateReport,
    GateStepResult,
    apply_default_scorability_gaming_days,
    bundle_venv_python,
    parse_gate_args,
    provision_bundle_venv,
    reset_bundle_feast_runtime,
    resolve_model_bundle_test_gaming_days,
    resolve_supplier_plan,
    run_deploy_e2e_gate,
    run_startup_refresh_gate,
    validate_bundle_contract,
    write_gate_report,
)
from trainer_hightier.serving.feature_supply import (
    assert_scorer_supplier_plan_or_raise,
    build_scorer_supplier_plan,
)
from trainer_hightier.tests.test_scorer_v2_feast import _write_min_registry


def _write_model_bundle(
    tmp_path: Path,
    *,
    include_mid: bool,
    include_slow: bool,
) -> Path:
    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg, include_mid=include_mid, include_slow=include_slow)
    snap = load_candidate_registry(reg)
    feats = ["wager", "player_id"]
    if include_mid:
        feats.append("fe__bets_cnt__w1d")
    if include_slow:
        feats.append("patron__adt__w180d_m1snap")
    plan = build_scorer_supplier_plan(snap, tuple(feats))
    assert_scorer_supplier_plan_or_raise(plan)
    model = DummyClassifier(strategy="constant", constant=1)
    model.fit([[0.0], [1.0]], [0, 1])
    payload = {
        "model": model,
        "feature_columns": list(
            plan.baseline_cols + plan.feast_mid_cols + plan.mid_composite_cols + plan.feast_slow_cols,
        ),
        "threshold": 0.99,
        "categorical_columns": [],
        "category_categories": {},
    }
    bundle_dir = tmp_path / "models"
    bundle_dir.mkdir()
    (bundle_dir / "model.pkl").write_bytes(pickle.dumps(payload))
    (bundle_dir / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME).write_text(
        reg.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (bundle_dir / "model_version").write_text("gate-test", encoding="utf-8")
    (bundle_dir / "split_report.json").write_text(
        json.dumps(
            {
                "splits": [
                    {"split": "test", "min_gaming_day": "2026-01-01", "max_gaming_day": "2026-01-07"},
                ],
            },
        ),
        encoding="utf-8",
    )
    return bundle_dir


def _deploy_layout(
    tmp_path: Path,
    *,
    include_mid: bool = True,
    include_slow: bool = True,
) -> Path:
    model_bundle = _write_model_bundle(
        tmp_path,
        include_mid=include_mid,
        include_slow=include_slow,
    )
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    feast_repo = deploy / "feast_repo"
    feast_repo.mkdir(parents=True)
    (feast_repo / "data").mkdir(parents=True, exist_ok=True)
    (feast_repo / "feature_store.yaml").write_text(
        "project: test\nprovider: local\nregistry: data/registry.db\n"
        "online_store:\n  type: sqlite\n  path: data/online_store.db\n",
        encoding="utf-8",
    )
    map_dest = deploy / "mapping" / "canonical_player_mapping.parquet"
    allow_dest = deploy / "mapping" / "adt_allowed.parquet"
    map_dest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"player_id": [1], "canonical_id": ["c1"]}).to_parquet(map_dest, index=False)
    pd.DataFrame({"player_id": [1]}).to_parquet(allow_dest, index=False)
    rel = {
        "model_bundle_dir": model_bundle.name,
        "canonical_mapping_parquet": "mapping/canonical_player_mapping.parquet",
        "adt_allowlist_parquet": "mapping/adt_allowed.parquet",
        "feast_repo_dir": "feast_repo",
        "local_state_dir": "local_state",
        "feast_artifacts_dir": "artifacts/feast",
        "snapshot_manifest_dir": "snapshots",
        "feast_readiness_path": "artifacts/feast/feast_online_readiness.json",
    }
    (deploy / "deploy_bundle_paths.json").write_text(json.dumps(rel), encoding="utf-8")
    shutil.copytree(model_bundle, deploy / model_bundle.name)
    (deploy / "local_state").mkdir(parents=True, exist_ok=True)
    (deploy / "artifacts" / "feast").mkdir(parents=True, exist_ok=True)
    return deploy


def test_resolve_model_bundle_test_gaming_days(tmp_path: Path) -> None:
    model_bundle = tmp_path / "models"
    model_bundle.mkdir()
    report = {
        "splits": [
            {"split": "train", "min_gaming_day": "2024-01-01", "max_gaming_day": "2025-01-01"},
            {"split": "test", "min_gaming_day": "2026-02-01", "max_gaming_day": "2026-05-11"},
        ],
    }
    (model_bundle / "split_report.json").write_text(json.dumps(report), encoding="utf-8")
    start, end = resolve_model_bundle_test_gaming_days(model_bundle)
    assert start.isoformat() == "2026-02-01"
    assert end.isoformat() == "2026-05-11"


def test_apply_default_scorability_gaming_days_from_split_report(tmp_path: Path) -> None:
    model_bundle = tmp_path / "models"
    model_bundle.mkdir()
    (model_bundle / "split_report.json").write_text(
        json.dumps(
            {
                "splits": [
                    {"split": "test", "min_gaming_day": "2025-10-01", "max_gaming_day": "2025-10-31"},
                ],
            },
        ),
        encoding="utf-8",
    )
    opts = DeployE2EGateOptions(
        bundle_dir=tmp_path,
        local_cleaned_bet=tmp_path / "bet",
        local_cleaned_session=tmp_path / "sess.parquet",
        output_json=None,
        gaming_day_start=None,
        gaming_day_end=None,
        gaming_day_source=None,
        max_bets=10,
        force_feast_refresh=True,
        reuse_readiness=False,
        strict_smoke=True,
        warn_only=False,
        provision_venv=False,
        recreate_venv=False,
        reset_feast_runtime=False,
        skip_venv_provision=True,
    )
    out = apply_default_scorability_gaming_days(opts, model_bundle)
    assert out.gaming_day_start is not None
    assert out.gaming_day_start.isoformat() == "2025-10-01"
    assert out.gaming_day_end is not None
    assert out.gaming_day_end.isoformat() == "2025-10-31"
    assert out.gaming_day_source == "split_report.json#test"


def test_parse_gate_args_defaults() -> None:
    opts = parse_gate_args(
        [
            "--bundle-dir",
            "/tmp/bundle",
            "--local-cleaned-bet",
            "/tmp/bet",
            "--local-cleaned-session",
            "/tmp/session.parquet",
        ],
    )
    assert opts.force_feast_refresh is True
    assert opts.strict_smoke is True
    assert opts.warn_only is False
    assert opts.provision_venv is True
    assert opts.recreate_venv is True
    assert opts.reset_feast_runtime is True


def test_validate_bundle_contract_ok(tmp_path: Path) -> None:
    deploy = _deploy_layout(tmp_path)
    contract = validate_bundle_contract(deploy)
    assert contract["model_bundle"].is_dir()
    assert contract["feast_repo"].is_dir()


def test_validate_bundle_contract_missing_deploy_paths(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="deploy_bundle_paths.json"):
        validate_bundle_contract(tmp_path / "missing")


def test_report_schema_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    report = DeployE2EGateReport(
        schema_version=REPORT_SCHEMA_VERSION,
        generated_at="2026-05-22T00:00:00+08:00",
        verdict="pass",
        bundle_dir=str(tmp_path),
        runtime={"trainer_hightier_file": "/x"},
        supplier_routes={"feast_online_slow": 1},
        steps=[GateStepResult(name="startup_refresh", ok=True, detail={"layers": ["slow"]})],
        artifact_paths={"feast_repo": "/tmp/feast_repo"},
    )
    write_gate_report(out, report)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == REPORT_SCHEMA_VERSION
    assert loaded["steps"][0]["name"] == "startup_refresh"


def test_resolve_refresh_layers_slow_only(tmp_path: Path) -> None:
    deploy = _deploy_layout(tmp_path, include_mid=False, include_slow=True)
    rel = json.loads((deploy / "deploy_bundle_paths.json").read_text(encoding="utf-8"))
    model_bundle = deploy / rel["model_bundle_dir"]
    plan = resolve_supplier_plan(model_bundle)
    assert plan.feast_slow_cols
    assert not plan.feast_mid_cols


def test_startup_refresh_registry_missing_runs_apply_before_materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: fresh bundle must feast apply before materialize (slow-only path)."""
    from trainer_hightier.deploy import main as deploy_main
    from trainer_hightier.serving import feast_online_refresh as refresh_mod

    deploy = _deploy_layout(tmp_path, include_mid=False, include_slow=True)
    rel = json.loads((deploy / "deploy_bundle_paths.json").read_text(encoding="utf-8"))
    cfg = deploy_main._serving_config_for_bundle(deploy, rel)
    cfg = replace(
        cfg,
        feature_state_db_path=deploy / "local_state" / "feature_state.db",
    )
    model_bundle = deploy / rel["model_bundle_dir"]
    mapping = deploy / rel["canonical_mapping_parquet"]
    allowlist = deploy / rel["adt_allowlist_parquet"]
    plan = resolve_supplier_plan(model_bundle)
    bet = tmp_path / "bet"
    bet.mkdir()
    sess = tmp_path / "session.parquet"
    pd.DataFrame({"player_id": [1], "gaming_day": ["2026-05-01"]}).to_parquet(sess, index=False)

    call_order: list[str] = []

    def _fake_apply(_repo: Path, **kwargs: object) -> float:
        call_order.append("apply")
        (deploy / "feast_repo" / "data" / "registry.db").write_bytes(b"ok")
        return 0.1

    def _fake_materialize(*_a: object, **_k: object) -> float:
        call_order.append("materialize")
        return 0.2

    def _fake_refresh(opts: refresh_mod.RefreshOptions) -> dict[str, object]:
        if opts.bootstrap_mid or opts.apply_schema or True:
            _fake_apply(opts.feast_repo)
        if not opts.skip_materialize:
            _fake_materialize()
        readiness = Path(opts.readiness_path)
        readiness.parent.mkdir(parents=True, exist_ok=True)
        readiness.write_text('{"schema_version":"test"}', encoding="utf-8")
        return {"verdict": "ok", "run_id": "test-run"}

    monkeypatch.setattr(refresh_mod, "run_feast_online_refresh", _fake_refresh)
    monkeypatch.setattr(deploy_main, "_try_acquire_feast_refresh_lock", lambda _cfg: 1)
    monkeypatch.setattr(deploy_main, "_release_feast_refresh_lock", lambda _cfg, _fd: None)

    step = run_startup_refresh_gate(
        bundle_root=deploy,
        rel=rel,
        cfg=cfg,
        plan=plan,
        mapping=mapping,
        allowlist=allowlist,
        local_cleaned_bet=bet,
        local_cleaned_session=sess,
        force_refresh=True,
    )
    assert step.ok, step.error
    assert call_order == ["apply", "materialize"]
    assert step.detail.get("layers") == ["slow"]


def test_run_deploy_e2e_gate_warn_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deploy = _deploy_layout(tmp_path, include_mid=False, include_slow=True)
    bet = tmp_path / "bet"
    bet.mkdir()
    sess = tmp_path / "session.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(sess, index=False)
    out = tmp_path / "report.json"

    monkeypatch.setattr(
        "trainer_hightier.serving.deploy_e2e_gate.run_startup_refresh_gate",
        lambda **_k: GateStepResult(name="startup_refresh", ok=False, error="simulated"),
    )

    opts = _gate_opts(deploy, bet, sess, output_json=out, warn_only=True)
    report = run_deploy_e2e_gate(opts)
    assert report.verdict == "warn"
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "warn"


def test_run_deploy_e2e_gate_fail_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deploy = _deploy_layout(tmp_path)
    bet = tmp_path / "bet"
    bet.mkdir()
    sess = tmp_path / "session.parquet"
    pd.DataFrame({"player_id": [1]}).to_parquet(sess, index=False)

    monkeypatch.setattr(
        "trainer_hightier.serving.deploy_e2e_gate.run_startup_refresh_gate",
        lambda **_k: GateStepResult(name="startup_refresh", ok=False, error="boom"),
    )

    opts = _gate_opts(deploy, bet, sess)
    report = run_deploy_e2e_gate(opts)
    assert report.verdict == "fail"
    assert report.failure_reason == "boom"
    assert any(s.name == "deploy_smoke" for s in report.steps) is False


def _gate_opts(
    deploy: Path,
    bet: Path,
    sess: Path,
    **kwargs: object,
) -> DeployE2EGateOptions:
    """Build gate options with venv provision disabled for fast unit tests."""
    base = {
        "bundle_dir": deploy,
        "local_cleaned_bet": bet,
        "local_cleaned_session": sess,
        "output_json": None,
        "gaming_day_start": None,
        "gaming_day_end": None,
        "gaming_day_source": None,
        "max_bets": 10,
        "force_feast_refresh": True,
        "reuse_readiness": False,
        "strict_smoke": True,
        "warn_only": False,
        "provision_venv": False,
        "recreate_venv": False,
        "reset_feast_runtime": False,
        "skip_venv_provision": True,
    }
    base.update(kwargs)
    return DeployE2EGateOptions(**base)


def test_reset_bundle_feast_runtime(tmp_path: Path) -> None:
    deploy = _deploy_layout(tmp_path, include_slow=True)
    rel = json.loads((deploy / "deploy_bundle_paths.json").read_text(encoding="utf-8"))
    (deploy / "feast_repo" / "data").mkdir(parents=True, exist_ok=True)
    reg = deploy / "feast_repo" / "data" / "registry.db"
    reg.write_bytes(b"old")
    readiness = deploy / "artifacts" / "feast" / "feast_online_readiness.json"
    readiness.write_text("{}", encoding="utf-8")
    meta = reset_bundle_feast_runtime(deploy, rel)
    assert not reg.is_file()
    assert not readiness.is_file()
    assert "feast_repo" in meta


def test_provision_bundle_venv_requires_requirements(tmp_path: Path) -> None:
    deploy = tmp_path / "empty_bundle"
    deploy.mkdir()
    step = provision_bundle_venv(deploy, recreate=True)
    assert not step.ok
    assert "requirements.txt" in (step.error or "")


def test_host_workspace_editable_target_from_pytest() -> None:
    from trainer_hightier.serving.deploy_e2e_gate import _host_workspace_editable_target

    target = _host_workspace_editable_target()
    assert target is not None
    assert (target / "pyproject.toml").is_file()


def test_run_cli_provisions_venv_and_reexecs_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent process provisions bundle venv then re-execs gate under bundle python."""
    import sys

    deploy = _deploy_layout(tmp_path)
    bet = tmp_path / "bet"
    bet.mkdir()
    sess = tmp_path / "session.parquet"
    pd.DataFrame({"player_id": [1], "gaming_day": ["2026-05-01"]}).to_parquet(sess, index=False)
    (deploy / "requirements.txt").write_text("trainer-hightier\n", encoding="utf-8")

    reexec_argv: list[str] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        reexec_argv.extend(cmd)
        return MagicMock(returncode=0)

    def _fake_provision(_root: Path, *, recreate: bool) -> GateStepResult:
        vpy = bundle_venv_python(deploy)
        vpy.parent.mkdir(parents=True, exist_ok=True)
        vpy.write_text("", encoding="utf-8")
        return GateStepResult(name="venv_provision", ok=True, detail={"recreate": recreate})

    monkeypatch.setattr(
        "trainer_hightier.serving.deploy_e2e_gate.running_in_bundle_venv",
        lambda _b: False,
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.deploy_e2e_gate.provision_bundle_venv",
        _fake_provision,
    )
    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    from trainer_hightier.serving.deploy_e2e_gate import run_cli

    argv = [
        "--bundle-dir",
        str(deploy),
        "--local-cleaned-bet",
        str(bet),
        "--local-cleaned-session",
        str(sess),
    ]
    code = run_cli(argv)
    assert code == 0
    assert str(bundle_venv_python(deploy)) in reexec_argv[0]
    assert "trainer_hightier.serving.deploy_e2e_gate" in reexec_argv
    bet_idx = reexec_argv.index("--local-cleaned-bet") + 1
    assert Path(reexec_argv[bet_idx]).is_absolute()
    assert Path(reexec_argv[bet_idx]) == bet.resolve()


def test_resolve_cli_data_path_prefers_repo_root_when_cwd_is_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trainer_hightier.serving.deploy_e2e_gate import _resolve_cli_data_path

    repo = Path(__file__).resolve().parents[2]
    rel = Path("trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet")
    bundle = tmp_path / "deploy_bundle"
    bundle.mkdir()
    monkeypatch.chdir(bundle)
    resolved = _resolve_cli_data_path(rel)
    assert resolved == (repo / rel).resolve()
