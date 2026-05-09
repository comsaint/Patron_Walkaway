"""Training pipeline core implementation (Issue #33 Phase B).

Loaded after ``trainer.training.trainer`` defines helpers; uses star-import
for the historical single-namespace scope of ``_run_pipeline_core``.
"""
from __future__ import annotations

from trainer.training.trainer import *  # noqa: F401,F403

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
            duckdb_runtime_track_llm_memory_gb: Optional[float] = None
            duckdb_runtime_track_llm_threads: Optional[int] = None
            # Task 7 DoD: Step 6 chunk cache counters -> pipeline_diagnostics.json
            chunk_cache_stats: Dict[str, int] = {}
            issue16_gate_report: Optional[Dict[str, Any]] = None
            feature_materialization_audit: Optional[Dict[str, Any]] = None

            _l2_bundle_arg = getattr(args, "l2_training_bundle", None)
            if _l2_bundle_arg:
                from trainer.training.pipeline_l2_bundle import execute_l2_training_bundle

                pipeline_step_set("Step 8/11")
                pipeline_echo(
                    "Step 8/11 — L2 bundle (CLI) — Steps 1–7/11 skipped; "
                    "loading bundle then Steps 8–10/11 …"
                )
                execute_l2_training_bundle(
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
                        if _cutoff_naive >= train_end:
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
                                {"cutoff_dtm": _cutoff_iso, "dummy_player_ids": list(dummy_player_ids)},
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
                                {"cutoff_dtm": _cutoff_iso, "dummy_player_ids": list(dummy_player_ids)},
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
            _auto_l2 = (
                use_local
                and not getattr(args, "l2_training_bundle", None)
                and not getattr(args, "no_l2_auto_bundle", False)
            )
            if _auto_l2:
                pipeline_step_set("Step 8/11")
                from trainer.training.l2_bundle_materialize import (
                    auto_bundle_cache_is_current,
                    bridge_manifest_stat_token,
                    build_auto_l2_cache_key,
                    default_auto_bundle_dir,
                    fingerprint_feature_spec,
                )
                from trainer.training.pipeline_l2_bundle import execute_l2_training_bundle

                # Cache hit must not require source_snapshot_id on the bridge manifest;
                # cache key already includes bridge_manifest_stat_token() when present.
                _raw_dir = getattr(args, "l2_auto_bundle_dir", None)
                _bundle_dir_early = Path(_raw_dir) if _raw_dir else default_auto_bundle_dir()
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
                _expected_key = build_auto_l2_cache_key(
                    bridge_manifest_stat=bridge_manifest_stat_token(),
                    window_start_iso=_ws_e,
                    window_end_iso=_we_e,
                    recent_chunks=recent_chunks,
                    train_split_frac=float(TRAIN_SPLIT_FRAC),
                    valid_split_frac=float(VALID_SPLIT_FRAC),
                    neg_sample_frac_config=float(NEG_SAMPLE_FRAC),
                    feature_spec_fingerprint=fingerprint_feature_spec(FEATURE_SPEC_PATH),
                    rebuild_canonical_mapping=bool(getattr(args, "rebuild_canonical_mapping", False)),
                    identity_mapping_mode=str(effective_identity_mode),
                    force_recompute=bool(force),
                )
                if auto_bundle_cache_is_current(bundle_dir=_bundle_dir_early, expected_key=_expected_key):
                    pipeline_echo(
                        f"Step 8/11 — L2 auto-cache — hit at {_bundle_dir_early}; "
                        "skipping Steps 4–10/11 (chunk path), running Steps 8–10/11 from bundle …"
                    )
                    execute_l2_training_bundle(
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
            # 3b. Auto-check local player_profile freshness and backfill missing
            #     ranges before training starts (one-command flow, OOM-safe helper).
            _player_asset_raw = (os.environ.get("TRAINER_PLAYER_LAYER_ASSET_PATH") or "").strip()
            if _player_asset_raw:
                pipeline_echo(
                    "Step 4/11 — Skip ensure_player_profile_ready (TRAINER_PLAYER_LAYER_ASSET_PATH set) …"
                )
                step4_duration_sec = 0.0
                pipeline_echo("Step 4/11 — skipped (player layer asset mode)")
                logger.info(
                    "player_profile ensure skipped: using TRAINER_PLAYER_LAYER_ASSET_PATH=%s",
                    _player_asset_raw,
                )
            else:
                pipeline_echo("Step 4/11 — Ensure player_profile ready (backfill if needed) …")
                t0 = time.perf_counter()
                ensure_player_profile_ready(
                    effective_start,
                    effective_end,
                    use_local_parquet=use_local,
                    canonical_id_whitelist=rated_whitelist,
                    snapshot_interval_days=1,
                    preload_sessions=not no_preload,
                    canonical_map=canonical_map,
                    max_lookback_days=365,
                )
                _el = time.perf_counter() - t0
                step4_duration_sec = _el
                pipeline_echo(f"Step 4/11 — done in {_el:.1f}s")
                logger.info("ensure_player_profile_ready: %.1fs", _el)

            pipeline_step_set("Step 5/11")
            # 3c. Load player_profile once for the entire training window (PLAN Step 4).
            #     Pass the resulting DataFrame to every process_chunk call so each chunk
            #     can do the PIT/as-of join without re-querying.  If load fails, profile
            #     features are 0 for all rows (graceful degradation).
            # R404 Review #1: empty map → [] so load_player_profile does not load full table (train-serve parity with backtester).
            _rated_cids: Optional[List[str]] = (
                list(rated_whitelist)
                if rated_whitelist
                else (
                    canonical_map["canonical_id"].astype(str).tolist()
                    if not canonical_map.empty
                    else []
                )
            )
            if _player_asset_raw:
                pipeline_echo("Step 5/11 — Load player layer asset (TRAINER_PLAYER_LAYER_ASSET_PATH) …")
                t0 = time.perf_counter()
                try:
                    profile_df = load_player_layer_asset_parquet(Path(_player_asset_raw))
                    _el = time.perf_counter() - t0
                    step5_duration_sec = _el
                    pipeline_echo(f"Step 5/11 — done in {_el:.1f}s ({len(profile_df)} asset rows)")
                    logger.info(
                        "player layer asset: loaded %d rows from %s (%.1fs)",
                        len(profile_df),
                        _player_asset_raw,
                        _el,
                    )
                except Exception as exc:
                    profile_df = None
                    _el = time.perf_counter() - t0
                    step5_duration_sec = _el
                    pipeline_echo(
                        f"Step 5/11 — done in {_el:.1f}s "
                        "(asset load failed; profile features will be NaN)"
                    )
                    logger.warning(
                        "player layer asset load failed from %s: %s; "
                        "profile features will be NaN for this run.",
                        _player_asset_raw,
                        exc,
                    )
            else:
                pipeline_echo("Step 5/11 — Load player_profile for PIT join …")
                t0 = time.perf_counter()
                profile_df = load_player_profile(
                    effective_start,
                    effective_end,
                    use_local_parquet=use_local,
                    canonical_ids=_rated_cids,
                )
                _el = time.perf_counter() - t0
                step5_duration_sec = _el
                if profile_df is not None:
                    pipeline_echo(f"Step 5/11 — done in {_el:.1f}s ({len(profile_df)} profile rows)")
                    logger.info("player_profile: loaded %d snapshot rows for PIT join (%.1fs)", len(profile_df), _el)
                else:
                    pipeline_echo(f"Step 5/11 — done in {_el:.1f}s (profile not available)")
                    logger.info("player_profile: not available — profile features will be NaN (%.1fs)", _el)

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
                    _train_for_screen = _read_parquet_head(step7_train_path, _sample_n_disk)
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
            # GitHub #17: materialize L2 bundle from Step 7 outputs, then train via L2 path (skip chunk 8–10).
            _auto_l2_post7 = (
                use_local
                and not getattr(args, "l2_training_bundle", None)
                and not getattr(args, "no_l2_auto_bundle", False)
            )
            if _auto_l2_post7:
                from trainer.training.l2_bundle_materialize import (
                    build_auto_l2_cache_key,
                    bridge_manifest_stat_token,
                    default_auto_bundle_dir,
                    fingerprint_feature_spec,
                    materialize_l2_training_bundle_dir,
                    touch_bundle_built_at,
                )
                from trainer.training.pipeline_l2_bundle import execute_l2_training_bundle

                _src_snap = read_bridge_source_snapshot_id() or "local_parquet_no_bridge_manifest"
                _raw_out = getattr(args, "l2_auto_bundle_dir", None)
                _bundle_out = Path(_raw_out) if _raw_out else default_auto_bundle_dir()
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
                _cache_key = build_auto_l2_cache_key(
                    bridge_manifest_stat=bridge_manifest_stat_token(),
                    window_start_iso=_ws_m,
                    window_end_iso=_we_m,
                    recent_chunks=recent_chunks,
                    train_split_frac=float(TRAIN_SPLIT_FRAC),
                    valid_split_frac=float(VALID_SPLIT_FRAC),
                    neg_sample_frac_config=float(NEG_SAMPLE_FRAC),
                    feature_spec_fingerprint=fingerprint_feature_spec(FEATURE_SPEC_PATH),
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
                materialize_l2_training_bundle_dir(
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
                touch_bundle_built_at(_bundle_out)
                pipeline_echo(
                    f"Step 8/11 — L2 bundle — materialized to {_bundle_out}; "
                    "running Steps 8–10/11 from bundle …"
                )
                execute_l2_training_bundle(
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
        
            active_feature_cols = get_all_candidate_feature_ids(feature_spec, screening_only=True)
        
            # 5b. Full-feature screening (DEC-020).
            # Runs on the TRAINING SET ONLY to comply with TRN-09 anti-leakage rules.
            #
            # Candidate set = active_feature_cols (Track Human + Legacy + Profile) PLUS
            # Track LLM candidate columns declared in feature spec and present in train_df.
            if feature_spec is not None:
                _track_llm_cols = [
                    cand.get("feature_id")
                    for cand in resolve_spec_track_section(feature_spec, "bet_duckdb_window").get(
                        "candidates", []
                    )
                    if cand.get("feature_id") in _train_cols
                ]
                if _track_llm_cols:
                    logger.info(
                        "screen_features: loaded %d bet_duckdb_window candidate columns from feature spec",
                        len(_track_llm_cols),
                    )
                _all_candidate_cols: List[str] = list(dict.fromkeys(active_feature_cols + _track_llm_cols))
            else:
                _all_candidate_cols = active_feature_cols
        
            # Only screen columns that actually exist in train (or train sample when B+ on disk).
            _present_candidate_cols = [c for c in _all_candidate_cols if c in _train_cols]
            if not _present_candidate_cols:
                _t8_skip = time.perf_counter()
                logger.warning(
                    "screen_features: no candidate columns found in train_df — skipping screening"
                )
                # R1004: restrict active_feature_cols to columns actually present in train.
                active_feature_cols = [c for c in active_feature_cols if c in _train_cols]
                step8_duration_sec = time.perf_counter() - _t8_skip
                pipeline_echo(
                    f"Step 8/11 — Feature screening skipped (no candidates in train) — "
                    f"done in {step8_duration_sec:.1f}s"
                )
            else:
                # PLAN 方案 B 策略 A / B+ Stage 2: use sample from memory or from file (_train_for_screen from _read_parquet_head when on disk).
                # Step 8 DuckDB std (PLAN): pass train_path or train_df so zv is computed on full data via DuckDB; keep _matrix_for_screen as sample to avoid OOM in corr/MI/LGBM.
                _cap = (
                    int(STEP8_SCREEN_SAMPLE_ROWS)
                    if (STEP8_SCREEN_SAMPLE_ROWS is not None and STEP8_SCREEN_SAMPLE_ROWS >= 1)
                    else 2_000_000
                )
                _screen_train_df: Optional[pd.DataFrame] = None
                if train_df is not None:
                    _sample_n = (
                        STEP8_SCREEN_SAMPLE_ROWS
                        if (STEP8_SCREEN_SAMPLE_ROWS is not None and STEP8_SCREEN_SAMPLE_ROWS >= 1)
                        else None
                    )
                    if _sample_n is not None:
                        _sample_n = int(_sample_n)
                    _matrix_for_screen = _step8_sample_in_memory_train(
                        train_df,
                        strategy=step8_screen_sample_strategy,
                        sample_n=_sample_n,
                        default_cap=_cap,
                    )
                    _screen_train_df = _matrix_for_screen
                    _lbl_ratio = (
                        float(_matrix_for_screen["label"].mean())
                        if "label" in _matrix_for_screen.columns and len(_matrix_for_screen)
                        else None
                    )
                    _pmin, _pmax = _payout_bounds_iso_from_series(
                        _matrix_for_screen["payout_complete_dtm"]
                        if "payout_complete_dtm" in _matrix_for_screen.columns
                        else pd.Series(dtype="datetime64[ns]")
                    )
                    _req_cap_im = (
                        int(_sample_n)
                        if _sample_n is not None and int(_sample_n) >= 1
                        else int(_cap)
                    )
                    logger.info(
                        "Step 8 screening: strategy=%s sample_rows=%d full_train_rows=%d "
                        "requested_cap=%d STEP8_SCREEN_SAMPLE_ROWS=%s label_mean=%s payout_span=[%s,%s]",
                        step8_screen_sample_strategy,
                        len(_matrix_for_screen),
                        len(train_df),
                        _req_cap_im,
                        str(STEP8_SCREEN_SAMPLE_ROWS),
                        f"{_lbl_ratio:.4f}" if _lbl_ratio is not None else "n/a",
                        _pmin or "n/a",
                        _pmax or "n/a",
                    )
                else:
                    _matrix_for_screen = _train_for_screen
                    _lbl_ratio_disk = (
                        float(_matrix_for_screen["label"].mean())
                        if "label" in _matrix_for_screen.columns and len(_matrix_for_screen)
                        else None
                    )
                    _pmin_d, _pmax_d = _payout_bounds_iso_from_series(
                        _matrix_for_screen["payout_complete_dtm"]
                        if "payout_complete_dtm" in _matrix_for_screen.columns
                        else pd.Series(dtype="datetime64[ns]")
                    )
                    logger.info(
                        "Step 8 screening: strategy=%s from train file (STEP7_KEEP_TRAIN_ON_DISK) "
                        "sample_rows=%d full_train_rows=%d requested_cap=%d STEP8_SCREEN_SAMPLE_ROWS=%s "
                        "label_mean=%s payout_span=[%s,%s]",
                        step8_screen_sample_strategy,
                        len(_matrix_for_screen),
                        _n_train_print,
                        int(_sample_n_disk),
                        str(STEP8_SCREEN_SAMPLE_ROWS),
                        f"{_lbl_ratio_disk:.4f}" if _lbl_ratio_disk is not None else "n/a",
                        _pmin_d or "n/a",
                        _pmax_d or "n/a",
                    )
                step8_screening_source = (
                    f"in_memory_{step8_screen_sample_strategy}"
                    if train_df is not None
                    else f"train_file_{step8_screen_sample_strategy}"
                )
                step8_screening_stats_source = (
                    "screening_sample_df" if train_df is not None else "train_path"
                )
                step8_screening_sample_rows = len(_matrix_for_screen)
                step8_screening_full_train_rows = (
                    len(train_df) if train_df is not None else _n_train_print
                )
                step8_screening_candidate_cols = len(_present_candidate_cols)
                try:
                    import psutil as _psutil

                    _avail_screen = int(_psutil.virtual_memory().available)
                except Exception:
                    _avail_screen = None
                _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
                if callable(_resolve_runtime):
                    _screen_input = int(_matrix_for_screen.memory_usage(deep=True).sum())
                    _screen_policy = _resolve_runtime(
                        "screening",
                        _avail_screen,
                        input_bytes=_screen_input,
                    )
                    duckdb_runtime_screening_memory_gb = (
                        float(_screen_policy["memory_limit_bytes"]) / 1024**3
                    )
                    duckdb_runtime_screening_threads = int(_screen_policy["threads"])
                step8_screened_feature_count = None
                pipeline_echo("Step 8/11 — Feature screening …")
                t0 = time.perf_counter()
                screened_cols = screen_features(
                    feature_matrix=_matrix_for_screen,
                    labels=_matrix_for_screen["label"],
                    feature_names=_present_candidate_cols,
                    screen_method=SCREEN_FEATURES_METHOD,
                    train_path=step7_train_path if step7_train_path is not None else None,
                    train_df=_screen_train_df,
                )
                _el = time.perf_counter() - t0
                step8_duration_sec = _el
                step8_screened_feature_count = len(screened_cols)
                pipeline_echo(
                    f"Step 8/11 — done in {_el:.1f}s ({len(_present_candidate_cols)} → {len(screened_cols)} features)"
                )
                logger.info(
                    "screen_features: %d -> %d features retained  (%.1fs)",
                    len(_present_candidate_cols), len(screened_cols), _el,
                )
                # R1001: post-screening sanity — ensure at least one Track Human feature survives.
                # Use YAML feature_spec (SSOT) instead of hardcoded list (feat-consolidation R123-2).
                _screened_set = set(screened_cols)
                _yaml_track_human = (
                    set(get_candidate_feature_ids(feature_spec, "track_human", screening_only=True))
                    if feature_spec is not None
                    else set()
                )
                if _yaml_track_human and not _screened_set.intersection(_yaml_track_human):
                    _missing_track_human = [c for c in _yaml_track_human if c in _train_cols]
                    if _missing_track_human:
                        logger.warning(
                            "screen_features: no track_human features survived screening — "
                            "re-appending %d track_human features as fallback (R1001)",
                            len(_missing_track_human),
                        )
                        screened_cols = screened_cols + [
                            c for c in _missing_track_human if c not in _screened_set
                        ]
                active_feature_cols = screened_cols
        
            # PLAN B+ Stage 2: load train from file after screening so export/Step 9 have train_df.
            if step7_train_path is not None:
                if _train_for_screen is not None:
                    _train_for_screen = None
                    if "_matrix_for_screen" in locals():
                        del _matrix_for_screen
                    gc.collect()
                if not STEP9_EXPORT_LIBSVM:
                    raise RuntimeError(
                        "STEP9_EXPORT_LIBSVM=False is incompatible with LibSVM-only training."
                    )
                assert step7_valid_path is not None and step7_test_path is not None  # R202 guard
                _train_libsvm, _valid_libsvm, _test_libsvm = _export_parquet_to_libsvm(
                    step7_train_path,
                    step7_valid_path,
                    active_feature_cols,
                    DATA_DIR / "export",
                    test_path=step7_test_path,
                )
                train_df = pd.read_parquet(step7_train_path)
                if step7_train_path.exists():
                    step7_train_path.unlink(missing_ok=True)
                logger.info(
                    "Step 7 B+: loaded train from file after screening (%d rows)%s",
                    len(train_df),
                    "; valid/test left on disk (B+ 階段 6 第 2 步)" if (valid_df is None and test_df is None) else "",
                )
        
            if not active_feature_cols:
                # R1613: explicit guardrail message for zero-feature situations.  In
                # integration / debug contexts (e.g. heavily mocked tests) we still
                # want the pipeline to run so that wiring between stages can be
                # exercised, so we fall back to a single constant "bias" feature
                # instead of terminating the process.
                msg = (
                    "screen_features + Track Human fallback both returned empty feature list. "
                    "Cannot train any model. Check data quality and feature definitions."
                )
                logger.warning(msg)
                pipeline_echo(
                    "Step 8/11 — warning: no usable features after screening; using placeholder 'bias' (see logs)."
                )
                _placeholder_col = "bias"  # constant feature for integration/debug runs (R1605: named via explicit variable)
                if train_df is not None and _placeholder_col not in train_df.columns:
                    train_df[_placeholder_col] = 0.0
                if valid_df is not None and not valid_df.empty and _placeholder_col not in valid_df.columns:
                    valid_df[_placeholder_col] = 0.0
                if test_df is not None and not test_df.empty and _placeholder_col not in test_df.columns:
                    test_df[_placeholder_col] = 0.0
                active_feature_cols = [_placeholder_col]
        
            pipeline_step_set("Step 9/11")
            if not STEP9_EXPORT_LIBSVM:
                raise RuntimeError(
                    "STEP9_EXPORT_LIBSVM=False is incompatible with LibSVM-only training."
                )
            remove_legacy_plan_b_csv_exports(DATA_DIR / "export")
            if _train_libsvm is None or _valid_libsvm is None:
                if train_df is None or valid_df is None or test_df is None:
                    raise RuntimeError(
                        "LibSVM-only: missing in-memory splits for export "
                        "(train_df/valid_df/test_df required when step7 parquet paths unset)."
                    )
                _export_root = DATA_DIR / "export"
                _tmp_sp = _export_root / "_tmp_splits_for_libsvm"
                if _tmp_sp.exists():
                    shutil.rmtree(_tmp_sp, ignore_errors=True)
                _tmp_sp.mkdir(parents=True, exist_ok=True)
                _tp = _tmp_sp / "train.parquet"
                _vp = _tmp_sp / "valid.parquet"
                _tsp = _tmp_sp / "test.parquet"
                train_df.to_parquet(_tp, index=False)
                valid_df.to_parquet(_vp, index=False)
                test_df.to_parquet(_tsp, index=False)
                _train_libsvm, _valid_libsvm, _test_libsvm = _export_parquet_to_libsvm(
                    _tp,
                    _vp,
                    active_feature_cols,
                    _export_root,
                    test_path=_tsp,
                )
                shutil.rmtree(_tmp_sp, ignore_errors=True)
            if _train_libsvm is None or _valid_libsvm is None:
                raise RuntimeError("LibSVM-only: export did not produce train/valid LibSVM paths.")

            # 6. Train dual model (Optuna + run-level sample_weight, DEC-013)
            #    test_df is passed so test-set metrics and feature importance are
            #    computed immediately after training and included in the artifact.
            pipeline_echo("Step 9/11 — Train rated GBM family + test-set eval …")
            t0 = time.perf_counter()
            model_version = pipeline_model_version
            _libsvm_paths = (_train_libsvm, _valid_libsvm)
            rated_art, _, combined_metrics = train_single_rated_model(
                train_df,
                valid_df,
                active_feature_cols,
                run_optuna=not skip_optuna,
                test_df=test_df,
                train_libsvm_paths=_libsvm_paths,
                test_libsvm_path=_test_libsvm,
                ranking_recipe=pipeline_ranking_recipe,
                gbm_bakeoff=pipeline_gbm_bakeoff,
                valid_split_parquet_path=step7_valid_path,
                test_split_parquet_path=step7_test_path,
                train_split_parquet_path=step7_train_path,
            )
            _el = time.perf_counter() - t0
            step9_duration_sec = _el
            pipeline_echo(f"Step 9/11 — done in {_el:.1f}s")
            logger.info("train_single_rated_model + A3 family compare + test eval: %.1fs", _el)

            # T12.2: capture RSS/sys RAM snapshot at Step 9 end (checkpoint scope Step 7-9).
            # Peak := max(start, end) to avoid heavy sampling/polling overhead.
            if step7_rss_start_gb is not None:
                try:
                    import psutil as _psutil

                    _proc_end = _psutil.Process()
                    step7_rss_end_gb = _proc_end.memory_info().rss / (1024**3)
                    if step7_rss_end_gb is not None:
                        step7_rss_peak_gb = max(step7_rss_start_gb, step7_rss_end_gb)

                    _vm_end = _psutil.virtual_memory()
                    if _step7_sys_available_start_gb is not None:
                        _vm_end_avail_gb = _vm_end.available / (1024**3)
                        step7_sys_available_min_gb = min(_step7_sys_available_start_gb, _vm_end_avail_gb)
                    if _step7_sys_used_percent_start is not None:
                        _vm_end_used_percent = float(_vm_end.percent)
                        step7_sys_used_percent_peak = max(_step7_sys_used_percent_start, _vm_end_used_percent)
                except Exception:
                    # If memory sampling fails, just keep metrics unset; never impact training.
                    pass

            # Step 9 no longer needs the in-memory split frames after training returns.
            # Release them before artifact / MLflow phases so large train/valid/test
            # DataFrames do not stay resident through the rest of the pipeline.
            train_df = None
            valid_df = None
            test_df = None
            gc.collect()

            pipeline_step_set("Step 10/11")
            # 7. Save artifacts (versioned subdir under MODEL_DIR; see Priority 1 investigation plan).
            pipeline_echo("Step 10/11 — Save artifact bundle …")
            t0 = time.perf_counter()
            _versions_root = MODEL_DIR
            _bundle_dir = safe_version_subdirectory(_versions_root, model_version)
            if _bundle_dir.exists() and (_bundle_dir / "model.pkl").exists():
                raise FileExistsError(
                    f"Refusing to overwrite existing model bundle: {_bundle_dir}. "
                    "Remove the directory or wait for a new model_version timestamp."
                )
            _bundle_dir.mkdir(parents=True, exist_ok=True)
            _baseline_align = _make_baseline_training_alignment_payload(
                effective_start,
                effective_end,
                float(TRAIN_SPLIT_FRAC),
                float(VALID_SPLIT_FRAC),
            )
            _split_mlflow_meta = split_row_metadata_to_mlflow_string_params(_split_row_meta)
            _model_meta_doc = build_model_metadata_document(
                model_version=model_version,
                effective_start=effective_start,
                effective_end=effective_end,
                splits=_split_row_meta,
                use_local_parquet=use_local,
                recent_chunks=None,
                sample_rated_n=sample_rated_n,
                skip_optuna=skip_optuna,
                neg_sample_frac_effective=_effective_neg_sample_frac,
                bundle_dir=_bundle_dir,
                combined_metrics=combined_metrics,
                model_used_splits=_model_used_split_meta,
                identity_mapping_mode=effective_identity_mode,
                t_game_features_enabled=bool(getattr(_cfg, "T_GAME_FEATURES_ENABLED", False)),
                t_game_visible_time_column=(
                    "__etl_insert_Dtm" if bool(getattr(_cfg, "T_GAME_FEATURES_ENABLED", False)) else "none"
                ),
            )
            save_artifact_bundle(
                rated_art, active_feature_cols, combined_metrics, model_version,
                sample_rated_n=sample_rated_n,
                feature_spec_path=FEATURE_SPEC_PATH,
                neg_sample_frac=_effective_neg_sample_frac,
                bundle_dir=_bundle_dir,
                baseline_training_alignment=_baseline_align,
                model_metadata=_model_meta_doc,
            )
            try:
                write_latest_model_manifest(_versions_root, model_version, _bundle_dir)
            except Exception as _man_exc:
                logger.warning(
                    "Failed to write latest model manifest (artifacts saved): %s",
                    _man_exc,
                )
            _el = time.perf_counter() - t0
            step10_duration_sec = _el
            pipeline_echo(f"Step 10/11 — done in {_el:.1f}s")
            logger.info("save_artifact_bundle: %.1fs", _el)

            # T13: Warm up MLflow (e.g. Cloud Run) before first log to reduce 503 on cold start.
            if has_active_run():
                warm_up_mlflow_run_safe()

            # Phase 2 T2: Log provenance to MLflow (no-op when URI unset/unreachable).
            try:
                _log_training_provenance_to_mlflow(
                    model_version=model_version,
                    artifact_dir=str(_bundle_dir),
                    training_window_start=effective_start,
                    training_window_end=effective_end,
                    feature_spec_path=str(FEATURE_SPEC_PATH),
                    training_metrics_path=str(_bundle_dir / "training_metrics.json"),
                    pipeline_diagnostics_path=str(_bundle_dir / "pipeline_diagnostics.json"),
                    pipeline_diagnostics_rel_path=f"{_bundle_dir.name}/pipeline_diagnostics.json",
                    model_metadata_path=str(_bundle_dir / "model_metadata.json"),
                    model_metadata_rel_path=f"{_bundle_dir.name}/model_metadata.json",
                    split_boundary_params=_split_mlflow_meta,
                )
            except Exception as e:
                logger.warning("MLflow provenance logging failed (training still succeeded): %s", e)
        
            # Remove stale dual-model and legacy pickles so operators do not assume
            # they are loadable (DEC-040: only model.pkl is read).
            for _stale in ["nonrated_model.pkl", "rated_model.pkl", "walkaway_model.pkl"]:
                _stale_path = _versions_root / _stale
                if _stale_path.exists():
                    _stale_path.unlink()
                    logger.info("Removed stale artifact: %s", _stale)
        
            total_sec = time.perf_counter() - pipeline_start
            _pipeline_finished_at_iso = datetime.now(timezone.utc).isoformat()
            if (
                oom_precheck_est_peak_ram_gb is not None
                and oom_precheck_est_peak_ram_gb > 0
                and step7_rss_peak_gb is not None
            ):
                oom_precheck_step7_rss_error_ratio = (
                    step7_rss_peak_gb / oom_precheck_est_peak_ram_gb
                )
            try:
                _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
                if callable(_resolve_runtime):
                    try:
                        import psutil as _psutil

                        _avail_track = int(_psutil.virtual_memory().available)
                    except Exception:
                        _avail_track = None
                    _track_policy = _resolve_runtime(
                        "bet_duckdb_window", _avail_track, input_bytes=None
                    )
                    duckdb_runtime_track_llm_memory_gb = (
                        float(_track_policy["memory_limit_bytes"]) / 1024**3
                    )
                    duckdb_runtime_track_llm_threads = int(_track_policy["threads"])
                _write_pipeline_diagnostics_json(
                    model_version=model_version,
                    pipeline_started_at=pipeline_started_at_iso,
                    pipeline_finished_at=_pipeline_finished_at_iso,
                    total_duration_sec=total_sec,
                    step0_duration_sec=step0_duration_sec,
                    step1_duration_sec=step1_duration_sec,
                    step2_duration_sec=step2_duration_sec,
                    step3_duration_sec=step3_duration_sec,
                    step4_duration_sec=step4_duration_sec,
                    step5_duration_sec=step5_duration_sec,
                    step6_duration_sec=step6_duration_sec,
                    step7_duration_sec=step7_duration_sec,
                    step7b_duration_sec=step7b_duration_sec,
                    step8_duration_sec=step8_duration_sec,
                    step9_duration_sec=step9_duration_sec,
                    step10_duration_sec=step10_duration_sec,
                    oom_precheck_est_peak_ram_gb=oom_precheck_est_peak_ram_gb,
                    oom_precheck_step7_rss_error_ratio=oom_precheck_step7_rss_error_ratio,
                    step7_rss_start_gb=step7_rss_start_gb,
                    step7_rss_peak_gb=step7_rss_peak_gb,
                    step7_rss_end_gb=step7_rss_end_gb,
                    step7_sys_available_min_gb=step7_sys_available_min_gb,
                    step7_sys_used_percent_peak=step7_sys_used_percent_peak,
                    step7_chunk_parquet_total_bytes=step7_chunk_parquet_total_bytes,
                    step7_chunk_parquet_est_ram_gb=step7_chunk_parquet_est_ram_gb,
                    step8_screening_source=step8_screening_source,
                    step8_screening_stats_source=step8_screening_stats_source,
                    step8_screening_sample_rows=step8_screening_sample_rows,
                    step8_screening_full_train_rows=step8_screening_full_train_rows,
                    step8_screening_candidate_cols=step8_screening_candidate_cols,
                    step8_screened_feature_count=step8_screened_feature_count,
                    step8_screen_sample_strategy=step8_screen_sample_strategy,
                    duckdb_runtime_step7_memory_gb=duckdb_runtime_step7_memory_gb,
                    duckdb_runtime_step7_threads=duckdb_runtime_step7_threads,
                    duckdb_runtime_screening_memory_gb=duckdb_runtime_screening_memory_gb,
                    duckdb_runtime_screening_threads=duckdb_runtime_screening_threads,
                    duckdb_runtime_track_llm_memory_gb=duckdb_runtime_track_llm_memory_gb,
                    duckdb_runtime_track_llm_threads=duckdb_runtime_track_llm_threads,
                    chunk_cache_stats=chunk_cache_stats,
                    issue16_audit=issue16_gate_report,
                    output_dir=_bundle_dir,
                    feature_materialization_audit=feature_materialization_audit,
                )
            except Exception as _diag_exc:
                logger.warning(
                    "pipeline_diagnostics.json write failed (training still succeeded): %s",
                    _diag_exc,
                )

            # Phase 2 / pipeline plan: small-file artifacts for MLflow UI (best-effort; no active run → no-op).
            # P1.5: full bundle under model_bundle/ (log_artifacts_safe) + SHA-256 params; keeps bundle/ copies below.
            if has_active_run():
                _checksum_params: Dict[str, str] = {}
                _mpath = _bundle_dir / "model.pkl"
                if _mpath.is_file():
                    try:
                        _checksum_params["model_pkl_sha256"] = _sha256_file_hex(_mpath)
                    except Exception as _h_exc:
                        logger.warning("model.pkl checksum failed (MLflow param skipped): %s", _h_exc)
                if FEATURE_SPEC_PATH.is_file():
                    try:
                        _checksum_params["feature_spec_sha256"] = _sha256_file_hex(FEATURE_SPEC_PATH)
                    except Exception as _h_exc:
                        logger.warning("feature_spec checksum failed (MLflow param skipped): %s", _h_exc)
                if _checksum_params:
                    try:
                        log_params_safe(_checksum_params)
                    except Exception as _p_exc:
                        logger.warning("MLflow checksum params failed (training still succeeded): %s", _p_exc)
                # P1.5: full bundle (includes model.pkl); transient retries in helper.
                log_artifacts_safe(
                    _bundle_dir, artifact_path=MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH
                )
                if _mpath.is_file():
                    _rel_model = f"{MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH}/model.pkl"
                    log_tags_safe({"trained_model_artifact": _rel_model})
                    logger.info(
                        "MLflow: trained model uploaded with bundle at artifact %r "
                        "(download model.pkl from this path in the run).",
                        _rel_model,
                    )
                    pipeline_echo(
                        f"Step 10/11 — MLflow — model artifact {_rel_model} "
                        f"(bundle under {MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH}/)"
                    )
                # Legacy UI path: small files under bundle/ (contract tests + existing dashboards).
                _bundle_artifact_path = "bundle"
                for _fname in (
                    "training_metrics.json",
                    "pipeline_diagnostics.json",
                    "model_metadata.json",
                    "feature_spec.yaml",
                    "model_version",
                ):
                    _ap = _bundle_dir / _fname
                    if _ap.is_file():
                        log_artifact_safe(_ap, artifact_path=_bundle_artifact_path)

            pipeline_echo(
                f"Complete — 11 steps (0–10) finished in {total_sec:.1f}s ({total_sec / 60.0:.1f} min)"
            )
            logger.info("Pipeline total: %.1fs (%.1f min)", total_sec, total_sec / 60.0)

            # T12.2: Log training success metrics + per-step durations + Step 7–9 memory/OOM diagnostics to MLflow.
            try:
                oom_params = {
                    "oom_precheck_est_peak_ram_gb": oom_precheck_est_peak_ram_gb,
                    "oom_precheck_step7_rss_error_ratio": oom_precheck_step7_rss_error_ratio,
                }
                # Avoid logging None values (MLflow params do not accept nulls well).
                oom_params_clean = {k: v for k, v in oom_params.items() if v is not None}
                if oom_params_clean:
                    log_params_safe(oom_params_clean)

                log_params_safe(
                    {
                        "trainer_device_mode_requested": _REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS,
                        "trainer_device_mode_effective": (
                            "gpu"
                            if (
                                str(_EFFECTIVE_LIGHTGBM_DEVICE).lower() == "gpu"
                                or str(_LAST_GBM_BACKEND_EFFECTIVE_DEVICE).lower() == "gpu"
                            )
                            else "cpu"
                        ),
                        "gpu_fallback_used": str(
                            bool(_LIGHTGBM_GPU_FALLBACK_USED or _GBM_BACKEND_GPU_FALLBACK_USED)
                        ),
                        "lightgbm_device_requested": _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS,
                        "lightgbm_device_effective": _EFFECTIVE_LIGHTGBM_DEVICE,
                        "lightgbm_device_fallback": str(bool(_LIGHTGBM_GPU_FALLBACK_USED)),
                    }
                )

                # Training metrics from artifact, then pipeline timing + memory/OOM last so
                # combined_metrics["rated"] cannot overwrite reserved keys (vs pipeline_diagnostics.json).
                mlflow_metrics: dict[str, Any] = {}
                _rated = (combined_metrics or {}).get("rated", {})
                if isinstance(_rated, dict):
                    mlflow_metrics.update(_rated)

                mlflow_metrics.update(
                    {
                        "total_duration_sec": total_sec,
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
                        # Step 7-9 checkpoint memory metrics (names align with plan).
                        "step7_rss_start_gb": step7_rss_start_gb,
                        "step7_rss_peak_gb": step7_rss_peak_gb,
                        "step7_rss_end_gb": step7_rss_end_gb,
                        "step7_sys_available_min_gb": step7_sys_available_min_gb,
                        "step7_sys_used_percent_peak": step7_sys_used_percent_peak,
                        # Keep this also as a metric for easier plotting.
                        "oom_precheck_step7_rss_error_ratio": oom_precheck_step7_rss_error_ratio,
                    }
                )

                log_metrics_safe(mlflow_metrics)
            except Exception as _mlflow_exc:
                logger.warning("MLflow success diagnostics logging failed: %s", _mlflow_exc)
        
            summary = {
                "model_version": model_version,
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "total_rows": n_rows,
                "metrics": combined_metrics,
            }
            logger.debug("Training summary JSON: %s", json.dumps(summary, default=str))
            pipeline_echo(
                f"Pipeline — Summary — model_version={model_version} total_rows={n_rows} "
                "(full JSON: logger DEBUG or TRAINER_SUMMARY_JSON_STDOUT=1)"
            )
            if os.environ.get("TRAINER_SUMMARY_JSON_STDOUT", "").strip().lower() in (
                "1",
                "true",
                "yes",
            ):
                print(json.dumps(summary, indent=2, default=str), flush=True)
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


