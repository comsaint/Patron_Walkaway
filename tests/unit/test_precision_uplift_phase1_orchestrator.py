"""Unit tests for precision uplift orchestrator (Phase 1 MVP T1–T8, Phase 2 T9)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _REPO_ROOT / "investigations/precision_uplift_recall_1pct" / "orchestrator"
_RUN_PIPELINE = _ORCHESTRATOR / "run_pipeline.py"

if str(_ORCHESTRATOR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR))

import collectors  # noqa: E402
import config_loader  # noqa: E402
import evaluators  # noqa: E402
import report_builder  # noqa: E402
import run_pipeline  # noqa: E402
import runner  # noqa: E402


def _minimal_config_dict() -> dict:
    return {
        "model_dir": "trainer/models",
        "state_db_path": "s.db",
        "prediction_log_db_path": "p.db",
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "thresholds": {
            "min_hours_preliminary": 48,
            "min_finalized_alerts_preliminary": 300,
            "min_finalized_true_positives_preliminary": 30,
            "min_hours_gate": 72,
            "min_finalized_alerts_gate": 800,
            "min_finalized_true_positives_gate": 50,
        },
    }


def test_config_validation_missing_window_end_raises() -> None:
    """Missing window.end_ts must raise ConfigValidationError with E_CONFIG_INVALID."""
    raw = _minimal_config_dict()
    del raw["window"]["end_ts"]
    with pytest.raises(config_loader.ConfigValidationError, match="E_CONFIG_INVALID"):
        config_loader.validate_phase1_config(raw)


def test_preflight_fails_when_prediction_log_table_missing(tmp_path: Path) -> None:
    """Prediction DB must contain prediction_log table."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    c1 = sqlite3.connect(state_db)
    c1.execute("CREATE TABLE alerts (x INT)")
    c1.execute("CREATE TABLE validation_results (y INT)")
    c1.commit()
    c1.close()
    c2 = sqlite3.connect(pred_db)
    c2.execute("CREATE TABLE other (z INT)")
    c2.commit()
    c2.close()

    cfg = _minimal_config_dict()
    cfg["model_dir"] = str(model_dir)
    cfg["state_db_path"] = str(state_db)
    cfg["prediction_log_db_path"] = str(pred_db)

    out = runner.run_preflight(tmp_path, cfg, skip_backtest_smoke=True)
    assert out["ok"] is False
    assert "prediction_log" in (out.get("message") or "").lower()


def test_preflight_ok_minimal_dbs(tmp_path: Path) -> None:
    """With model_dir + two DBs and required tables, preflight succeeds (no backtest smoke)."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    c1 = sqlite3.connect(state_db)
    c1.execute("CREATE TABLE alerts (x INT)")
    c1.execute("CREATE TABLE validation_results (y INT)")
    c1.commit()
    c1.close()
    c2 = sqlite3.connect(pred_db)
    c2.execute("CREATE TABLE prediction_log (a INT)")
    c2.commit()
    c2.close()

    cfg = _minimal_config_dict()
    cfg["model_dir"] = str(model_dir)
    cfg["state_db_path"] = str(state_db)
    cfg["prediction_log_db_path"] = str(pred_db)

    out = runner.run_preflight(tmp_path, cfg, skip_backtest_smoke=True)
    assert out["ok"] is True, out


def test_run_pipeline_rejects_unsupported_phase() -> None:
    """CLI must exit 2 for unsupported --phase."""
    cfg_path = _ORCHESTRATOR / "config" / "run_phase1.yaml"
    proc = subprocess.run(
        [
            sys.executable,
            str(_RUN_PIPELINE),
            "--phase",
            "phase9",
            "--config",
            str(cfg_path),
            "--run-id",
            "pytest_phase9",
            "--skip-backtest-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "phase9" in proc.stderr or "phase9" in proc.stdout


def _minimal_phase2_dict(
    *,
    model_dir: str,
    state_db: str,
    pred_db: str,
    yaml_run_id: str | None = None,
) -> dict:
    body: dict = {
        "phase": "phase2",
        "common": {
            "model_dir": model_dir,
            "state_db_path": state_db,
            "prediction_log_db_path": pred_db,
            "window": {
                "start_ts": "2026-01-01T00:00:00+08:00",
                "end_ts": "2026-01-08T00:00:00+08:00",
            },
            "contract": {
                "metric": "precision_at_recall_1pct",
                "timezone": "Asia/Hong_Kong",
                "exclude_censored": True,
            },
        },
        "resources": {
            "max_windows": 2,
            "max_trials_per_track": 2,
            "max_parallel_jobs": 1,
            "backtest_skip_optuna": True,
        },
        "tracks": {
            "track_a": {
                "enabled": True,
                "experiments": [{"exp_id": "a0", "overrides": {}}],
            },
            "track_b": {
                "enabled": True,
                "experiments": [{"exp_id": "b0", "overrides": {}}],
            },
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0", "overrides": {}}],
            },
        },
        "gate": {
            "min_uplift_pp_vs_baseline": 3.0,
            "max_std_pp_across_windows": 2.5,
        },
    }
    if yaml_run_id is not None:
        body["run_id"] = yaml_run_id
    return body


def test_phase2_config_missing_track_raises() -> None:
    """Phase 2 config must include track_a/b/c."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    del raw["tracks"]["track_c"]
    with pytest.raises(config_loader.ConfigValidationError, match="E_CONFIG_INVALID"):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_run_id_mismatch_raises() -> None:
    """Optional yaml run_id must match CLI --run-id."""
    raw = _minimal_phase2_dict(
        model_dir="m", state_db="s", pred_db="p", yaml_run_id="yaml_rid"
    )
    with pytest.raises(config_loader.ConfigValidationError, match="run_id mismatch"):
        config_loader.validate_phase2_config(raw, cli_run_id="cli_rid")


def test_build_phase2_input_summary_fingerprint_stable() -> None:
    """Phase 2 fingerprint changes when gate thresholds change."""
    model_dir = "/models"
    cfg1 = _minimal_phase2_dict(model_dir=model_dir, state_db="s", pred_db="p")
    cfg2 = _minimal_phase2_dict(model_dir=model_dir, state_db="s", pred_db="p")
    cfg2["gate"]["min_uplift_pp_vs_baseline"] = 9.0
    p = Path("/tmp/phase2.yaml")
    f1 = run_pipeline.build_phase2_input_summary(cfg1, p)["fingerprint"]
    f2 = run_pipeline.build_phase2_input_summary(cfg2, p)["fingerprint"]
    assert f1 != f2


def test_run_dry_run_readiness_extra_writable_check(tmp_path: Path) -> None:
    """extra_writable adds writable_* checks and artifacts when ok."""
    cfg = _minimal_config_dict()
    extra = tmp_path / "phase2_out"
    with mock.patch("run_pipeline.runner.run_r1_r6_cli_smoke", return_value=(True, None)):
        with mock.patch("run_pipeline.runner.run_backtest_cli_smoke", return_value=(True, None)):
            out = run_pipeline.run_dry_run_readiness(
                tmp_path,
                tmp_path / "orch",
                "dry_extra",
                cfg,
                skip_backtest_smoke=False,
                extra_writable={"phase2_dir": extra},
            )
    assert out["status"] == "READY"
    assert "writable_phase2_dir" in {c["name"] for c in out["checks"]}
    assert "phase2_dir" in out["artifacts"]


def test_run_pipeline_phase2_collect_only_skips_scaffold(tmp_path: Path) -> None:
    """Phase 2 --collect-only stops after preflight (no phase2_scaffold step)."""
    run_id = "pytest_phase2_collect_only"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")

    cfg_file = tmp_path / "phase2_co.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            _minimal_phase2_dict(
                model_dir=str(model_dir),
                state_db=str(state_db),
                pred_db=str(pred_db),
            )
        ),
        encoding="utf-8",
    )
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    if state_json.is_file():
        state_json.unlink()

    proc = subprocess.run(
        [
            sys.executable,
            str(_RUN_PIPELINE),
            "--phase",
            "phase2",
            "--config",
            str(cfg_file),
            "--run-id",
            run_id,
            "--collect-only",
            "--skip-backtest-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(state_json.read_text(encoding="utf-8"))
    assert "phase2_scaffold" not in data.get("steps", {})


def test_run_pipeline_phase2_scaffold_writes_run_state(tmp_path: Path) -> None:
    """Phase 2 run completes after preflight and records phase2_scaffold (T9)."""
    run_id = "pytest_phase2_scaffold"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")

    cfg_file = tmp_path / "phase2.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            _minimal_phase2_dict(
                model_dir=str(model_dir),
                state_db=str(state_db),
                pred_db=str(pred_db),
            )
        ),
        encoding="utf-8",
    )
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    if state_json.is_file():
        state_json.unlink()

    proc = subprocess.run(
        [
            sys.executable,
            str(_RUN_PIPELINE),
            "--phase",
            "phase2",
            "--config",
            str(cfg_file),
            "--run-id",
            run_id,
            "--skip-backtest-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert state_json.is_file()
    data = json.loads(state_json.read_text(encoding="utf-8"))
    assert data["phase"] == "phase2"
    assert data["steps"]["preflight"]["status"] == "success"
    assert data["steps"]["phase2_scaffold"]["status"] == "success"


def test_backtest_smoke_failure_returns_non_ok() -> None:
    """When trainer.backtester --help fails, preflight must not report ok."""
    model_dir = Path("m")
    # Use tmp_path pattern without fixture: minimal inline
    # This test patches subprocess at runner level.
    with mock.patch("runner.subprocess.run") as m_run:
        m_run.return_value = mock.Mock(returncode=1, stdout="", stderr="nope")
        ok, msg = runner.run_backtest_cli_smoke(Path("/tmp"), python_exe=sys.executable)
    assert ok is False
    assert msg is not None and "exit 1" in msg


def test_run_state_written_on_preflight_failure(tmp_path: Path) -> None:
    """Failed preflight still produces run_state.json under orchestrator/state (via CLI)."""
    run_id = "pytest_preflight_fail"
    cfg_file = tmp_path / "cfg.yaml"
    cfg_abs = _minimal_config_dict()
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    cfg_abs["model_dir"] = str(model_dir)
    cfg_abs["state_db_path"] = str(tmp_path / "nostate.db")
    cfg_abs["prediction_log_db_path"] = str(tmp_path / "nopred.db")
    cfg_file.write_text(yaml.safe_dump(cfg_abs), encoding="utf-8")
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    if state_json.is_file():
        state_json.unlink()

    proc = subprocess.run(
        [
            sys.executable,
            str(_RUN_PIPELINE),
            "--phase",
            "phase1",
            "--config",
            str(cfg_file),
            "--run-id",
            run_id,
            "--skip-backtest-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 3
    assert state_json.is_file(), "run_state.json should exist after failed preflight"
    data = json.loads(state_json.read_text(encoding="utf-8"))
    assert data["steps"]["preflight"]["status"] == "failed"


def test_resume_skips_preflight_when_previous_success(
    tmp_path: Path,
) -> None:
    """If run_state marks preflight success, second run should skip preflight (contract under test)."""
    run_id = "pytest_resume_skip"
    state_dir = _ORCHESTRATOR / "state" / run_id
    state_dir.mkdir(parents=True, exist_ok=True)
    state_json = state_dir / "run_state.json"
    cfg = _minimal_config_dict()
    cfg["model_dir"] = str(tmp_path / "missing_models")
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    cfg_path = cfg_file.resolve()
    valid_cfg = config_loader.load_phase1_config(cfg_path)
    input_summary = run_pipeline.build_input_summary(valid_cfg, cfg_path)
    state_json.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "phase": "phase1",
                "steps": {"preflight": {"status": "success"}},
                "input_summary": input_summary,
            },
            default=str,
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(_RUN_PIPELINE),
            "--phase",
            "phase1",
            "--config",
            str(cfg_file),
            "--run-id",
            run_id,
            "--resume",
            "--collect-only",
            "--skip-backtest-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # Current MVP: skip preflight → exit 0 even if model_dir missing (known risk; may tighten in T7).
    assert proc.returncode == 0, proc.stderr


def test_backtest_smoke_failure_error_code_is_not_db_unavailable(tmp_path: Path) -> None:
    """When only backtest CLI fails, error_code must be E_BACKTEST_CLI (not DB)."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    c1 = sqlite3.connect(state_db)
    c1.execute("CREATE TABLE alerts (x INT)")
    c1.execute("CREATE TABLE validation_results (y INT)")
    c1.commit()
    c1.close()
    c2 = sqlite3.connect(pred_db)
    c2.execute("CREATE TABLE prediction_log (a INT)")
    c2.commit()
    c2.close()

    cfg = _minimal_config_dict()
    cfg["model_dir"] = str(model_dir)
    cfg["state_db_path"] = str(state_db)
    cfg["prediction_log_db_path"] = str(pred_db)

    with mock.patch("runner.run_backtest_cli_smoke", return_value=(False, "cli failed")):
        out = runner.run_preflight(tmp_path, cfg, skip_backtest_smoke=False)
    assert out["ok"] is False
    assert out.get("error_code") == "E_BACKTEST_CLI"


# --- T3: subprocess logging & error classification ---


def test_classify_r1_r6_no_bets_clickhouse() -> None:
    """ClickHouse returned no bets for sampled players → E_NO_DATA_WINDOW."""
    code, _msg = runner.classify_r1_r6_failure(
        "No bets fetched from ClickHouse for sampled players", 1
    )
    assert code == "E_NO_DATA_WINDOW"


def test_classify_r1_r6_empty_sample_rows() -> None:
    """Empty sample CSV → E_EMPTY_SAMPLE."""
    code, _msg = runner.classify_r1_r6_failure("sample CSV contains no bet_id rows", 1)
    assert code == "E_EMPTY_SAMPLE"


def test_classify_r1_r6_prediction_log_missing() -> None:
    """Missing prediction_log table → E_ARTIFACT_MISSING."""
    code, _msg = runner.classify_r1_r6_failure("prediction_log table not found", 1)
    assert code == "E_ARTIFACT_MISSING"


def test_classify_backtest_no_bets_in_window() -> None:
    """Backtester exit when window has no rows → E_NO_DATA_WINDOW."""
    code, _msg = runner.classify_backtest_failure("No bets for the requested window", 1)
    assert code == "E_NO_DATA_WINDOW"


def test_run_logged_command_writes_files(tmp_path: Path) -> None:
    """Logged subprocess should persist stdout/stderr under log_dir."""
    log_dir = tmp_path / "logs"
    result = runner.run_logged_command(
        [sys.executable, "-c", "print('orch_ok')"],
        cwd=tmp_path,
        log_dir=log_dir,
        log_stem="probe",
    )
    assert result.get("ok") is True
    assert (log_dir / "probe.stdout.log").read_text(encoding="utf-8").strip() == "orch_ok"


def test_run_phase1_r1_r6_missing_script_is_artifact_missing(tmp_path: Path) -> None:
    """When r1_r6 script path does not exist, fail before subprocess with E_ARTIFACT_MISSING."""
    cfg = _minimal_config_dict()
    cfg["r1_r6_script"] = "definitely_missing_r1_script_4242.py"
    res = runner.run_phase1_r1_r6_all(tmp_path, cfg, tmp_path / "lg")
    assert res.get("ok") is False
    assert res.get("error_code") == "E_ARTIFACT_MISSING"


# --- T4: collectors ---


def test_collect_phase1_loads_backtest_r1_and_state_stats(tmp_path: Path) -> None:
    """Collector merges backtest_metrics.json, r1 stdout JSON, and validation_results counts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_collect_ok"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)

    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text(
        json.dumps({"precision": 0.5}), encoding="utf-8"
    )
    r1_obj = {"mode": "all", "evaluate": {"n": 3}}
    (logs / "r1_r6.stdout.log").write_text(
        json.dumps(r1_obj, ensure_ascii=False), encoding="utf-8"
    )

    state_db = repo / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        """CREATE TABLE validation_results (
            bet_id TEXT,
            alert_ts TEXT,
            validated_at TEXT,
            result INTEGER
        )"""
    )
    conn.execute(
        "INSERT INTO validation_results VALUES (?,?,?,?)",
        ("a", "2026-01-02T00:00:00+08:00", "2026-01-03T00:00:00+08:00", 1),
    )
    conn.execute(
        "INSERT INTO validation_results VALUES (?,?,?,?)",
        ("b", "2026-01-02T01:00:00+08:00", "", 0),
    )
    conn.commit()
    conn.close()

    cfg = _minimal_config_dict()
    cfg["state_db_path"] = str(state_db)
    cfg["backtest_metrics_path"] = "trainer/out_backtest/backtest_metrics.json"

    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    assert bundle["backtest_metrics"] == {"precision": 0.5}
    assert bundle["r1_r6_final"]["payload"] == r1_obj
    assert bundle["state_db_stats"]["finalized_alerts_count"] == 1
    assert bundle["state_db_stats"]["finalized_true_positives_count"] == 1
    assert bundle["errors"] == []


def test_collect_phase1_errors_when_backtest_and_r1_missing(tmp_path: Path) -> None:
    """Missing metrics file and r1 log must populate ``errors`` (non-silent)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_collect_bad"
    (orch / "state" / run_id / "logs").mkdir(parents=True)

    state_db = repo / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute("CREATE TABLE validation_results (alert_ts TEXT, validated_at TEXT, result INT)")
    conn.commit()
    conn.close()

    cfg = _minimal_config_dict()
    cfg["state_db_path"] = str(state_db)
    cfg["backtest_metrics_path"] = "trainer/out_backtest/backtest_metrics.json"

    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    codes = {e["code"] for e in bundle["errors"]}
    assert collectors.ERR_BACKTEST_METRICS in codes
    assert collectors.ERR_R1_PAYLOAD in codes


def test_collect_phase1_optional_r1_mid_stdout(tmp_path: Path) -> None:
    """When ``r1_r6_mid.stdout.log`` exists, it is parsed into ``r1_r6_mid``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_mid"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    (logs / "r1_r6_mid.stdout.log").write_text(
        json.dumps({"stage": "mid"}), encoding="utf-8"
    )
    (logs / "r1_r6.stdout.log").write_text(
        json.dumps({"stage": "final"}), encoding="utf-8"
    )
    metrics_dir = repo / "trainer" / "out_backtest"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "backtest_metrics.json").write_text("{}", encoding="utf-8")

    state_db = repo / "state.db"
    with sqlite3.connect(state_db) as conn_mid:
        conn_mid.execute(
            "CREATE TABLE validation_results (alert_ts TEXT, validated_at TEXT, result INT)"
        )
        conn_mid.commit()

    cfg = _minimal_config_dict()
    cfg["state_db_path"] = str(state_db)
    bundle = collectors.collect_phase1_artifacts(
        run_id, cfg, repo_root=repo, orchestrator_dir=orch
    )
    assert bundle["r1_r6_mid"]["payload"] == {"stage": "mid"}
    assert bundle["r1_r6_final"]["payload"] == {"stage": "final"}


def test_collect_summary_for_run_state_keys() -> None:
    """Summary view used in run_state must expose gate-oriented flags."""
    bundle = {
        "errors": [{"code": "X"}],
        "backtest_metrics": None,
        "r1_r6_final": {"payload": {"k": 1}},
        "r1_r6_mid": {"payload": None},
        "state_db_stats": {
            "finalized_alerts_count": 10,
            "finalized_true_positives_count": 2,
        },
    }
    s = collectors.collect_summary_for_run_state(bundle)
    assert s["error_count"] == 1
    assert s["has_r1_final_payload"] is True
    assert s["has_r1_mid_payload"] is False
    assert s["finalized_alerts_count"] == 10


# --- T5: gate evaluator ---


def _pat_block(pat: float) -> dict:
    return {
        "unified_sample_evaluation": {
            "precision_at_recall_target": {
                "target_recall": 0.01,
                "precision_at_target_recall": pat,
            }
        }
    }


def _gate_bundle(
    *,
    hours_span: str,
    alerts: int,
    tp: int,
    final_pat: float,
    mid_pat: float | None,
    errors: list | None = None,
    r2: dict | None = None,
) -> dict:
    payload = dict(_pat_block(final_pat))
    if r2 is not None:
        payload["r2_prediction_log_vs_alerts"] = r2
    b: dict = {
        "errors": list(errors) if errors is not None else [],
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": hours_span,
        },
        "thresholds": {
            "min_hours_preliminary": 48,
            "min_hours_gate": 72,
            "min_finalized_alerts_preliminary": 300,
            "min_finalized_true_positives_preliminary": 30,
            "min_finalized_alerts_gate": 800,
            "min_finalized_true_positives_gate": 50,
            "gate_pat_abs_tolerance": 0.15,
        },
        "state_db_stats": {
            "finalized_alerts_count": alerts,
            "finalized_true_positives_count": tp,
        },
        "r1_r6_final": {"payload": payload},
        "r1_r6_mid": {"payload": _pat_block(mid_pat) if mid_pat is not None else None},
        "backtest_metrics": {},
    }
    return b


def test_window_duration_hours_positive() -> None:
    """Window span must match ISO timestamps."""
    h = evaluators.window_duration_hours(
        {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-04T10:00:00+08:00",
        }
    )
    assert abs(h - 82.0) < 1e-6


def test_extract_precision_reads_unified_block() -> None:
    """PAT must be read from unified_sample_evaluation."""
    p = evaluators.extract_precision_at_target_recall(_pat_block(0.37))
    assert abs(p - 0.37) < 1e-9


def test_gate_fail_when_collector_errors() -> None:
    """Any collect error forces FAIL."""
    b = _gate_bundle(
        hours_span="2026-01-04T10:00:00+08:00",
        alerts=900,
        tp=50,
        final_pat=0.4,
        mid_pat=0.41,
        errors=[{"code": "E_COLLECT_BACKTEST_METRICS"}],
    )
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "FAIL"
    assert any("collect_error" in r for r in g["blocking_reasons"])


def test_gate_preliminary_short_window() -> None:
    """Below preliminary hours → PRELIMINARY."""
    b = _gate_bundle(
        hours_span="2026-01-02T00:00:00+08:00",
        alerts=900,
        tp=50,
        final_pat=0.4,
        mid_pat=0.41,
    )
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "PRELIMINARY"
    assert "observation_hours_below_preliminary_minimum" in g["blocking_reasons"]


def test_gate_pass_when_gate_thresholds_and_pat_aligned() -> None:
    """Sufficient window, samples, mid/final PAT within tolerance → PASS."""
    b = _gate_bundle(
        hours_span="2026-01-04T10:00:00+08:00",
        alerts=900,
        tp=50,
        final_pat=0.40,
        mid_pat=0.42,
    )
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "PASS"
    assert g["blocking_reasons"] == []


def test_gate_fail_on_mid_final_pat_divergence() -> None:
    """Large |Δpat| → FAIL."""
    b = _gate_bundle(
        hours_span="2026-01-04T10:00:00+08:00",
        alerts=900,
        tp=50,
        final_pat=0.10,
        mid_pat=0.80,
    )
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "FAIL"
    assert "divergence" in "".join(g["blocking_reasons"])


def test_gate_preliminary_when_mid_snapshot_missing() -> None:
    """Gate samples/time met but no mid PAT → PRELIMINARY (direction unknown)."""
    b = _gate_bundle(
        hours_span="2026-01-04T10:00:00+08:00",
        alerts=900,
        tp=50,
        final_pat=0.4,
        mid_pat=None,
    )
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "PRELIMINARY"
    assert "missing_mid_r1_snapshot" in "".join(g["blocking_reasons"])


def test_gate_fail_on_r2_large_gap() -> None:
    """R2 PL vs alerts mismatch heuristic → FAIL."""
    r2 = {
        "status": "ok",
        "difference_pl_minus_alerts": 500,
        "n_prediction_log_is_alert_rows": 10,
    }
    b = _gate_bundle(
        hours_span="2026-01-04T10:00:00+08:00",
        alerts=900,
        tp=50,
        final_pat=0.4,
        mid_pat=0.41,
        r2=r2,
    )
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "FAIL"
    assert "r2_prediction_log_vs_alerts_mismatch" in g["blocking_reasons"]


# --- T6: report_builder ---


def test_write_phase1_reports_writes_six_markdown_files(tmp_path: Path) -> None:
    """T6: all six phase1 artifacts are written with run metadata."""
    phase1 = tmp_path / "phase1"
    cfg = _minimal_config_dict()
    bundle = {
        "run_id": "rep_run",
        "errors": [{"code": "E_COLLECT_BACKTEST_METRICS", "message": "missing"}],
        "window": dict(cfg["window"]),
        "thresholds": dict(cfg["thresholds"]),
        "backtest_metrics": None,
        "state_db_stats": {"finalized_alerts_count": 1},
        "r1_r6_final": {"payload": None},
        "r1_r6_mid": {"payload": None},
    }
    gate = evaluators.evaluate_phase1_gate(bundle)
    paths = report_builder.write_phase1_reports(phase1, "rep_run", cfg, bundle, gate)
    assert len(paths) == 6
    names = {p.name for p in paths}
    assert names == {
        "upper_bound_repro.md",
        "label_noise_audit.md",
        "slice_performance_report.md",
        "point_in_time_parity_check.md",
        "phase1_gate_decision.md",
        "status_history_crosscheck.md",
    }
    gate_md = (phase1 / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "`rep_run`" in gate_md or "rep_run" in gate_md
    assert str(gate["status"]) in gate_md


def test_status_history_crosscheck_orchestrator_block_replaced_not_stacked(tmp_path: Path) -> None:
    """Orchestrator markers should leave manual prefix and replace note on re-run."""
    phase1 = tmp_path / "phase1"
    phase1.mkdir()
    (phase1 / "status_history_crosscheck.md").write_text(
        "# status_history_crosscheck\n\nManual narrative.\n",
        encoding="utf-8",
    )
    cfg = _minimal_config_dict()
    bundle = {
        "run_id": "x",
        "errors": [{"code": "E_COLLECT_R1_PAYLOAD", "message": "n"}],
        "window": dict(cfg["window"]),
        "thresholds": dict(cfg["thresholds"]),
        "backtest_metrics": None,
        "state_db_stats": {},
        "r1_r6_final": {"payload": None},
        "r1_r6_mid": {"payload": None},
    }
    report_builder.write_phase1_reports(phase1, "run_a", cfg, bundle, evaluators.evaluate_phase1_gate(bundle))
    t1 = (phase1 / "status_history_crosscheck.md").read_text(encoding="utf-8")
    assert "Manual narrative." in t1
    assert t1.count("<!-- ORCHESTRATOR_RUN_NOTE_START -->") == 1
    report_builder.write_phase1_reports(phase1, "run_b", cfg, bundle, evaluators.evaluate_phase1_gate(bundle))
    t2 = (phase1 / "status_history_crosscheck.md").read_text(encoding="utf-8")
    assert "Manual narrative." in t2
    assert t2.count("<!-- ORCHESTRATOR_RUN_NOTE_START -->") == 1
    assert "run_b" in t2


# --- T7: run_state / resume fingerprint ---


def test_build_input_summary_fingerprint_changes_with_threshold(tmp_path: Path) -> None:
    """Fingerprint must capture threshold values (not just keys)."""
    base = _minimal_config_dict()
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    base_a = {**base, "thresholds": {**base["thresholds"], "min_hours_gate": 72}}
    base_b = {**base, "thresholds": {**base["thresholds"], "min_hours_gate": 96}}
    a.write_text(yaml.safe_dump(base_a), encoding="utf-8")
    b.write_text(yaml.safe_dump(base_b), encoding="utf-8")
    pa = config_loader.load_phase1_config(a.resolve())
    pb = config_loader.load_phase1_config(b.resolve())
    fa = run_pipeline.build_input_summary(pa, a.resolve())["fingerprint"]
    fb = run_pipeline.build_input_summary(pb, b.resolve())["fingerprint"]
    assert fa != fb


def test_build_input_summary_includes_config_path(tmp_path: Path) -> None:
    """Different YAML paths → different fingerprint (even if contents equal)."""
    base = _minimal_config_dict()
    p1 = tmp_path / "c1.yaml"
    p2 = tmp_path / "c2.yaml"
    p1.write_text(yaml.safe_dump(base), encoding="utf-8")
    p2.write_text(yaml.safe_dump(base), encoding="utf-8")
    cfg1 = config_loader.load_phase1_config(p1.resolve())
    cfg2 = config_loader.load_phase1_config(p2.resolve())
    f1 = run_pipeline.build_input_summary(cfg1, p1.resolve())["fingerprint"]
    f2 = run_pipeline.build_input_summary(cfg2, p2.resolve())["fingerprint"]
    assert f1 != f2


# --- T8: dry-run readiness ---


def test_run_r1_r6_cli_smoke_missing_script_returns_false(tmp_path: Path) -> None:
    """Dry-run R1 smoke should fail fast when script path does not exist."""
    cfg = _minimal_config_dict()
    cfg["r1_r6_script"] = "missing_r1_script_abc.py"
    ok, msg = runner.run_r1_r6_cli_smoke(tmp_path, cfg)
    assert ok is False
    assert msg is not None and "not found" in msg


def test_run_dry_run_readiness_ready_when_smokes_and_paths_ok(tmp_path: Path) -> None:
    """Dry-run returns READY when command smokes and writable targets pass."""
    cfg = _minimal_config_dict()
    with mock.patch("run_pipeline.runner.run_r1_r6_cli_smoke", return_value=(True, None)):
        with mock.patch("run_pipeline.runner.run_backtest_cli_smoke", return_value=(True, None)):
            out = run_pipeline.run_dry_run_readiness(
                tmp_path,
                tmp_path / "orch",
                "dry_ready",
                cfg,
                skip_backtest_smoke=False,
            )
    assert out["status"] == "READY"
    assert out["blocking_reasons"] == []
    check_names = {c["name"] for c in out["checks"]}
    assert "r1_r6_cli_smoke" in check_names
    assert "backtester_cli_smoke" in check_names
    assert "writable_state_dir" in check_names


def test_run_dry_run_readiness_not_ready_on_failed_smoke(tmp_path: Path) -> None:
    """Dry-run returns NOT_READY when any required smoke check fails."""
    cfg = _minimal_config_dict()
    with mock.patch("run_pipeline.runner.run_r1_r6_cli_smoke", return_value=(False, "boom")):
        with mock.patch("run_pipeline.runner.run_backtest_cli_smoke", return_value=(True, None)):
            out = run_pipeline.run_dry_run_readiness(
                tmp_path,
                tmp_path / "orch",
                "dry_bad",
                cfg,
                skip_backtest_smoke=False,
            )
    assert out["status"] == "NOT_READY"
    assert "r1_r6_cli_smoke_failed" in out["blocking_reasons"]


def test_dry_run_cli_not_ready_returns_6_and_writes_readiness(tmp_path: Path) -> None:
    """CLI dry-run exits 6 when R1 smoke fails and records readiness in run_state."""
    run_id = "pytest_dry_not_ready"
    cfg_file = tmp_path / "cfg.yaml"
    cfg = _minimal_config_dict()
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg["model_dir"] = str(model_dir)
    cfg["state_db_path"] = str(state_db)
    cfg["prediction_log_db_path"] = str(pred_db)
    cfg["r1_r6_script"] = "missing_r1_script_abc.py"
    cfg_file.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(_RUN_PIPELINE),
            "--phase",
            "phase1",
            "--config",
            str(cfg_file),
            "--run-id",
            run_id,
            "--dry-run",
            "--skip-backtest-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 6
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    data = json.loads(state_json.read_text(encoding="utf-8"))
    assert data["mode"] == "dry_run"
    assert data["readiness"]["status"] == "NOT_READY"


def _minimal_run_full_dict(p1: str, p2: str, p3: str, p4: str) -> dict:
    """Minimal ``run_full`` root mapping for tests."""
    return {
        "phase": "all",
        "execution": {
            "phase_order": ["phase1", "phase2", "phase3", "phase4"],
            "stop_on_gate_block": True,
            "allow_force_next": False,
        },
        "phase_configs": {
            "phase1": p1,
            "phase2": p2,
            "phase3": p3,
            "phase4": p4,
        },
    }


def test_run_full_config_run_id_mismatch_raises() -> None:
    """Optional yaml run_id must match CLI --run-id for run_full."""
    raw = _minimal_run_full_dict("a.yaml", "b.yaml", "c.yaml", "d.yaml")
    raw["run_id"] = "yaml_rid"
    with pytest.raises(config_loader.ConfigValidationError, match="run_id mismatch"):
        config_loader.validate_run_full_config(raw, cli_run_id="cli_rid")


def test_build_run_full_input_summary_fingerprint_stable(tmp_path: Path) -> None:
    """Same run_full inputs must yield stable fingerprint."""
    p = tmp_path / "rf.yaml"
    paths = {k: tmp_path / f"{k}.yaml" for k in ("phase1", "phase2", "phase3", "phase4")}
    rf = _minimal_run_full_dict(
        str(paths["phase1"]),
        str(paths["phase2"]),
        str(paths["phase3"]),
        str(paths["phase4"]),
    )
    cfg = config_loader.validate_run_full_config(rf, cli_run_id="rid")
    rp = {k: paths[k].resolve() for k in paths}
    f1 = run_pipeline.build_run_full_input_summary(cfg, p, rp)["fingerprint"]
    f2 = run_pipeline.build_run_full_input_summary(cfg, p, rp)["fingerprint"]
    assert f1 == f2


def test_cli_phase_all_without_dry_run_exits_2(tmp_path: Path) -> None:
    """--phase all without --dry-run must exit 2 (autonomous mode not implemented)."""
    rf_path = tmp_path / "run_full.yaml"
    rf_path.write_text(
        yaml.safe_dump(
            _minimal_run_full_dict("x.yaml", "y.yaml", "z.yaml", "w.yaml")
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(_RUN_PIPELINE),
            "--phase",
            "all",
            "--config",
            str(rf_path),
            "--run-id",
            "pytest_all_nodry",
            "--skip-backtest-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


def test_run_all_phases_dry_run_readiness_ready(tmp_path: Path) -> None:
    """All-phase dry-run readiness READY when paths and checks pass (smokes mocked)."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")

    p1 = _minimal_config_dict()
    p1["model_dir"] = str(model_dir)
    p1["state_db_path"] = str(state_db)
    p1["prediction_log_db_path"] = str(pred_db)

    p1_path = tmp_path / "phase1.yaml"
    p1_path.write_text(yaml.safe_dump(p1), encoding="utf-8")

    p2_body = _minimal_phase2_dict(
        model_dir=str(model_dir),
        state_db=str(state_db),
        pred_db=str(pred_db),
    )
    p2_path = tmp_path / "phase2.yaml"
    p2_path.write_text(yaml.safe_dump(p2_body), encoding="utf-8")

    p3_body = {
        "phase": "phase3",
        "upstream": {
            "phase2_run_id": "p2rid",
            "winner_track": "track_a",
            "winner_exp_id": "a0",
        },
        "common": {
            "contract": {
                "metric": "precision_at_recall_1pct",
                "timezone": "Asia/Hong_Kong",
                "exclude_censored": True,
            }
        },
        "resources": {"max_parallel_jobs": 1},
        "workstreams": {},
        "gate": {},
    }
    p3_path = tmp_path / "phase3.yaml"
    p3_path.write_text(yaml.safe_dump(p3_body), encoding="utf-8")

    p4_body = {
        "phase": "phase4",
        "candidate": {
            "model_dir": str(model_dir),
            "source_phase3_run_id": "p3rid",
            "threshold_strategy": "holdout_selected",
        },
        "evaluation": {
            "windows": [
                {
                    "start_ts": "2026-01-01T00:00:00+08:00",
                    "end_ts": "2026-01-08T00:00:00+08:00",
                }
            ],
            "contract": {
                "metric": "precision_at_recall_1pct",
                "timezone": "Asia/Hong_Kong",
                "exclude_censored": True,
            },
        },
        "resources": {"max_parallel_jobs": 1},
        "gate": {},
    }
    p4_path = tmp_path / "phase4.yaml"
    p4_path.write_text(yaml.safe_dump(p4_body), encoding="utf-8")

    rf = _minimal_run_full_dict(
        str(p1_path), str(p2_path), str(p3_path), str(p4_path)
    )
    rf_cfg = config_loader.validate_run_full_config(rf, cli_run_id="all_rid")
    resolved = {
        "phase1": p1_path.resolve(),
        "phase2": p2_path.resolve(),
        "phase3": p3_path.resolve(),
        "phase4": p4_path.resolve(),
    }
    orch = tmp_path / "orch"
    with mock.patch.object(
        run_pipeline.runner,
        "run_preflight",
        return_value={"ok": True, "checks": []},
    ):
        with mock.patch.object(
            run_pipeline.runner,
            "run_r1_r6_cli_smoke",
            return_value=(True, None),
        ):
            with mock.patch.object(
                run_pipeline.runner,
                "run_backtest_cli_smoke",
                return_value=(True, None),
            ):
                out = run_pipeline.run_all_phases_dry_run_readiness(
                    tmp_path,
                    orch,
                    "run1",
                    rf_cfg,
                    resolved,
                    skip_backtest_smoke=False,
                    skip_phase1_preflight=True,
                    skip_phase2_preflight=False,
                )
    assert out["status"] == "READY", out
    assert out["blocking_reasons"] == []
