Training flow (Trainer) — RunTrip single-path contract
========================================================

Inputs
------
1. User supplies raw ``t_bet`` and ``t_session`` Parquet (L0). Paths MUST be portable:
   repo-relative or relative to the local bridge manifest directory (no machine-bound
   absolute paths in shipped manifests).

2. First local run: if ``trainer_local_parquet_bridge.manifest.json`` is missing under
   ``LOCAL_PARQUET_DIR``, the trainer preflight **auto-materializes** the bridge
   (snapshot emit or full MVP subprocess per Issue #14). User may disable full MVP
   autobuild via ``TRAINER_AUTOBUILD_FULL_MVP``.

3. User runs one training command. Default uses the full rated window available
   (no sampling) unless ``--sample-rated N`` is set.

Pipeline (single window; no legacy monthly chunks)
---------------------------------------------------
4. Preprocess ``t_session`` → cleaned session table.

5. Canonical ID mapping from cleaned ``t_session``.

6. Preprocess ``t_bet`` with canonical mapping; filter unrated patrons → cleaned bet table.

7. Build run/trip (and related) data assets from cleaned bets (bridge contract).

8. **Dev-only feature catalog**: load ``trainer/feature_spec/feature_candidates.yaml``
   (candidate universe + screening flags + PIT metadata). This file is **not** shipped
   as the runtime spec.

9. Compute features and assemble the training matrix; enforce PIT / cutoff rules.

10. Time-split into train / valid / test (no future leakage).

11. Negative sampling, if enabled, applies **only** to the training split — never to
    valid or test.

12. Train (with optional HPO / selection / ensembling) on train+valid; evaluate on test;
    log metrics next to the model bundle and to MLflow.

13. On success, write **bundle-only** ``feature_spec.yaml`` next to ``model.pkl``:
    contains **only** the features the trained model consumes, plus any **dependency
    closure** (derived ``depends_on`` intermediates required to compute those columns).
    Hash / fingerprint this frozen file for train–serve parity.

Production flow (Scorer)
========================
1. Data source: production ClickHouse (contract unchanged).

2. Ship **model bundle** (``model.pkl``, ``feature_list.json``, **frozen**
   ``feature_spec.yaml``, …) to production. **Do not** rely on repo
   ``package/deploy/models/feature_spec.yaml`` as a runtime authority.

3. Align shipped data assets with ClickHouse (freshness contract).

4. Poll and refresh scoring windows continuously.

5. Score bets; log predictions. Scorer loads **only** the bundle ``feature_spec.yaml``.
   If this file is missing in the bundle directory, runtime must **fail fast**
   (do not fallback to repo/deploy template specs).

6. Emit alerts when prediction == 1.

Validator (production ground truth)
===================================
- Validator **does not** load feature YAML. It validates alert outcomes against
  authoritative labels / DB ground truth only.

Notes
=====
- **Portable manifests**: trainer ingress rejects absolute paths outside ``PROJECT_ROOT``
  when ``TRAINER_MANIFEST_PATH_STRICT`` is unset or ``1`` (default). Emit-time
  ``parallel_lda_mvp`` bridge paths must be expressible relative to the chosen anchor
  (otherwise emit raises). Legacy hosts may set ``TRAINER_MANIFEST_PATH_STRICT=0``.
- **Final train+valid refit (§12)**: the main pipeline trains Rated LightGBM from **LibSVM files**
  under ``DATA_DIR/export`` (``STEP9_EXPORT_LIBSVM`` must be ``True``); legacy Plan B CSV exports
  are removed on each run. In-memory LightGBM (unit tests / callers without LibSVM paths) still
  refits on ``train ∪ valid`` after selection when applicable.
  LibSVM-on-disk **without** ``TRAINER_FILE_BACKED_STRICT`` skips refit (see
  ``training_metrics`` → ``final_refit_train_valid``).
  When ``TRAINER_FILE_BACKED_STRICT`` is ``1``/``true``/``yes``/``on`` (issue #25),
  the rated LibSVM path performs a **file-backed** refit by merging train and valid
  LibSVM (and weight lines) before a second ``lgb.train`` with ``num_boost_round=best_iteration``.
  Under the same strict flag, **A3** optional backends (CatBoost / XGBoost) use **LibSVM-disk Optuna**
  when a bakeoff LibSVM bundle is present, then a **train∪valid disk refit** (metrics:
  ``final_refit_train_valid``, ``final_refit_data_source``, ``final_refit_backend``).
  With strict + LibSVM bundle, **Phase E** treats streaming as **on** for val/train/test scores
  even if ``GBM_BAKEOFF_PREDICT_STREAMING`` is unset, and dense-matrix fallbacks for those scores
  are **rejected** (fail-fast).
  A3 CatBoost/XGBoost disk training **does not** fall back to in-memory fit in strict mode
  (disk failure is fatal). Set ``TRAINER_DISABLE_FINAL_REFIT_TRAIN_VALID=1`` to skip refit for debugging only.
  In strict mode, missing train/valid LibSVM, empty train LibSVM, or single-class train LibSVM
  **must not** silently fall back to in-memory training when LibSVM paths were requested.
- **A3 bakeoff observability / fair HPO**: ``GBM_BAKEOFF_SYMMETRIC_HPO=1`` disables dividing the
  global Optuna timeout by ``OPTUNA_ACTIVE_MODEL_COUNT_FOR_TOTAL_TIMEOUT_SPLIT`` so each optional
  backend (CatBoost/XGBoost) gets the full configured wall budget. Training metrics and
  ``gbm_bakeoff`` report may include ``lgb_train_dataset_bin_cache_hit``,
  ``a3_catboost_libsvm_cache_hit_{train,valid,test}``, ``a3_xgboost_external_memory_train``,
  ``a3_val_scores_data_source``, ``a3_train_metrics_data_source``, and ``optuna_hpo_data_source``
  (e.g. ``libsvm_disk`` vs ``in_memory_dense``).
- **A3 Phase E（#31）— 分批 LibSVM predict / memmap 分數**：可選降低 bakeoff 評估峰值 RAM。
  - ``GBM_BAKEOFF_PREDICT_STREAMING=1``：對 A3 disk 路徑的 val/train/test 以 **分批 LibSVM 行** 推論（取代整檔單次 ``DMatrix``/``Pool``）。
  - ``GBM_BAKEOFF_SCORE_MEMMAP=1``：串流分數寫入 bakeoff ``cache_dir`` 下 **float32 memmap**（路徑見 metrics ``a3_score_memmap_path``）。
  - ``GBM_BAKEOFF_PREDICT_BATCH_ROWS``：每批行數（有上下限）；CLI 對應 ``--gbm-bakeoff-predict-batch-rows N``。
  - ``GBM_BAKEOFF_AP_MODE``：``legacy``（sklearn AP）、``approx_histogram``（分箱近似 AP）、``exact_external_sort``（與 legacy 同 sklearn AP，名稱保留供對照）；CLI ``--gbm-bakeoff-ap-mode``。
  - ``TRAINER_FILE_BACKED_STRICT=1`` 且（啟用 Phase E 串流 **或** 已具 LibSVM bundle）時，串流 predict **失敗則 fail-fast**（不 silent 回退）。

A. Tables and the **dev** feature catalog change often; incremental rebuilds should
   touch only invalidated partitions / fingerprints (bridge + materialization cache keys).

B. Prefer incremental materialization; any materialization cache key MUST include
   spec fingerprint, window, and ingress manifest fingerprint.

C. Run/trip entities that can be open vs closed MUST carry explicit state in assets
   and be consistent train/serve.

D. Datasets can be huge — default to DuckDB / Parquet pushdown and staged outputs.
   OOM hardening is tracked separately (issue #10).

E. **Local L2 bundle reuse** (avoid recomputation): with ``--use-local-parquet``, the trainer
   may write or reuse a materialized split bundle under ``<LOCAL_PARQUET_DIR>/l2_training_bundle``
   (override with ``--l2-auto-bundle-dir``) when ``.l2_bundle_cache_key.json`` matches the
   current window, spec fingerprint, and bridge manifest stat token. Pass ``--no-l2-auto-bundle``
   to force the full in-process Steps 4–10 path. Explicit ``--l2-training-bundle DIR`` still
   skips Steps 1–7 regardless of cache.

Layered framework
-----------------
- ``layered_framework`` in the **dev** catalog is the **contract authority** for
  layer semantics (bet / run / trip / player). Legacy ``track_*`` blocks may remain
  as the **implementation partition** inside the same YAML until compute paths are
  fully migrated; new work MUST target layered semantics first.

- **Layer+method YAML aliases** (optional; mirrored to legacy keys at load time):
  ``bet_duckdb_window`` ↔ ``track_llm``, ``run_state_machine`` ↔ ``track_human``,
  ``player_profile_snapshot`` ↔ ``track_profile``. Author either vocabulary; do not
  duplicate conflicting candidate lists under both names in the same file.

- **Label intermediate disk cache** (Step 6, ``CHUNK_DIR``): full
  ``compute_labels`` output may be cached as ``chunk_<ws>_<we>.label_intermediate.parquet``
  with a JSON sidecar so reruns skip label CPU when label semantics + bet inputs are
  unchanged. Disable with ``DISABLE_LABEL_ASSET_CACHE=1``; bump semantics with
  ``LABEL_DEFINITION_VERSION``.
