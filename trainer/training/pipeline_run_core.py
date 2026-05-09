"""Training pipeline core implementation (Issue #33 Phase B).

Loaded after ``trainer.training.trainer`` defines helpers; uses star-import
for the historical single-namespace scope of ``_run_pipeline_core``.
"""
from __future__ import annotations

from trainer.training.trainer import *  # noqa: F401,F403

# ``from trainer.training.trainer import *`` omits names starting with ``_`` (Python default).
# This module calls many ``trainer`` helpers (``_detect_local_data_end``, ``_cfg``, …); mirror
# them onto this module so ``run_pipeline_core`` matches the historical monolithic scope.
import trainer.training.trainer as _trainer_private_src

for _priv in dir(_trainer_private_src):
    if _priv.startswith("__"):
        continue
    if _priv.startswith("_"):
        globals()[_priv] = getattr(_trainer_private_src, _priv)

import trainer.training.l2_bundle_materialize as l2_bundle_materialize  # noqa: E402
import trainer.training.pipeline_l2_bundle as pipeline_l2_bundle  # noqa: E402


def run_pipeline_core(args) -> None:
    """Phase-1 training pipeline implementation (see ``run_pipeline`` wrapper)."""
    pipeline_step_set("Step 0/11")
    step0_duration_sec: Optional[float] = None
    pipeline_start = time.perf_counter()
    pipeline_started_at_iso = datetime.now(timezone.utc).isoformat()
    start, end = parse_window(args)
    use_local = getattr(args, "use_local_parquet", False)
    force = getattr(args, "force_recompute", False)
    skip_optuna = getattr(args, "skip_optuna", False)
    pipeline_ranking_recipe = resolve_ranking_recipe(getattr(args, "ranking_recipe", None))
    logger.info("Precision uplift A2 ranking_recipe=%s", pipeline_ranking_recipe)
    pipeline_gbm_bakeoff = bool(getattr(_cfg, "STEP9_COMPARE_ALL_GBMS", True)) and not bool(
        getattr(args, "no_gbm_bakeoff", False)
    )
    _cli_cat = getattr(args, "gbm_bakeoff_catboost", None)
    if _cli_cat is not None:
        os.environ["GBM_BAKEOFF_ENABLE_CATBOOST"] = "1" if _cli_cat else "0"
    _cli_xgb = getattr(args, "gbm_bakeoff_xgboost", None)
    if _cli_xgb is not None:
        os.environ["GBM_BAKEOFF_ENABLE_XGBOOST"] = "1" if _cli_xgb else "0"
    _cli_bff = getattr(args, "gbm_bakeoff_from_file", None)
    if _cli_bff is not None:
        setattr(_cfg, "GBM_BAKEOFF_FROM_FILE", bool(_cli_bff))
    _cli_xem = getattr(args, "gbm_bakeoff_xgboost_external_memory", None)
    if _cli_xem is not None:
        setattr(_cfg, "GBM_BAKEOFF_XGBOOST_EXTERNAL_MEMORY", bool(_cli_xem))
    _cli_cbq = getattr(args, "gbm_bakeoff_catboost_quantize", None)
    if _cli_cbq is not None:
        setattr(_cfg, "GBM_BAKEOFF_CATBOOST_QUANTIZE", bool(_cli_cbq))
    _cli_pstr = getattr(args, "gbm_bakeoff_predict_streaming", None)
    if _cli_pstr is not None:
        setattr(_cfg, "GBM_BAKEOFF_PREDICT_STREAMING", bool(_cli_pstr))
        os.environ["GBM_BAKEOFF_PREDICT_STREAMING"] = "1" if _cli_pstr else "0"
    _cli_smm = getattr(args, "gbm_bakeoff_score_memmap", None)
    if _cli_smm is not None:
        setattr(_cfg, "GBM_BAKEOFF_SCORE_MEMMAP", bool(_cli_smm))
        os.environ["GBM_BAKEOFF_SCORE_MEMMAP"] = "1" if _cli_smm else "0"
    _cli_pbr = getattr(args, "gbm_bakeoff_predict_batch_rows", None)
    if _cli_pbr is not None:
        setattr(_cfg, "GBM_BAKEOFF_PREDICT_BATCH_ROWS", int(_cli_pbr))
        os.environ["GBM_BAKEOFF_PREDICT_BATCH_ROWS"] = str(int(_cli_pbr))
    _cli_apm = getattr(args, "gbm_bakeoff_ap_mode", None)
    if _cli_apm is not None:
        setattr(_cfg, "GBM_BAKEOFF_AP_MODE", str(_cli_apm).strip().lower())
        os.environ["GBM_BAKEOFF_AP_MODE"] = str(_cli_apm).strip().lower()
    if bool(getattr(args, "disable_oof_stacking", False)):
        setattr(_cfg, "OOF_STACKING_ENABLED", False)
    logger.info("Precision uplift A3 gbm_bakeoff_enabled=%s", pipeline_gbm_bakeoff)
    configure_lightgbm_device_for_run(args)
    # --no-preload: disable session full-table preload; use per-day PyArrow
    # pushdown reads instead.  Reduces peak RAM for low-RAM machines.
    no_preload = getattr(args, "no_preload", False)
    # --sample-rated N: restrict training to a deterministic subset of rated patrons.
    # None means "use all rated canonical_ids" (default).
    sample_rated_n: Optional[int] = getattr(args, "sample_rated", None)
    # R302: reject invalid sampling sizes early with an actionable error.
    if sample_rated_n is not None and sample_rated_n < 1:
        raise SystemExit(
            f"--sample-rated N must be >= 1, got {sample_rated_n}. "
            "Pass a positive integer or omit the flag to use all rated patrons."
        )

    # Log the config-file NEG_SAMPLE_FRAC at startup.  The OOM pre-check (run
    # after Step 1) may further lower this to _effective_neg_sample_frac.
    if NEG_SAMPLE_FRAC < 1.0:
        logger.info(
            "NEG_SAMPLE_FRAC=%.2f (config): negatives downsampled per window materialize (OOM mitigation)",
            NEG_SAMPLE_FRAC,
        )
    else:
        logger.info("NEG_SAMPLE_FRAC=1.0 (config): negative downsampling disabled (all rows kept)")

    # Issue #14 / WS4 v2: bridge manifest + ClickHouse preflight via shared hook.
    from trainer.training.cross_entry_preflight import run_cross_entry_data_preflight

    _pf0 = time.perf_counter()
    pipeline_echo(
        "Step 0/11 — Preflight — data source (local Parquet bridge readiness or ClickHouse connectivity) …"
    )
    run_cross_entry_data_preflight(
        entry="trainer", use_local_parquet=bool(use_local), logger=logger
    )
    step0_duration_sec = time.perf_counter() - _pf0
    pipeline_echo(f"Step 0/11 — Preflight — done in {step0_duration_sec:.1f}s")

    # Auto-adjust window to actual data end when using local Parquet without
    # explicit --start/--end (anchored to observed data end, not "today").
    if use_local and not (getattr(args, "start", None) or getattr(args, "end", None)):
        data_end = _detect_local_data_end()
        if data_end is not None:
            days = getattr(args, "days", TRAINER_DAYS)
            end = _to_hk(
                datetime.combine(
                    data_end, datetime.min.time()
                )
            )
            start = end - timedelta(days=days)
            logger.info(
                "Local Parquet data end: %s -> adjusted window: %s -> %s",
                data_end, start.date(), end.date(),
            )
        else:
            logger.warning(
                "Could not detect data range from local Parquet metadata; "
                "using default window relative to now. "
                "Consider --start/--end explicitly."
            )

    logger.info("Training window: %s -> %s  (local=%s)", start.date(), end.date(), use_local)

    # Single model_version for this process: matches out/models/<model_version>/ and MLflow run_name.
    pipeline_model_version = get_model_version()

    # T12: one MLflow run for the whole pipeline; on failure log status=FAILED and re-raise.
    with safe_start_run(
        experiment_name=MLFLOW_EXPERIMENT_TRAIN,
        run_name=pipeline_model_version,
    ):
        try:
            # T12.2 / T-PipelineStepDurations: best-effort per-step wall times (steps 0–10 / 11 total).
            # Note: all values are optional; log_*_safe helpers will skip None.
            chunks: list = []
            recent_chunks: Optional[int] = None  # legacy monthly trim removed (single-window pipeline)
            effective_start = start
            effective_end = end
            _effective_neg_sample_frac: float = NEG_SAMPLE_FRAC
            step1_duration_sec: Optional[float] = None
            step2_duration_sec: Optional[float] = None
            step3_duration_sec: Optional[float] = None
            step4_duration_sec: Optional[float] = None
            step5_duration_sec: Optional[float] = None
            step6_duration_sec: Optional[float] = None
            step7_duration_sec: Optional[float] = None
            step7b_duration_sec: Optional[float] = None
            step8_duration_sec: Optional[float] = None
            step9_duration_sec: Optional[float] = None
            step10_duration_sec: Optional[float] = None
            # OOM pre-check estimate (Step 1) and post-check RSS peak (Step 7-9 checkpoint).
            oom_precheck_est_peak_ram_gb: Optional[float] = None
            oom_precheck_step7_rss_error_ratio: Optional[float] = None
            # Process RSS (peak := max(start,end)) and system RAM min/max across Step 7-9.
            step7_rss_start_gb: Optional[float] = None
            step7_rss_end_gb: Optional[float] = None
            step7_rss_peak_gb: Optional[float] = None
            step7_sys_available_min_gb: Optional[float] = None
            step7_sys_used_percent_peak: Optional[float] = None
            _step7_sys_available_start_gb: Optional[float] = None
            _step7_sys_used_percent_start: Optional[float] = None
            step7_chunk_parquet_total_bytes: Optional[int] = None
            step7_chunk_parquet_est_ram_gb: Optional[float] = None
            step8_screening_source: Optional[str] = None
            step8_screening_stats_source: Optional[str] = None
            step8_screening_sample_rows: Optional[int] = None
            step8_screening_full_train_rows: Optional[int] = None
            step8_screening_candidate_cols: Optional[int] = None
            step8_screened_feature_count: Optional[int] = None
            step8_screen_sample_strategy: Optional[str] = None
            duckdb_runtime_step7_memory_gb: Optional[float] = None
            duckdb_runtime_step7_threads: Optional[int] = None
            duckdb_runtime_screening_memory_gb: Optional[float] = None
            duckdb_runtime_screening_threads: Optional[int] = None
            duckdb_runtime_bet_duckdb_window_memory_gb: Optional[float] = None
            duckdb_runtime_bet_duckdb_window_threads: Optional[int] = None
            # Task 7 DoD: Step 6 chunk cache counters -> pipeline_diagnostics.json
            chunk_cache_stats: Dict[str, int] = {}
            issue16_gate_report: Optional[Dict[str, Any]] = None
            feature_materialization_audit: Optional[Dict[str, Any]] = None

            _l2_bundle_arg = getattr(args, "l2_training_bundle", None)
            if _l2_bundle_arg:
                pipeline_step_set("Step 8/11")
                pipeline_echo(
                    "Step 8/11 — L2 bundle (CLI) — Steps 1–7/11 skipped; "
                    "loading bundle then Steps 8–10/11 …"
                )
                pipeline_l2_bundle.execute_l2_training_bundle(
                    args=args,
                    bundle_dir=Path(_l2_bundle_arg),
                    pipeline_model_version=pipeline_model_version,
                    pipeline_started_at_iso=pipeline_started_at_iso,
                    pipeline_start=pipeline_start,
                    use_local=use_local,
                    skip_optuna=skip_optuna,
                    sample_rated_n=sample_rated_n,
                    pipeline_ranking_recipe=pipeline_ranking_recipe,
                    pipeline_gbm_bakeoff=pipeline_gbm_bakeoff,
                )
                return

            # 1. Training window: single run/trip-level slice (no monthly partition).
            pipeline_step_set("Step 1/11")
            _chunk_mode_label = "single-window"
            pipeline_echo(
                f"Step 1/11 — Training window {start.date()} → {end.date()} "
                f"(local_parquet={use_local}); chunk list ({_chunk_mode_label}) …"
            )
            t0 = time.perf_counter()
            chunks = get_single_window_chunk(start, end)
            _el = time.perf_counter() - t0
            step1_duration_sec = _el
            pipeline_echo(f"Step 1/11 — done in {_el:.1f}s ({len(chunks)} window(s), {_chunk_mode_label})")
            logger.info("Chunks: %d mode=%s (%.1fs)", len(chunks), _chunk_mode_label, _el)
            _bundle_dir_raw = (os.environ.get("TRAINER_LAYER_ASSET_BUNDLE_DIR") or "").strip()
            if _bundle_dir_raw:
                try:
                    from trainer.training.layer_asset_store import (
                        load_layer_asset_bundle_index,
                        read_watermark_cursor,
                    )

                    _bd = Path(_bundle_dir_raw)
                    _idx, _idx_err = load_layer_asset_bundle_index(_bd)
                    _wm = read_watermark_cursor(_bd)
                    logger.info(
                        "TRAINER_LAYER_ASSET_BUNDLE_DIR=%s index_ok=%s watermark=%s detail=%s",
                        _bundle_dir_raw,
                        _idx_err is None,
                        _wm is not None,
                        _idx_err or "ok",
                    )
                except Exception as _b_exc:
                    logger.warning("layer asset bundle ingest skipped: %s", _b_exc)

            # Effective window is derived from the window list.
            # All subsequent data loading (identity/profile checks/profile load) must
            # use this window consistently for all tables.
            effective_start = chunks[0]["window_start"] if chunks else start
            effective_end = chunks[-1]["window_end"] if chunks else end
            # DEC-018: normalize effective window to tz-naive so all downstream helpers
            # (ensure_player_profile_ready, load_player_profile, apply_dq
            # called from the canonical-map path) receive tz-naive datetime arguments.
            effective_start = effective_start.replace(tzinfo=None) if effective_start.tzinfo else effective_start
            effective_end   = effective_end.replace(tzinfo=None)   if effective_end.tzinfo   else effective_end
        
            # --- OOM pre-check (earliest feasible point: chunk list is final) ---
            # Estimate Step 7 peak RAM and auto-reduce NEG_SAMPLE_FRAC when OOM is likely.
            # Result may equal NEG_SAMPLE_FRAC (no change) or be lower (auto-adjusted).
            _effective_neg_sample_frac = _oom_check_and_adjust_neg_sample_frac(
                chunks, NEG_SAMPLE_FRAC
            )

            # T12.2: best-effort OOM pre-check estimate (for later RSS error ratio).
            # Keep this as a deterministic/side-effect-free computation so logging
            # never changes pipeline behavior.
            try:
                existing_sizes = [
                    _chunk_parquet_path(c).stat().st_size
                    for c in chunks
                    if _chunk_parquet_path(c).exists()
                    and _chunk_parquet_path(c).with_suffix(".cache_key").exists()
                ]
                if existing_sizes:
                    _per_chunk_bytes = sum(existing_sizes) / len(existing_sizes)
                else:
                    _per_chunk_bytes = NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT

                _n_chunks = len(chunks)
                _estimated_on_disk = _per_chunk_bytes * _n_chunks
                if STEP7_USE_DUCKDB:
                    _estimated_peak_ram = _estimated_on_disk * CHUNK_CONCAT_RAM_FACTOR * TRAIN_SPLIT_FRAC
                else:
                    _estimated_peak_ram = _estimated_on_disk * CHUNK_CONCAT_RAM_FACTOR * (1.0 + TRAIN_SPLIT_FRAC)
                oom_precheck_est_peak_ram_gb = _estimated_peak_ram / (1024**3)
            except Exception:
                oom_precheck_est_peak_ram_gb = None
        
            # 2. Window-count partition — used ONLY to derive train_end for the canonical
            #    mapping cutoff (B1 / R25 identity-leakage guard).  The actual row
            #    assignment to train/valid/test happens later at row level (SSOT §9.2).
            pipeline_step_set("Step 2/11")
            pipeline_echo("Step 2/11 — Partition windows for train_end cutoff (B1) …")
            t0 = time.perf_counter()
            _window_partition = partition_windows_for_train_end_cutoff(chunks)
            _el = time.perf_counter() - t0
            step2_duration_sec = _el
            pipeline_echo(f"Step 2/11 — done in {_el:.1f}s")
            logger.info("Window partition for train_end cutoff: %.1fs", _el)
            train_end = (
                max(c["window_end"] for c in _window_partition["train_windows"])
                if _window_partition["train_windows"] else end
            )
            if hasattr(train_end, "tzinfo") and train_end.tzinfo:
                # DEC-018: tz_convert to HK first, then strip tz, matching labels.py semantics.
                train_end = pd.Timestamp(train_end).tz_convert("Asia/Hong_Kong")
                train_end = train_end.replace(tzinfo=None)

            # B3: identity mapping mode (PIT path is now chunk-scoped in process_chunk).
            _idm_raw = str(getattr(_cfg, "IDENTITY_MAPPING_MODE", "pit_asof")).strip().lower()
            if _idm_raw not in ("pit_asof", "cutoff_window"):
                logger.warning(
                    "IDENTITY_MAPPING_MODE=%r invalid (use pit_asof or cutoff_window); using cutoff_window",
                    getattr(_cfg, "IDENTITY_MAPPING_MODE", None),
                )
                _idm_raw = "cutoff_window"
            effective_identity_mode = _idm_raw
            if effective_identity_mode == "pit_asof" and not use_local:
                logger.warning(
                    "IDENTITY_MAPPING_MODE=pit_asof requires local Parquet; using cutoff_window for this run",
                )
                effective_identity_mode = "cutoff_window"
            if effective_identity_mode == "pit_asof":
                _sess_pit_path = local_parquet_session_path_for_trainer()
                if not _sess_pit_path.is_file():
                    logger.warning(
                        "IDENTITY_MAPPING_MODE=pit_asof but session Parquet missing (%s); "
                        "using cutoff_window",
                        _sess_pit_path,
                    )
                    effective_identity_mode = "cutoff_window"
        
            # 3. Build canonical mapping with TRAINING window cutoff (B1 — prevents
            #    identity links that arose after training from leaking into training data).
            #    Also get FND-12 dummy player_ids so we drop them from training (TRN-04).
            #    PLAN steps 4/7/8: local path may load from artifact; else DuckDB or pandas build; write after build.
            pipeline_step_set("Step 3/11")
            pipeline_echo("Step 3/11 — Build canonical identity mapping …")
            t0 = time.perf_counter()
            logger.info("Building canonical identity mapping (cutoff=%s)…", train_end)
            dummy_player_ids: set = set()
            rebuild_canonical = getattr(args, "rebuild_canonical_mapping", False)
            _canonical_built = False
            _canonical_source_snapshot_id = read_bridge_source_snapshot_id() if use_local else None
            _canonical_bridge_manifest_stat = (
                l2_bundle_materialize.bridge_manifest_stat_token() if use_local else None
            )
            # PLAN step 8: try load existing artifact once (use_local and ClickHouse paths both skip build if ok)
            loaded_from_artifact = False
            if not rebuild_canonical and CANONICAL_MAPPING_PARQUET.exists() and CANONICAL_MAPPING_CUTOFF_JSON.exists():
                try:
                    with open(CANONICAL_MAPPING_CUTOFF_JSON, encoding="utf-8") as _f:
                        _sidecar = json.load(_f)
                    _cutoff_str = _sidecar.get("cutoff_dtm")
                    _cutoff_ts = pd.Timestamp(_cutoff_str) if _cutoff_str else None
                    if _cutoff_ts is not None:
                        _cutoff_naive = _cutoff_ts.replace(tzinfo=None) if _cutoff_ts.tz else _cutoff_ts
                        _source_guard_reasons: List[str] = []
                        if use_local:
                            _sidecar_source_snapshot_id = (
                                str(_sidecar.get("source_snapshot_id") or "").strip() or None
                            )
                            _sidecar_bridge_manifest_stat = (
                                str(_sidecar.get("bridge_manifest_stat") or "").strip() or None
                            )
                            if _canonical_source_snapshot_id != _sidecar_source_snapshot_id:
                                _source_guard_reasons.append(
                                    "source_snapshot_id mismatch "
                                    f"(sidecar={_sidecar_source_snapshot_id!r}, "
                                    f"current={_canonical_source_snapshot_id!r})"
                                )
                            if _canonical_bridge_manifest_stat != _sidecar_bridge_manifest_stat:
                                _source_guard_reasons.append(
                                    "bridge_manifest_stat mismatch "
                                    f"(sidecar={_sidecar_bridge_manifest_stat!r}, "
                                    f"current={_canonical_bridge_manifest_stat!r})"
                                )
                        if _cutoff_naive >= train_end and not _source_guard_reasons:
                            canonical_map = pd.read_parquet(CANONICAL_MAPPING_PARQUET)
                            if set(canonical_map.columns) >= {"player_id", "canonical_id"}:
                                dummy_player_ids = set(_sidecar.get("dummy_player_ids") or [])
                                dummy_player_ids = set(int(x) for x in dummy_player_ids)
                                loaded_from_artifact = True
                                logger.info(
                                    "Canonical mapping loaded from %s (cutoff %s >= train_end)",
                                    CANONICAL_MAPPING_PARQUET, _cutoff_str,
                                )
                            else:
                                logger.warning(
                                    "Canonical mapping artifact missing required columns; will rebuild"
                                )
                        elif _source_guard_reasons:
                            logger.warning(
                                "Canonical mapping artifact source guard failed (%s); will rebuild",
                                "; ".join(_source_guard_reasons),
                            )
                except Exception as exc:
                    logger.warning("Load canonical mapping artifact failed (%s); will rebuild", exc)
        
            if loaded_from_artifact:
                pass  # canonical_map, dummy_player_ids already set; skip build for both use_local and ClickHouse
            elif use_local:
                sessions_all = None  # R403 guardrail: ensure release in every path; set again in pandas branch
                use_full_sessions_pandas = getattr(_cfg, "CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS", False)
                if use_full_sessions_pandas:
                    logger.warning(
                        "CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS=True: loading full session window into pandas (high OOM risk, A03). Use only for debugging; keep DuckDB path in production."
                    )
                    _, sessions_all = load_local_parquet(
                        effective_start,
                        effective_end + timedelta(days=1),
                        sessions_only=True,
                    )
                    _, sessions_all = normalize_bets_sessions(pd.DataFrame(), sessions_all)
                    _, sessions_all = apply_dq(
                        pd.DataFrame(columns=["bet_id"]),
                        sessions_all,
                        effective_start,
                        effective_end + timedelta(days=1),
                    )
                    canonical_map = build_canonical_mapping_from_df(sessions_all, cutoff_dtm=train_end)
                    try:
                        dummy_player_ids = get_dummy_player_ids_from_df(sessions_all, cutoff_dtm=train_end)
                    except Exception as exc:
                        logger.warning("get_dummy_player_ids_from_df failed (%s); not filtering dummies", exc)
                    sessions_all = None
                else:
                    sess_path = local_parquet_session_path_for_trainer()
                    links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(sess_path, train_end)
                    canonical_map = build_canonical_mapping_from_links(links_df, dummy_pids)
                    dummy_player_ids = dummy_pids
                    sessions_all = None  # not used in DuckDB path; clear for peak memory guardrail (R403)
                _canonical_built = True
        
                if _canonical_built:
                    try:
                        canonical_map.to_parquet(CANONICAL_MAPPING_PARQUET, index=False)
                        _cutoff_iso = train_end.isoformat() if hasattr(train_end, "isoformat") else str(train_end)
                        with open(CANONICAL_MAPPING_CUTOFF_JSON, "w", encoding="utf-8") as _f:
                            json.dump(
                                {
                                    "cutoff_dtm": _cutoff_iso,
                                    "dummy_player_ids": list(dummy_player_ids),
                                    "source_snapshot_id": _canonical_source_snapshot_id,
                                    "bridge_manifest_stat": _canonical_bridge_manifest_stat,
                                },
                                _f,
                                indent=0,
                            )
                        logger.info("Canonical mapping written to %s", CANONICAL_MAPPING_PARQUET)
                    except Exception as exc:
                        logger.warning("Write canonical mapping artifact failed (%s); next run will rebuild", exc)
                sessions_all = None
            else:
                try:
                    client = get_clickhouse_client()
                    canonical_map = build_canonical_mapping(client, cutoff_dtm=train_end)
                    dummy_player_ids = get_dummy_player_ids(client, cutoff_dtm=train_end)
                except Exception as exc:
                    logger.warning("ClickHouse canonical mapping failed (%s); using empty map", exc)
                    canonical_map = pd.DataFrame(columns=["player_id", "canonical_id"])
                    dummy_player_ids = set()
                sessions_all = None
                # PLAN § Canonical mapping 步驟 7：ClickHouse 路徑建完後也寫出，供共用／下次載入
                if set(canonical_map.columns) >= {"player_id", "canonical_id"} and not canonical_map.empty:
                    try:
                        canonical_map.to_parquet(CANONICAL_MAPPING_PARQUET, index=False)
                        _cutoff_iso = train_end.isoformat() if hasattr(train_end, "isoformat") else str(train_end)
                        with open(CANONICAL_MAPPING_CUTOFF_JSON, "w", encoding="utf-8") as _f:
                            json.dump(
                                {
                                    "cutoff_dtm": _cutoff_iso,
                                    "dummy_player_ids": list(dummy_player_ids),
                                    "source_snapshot_id": _canonical_source_snapshot_id,
                                    "bridge_manifest_stat": _canonical_bridge_manifest_stat,
                                },
                                _f,
                                indent=0,
                            )
                        logger.info("Canonical mapping written to %s (from ClickHouse)", CANONICAL_MAPPING_PARQUET)
                    except Exception as exc:
                        logger.warning("Write canonical mapping artifact failed (%s); next run will rebuild", exc)
        
            _el = time.perf_counter() - t0
            step3_duration_sec = _el
            pipeline_echo(f"Step 3/11 — done in {_el:.1f}s (canonical_map rows={len(canonical_map)})")
            logger.info(
                "Canonical mapping: %d rows; FND-12 dummy player_ids to exclude: %d  (%.1fs)",
                len(canonical_map), len(dummy_player_ids), _el,
            )

            # B3: enrich canonical sidecar with identity mode (merge with existing keys).
            try:
                _sidecar_path = CANONICAL_MAPPING_CUTOFF_JSON
                _sidecar_data: dict = {}
                if _sidecar_path.exists():
                    with open(_sidecar_path, encoding="utf-8") as _sf:
                        _sidecar_data = json.load(_sf)
                if not isinstance(_sidecar_data, dict):
                    _sidecar_data = {}
                if "cutoff_dtm" not in _sidecar_data:
                    _te_iso = train_end.isoformat() if hasattr(train_end, "isoformat") else str(train_end)
                    _sidecar_data["cutoff_dtm"] = _te_iso
                if "dummy_player_ids" not in _sidecar_data:
                    _sidecar_data["dummy_player_ids"] = list(dummy_player_ids)
                if use_local:
                    _sidecar_data["source_snapshot_id"] = _canonical_source_snapshot_id
                    _sidecar_data["bridge_manifest_stat"] = _canonical_bridge_manifest_stat
                _sidecar_data["identity_mapping_mode"] = effective_identity_mode
                _sidecar_data["t_game_features_enabled"] = bool(
                    getattr(_cfg, "T_GAME_FEATURES_ENABLED", False)
                )
                _sidecar_data["t_game_visible_time_column"] = (
                    "__etl_insert_Dtm" if bool(getattr(_cfg, "T_GAME_FEATURES_ENABLED", False)) else "none"
                )
                with open(_sidecar_path, "w", encoding="utf-8") as _sf:
                    json.dump(_sidecar_data, _sf, indent=0)
            except Exception as _side_exc:
                logger.warning("Could not update canonical_mapping.cutoff.json sidecar (%s)", _side_exc)

            # GitHub #17: auto L2 bundle cache — default for --use-local-parquet (single run/trip path).
            _auto_l2 = not getattr(args, "l2_training_bundle", None)
            if _auto_l2:
                pipeline_step_set("Step 8/11")
                # Cache hit must not require source_snapshot_id on the bridge manifest;
                # cache key already includes bridge_manifest_stat_token() when present.
                _raw_dir = getattr(args, "l2_auto_bundle_dir", None)
                _bundle_dir_early = Path(_raw_dir) if _raw_dir else l2_bundle_materialize.default_auto_bundle_dir()
                _ws_e = (
                    effective_start.isoformat()
                    if hasattr(effective_start, "isoformat")
                    else str(effective_start)
                )
                _we_e = (
                    effective_end.isoformat()
                    if hasattr(effective_end, "isoformat")
                    else str(effective_end)
                )
                _expected_key = l2_bundle_materialize.build_auto_l2_cache_key(
                    bridge_manifest_stat=l2_bundle_materialize.bridge_manifest_stat_token(),
                    window_start_iso=_ws_e,
                    window_end_iso=_we_e,
                    recent_chunks=recent_chunks,
                    train_split_frac=float(TRAIN_SPLIT_FRAC),
                    valid_split_frac=float(VALID_SPLIT_FRAC),
                    neg_sample_frac_config=float(NEG_SAMPLE_FRAC),
                    feature_spec_fingerprint=l2_bundle_materialize.fingerprint_feature_spec(FEATURE_SPEC_PATH),
                    rebuild_canonical_mapping=bool(getattr(args, "rebuild_canonical_mapping", False)),
                    identity_mapping_mode=str(effective_identity_mode),
                    force_recompute=bool(force),
                )
                if l2_bundle_materialize.auto_bundle_cache_is_current(
                    bundle_dir=_bundle_dir_early, expected_key=_expected_key
                ):
                    pipeline_echo(
                        f"Step 8/11 — L2 auto-cache — hit at {_bundle_dir_early}; "
                        "skipping Steps 4–10/11 (chunk path), running Steps 8–10/11 from bundle …"
                    )
                    pipeline_l2_bundle.execute_l2_training_bundle(
                        args=args,
                        bundle_dir=_bundle_dir_early,
                        pipeline_model_version=pipeline_model_version,
                        pipeline_started_at_iso=pipeline_started_at_iso,
                        pipeline_start=pipeline_start,
                        use_local=use_local,
                        skip_optuna=skip_optuna,
                        sample_rated_n=sample_rated_n,
                        pipeline_ranking_recipe=pipeline_ranking_recipe,
                        pipeline_gbm_bakeoff=pipeline_gbm_bakeoff,
                    )
                    return

            pipeline_step_set("Step 3/11")
            # Rated-patron sampling is an independent option controlled by --sample-rated N.
            rated_whitelist: Optional[set] = None
            if sample_rated_n is not None and not canonical_map.empty:
                _sample = (
                    canonical_map["canonical_id"]
                    .astype(str)
                    .drop_duplicates()
                    .sort_values()
                    .head(sample_rated_n)
                )
                rated_whitelist = set(_sample.tolist())
                logger.info(
                    "--sample-rated: sampled %d / %d rated canonical_ids (deterministic sort+head)",
                    len(rated_whitelist), canonical_map["canonical_id"].nunique(),
                )

            pipeline_step_set("Step 4/11")
            profile_df = None
            step4_duration_sec = 0.0
            step5_duration_sec = 0.0
            _player_run_gate = False
            try:
                import yaml as _yaml_gate
                from pathlib import Path as _Path_gate

                from trainer.features.features import _track_section_enabled_in_spec

                _raw_gate = _yaml_gate.safe_load(
                    _Path_gate(FEATURE_SPEC_PATH).read_text(encoding="utf-8")
                ) or {}
                _player_run_gate = _track_section_enabled_in_spec(_raw_gate, "player_run_asset")
            except Exception as _gate_exc:
                logger.debug("player_run_asset gate: spec read failed (%s)", _gate_exc)

            if _player_run_gate and use_local:
                pipeline_echo(
                    "Step 4/11 — Ensure layered player/run assets ready "
                    "(L1 run_fact / run_bet_map for bridge snapshot) …"
                )
                t0 = time.perf_counter()
                from trainer.features.player_run_layer import ensure_player_run_layer_assets_ready

                ensure_player_run_layer_assets_ready()
                step4_duration_sec = time.perf_counter() - t0
                pipeline_echo(f"Step 4/11 — done in {step4_duration_sec:.1f}s")
                logger.info("ensure_player_run_layer_assets_ready: %.1fs", step4_duration_sec)
            elif _player_run_gate and not use_local:
                raise RuntimeError(
                    "feature spec enables player_run_asset but --use-local-parquet was not set. "
                    "Run-primitive player features require local Parquet + data/l1_layered assets."
                )
            else:
                pipeline_echo(
                    "Step 4/11 — Skip layered player/run asset check "
                    "(player_run_asset disabled in feature spec) …"
                )
                step4_duration_sec = 0.0

            pipeline_step_set("Step 5/11")
            if _player_run_gate:
                pipeline_echo(
                    "Step 5/11 — Load layered player assets for PIT materialization "
                    "(inline per chunk via run_fact/run_bet_map; no player_profile parquet) …"
                )
                step5_duration_sec = 0.0
                logger.info(
                    "Step 5: skipping player_profile load — compute_player_layer_features "
                    "materializes player_run_asset per chunk"
                )
            else:
                pipeline_echo(
                    "Step 5/11 — Skip player-layer preload (player_run_asset disabled) …"
                )
                step5_duration_sec = 0.0
                logger.info("Step 5: player_run_asset disabled — no player-layer preload")

            pipeline_step_set("Step 6/11")
            feature_spec = load_feature_spec(FEATURE_SPEC_PATH)
            try:
                feature_spec_hash = hashlib.md5(Path(FEATURE_SPEC_PATH).read_bytes()).hexdigest()[:12]
            except Exception:
                feature_spec_hash = "unknown"
            logger.info(
                "Track LLM: loaded feature spec from %s (spec_hash=%s)",
                FEATURE_SPEC_PATH,
                feature_spec_hash,
            )
        
            # 4. Process chunks -> write parquet
            # When NEG_SAMPLE_FRAC_AUTO and there are chunks, run chunk 1 with frac=1.0 (OOM probe),
            # measure size, possibly lower _effective_neg_sample_frac, then process remaining chunks.
            _neg_sample_note = (
                f"  neg-sample={_effective_neg_sample_frac:.2f}" if _effective_neg_sample_frac < 1.0 else ""
            )
            pipeline_echo(
                f"Step 6/11 — Process chunks (DQ, labels, Track Human, Track LLM){_neg_sample_note} …"
            )
            t0 = time.perf_counter()
            chunk_paths: List[Path] = []
            _step6_disable_bar = getattr(_cfg, "DISABLE_PROGRESS_BAR", False)
            pbar = (
                _ProgressNoop()
                if _step6_disable_bar
                else _tqdm_bar(total=len(chunks), desc="Step 6/11 chunks", unit="chunk")
            )
            try:
                if NEG_SAMPLE_FRAC_AUTO and len(chunks) > 0:
                    # OOM probe: process chunk 1 with frac=1.0, then decide effective frac.
                    pipeline_echo("Step 6/11 — OOM probe: processing chunk 1 with neg_sample_frac=1.0 …")
                    logger.info("OOM probe: processing chunk 1 with neg_sample_frac=1.0")
                    path1 = process_chunk(
                        chunks[0],
                        canonical_map,
                        dummy_player_ids=dummy_player_ids,
                        use_local_parquet=use_local,
                        force_recompute=force,
                        profile_df=profile_df,
                        feature_spec=feature_spec,
                        feature_spec_hash=feature_spec_hash,
                        neg_sample_frac=1.0,
                        chunk_cache_stats=chunk_cache_stats,
                        identity_mapping_mode=effective_identity_mode,
                    )
                    if path1 is not None:
                        _path1 = Path(path1) if isinstance(path1, str) else path1
                        if getattr(_path1, "exists", lambda: False)() and _path1.is_file():
                            size_chunk1 = _path1.stat().st_size
                            _effective_neg_sample_frac = _oom_check_after_chunk1(
                                size_chunk1, len(chunks), _effective_neg_sample_frac
                            )
                            if _effective_neg_sample_frac < 1.0:
                                path1_rerun = process_chunk(
                                    chunks[0],
                                    canonical_map,
                                    dummy_player_ids=dummy_player_ids,
                                    use_local_parquet=use_local,
                                    force_recompute=force,
                                    profile_df=profile_df,
                                    feature_spec=feature_spec,
                                    feature_spec_hash=feature_spec_hash,
                                    neg_sample_frac=_effective_neg_sample_frac,
                                    chunk_cache_stats=chunk_cache_stats,
                                    identity_mapping_mode=effective_identity_mode,
                                )
                                if path1_rerun is not None:
                                    chunk_paths.append(path1_rerun)
                                    pbar.update(1)
                                else:
                                    chunk_paths.append(path1)
                                    pbar.update(1)
                            else:
                                chunk_paths.append(path1)
                                pbar.update(1)
                        else:
                            # Path does not exist (e.g. test mock): skip size-based adjustment
                            chunk_paths.append(path1)
                            pbar.update(1)
                        gc.collect()
                        for chunk in chunks[1:]:
                            path = process_chunk(
                                chunk,
                                canonical_map,
                                dummy_player_ids=dummy_player_ids,
                                use_local_parquet=use_local,
                                force_recompute=force,
                                profile_df=profile_df,
                                feature_spec=feature_spec,
                                feature_spec_hash=feature_spec_hash,
                                neg_sample_frac=_effective_neg_sample_frac,
                                chunk_cache_stats=chunk_cache_stats,
                                identity_mapping_mode=effective_identity_mode,
                            )
                            if path is not None:
                                chunk_paths.append(path)
                                pbar.update(1)
                            gc.collect()
                    else:
                        # Chunk 1 empty: no probe decision, use _effective_neg_sample_frac for all.
                        for chunk in chunks:
                            path = process_chunk(
                                chunk,
                                canonical_map,
                                dummy_player_ids=dummy_player_ids,
                                use_local_parquet=use_local,
                                force_recompute=force,
                                profile_df=profile_df,
                                feature_spec=feature_spec,
                                feature_spec_hash=feature_spec_hash,
                                neg_sample_frac=_effective_neg_sample_frac,
                                chunk_cache_stats=chunk_cache_stats,
                                identity_mapping_mode=effective_identity_mode,
                            )
                            if path is not None:
                                chunk_paths.append(path)
                                pbar.update(1)
                            gc.collect()
                else:
                    for i, chunk in enumerate(chunks):
                        path = process_chunk(
                            chunk,
                            canonical_map,
                            dummy_player_ids=dummy_player_ids,
                            use_local_parquet=use_local,
                            force_recompute=force,
                            profile_df=profile_df,
                            feature_spec=feature_spec,
                            feature_spec_hash=feature_spec_hash,
                            neg_sample_frac=_effective_neg_sample_frac,
                            chunk_cache_stats=chunk_cache_stats,
                            identity_mapping_mode=effective_identity_mode,
                        )
                        if path is not None:
                            chunk_paths.append(path)
                            pbar.update(1)
                        gc.collect()
            finally:
                pbar.close()

            if not chunk_paths:
                _cm_rows = len(canonical_map) if canonical_map is not None else 0
                raise RuntimeError(
                    "Step 6 produced no chunk Parquet outputs (empty chunk_paths). "
                    "Usually every row failed identity/PIT admission — see Step 6 logs for "
                    "IDENTITY_UNMATCHED / admitted=0. Check that canonical_mapping aligns with "
                    "the bet/session Parquet bridge (player_id keys), rebuild with "
                    "--rebuild-canonical-mapping if needed, or adjust IDENTITY_MAPPING_MODE. "
                    f"Current canonical_map rows={_cm_rows}. "
                    "This is not an OOM/days-window issue despite later Step 7 wording."
                )

            # 5. Load all chunks, sort, row-level train/valid/test split (PLAN Step 7 Out-of-Core).
            #    Orchestrator: DuckDB first (Layer 1), on failure pandas fallback (Layer 3).
            # T12.2: checkpoint memory sampling across Step 7-9.
            # Peak := max(start, end) to keep sampling overhead low while satisfying
            # "start/peak/end are present" logging contracts.
            try:
                import psutil as _psutil  # optional dependency (best-effort)

                _step7_process = _psutil.Process()
                step7_rss_start_gb = _step7_process.memory_info().rss / (1024**3)
                _vm_start = _psutil.virtual_memory()
                _step7_sys_available_start_gb = _vm_start.available / (1024**3)
                _step7_sys_used_percent_start = float(_vm_start.percent)

                # MLflow tag naming contract (constants must be present in source).
                log_tags_safe(
                    {
                        "memory_sampling": "checkpoint_peak",
                        "memory_sampling_scope": "step7_9",
                    }
                )
            except Exception as _e:
                # If psutil is unavailable, still tag so MLflow run can be diagnosed.
                log_tags_safe({"memory_sampling": "disabled_no_psutil"})

            pipeline_step_set("Step 7/11")
            pipeline_echo("Step 7/11 — Load chunks, concat, row-level train/valid/test split …")
            t0 = time.perf_counter()
            _chunk_total_bytes = sum(Path(p).stat().st_size for p in chunk_paths)
            _est_ram_gb = (_chunk_total_bytes * CHUNK_CONCAT_RAM_FACTOR) / (1024**3)
            step7_chunk_parquet_total_bytes = _chunk_total_bytes
            step7_chunk_parquet_est_ram_gb = _est_ram_gb
            if _chunk_total_bytes >= CHUNK_CONCAT_MEMORY_WARN_BYTES:
                logger.warning(
                    "Chunk Parquets total %.2f GB on disk -> estimated %.1f GB RAM for concat + train/valid split. "
                    "Reduce training window (--days / --start --end) or ensure sufficient RAM to avoid OOM.",
                    _chunk_total_bytes / (1024**3),
                    _est_ram_gb,
                )
            # R803: validate fractions at runtime so misconfiguration is caught early (-O safe).
            # Match _duckdb_sort_and_split / _step7_pandas_fallback: train and valid strictly
            # positive and TRAIN_SPLIT_FRAC + VALID_SPLIT_FRAC < 1.0 so implicit test_frac > 0.
            # Row-count adequacy (vs MIN_VALID_TEST_ROWS) is logged after the split, not here.
            if not (
                0 < float(TRAIN_SPLIT_FRAC)
                and 0 < float(VALID_SPLIT_FRAC)
                and TRAIN_SPLIT_FRAC + VALID_SPLIT_FRAC < 1.0
            ):
                _implicit_test_frac = 1.0 - float(TRAIN_SPLIT_FRAC) - float(VALID_SPLIT_FRAC)
                raise ValueError(
                    f"TRAIN_SPLIT_FRAC ({TRAIN_SPLIT_FRAC}) and VALID_SPLIT_FRAC ({VALID_SPLIT_FRAC}) "
                    f"must each be in (0, 1) with sum < 1.0; implicit test_frac would be {_implicit_test_frac}"
                )
        
            def _run_step6(neg_frac: float) -> List[Path]:
                """Re-run Step 6 with given neg_sample_frac and force_recompute=True (Layer 2 OOM retry)."""
                paths: List[Path] = []
                for _i, _chunk in enumerate(chunks):
                    _path = process_chunk(
                        _chunk,
                        canonical_map,
                        dummy_player_ids=dummy_player_ids,
                        use_local_parquet=use_local,
                        force_recompute=True,
                        profile_df=profile_df,
                        feature_spec=feature_spec,
                        feature_spec_hash=feature_spec_hash,
                        neg_sample_frac=neg_frac,
                        chunk_cache_stats=chunk_cache_stats,
                        identity_mapping_mode=effective_identity_mode,
                    )
                    if _path is not None:
                        paths.append(_path)
                    gc.collect()
                return paths
        
            _duckdb_step7_rt = DuckdbStep7Runtime()
            _step7_result = _step7_sort_and_split(
                chunk_paths,
                TRAIN_SPLIT_FRAC,
                VALID_SPLIT_FRAC,
                step6_runner=_run_step6,
                current_neg_frac=_effective_neg_sample_frac,
                step7_use_duckdb=STEP7_USE_DUCKDB,
                step7_keep_train_on_disk=STEP7_KEEP_TRAIN_ON_DISK,
                step9_export_libsvm=STEP9_EXPORT_LIBSVM,
                chunk_concat_ram_factor=CHUNK_CONCAT_RAM_FACTOR,
                step7_pandas_fallback_max_bytes=STEP7_PANDAS_FALLBACK_MAX_BYTES,
                neg_sample_ram_safety=NEG_SAMPLE_RAM_SAFETY,
                neg_sample_frac_min=NEG_SAMPLE_FRAC_MIN,
                duckdb_runtime=_duckdb_step7_rt,
            )
            duckdb_runtime_step7_memory_gb = _duckdb_step7_rt.memory_gb
            duckdb_runtime_step7_threads = _duckdb_step7_rt.threads
            train_df, valid_df, test_df, step7_train_path, step7_valid_path, step7_test_path = _step7_result
            # GitHub #19: apply negative downsampling on train split only (valid/test stay full).
            if _effective_neg_sample_frac < 1.0 - 1e-12:
                pipeline_step_set("Step 7b/11")
                _t7b0 = time.perf_counter()
                _train_rs = int(
                    hashlib.md5(
                        f"{effective_start.isoformat()}|{effective_end.isoformat()}".encode()
                    ).hexdigest()[:8],
                    16,
                ) % (2**31)
                pipeline_echo(
                    f"Step 7b/11 — train-only negative downsampling "
                    f"(frac={_effective_neg_sample_frac:.4f}) …"
                )
                if step7_train_path is not None:
                    _apply_train_neg_downsample_to_parquet(
                        step7_train_path,
                        neg_sample_frac=_effective_neg_sample_frac,
                        random_state=_train_rs,
                    )
                elif train_df is not None:
                    train_df = apply_train_only_negative_downsampling(
                        train_df,
                        neg_sample_frac=_effective_neg_sample_frac,
                        random_state=_train_rs,
                    )
                step7b_duration_sec = time.perf_counter() - _t7b0
                pipeline_echo(
                    f"Step 7b/11 — done in {step7b_duration_sec:.1f}s "
                    f"(train-only neg-sample frac={_effective_neg_sample_frac:.4f})"
                )
            pipeline_step_set("Step 7/11")
            step8_screen_sample_strategy = _step8_resolve_sample_strategy(
                getattr(_cfg, "STEP8_SCREEN_SAMPLE_STRATEGY", STEP8_SCREEN_SAMPLE_STRATEGY)
            )
            _train_libsvm: Optional[Path] = None
            _valid_libsvm: Optional[Path] = None
            _test_libsvm: Optional[Path] = None
            _step9_issue8_pretrained = False
            _step9_issue8_elapsed_sec = 0.0
            if step7_train_path is not None:
                # R202 Review #3: guard so _step7_metadata_from_paths never receives None (B+ path contract).
                if step7_valid_path is None or step7_test_path is None:
                    raise ValueError(
                        "step7_valid_path and step7_test_path must be set when step7_train_path is set (B+ path)."
                    )
                # PLAN B+ Stage 1–2: train not loaded; get metadata and sample for Step 8 from file.
                _n_train, _n_valid, _n_test, _label1_total, _train_end_max = step7_metadata_from_paths(
                    step7_train_path, step7_valid_path, step7_test_path
                )
                _total_rows = _n_train + _n_valid + _n_test
                _label1 = _label1_total
                _actual_train_end = _train_end_max
                _sample_n_disk = (
                    int(STEP8_SCREEN_SAMPLE_ROWS)
                    if (STEP8_SCREEN_SAMPLE_ROWS is not None and STEP8_SCREEN_SAMPLE_ROWS >= 1)
                    else 2_000_000
                )
                if step8_screen_sample_strategy == "tail":
                    _train_for_screen = _read_parquet_tail_step8(
                        step7_train_path, _sample_n_disk
                    )
                elif step8_screen_sample_strategy == "head_tail":
                    _train_for_screen = _read_parquet_head_tail_step8(
                        step7_train_path,
                        _sample_n_disk,
                        read_head=read_parquet_head,
                    )
                else:
                    _train_for_screen = read_parquet_head(step7_train_path, _sample_n_disk)
            else:
                assert train_df is not None  # step7_train_path is None implies train was loaded in Step 7
                assert valid_df is not None and test_df is not None  # pandas path always has both
                _train_for_screen = None
                _n_valid = len(valid_df)
                _n_test = len(test_df)
                _total_rows = len(train_df) + _n_valid + _n_test
                _label1 = int(train_df["label"].sum()) + int(valid_df["label"].sum()) + int(test_df["label"].sum())
                _actual_train_end = train_df["payout_complete_dtm"].max() if not train_df.empty else None
            if step7_train_path is not None:
                assert step7_valid_path is not None and step7_test_path is not None
                _split_row_meta = split_row_metadata_from_parquet_paths(
                    step7_train_path,
                    step7_valid_path,
                    step7_test_path,
                )
                _model_used_split_meta = split_row_metadata_from_parquet_paths(
                    step7_train_path,
                    step7_valid_path,
                    step7_test_path,
                    rated_only=True,
                )
            else:
                _split_row_meta = split_row_metadata_from_dataframes(
                    cast(pd.DataFrame, train_df),
                    cast(pd.DataFrame, valid_df),
                    cast(pd.DataFrame, test_df),
                )
                _model_used_split_meta = split_row_metadata_from_dataframes(
                    cast(pd.DataFrame, train_df),
                    cast(pd.DataFrame, valid_df),
                    cast(pd.DataFrame, test_df),
                    rated_only=True,
                )
            _train_schema_col_list: Optional[List[str]] = None
            if train_df is not None:
                _train_schema_col_list = list(train_df.columns)
            elif step7_train_path is not None:
                try:
                    import pyarrow.parquet as pq

                    _train_schema_col_list = list(pq.ParquetFile(step7_train_path).schema_arrow.names)
                except Exception as _schema_exc:
                    logger.warning(
                        "Could not read train parquet schema for spec-first audit: %s",
                        _schema_exc,
                    )
            if _train_schema_col_list is not None:
                try:
                    from trainer.training import feature_materialization as _fm_audit

                    _fm_audit.maybe_raise_spec_first_columns(_train_schema_col_list, feature_spec)
                    _curr_src_snap = read_bridge_source_snapshot_id() if use_local else None
                    _prev_src_snap = (os.environ.get("TRAINER_PREV_SOURCE_SNAPSHOT_ID") or "").strip() or None
                    _chunk_partition_ids_for_audit: Optional[List[str]] = None
                    try:
                        from trainer.training.layer_asset_store import chunk_partition_ids_for_windows

                        _chunk_partition_ids_for_audit = chunk_partition_ids_for_windows(chunks)
                    except Exception as _cpid_exc:
                        logger.debug("chunk_partition_ids_for audit skipped: %s", _cpid_exc)
                    feature_materialization_audit = _fm_audit.build_pipeline_feature_materialization_audit(
                        feature_spec=feature_spec,
                        train_columns=_train_schema_col_list,
                        curr_source_snapshot_id=_curr_src_snap,
                        prev_source_snapshot_id=_prev_src_snap,
                        pit_policy_id=str(effective_identity_mode),
                        chunk_partition_ids=_chunk_partition_ids_for_audit,
                    )
                    _fm_audit.raise_if_strict_materialization_gates_failed(
                        feature_materialization_audit["materialization_gates"],
                    )
                except RuntimeError:
                    raise
                except Exception as _fma_exc:
                    logger.warning("feature_materialization audit build failed (non-fatal): %s", _fma_exc)
            try:
                from trainer.training.issue16_gates import (
                    evaluate_issue16_gate_bundle,
                    raise_if_strict_issue16_gates_failed,
                )
                from trainer.training.l2_trainer_contracts import TRAIN_END_SOURCE_CHUNK_SPLIT

                _neg_mode = (
                    (os.environ.get("TRAINER_TRAIN_NEG_SAMPLING_MODE") or "post_step7")
                    .strip()
                    .lower()
                )
                if _neg_mode not in ("post_step7", "legacy_chunk"):
                    _neg_mode = "post_step7"
                issue16_gate_report = evaluate_issue16_gate_bundle(
                    effective_neg_sample_frac=float(_effective_neg_sample_frac),
                    chunk_train_end_naive=train_end,
                    row_level_train_end_max=_actual_train_end,
                    train_end_source=TRAIN_END_SOURCE_CHUNK_SPLIT,
                    l2_snapshot_id=None,
                    label_asset_meta=None,
                    training_source_snapshot_id=None,
                    split_flags=None,
                    train_column_names=_train_schema_col_list,
                    feature_spec=feature_spec,
                    train_neg_sampling_mode=_neg_mode,
                )
                raise_if_strict_issue16_gates_failed(issue16_gate_report)
            except RuntimeError:
                raise
            except Exception as _i16_exc:
                logger.warning("Issue #16 gate evaluation failed (non-fatal): %s", _i16_exc)
            _train_cols = (
                train_df.columns
                if train_df is not None
                else (_train_for_screen.columns if _train_for_screen is not None else pd.Index([]))
            )
            n_rows = _total_rows  # for downstream summary (artifact, logs)
            _n_train_print = _n_train if step7_train_path is not None else (len(train_df) if train_df is not None else 0)
            logger.info("Total rows: %d  (label=1: %d)", _total_rows, _label1)
        
            # R700: compare row-level _actual_train_end against chunk-level train_end.
            # The canonical mapping cutoff (B1/R25 guard) always uses chunk-level train_end;
            # this log makes any semantic drift between the two boundaries observable.
            # R701 (known limitation): same run rows may be assigned to different split sets
            # at row-level boundaries — group-aware split is a long-term improvement.
            if _actual_train_end is not None and pd.notnull(_actual_train_end):
                _te_chunk = pd.Timestamp(train_end) if train_end else None
                # DEC-018: strip tz from _te_chunk so both sides are tz-naive for comparison
                # (train_end comes from chunk["window_end"] which is tz-aware; _actual_train_end
                # comes from payout_complete_dtm which is tz-naive after apply_dq).
                if _te_chunk is not None and _te_chunk.tzinfo is not None:
                    _te_chunk = _te_chunk.replace(tzinfo=None)
                _te_row = pd.Timestamp(str(_actual_train_end))
                # DEC-018: strip tz from _te_row for the same reason as _te_chunk —
                # payout_complete_dtm may be tz-aware when sourced from test mocks or
                # external Parquet that skipped apply_dq().
                if _te_row.tzinfo is not None:
                    _te_row = _te_row.replace(tzinfo=None)
                if _te_chunk is not None and _te_row != _te_chunk:
                    logger.warning(
                        "R700: chunk-level train_end (%s) differs from row-level "
                        "_actual_train_end (%s) by %s — "
                        "B1/R25 canonical mapping cutoff uses chunk-level train_end.",
                        _te_chunk.date(), _te_row.date(),
                        abs(_te_row - _te_chunk),
                    )
                else:
                    logger.info(
                        "R700: chunk-level train_end (%s) matches row-level _actual_train_end (%s).",
                        _te_chunk, _te_row,
                    )
            _n_valid_print = _n_valid if valid_df is None else len(valid_df)
            _n_test_print = _n_test if test_df is None else len(test_df)
            _el = time.perf_counter() - t0
            step7_duration_sec = _el
            pipeline_echo(
                f"Step 7/11 — done in {_el:.1f}s (train={_n_train_print} valid={_n_valid_print} test={_n_test_print})"
            )
            logger.info(
                "Row-level split (%.0f/%.0f/%.0f) — train: %d  valid: %d  test: %d  (load+sort+split: %.1fs)",
                TRAIN_SPLIT_FRAC * 100,
                VALID_SPLIT_FRAC * 100,
                (1.0 - TRAIN_SPLIT_FRAC - VALID_SPLIT_FRAC) * 100,
                _n_train_print, _n_valid_print, _n_test_print,
                _el,
            )
            if _n_valid_print < MIN_VALID_TEST_ROWS:
                logger.warning(
                    "Validation set has only %d rows (MIN_VALID_TEST_ROWS=%d); "
                    "AP and Optuna results will be unreliable. "
                    "Consider widening the training window (--days or explicit --start/--end).",
                    _n_valid_print, MIN_VALID_TEST_ROWS,
                )
            if _n_test_print < MIN_VALID_TEST_ROWS:
                logger.warning(
                    "Test set has only %d rows (MIN_VALID_TEST_ROWS=%d); "
                    "backtester metrics will be unreliable.",
                    _n_test_print, MIN_VALID_TEST_ROWS,
                )

            pipeline_step_set("Step 8/11")
            # GitHub #17 / #16: after Step 7 row splits, always materialize L2 bundle then disk-backed Steps 8–10.
            _src_snap = read_bridge_source_snapshot_id() or "local_parquet_no_bridge_manifest"
            _raw_out = getattr(args, "l2_auto_bundle_dir", None)
            _bundle_out = Path(_raw_out) if _raw_out else l2_bundle_materialize.default_auto_bundle_dir()
            _ws_m = (
                effective_start.isoformat()
                if hasattr(effective_start, "isoformat")
                else str(effective_start)
            )
            _we_m = (
                effective_end.isoformat()
                if hasattr(effective_end, "isoformat")
                else str(effective_end)
            )
            _cache_key = l2_bundle_materialize.build_auto_l2_cache_key(
                bridge_manifest_stat=l2_bundle_materialize.bridge_manifest_stat_token(),
                window_start_iso=_ws_m,
                window_end_iso=_we_m,
                recent_chunks=recent_chunks,
                train_split_frac=float(TRAIN_SPLIT_FRAC),
                valid_split_frac=float(VALID_SPLIT_FRAC),
                neg_sample_frac_config=float(NEG_SAMPLE_FRAC),
                feature_spec_fingerprint=l2_bundle_materialize.fingerprint_feature_spec(FEATURE_SPEC_PATH),
                rebuild_canonical_mapping=bool(getattr(args, "rebuild_canonical_mapping", False)),
                identity_mapping_mode=str(effective_identity_mode),
                force_recompute=bool(force),
            )
            _bundle_per_fp = None
            if isinstance(feature_spec, dict):
                from trainer.training.feature_materialization import (
                    per_feature_fingerprints as _bundle_pfp_fn,
                )

                _bundle_per_fp = _bundle_pfp_fn(feature_spec)
            l2_bundle_materialize.materialize_l2_training_bundle_dir(
                _bundle_out,
                train_df=train_df,
                valid_df=valid_df,
                test_df=test_df,
                train_path=step7_train_path,
                valid_path=step7_valid_path,
                test_path=step7_test_path,
                source_snapshot_id=_src_snap,
                train_end=train_end,
                window_start=effective_start,
                window_end=effective_end,
                identity_mapping_mode=str(effective_identity_mode),
                train_sampling_applied=float(_effective_neg_sample_frac) < 1.0,
                cache_key=_cache_key,
                per_feature_fingerprints=_bundle_per_fp,
            )
            l2_bundle_materialize.touch_bundle_built_at(_bundle_out)
            pipeline_echo(
                f"Step 8/11 — L2 bundle — materialized to {_bundle_out}; "
                "running Steps 8–10/11 from bundle …"
            )
            pipeline_l2_bundle.execute_l2_training_bundle(
                args=args,
                bundle_dir=_bundle_out,
                pipeline_model_version=pipeline_model_version,
                pipeline_started_at_iso=pipeline_started_at_iso,
                pipeline_start=pipeline_start,
                use_local=use_local,
                skip_optuna=skip_optuna,
                sample_rated_n=sample_rated_n,
                pipeline_ranking_recipe=pipeline_ranking_recipe,
                pipeline_gbm_bakeoff=pipeline_gbm_bakeoff,
            )
            return
        
        except Exception as e:
            log_tags_safe({"status": "FAILED", "error": str(e)[:500]})
            # T12 failure diagnostics (optional follow-on): log best-effort params for post-mortem.
            # Never allow this diagnostics step to change failure behavior.
            try:
                def _iso_or_str(x: Any) -> Optional[str]:
                    # Code Review §6: keep MLflow params bounded to avoid oversized strings.
                    _MAX_CHARS = 200
                    if x is None:
                        return None
                    if hasattr(x, "isoformat"):
                        s = x.isoformat()  # datetime-like
                    else:
                        s = str(x)
                    if len(s) > _MAX_CHARS:
                        return s[:_MAX_CHARS]
                    return s

                chunk_count = len(chunks) if chunks else None
                failure_params = {
                    "training_window_start": _iso_or_str(effective_start),
                    "training_window_end": _iso_or_str(effective_end),
                    "recent_chunks": recent_chunks,
                    "neg_sample_frac": _effective_neg_sample_frac,
                    "chunk_count": chunk_count,
                    "use_local_parquet": bool(use_local),
                    "oom_precheck_est_peak_ram_gb": oom_precheck_est_peak_ram_gb,
                }
                # MLflow expects non-null scalar params; drop None.
                failure_params_clean = {k: v for k, v in failure_params.items() if v is not None}
                if failure_params_clean:
                    log_params_safe(failure_params_clean)
            except Exception:
                pass
            raise


