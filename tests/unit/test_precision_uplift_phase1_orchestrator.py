"""Unit tests for precision uplift orchestrator (Phase 1 MVP T1–T8, Phase 2 T9)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _REPO_ROOT / "investigations/precision_uplift_recall_1pct" / "orchestrator"
_RUN_PIPELINE = _ORCHESTRATOR / "run_pipeline.py"
_ADHOC_RUNBOOK = (
    _REPO_ROOT
    / "investigations"
    / "precision_uplift_recall_1pct"
    / "PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md"
)

if str(_ORCHESTRATOR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR))

import collectors  # noqa: E402
import common_exit_codes as orch_exits  # noqa: E402
import config_loader  # noqa: E402
import evaluators  # noqa: E402
import phase1_autonomous_fsm as p1_fsm  # noqa: E402
import phase2_exit_codes as phase2_exits  # noqa: E402
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
    assert proc.returncode == orch_exits.EXIT_CONFIG_INVALID
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


def test_phase2_config_model_version_resolves_model_dir() -> None:
    """common.model_version + models_root materializes common.model_dir (versioned layout)."""
    raw = _minimal_phase2_dict(model_dir="unused", state_db="s", pred_db="p")
    del raw["common"]["model_dir"]
    raw["common"]["models_root"] = "custom_models_root"
    raw["common"]["model_version"] = "train_run_01"
    out = config_loader.validate_phase2_config(raw, cli_run_id="rid")
    assert out["common"]["model_dir"] == "custom_models_root/train_run_01"


def test_phase2_config_model_version_default_models_root() -> None:
    """Omitting models_root defaults to out/models under repo-relative resolution."""
    raw = _minimal_phase2_dict(model_dir="unused", state_db="s", pred_db="p")
    del raw["common"]["model_dir"]
    raw["common"]["model_version"] = "v_only"
    out = config_loader.validate_phase2_config(raw, cli_run_id="rid")
    assert out["common"]["model_dir"] == "out/models/v_only"


def test_phase2_config_model_dir_and_model_version_exclusive() -> None:
    """common.model_dir and common.model_version cannot both be set."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["common"]["model_version"] = "v1"
    with pytest.raises(config_loader.ConfigValidationError, match="mutually exclusive"):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_models_root_without_model_version_raises() -> None:
    """models_root is only meaningful with model_version."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["common"]["models_root"] = "out/models"
    with pytest.raises(config_loader.ConfigValidationError, match="models_root is only valid"):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_model_version_path_traversal_rejected() -> None:
    """model_version must be a single path segment (delegates to safe_version_subdirectory)."""
    raw = _minimal_phase2_dict(model_dir="unused", state_db="s", pred_db="p")
    del raw["common"]["model_dir"]
    raw["common"]["model_version"] = "evil/../segment"
    with pytest.raises(config_loader.ConfigValidationError, match="model_version"):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_run_id_mismatch_raises() -> None:
    """Optional yaml run_id must match CLI --run-id."""
    raw = _minimal_phase2_dict(
        model_dir="m", state_db="s", pred_db="p", yaml_run_id="yaml_rid"
    )
    with pytest.raises(config_loader.ConfigValidationError, match="run_id mismatch"):
        config_loader.validate_phase2_config(raw, cli_run_id="cli_rid")


def test_phase2_config_gate_baseline_exp_id_by_track_validation() -> None:
    """gate.baseline_exp_id_by_track requires known track keys and non-empty exp ids."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["gate"]["baseline_exp_id_by_track"] = {"track_z": "z0"}
    with pytest.raises(config_loader.ConfigValidationError, match="unknown track"):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")

    raw2 = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw2["gate"]["baseline_exp_id_by_track"] = {"track_a": "   "}
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="baseline_exp_id_by_track.track_a must be a non-empty string exp_id",
    ):
        config_loader.validate_phase2_config(raw2, cli_run_id="rid")


def test_phase2_gate_cli_exit_code_when_disabled() -> None:
    """Without --phase2-fail-on-gate-fail policy, CLI helper returns None."""
    assert (
        run_pipeline.phase2_gate_cli_exit_code({"status": "FAIL"}, fail_on_gate_fail=False)
        is None
    )


def test_phase2_gate_cli_exit_code_on_fail_enabled() -> None:
    """With fail policy, FAIL status maps to EXIT_PHASE2_GATE_FAIL."""
    assert (
        run_pipeline.phase2_gate_cli_exit_code({"status": "FAIL"}, fail_on_gate_fail=True)
        == phase2_exits.EXIT_PHASE2_GATE_FAIL
    )


def test_phase2_gate_cli_exit_code_pass_and_blocked_unchanged() -> None:
    """PASS and BLOCKED never request exit 9 from this helper."""
    assert (
        run_pipeline.phase2_gate_cli_exit_code({"status": "PASS"}, fail_on_gate_fail=True)
        is None
    )
    assert (
        run_pipeline.phase2_gate_cli_exit_code(
            {"status": "BLOCKED"}, fail_on_gate_fail=True
        )
        is None
    )


def test_phase2_gate_cli_exit_code_blocked_exit_10() -> None:
    """With blocked policy, BLOCKED maps to EXIT_PHASE2_GATE_BLOCKED."""
    assert (
        run_pipeline.phase2_gate_cli_exit_code(
            {"status": "BLOCKED"},
            fail_on_gate_blocked=True,
        )
        == phase2_exits.EXIT_PHASE2_GATE_BLOCKED
    )


def test_phase2_gate_cli_fail_precedes_blocked_when_both_flags() -> None:
    """FAIL returns gate-fail exit before BLOCKED would apply when both policies are on."""
    assert (
        run_pipeline.phase2_gate_cli_exit_code(
            {"status": "FAIL"},
            fail_on_gate_fail=True,
            fail_on_gate_blocked=True,
        )
        == phase2_exits.EXIT_PHASE2_GATE_FAIL
    )


def test_phase2_failure_step_cli_exit_mapping_matches_constants() -> None:
    """Documented step→CLI exit mapping stays aligned with integer constants."""
    m = phase2_exits.PHASE2_FAILURE_STEP_CLI_EXITS
    assert m["phase2_runner_smoke"] == phase2_exits.EXIT_PHASE2_RUNNER_SMOKE_FAILED
    assert m["phase2_trainer_jobs"] == phase2_exits.EXIT_PHASE2_TRAINER_JOBS_FAILED
    assert (
        m["phase2_per_job_backtest_jobs"]
        == phase2_exits.EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE
    )
    assert m["phase2_backtest_jobs"] == phase2_exits.EXIT_PHASE2_BACKTEST_OR_ARTIFACT_FAILURE


def test_phase2_exit_codes_numeric_contract() -> None:
    """Lock integer CLI contract for _main_phase2 early exits (historic magic numbers)."""
    assert phase2_exits.EXIT_CONFIG_INVALID == 2
    assert phase2_exits.EXIT_PREFLIGHT_FAILED == 3
    assert phase2_exits.EXIT_RESUME_BUNDLE_LOAD_FAILED == 4
    assert phase2_exits.EXIT_DRY_RUN_NOT_READY == 6


def test_common_exit_codes_match_phase2_reexported_shared() -> None:
    """Shared 2/3/6 must stay single source: common module == phase2 re-exports."""
    assert orch_exits.EXIT_CONFIG_INVALID == phase2_exits.EXIT_CONFIG_INVALID
    assert orch_exits.EXIT_PREFLIGHT_FAILED == phase2_exits.EXIT_PREFLIGHT_FAILED
    assert orch_exits.EXIT_DRY_RUN_NOT_READY == phase2_exits.EXIT_DRY_RUN_NOT_READY


def test_common_exit_codes_phase1_four_five_numeric_contract() -> None:
    """Phase 1 mid/R1 and backtest CLI exits keep historic integers 4 and 5."""
    assert orch_exits.EXIT_PHASE1_MID_OR_R1_FAILED == 4
    assert orch_exits.EXIT_PHASE1_BACKTEST_FAILED == 5


def test_common_exit_codes_phase1_autonomous_pending_is_eleven() -> None:
    """T8A: autonomous without dry-run uses exit 11 until supervisor loop exists."""
    assert orch_exits.EXIT_PHASE1_AUTONOMOUS_PENDING == 11


def test_common_exit_codes_phase1_autonomous_mid_not_eligible_is_twelve() -> None:
    """T8C: --autonomous-mid-r1-once without eligible observe_context uses exit 12."""
    assert orch_exits.EXIT_PHASE1_AUTONOMOUS_MID_NOT_ELIGIBLE == 12


def test_phase1_autonomous_fsm_linear_successors() -> None:
    """T8A skeleton: backbone order matches MVP task list."""
    assert p1_fsm.successor(p1_fsm.STEP_INIT) == p1_fsm.STEP_OBSERVE
    assert p1_fsm.successor(p1_fsm.STEP_OBSERVE) == p1_fsm.STEP_MID_SNAPSHOT
    assert p1_fsm.successor(p1_fsm.STEP_REPORT) is None


def test_phase1_autonomous_fsm_observe_self_loop_allowed() -> None:
    """Supervisor may idle in observe before mid_snapshot."""
    assert p1_fsm.can_transition(p1_fsm.STEP_OBSERVE, p1_fsm.STEP_OBSERVE) is True
    assert p1_fsm.can_transition(p1_fsm.STEP_OBSERVE, p1_fsm.STEP_MID_SNAPSHOT) is True


def test_phase1_autonomous_fsm_restore_cursor_on_resume() -> None:
    """restore_cursor reads run_state.phase1_autonomous.current_step when valid."""
    prev = {"phase1_autonomous": {"current_step": p1_fsm.STEP_MID_SNAPSHOT}}
    assert p1_fsm.restore_cursor(prev, resume=True) == p1_fsm.STEP_MID_SNAPSHOT
    assert p1_fsm.restore_cursor(prev, resume=False) == p1_fsm.STEP_INIT
    bad = {"phase1_autonomous": {"current_step": "unknown_step"}}
    assert p1_fsm.restore_cursor(bad, resume=True) == p1_fsm.STEP_INIT


def test_read_autonomous_cursor_reads_block_without_resume_semantics() -> None:
    """read_autonomous_cursor always reads persisted block (for autonomous-once chain)."""
    assert (
        p1_fsm.read_autonomous_cursor({"phase1_autonomous": {"current_step": p1_fsm.STEP_OBSERVE}})
        == p1_fsm.STEP_OBSERVE
    )


def test_after_stub_tick_init_to_observe() -> None:
    """First stub tick moves init -> observe."""
    out = p1_fsm.after_stub_tick(None, tick_iso="2026-04-18T00:00:00+00:00", config_fingerprint="fp9")
    assert out["current_step"] == p1_fsm.STEP_OBSERVE
    assert out["stub_last_note"] == "stub_tick: init -> observe"
    assert out["tick_seq"] == 1
    assert out["checkpoint"]["tick_seq"] == 1
    assert out["checkpoint"]["cursor_before"] == p1_fsm.STEP_INIT
    assert out["checkpoint"]["cursor_after"] == p1_fsm.STEP_OBSERVE
    assert out["checkpoint"]["config_fingerprint"] == "fp9"


def test_after_stub_tick_observe_increments_counter() -> None:
    """Second+ ticks in observe increment stub_observe_ticks."""
    first = p1_fsm.after_stub_tick(None, tick_iso="t1")
    prev = {"phase1_autonomous": first}
    second = p1_fsm.after_stub_tick(prev, tick_iso="t2")
    assert second["stub_observe_ticks"] == 1
    assert second["tick_seq"] == 2
    third = p1_fsm.after_stub_tick({"phase1_autonomous": second}, tick_iso="t3")
    assert third["stub_observe_ticks"] == 2
    assert third["tick_seq"] == 3


def test_after_stub_tick_advance_observe_to_mid_when_eligible() -> None:
    """Eligible observe_context + advance flag moves observe -> mid_snapshot without stub tick."""
    prev_block = {
        "current_step": p1_fsm.STEP_OBSERVE,
        "tick_seq": 1,
        "stub_observe_ticks": 0,
        "fsm_schema_version": 1,
        "backbone": list(p1_fsm.ORDERED_STEPS),
        "observe_self_loop": True,
    }
    oc: dict[str, Any] = {"mid_snapshot_eligible": True}
    out = p1_fsm.after_stub_tick(
        {"phase1_autonomous": prev_block},
        tick_iso="t2",
        observe_context=oc,
        advance_mid_when_eligible=True,
    )
    assert out["current_step"] == p1_fsm.STEP_MID_SNAPSHOT
    assert out.get("stub_observe_ticks") == 0
    assert out["stub_last_note"] == "stub_tick: observe -> mid_snapshot (eligible)"


def test_phase1_observe_window_context_preliminary_ok_and_below() -> None:
    """``_phase1_observe_window_context`` mirrors gate window vs min_hours_preliminary."""
    cfg_ok = _minimal_config_dict()
    ctx_ok = run_pipeline._phase1_observe_window_context(cfg_ok)
    assert ctx_ok["observation_gate_hint"] == "preliminary_ok"
    assert ctx_ok["window_hours"] == 168.0
    assert ctx_ok["min_hours_preliminary"] == 48.0

    cfg_low = dict(cfg_ok)
    cfg_low["window"] = {
        "start_ts": "2026-01-01T00:00:00+08:00",
        "end_ts": "2026-01-02T00:00:00+08:00",
    }
    ctx_low = run_pipeline._phase1_observe_window_context(cfg_low)
    assert ctx_low["window_hours"] == 24.0
    assert ctx_low["observation_gate_hint"] == "below_preliminary"


def test_phase1_samples_preliminary_hint_ok_and_below_and_unknown() -> None:
    """``_phase1_samples_preliminary_hint`` mirrors preliminary sample floors."""
    cfg = _minimal_config_dict()
    ok_db = {"finalized_alerts_count": 400, "finalized_true_positives_count": 40}
    assert run_pipeline._phase1_samples_preliminary_hint(cfg, ok_db) == "preliminary_ok"
    low_db = {"finalized_alerts_count": 1, "finalized_true_positives_count": 0}
    assert run_pipeline._phase1_samples_preliminary_hint(cfg, low_db) == "below_preliminary"
    unk: dict = {"collect_errors": [{"code": "E_COLLECT_STATE_DB"}]}
    assert run_pipeline._phase1_samples_preliminary_hint(cfg, unk) == "unknown"


def test_collect_phase1_state_db_observe_counts_returns_counts(tmp_path: Path) -> None:
    """``collect_phase1_state_db_observe_counts`` uses COUNT-only path (no R1/backtest)."""
    state_db = tmp_path / "s.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute("CREATE TABLE validation_results (y INT)")
        conn.execute("INSERT INTO validation_results VALUES (1)")
        conn.commit()
    cfg = _minimal_config_dict()
    cfg["state_db_path"] = str(state_db)
    out = collectors.collect_phase1_state_db_observe_counts(tmp_path, cfg)
    assert out.get("collect_errors") is None
    assert out["finalized_alerts_count"] == 1
    assert out["finalized_true_positives_count"] == 0


def test_phase1_autonomous_observe_context_gate_hints_and_eligible(
    tmp_path: Path,
) -> None:
    """T8C hook: mid_snapshot_eligible when preliminary + gate time/samples all satisfied."""
    state_db = tmp_path / "s.db"
    start_w = "2026-01-01T00:00:00+08:00"
    end_w = "2026-01-08T00:00:00+08:00"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE validation_results (alert_ts TEXT, validated_at TEXT, result INT)"
        )
        for _ in range(5):
            conn.execute(
                "INSERT INTO validation_results VALUES (?, ?, 1)",
                ("2026-01-04T12:00:00+08:00", "2026-01-05T00:00:00+08:00"),
            )
        conn.commit()
    cfg = _minimal_config_dict()
    cfg["state_db_path"] = str(state_db)
    cfg["window"] = {"start_ts": start_w, "end_ts": end_w}
    thr = dict(cfg["thresholds"])
    thr["min_hours_preliminary"] = 1
    thr["min_finalized_alerts_preliminary"] = 2
    thr["min_finalized_true_positives_preliminary"] = 2
    thr["min_hours_gate"] = 1
    thr["min_finalized_alerts_gate"] = 3
    thr["min_finalized_true_positives_gate"] = 3
    cfg["thresholds"] = thr
    ctx = run_pipeline._phase1_autonomous_observe_context(cfg, tmp_path)
    assert ctx["gate_hours_hint"] == "ok"
    assert ctx["gate_sample_hint"] == "ok"
    assert ctx["samples_preliminary_hint"] == "preliminary_ok"
    assert ctx["observation_gate_hint"] == "preliminary_ok"
    assert ctx["mid_snapshot_eligible"] is True


def test_phase1_autonomous_observe_context_mid_snapshot_not_eligible_low_gate_samples(
    tmp_path: Path,
) -> None:
    """Gate sample floor fails → mid_snapshot_eligible False even when preliminary ok."""
    state_db = tmp_path / "s.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute("CREATE TABLE validation_results (y INT)")
        conn.execute("INSERT INTO validation_results VALUES (1)")
        conn.commit()
    cfg = _minimal_config_dict()
    cfg["state_db_path"] = str(state_db)
    thr = dict(cfg["thresholds"])
    thr["min_finalized_alerts_gate"] = 9999
    cfg["thresholds"] = thr
    ctx = run_pipeline._phase1_autonomous_observe_context(cfg, tmp_path)
    assert ctx["gate_sample_hint"] == "below"
    assert ctx["mid_snapshot_eligible"] is False


def test_exit_code_four_five_integer_collision_phase1_vs_phase2_documented() -> None:
    """Same integers as Phase 2 exits 4/5; names disambiguate failing step."""
    assert orch_exits.EXIT_PHASE1_MID_OR_R1_FAILED == phase2_exits.EXIT_RESUME_BUNDLE_LOAD_FAILED
    assert orch_exits.EXIT_PHASE1_BACKTEST_FAILED == phase2_exits.EXIT_PHASE2_RUNNER_SMOKE_FAILED


def test_run_pipeline_phase2_non_empty_overrides_exits_config_invalid(
    tmp_path: Path,
) -> None:
    """T10A: non-empty experiment overrides fail config load before preflight (exit 2)."""
    run_id = "pytest_phase2_bad_overrides"
    raw = _minimal_phase2_dict(
        model_dir="m", state_db="s", pred_db="p", yaml_run_id=run_id
    )
    raw["tracks"]["track_a"]["experiments"][0]["overrides"] = {"illegal": 1}
    cfg_file = tmp_path / "bad_ov.yaml"
    cfg_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
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
            "--dry-run",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == phase2_exits.EXIT_CONFIG_INVALID
    assert "E_CONFIG_INVALID" in proc.stderr or "overrides" in proc.stderr.lower()


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
                extra_writable={"phase2_reports_dir": extra},
            )
    assert out["status"] == "READY"
    assert "writable_phase2_reports_dir" in {c["name"] for c in out["checks"]}
    assert "phase2_reports_dir" in out["artifacts"]


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
            "--skip-phase2-trainer-smoke",
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
    assert data["steps"]["phase2_plan_bundle"]["status"] == "success"
    assert data["steps"]["phase2_runner_smoke"]["status"] == "success"
    bundle_path = _ORCHESTRATOR / "state" / run_id / "phase2_bundle.json"
    assert bundle_path.is_file()
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle.get("bundle_kind") == "phase2_plan_v1"
    assert bundle.get("status") == "plan_only"
    rs = bundle.get("runner_smoke")
    assert isinstance(rs, dict)
    assert rs.get("log_dirs_ok") is True
    assert rs.get("trainer_help_skipped") is True
    assert data.get("phase2_collect", {}).get("plan_experiment_slots", 0) >= 1
    assert data.get("phase2_collect", {}).get("runner_trainer_help_skipped") is True
    assert data["steps"]["phase2_gate_report"]["status"] == "success"
    assert data["phase2_gate_decision"]["status"] == "BLOCKED"
    gate_md = (
        run_pipeline.investigation_reports_subdir(run_id, "phase2")
        / "phase2_gate_decision.md"
    )
    assert gate_md.is_file()
    text = gate_md.read_text(encoding="utf-8")
    assert "phase2_bundle_plan_only_no_track_metrics" in text


def test_evaluate_phase2_gate_plan_only_blocked() -> None:
    """plan_only bundle must not pass Phase 2 gate."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    b = collectors.collect_phase2_plan_bundle("r", cfg)
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "BLOCKED"
    assert "phase2_bundle_plan_only_no_track_metrics" in g["blocking_reasons"]
    assert g.get("conclusion_strength") == "exploratory"
    assert g["metrics"].get("phase2_strategy_effective") is False


def test_evaluate_phase2_gate_t11a_blocks_when_trainer_params_job_missing_fingerprint() -> None:
    """T11A: trainer_params experiments require argv_fingerprint when trainer_jobs ran."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [
                    {
                        "exp_id": "c0",
                        "overrides": {},
                        "trainer_params": {"recent_chunks": 2},
                    },
                ],
            },
        },
        "trainer_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "resolved_trainer_argv": ["py", "-m", "trainer.trainer"],
                },
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "BLOCKED"
    assert "phase2_strategy_params_not_effective" in g["blocking_reasons"]
    assert "T11A strategy audit" in (g.get("evidence_summary") or "")
    assert g.get("conclusion_strength") == "exploratory"


def test_evaluate_phase2_gate_conclusion_strength_decision_grade_with_trainer_jobs_audit() -> None:
    """T11A: PASS + multi-window series + trainer_jobs audit → decision_grade."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.63,
                },
            ],
        },
        "phase2_pat_series_by_experiment": {
            "track_c": {
                "c0": [0.60, 0.6001, 0.5999],
                "c1": [0.63, 0.6302, 0.6298],
            },
        },
        "trainer_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "argv_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaa",
                    "resolved_trainer_argv": ["py", "-m", "trainer.trainer"],
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "ok": True,
                    "argv_fingerprint": "bbbbbbbbbbbbbbbbbbbbbbbb",
                    "resolved_trainer_argv": ["py", "-m", "trainer.trainer"],
                },
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "PASS"
    assert g.get("conclusion_strength") == "decision_grade"


def test_write_phase2_gate_decision_includes_t11a_section(tmp_path: Path) -> None:
    """phase2_gate_decision.md documents conclusion_strength and strategy flags."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    b = collectors.collect_phase2_plan_bundle("r", cfg)
    g = evaluators.evaluate_phase2_gate(b)
    out = tmp_path / "phase2_gate_decision.md"
    report_builder.write_phase2_gate_decision(out, "r", cfg, b, g)
    text = out.read_text(encoding="utf-8")
    assert "## Scientific validity (T11A)" in text
    assert "conclusion_strength" in text
    assert "phase2_strategy_effective" in text


def test_write_phase2_gate_decision_includes_winner_section(tmp_path: Path) -> None:
    """T11A: gate metrics with winner fields produce Winner section in md."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    b: Mapping[str, Any] = {
        "status": "metrics_ingested",
        "bundle_kind": "phase2_plan_v1",
    }
    g: dict[str, Any] = {
        "status": "PASS",
        "blocking_reasons": [],
        "evidence_summary": "synthetic",
        "conclusion_strength": "comparative",
        "metrics": {
            "phase2_strategy_effective": True,
            "phase2_trainer_jobs_executed": True,
            "phase2_strategy_note": "ok",
            "phase2_winner_track": "track_a",
            "phase2_winner_exp_id": "a_win",
            "phase2_winner_baseline_exp_id": "a0",
            "phase2_winner_uplift_pp_vs_baseline": 4.2,
        },
    }
    out = tmp_path / "phase2_gate_decision.md"
    report_builder.write_phase2_gate_decision(out, "r", cfg, b, g)
    text = out.read_text(encoding="utf-8")
    assert "## Winner track / experiment (T11A)" in text
    assert "`track_a`" in text
    assert "`a_win`" in text
    assert "4.2" in text


def test_write_phase2_gate_decision_includes_elimination_section(tmp_path: Path) -> None:
    """T11: gate metrics with phase2_elimination_rows produce elimination section."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    b: Mapping[str, Any] = {"status": "metrics_ingested", "bundle_kind": "phase2_plan_v1"}
    g: dict[str, Any] = {
        "status": "FAIL",
        "blocking_reasons": ["phase2_uplift_below_min_pp_vs_baseline"],
        "evidence_summary": "synthetic",
        "conclusion_strength": "exploratory",
        "metrics": {
            "phase2_strategy_effective": False,
            "phase2_trainer_jobs_executed": False,
            "phase2_elimination_rows": [
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "reason_code": "below_min_uplift_pp_vs_baseline",
                    "detail": "uplift 1.5000 pp < 3.00 pp vs baseline `c0`",
                },
            ],
        },
    }
    out = tmp_path / "phase2_gate_decision.md"
    report_builder.write_phase2_gate_decision(out, "r", cfg, b, g)
    text = out.read_text(encoding="utf-8")
    assert "## Uplift elimination / non-winners (T11 narrative)" in text
    assert "track_c/c1" in text
    assert "below_min_uplift_pp_vs_baseline" in text


def test_adhoc_runbook_documents_phase2_t11a_gate_mechanics() -> None:
    """ADHOC_RUNBOOK §1.8.1 stays aligned with evaluate_phase2_gate keywords (drift guard)."""
    text = _ADHOC_RUNBOOK.read_text(encoding="utf-8")
    assert "#### 1.8.1" in text
    assert "min_pat_windows_for_pass" in text
    assert "phase2_insufficient_pat_windows_for_pass" in text
    assert "merge_phase2_pat_series_from_shared_and_per_job" in text
    assert "conclusion_strength" in text
    assert "phase2_winner_" in text


def test_adhoc_runbook_documents_phase2_error_code_reference() -> None:
    """ADHOC_RUNBOOK §1.8.2 documents E_SUBPROCESS_TIMEOUT vs E_NO_DATA_WINDOW and common codes."""
    text = _ADHOC_RUNBOOK.read_text(encoding="utf-8")
    assert "#### 1.8.2" in text
    assert "E_SUBPROCESS_TIMEOUT" in text
    assert "E_NO_DATA_WINDOW" in text
    assert "E_ARTIFACT_MISSING" in text
    assert "E_PHASE2_BACKTEST_JOBS" in text
    assert "E_PHASE2_PER_JOB_BACKTEST_JOBS" in text
    assert "E_CONFIG_INVALID" in text
    assert "phase2_bundle.json" in text
    assert "phase2_pat_matrix_yaml_experiment_count" in text
    assert "phase2_exit_codes.py" in text
    assert "common_exit_codes.py" in text
    assert "EXIT_PHASE1_MID_OR_R1_FAILED" in text
    assert "EXIT_PHASE1_BACKTEST_FAILED" in text
    assert "EXIT_PHASE1_AUTONOMOUS_PENDING" in text
    assert "autonomous-once" in text
    assert "phase2_runner_smoke" in text


def test_adhoc_runbook_phase23_phase2_example_uses_t10a_trainer_params() -> None:
    """§2.3.1 Phase 2 YAML draft must not revive non-empty overrides (T10A)."""
    text = _ADHOC_RUNBOOK.read_text(encoding="utf-8")
    assert "### 2.3.1" in text
    assert "a_recent_chunks_v1" in text
    assert "trainer_params:" in text
    assert "a_hard_negative_v1" not in text
    assert "hard_negative_weight:" not in text


def test_evaluate_phase2_gate_errors_fail() -> None:
    """Non-empty collector errors in bundle → FAIL."""
    b = {
        "status": "plan_only",
        "errors": [{"code": "E_TEST", "message": "x"}],
        "experiments_index": [],
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "FAIL"
    assert "E_TEST" in g["blocking_reasons"]


def test_evaluate_phase2_gate_unsupported_status_blocked() -> None:
    g = evaluators.evaluate_phase2_gate({"status": "unknown", "errors": []})
    assert g["status"] == "BLOCKED"
    assert "phase2_gate_unsupported_bundle_status" in g["blocking_reasons"]


def test_collect_phase2_plan_bundle_deterministic_json() -> None:
    """Same phase2 config + run_id yields identical canonical JSON (plan_only reproducibility)."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    b1 = collectors.collect_phase2_plan_bundle("same_run", cfg)
    b2 = collectors.collect_phase2_plan_bundle("same_run", cfg)
    s1 = json.dumps(b1, sort_keys=True, separators=(",", ":"), default=str)
    s2 = json.dumps(b2, sort_keys=True, separators=(",", ":"), default=str)
    assert s1 == s2


def test_collect_phase2_plan_bundle_shape() -> None:
    """Plan bundle lists experiments in stable track order."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    b = collectors.collect_phase2_plan_bundle("rid", cfg)
    assert b["bundle_kind"] == "phase2_plan_v1"
    assert b["status"] == "plan_only"
    assert b["run_id"] == "rid"
    tracks = b["tracks"]
    assert tracks["track_a"]["enabled"] is True
    assert len(tracks["track_a"]["experiments"]) == 1
    idx = b["experiments_index"]
    assert all("track" in e and "exp_id" in e for e in idx)
    summ = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert summ["plan_experiment_slots"] == len(idx)
    assert summ["plan_experiments_active"] >= 1
    jobs = b["job_specs"]
    assert isinstance(jobs, list) and len(jobs) == summ["plan_experiments_active"]
    assert summ["job_specs_count"] == len(jobs)
    assert summ.get("job_specs_training_metrics_hint_count") == 0
    orch_rel = _ORCHESTRATOR.relative_to(_REPO_ROOT)
    assert jobs[0]["logs_subdir_relative"].startswith(
        f"{orch_rel.as_posix()}/state/rid/logs/phase2/"
    )


def test_ensure_phase2_job_log_dirs_creates(tmp_path: Path) -> None:
    """Log dir paths from job_specs are created under repo root."""
    orch_rel = _ORCHESTRATOR.relative_to(_REPO_ROOT)
    rel_log = orch_rel / "state" / "rid" / "logs" / "phase2" / "track_a" / "e0"
    specs = [
        {
            "track": "track_a",
            "exp_id": "e0",
            "logs_subdir_relative": rel_log.as_posix(),
        }
    ]
    ok, msg = runner.ensure_phase2_job_log_dirs(tmp_path, specs)
    assert ok, msg
    d = tmp_path / rel_log
    assert d.is_dir()


def test_build_phase2_trainer_argv_skip_optuna() -> None:
    """Argv includes window and --skip-optuna when resources say so."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    argv, unapplied = runner.build_phase2_trainer_argv(
        bundle, track="track_a", exp_id="a0", python_exe="/x/python"
    )
    assert argv[:3] == ["/x/python", "-m", "trainer.trainer"]
    assert "--start" in argv and "--end" in argv
    i0 = argv.index("--start")
    assert argv[i0 + 1] == "2026-01-01T00:00:00+08:00"
    i1 = argv.index("--end")
    assert argv[i1 + 1] == "2026-01-08T00:00:00+08:00"
    assert "--skip-optuna" in argv
    assert unapplied == []


def test_phase2_config_rejects_non_empty_overrides() -> None:
    """T10A: non-empty overrides fail fast at config validation."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"] = [
        {"exp_id": "a0", "overrides": {"legacy_key": 1}},
    ]
    with pytest.raises(config_loader.ConfigValidationError, match="T10A"):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_accepts_integer_like_float_for_recent_chunks() -> None:
    """YAML may parse small integers as float (e.g. 3.0); coerce to int (Review #1)."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"] = [
        {
            "exp_id": "a0",
            "overrides": {},
            "trainer_params": {"recent_chunks": 3.0},
        },
    ]
    out = config_loader.validate_phase2_config(raw, cli_run_id="rid")
    tp = out["tracks"]["track_a"]["experiments"][0]["trainer_params"]
    assert tp["recent_chunks"] == 3.0


def test_phase2_config_rejects_fractional_recent_chunks() -> None:
    """Non-integer floats must not coerce silently."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"] = [
        {
            "exp_id": "a0",
            "overrides": {},
            "trainer_params": {"recent_chunks": 3.1},
        },
    ]
    with pytest.raises(config_loader.ConfigValidationError, match="whole"):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_rejects_unknown_trainer_params_key() -> None:
    """trainer_params keys must stay on the whitelist."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"] = [
        {
            "exp_id": "a0",
            "overrides": {},
            "trainer_params": {"not_whitelisted": 1},
        },
    ]
    with pytest.raises(config_loader.ConfigValidationError, match="unknown keys"):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_build_phase2_trainer_argv_rejects_stale_bundle_overrides() -> None:
    """Stale plan bundles with non-empty overrides must not silently train."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    bundle["tracks"]["track_a"]["experiments"][0]["overrides"] = {"stale": True}
    with pytest.raises(ValueError, match="non-empty overrides"):
        runner.build_phase2_trainer_argv(
            bundle, track="track_a", exp_id="a0", python_exe=sys.executable
        )


def test_build_phase2_trainer_argv_trainer_params_recent_chunks() -> None:
    """trainer_params.recent_chunks maps to --recent-chunks."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    cfg["tracks"]["track_a"]["experiments"] = [
        {
            "exp_id": "a0",
            "overrides": {},
            "trainer_params": {"recent_chunks": 4},
        },
    ]
    cfg = config_loader.validate_phase2_config(cfg, cli_run_id="rid")
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    argv, unapplied = runner.build_phase2_trainer_argv(
        bundle, track="track_a", exp_id="a0", python_exe="/py/bin/python"
    )
    assert unapplied == []
    i = argv.index("--recent-chunks")
    assert argv[i + 1] == "4"
    assert len(runner.phase2_trainer_argv_fingerprint(argv)) == 24


def test_phase2_trainer_argv_fingerprint_ignores_python_executable() -> None:
    """Fingerprint uses argv from ``-m`` onward so exe path does not matter."""
    a = [
        "/usr/bin/python3",
        "-m",
        "trainer.trainer",
        "--start",
        "s",
        "--end",
        "e",
    ]
    b = ["C:\\\\Python\\\\python.exe", "-m", "trainer.trainer", "--start", "s", "--end", "e"]
    assert runner.phase2_trainer_argv_fingerprint(a) == runner.phase2_trainer_argv_fingerprint(b)


def test_collect_phase2_plan_bundle_includes_trainer_params() -> None:
    """Plan bundle snapshots trainer_params for audit."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    cfg["tracks"]["track_a"]["experiments"] = [
        {
            "exp_id": "a0",
            "overrides": {},
            "trainer_params": {"sample_rated": 100, "lgbm_device": "cpu"},
        },
    ]
    cfg = config_loader.validate_phase2_config(cfg, cli_run_id="rid")
    b = collectors.collect_phase2_plan_bundle("rid", cfg)
    tp = b["tracks"]["track_a"]["experiments"][0].get("trainer_params")
    assert isinstance(tp, dict)
    assert tp["lgbm_device"] == "cpu"
    assert tp["sample_rated"] == 100


def test_build_phase2_trainer_argv_use_local_parquet() -> None:
    """resources.trainer_use_local_parquet adds --use-local-parquet."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    cfg["resources"]["trainer_use_local_parquet"] = True
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    argv, _ = runner.build_phase2_trainer_argv(
        bundle, track="track_a", exp_id="a0", python_exe="/py"
    )
    assert "--use-local-parquet" in argv


def test_run_phase2_trainer_jobs_invalid_spec(tmp_path: Path) -> None:
    """Invalid job_spec entries yield ok=False without spawning."""
    bundle = {
        "common": {
            "window": {
                "start_ts": "2026-01-01T00:00:00+08:00",
                "end_ts": "2026-01-02T00:00:00+08:00",
            }
        },
        "resources": {"backtest_skip_optuna": True},
        "tracks": {},
        "job_specs": [
            {"track": "", "exp_id": "e", "logs_subdir_relative": "logs/a"},
        ],
    }
    ok, msg, res = runner.run_phase2_trainer_jobs(tmp_path, bundle)
    assert ok is False
    assert msg and "invalid job_spec" in msg
    assert len(res) == 1
    assert res[0].get("ok") is False
    assert res[0].get("inferred_training_metrics_repo_relative") is None


def test_run_phase2_trainer_jobs_empty_job_specs(tmp_path: Path) -> None:
    """Missing or empty job_specs is a no-op success."""
    ok, msg, res = runner.run_phase2_trainer_jobs(tmp_path, {"job_specs": []})
    assert ok is True and msg is None and res == []
    ok2, msg2, res2 = runner.run_phase2_trainer_jobs(tmp_path, {})
    assert ok2 is True and msg2 is None and res2 == []


def test_infer_training_metrics_repo_relative_from_trainer_logs_stderr(
    tmp_path: Path,
) -> None:
    """Parser finds trainer 'Artifacts saved to' line in stderr tail."""
    bundle_dir = tmp_path / "out" / "models" / "v1"
    bundle_dir.mkdir(parents=True)
    err = tmp_path / "job.stderr.log"
    err.write_text(
        f"noise\nINFO trainer: Artifacts saved to {bundle_dir.resolve()}  (version=abc)\n",
        encoding="utf-8",
    )
    got = runner.infer_training_metrics_repo_relative_from_trainer_logs(
        tmp_path,
        stdout_path=tmp_path / "missing.stdout",
        stderr_path=err,
    )
    assert got == "out/models/v1"


def test_infer_training_metrics_repo_relative_uses_last_match(tmp_path: Path) -> None:
    """When multiple saves appear in the tail, use the last match."""
    old = tmp_path / "out" / "models" / "old"
    new = tmp_path / "out" / "models" / "new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    err = tmp_path / "t.log"
    err.write_text(
        f"A Artifacts saved to {old.resolve()}  (version=o)\n"
        f"B Artifacts saved to {new.resolve()}  (version=n)\n",
        encoding="utf-8",
    )
    got = runner.infer_training_metrics_repo_relative_from_trainer_logs(
        tmp_path,
        stdout_path=tmp_path / "x",
        stderr_path=err,
    )
    assert got == "out/models/new"


def test_infer_training_metrics_repo_relative_outside_repo_returns_none(
    tmp_path: Path,
) -> None:
    """Absolute artifact path outside repo_root yields None."""
    err = tmp_path / "e.log"
    err.write_text(
        "Artifacts saved to /this/path/is/not/under/repo  (version=z)\n",
        encoding="utf-8",
    )
    assert (
        runner.infer_training_metrics_repo_relative_from_trainer_logs(
            tmp_path,
            stdout_path=tmp_path / "s",
            stderr_path=err,
        )
        is None
    )


def test_merge_inferred_training_metrics_paths_into_phase2_bundle_fills_specs(
    tmp_path: Path,
) -> None:
    """Successful trainer results backfill job_specs when YAML did not set path."""
    bundle_dir = tmp_path / "m" / "run"
    bundle_dir.mkdir(parents=True)
    rel = bundle_dir.relative_to(tmp_path).as_posix()
    p2: dict = {
        "job_specs": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "logs_subdir_relative": "logs/a",
            }
        ],
        "trainer_jobs": {
            "results": [
                {
                    "track": "track_a",
                    "exp_id": "a0",
                    "ok": True,
                    "inferred_training_metrics_repo_relative": rel,
                }
            ]
        },
    }
    runner.merge_inferred_training_metrics_paths_into_phase2_bundle(p2, tmp_path)
    assert p2["job_specs"][0]["training_metrics_repo_relative"] == rel


def test_merge_inferred_training_metrics_paths_does_not_override_yaml(
    tmp_path: Path,
) -> None:
    """Explicit training_metrics_repo_relative on job_spec is preserved."""
    p2: dict = {
        "job_specs": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "logs_subdir_relative": "logs/a",
                "training_metrics_repo_relative": "yaml/wins",
            }
        ],
        "trainer_jobs": {
            "results": [
                {
                    "track": "track_a",
                    "exp_id": "a0",
                    "ok": True,
                    "inferred_training_metrics_repo_relative": "infer/loses",
                }
            ]
        },
    }
    runner.merge_inferred_training_metrics_paths_into_phase2_bundle(p2, tmp_path)
    assert p2["job_specs"][0]["training_metrics_repo_relative"] == "yaml/wins"


def test_phase2_bundle_trainer_jobs_skipped_after_pipeline(tmp_path: Path) -> None:
    """Default phase2 run records trainer_jobs.executed=false in bundle."""
    run_id = "pytest_phase2_trainer_jobs_skipped"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg_file = tmp_path / "p2tj.yaml"
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
    bundle_path = _ORCHESTRATOR / "state" / run_id / "phase2_bundle.json"
    for p in (state_json, bundle_path):
        if p.is_file():
            p.unlink()
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
            "--skip-phase2-trainer-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    tj = data.get("trainer_jobs")
    assert isinstance(tj, dict)
    assert tj.get("executed") is False
    assert tj.get("skip_reason")
    assert tj.get("results") == []


def test_phase2_resume_skips_completed_trainer_jobs(tmp_path: Path) -> None:
    """Second --resume should skip phase2_trainer_jobs when step already success."""
    run_id = "pytest_phase2_resume_trainer_skip"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg_file = tmp_path / "p2tjskip.yaml"
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
    argv = [
        sys.executable,
        str(_RUN_PIPELINE),
        "--phase",
        "phase2",
        "--config",
        str(cfg_file),
        "--run-id",
        run_id,
        "--skip-backtest-smoke",
        "--skip-phase2-trainer-smoke",
    ]
    proc1 = subprocess.run(argv, cwd=_REPO_ROOT, capture_output=True, text=True, check=False)
    assert proc1.returncode == 0, proc1.stderr
    t1 = json.loads(state_json.read_text(encoding="utf-8"))["steps"]["phase2_trainer_jobs"][
        "finished_at"
    ]
    proc2 = subprocess.run(
        argv + ["--resume"], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert proc2.returncode == 0, proc2.stderr
    t2 = json.loads(state_json.read_text(encoding="utf-8"))["steps"]["phase2_trainer_jobs"][
        "finished_at"
    ]
    assert t1 == t2


def test_phase2_cfg_to_backtest_cfg_maps_window_and_skip_optuna() -> None:
    """phase2_cfg_to_backtest_cfg feeds run_phase1_backtest."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    bt = run_pipeline.phase2_cfg_to_backtest_cfg(cfg)
    assert bt["model_dir"] == "m"
    assert bt["window"]["start_ts"] == "2026-01-01T00:00:00+08:00"
    assert bt["backtest_skip_optuna"] is True


def test_phase1_mid_snapshot_window_defaults_to_half_window() -> None:
    """Without checkpoints override, Phase 1 mid snapshot uses 50% window end."""
    cfg = _minimal_config_dict()
    w = run_pipeline.phase1_mid_snapshot_window(cfg)
    assert w is not None
    assert w["start_ts"] == "2026-01-01T00:00:00+08:00"
    assert w["end_ts"] == "2026-01-04T12:00:00+08:00"


def test_phase1_mid_snapshot_window_honors_disable_flag() -> None:
    """checkpoints.enable_mid_snapshot=false disables auto mid snapshot."""
    cfg = _minimal_config_dict()
    cfg["checkpoints"] = {"enable_mid_snapshot": False}
    assert run_pipeline.phase1_mid_snapshot_window(cfg) is None


def test_phase1_mid_snapshot_windows_supports_ratio_list() -> None:
    """When midpoint_ratios is set, pipeline should produce sorted unique checkpoints."""
    cfg = _minimal_config_dict()
    cfg["checkpoints"] = {"midpoint_ratios": [0.75, 0.25, 0.25]}
    ws = run_pipeline.phase1_mid_snapshot_windows(cfg)
    assert [w["end_ts"] for w in ws] == [
        "2026-01-02T18:00:00+08:00",
        "2026-01-06T06:00:00+08:00",
    ]


def test_phase1_mid_snapshot_windows_invalid_ratio_list_falls_back_to_single() -> None:
    """All-invalid midpoint_ratios currently fall back to midpoint_ratio/default."""
    cfg = _minimal_config_dict()
    cfg["checkpoints"] = {
        "midpoint_ratios": [-1, 0, 1, 2],  # all invalid for (0,1)
        "midpoint_ratio": 0.25,
    }
    ws = run_pipeline.phase1_mid_snapshot_windows(cfg)
    assert [w["end_ts"] for w in ws] == ["2026-01-02T18:00:00+08:00"]


def test_phase1_mid_snapshot_windows_merges_wallclock_offsets_deduped() -> None:
    """84h from start equals 50% of 7d window — merged list dedupes identical end_ts."""
    cfg = _minimal_config_dict()
    cfg["checkpoints"] = {"midpoint_ratio": 0.5, "wallclock_offsets_hours": [84.0]}
    ws = run_pipeline.phase1_mid_snapshot_windows(cfg)
    assert len(ws) == 1
    assert ws[0]["start_ts"] == "2026-01-01T00:00:00+08:00"
    assert ws[0]["end_ts"] == "2026-01-04T12:00:00+08:00"


def test_phase1_mid_snapshot_windows_wallclock_only_sorted() -> None:
    """ratio_midpoints_enabled false yields only wall-clock checkpoints, sorted."""
    cfg = _minimal_config_dict()
    cfg["checkpoints"] = {
        "ratio_midpoints_enabled": False,
        "wallclock_offsets_hours": [48.0, 6.0, 24.0],
    }
    ws = run_pipeline.phase1_mid_snapshot_windows(cfg)
    assert [w["end_ts"] for w in ws] == [
        "2026-01-01T06:00:00+08:00",
        "2026-01-02T00:00:00+08:00",
        "2026-01-03T00:00:00+08:00",
    ]


def test_phase1_config_wallclock_offsets_hours_must_be_list() -> None:
    """checkpoints.wallclock_offsets_hours rejects non-list."""
    raw = _minimal_config_dict()
    raw["checkpoints"] = {"wallclock_offsets_hours": "6"}  # type: ignore[assignment]
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="wallclock_offsets_hours must be a list",
    ):
        config_loader.validate_phase1_config(raw)


def test_phase1_config_wallclock_offsets_hours_entry_must_be_positive_finite() -> None:
    """Each wallclock offset must be a finite positive number."""
    raw = _minimal_config_dict()
    raw["checkpoints"] = {"wallclock_offsets_hours": [0.0]}
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="wallclock_offsets_hours\\[0\\]",
    ):
        config_loader.validate_phase1_config(raw)


def test_phase1_config_ratio_midpoints_enabled_must_be_bool() -> None:
    """checkpoints.ratio_midpoints_enabled rejects non-bool."""
    raw = _minimal_config_dict()
    raw["checkpoints"] = {"ratio_midpoints_enabled": "yes"}  # type: ignore[assignment]
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="ratio_midpoints_enabled must be bool",
    ):
        config_loader.validate_phase1_config(raw)


def test_phase1_config_wallclock_offsets_hours_max_length() -> None:
    """At most 64 wall-clock checkpoint entries."""
    raw = _minimal_config_dict()
    raw["checkpoints"] = {"wallclock_offsets_hours": [1.0] * 65}
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="at most 64 entries",
    ):
        config_loader.validate_phase1_config(raw)


def test_phase1_mid_snapshot_window_invalid_bounds_returns_none() -> None:
    """Invalid or zero-length windows must disable auto mid snapshot."""
    cfg = _minimal_config_dict()
    cfg["window"] = {
        "start_ts": "2026-01-08T00:00:00+08:00",
        "end_ts": "2026-01-08T00:00:00+08:00",
    }
    assert run_pipeline.phase1_mid_snapshot_window(cfg) is None

    cfg2 = _minimal_config_dict()
    cfg2["window"] = {
        "start_ts": "2026-01-09T00:00:00+08:00",
        "end_ts": "2026-01-08T00:00:00+08:00",
    }
    assert run_pipeline.phase1_mid_snapshot_window(cfg2) is None


def test_phase1_config_checkpoints_type_validation() -> None:
    """Phase1 checkpoints schema must reject wrong primitive types."""
    raw = _minimal_config_dict()
    raw["checkpoints"] = {"enable_mid_snapshot": "yes"}  # type: ignore[assignment]
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="checkpoints.enable_mid_snapshot must be bool",
    ):
        config_loader.validate_phase1_config(raw)

    raw2 = _minimal_config_dict()
    raw2["checkpoints"] = {"midpoint_ratio": "bad"}  # type: ignore[assignment]
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="checkpoints.midpoint_ratio must be numeric",
    ):
        config_loader.validate_phase1_config(raw2)

    raw3 = _minimal_config_dict()
    raw3["checkpoints"] = {"midpoint_ratios": []}  # type: ignore[assignment]
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="checkpoints.midpoint_ratios must be a non-empty list",
    ):
        config_loader.validate_phase1_config(raw3)

    raw4 = _minimal_config_dict()
    raw4["checkpoints"] = {"midpoint_ratios": [0.3, "bad"]}  # type: ignore[list-item]
    with pytest.raises(
        config_loader.ConfigValidationError,
        match=r"checkpoints.midpoint_ratios\[1\] must be numeric",
    ):
        config_loader.validate_phase1_config(raw4)


def test_run_phase1_r1_r6_all_mid_window_override_and_log_stem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid snapshot run must use overridden window and r1_r6_mid log stem."""
    captured: list[list[str]] = []
    captured_stems: list[str] = []

    def fake_logged(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        captured.append(list(argv))
        captured_stems.append(str(kwargs.get("log_stem") or ""))
        log_dir = kwargs["log_dir"]
        stem = str(kwargs["log_stem"])
        return {
            "ok": True,
            "returncode": 0,
            "stdout_path": log_dir / f"{stem}.stdout.log",
            "stderr_path": log_dir / f"{stem}.stderr.log",
            "stdout_text": "",
            "stderr_text": "",
            "combined_text": "",
            "error_code": None,
            "message": None,
        }

    monkeypatch.setattr(runner, "run_logged_command", fake_logged)
    (tmp_path / "m").mkdir()
    script = tmp_path / "dummy_r1.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    cfg = {
        "model_dir": "m",
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-08T00:00:00+08:00",
        },
        "state_db_path": "s.db",
        "prediction_log_db_path": "p.db",
        "r1_r6_script": str(script),
    }
    log_d = tmp_path / "logs"
    log_d.mkdir()
    mid_w = {
        "start_ts": "2026-01-01T00:00:00+08:00",
        "end_ts": "2026-01-03T00:00:00+08:00",
    }
    res = runner.run_phase1_r1_r6_all(
        tmp_path,
        cfg,
        log_d,
        window_override=mid_w,
        log_stem="r1_r6_mid",
    )
    assert res.get("ok") is True
    argv = captured[0]
    assert argv[argv.index("--start-ts") + 1] == mid_w["start_ts"]
    assert argv[argv.index("--end-ts") + 1] == mid_w["end_ts"]
    assert captured_stems == ["r1_r6_mid"]


def test_run_phase1_backtest_includes_output_dir_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional cfg backtest_output_dir maps to trainer.backtester --output-dir."""
    captured: list[list[str]] = []

    def fake_logged(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        captured.append(list(argv))
        log_dir = kwargs["log_dir"]
        return {
            "ok": True,
            "returncode": 0,
            "stdout_path": log_dir / "backtest.stdout.log",
            "stderr_path": log_dir / "backtest.stderr.log",
            "stdout_text": "",
            "stderr_text": "",
            "combined_text": "",
            "error_code": None,
            "message": None,
        }

    monkeypatch.setattr(runner, "run_logged_command", fake_logged)
    (tmp_path / "m").mkdir()
    out_d = tmp_path / "bt_out"
    out_d.mkdir()
    cfg = {
        "model_dir": "m",
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": "2026-01-08T00:00:00+08:00",
        },
        "state_db_path": "s.db",
        "prediction_log_db_path": "p.db",
        "backtest_skip_optuna": True,
        "backtest_output_dir": str(out_d),
    }
    log_d = tmp_path / "logs"
    log_d.mkdir()
    res = runner.run_phase1_backtest(tmp_path, cfg, log_d)
    assert res.get("ok") is True
    argv = captured[0]
    idx = argv.index("--output-dir")
    assert Path(argv[idx + 1]).resolve() == out_d.resolve()


def test_phase2_shared_backtest_logs_subdir_relative() -> None:
    """Shared backtest logs live under investigation orchestrator state."""
    rel = collectors.phase2_shared_backtest_logs_subdir_relative("my_run")
    orch_rel = _ORCHESTRATOR.relative_to(_REPO_ROOT)
    assert rel == f"{orch_rel.as_posix()}/state/my_run/logs/phase2/_shared_backtest"


def test_run_phase2_example_yaml_documents_phase2_gate_contract() -> None:
    """Example run_phase2.yaml keeps gate keys and per-job / std bundle hints documented."""
    p = (
        _REPO_ROOT
        / "investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml"
    )
    text = p.read_text(encoding="utf-8")
    assert "min_uplift_pp_vs_baseline:" in text
    assert "max_std_pp_across_windows:" in text
    assert "phase2_pat_series_by_experiment" in text
    assert "_per_job_backtest" in text
    assert "evaluate_phase2_gate" in text


def test_plan_precision_uplift_sprint_phase2_gate_orchestrator_bridge() -> None:
    """Sprint plan documents how Phase 2 Gate maps to the investigation orchestrator."""
    plan = _REPO_ROOT / ".cursor/plans/PLAN_precision_uplift_sprint.md"
    text = plan.read_text(encoding="utf-8")
    assert "evaluate_phase2_gate" in text
    assert "min_uplift_pp_vs_baseline" in text
    assert "max_std_pp_across_windows" in text


def test_phase2_per_job_backtest_metrics_repo_relative() -> None:
    """Per-job metrics JSON sits under _per_job_backtest next to subprocess logs."""
    orch_rel = _ORCHESTRATOR.relative_to(_REPO_ROOT)
    rel = collectors.phase2_per_job_backtest_metrics_repo_relative(
        "r1", "track_a", "exp0"
    )
    assert rel.endswith("/_per_job_backtest/backtest_metrics.json")
    assert rel.startswith(f"{orch_rel.as_posix()}/state/r1/logs/phase2/track_a/exp0")


def test_load_json_under_repo_ok(tmp_path: Path) -> None:
    """load_json_under_repo reads JSON object under repo root."""
    p = tmp_path / "sub" / "a.json"
    p.parent.mkdir(parents=True)
    p.write_text('{"k": 1}', encoding="utf-8")
    obj, err = collectors.load_json_under_repo(tmp_path, "sub/a.json")
    assert err is None and obj == {"k": 1}


def test_load_json_under_repo_missing(tmp_path: Path) -> None:
    obj, err = collectors.load_json_under_repo(tmp_path, "nope.json")
    assert obj is None and err and "file not found" in err


def test_harvest_phase2_job_training_metrics_empty_specs(tmp_path: Path) -> None:
    """Non-list job_specs yields no harvest rows."""
    assert collectors.harvest_phase2_job_training_metrics(tmp_path, {"job_specs": None}) == []


def test_harvest_phase2_job_training_metrics_missing_logs_subdir(tmp_path: Path) -> None:
    """Job spec without logs_subdir_relative records load_error."""
    bundle = {"job_specs": [{"track": "track_a", "exp_id": "a0"}]}
    rows = collectors.harvest_phase2_job_training_metrics(tmp_path, bundle)
    assert len(rows) == 1
    assert rows[0]["found"] is False
    assert "logs_subdir_relative" in (rows[0].get("load_error") or "")


def test_harvest_phase2_job_training_metrics_found_and_invalid_json(tmp_path: Path) -> None:
    """Harvest loads valid JSON and surfaces invalid JSON as not found."""
    d_ok = tmp_path / "logs" / "track_a" / "a0"
    d_ok.mkdir(parents=True)
    (d_ok / "training_metrics.json").write_text('{"auc": 0.9}', encoding="utf-8")
    rel_ok = d_ok.relative_to(tmp_path).as_posix()

    d_bad = tmp_path / "logs" / "track_b" / "b0"
    d_bad.mkdir(parents=True)
    (d_bad / "training_metrics.json").write_text("{", encoding="utf-8")
    rel_bad = d_bad.relative_to(tmp_path).as_posix()

    bundle = {
        "job_specs": [
            {"track": "track_a", "exp_id": "a0", "logs_subdir_relative": rel_ok},
            {"track": "track_b", "exp_id": "b0", "logs_subdir_relative": rel_bad},
        ]
    }
    rows = collectors.harvest_phase2_job_training_metrics(tmp_path, bundle)
    assert len(rows) == 2
    assert rows[0]["found"] is True
    assert rows[0]["training_metrics"] == {"auc": 0.9}
    assert rows[1]["found"] is False
    assert rows[1]["training_metrics"] is None
    assert "invalid JSON" in (rows[1].get("load_error") or "")


def test_trainer_artifacts_saved_log_line_contract_for_orchestrator() -> None:
    """Trainer save_artifact_bundle must keep logger.info shape expected by runner regex."""
    trainer_py = _REPO_ROOT / "trainer" / "training" / "trainer.py"
    src = trainer_py.read_text(encoding="utf-8")
    assert runner.TRAINER_ARTIFACTS_SAVED_LOGGER_INFO_FORMAT in src


def test_collect_summary_phase2_plan_counts_training_metrics_hints() -> None:
    """job_specs_training_metrics_hint_count reflects non-empty training_metrics_repo_relative."""
    b: dict = {
        "bundle_kind": "phase2_plan_v1",
        "experiments_index": [],
        "job_specs": [
            {"track": "track_a", "exp_id": "e1"},
            {
                "track": "track_b",
                "exp_id": "e2",
                "training_metrics_repo_relative": "out/models/run1",
            },
        ],
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert s.get("job_specs_training_metrics_hint_count") == 1


def test_collect_summary_phase2_plan_includes_job_training_harvest() -> None:
    """phase2_collect summary counts harvest rows and found files."""
    b: dict = {
        "bundle_kind": "phase2_plan_v1",
        "tracks": {"track_a": {"enabled": True}},
        "job_specs": [{"exp_id": "a0", "track": "track_a"}],
        "job_training_harvest": {
            "rows": [{"found": True}, {"found": False}, {"found": True}],
        },
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert s.get("job_training_harvest_rows") == 3
    assert s.get("job_training_harvest_found") == 2


def test_collect_summary_phase2_plan_includes_per_job_backtest_jobs() -> None:
    """phase2_collect summary exposes per_job_backtest_jobs execution flags."""
    b: dict = {
        "bundle_kind": "phase2_plan_v1",
        "job_specs": [],
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [{"ok": True}, {"ok": True}],
        },
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert s.get("per_job_backtest_jobs_executed") is True
    assert s.get("per_job_backtest_jobs_all_ok") is True
    assert s.get("per_job_backtest_jobs_count") == 2


def test_append_phase2_errors_for_failed_per_job_backtests_appends_artifact_missing(
    tmp_path: Path,
) -> None:
    """Non-skipped failed per-job rows append ``E_ARTIFACT_MISSING`` (T10 fail-fast)."""
    mrel = "state/rid/logs/x/_per_job_backtest/backtest_metrics.json"
    p2: dict[str, Any] = {"errors": []}
    rows: list[dict[str, Any]] = [
        {"track": "track_a", "exp_id": "a0", "skipped": True, "ok": True},
        {
            "track": "track_a",
            "exp_id": "a1",
            "skipped": False,
            "ok": False,
            "metrics_load_error": "ENOENT",
            "metrics_repo_relative": mrel,
        },
        {"track": "track_a", "exp_id": "a2", "skipped": False, "ok": True},
    ]
    run_pipeline._append_phase2_errors_for_failed_per_job_backtests(
        p2, rows, repo_root=tmp_path
    )
    errs = p2["errors"]
    assert len(errs) == 1
    assert errs[0]["code"] == "E_ARTIFACT_MISSING"
    assert "track_a/a1" in str(errs[0]["message"])
    assert errs[0]["path"] == str((tmp_path / mrel).resolve())


def test_evaluate_phase2_gate_fails_on_e_no_data_window_in_errors() -> None:
    """Pipeline-injected ``E_NO_DATA_WINDOW`` must surface as gate FAIL (collector errors)."""
    b: dict[str, Any] = {
        "status": "metrics_ingested",
        "errors": [
            {
                "code": "E_NO_DATA_WINDOW",
                "message": "missing PAT",
                "path": "trainer/out_backtest/backtest_metrics.json",
            }
        ],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "FAIL"
    assert "E_NO_DATA_WINDOW" in g["blocking_reasons"]


def test_model_bundle_dir_from_training_metrics_hint_file_and_directory(
    tmp_path: Path,
) -> None:
    """Hint may be training_metrics.json path or bundle directory."""
    d = tmp_path / "bundle"
    d.mkdir()
    tm = d / "training_metrics.json"
    tm.write_text("{}", encoding="utf-8")
    rel_tm = tm.relative_to(tmp_path).as_posix()
    rel_d = d.relative_to(tmp_path).as_posix()
    p1, e1 = collectors.model_bundle_dir_from_training_metrics_hint(tmp_path, rel_tm)
    assert e1 is None and p1 == d.resolve()
    p2, e2 = collectors.model_bundle_dir_from_training_metrics_hint(tmp_path, rel_d)
    assert e2 is None and p2 == d.resolve()


def test_run_phase2_per_job_backtests_skips_without_hint(tmp_path: Path) -> None:
    """Jobs with no training_metrics_repo_relative are skipped and do not fail the batch."""
    bundle = {
        "job_specs": [
            {"track": "track_a", "exp_id": "a0"},
            {"track": "track_b", "exp_id": "b0", "training_metrics_repo_relative": ""},
        ],
        "resources": {},
    }
    template = {
        "model_dir": "common",
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "backtest_skip_optuna": True,
    }
    ok, err, rows = runner.run_phase2_per_job_backtests(
        tmp_path, bundle, template, run_id="rid"
    )
    assert ok and err is None
    assert len(rows) == 2
    assert all(r.get("skipped") for r in rows)


def test_run_phase2_per_job_backtests_resolves_model_dir_and_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-job backtest uses resolved bundle dir and records PAT@1% preview from metrics JSON."""
    bundle_dir = tmp_path / "mbundle"
    bundle_dir.mkdir()
    (bundle_dir / "training_metrics.json").write_text("{}", encoding="utf-8")
    hint = bundle_dir.relative_to(tmp_path).as_posix()
    bundle = {
        "job_specs": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "training_metrics_repo_relative": hint,
            }
        ],
        "resources": {},
    }
    template = {
        "model_dir": "ignored",
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "backtest_skip_optuna": True,
    }
    captured: list[str] = []
    captured_out: list[str | None] = []
    rel_log = collectors.phase2_per_job_backtest_logs_subdir_relative(
        "rid", "track_a", "a0"
    )
    expect_out = str((tmp_path / rel_log).resolve())

    def fake_bt(
        repo_root: Path,
        cfg: Mapping[str, Any],
        log_dir: Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured.append(str(Path(cfg["model_dir"]).resolve()))
        captured_out.append(cfg.get("backtest_output_dir"))
        return {
            "ok": True,
            "returncode": 0,
            "stdout_path": log_dir / "backtest.stdout.log",
            "stderr_path": log_dir / "backtest.stderr.log",
        }

    monkeypatch.setattr(runner, "run_phase1_backtest", fake_bt)

    seen_metrics_paths: list[str] = []

    def fake_load(
        repo_root: Path, rel_path: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        seen_metrics_paths.append(rel_path)
        return (
            {
                "model_default": {
                    "test_precision_at_recall_0.01": 0.42,
                    "test_precision_at_recall_0.01_by_window": [0.4, 0.42, 0.41],
                    "test_precision_at_recall_0.01_window_ids": ["w0", "w1", "w2"],
                }
            },
            None,
        )

    monkeypatch.setattr(collectors, "load_json_under_repo", fake_load)
    ok, err, rows = runner.run_phase2_per_job_backtests(
        tmp_path, bundle, template, run_id="rid", python_exe=sys.executable
    )
    assert ok and err is None
    assert len(rows) == 1
    assert captured == [str(bundle_dir.resolve())]
    assert captured_out == [expect_out]
    expect_m = collectors.phase2_per_job_backtest_metrics_repo_relative(
        "rid", "track_a", "a0"
    )
    assert seen_metrics_paths == [expect_m]
    assert rows[0].get("metrics_repo_relative") == expect_m
    assert rows[0].get("shared_precision_at_recall_1pct_preview") == pytest.approx(0.42)
    assert rows[0].get("precision_at_recall_1pct_by_window_preview") == [0.4, 0.42, 0.41]
    assert rows[0].get("precision_at_recall_1pct_window_ids_preview") == ["w0", "w1", "w2"]


def test_run_phase2_per_job_backtests_fails_when_metrics_missing_pat_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loaded backtest_metrics without parseable PAT@1% must fail (aligned with shared ingest)."""
    bundle_dir = tmp_path / "mbundle"
    bundle_dir.mkdir()
    (bundle_dir / "training_metrics.json").write_text("{}", encoding="utf-8")
    hint = bundle_dir.relative_to(tmp_path).as_posix()
    bundle = {
        "job_specs": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "training_metrics_repo_relative": hint,
            }
        ],
        "resources": {},
    }
    template = {
        "model_dir": "ignored",
        "window": {"start_ts": "2026-01-01T00:00:00+08:00", "end_ts": "2026-01-08T00:00:00+08:00"},
        "backtest_skip_optuna": True,
    }

    def fake_bt(
        repo_root: Path,
        cfg: Mapping[str, Any],
        log_dir: Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "returncode": 0,
            "stdout_path": log_dir / "backtest.stdout.log",
            "stderr_path": log_dir / "backtest.stderr.log",
        }

    monkeypatch.setattr(runner, "run_phase1_backtest", fake_bt)

    def fake_load(
        repo_root: Path, rel_path: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        return ({"precision": 1.0}, None)

    monkeypatch.setattr(collectors, "load_json_under_repo", fake_load)
    ok, err, rows = runner.run_phase2_per_job_backtests(
        tmp_path, bundle, template, run_id="rid", python_exe=sys.executable
    )
    assert not ok
    assert err and "model_default" in err
    assert len(rows) == 1
    assert rows[0].get("ok") is False
    assert rows[0].get("ingest_error_code") == "E_NO_DATA_WINDOW"
    assert rows[0].get("metrics_load_error")
    assert "test_precision_at_recall_0.01" in str(rows[0].get("metrics_load_error"))


def test_append_phase2_errors_for_failed_per_job_backtests_respects_ingest_error_code(
    tmp_path: Path,
) -> None:
    """Rows with ingest_error_code E_NO_DATA_WINDOW produce matching bundle errors."""
    mrel = "state/rid/x/_per_job_backtest/backtest_metrics.json"
    p2: dict[str, Any] = {"errors": []}
    rows: list[dict[str, Any]] = [
        {
            "track": "track_a",
            "exp_id": "a0",
            "skipped": False,
            "ok": False,
            "ingest_error_code": "E_NO_DATA_WINDOW",
            "metrics_load_error": "backtest_metrics lacks parseable model_default.x",
            "metrics_repo_relative": mrel,
        },
    ]
    run_pipeline._append_phase2_errors_for_failed_per_job_backtests(
        p2, rows, repo_root=tmp_path
    )
    assert len(p2["errors"]) == 1
    assert p2["errors"][0]["code"] == "E_NO_DATA_WINDOW"
    assert "track_a/a0" in str(p2["errors"][0]["message"])


def test_phase2_config_training_metrics_repo_relative_empty_raises() -> None:
    """Whitespace-only training_metrics_repo_relative must fail validation."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"][0]["training_metrics_repo_relative"] = "   "
    with pytest.raises(
        config_loader.ConfigValidationError, match="training_metrics_repo_relative"
    ):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_training_metrics_repo_relative_non_string_raises() -> None:
    """training_metrics_repo_relative must be a string when set."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"][0]["training_metrics_repo_relative"] = 1
    with pytest.raises(
        config_loader.ConfigValidationError, match="training_metrics_repo_relative"
    ):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_precision_at_recall_by_window_empty_raises() -> None:
    """precision_at_recall_1pct_by_window must be non-empty list when set."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"][0][
        "precision_at_recall_1pct_by_window"
    ] = []
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="precision_at_recall_1pct_by_window",
    ):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_phase2_config_precision_at_recall_by_window_non_numeric_raises() -> None:
    """precision_at_recall_1pct_by_window entries must be numeric."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"][0][
        "precision_at_recall_1pct_by_window"
    ] = [0.5, "x"]
    with pytest.raises(
        config_loader.ConfigValidationError,
        match="precision_at_recall_1pct_by_window\\[1\\]",
    ):
        config_loader.validate_phase2_config(raw, cli_run_id="rid")


def test_collect_phase2_plan_bundle_propagates_training_metrics_repo_relative() -> None:
    """Optional experiment path appears on job_specs and tracks snapshot."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"][0][
        "training_metrics_repo_relative"
    ] = "out/models/run1"
    cfg = config_loader.validate_phase2_config(raw, cli_run_id="rid")
    b = collectors.collect_phase2_plan_bundle("rid", cfg)
    spec_a0 = next(s for s in b["job_specs"] if s.get("exp_id") == "a0")
    assert spec_a0.get("training_metrics_repo_relative") == "out/models/run1"
    exp0 = b["tracks"]["track_a"]["experiments"][0]
    assert exp0.get("training_metrics_repo_relative") == "out/models/run1"
    summ = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert summ.get("job_specs_training_metrics_hint_count") == 1


def test_collect_phase2_plan_bundle_propagates_precision_at_recall_by_window() -> None:
    """Optional per-experiment PAT@1% series fills bundle phase2_pat_series_by_experiment."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"][0][
        "precision_at_recall_1pct_by_window"
    ] = [0.5, 0.51, 0.505]
    cfg = config_loader.validate_phase2_config(raw, cli_run_id="rid")
    b = collectors.collect_phase2_plan_bundle("rid", cfg)
    exp0 = b["tracks"]["track_a"]["experiments"][0]
    assert exp0.get("precision_at_recall_1pct_by_window") == [
        0.5,
        0.51,
        0.505,
    ]
    assert b["phase2_pat_series_by_experiment"]["track_a"]["a0"] == [
        0.5,
        0.51,
        0.505,
    ]


def test_build_phase2_pat_series_from_plan_tracks_coerces_numeric() -> None:
    """Plan helper skips non-coercible list elements (config should already validate)."""
    tracks = {
        "track_a": {
            "enabled": True,
            "experiments": [
                {"exp_id": "a0", "precision_at_recall_1pct_by_window": [0.1, 0.2]},
            ],
        },
    }
    got = collectors.build_phase2_pat_series_from_plan_tracks(tracks)
    assert got["track_a"]["a0"] == [0.1, 0.2]


def test_harvest_prefers_training_metrics_repo_relative_over_logs(tmp_path: Path) -> None:
    """YAML hint wins over logs_subdir_relative/training_metrics.json."""
    log_dir = tmp_path / "logs" / "track_a" / "a0"
    log_dir.mkdir(parents=True)
    (log_dir / "training_metrics.json").write_text('{"from": "logs"}', encoding="utf-8")
    alt_dir = tmp_path / "out" / "models" / "run1"
    alt_dir.mkdir(parents=True)
    (alt_dir / "training_metrics.json").write_text('{"from": "hint"}', encoding="utf-8")
    rel_log = log_dir.relative_to(tmp_path).as_posix()
    rel_hint = alt_dir.relative_to(tmp_path).as_posix()
    bundle = {
        "job_specs": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "logs_subdir_relative": rel_log,
                "training_metrics_repo_relative": rel_hint,
            }
        ]
    }
    rows = collectors.harvest_phase2_job_training_metrics(tmp_path, bundle)
    assert len(rows) == 1
    assert rows[0]["found"] is True
    assert rows[0]["training_metrics"] == {"from": "hint"}


def test_harvest_training_metrics_repo_relative_file_path(tmp_path: Path) -> None:
    """Hint may point directly at training_metrics.json."""
    f = tmp_path / "bundle" / "training_metrics.json"
    f.parent.mkdir(parents=True)
    f.write_text('{"direct": true}', encoding="utf-8")
    rel = f.relative_to(tmp_path).as_posix()
    bundle = {
        "job_specs": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "logs_subdir_relative": "noop/logs",
                "training_metrics_repo_relative": rel,
            }
        ]
    }
    rows = collectors.harvest_phase2_job_training_metrics(tmp_path, bundle)
    assert rows[0]["found"] and rows[0]["training_metrics"] == {"direct": True}


def test_harvest_training_metrics_repo_relative_rejects_escape(tmp_path: Path) -> None:
    """Path must stay under repo root after resolve."""
    bundle = {
        "job_specs": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "logs_subdir_relative": "l/a",
                "training_metrics_repo_relative": "..",
            }
        ]
    }
    rows = collectors.harvest_phase2_job_training_metrics(tmp_path, bundle)
    assert rows[0]["found"] is False
    assert "escapes" in (rows[0].get("load_error") or "").lower()


def test_harvest_training_metrics_repo_relative_rejects_absolute(tmp_path: Path) -> None:
    """Disallowed paths (absolute or outside repo) are rejected before load."""
    bundle = {
        "job_specs": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "logs_subdir_relative": "l/a",
                "training_metrics_repo_relative": "/etc/passwd",
            }
        ]
    }
    rows = collectors.harvest_phase2_job_training_metrics(tmp_path, bundle)
    assert rows[0]["found"] is False
    err = (rows[0].get("load_error") or "").lower()
    assert "absolute" in err or "escapes" in err


def test_evaluate_phase2_gate_metrics_ingested_blocked() -> None:
    """metrics_ingested bundle is BLOCKED until per-track uplift can be evaluated."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"precision": 0.5},
        "bundle_kind": "phase2_plan_v1",
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "BLOCKED"
    assert "phase2_shared_metrics_no_per_track_uplift" in g["blocking_reasons"]


def test_extract_phase2_shared_precision_at_recall_1pct() -> None:
    """Extractor reads model_default.test_precision_at_recall_0.01."""
    m = {"model_default": {"test_precision_at_recall_0.01": 0.42}}
    assert evaluators.extract_phase2_shared_precision_at_recall_1pct(m) == pytest.approx(
        0.42
    )
    assert evaluators.extract_phase2_shared_precision_at_recall_1pct({}) is None
    assert evaluators.extract_phase2_shared_precision_at_recall_1pct(None) is None


def test_evaluate_phase2_gate_metrics_ingested_includes_pat_in_evidence() -> None:
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.55}},
        "bundle_kind": "phase2_plan_v1",
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["metrics"].get("shared_precision_at_recall_1pct") == pytest.approx(0.55)
    assert "0.5500" in (g.get("evidence_summary") or "")


def test_phase2_per_job_backtest_metrics_normalizes_rows() -> None:
    """phase2_per_job_backtest_metrics coerces previews and counts numeric entries."""
    b: dict = {
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_a",
                    "exp_id": "a0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.33,
                },
                {
                    "track": "track_b",
                    "exp_id": "b0",
                    "skipped": True,
                    "ok": True,
                },
            ],
        }
    }
    m = evaluators.phase2_per_job_backtest_metrics(b)
    assert m.get("per_job_backtest_preview_count") == 1
    prev = m.get("per_job_backtest_previews")
    assert isinstance(prev, list) and len(prev) == 2
    assert prev[0].get("shared_precision_at_recall_1pct_preview") == pytest.approx(0.33)
    assert prev[1].get("skipped") is True


def test_evaluate_phase2_gate_plan_only_includes_per_job_preview_evidence() -> None:
    """plan_only gate evidence lists per-job PAT@1% previews when batch ran."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    b = collectors.collect_phase2_plan_bundle("r", cfg)
    b["per_job_backtest_jobs"] = {
        "executed": True,
        "all_ok": True,
        "results": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "skipped": False,
                "ok": True,
                "shared_precision_at_recall_1pct_preview": 0.41,
            },
        ],
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "BLOCKED"
    assert g["metrics"].get("per_job_backtest_preview_count") == 1
    ev = g.get("evidence_summary") or ""
    assert "per-job PAT@1%" in ev
    assert "track_a/a0=" in ev
    assert "0.4100" in ev


def test_evaluate_phase2_gate_metrics_ingested_includes_per_job_preview_evidence() -> None:
    """metrics_ingested + two per-job previews + dual-window series → PASS + winner (T11A)."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.63,
                },
            ],
        },
        "phase2_pat_series_by_experiment": {
            "track_c": {"c0": [0.60, 0.601], "c1": [0.63, 0.631]},
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "PASS"
    assert g["blocking_reasons"] == []
    ev = g.get("evidence_summary") or ""
    assert "0.5000" in ev
    assert "track_c/c0=" in ev
    assert "uplift gate: PASS" in ev
    assert "dual-window gate:" in ev
    assert "uplift winner:" in ev
    assert g["metrics"].get("per_job_backtest_preview_count") == 2
    assert g["metrics"].get("phase2_uplift_pass") is True
    assert g["metrics"].get("phase2_winner_track") == "track_c"
    assert g["metrics"].get("phase2_winner_exp_id") == "c1"
    assert g["metrics"].get("phase2_winner_baseline_exp_id") == "c0"
    assert g["metrics"].get("phase2_winner_uplift_pp_vs_baseline") == pytest.approx(3.0)
    assert g["metrics"].get("phase2_pat_windows_max") == 2


def test_evaluate_phase2_gate_dual_window_blocks_when_no_pat_series() -> None:
    """T11A: uplift PASS but max PAT series length < min_pat_windows_for_pass → BLOCKED."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.63,
                },
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "BLOCKED"
    assert "phase2_insufficient_pat_windows_for_pass" in g["blocking_reasons"]
    assert g["metrics"].get("phase2_winner_track") == "track_c"
    assert g["metrics"].get("phase2_pat_windows_max") == 0
    assert g["metrics"].get("phase2_pat_windows_required") == 2


def test_evaluate_phase2_gate_min_pat_windows_zero_disables_dual_window_check() -> None:
    """gate.min_pat_windows_for_pass <= 0 skips dual-window hard gate."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {
            "min_uplift_pp_vs_baseline": 3.0,
            "max_std_pp_across_windows": 2.5,
            "min_pat_windows_for_pass": 0,
        },
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.63,
                },
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "PASS"
    assert "dual-window gate:" not in (g.get("evidence_summary") or "")


def test_evaluate_phase2_gate_winner_tiebreak_prefers_track_a_over_track_b() -> None:
    """Same uplift_pp on two tracks → track_a wins (deterministic tie-break)."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {
            "min_uplift_pp_vs_baseline": 3.0,
            "max_std_pp_across_windows": 50.0,
        },
        "tracks": {
            "track_a": {
                "enabled": True,
                "experiments": [{"exp_id": "a0"}, {"exp_id": "a1"}],
            },
            "track_b": {
                "enabled": True,
                "experiments": [{"exp_id": "b0"}, {"exp_id": "b1"}],
            },
            "track_c": {"enabled": False, "experiments": []},
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_a",
                    "exp_id": "a0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.5,
                },
                {
                    "track": "track_a",
                    "exp_id": "a1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.56,
                },
                {
                    "track": "track_b",
                    "exp_id": "b0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.5,
                },
                {
                    "track": "track_b",
                    "exp_id": "b1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.56,
                },
            ],
        },
        "phase2_pat_series_by_experiment": {
            "track_a": {"a0": [0.5, 0.51], "a1": [0.56, 0.57]},
            "track_b": {"b0": [0.5, 0.51], "b1": [0.56, 0.57]},
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "PASS"
    assert g["metrics"].get("phase2_winner_track") == "track_a"
    assert g["metrics"].get("phase2_winner_exp_id") == "a1"
    elim = g["metrics"].get("phase2_elimination_rows")
    assert isinstance(elim, list)
    b1 = [r for r in elim if isinstance(r, Mapping) and r.get("exp_id") == "b1"]
    assert len(b1) == 1
    assert b1[0]["reason_code"] == "meets_min_uplift_but_not_global_winner"


def test_evaluate_phase2_gate_std_pass_with_low_variance_series() -> None:
    """phase2_pat_series_by_experiment with low stdev keeps PASS after uplift PASS."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.63,
                },
            ],
        },
        "phase2_pat_series_by_experiment": {
            "track_c": {
                "c0": [0.60, 0.6001, 0.5999],
                "c1": [0.63, 0.6302, 0.6298],
            },
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "PASS"
    assert g["metrics"].get("phase2_std_gate_evaluated") is True
    assert "std gate:" in (g.get("evidence_summary") or "")
    assert "— PASS" in (g.get("evidence_summary") or "")


def test_evaluate_phase2_gate_std_fail_when_series_too_volatile() -> None:
    """High cross-window stdev fails gate after uplift PASS."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.63,
                },
            ],
        },
        "phase2_pat_series_by_experiment": {
            "track_c": {"c0": [0.5, 0.95, 0.52]},
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "FAIL"
    assert "phase2_std_exceeds_max_pp_across_windows" in g["blocking_reasons"]


def test_evaluate_phase2_gate_std_informational_when_uplift_fail() -> None:
    """Std metrics recorded but do not replace uplift FAIL reason."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.615,
                },
            ],
        },
        "phase2_pat_series_by_experiment": {
            "track_c": {"c0": [0.5, 0.92]},
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "FAIL"
    assert g["blocking_reasons"] == ["phase2_uplift_below_min_pp_vs_baseline"]
    assert "informational" in (g.get("evidence_summary") or "")
    assert g["metrics"].get("phase2_std_gate_evaluated") is True


def test_phase2_pat_series_mapping_has_evaluable_series() -> None:
    """Helper matches std-gate evaluable shape (track_* + len>=2)."""
    assert collectors.phase2_pat_series_mapping_has_evaluable_series(None) is False
    assert collectors.phase2_pat_series_mapping_has_evaluable_series({}) is False
    assert (
        collectors.phase2_pat_series_mapping_has_evaluable_series(
            {"track_a": {"x": [0.1]}}
        )
        is False
    )
    assert (
        collectors.phase2_pat_series_mapping_has_evaluable_series(
            {"track_a": {"x": [0.1, 0.2]}, "noise": {"y": [1.0, 2.0]}}
        )
        is True
    )
    assert (
        collectors.phase2_pat_series_mapping_has_evaluable_series(
            {"noise": {"y": [1.0, 2.0]}}
        )
        is False
    )


def test_merge_phase2_pat_series_builds_two_point_series() -> None:
    """MVP merge pairs shared PAT@1% with each ok per-job preview."""
    b: dict = {
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "per_job_backtest_jobs": {
            "executed": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.52,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is True
    assert b["phase2_pat_series_by_experiment"]["track_c"]["c0"] == [0.5, 0.52]


def test_merge_phase2_pat_series_prefers_per_job_window_series() -> None:
    """When per-job window series exists, merge should use it instead of two-point bridge."""
    b: dict = {
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "per_job_backtest_jobs": {
            "executed": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "precision_at_recall_1pct_by_window_preview": [0.61, 0.63, 0.62],
                    "precision_at_recall_1pct_window_ids_preview": ["wA", "wB", "wC"],
                    "shared_precision_at_recall_1pct_preview": 0.52,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is True
    assert b["phase2_pat_series_by_experiment"]["track_c"]["c0"] == [0.61, 0.63, 0.62]
    src = b["phase2_pat_series_source_by_experiment"]["track_c"]["c0"]
    assert src["source"] == "per_job_window_series"
    assert src["window_ids"] == ["wA", "wB", "wC"]


def test_merge_phase2_pat_series_short_series_falls_back_to_bridge() -> None:
    """Single-point per-job series is not evaluable; merge falls back to [shared, preview]."""
    b: dict = {
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "per_job_backtest_jobs": {
            "executed": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "precision_at_recall_1pct_by_window_preview": [0.61],
                    "shared_precision_at_recall_1pct_preview": 0.52,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is True
    assert b["phase2_pat_series_by_experiment"]["track_c"]["c0"] == [0.5, 0.52]
    src = b["phase2_pat_series_source_by_experiment"]["track_c"]["c0"]
    assert src["source"] == "shared_bridge"


def test_merge_phase2_pat_series_skips_when_manual_len_ge_2() -> None:
    """Do not overwrite user-provided multi-window series."""
    b: dict = {
        "phase2_pat_series_by_experiment": {"track_c": {"c0": [0.1, 0.2]}},
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "per_job_backtest_jobs": {
            "executed": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.99,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is False
    assert b["phase2_pat_series_by_experiment"]["track_c"]["c0"] == [0.1, 0.2]


def test_merge_phase2_pat_series_preserves_nonempty_yaml_fills_other_exp() -> None:
    """Auto-merge adds missing exp_ids without overwriting YAML single-point series."""
    b: dict = {
        "phase2_pat_series_by_experiment": {"track_c": {"c0": [0.55]}},
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "per_job_backtest_jobs": {
            "executed": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.99,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is True
    assert b["phase2_pat_series_by_experiment"]["track_c"]["c0"] == [0.55]
    assert b["phase2_pat_series_by_experiment"]["track_c"]["c1"] == [0.5, 0.6]


def test_merge_phase2_pat_series_noop_when_only_nonempty_yaml_matches_results() -> None:
    """If every per-job row already has a non-empty series, merge makes no writes."""
    b: dict = {
        "phase2_pat_series_by_experiment": {"track_c": {"c0": [0.55]}},
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "per_job_backtest_jobs": {
            "executed": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.99,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is False
    assert b["phase2_pat_series_by_experiment"]["track_c"]["c0"] == [0.55]


def test_merge_phase2_pat_series_noop_without_shared_pat() -> None:
    """Without ingested shared PAT, cannot build two-point series."""
    b: dict = {
        "per_job_backtest_jobs": {
            "executed": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.52,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is False
    assert "phase2_pat_series_by_experiment" not in b


def test_merge_phase2_pat_series_writes_source_map_for_bridge_and_series() -> None:
    """Merge should persist per-exp source metadata for auditing."""
    b: dict = {
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "per_job_backtest_jobs": {
            "executed": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "ok": True,
                    "precision_at_recall_1pct_by_window_preview": [0.61, 0.63],
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.52,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is True
    src = b["phase2_pat_series_source_by_experiment"]["track_c"]
    assert src["c0"]["source"] == "per_job_window_series"
    assert src["c1"]["source"] == "shared_bridge"


def test_evaluate_phase2_gate_after_auto_merge_std_evaluated() -> None:
    """merge_phase2_pat_series_from_shared_and_per_job + gate runs std on two-point lists."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        # Two-point [shared, preview] stdev is large when preview differs from shared; use a
        # loose std cap here to assert the auto-merge + std path, not production thresholds.
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 50.0},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.63,
                },
            ],
        },
    }
    assert collectors.merge_phase2_pat_series_from_shared_and_per_job(b) is True
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "PASS"
    assert g["metrics"].get("phase2_std_gate_evaluated") is True


def test_collect_summary_phase2_pat_series_merge_hints() -> None:
    """Summary flags when auto-merge is skipped or eligible."""
    base: dict = {
        "bundle_kind": "phase2_plan_v1",
        "status": "metrics_ingested",
        "tracks": {},
        "experiments_index": [],
    }
    s_skip = collectors.collect_summary_phase2_plan_for_run_state(
        {
            **base,
            "phase2_pat_series_by_experiment": {"track_a": {"e": [0.1, 0.2]}},
        }
    )
    assert s_skip.get("phase2_pat_series_auto_merge_skipped") is True
    s_elig = collectors.collect_summary_phase2_plan_for_run_state(
        {
            **base,
            "per_job_backtest_jobs": {"executed": True},
            "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.4}},
        }
    )
    assert s_elig.get("phase2_pat_series_auto_merge_eligible") is True


def test_collect_summary_phase2_pat_series_coverage_counts() -> None:
    """Summary reports PAT@1% series key count, max len, and len>=2 count."""
    b: dict = {
        "bundle_kind": "phase2_plan_v1",
        "status": "metrics_ingested",
        "tracks": {},
        "experiments_index": [],
        "phase2_pat_series_by_experiment": {
            "track_a": {"e0": [0.1], "e1": [0.2, 0.21]},
            "track_b": {"b0": [0.3, 0.31, 0.32]},
            "not_a_track": {"x": [1.0, 2.0]},
        },
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert s.get("phase2_pat_series_key_count") == 3
    assert s.get("phase2_pat_series_max_len") == 3
    assert s.get("phase2_pat_series_len_ge_2_count") == 2


def test_count_phase2_yaml_pat_matrix_experiments() -> None:
    """count_phase2_yaml_pat_matrix_experiments only counts track_* experiments with lists."""
    assert collectors.count_phase2_yaml_pat_matrix_experiments(None) == 0
    tr: dict[str, Any] = {
        "track_a": {
            "experiments": [
                {"exp_id": "a0", "precision_at_recall_1pct_by_window": [0.1, 0.2]},
                {"exp_id": "a1", "precision_at_recall_1pct_by_window": []},
            ]
        },
        "other": {
            "experiments": [{"exp_id": "x", "precision_at_recall_1pct_by_window": [0.9]}],
        },
    }
    assert collectors.count_phase2_yaml_pat_matrix_experiments(tr) == 1


def test_collect_summary_phase2_pat_matrix_yaml_experiment_count() -> None:
    """phase2_collect includes phase2_pat_matrix_yaml_experiment_count when YAML lists exist."""
    b: dict[str, Any] = {
        "bundle_kind": "phase2_plan_v1",
        "status": "plan_only",
        "experiments_index": [],
        "job_specs": [],
        "tracks": {
            "track_a": {
                "enabled": True,
                "experiments": [
                    {"exp_id": "e1", "precision_at_recall_1pct_by_window": [0.1, 0.2]},
                    {"exp_id": "e2", "precision_at_recall_1pct_by_window": [0.3]},
                ],
            }
        },
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert s.get("phase2_pat_matrix_yaml_experiment_count") == 2


def test_collect_summary_phase2_omits_pat_matrix_yaml_count_when_zero() -> None:
    """No YAML PAT-by-window lists → summary omits the counter key."""
    b: dict[str, Any] = {
        "bundle_kind": "phase2_plan_v1",
        "status": "plan_only",
        "experiments_index": [],
        "job_specs": [],
        "tracks": {},
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert "phase2_pat_matrix_yaml_experiment_count" not in s


def test_extract_phase2_shared_pat_series_from_backtest_metrics_ok() -> None:
    m: Mapping[str, Any] = {
        "model_default": {"test_precision_at_recall_0.01_by_window": [0.42, 0.43]}
    }
    assert evaluators.extract_phase2_shared_pat_series_from_backtest_metrics(m) == [
        0.42,
        0.43,
    ]


def test_extract_phase2_shared_pat_series_from_backtest_metrics_bad_element() -> None:
    m: Mapping[str, Any] = {
        "model_default": {"test_precision_at_recall_0.01_by_window": [0.1, "nope"]}
    }
    assert (
        evaluators.extract_phase2_shared_pat_series_from_backtest_metrics(m) is None
    )


def test_extract_phase2_shared_pat_window_ids_from_backtest_metrics() -> None:
    m: Mapping[str, Any] = {
        "model_default": {"test_precision_at_recall_0.01_window_ids": ["w1", 2]}
    }
    assert evaluators.extract_phase2_shared_pat_window_ids_from_backtest_metrics(
        m
    ) == ["w1", "2"]


def test_collect_summary_phase2_includes_shared_backtest_pat_series_fields() -> None:
    """run_state.phase2_collect can surface shared backtest multi-window PAT shape."""
    b: dict[str, Any] = {
        "bundle_kind": "phase2_plan_v1",
        "status": "metrics_ingested",
        "experiments_index": [],
        "job_specs": [],
        "backtest_metrics": {
            "model_default": {
                "test_precision_at_recall_0.01_by_window": [0.5, 0.6, 0.7],
                "test_precision_at_recall_0.01_window_ids": ["a", "b", "c"],
            }
        },
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert s.get("phase2_shared_backtest_pat_series_len") == 3
    assert s.get("phase2_shared_backtest_pat_window_ids_len") == 3


def test_collect_summary_phase2_flags_pat_series_ids_mismatch_when_lengths_differ() -> None:
    """When both series and window_ids exist but differ in length, summary flags mismatch."""
    b: dict[str, Any] = {
        "bundle_kind": "phase2_plan_v1",
        "status": "metrics_ingested",
        "experiments_index": [],
        "job_specs": [],
        "backtest_metrics": {
            "model_default": {
                "test_precision_at_recall_0.01_by_window": [0.1, 0.2],
                "test_precision_at_recall_0.01_window_ids": ["only_one"],
            }
        },
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert s.get("phase2_shared_backtest_pat_series_len") == 2
    assert s.get("phase2_shared_backtest_pat_window_ids_len") == 1
    assert s.get("phase2_shared_backtest_pat_series_ids_mismatch") is True


def test_collect_summary_phase2_omits_mismatch_when_series_and_ids_aligned() -> None:
    b: dict[str, Any] = {
        "bundle_kind": "phase2_plan_v1",
        "status": "metrics_ingested",
        "experiments_index": [],
        "job_specs": [],
        "backtest_metrics": {
            "model_default": {
                "test_precision_at_recall_0.01_by_window": [0.1, 0.2],
                "test_precision_at_recall_0.01_window_ids": ["a", "b"],
            }
        },
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert "phase2_shared_backtest_pat_series_ids_mismatch" not in s


def test_collect_summary_phase2_omits_mismatch_when_only_pat_series() -> None:
    b: dict[str, Any] = {
        "bundle_kind": "phase2_plan_v1",
        "status": "metrics_ingested",
        "experiments_index": [],
        "job_specs": [],
        "backtest_metrics": {
            "model_default": {
                "test_precision_at_recall_0.01_by_window": [0.5],
            }
        },
    }
    s = collectors.collect_summary_phase2_plan_for_run_state(b)
    assert s.get("phase2_shared_backtest_pat_series_len") == 1
    assert "phase2_shared_backtest_pat_window_ids_len" not in s
    assert "phase2_shared_backtest_pat_series_ids_mismatch" not in s


def test_run_logged_command_timeout_uses_e_subprocess_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wall-clock timeout must not reuse E_NO_DATA_WINDOW (PAT / empty-window semantics)."""

    def fake_run(*_a: Any, **_k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="dummy", timeout=0.5)

    monkeypatch.setattr(subprocess, "run", fake_run)
    log_dir = tmp_path / "logs_timeout"
    r = runner.run_logged_command(
        [sys.executable, "-c", "0"],
        cwd=tmp_path,
        log_dir=log_dir,
        log_stem="to",
        timeout_sec=0.5,
    )
    assert r.get("ok") is False
    assert r.get("error_code") == "E_SUBPROCESS_TIMEOUT"
    assert "timeout" in str(r.get("message") or "").lower()


def test_evaluate_phase2_gate_metrics_ingested_uplift_blocked_single_preview() -> None:
    """One preview only → cannot compare; BLOCKED with phase2_uplift_insufficient_comparisons."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "BLOCKED"
    assert "phase2_uplift_insufficient_comparisons" in g["blocking_reasons"]
    assert g["metrics"].get("phase2_uplift_gate_evaluated") is True


def test_phase2_preview_map_excludes_failed_per_job_rows() -> None:
    """Failed per-job rows must not participate in uplift baseline/challenger."""
    b: dict = {
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": False,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": False,
                    "shared_precision_at_recall_1pct_preview": 0.9,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.65,
                },
            ],
        }
    }
    m = evaluators._phase2_preview_map_from_bundle(b)
    assert ("track_c", "c0") not in m
    assert m.get(("track_c", "c1")) == pytest.approx(0.65)


def test_evaluate_phase2_gate_metrics_ingested_uplift_fail_below_min() -> None:
    """Two previews but uplift below min_uplift_pp_vs_baseline → FAIL."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.615,
                },
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "FAIL"
    assert "phase2_uplift_below_min_pp_vs_baseline" in g["blocking_reasons"]
    assert g["metrics"].get("phase2_uplift_pass") is False
    elim = g["metrics"].get("phase2_elimination_rows")
    assert isinstance(elim, list) and len(elim) == 1
    assert elim[0]["exp_id"] == "c1"
    assert elim[0]["reason_code"] == "below_min_uplift_pp_vs_baseline"


def test_evaluate_phase2_gate_uses_configured_baseline_per_track() -> None:
    """Configured baseline_exp_id_by_track overrides implicit first-preview baseline."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {
            "min_uplift_pp_vs_baseline": 3.0,
            "max_std_pp_across_windows": 2.5,
            "baseline_exp_id_by_track": {"track_c": "c1"},
            "min_pat_windows_for_pass": 0,
        },
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}, {"exp_id": "c2"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.70,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.60,
                },
                {
                    "track": "track_c",
                    "exp_id": "c2",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.64,
                },
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "PASS"
    rows = g["metrics"].get("phase2_uplift_rows") or []
    baseline_rows = [r for r in rows if isinstance(r, dict) and r.get("role") == "baseline"]
    assert baseline_rows
    assert baseline_rows[0].get("exp_id") == "c1"
    assert baseline_rows[0].get("baseline_source") == "gate.baseline_exp_id_by_track"


def test_evaluate_phase2_gate_configured_baseline_preview_missing_blocked() -> None:
    """Configured baseline without successful preview blocks uplift gate."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {
            "min_uplift_pp_vs_baseline": 3.0,
            "max_std_pp_across_windows": 2.5,
            "baseline_exp_id_by_track": {"track_c": "c1"},
        },
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.60,
                }
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "BLOCKED"
    assert "phase2_uplift_baseline_preview_missing" in g["blocking_reasons"]


def test_evaluate_phase2_gate_configured_baseline_invalid_fails() -> None:
    """Configured baseline not present in enabled track experiments fails fast."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {
            "min_uplift_pp_vs_baseline": 3.0,
            "max_std_pp_across_windows": 2.5,
            "baseline_exp_id_by_track": {"track_c": "c_missing"},
        },
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.60,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.65,
                },
            ],
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["status"] == "FAIL"
    assert "phase2_uplift_baseline_config_invalid" in g["blocking_reasons"]


def test_write_phase2_track_results_per_job_backtest_section(tmp_path: Path) -> None:
    """Each track md lists per-job backtest rows when per_job_backtest_jobs ran."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    bundle["status"] = "plan_only"
    bundle["per_job_backtest_jobs"] = {
        "executed": True,
        "all_ok": True,
        "results": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "skipped": False,
                "ok": True,
                "shared_precision_at_recall_1pct_preview": 0.777,
            },
            {
                "track": "track_a",
                "exp_id": "a_extra",
                "skipped": True,
                "ok": True,
                "skip_reason": "no training_metrics_repo_relative",
            },
            {
                "track": "track_b",
                "exp_id": "b0",
                "skipped": True,
                "ok": True,
                "skip_reason": "no training_metrics_repo_relative",
            },
        ],
    }
    gate = evaluators.evaluate_phase2_gate(bundle)
    p2 = tmp_path / "phase2"
    report_builder.write_phase2_track_results(p2, "rid", cfg, bundle, gate)
    ta = (p2 / "track_a_results.md").read_text(encoding="utf-8")
    assert "## Trainer CLI evidence (T10A)" in ta
    assert "## Per-job backtest preview" in ta
    assert "0.7770" in ta
    assert "**skipped**" in ta
    tb = (p2 / "track_b_results.md").read_text(encoding="utf-8")
    assert "## Per-job backtest preview" in tb
    assert "no training_metrics_repo_relative" in tb
    tc = (p2 / "track_c_results.md").read_text(encoding="utf-8")
    assert "no rows for this track" in tc
    assert "## Uplift vs baseline (gate)" in ta
    assert "uplift gate not evaluated" in ta.lower()
    assert "## PAT@1% series & std (gate)" in ta


def test_write_phase2_track_results_std_section_shows_evaluated_metrics(
    tmp_path: Path,
) -> None:
    """Track md includes bundle PAT@1% series and std gate rows for that track."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_c"]["experiments"] = [
        {"exp_id": "c0", "overrides": {}},
        {"exp_id": "c1", "overrides": {}},
    ]
    cfg = config_loader.validate_phase2_config(raw, cli_run_id="rid")
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    bundle["status"] = "metrics_ingested"
    bundle["backtest_metrics"] = {"model_default": {"test_precision_at_recall_0.01": 0.5}}
    bundle["per_job_backtest_jobs"] = {
        "executed": True,
        "all_ok": True,
        "results": [
            {
                "track": "track_c",
                "exp_id": "c0",
                "skipped": False,
                "ok": True,
                "shared_precision_at_recall_1pct_preview": 0.6,
            },
            {
                "track": "track_c",
                "exp_id": "c1",
                "skipped": False,
                "ok": True,
                "shared_precision_at_recall_1pct_preview": 0.63,
            },
        ],
    }
    bundle["phase2_pat_series_by_experiment"] = {
        "track_c": {
            "c0": [0.60, 0.6001, 0.5999],
            "c1": [0.63, 0.6302, 0.6298],
        },
    }
    gate = evaluators.evaluate_phase2_gate(bundle)
    p2 = tmp_path / "phase2"
    report_builder.write_phase2_track_results(p2, "rid", cfg, bundle, gate)
    tc = (p2 / "track_c_results.md").read_text(encoding="utf-8")
    assert "## PAT@1% series & std (gate)" in tc
    assert "### Bundle series (this track)" in tc
    assert "`c0`" in tc and "`c1`" in tc
    assert "**evaluated**: yes" in tc
    assert "max sample stdev (pp, gate-wide)" in tc
    assert "n_windows=3" in tc
    assert "std_pp=" in tc
    ta = (p2 / "track_a_results.md").read_text(encoding="utf-8")
    assert "no `phase2_pat_series_by_experiment` entries for this track" in ta


def test_write_phase2_track_results_trainer_cli_evidence_recorded_from_trainer_jobs(
    tmp_path: Path,
) -> None:
    """T10A: when trainer_jobs ran, md shows recorded argv_fingerprint and trainer_params."""
    raw = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    raw["tracks"]["track_a"]["experiments"] = [
        {
            "exp_id": "a0",
            "overrides": {},
            "trainer_params": {"recent_chunks": 2},
        },
    ]
    cfg = config_loader.validate_phase2_config(raw, cli_run_id="rid")
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    bundle["status"] = "plan_only"
    bundle["trainer_jobs"] = {
        "executed": True,
        "all_ok": True,
        "results": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "ok": True,
                "argv_fingerprint": "a1b2c3d4e5f6a7b8c9d0e1f2",
                "resolved_trainer_argv": [
                    "python",
                    "-m",
                    "trainer.trainer",
                    "--start",
                    "2026-01-01T00:00:00+08:00",
                    "--end",
                    "2026-01-08T00:00:00+08:00",
                    "--recent-chunks",
                    "2",
                ],
            },
        ],
    }
    gate = evaluators.evaluate_phase2_gate(bundle)
    p2 = tmp_path / "phase2"
    report_builder.write_phase2_track_results(p2, "rid", cfg, bundle, gate)
    ta = (p2 / "track_a_results.md").read_text(encoding="utf-8")
    assert "## Trainer CLI evidence (T10A)" in ta
    assert "a1b2c3d4e5f6a7b8c9d0e1f2" in ta
    assert "recent_chunks" in ta
    assert "**resolved_trainer_argv** (recorded)" in ta
    assert "--recent-chunks" in ta


def test_write_phase2_track_results_pat_series_shows_source_metadata(
    tmp_path: Path,
) -> None:
    """Track series section should show source + optional window ids when provided."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    bundle["status"] = "metrics_ingested"
    bundle["phase2_pat_series_by_experiment"] = {"track_c": {"c0": [0.61, 0.63, 0.62]}}
    bundle["phase2_pat_series_source_by_experiment"] = {
        "track_c": {
            "c0": {"source": "per_job_window_series", "window_ids": ["wA", "wB", "wC"]}
        }
    }
    gate = {"status": "BLOCKED", "blocking_reasons": [], "evidence_summary": "", "metrics": {}}
    p2 = tmp_path / "phase2"
    report_builder.write_phase2_track_results(p2, "rid", cfg, bundle, gate)
    tc = (p2 / "track_c_results.md").read_text(encoding="utf-8")
    assert "source=per_job_window_series" in tc
    assert "window_ids=[wA, wB, wC]" in tc


def test_phase2_pat_series_source_counts_helper() -> None:
    """Count per-source rows from phase2_pat_series_source_by_experiment."""
    b: dict = {
        "phase2_pat_series_source_by_experiment": {
            "track_c": {
                "c0": {"source": "per_job_window_series"},
                "c1": {"source": "shared_bridge"},
            },
            "track_b": {"b0": {"source": "shared_bridge"}},
        }
    }
    out = evaluators.phase2_pat_series_source_counts(b)
    assert out == {"per_job_window_series": 1, "shared_bridge": 2}


def test_evaluate_phase2_gate_metrics_ingested_includes_source_counts() -> None:
    """Gate metrics/evidence should include PAT series source count summary."""
    b: dict = {
        "status": "metrics_ingested",
        "errors": [],
        "backtest_metrics": {"model_default": {"test_precision_at_recall_0.01": 0.5}},
        "bundle_kind": "phase2_plan_v1",
        "gate": {"min_uplift_pp_vs_baseline": 3.0, "max_std_pp_across_windows": 2.5},
        "tracks": {
            "track_a": {"enabled": False, "experiments": []},
            "track_b": {"enabled": False, "experiments": []},
            "track_c": {
                "enabled": True,
                "experiments": [{"exp_id": "c0"}, {"exp_id": "c1"}],
            },
        },
        "per_job_backtest_jobs": {
            "executed": True,
            "all_ok": True,
            "results": [
                {
                    "track": "track_c",
                    "exp_id": "c0",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.6,
                },
                {
                    "track": "track_c",
                    "exp_id": "c1",
                    "skipped": False,
                    "ok": True,
                    "shared_precision_at_recall_1pct_preview": 0.64,
                },
            ],
        },
        "phase2_pat_series_source_by_experiment": {
            "track_c": {
                "c0": {"source": "shared_bridge"},
                "c1": {"source": "per_job_window_series"},
            }
        },
    }
    g = evaluators.evaluate_phase2_gate(b)
    assert g["metrics"].get("phase2_pat_series_source_counts") == {
        "shared_bridge": 1,
        "per_job_window_series": 1,
    }
    assert "PAT series source counts:" in g["evidence_summary"]


def test_write_phase2_gate_decision_includes_source_counts(tmp_path: Path) -> None:
    """Gate decision markdown prints PAT series source count section."""
    p = tmp_path / "phase2_gate_decision.md"
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    bundle = {"status": "metrics_ingested"}
    gate = {
        "status": "PASS",
        "blocking_reasons": [],
        "evidence_summary": "ok",
        "metrics": {"phase2_pat_series_source_counts": {"shared_bridge": 2}},
    }
    report_builder.write_phase2_gate_decision(p, "rid", cfg, bundle, gate)
    text = p.read_text(encoding="utf-8")
    assert "### PAT series source counts" in text
    assert "`shared_bridge`: 2" in text


def test_write_phase2_track_results_writes_three_files(tmp_path: Path) -> None:
    """T11 stub writes one markdown per track."""
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    bundle = collectors.collect_phase2_plan_bundle("rid", cfg)
    bundle["status"] = "metrics_ingested"
    bundle["backtest_metrics"] = {"model_default": {"test_precision_at_recall_0.01": 0.1}}
    bundle["job_training_harvest"] = {
        "rows": [
            {
                "track": "track_a",
                "exp_id": "a0",
                "found": True,
                "metrics_relative": "orch/state/rid/logs/phase2/track_a/a0/training_metrics.json",
            },
            {
                "track": "track_b",
                "exp_id": "b0",
                "found": False,
                "load_error": "file not found",
            },
        ],
    }
    gate = evaluators.evaluate_phase2_gate(bundle)
    p2 = tmp_path / "phase2"
    paths = report_builder.write_phase2_track_results(p2, "rid", cfg, bundle, gate)
    assert len(paths) == 3
    for p in paths:
        assert p.is_file()
        text = p.read_text(encoding="utf-8")
        assert "## Trainer CLI evidence (T10A)" in text
        assert "argv_fingerprint" in text
        assert "shared backtest" in text.lower() or "Shared" in text
        assert "0.1000" in text
        assert "## Per-job training_metrics harvest" in text
        assert "## Per-job backtest preview" in text
        assert "## Uplift vs baseline (gate)" in text
        assert "## PAT@1% series & std (gate)" in text
    track_a = p2 / "track_a_results.md"
    assert "**found**" in track_a.read_text(encoding="utf-8")
    assert "`a0`" in track_a.read_text(encoding="utf-8")
    track_b = p2 / "track_b_results.md"
    tb = track_b.read_text(encoding="utf-8")
    assert "**not found**" in tb
    assert "`b0`" in tb


def test_phase2_bundle_backtest_jobs_skipped_after_pipeline(tmp_path: Path) -> None:
    """Default phase2 run records backtest_jobs.executed=false."""
    run_id = "pytest_phase2_backtest_skipped"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg_file = tmp_path / "p2bt.yaml"
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
    bundle_path = _ORCHESTRATOR / "state" / run_id / "phase2_bundle.json"
    for p in (state_json, bundle_path):
        if p.is_file():
            p.unlink()
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
            "--skip-phase2-trainer-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    bj = data.get("backtest_jobs")
    assert isinstance(bj, dict)
    assert bj.get("executed") is False
    assert bj.get("skip_reason")


def test_phase2_bundle_includes_job_training_harvest_after_pipeline(tmp_path: Path) -> None:
    """phase2_job_metrics_harvest writes job_training_harvest with one row per job_spec."""
    run_id = "pytest_phase2_harvest_bundle"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg_file = tmp_path / "p2harvest.yaml"
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
    bundle_path = _ORCHESTRATOR / "state" / run_id / "phase2_bundle.json"
    for p in (state_json, bundle_path):
        if p.is_file():
            p.unlink()
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
            "--skip-phase2-trainer-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    jh = data.get("job_training_harvest")
    assert isinstance(jh, dict)
    assert jh.get("metrics_filename") == collectors.PHASE2_JOB_TRAINING_METRICS_NAME
    rows = jh.get("rows")
    assert isinstance(rows, list) and len(rows) == 3
    st = json.loads(state_json.read_text(encoding="utf-8"))
    assert st["steps"]["phase2_job_metrics_harvest"]["status"] == "success"


def test_phase2_resume_skips_completed_backtest_jobs(tmp_path: Path) -> None:
    """Second --resume should skip phase2_backtest_jobs when step already success."""
    run_id = "pytest_phase2_resume_backtest_skip"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg_file = tmp_path / "p2btskip.yaml"
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
    argv = [
        sys.executable,
        str(_RUN_PIPELINE),
        "--phase",
        "phase2",
        "--config",
        str(cfg_file),
        "--run-id",
        run_id,
        "--skip-backtest-smoke",
        "--skip-phase2-trainer-smoke",
    ]
    proc1 = subprocess.run(argv, cwd=_REPO_ROOT, capture_output=True, text=True, check=False)
    assert proc1.returncode == 0, proc1.stderr
    t1 = json.loads(state_json.read_text(encoding="utf-8"))["steps"]["phase2_backtest_jobs"][
        "finished_at"
    ]
    proc2 = subprocess.run(
        argv + ["--resume"], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert proc2.returncode == 0, proc2.stderr
    t2 = json.loads(state_json.read_text(encoding="utf-8"))["steps"]["phase2_backtest_jobs"][
        "finished_at"
    ]
    assert t1 == t2


def test_phase2_resume_skips_completed_per_job_backtest_jobs(tmp_path: Path) -> None:
    """Second --resume should skip phase2_per_job_backtest_jobs when step already success."""
    run_id = "pytest_phase2_resume_per_job_bt_skip"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg_file = tmp_path / "p2pjbskip.yaml"
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
    argv = [
        sys.executable,
        str(_RUN_PIPELINE),
        "--phase",
        "phase2",
        "--config",
        str(cfg_file),
        "--run-id",
        run_id,
        "--skip-backtest-smoke",
        "--skip-phase2-trainer-smoke",
        "--phase2-run-per-job-backtests",
    ]
    proc1 = subprocess.run(argv, cwd=_REPO_ROOT, capture_output=True, text=True, check=False)
    assert proc1.returncode == 0, proc1.stderr
    t1 = json.loads(state_json.read_text(encoding="utf-8"))["steps"][
        "phase2_per_job_backtest_jobs"
    ]["finished_at"]
    proc2 = subprocess.run(
        argv + ["--resume"], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert proc2.returncode == 0, proc2.stderr
    t2 = json.loads(state_json.read_text(encoding="utf-8"))["steps"][
        "phase2_per_job_backtest_jobs"
    ]["finished_at"]
    assert t1 == t2


def test_phase2_resume_skips_completed_job_metrics_harvest(tmp_path: Path) -> None:
    """Second --resume should skip phase2_job_metrics_harvest when step already success."""
    run_id = "pytest_phase2_resume_harvest_skip"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg_file = tmp_path / "p2jhskip.yaml"
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
    argv = [
        sys.executable,
        str(_RUN_PIPELINE),
        "--phase",
        "phase2",
        "--config",
        str(cfg_file),
        "--run-id",
        run_id,
        "--skip-backtest-smoke",
        "--skip-phase2-trainer-smoke",
    ]
    proc1 = subprocess.run(argv, cwd=_REPO_ROOT, capture_output=True, text=True, check=False)
    assert proc1.returncode == 0, proc1.stderr
    t1 = json.loads(state_json.read_text(encoding="utf-8"))["steps"][
        "phase2_job_metrics_harvest"
    ]["finished_at"]
    proc2 = subprocess.run(
        argv + ["--resume"], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert proc2.returncode == 0, proc2.stderr
    t2 = json.loads(state_json.read_text(encoding="utf-8"))["steps"][
        "phase2_job_metrics_harvest"
    ]["finished_at"]
    assert t1 == t2


def test_phase2_resume_skips_completed_runner_smoke(tmp_path: Path) -> None:
    """Second --resume should skip phase2_runner_smoke when step already success."""
    run_id = "pytest_phase2_resume_runner_skip"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg_file = tmp_path / "p2skip.yaml"
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
    argv = [
        sys.executable,
        str(_RUN_PIPELINE),
        "--phase",
        "phase2",
        "--config",
        str(cfg_file),
        "--run-id",
        run_id,
        "--skip-backtest-smoke",
        "--skip-phase2-trainer-smoke",
    ]
    proc1 = subprocess.run(
        argv,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc1.returncode == 0, proc1.stderr
    t1 = json.loads(state_json.read_text(encoding="utf-8"))["steps"]["phase2_runner_smoke"][
        "finished_at"
    ]
    proc2 = subprocess.run(
        argv + ["--resume"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stderr
    t2 = json.loads(state_json.read_text(encoding="utf-8"))["steps"]["phase2_runner_smoke"][
        "finished_at"
    ]
    assert t1 == t2


def test_phase2_resume_missing_bundle_exits_4(tmp_path: Path) -> None:
    """--resume after deleting phase2_bundle.json must exit 4 when plan step was success."""
    run_id = "pytest_phase2_resume_no_bundle"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")

    cfg_file = tmp_path / "phase2_resume.yaml"
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
    bundle_path = _ORCHESTRATOR / "state" / run_id / "phase2_bundle.json"
    for p in (state_json, bundle_path):
        if p.is_file():
            p.unlink()

    proc1 = subprocess.run(
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
            "--skip-phase2-trainer-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc1.returncode == 0, proc1.stderr
    assert bundle_path.is_file()
    bundle_path.unlink()
    assert not bundle_path.is_file()

    proc2 = subprocess.run(
        [
            sys.executable,
            str(_RUN_PIPELINE),
            "--phase",
            "phase2",
            "--config",
            str(cfg_file),
            "--run-id",
            run_id,
            "--resume",
            "--skip-backtest-smoke",
            "--skip-phase2-trainer-smoke",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == phase2_exits.EXIT_RESUME_BUNDLE_LOAD_FAILED
    assert "cannot load phase2_bundle.json" in proc2.stderr


def test_collect_phase2_plan_bundle_raises_on_non_mapping() -> None:
    with pytest.raises(TypeError, match="mapping"):
        collectors.collect_phase2_plan_bundle("r", "not-a-mapping")  # type: ignore[arg-type]


def test_collect_phase2_plan_bundle_raises_on_bad_tracks() -> None:
    cfg = _minimal_phase2_dict(model_dir="m", state_db="s", pred_db="p")
    cfg["tracks"] = "broken"
    with pytest.raises(ValueError, match="tracks"):
        collectors.collect_phase2_plan_bundle("r", cfg)


def test_backtest_smoke_failure_returns_non_ok() -> None:
    """When trainer.backtester --help fails, preflight must not report ok."""
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
    assert proc.returncode == orch_exits.EXIT_PREFLIGHT_FAILED
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


def test_collect_phase1_mid_snapshots_collects_cp_and_alias(tmp_path: Path) -> None:
    """Collector should expose cp snapshots plus canonical mid alias in order."""
    repo = tmp_path / "repo"
    repo.mkdir()
    orch = tmp_path / "orch"
    run_id = "run_mid_multi"
    logs = orch / "state" / run_id / "logs"
    logs.mkdir(parents=True)
    (logs / "r1_r6_mid_cp2.stdout.log").write_text(
        json.dumps({"evaluate": {"precision_at_recall_target": {"precision_at_target_recall": 0.4}}}),
        encoding="utf-8",
    )
    (logs / "r1_r6_mid_cp1.stdout.log").write_text(
        json.dumps({"evaluate": {"precision_at_recall_target": {"precision_at_target_recall": 0.3}}}),
        encoding="utf-8",
    )
    (logs / "r1_r6_mid.stdout.log").write_text(
        json.dumps({"evaluate": {"precision_at_recall_target": {"precision_at_target_recall": 0.5}}}),
        encoding="utf-8",
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
    mids = bundle.get("r1_r6_mid_snapshots")
    assert isinstance(mids, list)
    assert len(mids) == 3
    assert mids[0]["checkpoint_index"] == 1
    assert mids[1]["checkpoint_index"] == 2
    assert mids[2].get("is_canonical_mid_alias") is True
    assert bundle["r1_r6_mid"]["payload"]["evaluate"]["precision_at_recall_target"]["precision_at_target_recall"] == 0.5


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
    pit_parity: dict[str, Any] | None = None,
    pit_threshold_overrides: dict[str, Any] | None = None,
) -> dict:
    payload = dict(_pat_block(final_pat))
    if r2 is not None:
        payload["r2_prediction_log_vs_alerts"] = r2
    thresholds: dict[str, Any] = {
        "min_hours_preliminary": 48,
        "min_hours_gate": 72,
        "min_finalized_alerts_preliminary": 300,
        "min_finalized_true_positives_preliminary": 30,
        "min_finalized_alerts_gate": 800,
        "min_finalized_true_positives_gate": 50,
        "gate_pat_abs_tolerance": 0.15,
    }
    if pit_threshold_overrides:
        thresholds.update(pit_threshold_overrides)
    b: dict = {
        "errors": list(errors) if errors is not None else [],
        "window": {
            "start_ts": "2026-01-01T00:00:00+08:00",
            "end_ts": hours_span,
        },
        "thresholds": thresholds,
        "state_db_stats": {
            "finalized_alerts_count": alerts,
            "finalized_true_positives_count": tp,
        },
        "r1_r6_final": {"payload": payload},
        "r1_r6_mid": {"payload": _pat_block(mid_pat) if mid_pat is not None else None},
        "backtest_metrics": {},
    }
    if pit_parity is not None:
        b["pit_parity"] = pit_parity
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


def test_gate_fail_on_multi_mid_divergence() -> None:
    """When mid snapshot PAT spread exceeds tolerance, gate should FAIL."""
    b = _gate_bundle(
        hours_span="2026-01-06T00:00:00+08:00",
        alerts=2000,
        tp=200,
        final_pat=0.6,
        mid_pat=0.55,
    )
    b["r1_r6_mid_snapshots"] = [
        {"checkpoint_index": 1, "payload": _pat_block(0.20)},
        {"checkpoint_index": 2, "payload": _pat_block(0.50)},
        {"checkpoint_index": None, "payload": _pat_block(0.40)},
    ]
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "FAIL"
    assert "r1_multi_mid_precision_at_target_recall_divergence" in g["blocking_reasons"]

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


def test_evaluate_phase1_gate_strict_pit_parity_violation_fails() -> None:
    """STRICT: parity threshold breach blocks PASS with pit_parity_violation (T6 DoD)."""
    b = _gate_bundle(
        hours_span="2026-01-04T10:00:00+08:00",
        alerts=900,
        tp=50,
        final_pat=0.40,
        mid_pat=0.41,
        pit_parity={
            "status": "ok",
            "scored_at_in_window_ratio": 0.5,
            "validated_at_non_null_ratio": 1.0,
            "alerts_vs_prediction_log_gap": 0,
        },
        pit_threshold_overrides={
            "pit_parity_mode": "STRICT",
            "min_scored_at_in_window_ratio": 0.995,
        },
    )
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "FAIL"
    assert "pit_parity_violation" in g["blocking_reasons"]
    assert "pit_scored_at_in_window_ratio_below_threshold" in g["blocking_reasons"]


def test_evaluate_phase1_gate_warn_only_passes_with_pit_violation_in_metrics() -> None:
    """WARN_ONLY: parity breach does not FAIL gate; metrics retain violation count (T6 DoD)."""
    b = _gate_bundle(
        hours_span="2026-01-04T10:00:00+08:00",
        alerts=900,
        tp=50,
        final_pat=0.40,
        mid_pat=0.41,
        pit_parity={
            "status": "warn",
            "scored_at_in_window_ratio": 0.5,
            "validated_at_non_null_ratio": 1.0,
            "alerts_vs_prediction_log_gap": 0,
        },
        pit_threshold_overrides={
            "pit_parity_mode": "WARN_ONLY",
            "min_scored_at_in_window_ratio": 0.995,
        },
    )
    g = evaluators.evaluate_phase1_gate(b)
    assert g["status"] == "PASS"
    assert g["blocking_reasons"] == []
    assert int(g["metrics"]["pit_parity_violation_count"]) >= 1
    assert "pit_violation_n=" in g["evidence_summary"]


def test_collect_phase1_pit_parity_warns_when_validated_at_column_missing(
    tmp_path: Path,
) -> None:
    """Missing validation_results.validated_at → collector warn + reason (T6 DoD)."""
    pred_db = tmp_path / "prediction_log.db"
    state_db = tmp_path / "state.db"
    window = {
        "start_ts": "2026-01-01T00:00:00+08:00",
        "end_ts": "2026-01-08T00:00:00+08:00",
    }
    with sqlite3.connect(pred_db) as conn:
        conn.execute(
            "CREATE TABLE prediction_log (scored_at TEXT, is_alert INT, is_rated_obs INT)"
        )
        conn.execute(
            "INSERT INTO prediction_log VALUES ('2026-01-02T12:00:00+08:00', 1, 1)"
        )
        conn.commit()
    with sqlite3.connect(state_db) as conn:
        conn.execute("CREATE TABLE validation_results (result INT)")
        conn.execute("CREATE TABLE alerts (ts TEXT)")
        conn.execute("INSERT INTO alerts VALUES ('2026-01-02T12:00:00+08:00')")
        conn.commit()
    out = collectors.collect_phase1_pit_parity(pred_db, state_db, window)
    assert out["status"] == "warn"
    assert "validation_results_missing_validated_at" in out["reasons"]


# --- T8A: phase1 autonomous FSM (skeleton) ---


def _write_minimal_phase1_yaml_for_fsm(tmp_path: Path) -> Path:
    """Minimal phase1 YAML + SQLite stubs for orchestrator preflight."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    cfg = _minimal_config_dict()
    cfg["model_dir"] = str(model_dir)
    cfg["state_db_path"] = str(state_db)
    cfg["prediction_log_db_path"] = str(pred_db)
    p = tmp_path / "phase1_fsm.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_main_rejects_autonomous_mode_for_phase2(tmp_path: Path) -> None:
    """--mode autonomous is only valid with --phase phase1."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "state.db"
    pred_db = tmp_path / "pred.db"
    with sqlite3.connect(state_db) as c1:
        c1.execute("CREATE TABLE alerts (x INT)")
        c1.execute("CREATE TABLE validation_results (y INT)")
    with sqlite3.connect(pred_db) as c2:
        c2.execute("CREATE TABLE prediction_log (a INT)")
    rid = "pytest_p2_autonomous_reject"
    cfg_path = tmp_path / "p2.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            _minimal_phase2_dict(
                model_dir=str(model_dir),
                state_db=str(state_db),
                pred_db=str(pred_db),
                yaml_run_id=rid,
            )
        ),
        encoding="utf-8",
    )
    rc = run_pipeline.main(
        [
            "--phase",
            "phase2",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            rid,
            "--dry-run",
            "--mode",
            "autonomous",
            "--skip-backtest-smoke",
            "--skip-phase2-trainer-smoke",
        ]
    )
    assert rc == orch_exits.EXIT_CONFIG_INVALID


def test_main_autonomous_once_with_dry_run_is_config_invalid(tmp_path: Path) -> None:
    """--autonomous-once is incompatible with --dry-run."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            "pytest_p1_once_dry_bad",
            "--dry-run",
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
            "--autonomous-once",
        ]
    )
    assert rc == orch_exits.EXIT_CONFIG_INVALID


def test_main_autonomous_once_requires_autonomous_mode(tmp_path: Path) -> None:
    """--autonomous-once without --mode autonomous is invalid."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            "pytest_p1_once_mode_bad",
            "--skip-backtest-smoke",
            "--autonomous-once",
        ]
    )
    assert rc == orch_exits.EXIT_CONFIG_INVALID


def test_main_autonomous_advance_mid_requires_autonomous_once(tmp_path: Path) -> None:
    """--autonomous-advance-mid-when-eligible without --autonomous-once is invalid."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            "pytest_p1_adv_mid_alone",
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
            "--autonomous-advance-mid-when-eligible",
        ]
    )
    assert rc == orch_exits.EXIT_CONFIG_INVALID


def test_main_autonomous_advance_mid_with_dry_run_is_config_invalid(tmp_path: Path) -> None:
    """--autonomous-advance-mid-when-eligible is incompatible with --dry-run."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            "pytest_p1_adv_mid_dry",
            "--dry-run",
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
            "--autonomous-once",
            "--autonomous-advance-mid-when-eligible",
        ]
    )
    assert rc == orch_exits.EXIT_CONFIG_INVALID


def test_main_autonomous_advance_mid_second_tick_moves_to_mid_when_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two autonomous-once ticks with advance: observe -> mid_snapshot when eligible."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "st.db"
    pred_db = tmp_path / "pr.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute("CREATE TABLE alerts (x INT)")
        conn.execute(
            "CREATE TABLE validation_results (alert_ts TEXT, validated_at TEXT, result INT)"
        )
        for _ in range(5):
            conn.execute(
                "INSERT INTO validation_results VALUES (?, ?, 1)",
                ("2026-01-04T12:00:00+08:00", "2026-01-05T00:00:00+08:00"),
            )
        conn.commit()
    with sqlite3.connect(pred_db) as conn:
        conn.execute("CREATE TABLE prediction_log (a INT)")
        conn.commit()
    cfg = _minimal_config_dict()
    cfg["model_dir"] = str(model_dir)
    cfg["state_db_path"] = str(state_db)
    cfg["prediction_log_db_path"] = str(pred_db)
    cfg["window"] = {
        "start_ts": "2026-01-01T00:00:00+08:00",
        "end_ts": "2026-01-08T00:00:00+08:00",
    }
    thr = dict(cfg["thresholds"])
    thr["min_hours_preliminary"] = 1
    thr["min_finalized_alerts_preliminary"] = 2
    thr["min_finalized_true_positives_preliminary"] = 2
    thr["min_hours_gate"] = 1
    thr["min_finalized_alerts_gate"] = 3
    thr["min_finalized_true_positives_gate"] = 3
    cfg["thresholds"] = thr
    cfg_path = tmp_path / "adv_mid.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    run_id = f"pytest_p1_adv_mid_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(
        run_pipeline.runner,
        "run_preflight",
        lambda *a, **k: {"ok": True, "error_code": None, "message": None, "checks": []},
    )
    argv = [
        "--phase",
        "phase1",
        "--config",
        str(cfg_path.resolve()),
        "--run-id",
        run_id,
        "--skip-backtest-smoke",
        "--mode",
        "autonomous",
        "--autonomous-once",
        "--autonomous-advance-mid-when-eligible",
    ]
    assert run_pipeline.main(argv) == 0
    assert run_pipeline.main(argv) == 0
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    data = json.loads(state_json.read_text(encoding="utf-8"))
    assert data["phase1_autonomous"]["current_step"] == p1_fsm.STEP_MID_SNAPSHOT
    assert int(data["phase1_autonomous"].get("stub_observe_ticks") or 0) == 0


def test_main_autonomous_mid_r1_once_requires_autonomous_once(tmp_path: Path) -> None:
    """--autonomous-mid-r1-once without --autonomous-once is invalid."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            "pytest_p1_mid_once_alone",
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
            "--autonomous-mid-r1-once",
        ]
    )
    assert rc == orch_exits.EXIT_CONFIG_INVALID


def test_main_autonomous_mid_r1_once_with_dry_run_is_config_invalid(tmp_path: Path) -> None:
    """--autonomous-mid-r1-once is incompatible with --dry-run."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            "pytest_p1_mid_dry_bad",
            "--dry-run",
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
            "--autonomous-once",
            "--autonomous-mid-r1-once",
        ]
    )
    assert rc == orch_exits.EXIT_CONFIG_INVALID


def test_main_autonomous_mid_r1_once_not_eligible_returns_twelve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--autonomous-mid-r1-once exits 12 when observe_context is not gate-eligible."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    run_id = f"pytest_p1_mid_ne_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(
        run_pipeline.runner,
        "run_preflight",
        lambda *a, **k: {"ok": True, "error_code": None, "message": None, "checks": []},
    )
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            run_id,
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
            "--autonomous-once",
            "--autonomous-mid-r1-once",
        ]
    )
    assert rc == orch_exits.EXIT_PHASE1_AUTONOMOUS_MID_NOT_ELIGIBLE


def test_main_autonomous_mid_r1_once_invokes_mid_when_eligible_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T8C: eligible observe_context + mocked R1 runner records mid snapshot success."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    state_db = tmp_path / "st.db"
    pred_db = tmp_path / "pr.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute("CREATE TABLE alerts (x INT)")
        conn.execute(
            "CREATE TABLE validation_results (alert_ts TEXT, validated_at TEXT, result INT)"
        )
        for _ in range(5):
            conn.execute(
                "INSERT INTO validation_results VALUES (?, ?, 1)",
                ("2026-01-04T12:00:00+08:00", "2026-01-05T00:00:00+08:00"),
            )
        conn.commit()
    with sqlite3.connect(pred_db) as conn:
        conn.execute("CREATE TABLE prediction_log (a INT)")
        conn.commit()
    cfg = _minimal_config_dict()
    cfg["model_dir"] = str(model_dir)
    cfg["state_db_path"] = str(state_db)
    cfg["prediction_log_db_path"] = str(pred_db)
    cfg["window"] = {
        "start_ts": "2026-01-01T00:00:00+08:00",
        "end_ts": "2026-01-08T00:00:00+08:00",
    }
    thr = dict(cfg["thresholds"])
    thr["min_hours_preliminary"] = 1
    thr["min_finalized_alerts_preliminary"] = 2
    thr["min_finalized_true_positives_preliminary"] = 2
    thr["min_hours_gate"] = 1
    thr["min_finalized_alerts_gate"] = 3
    thr["min_finalized_true_positives_gate"] = 3
    cfg["thresholds"] = thr
    cfg_path = tmp_path / "mid_elig.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    run_id = f"pytest_p1_mid_ok_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(
        run_pipeline.runner,
        "run_preflight",
        lambda *a, **k: {"ok": True, "error_code": None, "message": None, "checks": []},
    )
    calls: list[tuple[Any, ...]] = []

    def fake_r1(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append((a, k))
        return {
            "ok": True,
            "message": None,
            "error_code": None,
            "stdout_path": "/dev/null",
            "stderr_path": "/dev/null",
        }

    monkeypatch.setattr(run_pipeline.runner, "run_phase1_r1_r6_all", fake_r1)
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            run_id,
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
            "--autonomous-once",
            "--autonomous-mid-r1-once",
        ]
    )
    assert rc == 0
    assert len(calls) >= 1
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    data = json.loads(state_json.read_text(encoding="utf-8"))
    assert data["steps"].get("r1_r6_mid_snapshot", {}).get("status") == "success"
    assert data["phase1_autonomous"].get("last_autonomous_mid_r1_at")


def test_phase1_autonomous_dry_run_registers_fsm_in_run_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8A: --dry-run + --mode autonomous writes phase1_autonomous + snapshot step."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    run_id = "pytest_p1_autonomous_fsm_dry"
    monkeypatch.setattr(run_pipeline.runner, "run_r1_r6_cli_smoke", lambda *a, **k: (True, None))
    monkeypatch.setattr(run_pipeline.runner, "run_backtest_cli_smoke", lambda *a, **k: (True, None))
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            run_id,
            "--dry-run",
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
        ]
    )
    assert rc == 0
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    data = json.loads(state_json.read_text(encoding="utf-8"))
    assert data.get("cli_run_mode") == "autonomous"
    pa = data.get("phase1_autonomous")
    assert isinstance(pa, dict)
    assert pa.get("fsm_schema_version") == 1
    assert pa.get("current_step") == p1_fsm.STEP_INIT
    assert data["steps"].get("phase1_autonomous_fsm_snapshot", {}).get("status") == "success"


def test_phase1_autonomous_once_chains_observe_ticks_without_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T8A: two --autonomous-once invocations read disk cursor (no --resume required)."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    run_id = f"pytest_p1_autonomous_once_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(
        run_pipeline.runner,
        "run_preflight",
        lambda *a, **k: {"ok": True, "error_code": None, "message": None, "checks": []},
    )
    argv_base = [
        "--phase",
        "phase1",
        "--config",
        str(cfg_path.resolve()),
        "--run-id",
        run_id,
        "--skip-backtest-smoke",
        "--mode",
        "autonomous",
        "--autonomous-once",
    ]
    assert run_pipeline.main(argv_base) == 0
    assert run_pipeline.main(argv_base) == 0
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    data = json.loads(state_json.read_text(encoding="utf-8"))
    pa = data.get("phase1_autonomous")
    assert pa.get("current_step") == p1_fsm.STEP_OBSERVE
    assert pa.get("stub_observe_ticks") == 1
    assert pa.get("tick_seq") == 2
    assert data["steps"].get("phase1_autonomous_stub_tick", {}).get("status") == "success"
    oc = pa.get("observe_context")
    assert isinstance(oc, dict)
    assert oc.get("window_hours") == 168.0
    assert oc.get("observation_gate_hint") == "preliminary_ok"
    assert oc.get("samples_preliminary_hint") == "below_preliminary"
    # FSM fixture creates empty validation_results (schema-only for preflight).
    assert oc.get("finalized_alerts_count") == 0
    assert oc.get("gate_hours_hint") == "ok"
    assert oc.get("gate_sample_hint") == "below"
    assert oc.get("mid_snapshot_eligible") is False


def test_phase1_autonomous_resume_once_continues_tick_seq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T8A: --resume reloads run_state so stub tick continues tick_seq (process restart sim)."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    run_id = f"pytest_p1_auto_resume_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(
        run_pipeline.runner,
        "run_preflight",
        lambda *a, **k: {"ok": True, "error_code": None, "message": None, "checks": []},
    )
    argv_once = [
        "--phase",
        "phase1",
        "--config",
        str(cfg_path.resolve()),
        "--run-id",
        run_id,
        "--skip-backtest-smoke",
        "--mode",
        "autonomous",
        "--autonomous-once",
    ]
    assert run_pipeline.main(argv_once) == 0
    argv_resume = argv_once + ["--resume"]
    assert run_pipeline.main(argv_resume) == 0
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    data = json.loads(state_json.read_text(encoding="utf-8"))
    pa = data.get("phase1_autonomous")
    assert pa.get("tick_seq") == 2
    assert pa.get("stub_observe_ticks") == 1
    assert pa.get("checkpoint", {}).get("cursor_before") == p1_fsm.STEP_OBSERVE


def test_phase1_autonomous_without_dry_run_returns_pending_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8A: autonomous without --dry-run exits 11 after preflight (supervisor not implemented)."""
    cfg_path = _write_minimal_phase1_yaml_for_fsm(tmp_path)
    run_id = "pytest_p1_autonomous_pending"
    monkeypatch.setattr(
        run_pipeline.runner,
        "run_preflight",
        lambda *a, **k: {"ok": True, "error_code": None, "message": None, "checks": []},
    )
    rc = run_pipeline.main(
        [
            "--phase",
            "phase1",
            "--config",
            str(cfg_path.resolve()),
            "--run-id",
            run_id,
            "--skip-backtest-smoke",
            "--mode",
            "autonomous",
        ]
    )
    assert rc == orch_exits.EXIT_PHASE1_AUTONOMOUS_PENDING
    state_json = _ORCHESTRATOR / "state" / run_id / "run_state.json"
    data = json.loads(state_json.read_text(encoding="utf-8"))
    assert data["phase1_autonomous"].get("full_run_status") == "pending_t8a_supervisor"


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
    assert "Mid R1/R6 snapshots" in gate_md
    assert "筆數（log 列）" in gate_md and "`0`" in gate_md


def test_phase1_gate_decision_mid_snapshots_section_lists_paths_and_pats(tmp_path: Path) -> None:
    """phase1_gate_decision.md lists mid row count, PAT sequence, and stdout paths."""
    phase1 = tmp_path / "phase1"
    cfg = _minimal_config_dict()
    p0 = _pat_block(0.31)
    p1 = _pat_block(0.32)
    bundle = {
        "run_id": "mid_rep",
        "errors": [],
        "window": dict(cfg["window"]),
        "thresholds": dict(cfg["thresholds"]),
        "backtest_metrics": None,
        "state_db_stats": {"finalized_alerts_count": 1},
        "r1_r6_final": {"payload": None},
        "r1_r6_mid": {"payload": p1},
        "r1_r6_mid_snapshots": [
            {
                "checkpoint_index": 1,
                "stdout_log": "/tmp/orch/state/mid_rep/logs/r1_r6_mid_cp1.stdout.log",
                "payload": p0,
                "parse_error": None,
            },
            {
                "checkpoint_index": None,
                "stdout_log": "/tmp/orch/state/mid_rep/logs/r1_r6_mid.stdout.log",
                "payload": p1,
                "parse_error": None,
                "is_canonical_mid_alias": True,
            },
        ],
    }
    gate = evaluators.evaluate_phase1_gate(bundle)
    report_builder.write_phase1_reports(phase1, "mid_rep", cfg, bundle, gate)
    gate_md = (phase1 / "phase1_gate_decision.md").read_text(encoding="utf-8")
    assert "筆數（log 列）**: `2`" in gate_md
    assert "0.3100, 0.3200" in gate_md
    assert "r1_r6_mid_cp1.stdout.log" in gate_md
    assert "r1_r6_mid.stdout.log" in gate_md
    assert "checkpoint `cp1`" in gate_md
    assert "canonical `r1_r6_mid`" in gate_md


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
    assert proc.returncode == orch_exits.EXIT_DRY_RUN_NOT_READY
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
    assert proc.returncode == orch_exits.EXIT_CONFIG_INVALID


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


def test_t16a_dry_run_checklist_keys_match_config_loader_contract() -> None:
    """T16A ``dry_run`` checklist keys stay aligned with MVP_TASKLIST / run_full SSOT."""
    expected = frozenset(
        {
            "validate_phase_configs_exist",
            "validate_phase_schemas",
            "validate_phase_dependencies",
            "validate_contract_consistency",
            "validate_paths_readable",
            "validate_writable_targets",
            "validate_cli_smoke_per_phase",
            "validate_resource_limits",
            "fail_on_any_check",
        }
    )
    assert frozenset(config_loader.DRY_RUN_FLAG_DEFAULTS.keys()) == expected
