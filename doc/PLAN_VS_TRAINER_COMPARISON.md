# Phase 1 Plan vs. Existing trainer.py — Comparison Summary

This document summarizes how the approach in `ssot/patron_walkaway_phase_1.plan.md` differs from what is implemented in `trainer/trainer.py`. Tables are organized by functional area.

---

## 1. Data Loading & Time Window


| Aspect                     | Existing `trainer.py`                                                         | Phase 1 Plan                                                                                                                          |
| -------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| **Data source**            | ClickHouse only (or local CSV cache `bets_buffer.csv`, `sessions_buffer.csv`) | ClickHouse for production; **optional** local Parquet (e.g. `.data/`) for training/dev acceleration                                   |
| **Time window definition** | Ad-hoc `parse_window()` / `default_training_window()`; 7 days default         | Centralized `time_fold.py` with `get_monthly_chunks()`, `get_train_valid_test_split()`; strict `[window_start, window_end)` semantics |
| **Chunking**               | Single window; full range loaded at once                                      | Time-windowed extraction; per-chunk processing; no full-range load                                                                    |
| **Extended pull (C1)**     | None; no lookahead buffer for label calc                                      | `extended_end` = window_end + max(LABEL_LOOKAHEAD_MIN, 1 day); extended data used only for labels, never for training samples         |
| **Train/Valid/Test split** | 80/20 time-based (train vs. validation only)                                  | 70/15/15 time-based (train / valid / test) via `time_fold.get_train_valid_test_split()`                                               |


---

## 2. Data Quality (DQ) Guardrails


| Aspect                    | Existing `trainer.py`                                                                 | Phase 1 Plan                                                                                                                                                    |
| ------------------------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **t_bet queries**         | `wager > 0` filter; no `payout_complete_dtm IS NOT NULL`; no `player_id != -1`        | `payout_complete_dtm IS NOT NULL`, `player_id != PLACEHOLDER_PLAYER_ID`, `FINAL` allowed                                                                        |
| **t_session queries**     | Simple `session_start_dtm` range; no dedup; no `is_manual`/`is_deleted`/`is_canceled` | **NO FINAL** (G1); FND-01 ROW_NUMBER CTE; `is_manual=0`, `is_deleted=0`, `is_canceled=0`; FND-04 `COALESCE(turnover,0)>0 OR COALESCE(num_games_with_wager,0)>0` |
| **Session dedup**         | `groupby(session_id).agg(...)` + `drop_duplicates(keep='last')` by `session_end_dtm`  | FND-01: `ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC) = 1`                                               |
| **player_id placeholder** | No explicit handling                                                                  | `PLACEHOLDER_PLAYER_ID = -1`; exclude; G2: fallback to `t_session.player_id` when `session_id` valid                                                            |
| **casino_player_id**      | Not used in trainer                                                                   | CASINO_PLAYER_ID_CLEAN_SQL; D2 canonical mapping                                                                                                                |


---

## 3. Identity Resolution


| Aspect                   | Existing `trainer.py`                         | Phase 1 Plan                                                                                           |
| ------------------------ | --------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| **Grouping key**         | `player_id` only (from `t_bet` / `t_session`) | **D2 canonical_id**: `casino_player_id` (cleaned) preferred; fallback to `player_id`                   |
| **Mapping module**       | None                                          | New `identity.py`: `build_canonical_mapping()`, `resolve_canonical_id()`                               |
| **M:N handling**         | Not addressed                                 | Same card → multiple player_ids → one canonical; same player_id → multiple cards → latest by `lud_dtm` |
| **FND-12 fake accounts** | Not excluded                                  | Exclude `session_cnt=1 AND SUM(num_games_with_wager)<=1`                                               |
| **Cutoff for mapping**   | N/A                                           | Mapping built with `available_time <= cutoff_dtm` only (anti-leakage)                                  |


---

## 4. Label Construction


| Aspect                       | Existing `trainer.py`                                                                                                          | Phase 1 Plan                                                                                                             |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| **Label definition**         | `gap_to_next_min >= 30` AND `minutes_to_session_end` in [0, 15] — **session-based** (uses `session_end_dtm`, next session gap) | **Bet-based**: gap start = `b_{i+1} - b_i ≥ WALKAWAY_GAP_MIN`; label=1 if gap start exists in `[t, t+ALERT_HORIZON_MIN]` |
| **Data source for labels**   | Session `gap_to_next_min`, `minutes_to_session_end` (relies on `session_end_dtm`)                                              | `t_bet` payout times only; no session-end dependency                                                                     |
| **Right-censoring (TRN-06)** | Effectively inflates positives at window end (no extended pull)                                                                | C1 extended pull; H1: next_bet missing → censored if coverage insufficient                                               |
| **Sorting**                  | `session_id`, `payout_complete_dtm`, `bet_id` (no explicit tie-break contract)                                                 | **G3** stable sort: `ORDER BY payout_complete_dtm ASC, bet_id ASC` everywhere                                            |
| **Label module**             | Inline in `build_labels_and_features()`                                                                                        | New `labels.py`: `compute_labels(bets_df, window_end, extended_end)`                                                     |
| **Future-info guard**        | Uses `minutes_to_session_end`, `gap_to_next_min` (session-level future info)                                                   | Explicit ban: no label-derived quantities (e.g. `minutes_to_next_bet`) in features                                       |


---

## 5. Feature Engineering


| Aspect                                     | Existing `trainer.py`                                                                   | Phase 1 Plan                                                                                                                                                   |
| ------------------------------------------ | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Architecture**                           | Single monolithic block; hardcoded feature list                                         | **Dual-track**: Track A (Featuretools DFS) + Track Human (vectorized hand-written)                                                                                 |
| **Track A (automated)**                    | None                                                                                    | EntitySet: `t_bet` → `t_session` → `player`; DFS exploration (10–20% sample) → `save_features` → `calculate_feature_matrix` on full data; cutoff_time enforced |
| **Track Human (hand-written)**                 | Inline rolling (5/15/30m bet counts, 10/30m wager); `_loss_streak` per-session for-loop | `features.py`: `compute_loss_streak()`, `compute_run_boundary()`, `compute_table_hc()` — **vectorized**; shared by trainer + scorer                            |
| **loss_streak**                            | `status.upper() == "LOSE"` (TRN-09 string bug possible); PUSH not configurable          | `status='LOSE'`; PUSH behavior via `LOSS_STREAK_PUSH_RESETS`                                                                                                   |
| **run_boundary / minutes_since_run_start** | Not present                                                                             | New: `RUN_BREAK_MIN` gap → new run; vectorized                                                                                                                 |
| **table_hc (S1)**                          | Uses `session_start_dtm` / `session_end_dtm` (interval overlap) — **leakage** (TRN-05)  | Past `TABLE_HC_WINDOW_MIN` minutes, `t_bet`-based; unique players per `table_id`; `BET_AVAIL_DELAY_MIN` applied                                                |
| **minutes_to_session_end**                 | Used as input (session-end dependency)                                                  | **Removed** (S1: no session_end in features)                                                                                                                   |
| **Cutoff / leakage**                       | No explicit cutoff; rolling over full history                                           | Strict `cutoff_time` for both tracks; `session_avail_dtm <= cutoff_time` for session-based features                                                            |
| **Feature screening**                      | None; fixed list                                                                        | Two-stage: mutual info + VIF; optional LightGBM importance on train only                                                                                       |
| **Train-serve parity**                     | Scorer has its own inline logic                                                         | Shared `features.py` + `saved_feature_defs`; scorer imports same functions                                                                                     |


---

## 6. Model Architecture & Training


| Aspect                    | Existing `trainer.py`                                     | Phase 1 Plan                                                                  |
| ------------------------- | --------------------------------------------------------- | ----------------------------------------------------------------------------- |
| **Number of models**      | Single LightGBM                                           | **Two models**: Rated (card-holders) + Non-rated                              |
| **Model routing**         | N/A                                                       | `is_rated_obs = (resolved_card_id IS NOT NULL)`                               |
| **Class weighting**       | `class_weight='balanced'`                                 | Same                                 |
| **Sample weighting**      | None                                                      | None（DEC-013：已移除 visit-level 樣本加權） |
| **Hyperparameter tuning** | Small grid (`num_leaves`, `min_child_samples`); no Optuna | **Optuna TPE** on validation set; `OPTUNA_N_TRIALS=300`                       |
| **Time split**            | 80/20 (train/val)                                         | 70/15/15 (train/valid/test)                                                   |
| **Early stopping**        | `stopping_rounds=50`                                      | Plan does not specify; Optuna optimizes F-beta / AP (average precision)       |


---

## 7. Threshold Selection & Backtesting


| Aspect                 | Existing `trainer.py`                                                      | Phase 1 Plan                                                                            |
| ---------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| **Threshold search**   | Exhaustive over `np.unique(val_scores)`; `min_recall=0.02`, `min_alerts=5` | **Optuna TPE** 2D search over `(rated_threshold, nonrated_threshold)`                   |
| **Constraints**        | Ad-hoc min recall / min alerts                                             | G1: `Precision ≥ G1_PRECISION_MIN`; total alert volume `≥ G1_ALERT_VOLUME_MIN_PER_HOUR` |
| **Objective**          | Maximize precision (then recall)                                           | Maximize F-beta (β<1) subject to G1 constraints                                         |
| **Per-run TP dedup** | Not applied                                                                | Evaluation only: at most 1 TP per run; does not affect online alerting                |
| **評估指標**           | Single precision/recall                                                    | **Bet-level**（Phase 1；Run-level 延後見 DEC-012）                                      |


---

## 8. Output Artifacts


| Aspect                  | Existing `trainer.py`                                      | Phase 1 Plan                                                                                            |
| ----------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **Model file(s)**       | `walkaway_model.pkl` (single model + features + threshold) | `rated_model.pkl`, `nonrated_model.pkl`                                                                 |
| **Feature definitions** | `feature_cols` in pickle                                   | `saved_feature_defs/` (Featuretools); `feature_list.json`; `features.py` (Track Human)                      |
| **Reason codes**        | None                                                       | `reason_code_map.json`; SHAP top-k → 4 fixed codes                                                      |
| **Versioning**          | None                                                       | `model_version` (e.g. `YYYYMMDD-HHMMSS-{git_short_hash}`)                                               |
| **Atomic deployment**   | Single pkl                                                 | Bundle: models + saved_feature_defs + features.py + feature_list.json + reason_code_map + model_version |


---

## 9. Modular Structure


| Component        | Existing `trainer.py`                                | Phase 1 Plan                                                                                                                                                                                                 |
| ---------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Config**       | Sparse (`TRAINER_DAYS`, `TBET`, etc.)                | Centralized: `WALKAWAY_GAP_MIN`, `LABEL_LOOKAHEAD_MIN`, `OPTUNA_N_TRIALS`, `PLACEHOLDER_PLAYER_ID`, `TABLE_HC_WINDOW_MIN`, `LOSS_STREAK_PUSH_RESETS`, `HIST_AVG_BET_CAP`, `CASINO_PLAYER_ID_CLEAN_SQL`, etc. |
| **Time windows** | Inline `parse_window()`, `default_training_window()` | `time_fold.py`                                                                                                                                                                                               |
| **Identity**     | None                                                 | `identity.py`                                                                                                                                                                                                |
| **Labels**       | Inline in `build_labels_and_features()`              | `labels.py`                                                                                                                                                                                                  |
| **Features**     | Inline in `build_labels_and_features()`              | `features.py`                                                                                                                                                                                                |
| **Backtester**   | Imports `build_labels_and_features` from trainer     | Imports from `labels.py`, `features.py`; no trainer dependency for pipeline                                                                                                                                  |
| **Tests**        | None                                                 | `tests/`: `test_labels.py`, `test_features.py`, `test_identity.py`, `test_trainer.py`, `test_backtester.py`, `test_scorer.py`, `test_dq_guardrails.py`                                                       |


---

## 10. Key TRN / FND Remediations Addressed by Plan


| Issue                                   | trainer.py                         | Phase 1 Plan                                         |
| --------------------------------------- | ---------------------------------- | ---------------------------------------------------- |
| **TRN-01** (Session dedup)              | Uses `session_end_dtm` aggregation | FND-01 ROW_NUMBER with `lud_dtm`, `__etl_insert_Dtm` |
| **TRN-02** (`is_manual`)                | Not filtered                       | `is_manual=0` on t_session                           |
| **TRN-03** (player_id chain break)      | No canonical mapping               | D2 identity.py                                       |
| **TRN-05** (`table_hc` leakage)         | Uses session intervals             | S1: t_bet-based, no session_end                      |
| **TRN-06** (Right-censoring)            | No extended pull                   | C1 + H1 censoring                                    |
| **TRN-07** (Cache consistency)          | Simple file existence              | `(window_start, window_end, data_hash)` key          |
| **TRN-08** (Rolling boundaries)         | Inline logic                       | Shared `features.py` + cutoff_time                   |
| **TRN-09** (`loss_streak` bug)          | String comparison                  | Explicit `status='LOSE'`                             |
| **TRN-11** (Threshold too conservative) | Ad-hoc min_alerts                  | G1 + Optuna 2D search                                |


---

## 11. Summary

The Phase 1 plan represents a comprehensive refactor that:

1. **Modularizes** data loading, identity, labels, and features into separate modules with clear contracts.
2. **Closes leakage and parity gaps** via cutoff_time, C1 extended pull, S1 table_hc, and shared feature code.
3. **Introduces dual-track feature engineering** (Featuretools DFS + vectorized hand-written) with persistent `saved_feature_defs` for train-serve parity.
4. **Adds D2 canonical identity** and FND-compliant DQ guardrails.
5. **Splits models** (Rated vs. Non-rated). Phase 1 無樣本加權（DEC-013）。
6. **Uses Optuna** for both hyperparameter and 2D threshold search.
7. **Produces an atomic artifact bundle** with versioning and reason codes for deployment.

---

# Phase 1 計畫與現有 trainer.py — 比較摘要（中文譯文）

本文件摘要說明 `ssot/patron_walkaway_phase_1.plan.md` 與 `trainer/trainer.py` 在做法上的差異，表格依功能領域分組。

---

## 1. 資料載入與時間窗口

| 面向 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **資料來源** | 僅 ClickHouse（或本機 CSV 快取 `bets_buffer.csv`、`sessions_buffer.csv`） | 上線用 ClickHouse；訓練/開發可選用本機 Parquet（如 `.data/`）加速 |
| **時間窗口定義** | 臨時 `parse_window()` / `default_training_window()`；預設 7 天 | 集中式 `time_fold.py`：`get_monthly_chunks()`、`get_train_valid_test_split()`；嚴格 `[window_start, window_end)` 語義 |
| **分塊** | 單一窗口；全範圍一次載入 | 依時間分窗；逐窗處理；不一次載入全範圍 |
| **延伸拉取 (C1)** | 無；標籤計算無前瞻緩衝 | `extended_end` = window_end + max(LABEL_LOOKAHEAD_MIN, 1 天)；延伸資料僅供標籤，永不納入訓練樣本 |
| **Train/Valid/Test 切分** | 80/20 時間切分（僅 train vs. validation） | 70/15/15 時間切分，透過 `time_fold.get_train_valid_test_split()` |

---

## 2. 資料品質 (DQ) 護欄

| 面向 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **t_bet 查詢** | 僅 `wager > 0`；未要求 `payout_complete_dtm IS NOT NULL`；未排除 `player_id != -1` | `payout_complete_dtm IS NOT NULL`、`player_id != PLACEHOLDER_PLAYER_ID`；允許 `FINAL` |
| **t_session 查詢** | 簡單 `session_start_dtm` 範圍；無去重；無 `is_manual` / `is_deleted` / `is_canceled` | **禁用 FINAL**（G1）；FND-01 ROW_NUMBER CTE；`is_manual=0`、`is_deleted=0`、`is_canceled=0`；FND-04 `COALESCE(turnover,0)>0 OR COALESCE(num_games_with_wager,0)>0` |
| **Session 去重** | `groupby(session_id).agg(...)` + `drop_duplicates(keep='last')` 依 `session_end_dtm` | FND-01：`ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC) = 1` |
| **player_id 佔位符** | 無明確處理 | `PLACEHOLDER_PLAYER_ID = -1`；排除；G2：`session_id` 有效時回補 `t_session.player_id` |
| **casino_player_id** | trainer 未使用 | CASINO_PLAYER_ID_CLEAN_SQL；D2 canonical  mapping |

---

## 3. 身份歸戶

| 面向 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **分組鍵** | 僅 `player_id`（來自 t_bet / t_session） | **D2 canonical_id**：優先 `casino_player_id`（經清洗）；兜底 `player_id` |
| **Mapping 模組** | 無 | 新建 `identity.py`：`build_canonical_mapping()`、`resolve_canonical_id()` |
| **M:N 處理** | 未處理 | 同卡多 ID → 歸一 canonical；同 ID 多卡 → 取最近 `lud_dtm` |
| **FND-12 假帳號** | 未排除 | 排除 `session_cnt=1 AND SUM(num_games_with_wager)<=1` |
| **Mapping 截止時間** | 不適用 | 建 mapping 時僅使用 `available_time <= cutoff_dtm`（防洩漏） |

---

## 4. 標籤建構

| 面向 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **標籤定義** | `gap_to_next_min >= 30` 且 `minutes_to_session_end` 在 [0, 15] — **依 Session**（使用 `session_end_dtm`、下一 session 間距） | **依下注**：gap start = `b_{i+1} - b_i ≥ WALKAWAY_GAP_MIN`；label=1 表示 `[t, t+ALERT_HORIZON_MIN]` 內存在 gap start |
| **標籤資料來源** | Session 的 `gap_to_next_min`、`minutes_to_session_end`（依賴 `session_end_dtm`） | 僅 `t_bet` 派彩時間；不依賴 session 結束 |
| **右截尾 (TRN-06)** | 窗口末端實際膨脹正例（無延伸拉取） | C1 延伸拉取；H1：next_bet 缺失 → 覆蓋不足則 censored |
| **排序** | `session_id`, `payout_complete_dtm`, `bet_id`（無 tie-break 合約） | **G3** 穩定排序：全域 `ORDER BY payout_complete_dtm ASC, bet_id ASC` |
| **標籤模組** | 內建於 `build_labels_and_features()` | 新建 `labels.py`：`compute_labels(bets_df, window_end, extended_end)` |
| **未來資訊護欄** | 使用 `minutes_to_session_end`、`gap_to_next_min`（Session 層級未來資訊） | 明確禁止：任何標籤衍生量（如 `minutes_to_next_bet`）不得作為特徵 |

---

## 5. 特徵工程

| 面向 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **架構** | 單一龐大區塊；特徵清單寫死 | **雙軌**：軌道 A（Featuretools DFS）+ 軌道 B（向量化手寫） |
| **軌道 A（自動化）** | 無 | EntitySet：t_bet → t_session → player；DFS 探索（10–20% 抽樣）→ `save_features` → 全量 `calculate_feature_matrix`；嚴格 cutoff_time |
| **軌道 B（手寫）** | 內建 rolling（5/15/30m 下注數、10/30m 投注額）；`_loss_streak` 每 session for-loop | `features.py`：`compute_loss_streak()`、`compute_run_boundary()`、`compute_table_hc()` — **向量化**；trainer 與 scorer 共用 |
| **loss_streak** | `status.upper() == "LOSE"`（可能 TRN-09 字串 bug）；PUSH 無法設定 | 明確 `status='LOSE'`；PUSH 行為由 `LOSS_STREAK_PUSH_RESETS` 控制 |
| **run_boundary / minutes_since_run_start** | 無 | 新增：`RUN_BREAK_MIN` 間距 → 新 run；向量化 |
| **table_hc (S1)** | 使用 `session_start_dtm` / `session_end_dtm`（區間重疊）— **洩漏** (TRN-05) | 過去 `TABLE_HC_WINDOW_MIN` 分鐘、依 `t_bet`；每 `table_id` 不重複玩家數；扣除 `BET_AVAIL_DELAY_MIN` |
| **minutes_to_session_end** | 作為輸入（依賴 session_end） | **移除**（S1：特徵禁用 session_end） |
| **Cutoff / 洩漏** | 無 cutoff；rolling 涵蓋全歷史 | 雙軌嚴格 `cutoff_time`；session 類特徵須 `session_avail_dtm <= cutoff_time` |
| **特徵篩選** | 無；固定清單 | 兩階段：mutual info + VIF；可選 LightGBM importance（僅 train） |
| **Train-serve parity** | Scorer 自有一套邏輯 | 共用 `features.py` 與 `saved_feature_defs`；scorer 匯入相同函數 |

---

## 6. 模型架構與訓練

| 面向 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **模型數量** | 單一 LightGBM | **雙模型**：Rated（有卡客）+ Non-rated |
| **模型路由** | 不適用 | `is_rated_obs = (resolved_card_id IS NOT NULL)` |
| **類別權重** | `class_weight='balanced'` | 同上 |
| **樣本加權** | 無 | 無（DEC-013：已移除 visit-level 樣本加權） |
| **超參調優** | 小網格（`num_leaves`、`min_child_samples`）；無 Optuna | **Optuna TPE** 在 validation set；`OPTUNA_N_TRIALS=300` |
| **時間切分** | 80/20 (train/val) | 70/15/15 (train/valid/test) |
| **Early stopping** | `stopping_rounds=50` | 計畫未明訂；Optuna 以 F-beta / AP (average precision) 為目標 |

---

## 7. 閾值選擇與回測

| 面向 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **閾值搜尋** | 窮舉 `np.unique(val_scores)`；`min_recall=0.02`、`min_alerts=5` | **Optuna TPE** 對 `(rated_threshold, nonrated_threshold)` 做 2D 搜尋 |
| **約束** | 臨時 min_recall / min_alerts | G1：`Precision ≥ G1_PRECISION_MIN`；總警報量 `≥ G1_ALERT_VOLUME_MIN_PER_HOUR` |
| **目標** | 最大化 precision（其次 recall） | 在 G1 約束下最大化 F-beta（β<1） |
| **評估口徑** | 僅 precision/recall | **Bet-level**（Phase 1；Run-level 延後 DEC-012） |

---

## 8. 輸出產物

| 面向 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **模型檔** | `walkaway_model.pkl`（單一模型 + features + threshold） | `rated_model.pkl`、`nonrated_model.pkl` |
| **特徵定義** | pickle 內 `feature_cols` | `saved_feature_defs/`（Featuretools）；`feature_list.json`；`features.py`（軌道 B） |
| **Reason codes** | 無 | `reason_code_map.json`；SHAP top-k → 4 組固定代碼 |
| **版本控制** | 無 | `model_version`（如 `YYYYMMDD-HHMMSS-{git_short_hash}`） |
| **原子部署** | 單一 pkl | 整套：models + saved_feature_defs + features.py + feature_list.json + reason_code_map + model_version |

---

## 9. 模組結構

| 組件 | 現有 `trainer.py` | Phase 1 計畫 |
|------|-------------------|--------------|
| **Config** | 零散（TRAINER_DAYS、TBET 等） | 集中：`WALKAWAY_GAP_MIN`、`LABEL_LOOKAHEAD_MIN`、`OPTUNA_N_TRIALS`、`PLACEHOLDER_PLAYER_ID`、`TABLE_HC_WINDOW_MIN`、`LOSS_STREAK_PUSH_RESETS`、`HIST_AVG_BET_CAP`、`CASINO_PLAYER_ID_CLEAN_SQL` 等 |
| **時間窗口** | 內建 `parse_window()`、`default_training_window()` | `time_fold.py` |
| **Identity** | 無 | `identity.py` |
| **Labels** | 內建於 `build_labels_and_features()` | `labels.py` |
| **Features** | 內建於 `build_labels_and_features()` | `features.py` |
| **Backtester** | 自 trainer 匯入 `build_labels_and_features` | 自 `labels.py`、`features.py` 匯入；不依賴 trainer pipeline |
| **Tests** | 無 | `tests/`：`test_labels.py`、`test_features.py`、`test_identity.py`、`test_trainer.py`、`test_backtester.py`、`test_scorer.py`、`test_dq_guardrails.py` |

---

## 10. 計畫針對的 TRN / FND 修正

| 項目 | trainer.py | Phase 1 計畫 |
|------|------------|--------------|
| **TRN-01** (Session 去重) | 依 `session_end_dtm` 聚合 | FND-01 ROW_NUMBER，依 `lud_dtm`、`__etl_insert_Dtm` |
| **TRN-02** (`is_manual`) | 未過濾 | t_session 加 `is_manual=0` |
| **TRN-03** (player_id 斷鏈) | 無 canonical mapping | D2 identity.py |
| **TRN-05** (`table_hc` 洩漏) | 使用 session 區間 | S1：依 t_bet，不用 session_end |
| **TRN-06** (右截尾) | 無延伸拉取 | C1 + H1 censoring |
| **TRN-07** (快取一致性) | 僅檢查檔案存在 | key 包含 `(window_start, window_end, data_hash)` |
| **TRN-08** (Rolling 邊界) | 內建邏輯 | 共用 `features.py` 與 cutoff_time |
| **TRN-09** (`loss_streak` bug) | 字串比較 | 明確 `status='LOSE'` |
| **TRN-11** (閾值過於保守) | 臨時 min_alerts | G1 + Optuna 2D 搜尋 |

---

## 11. 摘要

Phase 1 計畫為一次完整重構，涵蓋：

1. **模組化**：資料載入、身份、標籤、特徵拆成獨立模組，介面清楚。
2. **封閉洩漏與 parity 破口**：透過 cutoff_time、C1 延伸拉取、S1 table_hc、共用特徵程式碼。
3. **導入雙軌特徵工程**（Featuretools DFS + 向量化手寫），以持久化 `saved_feature_defs` 維持 train-serve parity。
4. **納入 D2 canonical 身份**與符合 FND 的 DQ 護欄。
5. **模型拆為 Rated / Non-rated**。Phase 1 無樣本加權（DEC-013）。
6. **以 Optuna** 同時做超參與 2D 閾值搜尋。
7. **產出原子化 artifact 套件**，含版本與 reason codes，供部署使用。

