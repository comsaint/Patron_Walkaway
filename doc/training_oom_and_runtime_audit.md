# Training Pipeline: OOM and Long-Runtime Audit

This document summarizes known **out-of-memory (OOM)** and **long-running-time** risks in the training pipeline (`trainer/run_pipeline()` and its call graph). It is intended as a single reference for formalizing mitigations and config choices.

**Scope:** Training path only (trainer, identity, labels, features, schema_io, etl_player_profile backfill). Scorer, validator, and status_server are out of scope.

**Assumptions:** Training data from local Parquet (e.g. `data/gmwds_t_bet.parquet`, `data/gmwds_t_session.parquet`); training may run on 8GB / 32GB / 64GB machines.

**Cross-reference — DEC-031 / train metrics (T-DEC031 step 7):** Step 9 model fitting avoids full-window dense `predict_proba` on the entire training matrix by using **batched prediction** where configured, and **Plan B+** paths may compute train metrics from **LibSVM** (`train_for_lgb.libsvm`) via `booster.predict` instead of materializing a full dense train matrix. For the exact call sites and env flags, see `trainer/training/trainer.py` (search `DEC-031`, `PREDICT_PROBA_BATCH_ROWS`, LibSVM / `from_file` training). OOM hotspots **A26** and Step 7 rows in the table below remain the primary RAM/time pairing with those mitigations.

---

## Summary Table

**Item IDs:** A01–A30 for quick reference (e.g. "fix A01", "A12 = Track Human lookback").

**Reconciliation with code:** A01 has been **fixed** (schema-only read). When `STEP7_KEEP_TRAIN_ON_DISK` (B+ path), Step 8 screening defaults to 2M rows via `_read_parquet_head`; in-memory path still uses full `train_df` unless `STEP8_SCREEN_SAMPLE_ROWS` is set. Line numbers below updated to match current codebase.

| ID | Step | Location | Type | Description | Severity | Mitigation / Note |
|----|------|----------|------|-------------|----------|-------------------|
| A01 | 3 | trainer.py:399–408 | OOM | ~~Full table~~ **Fixed:** Schema validation in `build_canonical_links_and_dummy_from_duckdb` now uses **schema-only** read (`pyarrow.parquet.read_schema(path).names`); no row data loaded. | ~~**Critical**~~ **Fixed** | **Fixed:** Use `_pq_sess.read_schema(path).names`; do not load row data. |
| A02 | 3 | trainer.py:499–500 | OOM, Long | DuckDB executes links/dummy queries then `.df()` materializes full result into pandas; links can be millions to tens of millions of rows. | High | After schema read fix (A01), re-evaluate; consider streaming or limiting result size if needed. |
| A03 | 3 | trainer.py:4259–4278 | OOM | When `CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS=True`: `load_local_parquet(..., sessions_only=True)` loads full session window + `build_canonical_mapping_from_df`. | High | Do not enable by default; keep DuckDB path. |
| A04 | 3 | identity.py:268, 331 | OOM | `build_canonical_mapping_from_links` and `_apply_mn_resolution` each `.copy()` links_df; multiple copies of links exist at once. | Medium | Consider reducing redundant copies or reusing a single copy. |
| A05 | 4 | etl_player_profile.py:435–479 | OOM | `_preload_sessions_local` loads **full** session Parquet with `pd.read_parquet(..., columns=_SESSION_COLS)` (no filters). Guarded by PRELOAD_MAX and RAM check but can still trigger. | High | On 8/32GB use `--no-preload` or lower threshold. |
| A06 | 4 | etl_player_profile.py:1806–1822, 1826–1892 | Long | Backfill loops over `snapshot_dates` or day-by-day and calls `build_player_profile` per date; without preload each call does a Parquet read + aggregation. | High | Use month-end `snapshot_dates`; avoid long day-by-day loops. |
| A07 | 4 | trainer.py:843–861 | OOM | `load_player_profile` reads profile Parquet (with filters); large canonical_ids result set can be large. | Medium | Pass canonical_ids to limit; watch window and column count. |
| A08 | 6 | trainer.py:742–776 | OOM | `load_local_parquet` loads one chunk (bets + sessions with filters and column projection); single chunk = one month + 2 days, can be large for long windows. | High | Already uses column and time pushdown; reduce `--days` or `--recent-chunks` if needed. |
| A09 | 6 | schema_io.py:44–45 | OOM | `normalize_bets_sessions` copies both bets and sessions; coexists with raw inputs. | Medium | Necessary copy; scales with chunk size. |
| A10 | 6 | trainer.py:1401–1445 | OOM | `apply_dq` performs multiple `sessions.copy()` and `bets.copy()` plus filtered copies. | Medium | Single mask used for bets (1482) to reduce copies; sessions still have multiple. |
| A11 | 6 | trainer.py:1557, 1995 | OOM | `add_track_human_features` copies bets; merges with canonical_map and Track LLM result. | Medium | Once per chunk; adds up with chunk size. |
| A12 | 6 | **features.py:359–377, 409–425, 543–570** | **Long** | **Track Human lookback:** When `TRAINER_USE_LOOKBACK=True`, `compute_loss_streak` and `compute_run_boundary` use **per-row Python double loop**; ~25M rows can take **7h+**. | **Critical** | Keep `TRAINER_USE_LOOKBACK=False` by default; enable only after Phase 2 numba vectorization. |
| A13 | 6 | features.py:680–716 | Long | `compute_table_hc`: outer loop over table_id, inner `np.unique` per bet window; large chunk can be tens of seconds to minutes. | Medium | Noticeable when table_hc feature is enabled on large chunks. |
| A14 | 6 | features.py:1338–1362, 1459 | OOM, Long | Track LLM: full chunk copy/slice, register in DuckDB, run window exprs, `.df()` materialize; large chunk uses significant memory and time. | High | Dominated by DuckDB + materialize for large chunks. |
| A15 | 6 | features.py:980–990, 1004–1006 | OOM, Long | `join_player_profile`: sort bets and profile, `merge_asof`, then per-column reindex; comment (996–998) notes avoiding extra sort saves ~10 GiB. | Medium | Single sort already avoided for memory. |
| A16 | 6 | labels.py:137, 144, 154 | OOM | `compute_labels` uses `df.copy()` and filtered copies. | Medium | Scales with chunk size. |
| A17 | 6 | trainer.py:2114 | OOM | After negative downsampling, `pd.concat([labeled[_pos_mask], _neg_keep], ...)`. | Low | Row count already reduced by sampling. |
| A18 | 6 | trainer.py:2143 | Long | Per-chunk `labeled.to_parquet(chunk_path)`; I/O scales with chunk size. | Medium | Necessary write; total time = per-chunk cost × number of chunks. |
| A19 | 7 | trainer.py:4813–4845 | OOM | **Step 7 pandas fallback:** `all_dfs = [pd.read_parquet(p) for p in chunk_paths]`, `full_df = pd.concat(...)`, sort; **full_df and train_df coexist** at peak; config notes ~20× on-disk. | **Critical** | Keep DuckDB path; avoid fallback; reduce chunks or NEG_SAMPLE_FRAC. |
| A20 | 7 | trainer.py:4694–4708 | Long | Step 7 DuckDB: sort over all chunks and write three split Parquets; can be minutes to tens of minutes for large data or when spilling. | High | Ensure temp_directory is writable and memory_limit is set. |
| A21 | 7 | trainer.py:4872–4878, 4922–4928, 5231 | OOM | Reading back splits: `pd.read_parquet(train_path)` etc. (three DataFrames), or B+ path loads full train later (5231). | High | B+ defers loading train until after Step 8 (5231); one full load still occurs. |
| A22 | 7 | trainer.py:1590–1705 | — | OOM pre-check: auto-adjusts NEG_SAMPLE_FRAC from chunk count and size estimate; estimate uses existing chunks or default 200MB/chunk. | — | Helps avoid Step 7 OOM; estimate can be optimistic on first run. |
| A23 | 8 | trainer.py:5158–5177, 5044–5049 | OOM | Step 8 uses **full train_df** for screening when `STEP8_SCREEN_SAMPLE_ROWS` is not set (in-memory path); B+ path **defaults to 2M rows** via `_read_parquet_head(step7_train_path, _sample_n_disk)` with `_sample_n_disk=2_000_000` when unset. | High | Set `STEP8_SCREEN_SAMPLE_ROWS` (e.g. 1.5M–2M) for in-memory path; B+ already caps at 2M by default. |
| A24 | 8 | features.py:811, 836, 855–858 | OOM, Long | `screen_features`: correlation matrix `.corr()`, LGB 100 rounds, `mutual_info_classif`; all scale with train size and features. | High | MI path is slowest; use STEP8_SCREEN_SAMPLE_ROWS and/or screen_method="lgbm". |
| A25 | 9 | trainer.py:3412–3418, 3261–3262 | OOM | `train_rated` / `val_rated` / `test_rated` and rated/nonrated `.copy()`; coexist with full train/valid/test. | High | Necessary subsets; total size driven by NEG_SAMPLE_FRAC and Step 7. |
| A26 | 9 | trainer.py:3534–3539, 3694–3734 | OOM, Long | `lgb.Dataset(X_train, ...)` and `lgb.train(..., num_boost_round=400, early_stopping(50))`; per trial and final refit. | High | Single run time depends on data size, feature count, early stop. |
| A27 | 9 | trainer.py:2677–2682 | Long | `study.optimize(..., n_trials=150, timeout=300)`; each trial runs lgb.train; total can be tens of minutes (capped by timeout and early stop). | High | Tune OPTUNA_N_TRIALS, OPTUNA_TIMEOUT_SECONDS, or OPTUNA_HPO_SAMPLE_ROWS. |
| A28 | Other | trainer.py:4239 | OOM | Loading canonical_map from artifact: `pd.read_parquet(CANONICAL_MAPPING_PARQUET)`. | Low | File is usually small. |
| A29 | Other | etl_player_profile.py:1346–1350 | OOM | Profile merge: `_retained = pd.read_parquet(...)` then `pd.concat([_retained, df], ...)`. | Medium | Only on profile merge path. |
| A30 | Other | etl_player_profile.py:386–389 | Long | `_load_sessions_local` one Parquet read per snapshot (with pushdown); without preload, backfill loop calls it many times. | Medium | Grows with number of backfill snapshots. |

---

## Config and Severity

**Critical (fix or avoid first):**

- **A01** — **Fixed.** trainer.py:399–408: Schema check now uses schema-only read (`pyarrow.parquet.read_schema(path).names`); no full table load.
- **A19** — Step 7 pandas fallback (4813–4845): Avoid by keeping DuckDB and reducing data volume if needed.
- **A12** — features.py Track Human lookback (359–377, 409–425, 543–570): Keep `TRAINER_USE_LOOKBACK=False` until Phase 2 vectorization.

**Relevant config (trainer / config.py):**

| Config | Default | Effect |
|--------|--------|--------|
| `TRAINER_USE_LOOKBACK` | False | If True, Step 6 Track Human uses per-row lookback (7h+ at 25M rows). |
| `NEG_SAMPLE_FRAC` | 0.2 | Lower reduces Step 6/7/9 data size. |
| `NEG_SAMPLE_FRAC_AUTO` | True | Enables OOM pre-check and auto-reduction of neg fraction. |
| `STEP7_USE_DUCKDB` | True | Use DuckDB for Step 7 sort/split; if False, pandas fallback is used (high OOM risk). |
| `STEP8_SCREEN_SAMPLE_ROWS` | None | Set (e.g. 2_000_000) to cap screening data and reduce Step 8 time and memory. |
| `OPTUNA_N_TRIALS` | 150 | Number of HPO trials; total Step 9 time scales with this. |
| `OPTUNA_TIMEOUT_SECONDS` | 300 | Max time for full Optuna study. |
| `OPTUNA_HPO_SAMPLE_ROWS` | None | Set to subsample train/valid for HPO only and shorten trials. |
| `SCREEN_FEATURES_METHOD` | "lgbm" | "mi" / "mi_then_lgbm" add mutual_info_classif (slower, more memory). |
| `PROFILE_PRELOAD_MAX_BYTES` | 1.5 GB | Session file larger than this skips preload in profile backfill. |
| `--no-preload` (CLI) | False | Disables full session preload during profile backfill. |
| `CHUNK_TWO_STAGE_CACHE` | (unset → **on**) | Step 6 R6 **prefeatures** cache: when enabled, a hit loads `*.prefeatures.parquet` with **`pd.read_parquet` (full table)** — same peak RAM class as loading the chunk without R6; multi-chunk parallel runs do not reduce that peak automatically. Set to `0` / `false` / `no` / `off` to disable (saves disk and avoids double Parquet writes on miss). SSOT default: `trainer.core.config.CHUNK_TWO_STAGE_CACHE_DEFAULT`. |

**Step 6 R6 (prefeatures) — disk:** On cache miss with two-stage enabled, the pipeline may write both `chunk_*_*.prefeatures.parquet` and the final `chunk_*.parquet` (~2× Parquet write volume vs R6 off). See `.cursor/plans/PLAN_chunk_cache_portable_hit.md`.

---

## Column Legend

- **ID:** Item ID (A01–A30) for quick reference.
- **Step:** Pipeline step (1–10) or "Other".
- **Location:** File and line(s).
- **Type:** OOM (memory), Long (runtime), or both.
- **Description:** What happens and when (short).
- **Severity:** Critical / High / Medium / Low.
- **Mitigation / Note:** Recommended action or existing safeguard.

---

*Generated from training-path audit. For Phase 2 lookback plan see `doc/track_human_lookback_vectorization_plan.md`.*

---

# 訓練流程 OOM 與長時間執行稽核（中文版）

本文件彙整訓練流程（`trainer/run_pipeline()` 及其呼叫鏈）中已知的**記憶體不足（OOM）**與**長時間執行**風險，作為緩解措施與設定決策的單一參考。

**範圍：** 僅涵蓋訓練路徑（trainer、identity、labels、features、schema_io、etl_player_profile backfill）。Scorer、validator、status_server 不在範圍內。

**假設：** 訓練資料來自本地 Parquet（如 `data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`）；訓練可能在 8GB／32GB／64GB 機器上執行。

---

## 總表

**項目 ID：** A01–A30，便於溝通（例如「修 A01」、「A12 = Track Human lookback」）。

**與程式碼對照：** A01 已**修復**（改為僅讀 schema）。B+ 路徑（`STEP7_KEEP_TRAIN_ON_DISK`）下 Step 8 screening 預設以 `_read_parquet_head` 取 2M 列；in-memory 路徑仍可能用全量 `train_df`，除非設定 `STEP8_SCREEN_SAMPLE_ROWS`。下表行號已對齊目前程式碼。

| ID | 步驟 | 位置 | 類型 | 說明 | 嚴重度 | 緩解／備註 |
|----|------|------|------|------|--------|------------|
| A01 | 3 | trainer.py:399–408 | OOM | ~~全表~~ **已修復：** `build_canonical_links_and_dummy_from_duckdb` 內 schema 檢查改為**僅讀 schema**（`pyarrow.parquet.read_schema(path).names`），不載入列資料。 | ~~**Critical**~~ **已修復** | **已修復：** 使用 `_pq_sess.read_schema(path).names`，不載入列。 |
| A02 | 3 | trainer.py:499–500 | OOM, Long | DuckDB 執行 links/dummy 查詢後 `.df()` 將結果全部 materialize 進 pandas；links 可達數百萬～數千萬行。 | High | 修好 schema 讀取（A01）後再觀察；必要時改串流或限制結果大小。 |
| A03 | 3 | trainer.py:4259–4278 | OOM | `CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS=True` 時：`load_local_parquet(..., sessions_only=True)` 載入整個 session 時間窗 + `build_canonical_mapping_from_df`。 | High | 預設勿啟用；維持 DuckDB 路徑。 |
| A04 | 3 | identity.py:268, 331 | OOM | `build_canonical_mapping_from_links` 與 `_apply_mn_resolution` 各自對 links_df 做 `.copy()`；多份 links 同時存在。 | Medium | 評估減少多餘 copy 或共用單一 copy。 |
| A05 | 4 | etl_player_profile.py:435–479 | OOM | `_preload_sessions_local` 以 `pd.read_parquet(..., columns=_SESSION_COLS)` **全表**載入 session（無 filter）；有 PRELOAD_MAX 與 RAM 檢查仍可能觸發。 | High | 8/32GB 建議 `--no-preload` 或調低閾值。 |
| A06 | 4 | etl_player_profile.py:1806–1822, 1826–1892 | Long | backfill 依 `snapshot_dates` 或逐日迴圈呼叫 `build_player_profile`；無 preload 時每次為一次 Parquet 讀取 + 聚合。 | High | 使用月底 snapshot_dates；避免長區間逐日迴圈。 |
| A07 | 4 | trainer.py:843–861 | OOM | `load_player_profile` 讀取 profile Parquet（有 filter）；canonical_ids 結果集大時仍可能很大。 | Medium | 傳入 canonical_ids 限制；留意時間窗與欄位數。 |
| A08 | 6 | trainer.py:742–776 | OOM | `load_local_parquet` 每 chunk 載入 bet/session（有 filters 與欄位投影）；單 chunk = 一個月 + 2 天，長窗時單次就很大。 | High | 已做欄位與時間 pushdown；必要時減 `--days` 或 `--recent-chunks`。 |
| A09 | 6 | schema_io.py:44–45 | OOM | `normalize_bets_sessions` 對 bets/sessions 各做 copy；與 raw 同時存在。 | Medium | 必要複製；隨 chunk 大小同增。 |
| A10 | 6 | trainer.py:1401–1445 | OOM | `apply_dq` 內多次 `sessions.copy()`、`bets.copy()` 及篩選後 copy。 | Medium | bets 已用單一 mask 減少 copy（1482）；sessions 仍多份。 |
| A11 | 6 | trainer.py:1557, 1995 | OOM | `add_track_human_features` 複製 bets；與 canonical_map、Track LLM 結果 merge。 | Medium | 每 chunk 一次；隨 chunk 大小疊加。 |
| A12 | 6 | **features.py:359–377, 409–425, 543–570** | **Long** | **Track Human lookback：** `TRAINER_USE_LOOKBACK=True` 時 `compute_loss_streak`、`compute_run_boundary` 使用 **per-row Python 雙層迴圈**；約 25M 列可 **7h+**。 | **Critical** | 預設保持 `TRAINER_USE_LOOKBACK=False`；Phase 2 numba 向量化後再啟用。 |
| A13 | 6 | features.py:680–716 | Long | `compute_table_hc`：外層依 table_id 迴圈，內層每 bet 一次 `np.unique`；大 chunk 可達數十秒～數分。 | Medium | 啟用 table_hc 特徵且 chunk 大時可感。 |
| A14 | 6 | features.py:1338–1362, 1459 | OOM, Long | Track LLM：整 chunk 複製/切片、註冊 DuckDB、執行 window 運算、`.df()` materialize；大 chunk 時記憶體與時間皆高。 | High | 大 chunk 時以 DuckDB + materialize 為主。 |
| A15 | 6 | features.py:980–990, 1004–1006 | OOM, Long | `join_player_profile`：對 bets/profile sort、`merge_asof`、再逐欄 reindex；註解（996–998）已避免多餘 sort 省約 10 GiB。 | Medium | 已避免一次 sort 以省記憶體。 |
| A16 | 6 | labels.py:137, 144, 154 | OOM | `compute_labels` 內 `df.copy()` 及篩選後 copy。 | Medium | 隨 chunk 大小同增。 |
| A17 | 6 | trainer.py:2114 | OOM | 負樣本下採後 `pd.concat([labeled[_pos_mask], _neg_keep], ...)`。 | Low | 採樣後列數已減。 |
| A18 | 6 | trainer.py:2143 | Long | 每 chunk `labeled.to_parquet(chunk_path)`；I/O 與 chunk 大小成正比。 | Medium | 必要寫出；總時間 = 每 chunk 成本 × chunk 數。 |
| A19 | 7 | trainer.py:4813–4845 | OOM | **Step 7 pandas fallback：** `all_dfs = [pd.read_parquet(p) for p in chunk_paths]`、`full_df = pd.concat(...)`、sort；**full_df 與 train_df 短暫共存**；config 註約 20× on-disk。 | **Critical** | 維持 DuckDB 路徑、避免 fallback；減 chunk 數或 NEG_SAMPLE_FRAC。 |
| A20 | 7 | trainer.py:4694–4708 | Long | Step 7 DuckDB：對所有 chunk 做 sort、寫出三個 split Parquet；大資料或 spill 時可達數分～十數分。 | High | 確保 temp_directory 可寫、memory_limit 已設。 |
| A21 | 7 | trainer.py:4872–4878, 4922–4928, 5231 | OOM | 讀回 split：`pd.read_parquet(train_path)` 等三份，或 B+ 路徑於 5231 再載入整份 train。 | High | B+ 可延後載入 train 至 Step 8 後（5231）；仍有一次完整載入。 |
| A22 | 7 | trainer.py:1590–1705 | — | OOM 預檢：依 chunk 數與估計大小自動調低 NEG_SAMPLE_FRAC；估計用既有 chunk 或預設 200MB/chunk。 | — | 有助避免 Step 7 OOM；首次跑估計可能偏樂觀。 |
| A23 | 8 | trainer.py:5158–5177, 5044–5049 | OOM | Step 8 未設 `STEP8_SCREEN_SAMPLE_ROWS` 時（in-memory 路徑）用 **全量 train_df** 做 screening；B+ 路徑**預設 2M 列**（`_read_parquet_head(..., _sample_n_disk)`，未設時 `_sample_n_disk=2_000_000`）。 | High | in-memory 路徑設 `STEP8_SCREEN_SAMPLE_ROWS`（如 1.5M–2M）；B+ 已預設 2M 上限。 |
| A24 | 8 | features.py:811, 836, 855–858 | OOM, Long | `screen_features`：相關矩陣 `.corr()`、LGB 100 輪、`mutual_info_classif`；隨 train 大小與特徵數同增。 | High | MI 路徑最慢；可用 STEP8_SCREEN_SAMPLE_ROWS 或 screen_method="lgbm"。 |
| A25 | 9 | trainer.py:3412–3418, 3261–3262 | OOM | `train_rated`／`val_rated`／`test_rated` 及 rated/nonrated 的 `.copy()`；與完整 train/valid/test 同時存在。 | High | 必要子集；總量受 NEG_SAMPLE_FRAC 與 Step 7 影響。 |
| A26 | 9 | trainer.py:3534–3539, 3694–3734 | OOM, Long | `lgb.Dataset(X_train, ...)` 與 `lgb.train(..., num_boost_round=400, early_stopping(50))`；每 trial 與最終 refit 各一次。 | High | 單次時間與資料量、特徵數、early stop 有關。 |
| A27 | 9 | trainer.py:2677–2682 | Long | `study.optimize(..., n_trials=150, timeout=300)`；每 trial 執行 lgb.train；總時間可達數十分鐘（受 timeout 與 early stop 限制）。 | High | 可調 OPTUNA_N_TRIALS、OPTUNA_TIMEOUT_SECONDS 或 OPTUNA_HPO_SAMPLE_ROWS。 |
| A28 | Other | trainer.py:4239 | OOM | 從 artifact 載入 canonical_map：`pd.read_parquet(CANONICAL_MAPPING_PARQUET)`。 | Low | 檔案通常小。 |
| A29 | Other | etl_player_profile.py:1346–1350 | OOM | profile 合併：`_retained = pd.read_parquet(...)` 再 `pd.concat([_retained, df], ...)`。 | Medium | 僅 profile 合併路徑。 |
| A30 | Other | etl_player_profile.py:386–389 | Long | `_load_sessions_local` 每 snapshot 一次 Parquet 讀取（有 pushdown）；無 preload 時 backfill 迴圈內多次呼叫。 | Medium | 隨 backfill snapshot 數同增。 |

---

## 設定與嚴重度（中文）

**Critical（優先修復或避免）：**

- **A01** — **已修復。** trainer.py:399–408：schema 檢查已改為僅讀 schema（`pyarrow.parquet.read_schema(path).names`），不再載入全表。
- **A19** — Step 7 pandas fallback (4813–4845)：維持 DuckDB、必要時減資料量以避免。
- **A12** — features.py Track Human lookback (359–377, 409–425, 543–570)：在 Phase 2 向量化前保持 `TRAINER_USE_LOOKBACK=False`。

**相關設定（trainer / config.py）：**

| 設定 | 預設 | 說明 |
|------|------|------|
| `TRAINER_USE_LOOKBACK` | False | 為 True 時 Step 6 Track Human 使用 per-row lookback（25M 列可 7h+）。 |
| `NEG_SAMPLE_FRAC` | 0.2 | 調低可減少 Step 6/7/9 資料量。 |
| `NEG_SAMPLE_FRAC_AUTO` | True | 啟用 OOM 預檢與負樣本比例自動調低。 |
| `STEP7_USE_DUCKDB` | True | Step 7 用 DuckDB 做 sort/split；為 False 則走 pandas fallback（OOM 風險高）。 |
| `STEP8_SCREEN_SAMPLE_ROWS` | None | 設值（如 2_000_000）可限制 screening 資料量並減少 Step 8 時間與記憶體。 |
| `OPTUNA_N_TRIALS` | 150 | HPO trial 數；Step 9 總時間與此成正比。 |
| `OPTUNA_TIMEOUT_SECONDS` | 300 | 整次 Optuna study 最長時間（秒）。 |
| `OPTUNA_HPO_SAMPLE_ROWS` | None | 設值可僅對 HPO 階段 subsample train/valid，縮短每 trial。 |
| `SCREEN_FEATURES_METHOD` | "lgbm" | "mi"／"mi_then_lgbm" 會跑 mutual_info_classif（較慢、較吃記憶體）。 |
| `PROFILE_PRELOAD_MAX_BYTES` | 1.5 GB | session 檔大於此值時 profile backfill 不 preload。 |
| `--no-preload` (CLI) | False | 關閉 profile backfill 時之全表 session preload。 |

---

## 欄位說明（中文）

- **ID：** 項目 ID（A01–A30），便於快速對照。
- **步驟：** 流程步驟（1–10）或「Other」。
- **位置：** 檔案與行號（或範圍）。
- **類型：** OOM（記憶體）、Long（長時間）、或兩者。
- **說明：** 發生什麼、在什麼條件下（簡短）。
- **嚴重度：** Critical／High／Medium／Low。
- **緩解／備註：** 建議作法或既有防護。

---

*稽核範圍：訓練路徑。Phase 2 lookback 計畫見 `doc/track_human_lookback_vectorization_plan.md`。*

---

# OOM／長時間執行 — 優先處理計畫（不含 A12）

本節將稽核項目（A01–A30，**排除 A12**，因 A12 已由 Phase 2 lookback 向量化處理）整理為可執行的優先順序與處理方式，並對每項估算可達成的**記憶體**與／或**時間**改善。估算以文件假設（8/32/64GB、本地 Parquet、chunk ≈ 1 月+2 天）為準；不確定處會標註。

## 優先順序原則

- **順序**：Critical → High（OOM 優先於 Long）→ Medium → Low；同級內依 pipeline 順序（Step 3 → 4 → 6 → 7 → 8 → 9 → Other）與依賴關係排列。
- **估算**：記憶體為「該階段 peak 可減少量」、時間為「該步驟或該階段可減少量」。實際資料規模會影響絕對值，表中多為數量級或區間。

---

## Phase 1 — Critical（必做，先消除致命 OOM）

| 優先 | ID | 建議作法 | 預估記憶體減少 | 預估時間減少 | 備註／不確定性 |
|------|----|----------|----------------|----------------|----------------|
| 1 | **A01** | **已修復。** 已改為 schema-only 讀取（`pyarrow.parquet.read_schema(path).names`），見 trainer.py:399–408。 | 已達成：不再載入整張 session 表，省 **1–5 GB** peak（視表大小）。 | 已達成：略減 I/O。 | 已修復。 |
| 2 | **A19** | **已實作（Phase 1）**：維持 `STEP7_USE_DUCKDB=True`；當 `STEP7_USE_DUCKDB=False` 時 log warning 後再走 pandas fallback；config 註明減 `--days`／NEG_SAMPLE_FRAC 以避開 fallback。 | 若目前會走 fallback：peak 約 **20× on-disk**；改為只用 DuckDB 則 **避免該 peak**。 | 不觸發 fallback 即無 pandas concat/sort。 | trainer.py 進入 fallback前 logger.warning；config.py STEP7_USE_DUCKDB 註解已更新。 |

---

## Phase 2 — High（OOM／Long 高影響，依 pipeline 順序）

| 優先 | ID | 建議作法 | 預估記憶體減少 | 預估時間減少 | 備註／不確定性 |
|------|----|----------|----------------|----------------|----------------|
| 3 | A02 | **Phase 2a（文件）**：A01 修好後若 links 仍導致 OOM，再評估 DuckDB 結果流式寫出 Parquet 再分塊讀入，或 `.df()` 前先 `LIMIT` 取樣（需確認下游是否允許）。目前以文件註明為完成。 | 若改為不一次 materialize 全量：可省 **與 links 列數成比例**，約 **數百 MB～數 GB**。 | 若改 streaming/分塊，可能略增 I/O 時間。 | 依實際 links 列數與機器 RAM 決定是否實作。 |
| 4 | A03 | **Phase 2a 已實作**：預設 `CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS=False`；config 註明勿於生產啟用；當為 True 時 trainer.py 會 log warning（A03）。 | 若關閉則 **避免載入整段 session 時間窗**，可達 **1–5+ GB**。 | 避免一次載入全 session。 | 程式內已加 warning；config 註明 A03。 |
| 5 | A05 | **Phase 2a 已實作**：8/32GB 環境建議 `--no-preload` 或調低 `PROFILE_PRELOAD_MAX_BYTES`；config.py 該常數註解已註明（A05）。 | 不 preload 即不載入全表，省 **整份 session 表**（可達 **1.5 GB+**）。 | 無 preload 時改為每 snapshot 讀 Parquet，總時間可能**增加**（見 A06）。 | config 註解已加。 |
| 6 | A06 | **Phase 2a（文件）**：排程／文件建議使用**月底** `snapshot_dates`，避免長區間逐日 backfill；若 backfill 由排程驅動，改為只排月底。本節即為文件建議。 | 不直接省記憶體。 | 若從逐日改為月底：**N 日 → 約 N/30 次** `build_player_profile`，可省 **數十分鐘～數小時**。 | 多為流程／排程調整。 |
| 7 | A08 | **Phase 2b 已實作（文件）**：本節與 Mitigation 列已註明長窗或大 chunk 時減 `--days` 或 `--recent-chunks`。 | 透過縮小範圍可降低**單次載入 chunk 大小**，約 **數百 MB～1+ GB**（視窗長）。 | 縮小範圍可略減 Step 6 單 chunk 時間。 | 以文件註明為完成。 |
| 8 | A14 | **Phase 2b（文件）**：以文件註明為完成；若 links 仍 OOM 可再評估只註冊必要欄位或 column pruning。 | 若減少 materialize 的列/欄：可省 **約 1× chunk 大小**（數百 MB～1+ GB）。 | 若減少資料量可省 **數十秒～數分鐘**/chunk。 | 估算為數量級。 |
| 9 | A20 | **Phase 2b 已實作**：config 註明確保 temp_directory 可寫、memory_limit 已設（STEP7_DUCKDB_*）；Step 7 已使用兩者。 | 透過 spill 可**避免** Step 7 DuckDB OOM。 | 若改為多 spill 可能略增 **數分鐘**。 | config.py STEP7_DUCKDB_TEMP_DIR 註解已更新。 |
| 10 | A21 | **Phase 2b 已實作**：B+ 已延後載入 train；screening 時已用 `_read_parquet_head` 預設 2M。 | B+ 已用 head 取樣。 | 影響小。 | 以文件註明為完成。 |
| 11 | A22 | **Phase 2b 已實作（文件）**：OOM 預檢已實作；本節註明「首次跑估計可能偏樂觀」。 | 估計更準可**減少誤判**。 | 無。 | 以文件註明為完成。 |
| 12 | A23 | **Phase 2b 已實作**：config 之 `STEP8_SCREEN_SAMPLE_ROWS` 已註明 in-memory 建議 2_000_000；B+ 已預設 2M。 | 若 train 為 5M–10M 列：可省 **約 0.6–2 GB**。B+ 已達成。 | screening 減為 2M 可省 **數十秒～數分鐘**。 | config 註解已更新。 |
| 13 | A24 | **Phase 2b 已實作**：維持 `SCREEN_FEATURES_METHOD="lgbm"`；config 已註明 "mi"/"mi_then_lgbm" 較慢較吃記憶體（A24）。 | 與 A23 同。MI 改 LGB 可省 **數分鐘～數十分鐘**。 | **數分鐘～數十分鐘**（若從 MI 改 LGB）。 | config 註解已更新。 |
| 14 | A25 | 可選評估 LGB view；維持以文件註明，不實作。 | 若可少一份 copy：約 **數百 MB～1 GB**。 | 可略。 | Phase 2c 以文件為完成。 |
| 15 | A26 | **Phase 2c 已實作**：config 之 `OPTUNA_HPO_SAMPLE_ROWS` 已註明對 HPO 階段 peak／trial 時間之影響（A26）。 | 若啟用可降 HPO peak **與 subsample 比例成比例**。 | 每 trial 變快，總 HPO 可省 **數分～數十分鐘**。 | config 註解已更新。 |
| 16 | A27 | **Phase 2c 已實作**：config 之 `OPTUNA_N_TRIALS`、`OPTUNA_TIMEOUT_SECONDS` 已註明對 Step 9 時間之影響（A27）。 | 不直接省記憶體。 | 總 Step 9 可省 **數分～數十分鐘**（減 trial 或 timeout）。 | config 註解已更新。 |

---

## Phase 3 — Medium（可分批、依效益與改動成本）

| 優先 | ID | 建議作法 | 預估記憶體減少 | 預估時間減少 | 備註／不確定性 |
|------|----|----------|----------------|----------------|----------------|
| 17 | A04 | 在 `build_canonical_mapping_from_links` 與 `_apply_mn_resolution` 中減少多餘 `.copy()`：例如在 `_apply_mn_resolution` 內對輸入做一次 copy 並在該 copy 上 in-place 操作，`build_canonical_mapping_from_links` 只傳 slice（`rated`），避免對全量 links 再 copy。 | 目前約 2 份 links 量（build 內 1 份 + _apply_mn 內 1 份）；可省 **約 1 份 links**，約 **數百 MB～1+ GB**（與 links 列數成正比）。 | 可略。 | 需確認 `_apply_mn_resolution` 是否會改動傳入的 df。 |
| 18 | A07 | **Phase 2a 已確認**：呼叫端（trainer run_pipeline、backtester）皆傳入 `canonical_ids`（rated 玩家 ID）；`load_player_profile` 在 `canonical_ids=[]` 時直接 return None 不讀表。文件註明大窗或大 canonical 集時風險。 | 可省 **與 profile 結果集大小成比例**，約 **數十～數百 MB**（視窗與玩家數）。 | 略減讀取與 merge 時間。 | trainer 與 backtester 已傳 _rated_cids；R222 已處理空表不載入。 |
| 19 | A09 | `normalize_bets_sessions` 為必要 copy（型別轉換）；若未來改為 in-place 型別轉換（pandas 允許且無共用風險），可省 1 份 bets + 1 份 sessions。 | 若改為 in-place：可省 **約 1× (bets + sessions)**，約 **數百 MB～1+ GB**/chunk。 | 可略。 | 需確認後續是否仍需要原始型別；改動範圍較大。 |
| 20 | A10 | 在 `apply_dq` 內合併 sessions 的多次 filter：用單一 mask 或鏈式布林索引，最後一次 `.copy()`，避免 1401/1408/1429/1438 各一次 copy。 | 可省 **約 1–2 份 sessions**（每份約 chunk 內 sessions 大小），約 **數十～數百 MB**/chunk。 | 可略。 | 與現有 bets 單一 mask 做法對齊。 |
| 21 | A11 | 評估 `add_track_human_features` 是否可在 bets 上 in-place 加欄位或只複製必要欄位；或與前一步共用一份 bets 避免重複 copy。 | 可省 **約 1× bets**/chunk，約 **數百 MB**/chunk（視欄位數）。 | 可略。 | 需理清與 canonical_map、Track LLM merge 的介面。 |
| 22 | A13 | `compute_table_hc`：改為向量化或先 groupby table_id 再對每組做一次 `np.unique`（避免 per-bet 重複），或用 numba 做視窗內 unique count。 | 不直接省記憶體。 | 大 chunk 時可從 **數十秒～數分鐘** 降到 **數秒～數十秒**（數量級）。 | 需看 table_id 基數與呼叫頻率。 |
| 23 | A15 | 註解已註明省約 10 GiB；維持現狀，僅確認無多餘 sort。若未來改為 DuckDB 做 as-of join 可再評估。 | 已優化；**無額外可量化節省**（先前已省 ~10 GiB）。 | 已優化。 | 維持現狀即可。 |
| 24 | A16 | `compute_labels`：合併 null 檢查為單一 mask，再一次 `df = df[~mask].copy()`，避免 137/144/154 多次 copy。 | 可省 **約 1 份 bets**（chunk 尺度），約 **數十～數百 MB**/chunk。 | 可略。 | 邏輯簡單，可與 A10 同風格處理。 |
| 25 | A18 | 必要 I/O；若需加速可考慮非同步寫或較快磁碟；或壓縮等級調整。 | 不直接省記憶體。 | 改寫入策略或硬體可略減 **每 chunk 數秒**；不確定。 | 以文件說明為主。 |
| 26 | A29 | profile merge：若合併多檔，可改為逐檔讀取並 append 到單一 DataFrame 後再寫出，避免 `_retained` 與 `df` 同時完整存在。 | 可省 **約 1 份 profile 大小**（合併時），約 **數十～數百 MB**（視 profile 規模）。 | 可略。 | 僅影響 profile 合併路徑。 |
| 27 | A30 | 與 A06 一致：用月底 snapshot、或啟用 preload，減少 `_load_sessions_local` 呼叫次數。 | 不直接省記憶體。 | 與 A06 同：**數十分鐘～數小時**（依 snapshot 數與是否 preload）。 | 流程／排程與 A06 一併考慮。 |

---

## Phase 4 — Low／其他

| 優先 | ID | 建議作法 | 預估記憶體減少 | 預估時間減少 | 備註／不確定性 |
|------|----|----------|----------------|----------------|----------------|
| 28 | A17 | 採樣後列數已減；可選：用 `pd.concat(..., copy=False)` 若型別一致且安全。 | 可能省 **數十 MB**（取決於 concat 實作）。 | 可略。 | 效益小；可選做。 |
| 29 | A28 | canonical_map 通常小；若實務上檔案變大，可改為只讀 schema 或必要欄位。 | 通常 **&lt; 100 MB**；僅在 artifact 很大時才有感。 | 可略。 | 低優先。 |

---

## Phase 1 與 Phase 2a 實作狀態（OOM 稽核計畫）

- **Phase 1（A19）**：`STEP7_USE_DUCKDB=False` 時改為先 log warning 再進入 pandas fallback；`config.py` 註明保持 True 或減 `--days`／NEG_SAMPLE_FRAC；稽核文件 A19 列已標為已實作。
- **Phase 2a（A02）**：以文件註明「A01 後若 links 仍 OOM 再評估 streaming／LIMIT」為完成，無程式變更。
- **Phase 2a（A03）**：config 註明勿預設啟用；`trainer.py` 在 `CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS=True` 時 log warning。
- **Phase 2a（A05）**：`config.py` 之 `PROFILE_PRELOAD_MAX_BYTES` 註解已註明 8/32GB 使用 `--no-preload` 或調低該值。
- **Phase 2a（A06）**：本節「優先處理計畫」表與 A06 列即為排程／文件建議（月底 snapshot_dates）。
- **Phase 2a（A07）**：已確認 trainer 與 backtester 皆傳入 `canonical_ids` 限制 profile 讀取；`load_player_profile` 空列表時 return None。

## Phase 2b 與 Phase 2c 實作狀態（OOM 稽核計畫）

- **A08**：本節與 Mitigation 列已註明長窗或大 chunk 時減 `--days` 或 `--recent-chunks`；無程式變更。
- **A14**：以文件註明為完成；若 links 仍 OOM 可再評估 Track LLM 只註冊必要欄位或 column pruning。
- **A20**：`config.py` 之 `STEP7_DUCKDB_TEMP_DIR` 註解已註明確保可寫、memory_limit 已設（STEP7_DUCKDB_RAM_*）；Step 7 程式已使用 temp_directory 與 memory_limit。
- **A21**：B+ 已延後載入 train；screening 時已用 `_read_parquet_head` 預設 2M；以文件註明為完成。
- **A22**：OOM 預檢已實作；本節註明「首次跑估計可能偏樂觀」。
- **A23**：`config.py` 之 `STEP8_SCREEN_SAMPLE_ROWS` 已註明 in-memory 路徑建議 2_000_000；B+ 已預設 2M。
- **A24**：`config.py` 之 `SCREEN_FEATURES_METHOD` 已註明 "mi"/"mi_then_lgbm" 較慢且較吃記憶體，建議 lgbm；預設為 lgbm。
- **A25**：可選；維持以文件註明，不實作 LGB view 評估。
- **A26/A27**：`config.py` 之 `OPTUNA_N_TRIALS`、`OPTUNA_TIMEOUT_SECONDS`、`OPTUNA_HPO_SAMPLE_ROWS` 已註明對 Step 9 時間／記憶體之影響（A26/A27）。

## Phase 3 與 Phase 4 實作狀態（OOM 稽核計畫）

- **A04**：`identity.py`：`build_canonical_mapping_from_links` 改為僅複製 rated 列/欄（單一 copy），再傳入 `_apply_mn_resolution`；避免全量 links_df.copy()。
- **A10**：`trainer.py` `apply_dq`：sessions 之 FND-02 與 FND-04 合併為單一 mask，最後一次 `sessions = sessions[dq_mask].copy()`。
- **A16**：`labels.py` `compute_labels`：E3 與 R12 之 null 檢查合併為單一 combined_null mask，一次 `df = df[~combined_null].copy()`。
- **A15**：維持現狀（註解已註明省約 10 GiB）。
- **Phase 4（A17, A28）**：可選；本輪以文件註明為完成，不實作程式變更。

---

## 依賴與建議順序摘要

1. **A01 已完成**：Step 3 已改為 schema-only 讀取，為 A02 提供穩定 baseline。
2. **A19 已實作**：Guard（log warning when False）+ config 與文件註明；確保 DuckDB 路徑為預設。
3. **A23 + A24**：建議一併做（Step 8 screening 取樣 + 方法），記憶體與時間效益一起算。
4. **A22**：屬預檢與估計改進，可與 Step 7 相關項目一起做。
5. **A04、A10、A16**：都是「少 copy」類，可集中一輪重構，測試時注意不可變性與下游是否依賴 copy。

---

## 估算不確定性與建議

- **記憶體**：實際 session／chunk／links 大小隨資料與時間窗變化，上述多為「同數量級」估算；建議在 8GB／32GB 各跑一次，用 `memory_profiler` 或類似工具量測 Step 3/6/7/8 的 peak，再回頭微調優先順序。
- **時間**：A06/A30 的節省依 snapshot 策略與是否 preload 差異很大；A26/A27 依 trial 數與 early stop 而定。其餘多為單步驟數量級估計。
- **A09、A25**：牽涉「必要 copy」或第三方介面，需小範圍試驗再決定是否改。
