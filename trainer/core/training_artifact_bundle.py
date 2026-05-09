"""Training artifact bundle writers (DEC-021 / pipeline diagnostics / MLflow provenance).

Moved from ``trainer.training.trainer`` for Issue #33 Phase D strict split.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

import joblib

from trainer.core import config as _cfg
from trainer.core.mlflow_utils import (
    has_active_run,
    log_params_safe,
    safe_start_run,
)
from trainer.core.training_metrics_v2_bundle_write import write_training_metrics_v2_sidecars
from trainer.features import (
    PROFILE_FEATURE_COLS,
    get_candidate_feature_ids,
    load_feature_spec,
    resolve_spec_track_section,
)
from trainer.training.common_runtime import BASE_DIR, FEATURE_SPEC_PATH, MODEL_DIR
from trainer.training.two_stage import A4_FUSION_MODE_PRODUCT

logger = logging.getLogger("trainer")

MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH = "model_bundle"

PRODUCTION_NEG_POS_RATIO: Optional[float] = getattr(_cfg, "PRODUCTION_NEG_POS_RATIO", None)
SELECTION_MODE: str = str(getattr(_cfg, "SELECTION_MODE", "field_test") or "field_test").strip() or "field_test"
THRESHOLD_MIN_RECALL: Optional[float] = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)


def _log_training_provenance_to_mlflow(
    model_version: str,
    artifact_dir: str,
    training_window_start: Union[datetime, str],
    training_window_end: Union[datetime, str],
    feature_spec_path: str,
    training_metrics_path: str,
    git_commit: Optional[str] = None,
    pipeline_diagnostics_path: Optional[str] = None,
    pipeline_diagnostics_rel_path: Optional[str] = None,
    model_metadata_path: Optional[str] = None,
    model_metadata_rel_path: Optional[str] = None,
    split_boundary_params: Optional[dict[str, str]] = None,
) -> None:
    """Phase 2 T2: Log training provenance to MLflow (no-op when URI unset/unreachable).

    See doc/phase2_provenance_schema.md for key names. On failure (no URI, network
    error), logs warning only; training is still considered successful.

    ``pipeline_diagnostics_*`` default from ``artifact_dir`` when omitted (same directory
    as ``training_metrics.json`` convention). Provenance may run before the diagnostics
    file is written; paths still denote the canonical bundle location.

    Optional ``model_metadata_*`` and ``split_boundary_params`` extend the Phase 2 schema
    with per-split time bounds (string params) and ``model_metadata.json`` paths.
    """
    if git_commit is None:
        try:
            git_commit = (
                subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=BASE_DIR,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
        except Exception:
            git_commit = "nogit"
    _start = training_window_start.isoformat() if hasattr(training_window_start, "isoformat") else str(training_window_start)
    _end = training_window_end.isoformat() if hasattr(training_window_end, "isoformat") else str(training_window_end)
    _artifact = Path(artifact_dir)
    _pd_path = pipeline_diagnostics_path
    if _pd_path is None:
        _pd_path = str(_artifact / "pipeline_diagnostics.json")
    _pd_rel = pipeline_diagnostics_rel_path
    if _pd_rel is None:
        _pd_rel = f"{_artifact.name}/pipeline_diagnostics.json"
    params = {
        "model_version": model_version,
        "git_commit": git_commit,
        "training_window_start": _start,
        "training_window_end": _end,
        "artifact_dir": artifact_dir,
        "feature_spec_path": feature_spec_path,
        "training_metrics_path": training_metrics_path,
        "pipeline_diagnostics_path": _pd_path,
        "pipeline_diagnostics_rel_path": _pd_rel,
    }
    if model_metadata_path:
        params["model_metadata_path"] = model_metadata_path
    if model_metadata_rel_path:
        params["model_metadata_rel_path"] = model_metadata_rel_path
    if split_boundary_params:
        params.update(split_boundary_params)
    _model_pkl = _artifact / "model.pkl"
    if _model_pkl.is_file():
        params["mlflow_trained_model_artifact"] = (
            f"{MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH}/model.pkl"
        )
    # T12: if pipeline already started a run (e.g. at pipeline entry), log to it; else start one.
    if has_active_run():
        log_params_safe(params)
    else:
        with safe_start_run(run_name=model_version):
            log_params_safe(params)


def save_artifact_bundle(
    rated: Optional[dict],
    feature_cols: List[str],
    combined_metrics: dict,
    model_version: str,
    sample_rated_n: Optional[int] = None,
    feature_spec_path: Optional[Path] = None,
    neg_sample_frac: float = 1.0,
    bundle_dir: Optional[Path] = None,
    baseline_training_alignment: Optional[dict[str, Any]] = None,
    model_metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Write all model artifacts atomically (v10 rated artifact entry, DEC-021).

    When *bundle_dir* is set, artifacts are written there; otherwise :data:`MODEL_DIR`
    (typically ``out/models``). Versioned training uses
    ``out/models/<model_version>/`` (see Priority 1 investigation plan).

    v10 single-entry format
    ----------------------
    models/model.pkl               {"model", "threshold", "features", "model_kind", ...}
    models/feature_list.json       [{name, track}]
    models/reason_code_map.json   {feature_name: reason_code} for scorer SHAP lookup
    models/model_version          <version string>
    models/training_metrics.json  legacy v1 per-model metrics (rated only)
    models/training_metrics.v2.json  v2 metrics (datasets + selection; large blobs split out)
    models/feature_importance.json  winner feature importance (gain list)
    models/comparison_metrics.json  comparison families (e.g. gbm_bakeoff)
    models/feature_spec.yaml      frozen feature spec snapshot (DEC-024, R3501)
    models/model_metadata.json    train/valid/test time bounds + run params (schema v1)
    """
    _out: Path = bundle_dir if bundle_dir is not None else MODEL_DIR
    _out.mkdir(parents=True, exist_ok=True)
    # DEC-024 / R3501: freeze a copy of the feature spec into the artifact bundle so
    # the scorer can load an exact match to training-time spec_hash for reproducibility.
    spec_hash: Optional[str] = None
    feature_spec: Optional[dict] = None
    _fsp = Path(feature_spec_path) if feature_spec_path is not None else FEATURE_SPEC_PATH
    if _fsp.exists():
        from trainer.features.features import (
            build_runtime_feature_spec_subset,
            write_feature_spec_yaml,
        )

        _full = load_feature_spec(_fsp)
        if feature_cols:
            _frozen = build_runtime_feature_spec_subset(_full, feature_cols)
            write_feature_spec_yaml(_out / "feature_spec.yaml", _frozen)
        else:
            import shutil as _shutil

            _shutil.copy2(_fsp, _out / "feature_spec.yaml")
        _written = _out / "feature_spec.yaml"
        spec_hash = hashlib.md5(_written.read_bytes()).hexdigest()[:12]
        feature_spec = load_feature_spec(_written)
    # v10 single-entry format (DEC-021 / ensemble-capable): one model.pkl only
    if rated:
        _pkl_path = _out / "model.pkl"
        _tmp = _pkl_path.with_suffix(".pkl.tmp")
        _pkl_payload: Dict[str, Any] = {
            "model": rated["model"],
            "threshold": rated["threshold"],
            "features": rated["features"],
            "model_kind": rated.get("model_kind", "lightgbm"),
            "reason_codes_enabled": bool(rated.get("reason_codes_enabled", True)),
            "component_backends": list(rated.get("component_backends") or []),
            "a4_enabled": bool(rated.get("a4_enabled", False)),
            "a4_fusion_mode": rated.get("a4_fusion_mode", A4_FUSION_MODE_PRODUCT),
            "a4_candidate_cutoff": rated.get("a4_candidate_cutoff"),
            "stage2_model": rated.get("stage2_model"),
            "stage2_features": list(rated.get("stage2_features") or rated.get("features") or []),
        }
        if rated.get("high_roller_segmentation") is not None:
            _pkl_payload["high_roller_segmentation"] = rated["high_roller_segmentation"]
        joblib.dump(
            _pkl_payload,
            _tmp,
        )
        os.replace(_tmp, _pkl_path)

    _profile_set = set(get_candidate_feature_ids(feature_spec, "track_profile", screening_only=False)) if feature_spec else set(PROFILE_FEATURE_COLS)
    _llm_set = set(get_candidate_feature_ids(feature_spec, "track_llm", screening_only=False)) if feature_spec else set()
    _human_set = set(get_candidate_feature_ids(feature_spec, "track_human", screening_only=False)) if feature_spec else set()

    # ``name`` == training DataFrame column; layered metadata lives on candidates
    # in ``feature_candidates.yaml`` (``target_layer``) — not duplicated in bundle.
    feature_list = [
        {
            "name": c,
            "track": (
                "player_profile_snapshot" if c in _profile_set
                else "run_state_machine" if c in _human_set
                else "bet_duckdb_window"
            ),
        }
        for c in feature_cols
    ]
    (_out / "feature_list.json").write_text(
        json.dumps(feature_list, indent=2), encoding="utf-8"
    )

    # reason_code_map.json: feature name -> short reason code for SHAP output.
    # Phase E (reason-code removal + compat): only emit a populated map when the
    # bundle actually supports SHAP reason codes; otherwise write an empty map so
    # legacy scorer load paths keep working without the PROFILE_/FEAT_ fallback
    # noise (which never reaches downstream when SCORER_ENABLE_SHAP_REASON_CODES
    # is off, see ``trainer.core._config_serving_runtime``).
    _reason_codes_enabled_for_bundle = bool(rated.get("reason_codes_enabled", True)) if rated else False
    reason_code_map: dict[str, str] = {}
    if _reason_codes_enabled_for_bundle and feature_spec is not None:
        for track in ("bet_duckdb_window", "run_state_machine", "player_profile_snapshot"):
            for c in resolve_spec_track_section(feature_spec, track).get("candidates", []):
                fid = c.get("feature_id")
                rcode = c.get("reason_code_category")
                if fid and rcode:
                    reason_code_map[fid] = rcode
        for feat in feature_cols:
            if feat not in reason_code_map:
                if feat in PROFILE_FEATURE_COLS:
                    reason_code_map[feat] = f"PROFILE_{feat[:28].upper()}"
                else:
                    reason_code_map[feat] = f"FEAT_{feat[:30].upper()}"

    (_out / "reason_code_map.json").write_text(
        json.dumps(reason_code_map, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    (_out / "model_version").write_text(model_version, encoding="utf-8")
    # R703: flag when the fallback (uncalibrated) 0.5 threshold was used.
    # R804: read from the _uncalibrated code-path flag set by _train_one_model,
    # not from `threshold == 0.5` — a legitimately-optimised threshold of 0.5
    # must not be falsely flagged as uncalibrated.
    # R2207: _uncalibrated is stored inside rated["metrics"], not at the top level.
    # v10 rated artifact entry: only rated threshold is relevant; nonrated removed (R1606/R1908).
    _uncalibrated_threshold = {
        "rated": rated is not None and bool(
            rated["metrics"].get("_uncalibrated", False)
            if isinstance(rated.get("metrics"), dict)
            else rated.get("_uncalibrated", False)
        ),
    }
    _metrics_root: dict[str, Any] = {
        **combined_metrics,
        "model_version": model_version,
        # R301: record sampling metadata so artifacts can be audited
        # even when loaded later.  None = full rated population was used.
        "sample_rated_n": sample_rated_n,
        # R-NEG-2: record effective neg_sample_frac for auditability.
        # 1.0 = no downsampling; < 1.0 = negatives were downsampled.
        "neg_sample_frac": neg_sample_frac,
        # Production neg/pos ratio assumed for test_precision_prod_adjusted.
        # None = feature disabled (PRODUCTION_NEG_POS_RATIO not set in config).
        "production_neg_pos_ratio": PRODUCTION_NEG_POS_RATIO,
        # W2: operating contract for threshold objective / prod-adjusted semantics.
        "selection_mode": str(SELECTION_MODE or "field_test").strip() or "field_test",
        # R703: uncalibrated_threshold=True means the 0.5 fallback was used.
        "uncalibrated_threshold": _uncalibrated_threshold,
        # DEC-032 / PLAN: artifact threshold is chosen at this recall floor (vs multi-recall backtester keys).
        "threshold_selected_at_recall_floor": THRESHOLD_MIN_RECALL,
        # DEC-024 / R3501: SHA-256 prefix of the frozen feature spec for audit.
        "spec_hash": spec_hash,
    }
    if baseline_training_alignment is not None:
        _metrics_root["baseline_data_alignment"] = baseline_training_alignment
        (_out / "training_provenance.json").write_text(
            json.dumps(baseline_training_alignment, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    (_out / "training_metrics.json").write_text(
        json.dumps(
            _metrics_root,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    write_training_metrics_v2_sidecars(
        _out,
        model_version=model_version,
        metrics_root=_metrics_root,
        model_metadata=model_metadata,
    )
    if model_metadata is not None:
        (_out / "model_metadata.json").write_text(
            json.dumps(model_metadata, indent=2, default=str, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # Contract: precision uplift phase2 orchestrator regex-parses this line
    # (``investigations/precision_uplift_recall_1pct/orchestrator/runner.py``).
    logger.info("Artifacts saved to %s  (version=%s)", _out, model_version)


def _write_pipeline_diagnostics_json(
    *,
    model_version: str,
    pipeline_started_at: str,
    pipeline_finished_at: str,
    total_duration_sec: float,
    step0_duration_sec: Optional[float] = None,
    step1_duration_sec: Optional[float] = None,
    step2_duration_sec: Optional[float] = None,
    step3_duration_sec: Optional[float] = None,
    step4_duration_sec: Optional[float] = None,
    step5_duration_sec: Optional[float] = None,
    step6_duration_sec: Optional[float] = None,
    step7_duration_sec: Optional[float] = None,
    step7b_duration_sec: Optional[float] = None,
    step8_duration_sec: Optional[float] = None,
    step9_duration_sec: Optional[float] = None,
    step10_duration_sec: Optional[float] = None,
    oom_precheck_est_peak_ram_gb: Optional[float] = None,
    oom_precheck_step7_rss_error_ratio: Optional[float] = None,
    step7_rss_start_gb: Optional[float] = None,
    step7_rss_peak_gb: Optional[float] = None,
    step7_rss_end_gb: Optional[float] = None,
    step7_sys_available_min_gb: Optional[float] = None,
    step7_sys_used_percent_peak: Optional[float] = None,
    step7_chunk_parquet_total_bytes: Optional[int] = None,
    step7_chunk_parquet_est_ram_gb: Optional[float] = None,
    step8_screening_source: Optional[str] = None,
    step8_screening_stats_source: Optional[str] = None,
    step8_screening_sample_rows: Optional[int] = None,
    step8_screening_full_train_rows: Optional[int] = None,
    step8_screening_candidate_cols: Optional[int] = None,
    step8_screened_feature_count: Optional[int] = None,
    step8_screen_sample_strategy: Optional[str] = None,
    duckdb_runtime_step7_memory_gb: Optional[float] = None,
    duckdb_runtime_step7_threads: Optional[int] = None,
    duckdb_runtime_screening_memory_gb: Optional[float] = None,
    duckdb_runtime_screening_threads: Optional[int] = None,
    duckdb_runtime_track_llm_memory_gb: Optional[float] = None,
    duckdb_runtime_track_llm_threads: Optional[int] = None,
    chunk_cache_stats: Optional[Dict[str, int]] = None,
    issue16_audit: Optional[Mapping[str, Any]] = None,
    oom_estimate_strategy: Optional[str] = None,
    l2_split_parquet_total_bytes: Optional[int] = None,
    output_dir: Optional[Path] = None,
    feature_materialization_audit: Optional[Mapping[str, Any]] = None,
) -> None:
    """Write resource/timing diagnostics to ``output_dir/pipeline_diagnostics.json`` (omit None keys).

    *output_dir* defaults to :data:`MODEL_DIR` when omitted.

    See doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md — RSS/OOM fields align with
    run_pipeline sampling and oom_precheck estimate, not OOM helper return values.

    ``chunk_cache_stats``: optional Step 6 cache counters from :func:`process_chunk`
    (keys ``step6_chunk_cache_*``) for Task 7 DoD / hit-ratio analysis.

    ``issue16_audit``: optional GitHub #16 gate bundle (split / label / metric semantics).

    ``oom_estimate_strategy``: when using L2 split-bytes OOM model (TRN-16-04), set to
    ``l2_train_valid_test_parquet_bytes`` (see ``l2_trainer_contracts``).

    ``l2_split_parquet_total_bytes``: total on-disk bytes for train/valid/test split parquets.

    ``feature_materialization_audit``: optional unified feature lineage / spec-first audit blob.
    """
    payload: dict[str, Any] = {
        "model_version": model_version,
        "pipeline_started_at": pipeline_started_at,
        "pipeline_finished_at": pipeline_finished_at,
        "total_duration_sec": total_duration_sec,
        "step0_duration_sec": step0_duration_sec,
        "step1_duration_sec": step1_duration_sec,
        "step2_duration_sec": step2_duration_sec,
        "step3_duration_sec": step3_duration_sec,
        "step4_duration_sec": step4_duration_sec,
        "step5_duration_sec": step5_duration_sec,
        "step6_duration_sec": step6_duration_sec,
        "step7_duration_sec": step7_duration_sec,
        "step7b_duration_sec": step7b_duration_sec,
        "step8_duration_sec": step8_duration_sec,
        "step9_duration_sec": step9_duration_sec,
        "step10_duration_sec": step10_duration_sec,
        "oom_precheck_est_peak_ram_gb": oom_precheck_est_peak_ram_gb,
        "oom_precheck_step7_rss_error_ratio": oom_precheck_step7_rss_error_ratio,
        "step7_rss_start_gb": step7_rss_start_gb,
        "step7_rss_peak_gb": step7_rss_peak_gb,
        "step7_rss_end_gb": step7_rss_end_gb,
        "step7_sys_available_min_gb": step7_sys_available_min_gb,
        "step7_sys_used_percent_peak": step7_sys_used_percent_peak,
        "step7_chunk_parquet_total_bytes": step7_chunk_parquet_total_bytes,
        "step7_chunk_parquet_est_ram_gb": step7_chunk_parquet_est_ram_gb,
        "step8_screening_source": step8_screening_source,
        "step8_screening_stats_source": step8_screening_stats_source,
        "step8_screening_sample_rows": step8_screening_sample_rows,
        "step8_screening_full_train_rows": step8_screening_full_train_rows,
        "step8_screening_candidate_cols": step8_screening_candidate_cols,
        "step8_screened_feature_count": step8_screened_feature_count,
        "step8_screen_sample_strategy": step8_screen_sample_strategy,
        "duckdb_runtime_step7_memory_gb": duckdb_runtime_step7_memory_gb,
        "duckdb_runtime_step7_threads": duckdb_runtime_step7_threads,
        "duckdb_runtime_screening_memory_gb": duckdb_runtime_screening_memory_gb,
        "duckdb_runtime_screening_threads": duckdb_runtime_screening_threads,
        "duckdb_runtime_track_llm_memory_gb": duckdb_runtime_track_llm_memory_gb,
        "duckdb_runtime_track_llm_threads": duckdb_runtime_track_llm_threads,
        # Canonical naming (dual-emit; legacy keys above retained for dashboards).
        "duckdb_runtime_bet_duckdb_window_memory_gb": duckdb_runtime_track_llm_memory_gb,
        "duckdb_runtime_bet_duckdb_window_threads": duckdb_runtime_track_llm_threads,
    }
    out = {k: v for k, v in payload.items() if v is not None}
    if chunk_cache_stats:
        for _ck, _cv in chunk_cache_stats.items():
            if _cv is not None:
                out[_ck] = _cv
    if issue16_audit is not None:
        out["issue16_audit"] = dict(issue16_audit)
    if oom_estimate_strategy is not None:
        out["oom_estimate_strategy"] = oom_estimate_strategy
    if l2_split_parquet_total_bytes is not None:
        out["l2_split_parquet_total_bytes"] = int(l2_split_parquet_total_bytes)
    if feature_materialization_audit is not None:
        out["feature_materialization_audit"] = dict(feature_materialization_audit)
    _dir = output_dir if output_dir is not None else MODEL_DIR
    (_dir / "pipeline_diagnostics.json").write_text(
        json.dumps(out, indent=2, default=str),
        encoding="utf-8",
    )


def _sha256_file_hex(path: Path, chunk_bytes: int = 1 << 20) -> str:
    """Return lowercase hex SHA-256 of the file at *path* (streaming, bounded chunk size)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _make_baseline_training_alignment_payload(
    effective_start: Any,
    effective_end: Any,
    train_split_frac: float,
    valid_split_frac_of_total: float,
) -> dict[str, Any]:
    """供 ``baseline_data_alignment``／``training_provenance.json``：與 baseline 契約對齊。"""

    def _iso(x: Any) -> Any:
        if x is None:
            return None
        if hasattr(x, "isoformat"):
            return x.isoformat()
        return str(x)

    den = 1.0 - float(train_split_frac)
    baseline_valid = float(valid_split_frac_of_total) / den if den > 1e-15 else 0.5
    return {
        "data_window": {
            "start": _iso(effective_start),
            "end": _iso(effective_end),
        },
        "split": {
            "train_frac": float(train_split_frac),
            "valid_frac": float(baseline_valid),
        },
        "_trainer_split_row_fractions": {
            "TRAIN_SPLIT_FRAC": float(train_split_frac),
            "VALID_SPLIT_FRAC": float(valid_split_frac_of_total),
            "note": "Baseline valid_frac = VALID_SPLIT_FRAC / (1 - TRAIN_SPLIT_FRAC).",
        },
    }
