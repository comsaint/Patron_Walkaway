"""Load and validate Phase 1 orchestrator YAML config."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

REQUIRED_ROOT_KEYS: tuple[str, ...] = (
    "model_dir",
    "state_db_path",
    "prediction_log_db_path",
    "window",
    "thresholds",
)
REQUIRED_WINDOW_KEYS: tuple[str, ...] = ("start_ts", "end_ts")
REQUIRED_THRESHOLD_KEYS: tuple[str, ...] = (
    "min_hours_preliminary",
    "min_finalized_alerts_preliminary",
    "min_finalized_true_positives_preliminary",
    "min_hours_gate",
    "min_finalized_alerts_gate",
    "min_finalized_true_positives_gate",
)


class ConfigValidationError(ValueError):
    """Raised when config YAML is missing required fields or has wrong types."""

    def __init__(self, message: str) -> None:
        super().__init__(f"E_CONFIG_INVALID: {message}")


def load_raw_config(path: Path) -> dict[str, Any]:
    """Parse YAML config file into a dict.

    Args:
        path: Path to YAML file.

    Returns:
        Parsed mapping.

    Raises:
        ConfigValidationError: If file is missing or YAML is invalid.
        OSError: If the file cannot be read.
    """
    if not path.is_file():
        raise ConfigValidationError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"invalid YAML: {exc}") from exc
    if raw is None or not isinstance(raw, dict):
        raise ConfigValidationError(
            f"config must be a mapping at root, got {type(raw).__name__}"
        )
    return raw


def validate_phase1_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate required Phase 1 fields and return the same mapping as a plain dict.

    Args:
        raw: Parsed YAML root mapping.

    Returns:
        Shallow-validated config dict.

    Raises:
        ConfigValidationError: On missing keys or wrong nested types.
    """
    missing = [k for k in REQUIRED_ROOT_KEYS if k not in raw]
    if missing:
        raise ConfigValidationError(
            f"missing keys {missing}; required {list(REQUIRED_ROOT_KEYS)}"
        )

    window = raw["window"]
    if not isinstance(window, Mapping):
        raise ConfigValidationError(
            f"window must be a mapping, got {type(window).__name__}"
        )
    w_missing = [k for k in REQUIRED_WINDOW_KEYS if k not in window]
    if w_missing:
        raise ConfigValidationError(
            f"window missing {w_missing}; required {list(REQUIRED_WINDOW_KEYS)}"
        )

    thresholds = raw["thresholds"]
    if not isinstance(thresholds, Mapping):
        raise ConfigValidationError(
            f"thresholds must be a mapping, got {type(thresholds).__name__}"
        )
    t_missing = [k for k in REQUIRED_THRESHOLD_KEYS if k not in thresholds]
    if t_missing:
        raise ConfigValidationError(
            f"thresholds missing {t_missing}; required {list(REQUIRED_THRESHOLD_KEYS)}"
        )

    return dict(raw)


def load_phase1_config(path: Path) -> dict[str, Any]:
    """Load YAML from path and validate Phase 1 schema.

    Args:
        path: Config file path.

    Returns:
        Validated config dictionary.
    """
    return validate_phase1_config(load_raw_config(path))


PHASE2_ROOT_KEYS: tuple[str, ...] = (
    "phase",
    "common",
    "resources",
    "tracks",
    "gate",
)
PHASE2_COMMON_KEYS: tuple[str, ...] = (
    "model_dir",
    "state_db_path",
    "prediction_log_db_path",
    "window",
    "contract",
)
PHASE2_CONTRACT_KEYS: tuple[str, ...] = ("metric", "timezone", "exclude_censored")
PHASE2_RESOURCE_KEYS: tuple[str, ...] = (
    "max_windows",
    "max_trials_per_track",
    "max_parallel_jobs",
    "backtest_skip_optuna",
)
PHASE2_TRACK_NAMES: tuple[str, ...] = ("track_a", "track_b", "track_c")
PHASE2_GATE_KEYS: tuple[str, ...] = ("min_uplift_pp_vs_baseline", "max_std_pp_across_windows")

# T10A: only these keys may appear under ``tracks.*.experiments[].trainer_params`` (maps to trainer CLI).
PHASE2_TRAINER_PARAM_KEYS: tuple[str, ...] = (
    "use_local_parquet",
    "skip_optuna",
    "recent_chunks",
    "sample_rated",
    "lgbm_device",
)


def _validate_phase2_experiment_trainer_params(
    tp: Mapping[str, Any],
    *,
    track: str,
    exp_index: int,
) -> None:
    """Validate a single experiment's ``trainer_params`` mapping (T10A).

    Raises:
        ConfigValidationError: On unknown keys or wrong value types.
    """
    unknown = sorted(str(k) for k in tp if str(k) not in PHASE2_TRAINER_PARAM_KEYS)
    if unknown:
        raise ConfigValidationError(
            f"tracks.{track}.experiments[{exp_index}].trainer_params has unknown keys {unknown}; "
            f"allowed {list(PHASE2_TRAINER_PARAM_KEYS)}"
        )
    for key in PHASE2_TRAINER_PARAM_KEYS:
        if key not in tp:
            continue
        val = tp[key]
        if key in ("use_local_parquet", "skip_optuna"):
            if not isinstance(val, bool):
                raise ConfigValidationError(
                    f"tracks.{track}.experiments[{exp_index}].trainer_params.{key} must be bool, "
                    f"got {type(val).__name__}"
                )
        elif key in ("recent_chunks", "sample_rated"):
            if isinstance(val, bool):
                raise ConfigValidationError(
                    f"tracks.{track}.experiments[{exp_index}].trainer_params.{key} must be int, "
                    f"got {type(val).__name__}"
                )
            if isinstance(val, float) and not val.is_integer():
                raise ConfigValidationError(
                    f"tracks.{track}.experiments[{exp_index}].trainer_params.{key} must be a whole "
                    f"number, got {val!r}"
                )
            try:
                n = int(val)
            except (TypeError, ValueError) as exc:
                raise ConfigValidationError(
                    f"tracks.{track}.experiments[{exp_index}].trainer_params.{key} must be int, "
                    f"got {type(val).__name__}"
                ) from exc
            if n < 1:
                raise ConfigValidationError(
                    f"tracks.{track}.experiments[{exp_index}].trainer_params.{key} must be >= 1, "
                    f"got {val!r}"
                )
        elif key == "lgbm_device":
            if not isinstance(val, str) or not str(val).strip():
                raise ConfigValidationError(
                    f"tracks.{track}.experiments[{exp_index}].trainer_params.lgbm_device "
                    "must be a non-empty string"
                )


def validate_phase2_config(raw: Mapping[str, Any], *, cli_run_id: str) -> dict[str, Any]:
    """Validate Phase 2 orchestrator YAML (T9 schema).

    Args:
        raw: Parsed YAML root mapping.
        cli_run_id: ``--run-id`` from CLI (authoritative for ``run_state`` paths).

    Returns:
        Validated config dict.

    Raises:
        ConfigValidationError: On missing keys, wrong types, or run_id mismatch.
    """
    missing = [k for k in PHASE2_ROOT_KEYS if k not in raw]
    if missing:
        raise ConfigValidationError(
            f"phase2 missing keys {missing}; required {list(PHASE2_ROOT_KEYS)}"
        )

    phase_val = raw["phase"]
    if str(phase_val).strip() != "phase2":
        raise ConfigValidationError(
            f"phase2 config phase must be 'phase2', got {phase_val!r}"
        )

    yaml_run_id = raw.get("run_id")
    if yaml_run_id is not None and str(yaml_run_id).strip():
        if str(yaml_run_id).strip() != str(cli_run_id).strip():
            raise ConfigValidationError(
                f"run_id mismatch: yaml run_id={yaml_run_id!r} vs cli --run-id={cli_run_id!r}"
            )

    common = raw["common"]
    if not isinstance(common, Mapping):
        raise ConfigValidationError(
            f"common must be a mapping, got {type(common).__name__}"
        )
    c_missing = [k for k in PHASE2_COMMON_KEYS if k not in common]
    if c_missing:
        raise ConfigValidationError(
            f"common missing {c_missing}; required {list(PHASE2_COMMON_KEYS)}"
        )

    window = common["window"]
    if not isinstance(window, Mapping):
        raise ConfigValidationError(
            f"common.window must be a mapping, got {type(window).__name__}"
        )
    w_missing = [k for k in REQUIRED_WINDOW_KEYS if k not in window]
    if w_missing:
        raise ConfigValidationError(
            f"common.window missing {w_missing}; required {list(REQUIRED_WINDOW_KEYS)}"
        )

    contract = common["contract"]
    if not isinstance(contract, Mapping):
        raise ConfigValidationError(
            f"common.contract must be a mapping, got {type(contract).__name__}"
        )
    ct_missing = [k for k in PHASE2_CONTRACT_KEYS if k not in contract]
    if ct_missing:
        raise ConfigValidationError(
            f"common.contract missing {ct_missing}; required {list(PHASE2_CONTRACT_KEYS)}"
        )

    resources = raw["resources"]
    if not isinstance(resources, Mapping):
        raise ConfigValidationError(
            f"resources must be a mapping, got {type(resources).__name__}"
        )
    r_missing = [k for k in PHASE2_RESOURCE_KEYS if k not in resources]
    if r_missing:
        raise ConfigValidationError(
            f"resources missing {r_missing}; required {list(PHASE2_RESOURCE_KEYS)}"
        )

    tracks = raw["tracks"]
    if not isinstance(tracks, Mapping):
        raise ConfigValidationError(
            f"tracks must be a mapping, got {type(tracks).__name__}"
        )
    for tn in PHASE2_TRACK_NAMES:
        if tn not in tracks:
            raise ConfigValidationError(
                f"tracks missing {tn!r}; required {list(PHASE2_TRACK_NAMES)}"
            )
        block = tracks[tn]
        if not isinstance(block, Mapping):
            raise ConfigValidationError(
                f"tracks.{tn} must be a mapping, got {type(block).__name__}"
            )
        if "enabled" not in block:
            raise ConfigValidationError(f"tracks.{tn} missing enabled (bool)")
        if not isinstance(block["enabled"], bool):
            raise ConfigValidationError(
                f"tracks.{tn}.enabled must be bool, got {type(block['enabled']).__name__}"
            )
        if "experiments" not in block:
            raise ConfigValidationError(f"tracks.{tn} missing experiments (list)")
        exps = block["experiments"]
        if not isinstance(exps, list) or not exps:
            raise ConfigValidationError(
                f"tracks.{tn}.experiments must be a non-empty list"
            )
        for i, exp in enumerate(exps):
            if not isinstance(exp, Mapping):
                raise ConfigValidationError(
                    f"tracks.{tn}.experiments[{i}] must be a mapping"
                )
            if "exp_id" not in exp or not str(exp["exp_id"]).strip():
                raise ConfigValidationError(
                    f"tracks.{tn}.experiments[{i}] missing non-empty exp_id"
                )
            ov = exp.get("overrides", {})
            if not isinstance(ov, Mapping):
                raise ConfigValidationError(
                    f"tracks.{tn}.experiments[{i}].overrides must be a mapping"
                )
            legacy_keys = [str(k) for k in ov if str(k).strip()]
            if legacy_keys:
                raise ConfigValidationError(
                    f"tracks.{tn}.experiments[{i}].overrides must be empty (T10A); "
                    f"found unsupported keys {sorted(legacy_keys)} — use "
                    f"trainer_params with whitelist {list(PHASE2_TRAINER_PARAM_KEYS)}"
                )
            tp_raw = exp.get("trainer_params")
            if tp_raw is None:
                pass
            elif not isinstance(tp_raw, Mapping):
                raise ConfigValidationError(
                    f"tracks.{tn}.experiments[{i}].trainer_params must be a mapping or omitted, "
                    f"got {type(tp_raw).__name__}"
                )
            else:
                _validate_phase2_experiment_trainer_params(
                    tp_raw, track=tn, exp_index=i
                )
            tm_opt = exp.get("training_metrics_repo_relative")
            if tm_opt is not None:
                if not isinstance(tm_opt, str) or not str(tm_opt).strip():
                    raise ConfigValidationError(
                        f"tracks.{tn}.experiments[{i}].training_metrics_repo_relative "
                        "must be a non-empty string when set"
                    )
            pab = exp.get("precision_at_recall_1pct_by_window")
            if pab is not None:
                if not isinstance(pab, list) or not pab:
                    raise ConfigValidationError(
                        f"tracks.{tn}.experiments[{i}].precision_at_recall_1pct_by_window "
                        "must be a non-empty list when set"
                    )
                for j, v in enumerate(pab):
                    try:
                        float(v)
                    except (TypeError, ValueError) as exc:
                        raise ConfigValidationError(
                            f"tracks.{tn}.experiments[{i}].precision_at_recall_1pct_by_window[{j}] "
                            "must be numeric"
                        ) from exc

    gate = raw["gate"]
    if not isinstance(gate, Mapping):
        raise ConfigValidationError(f"gate must be a mapping, got {type(gate).__name__}")
    g_missing = [k for k in PHASE2_GATE_KEYS if k not in gate]
    if g_missing:
        raise ConfigValidationError(
            f"gate missing {g_missing}; required {list(PHASE2_GATE_KEYS)}"
        )

    return dict(raw)


def load_phase2_config(path: Path, *, cli_run_id: str) -> dict[str, Any]:
    """Load YAML and validate Phase 2 schema.

    Args:
        path: Config file path.
        cli_run_id: CLI ``--run-id`` (must match optional yaml ``run_id``).

    Returns:
        Validated Phase 2 config dict.
    """
    return validate_phase2_config(load_raw_config(path), cli_run_id=cli_run_id)


# --- run_full.yaml (all-phase orchestrator root; T16A dry-run) ---

RUN_FULL_ROOT_KEYS: tuple[str, ...] = ("phase", "execution", "phase_configs")
RUN_FULL_EXECUTION_KEYS: tuple[str, ...] = (
    "phase_order",
    "stop_on_gate_block",
    "allow_force_next",
)

DRY_RUN_FLAG_DEFAULTS: dict[str, bool] = {
    "validate_phase_configs_exist": True,
    "validate_phase_schemas": True,
    "validate_phase_dependencies": True,
    "validate_contract_consistency": True,
    "validate_paths_readable": True,
    "validate_writable_targets": True,
    "validate_cli_smoke_per_phase": True,
    "validate_resource_limits": True,
    "fail_on_any_check": True,
}

PHASE3_ROOT_KEYS: tuple[str, ...] = (
    "phase",
    "upstream",
    "common",
    "resources",
    "workstreams",
    "gate",
)
PHASE3_UPSTREAM_KEYS: tuple[str, ...] = ("phase2_run_id", "winner_track", "winner_exp_id")

PHASE4_ROOT_KEYS: tuple[str, ...] = ("phase", "candidate", "evaluation", "resources", "gate")
PHASE4_CANDIDATE_KEYS: tuple[str, ...] = ("model_dir", "source_phase3_run_id", "threshold_strategy")


def _normalize_dry_run_flags(raw: Mapping[str, Any]) -> dict[str, bool]:
    """Merge optional ``dry_run`` block with safe defaults."""
    dr = raw.get("dry_run")
    if dr is None:
        return dict(DRY_RUN_FLAG_DEFAULTS)
    if not isinstance(dr, Mapping):
        raise ConfigValidationError(
            f"dry_run must be a mapping or omitted, got {type(dr).__name__}"
        )
    out = dict(DRY_RUN_FLAG_DEFAULTS)
    for key, default in DRY_RUN_FLAG_DEFAULTS.items():
        if key in dr:
            val = dr[key]
            if not isinstance(val, bool):
                raise ConfigValidationError(
                    f"dry_run.{key} must be bool, got {type(val).__name__}"
                )
            out[key] = val
    return out


def validate_phase3_config_minimal(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate minimal Phase 3 YAML shape (until full T12 schema lands).

    Args:
        raw: Parsed YAML root.

    Returns:
        Validated dict.

    Raises:
        ConfigValidationError: On missing keys or wrong types.
    """
    missing = [k for k in PHASE3_ROOT_KEYS if k not in raw]
    if missing:
        raise ConfigValidationError(
            f"phase3 missing keys {missing}; required {list(PHASE3_ROOT_KEYS)}"
        )
    if str(raw["phase"]).strip() != "phase3":
        raise ConfigValidationError(
            f"phase3 config phase must be 'phase3', got {raw['phase']!r}"
        )
    up = raw["upstream"]
    if not isinstance(up, Mapping):
        raise ConfigValidationError(
            f"upstream must be a mapping, got {type(up).__name__}"
        )
    u_missing = [k for k in PHASE3_UPSTREAM_KEYS if k not in up]
    if u_missing:
        raise ConfigValidationError(
            f"upstream missing {u_missing}; required {list(PHASE3_UPSTREAM_KEYS)}"
        )
    common = raw["common"]
    if not isinstance(common, Mapping):
        raise ConfigValidationError(
            f"common must be a mapping, got {type(common).__name__}"
        )
    if "contract" not in common:
        raise ConfigValidationError("common.contract is required for phase3")
    contract = common["contract"]
    if not isinstance(contract, Mapping):
        raise ConfigValidationError(
            f"common.contract must be a mapping, got {type(contract).__name__}"
        )
    ct_missing = [k for k in PHASE2_CONTRACT_KEYS if k not in contract]
    if ct_missing:
        raise ConfigValidationError(
            f"common.contract missing {ct_missing}; required {list(PHASE2_CONTRACT_KEYS)}"
        )
    resources = raw["resources"]
    if not isinstance(resources, Mapping):
        raise ConfigValidationError(
            f"resources must be a mapping, got {type(resources).__name__}"
        )
    if "max_parallel_jobs" not in resources:
        raise ConfigValidationError("resources.max_parallel_jobs is required for phase3")
    ws = raw["workstreams"]
    if not isinstance(ws, Mapping):
        raise ConfigValidationError(
            f"workstreams must be a mapping, got {type(ws).__name__}"
        )
    gate = raw["gate"]
    if not isinstance(gate, Mapping):
        raise ConfigValidationError(f"gate must be a mapping, got {type(gate).__name__}")
    return dict(raw)


def validate_phase4_config_minimal(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate minimal Phase 4 YAML shape (until full T14 schema lands).

    Args:
        raw: Parsed YAML root.

    Returns:
        Validated dict.

    Raises:
        ConfigValidationError: On missing keys or wrong types.
    """
    missing = [k for k in PHASE4_ROOT_KEYS if k not in raw]
    if missing:
        raise ConfigValidationError(
            f"phase4 missing keys {missing}; required {list(PHASE4_ROOT_KEYS)}"
        )
    if str(raw["phase"]).strip() != "phase4":
        raise ConfigValidationError(
            f"phase4 config phase must be 'phase4', got {raw['phase']!r}"
        )
    cand = raw["candidate"]
    if not isinstance(cand, Mapping):
        raise ConfigValidationError(
            f"candidate must be a mapping, got {type(cand).__name__}"
        )
    c_missing = [k for k in PHASE4_CANDIDATE_KEYS if k not in cand]
    if c_missing:
        raise ConfigValidationError(
            f"candidate missing {c_missing}; required {list(PHASE4_CANDIDATE_KEYS)}"
        )
    ev = raw["evaluation"]
    if not isinstance(ev, Mapping):
        raise ConfigValidationError(
            f"evaluation must be a mapping, got {type(ev).__name__}"
        )
    if "windows" not in ev or not isinstance(ev["windows"], list) or not ev["windows"]:
        raise ConfigValidationError("evaluation.windows must be a non-empty list")
    if "contract" not in ev:
        raise ConfigValidationError("evaluation.contract is required for phase4")
    contract = ev["contract"]
    if not isinstance(contract, Mapping):
        raise ConfigValidationError(
            f"evaluation.contract must be a mapping, got {type(contract).__name__}"
        )
    ct_missing = [k for k in PHASE2_CONTRACT_KEYS if k not in contract]
    if ct_missing:
        raise ConfigValidationError(
            f"evaluation.contract missing {ct_missing}; required {list(PHASE2_CONTRACT_KEYS)}"
        )
    resources = raw["resources"]
    if not isinstance(resources, Mapping):
        raise ConfigValidationError(
            f"resources must be a mapping, got {type(resources).__name__}"
        )
    if "max_parallel_jobs" not in resources:
        raise ConfigValidationError("resources.max_parallel_jobs is required for phase4")
    gate = raw["gate"]
    if not isinstance(gate, Mapping):
        raise ConfigValidationError(f"gate must be a mapping, got {type(gate).__name__}")
    return dict(raw)


def validate_run_full_config(raw: Mapping[str, Any], *, cli_run_id: str) -> dict[str, Any]:
    """Validate ``run_full.yaml`` root (all-phase orchestrator).

    Args:
        raw: Parsed YAML root.
        cli_run_id: CLI ``--run-id`` (authoritative; optional yaml ``run_id`` must match).

    Returns:
        Config dict with normalized ``dry_run`` flags.

    Raises:
        ConfigValidationError: On invalid structure.
    """
    missing = [k for k in RUN_FULL_ROOT_KEYS if k not in raw]
    if missing:
        raise ConfigValidationError(
            f"run_full missing keys {missing}; required {list(RUN_FULL_ROOT_KEYS)}"
        )
    if str(raw["phase"]).strip() != "all":
        raise ConfigValidationError(
            f"run_full phase must be 'all', got {raw['phase']!r}"
        )
    yaml_run_id = raw.get("run_id")
    if yaml_run_id is not None and str(yaml_run_id).strip():
        if str(yaml_run_id).strip() != str(cli_run_id).strip():
            raise ConfigValidationError(
                f"run_id mismatch: yaml run_id={yaml_run_id!r} vs cli --run-id={cli_run_id!r}"
            )
    execution = raw["execution"]
    if not isinstance(execution, Mapping):
        raise ConfigValidationError(
            f"execution must be a mapping, got {type(execution).__name__}"
        )
    e_missing = [k for k in RUN_FULL_EXECUTION_KEYS if k not in execution]
    if e_missing:
        raise ConfigValidationError(
            f"execution missing {e_missing}; required {list(RUN_FULL_EXECUTION_KEYS)}"
        )
    order = execution["phase_order"]
    if not isinstance(order, list) or not order:
        raise ConfigValidationError("execution.phase_order must be a non-empty list")
    for i, p in enumerate(order):
        if str(p).strip() not in ("phase1", "phase2", "phase3", "phase4"):
            raise ConfigValidationError(
                f"execution.phase_order[{i}] must be phase1|phase2|phase3|phase4, got {p!r}"
            )
    for key in ("stop_on_gate_block", "allow_force_next"):
        if not isinstance(execution[key], bool):
            raise ConfigValidationError(
                f"execution.{key} must be bool, got {type(execution[key]).__name__}"
            )
    pc = raw["phase_configs"]
    if not isinstance(pc, Mapping):
        raise ConfigValidationError(
            f"phase_configs must be a mapping, got {type(pc).__name__}"
        )
    for pk in ("phase1", "phase2", "phase3", "phase4"):
        if pk not in pc:
            raise ConfigValidationError(
                f"phase_configs missing {pk!r}; required "
                "phase1, phase2, phase3, phase4 paths"
            )
        pth = pc[pk]
        if not isinstance(pth, str) or not pth.strip():
            raise ConfigValidationError(f"phase_configs.{pk} must be a non-empty string path")
    out = dict(raw)
    out["dry_run"] = _normalize_dry_run_flags(raw)
    return out


def load_run_full_config(path: Path, *, cli_run_id: str) -> dict[str, Any]:
    """Load and validate ``run_full.yaml``."""
    return validate_run_full_config(load_raw_config(path), cli_run_id=cli_run_id)
