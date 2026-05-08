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
- **Final train+valid refit (§12)**: in-memory / Plan-B-CSV LightGBM paths refit on
  ``train ∪ valid`` after selection using the same tree budget as early stopping.
  LibSVM-on-disk **without** ``TRAINER_FILE_BACKED_STRICT`` skips refit (see
  ``training_metrics`` → ``final_refit_train_valid``).
  When ``TRAINER_FILE_BACKED_STRICT`` is ``1``/``true``/``yes``/``on`` (issue #25),
  the rated LibSVM path performs a **file-backed** refit by merging train and valid
  LibSVM (and weight lines) before a second ``lgb.train`` with ``num_boost_round=best_iteration``.
  A3 CatBoost/XGBoost disk training **does not** fall back to in-memory fit in strict mode
  (disk failure is fatal). Set ``TRAINER_DISABLE_FINAL_REFIT_TRAIN_VALID=1`` to skip refit for debugging only.

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
