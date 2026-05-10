"""End-to-end training from L2 pre-assembled split parquets (GitHub #16 TRN-16-03).

Invoked from ``run_pipeline`` when ``--l2-training-bundle DIR`` is set.  Lazy-imports
``trainer.training.trainer`` at call time to avoid import cycles.

**RAM**: row counts, Issue #16 gates, and Step 8 screening use DuckDB / bounded
Parquet head reads on the monolithic train split; LibSVM export streams from
``manifest.*_export_paths`` (day shards when schema v2) without concatenating
splits into a single in-memory frame.
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _t_game_enabled() -> bool:
    """Read T_GAME_FEATURES_ENABLED from trainer config module (no trainer import cycles)."""
    try:
        import config as cfg_mod  # type: ignore[import]
    except ModuleNotFoundError:
        import trainer.config as cfg_mod  # type: ignore[import]
    return bool(getattr(cfg_mod, "T_GAME_FEATURES_ENABLED", False))


def execute_l2_training_bundle(
    *,
    args: Any,
    bundle_dir: Path,
    pipeline_model_version: str,
    pipeline_started_at_iso: str,
    pipeline_start: float,
    use_local: bool,
    skip_optuna: bool,
    sample_rated_n: Optional[int],
    pipeline_ranking_recipe: Any,
    pipeline_gbm_bakeoff: bool,
    l2_reuse_audit: Optional[Dict[str, Any]] = None,
) -> None:
    """Run Steps 8–10/11 from an L2 bundle (skip chunk Steps 1–6/11 and Step 7 merge)."""
    from trainer.training.pipeline_step_context import (
        ensure_pipeline_step_log_filter_installed,
        pipeline_step_set,
    )

    ensure_pipeline_step_log_filter_installed()
    pipeline_step_set("Step 8/11")

    import trainer.training.trainer as tr

    from trainer.core import config as _core_cfg

    from trainer.training.issue16_gates import (
        evaluate_issue16_gate_bundle,
        raise_if_strict_issue16_gates_failed,
    )
    from trainer.training.l2_day_shard import canonical_l2_bundle_read_parquet_expr
    from trainer.training.l2_trainer_contracts import (
        KEY_L2_SNAPSHOT_ID,
        KEY_TEST_FULL_UNSAMPLED,
        KEY_TRAIN_SAMPLING_APPLIED,
        KEY_VALID_FULL_UNSAMPLED,
        OOM_ESTIMATE_STRATEGY_L2_SPLIT_FILES,
        TRAIN_END_SOURCE_L2_MANIFEST,
    )
    from trainer.training.l2_training_manifest import (
        L2_TRAINING_BUNDLE_MANIFEST_FILE,
        estimate_step7_peak_ram_gb_from_split_bytes,
        load_and_validate_bundle,
        split_parquet_total_bytes,
    )
    from trainer.training.training_objective import primary_rated_gbm_bakeoff_enabled

    if sample_rated_n is not None:
        logger.warning(
            "--sample-rated is ignored for --l2-training-bundle (pre-assembled splits)."
        )
    t_load = time.perf_counter()
    manifest = load_and_validate_bundle(bundle_dir)
    import duckdb

    train_expr = canonical_l2_bundle_read_parquet_expr(manifest.train_export_paths)
    valid_expr = canonical_l2_bundle_read_parquet_expr(manifest.valid_export_paths)
    test_expr = canonical_l2_bundle_read_parquet_expr(manifest.test_export_paths)

    def _l2_scalar(sql: str) -> Any:
        con = duckdb.connect(":memory:")
        try:
            row = con.execute(sql).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    n_train = int(_l2_scalar(f"SELECT count(*) FROM {train_expr}") or 0)
    n_valid = int(_l2_scalar(f"SELECT count(*) FROM {valid_expr}") or 0)
    n_test = int(_l2_scalar(f"SELECT count(*) FROM {test_expr}") or 0)
    step7_duration_sec = time.perf_counter() - t_load
    tr.pipeline_echo(
        f"Step 8/11 — L2 bundle — manifest OK; probed splits from {bundle_dir} in "
        f"{step7_duration_sec:.1f}s "
        f"(train={n_train} valid={n_valid} test={n_test} rows; schema={manifest.schema_version})"
    )

    _probe_con = duckdb.connect(":memory:")
    try:
        _desc = _probe_con.execute(f"SELECT * FROM {train_expr} LIMIT 0").description
        _l2_train_cols: List[str] = [d[0] for d in (_desc or [])]
    finally:
        _probe_con.close()

    if "label" not in _l2_train_cols:
        raise SystemExit("L2 bundle train parquet missing required column 'label'")

    def _read_l2_screen_sample(from_sql: str, n: int, strategy: str) -> pd.DataFrame:
        """Read a bounded Step 8 sample via canonicalized DuckDB bundle expression."""
        if n <= 0:
            return pd.DataFrame()

        def _query_df(sql: str) -> pd.DataFrame:
            con = duckdb.connect(":memory:")
            try:
                return con.execute(sql).df()
            finally:
                con.close()

        def _head(limit: int) -> pd.DataFrame:
            return _query_df(f"SELECT * FROM {from_sql} LIMIT {limit}")

        def _tail(limit: int) -> pd.DataFrame:
            return _query_df(
                f"SELECT * FROM {from_sql} "
                "ORDER BY payout_complete_dtm DESC NULLS LAST, bet_id DESC NULLS LAST "
                f"LIMIT {limit}"
            )

        if strategy == "tail":
            out = _tail(int(n))
        elif strategy == "head_tail":
            nh = max(1, int(n) // 2)
            nt = max(1, int(n) - nh)
            head_df = _head(nh)
            tail_df = _tail(nt)
            if head_df.empty:
                out = tail_df
            elif tail_df.empty:
                out = head_df
            else:
                out = pd.concat([head_df, tail_df], ignore_index=True)
                if "canonical_id" in out.columns and "bet_id" in out.columns:
                    out = out.drop_duplicates(subset=["canonical_id", "bet_id"], keep="first")
                else:
                    out = out.drop_duplicates()
                if len(out) > int(n):
                    out = out.iloc[: int(n)].copy()
        else:
            out = _head(int(n))
        out.columns = [str(col).strip().lower() for col in out.columns]
        return out

    effective_start = pd.Timestamp(manifest.window_start).to_pydatetime()
    effective_end = pd.Timestamp(manifest.window_end).to_pydatetime()
    train_end = pd.Timestamp(manifest.train_end)
    if train_end.tzinfo is not None:
        train_end = train_end.tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    else:
        train_end = train_end.to_pydatetime()

    if "payout_complete_dtm" in _l2_train_cols and n_train > 0:
        _row_max = _l2_scalar(f"SELECT max(payout_complete_dtm) FROM {train_expr}")
        _actual_train_end = pd.Timestamp(_row_max).to_pydatetime() if _row_max is not None else train_end
    else:
        _actual_train_end = train_end

    split_flags = {
        KEY_VALID_FULL_UNSAMPLED: manifest.valid_full_unsampled,
        KEY_TEST_FULL_UNSAMPLED: manifest.test_full_unsampled,
        KEY_TRAIN_SAMPLING_APPLIED: manifest.train_sampling_applied,
        KEY_L2_SNAPSHOT_ID: manifest.l2_snapshot_id,
    }

    _l2_feature_spec: Optional[dict] = None
    try:
        if tr.FEATURE_SPEC_PATH.is_file():
            _l2_feature_spec = tr.load_feature_spec(tr.FEATURE_SPEC_PATH)
    except Exception as _l2_spec_exc:
        logger.warning("L2 bundle: could not load feature spec for spec-first gate: %s", _l2_spec_exc)

    feature_materialization_audit: Optional[Dict[str, Any]] = None
    try:
        from trainer.training import feature_materialization as _fm_l2

        _fm_l2.maybe_raise_spec_first_columns(_l2_train_cols, _l2_feature_spec)
        feature_materialization_audit = _fm_l2.build_pipeline_feature_materialization_audit(
            feature_spec=_l2_feature_spec,
            train_columns=_l2_train_cols,
            prev_per_feature_fp=manifest.per_feature_fingerprints,
            curr_source_snapshot_id=manifest.source_snapshot_id,
            pit_policy_id=str(manifest.identity_mapping_mode),
        )
        _fm_l2.raise_if_strict_materialization_gates_failed(
            feature_materialization_audit["materialization_gates"],
        )
    except RuntimeError:
        raise
    except Exception as _l2fma_exc:
        logger.warning("L2 bundle: feature_materialization audit failed: %s", _l2fma_exc)

    if l2_reuse_audit:
        if isinstance(feature_materialization_audit, dict):
            feature_materialization_audit = {**feature_materialization_audit, "l2_reuse_cache": dict(l2_reuse_audit)}
        else:
            feature_materialization_audit = {"l2_reuse_cache": dict(l2_reuse_audit)}

    issue16_gate_report = evaluate_issue16_gate_bundle(
        effective_neg_sample_frac=1.0,
        chunk_train_end_naive=train_end,
        row_level_train_end_max=_actual_train_end,
        train_end_source=TRAIN_END_SOURCE_L2_MANIFEST,
        l2_snapshot_id=manifest.l2_snapshot_id,
        label_asset_meta=manifest.label_asset_meta,
        training_source_snapshot_id=manifest.source_snapshot_id,
        split_flags=split_flags,
        train_column_names=_l2_train_cols,
        feature_spec=_l2_feature_spec,
        train_neg_sampling_mode="post_step7",
    )
    raise_if_strict_issue16_gates_failed(issue16_gate_report)
    issue16_gate_report = {
        **issue16_gate_report,
        "l2_bundle_manifest_file": L2_TRAINING_BUNDLE_MANIFEST_FILE,
        "l2_bundle_dir": str(manifest.bundle_dir),
    }

    _split_row_meta = tr.split_row_metadata_from_parquet_path_sequences(
        manifest.train_export_paths,
        manifest.valid_export_paths,
        manifest.test_export_paths,
    )
    _model_used_split_meta = tr.split_row_metadata_from_parquet_path_sequences(
        manifest.train_export_paths,
        manifest.valid_export_paths,
        manifest.test_export_paths,
        rated_only=True,
    )
    n_rows = n_train + n_valid + n_test
    _lt = int(_l2_scalar(f"SELECT coalesce(sum(cast(label AS INTEGER)), 0) FROM {train_expr}") or 0)
    _lv = int(_l2_scalar(f"SELECT coalesce(sum(cast(label AS INTEGER)), 0) FROM {valid_expr}") or 0)
    _ltest = int(_l2_scalar(f"SELECT coalesce(sum(cast(label AS INTEGER)), 0) FROM {test_expr}") or 0)
    _label1 = _lt + _lv + _ltest

    split_total_bytes = split_parquet_total_bytes(manifest)
    oom_precheck_est_peak_ram_gb = estimate_step7_peak_ram_gb_from_split_bytes(
        split_total_bytes,
        train_split_frac=float(tr.TRAIN_SPLIT_FRAC),
        use_duckdb=bool(tr.STEP7_USE_DUCKDB),
        chunk_concat_ram_factor=float(tr.CHUNK_CONCAT_RAM_FACTOR),
    )

    feature_spec = tr.load_feature_spec(tr.FEATURE_SPEC_PATH)
    try:
        feature_spec_hash = hashlib.md5(Path(tr.FEATURE_SPEC_PATH).read_bytes()).hexdigest()[:12]
    except Exception:
        feature_spec_hash = "unknown"

    step8_screen_sample_strategy = tr._step8_resolve_sample_strategy(tr.STEP8_SCREEN_SAMPLE_STRATEGY)
    _train_cols = pd.Index(_l2_train_cols)
    active_feature_cols = tr.get_all_candidate_feature_ids(feature_spec, screening_only=True)
    if feature_spec is not None:
        _bet_method_cols = [
            cand.get("feature_id")
            for cand in (feature_spec.get("bet_duckdb_window", {}) or {}).get("candidates", [])
            if cand.get("feature_id") in _train_cols
        ]
        _all_candidate_cols = list(dict.fromkeys(active_feature_cols + _bet_method_cols))
    else:
        _all_candidate_cols = active_feature_cols
    _present_candidate_cols = [c for c in _all_candidate_cols if c in _train_cols]
    step8_screening_source = None
    step8_screening_stats_source = None
    step8_screening_sample_rows = None
    step8_screening_full_train_rows = None
    step8_screening_candidate_cols = None
    step8_screened_feature_count = None
    step8_duration_sec: Optional[float] = None

    if not _present_candidate_cols:
        logger.warning("L2 bundle: no screening candidates — using columns present in train only")
        active_feature_cols = [c for c in active_feature_cols if c in _train_cols]
    else:
        _cap = (
            int(tr.STEP8_SCREEN_SAMPLE_ROWS)
            if (tr.STEP8_SCREEN_SAMPLE_ROWS is not None and tr.STEP8_SCREEN_SAMPLE_ROWS >= 1)
            else 2_000_000
        )
        _sample_n_disk = (
            int(tr.STEP8_SCREEN_SAMPLE_ROWS)
            if (tr.STEP8_SCREEN_SAMPLE_ROWS is not None and tr.STEP8_SCREEN_SAMPLE_ROWS >= 1)
            else _cap
        )
        _matrix_for_screen = _read_l2_screen_sample(
            train_expr,
            _sample_n_disk,
            step8_screen_sample_strategy,
        )
        if "is_rated" not in _matrix_for_screen.columns:
            logger.warning(
                "L2 bundle train sample: missing is_rated — defaulting all rows to True for screening"
            )
            _matrix_for_screen = _matrix_for_screen.copy()
            _matrix_for_screen["is_rated"] = True
        step8_screening_source = f"parquet_{step8_screen_sample_strategy}"
        step8_screening_stats_source = "screening_sample_parquet"
        step8_screening_sample_rows = len(_matrix_for_screen)
        step8_screening_full_train_rows = n_train
        step8_screening_candidate_cols = len(_present_candidate_cols)
        tr.pipeline_echo("Step 8/11 — Feature screening …")
        t0 = time.perf_counter()
        screened_cols = tr.screen_features(
            feature_matrix=_matrix_for_screen,
            labels=_matrix_for_screen["label"],
            feature_names=_present_candidate_cols,
            screen_method=tr.SCREEN_FEATURES_METHOD,
            train_path=manifest.train_path,
            train_df=None,
        )
        step8_duration_sec = time.perf_counter() - t0
        step8_screened_feature_count = len(screened_cols)
        active_feature_cols = screened_cols

    if not active_feature_cols:
        raise SystemExit("L2 bundle: no features remain after screening — cannot train")

    if step8_duration_sec is not None and step8_screening_candidate_cols is not None:
        tr.pipeline_echo(
            f"Step 8/11 — done in {step8_duration_sec:.1f}s "
            f"({step8_screening_candidate_cols} → {step8_screened_feature_count} features)"
        )
    else:
        tr.pipeline_echo("Step 8/11 — Feature screening skipped or not applicable")

    pipeline_step_set("Step 9/11")
    tr.pipeline_echo("Step 9/11 — Train rated GBM (L2 bundle path) …")
    t0 = time.perf_counter()
    model_version = pipeline_model_version
    if not tr.STEP9_EXPORT_LIBSVM:
        raise RuntimeError(
            "STEP9_EXPORT_LIBSVM=False is incompatible with LibSVM-only training."
        )
    _export_dir = tr.DATA_DIR / "export"
    tr.remove_legacy_plan_b_csv_exports(_export_dir)
    _primary_bakeoff = primary_rated_gbm_bakeoff_enabled(pipeline_gbm_bakeoff)
    _hr_l2 = tr.train_issue8_high_roller_segmented_bundle(
        step7_train_path=manifest.train_path,
        step7_valid_path=manifest.valid_path,
        step7_test_path=manifest.test_path,
        active_feature_cols=active_feature_cols,
        export_base=_export_dir,
        run_optuna=not skip_optuna,
        ranking_recipe=pipeline_ranking_recipe,
        gbm_bakeoff=_primary_bakeoff,
    )
    if _hr_l2 is not None:
        rated_art, combined_metrics = _hr_l2
        logger.info(
            "L2 bundle investigate: Issue #8 segmented training complete "
            "(gbm_bakeoff=%s skip_optuna=%s)",
            pipeline_gbm_bakeoff,
            skip_optuna,
        )
    else:
        if bool(getattr(_core_cfg, "HIGH_ROLLER_SEGMENT_ENABLE", False)):
            raise RuntimeError(
                "L2 bundle: HIGH_ROLLER_SEGMENT_ENABLE is True but "
                "train_issue8_high_roller_segmented_bundle returned None "
                "(expected a raised error from Issue #8 instead)."
            )
        _l2_train_libsvm, _l2_valid_libsvm, _l2_test_libsvm = tr._export_parquet_to_libsvm(
            manifest.train_export_paths,
            manifest.valid_export_paths,
            active_feature_cols,
            _export_dir,
            test_path=manifest.test_export_paths,
        )
        logger.info(
            "L2 bundle investigate: entering train_single_rated_model "
            "(gbm_bakeoff=%s skip_optuna=%s train_rows=%d)",
            _primary_bakeoff,
            skip_optuna,
            n_train,
        )
        _empty = pd.DataFrame()
        rated_art, _, combined_metrics = tr.train_single_rated_model(
            _empty,
            _empty,
            active_feature_cols,
            run_optuna=not skip_optuna,
            test_df=_empty,
            train_libsvm_paths=(_l2_train_libsvm, _l2_valid_libsvm),
            test_libsvm_path=_l2_test_libsvm,
            ranking_recipe=pipeline_ranking_recipe,
            gbm_bakeoff=_primary_bakeoff,
            valid_split_parquet_path=manifest.valid_path,
            test_split_parquet_path=manifest.test_path,
            train_split_parquet_path=manifest.train_path,
        )
    _cm_keys = (
        list(combined_metrics.keys())[:8]
        if isinstance(combined_metrics, dict) and combined_metrics
        else []
    )
    logger.info(
        "L2 bundle investigate: train_single_rated_model returned "
        "(rated_art is None=%s combined_metrics_key_sample=%s)",
        rated_art is None,
        _cm_keys,
    )
    step9_duration_sec = time.perf_counter() - t0
    tr.pipeline_echo(f"Step 9/11 — done in {step9_duration_sec:.1f}s")
    gc.collect()

    pipeline_step_set("Step 10/11")
    tr.pipeline_echo("Step 10/11 — Save artifact bundle (L2 bundle path) …")
    t0 = time.perf_counter()
    _versions_root = tr.MODEL_DIR
    _bundle_dir = tr.safe_version_subdirectory(_versions_root, model_version)
    if _bundle_dir.exists() and (_bundle_dir / "model.pkl").exists():
        raise FileExistsError(
            f"Refusing to overwrite existing model bundle: {_bundle_dir}. "
            "Remove the directory or wait for a new model_version timestamp."
        )
    _bundle_dir.mkdir(parents=True, exist_ok=True)
    _baseline_align = tr._make_baseline_training_alignment_payload(
        effective_start,
        effective_end,
        float(tr.TRAIN_SPLIT_FRAC),
        float(tr.VALID_SPLIT_FRAC),
    )
    _split_mlflow_meta = tr.split_row_metadata_to_mlflow_string_params(_split_row_meta)
    _model_meta_doc = tr.build_model_metadata_document(
        model_version=model_version,
        effective_start=effective_start,
        effective_end=effective_end,
        splits=_split_row_meta,
        use_local_parquet=use_local,
        recent_chunks=None,
        sample_rated_n=None,
        skip_optuna=skip_optuna,
        neg_sample_frac_effective=1.0,
        bundle_dir=_bundle_dir,
        combined_metrics=combined_metrics,
        model_used_splits=_model_used_split_meta,
        identity_mapping_mode=manifest.identity_mapping_mode,
        t_game_features_enabled=_t_game_enabled(),
        t_game_visible_time_column=(
            "__etl_insert_Dtm" if _t_game_enabled() else "none"
        ),
        l2_snapshot_id=manifest.l2_snapshot_id,
        source_snapshot_id=manifest.source_snapshot_id,
        l2_training_bundle_dir=str(bundle_dir.resolve()),
    )
    tr.save_artifact_bundle(
        rated_art,
        active_feature_cols,
        combined_metrics,
        model_version,
        sample_rated_n=None,
        feature_spec_path=tr.FEATURE_SPEC_PATH,
        neg_sample_frac=1.0,
        bundle_dir=_bundle_dir,
        baseline_training_alignment=_baseline_align,
        model_metadata=_model_meta_doc,
    )
    try:
        from trainer.core.mlflow_utils import has_active_run, log_artifact_safe, log_params_safe
        from trainer.core.training_artifact_bundle import MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH

        from trainer.core.training_metrics_unified import SCHEMA_TRAINING_METRICS_UNIFIED

        _tm_main = _bundle_dir / "training_metrics.json"
        if not _tm_main.is_file():
            logger.warning(
                "training_metrics.json missing after save_artifact_bundle (expected at %s); "
                "MLflow metrics artifact upload skipped.",
                _tm_main,
            )
        elif has_active_run():
            log_artifact_safe(
                _tm_main,
                artifact_path=MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH,
            )
            log_params_safe(
                {
                    "training_metrics_rel_path": f"{_bundle_dir.name}/training_metrics.json",
                }
            )
            try:
                from trainer.core.mlflow_utils import log_metrics_safe

                _tm_obj = json.loads(_tm_main.read_text(encoding="utf-8"))
                _v3_obj: dict[str, Any] | None = None
                if (
                    isinstance(_tm_obj, dict)
                    and str(_tm_obj.get("schema_version") or "") == SCHEMA_TRAINING_METRICS_UNIFIED
                ):
                    _emb = _tm_obj.get("contract_v3")
                    if isinstance(_emb, dict):
                        _v3_obj = _emb
                if _v3_obj is None:
                    _legacy_v3 = _bundle_dir / "training_metrics.v3.json"
                    if _legacy_v3.is_file():
                        _v3_obj = json.loads(_legacy_v3.read_text(encoding="utf-8"))
                if isinstance(_v3_obj, dict):
                    _ds = _v3_obj.get("datasets")
                    if isinstance(_ds, dict):
                        _ml_m: dict[str, Any] = {}
                        for _split in ("val", "test"):
                            _blob = _ds.get(_split)
                            if not isinstance(_blob, dict):
                                continue
                            for _k in ("ap", "precision", "recall", "f1"):
                                if _k in _blob and _blob[_k] is not None:
                                    _ml_m[f"model/{_split}_{_k}"] = _blob[_k]
                        if _ml_m:
                            log_metrics_safe(_ml_m)
            except Exception as _mm_exc:
                logger.warning("MLflow raw model metrics (val/test) log skipped: %s", _mm_exc)
    except Exception as _art_exc:
        logger.warning("MLflow training_metrics.json artifact upload skipped: %s", _art_exc)
    try:
        from trainer.core.mlflow_utils import has_active_run, log_params_safe

        if has_active_run():
            _lineage_params = {
                "l2_snapshot_id": manifest.l2_snapshot_id,
                "source_snapshot_id": manifest.source_snapshot_id,
                "l2_training_bundle_dir": str(bundle_dir.resolve()),
                "l2_oom_estimate_strategy": OOM_ESTIMATE_STRATEGY_L2_SPLIT_FILES,
            }
            log_params_safe({k: v for k, v in _lineage_params.items() if v})
    except Exception as _ml_exc:
        logger.warning("MLflow L2 lineage params skipped: %s", _ml_exc)
    try:
        tr.write_latest_model_manifest(_versions_root, model_version, _bundle_dir)
    except Exception as _man_exc:
        logger.warning("Failed to write latest model manifest (artifacts saved): %s", _man_exc)
    step10_duration_sec = time.perf_counter() - t0
    tr.pipeline_echo(f"Step 10/11 — done in {step10_duration_sec:.1f}s")

    total_sec = time.perf_counter() - pipeline_start
    _pipeline_finished_at_iso = datetime.now(timezone.utc).isoformat()

    try:
        tr._write_pipeline_diagnostics_json(
            model_version=model_version,
            pipeline_started_at=pipeline_started_at_iso,
            pipeline_finished_at=_pipeline_finished_at_iso,
            total_duration_sec=total_sec,
            step0_duration_sec=None,
            step1_duration_sec=None,
            step2_duration_sec=None,
            step3_duration_sec=None,
            step4_duration_sec=None,
            step5_duration_sec=None,
            step6_duration_sec=None,
            step7_duration_sec=step7_duration_sec,
            step7b_duration_sec=None,
            step8_duration_sec=step8_duration_sec,
            step9_duration_sec=step9_duration_sec,
            step10_duration_sec=step10_duration_sec,
            oom_precheck_est_peak_ram_gb=oom_precheck_est_peak_ram_gb,
            oom_precheck_step7_rss_error_ratio=None,
            step7_chunk_parquet_total_bytes=split_total_bytes,
            step7_chunk_parquet_est_ram_gb=oom_precheck_est_peak_ram_gb,
            chunk_cache_stats={},
            issue16_audit=issue16_gate_report,
            output_dir=_bundle_dir,
            oom_estimate_strategy=OOM_ESTIMATE_STRATEGY_L2_SPLIT_FILES,
            l2_split_parquet_total_bytes=split_total_bytes,
            feature_materialization_audit=feature_materialization_audit,
        )
    except Exception as _diag_exc:
        logger.warning("pipeline_diagnostics.json write failed (training still succeeded): %s", _diag_exc)

    summary = {
        "model_version": model_version,
        "l2_training_bundle": str(bundle_dir),
        "source_snapshot_id": manifest.source_snapshot_id,
        "total_rows": n_rows,
        "metrics": combined_metrics,
    }
    logger.debug("L2 training summary JSON: %s", json.dumps(summary, default=str))
    tr.pipeline_echo(
        f"Complete — 11 steps (0–10, L2 bundle path) finished in {total_sec:.1f}s "
        f"(model_version={model_version} rows={n_rows}; "
        "full JSON: logger DEBUG or TRAINER_SUMMARY_JSON_STDOUT=1)"
    )
    if os.environ.get("TRAINER_SUMMARY_JSON_STDOUT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        print(json.dumps(summary, indent=2, default=str), flush=True)
