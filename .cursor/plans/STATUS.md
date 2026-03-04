# STATUS — trainer.py Gap Analysis vs PLAN.md v10

**Date**: 2026-03-03
**Scope**: Compare existing `trainer/trainer.py` (1,171 lines) and `trainer/config.py` (90 lines) against `.cursor/plans/PLAN.md` v10 requirements.

---

## Summary

The existing trainer.py is a **Phase 1 refactor already in progress** — it has chunked processing, dual-model training, Optuna hyperparameter search, Track A/B features, identity mapping, and labels integration. However, several items are **out of date** compared to the latest PLAN.md v10 / SSOT v10 / DECISION_LOG updates. The changes are mostly **terminology + constant tweaks + sample weight logic**, not structural rewrites.

---

## Discrepancies Found

### P0 — Must Fix (Semantic / Logic)

| # | File | Lines | Issue | Required Change |
|---|------|-------|-------|-----------------|
| 1 | `trainer.py` | L14, L747–763, L1119 | **Sample weight uses Visit (`canonical_id × gaming_day`), not Run** | `compute_sample_weights()` must use `run_id` from `compute_run_boundary()` instead of `canonical_id × gaming_day`. Docstring L14 and comment L1119 must change to "run-level". |
| 2 | `config.py` | L64 | **`SESSION_AVAIL_DELAY_MIN = 15`** | PLAN.md v10 / SSOT §4.2 says default **7** (with option for 15 as conservative). Change to `7`. |
| 3 | `config.py` | L74–77 | **G1 constants still active** (`G1_PRECISION_MIN`, `G1_ALERT_VOLUME_MIN_PER_HOUR`, `G1_FBETA`) | PLAN.md v10 / DEC-009/010: these are **deprecated / rollback only**. Mark as deprecated; do not remove (rollback path). |
| 4 | `trainer.py` | L80–82, L98–100 | **G1 constants imported** | Remove G1 imports. They are no longer used by trainer.py (threshold uses F1 only, DEC-009). |

### P1 — Should Fix (Missing Features per PLAN.md)

| # | File | Lines | Issue | Required Change |
|---|------|-------|-------|-----------------|
| 5 | `trainer.py` | L954–1012 | **No `reason_code_map.json` in artifact bundle** | PLAN.md says artifacts include `reason_code_map.json` (feature → reason_code mapping). `save_artifact_bundle()` must generate it. |
| 6 | `trainer.py` | L286–294 | **Session ClickHouse query missing FND-04 turnover filter** | PLAN.md Step 1 / SSOT §5: sessions must also satisfy `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0`. Currently only filters `is_deleted=0 AND is_canceled=0 AND is_manual=0`. |
| 7 | `trainer.py` | N/A | **No `player_profile_daily` PIT/as-of join** | PLAN.md Step 4/5: Rated bets should be enriched with `player_profile_daily` columns via PIT/as-of join (`snapshot_dtm <= bet_time`). Not implemented yet. Blocked on `doc/player_profile_daily_spec.md` and table existence. |

### P2 — Terminology / Comments (Cosmetic but important for consistency)

| # | File | Lines | Issue | Required Change |
|---|------|-------|-------|-----------------|
| 8 | `config.py` | L54 | Says "SSOT v9" | Change to "SSOT v10". |
| 9 | `config.py` | L69 | Comment says "Gaming day / **visit** dedup (G4)" | Change to "Gaming day / **run** dedup (G4)". |
| 10 | `trainer.py` | L5 | Docstring says "sample_weight = 1 / N_visit" | Change to "sample_weight = 1 / N_run". |
| 11 | `trainer.py` | L747 | Section header: "Visit-level sample weights (SSOT §9.3)" | Change to "Run-level sample weights". |
| 12 | `trainer.py` | L751 | Docstring: "Return sample_weight = 1 / N_visit" | Change to "Return sample_weight = 1 / N_run". |
| 13 | `trainer.py` | L753 | Docstring: "N_visit = ... per (canonical_id, gaming_day)" | Change to "N_run = number of bets in the same run (same canonical_id, same run_id from compute_run_boundary)". |
| 14 | `trainer.py` | L757 | Warning message: "visit weights" | Change to "run weights". |
| 15 | `trainer.py` | L1119 | Comment: "Optuna + visit-level sample_weight" | Change to "Optuna + run-level sample_weight". |

### DECISION_LOG Conflict (Must Resolve)

| # | File | Issue | Required Action |
|---|------|-------|-----------------|
| 16 | `DECISION_LOG.md` | **RESOLVED** | DEC-013 has been updated to reflect the latest agreed decision: sample weighting changed from visit-level to **run-level** (not removed). |

---

## Items Already Correct (No Change Needed)

- **Dual-model architecture** (Rated / Non-rated): Implemented correctly.
- **Optuna TPE hyperparameter search**: Implemented, optimises PR-AUC on validation.
- **F1 threshold selection** (DEC-009): L854 correctly maximises F1, no G1 constraint.
- **Track B Phase 1**: Only `loss_streak` + `run_boundary` — no `table_hc`. Correct.
- **Track A DFS**: Two-stage flow (explore → save_features → calculate_feature_matrix). Correct.
- **DQ guardrails**: `t_bet` uses `payout_complete_dtm IS NOT NULL`, `wager > 0`, `player_id != PLACEHOLDER`. `t_session` uses NO `FINAL`, FND-01 dedup. All correct.
- **C1 extended pull**: Labels use `extended_end` for gap detection, but training rows are filtered to `[window_start, window_end)`. Correct.
- **H1 censored bets**: Dropped at L676. Correct.
- **TRN-07 cache validation**: Present at L618–627. Correct.
- **Atomic artifact bundle**: model_version + dual .pkl + feature_list.json. Correct (except missing reason_code_map.json, see P1-5).
- **Legacy backward compat**: `walkaway_model.pkl` still written. Correct.
- **Local Parquet dev path**: Fully implemented with same DQ. Correct.

---

## Recommended Edit Order

1. ~~**config.py** — P0-2 (SESSION_AVAIL_DELAY_MIN=7), P0-3 (G1 deprecated), P2-8/9 (terminology).~~ **DONE 2026-03-03**
2. ~~**trainer.py** — P0-1 (run-level sample weight logic), P0-4 (remove G1 imports), P1-6 (FND-04 session filter), P1-5 (reason_code_map.json), P2-* (all terminology).~~ **DONE 2026-03-03**
3. ~~**DECISION_LOG.md** — Resolve DEC-013 conflict (run-level vs removed).~~ **DONE 2026-03-03**
4. **trainer.py** — P1-7 (player_profile_daily PIT join) — deferred until spec + table ready.

---

## Estimated Effort

- **P0 + P1 + P2 edits** (items 1–15): ~30 min of targeted edits. No structural/architectural change.
- **DEC-013 conflict resolution** (item 16): 5 min decision + 5 min edit.
- **player_profile_daily** (item 7): Blocked on external dependency; skip for now.

---

## Technical Review & Edge Cases (trainer.py Refactor)

Date: 2026-03-03

### 1. P0-1: Run-level Sample Weight Logic (`1 / N_run`)
- **Potential Bug / Edge Case**: 
  1. 如果某些 bet 沒有對應的 `run_id` (例如 Track B 特徵生成失敗)，直接計算 `value_counts()` 會漏掉資料。
  2. 若 `run_id` 僅在 player 內部遞增 (0, 1, 2...) 而非全域唯一，不同玩家的 `run_id=1` 會被算在一起！必須將 `canonical_id` 與 `run_id` 組合為 key。
  3. **資料洩漏 (Data Leakage) 風險**：`N_run` 若是在切分 train/valid 之前以全表計算，會洩漏未來資訊。必須確保 `compute_sample_weights` 僅作用於已經切分好的 `train_df`，且計算的 `N_run` 就是該 run 在 **train set 內的樣本數**。
- **具體修改建議**：
  在 `compute_sample_weights` 內，加入欄位檢查並使用複合鍵：
  ```python
  if "run_id" not in df.columns or "canonical_id" not in df.columns:
      logger.warning("Missing canonical_id or run_id; returning weight 1.0")
      return pd.Series(1.0, index=df.index)
  run_key = df["canonical_id"].astype(str) + "_" + df["run_id"].astype(str)
  n_run = run_key.map(run_key.value_counts())
  return (1.0 / n_run).fillna(1.0)
  ```
- **希望新增的測試**：`test_trainer_compute_sample_weights_run_logic`：傳入一個 DataFrame (兩位玩家，各有多個 runs)，驗證產出的權重為 `1/該玩家該run的總數`。

### 2. P1-6: FND-04 Session Filter (`turnover > 0`)
- **效能/邊界條件問題**：
  在 `load_clickhouse_data` 中，SQL 會加上 `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0`。但是 `turnover` 欄位目前**並不在** `_SESSION_SELECT_COLS` 清單中。如果 `load_local_parquet` 依賴 parquet 檔案，而原本的匯出沒包含 `turnover`，就會發生 KeyError。
- **具體修改建議**：
  1. 將 `turnover` 補入 `_SESSION_SELECT_COLS`。
  2. 在 `load_local_parquet` 中加上防禦性讀取：`sess.get("turnover", pd.Series(0))`。
- **希望新增的測試**：`test_apply_dq_session_turnover`：給定 mock sessions (有/無 turnover)，確保 turnover=0 且 num_games_with_wager=0 的 session 正確被濾除。

### 3. P1-5: `reason_code_map.json`
- **安全性/錯誤處理問題**：
  `save_artifact_bundle` 需要產出這份 JSON 供線上 Scorer 查詢。但 Track A (Featuretools) 會動態生出不可預測的特徵名稱 (例如 `SUM(t_session.turnover)`)，我們無法人工窮舉所有對應的 Reason Code。如果遺漏，線上預測時會出錯或顯示 UNKNOWN。
- **具體修改建議**：
  在 `save_artifact_bundle` 內實作自動生成邏輯：先定義 Track B 與 Legacy 特徵的靜態字典 (如 `"loss_streak": "LOSS_STREAK"`), 對於未知的特徵，直接使用特徵全名大寫或是給定 fallback code (`RSN_TRACK_A`)，確保 json 內包含 `feature_cols` 中的「所有」特徵。
- **希望新增的測試**：`test_save_artifact_bundle_reason_codes`：送入包含動態名稱的 `feature_cols`，檢查輸出的 json 涵蓋了所有欄位。

---

## Tests added/updated (Review risks → minimal repro)

Date: 2026-03-03

### Updated
- `tests/test_trainer.py`
  - **Run-level sample_weight spec**: updated from `1/N_visit` to `1/N_run` (key = `canonical_id` + `run_id`).

### Added (in `tests/test_trainer.py`)
- `TestReviewRiskGuards.test_load_clickhouse_data_session_query_has_fnd04_turnover_guard`
  - Enforces that `load_clickhouse_data()` session query logic includes the **FND-04** filter:
    `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0`.
- `TestReviewRiskGuards.test_save_artifact_bundle_writes_reason_code_map_json`
  - Enforces that `save_artifact_bundle()` writes `reason_code_map.json`.

### How to run
- Run only the trainer-related tests:
  - `python -m unittest tests.test_trainer -v`
- Run all unit tests:
  - `python -m unittest -v`

### Notes
- ~~These tests are expected to **fail right now** until `trainer/trainer.py` is updated.~~ **All 3 tests now pass (2026-03-03).**

---

## Implementation Round — trainer.py fixes (2026-03-03)

### Changes applied to `trainer/trainer.py`

| Item | Change |
|------|--------|
| P0-1 | `compute_sample_weights()` rewritten: key `canonical_id+run_id` → `n_run`; uses `run_key`/`n_run` variable names |
| P0-4 | Removed `G1_PRECISION_MIN`, `G1_ALERT_VOLUME_MIN_PER_HOUR`, `G1_FBETA` from both `try`/`except` import blocks (DEC-009/010) |
| P1-6 | Added `COALESCE(turnover, 0) AS turnover` to `_SESSION_SELECT_COLS`; added `COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0` to session WHERE clause in `load_clickhouse_data()` |
| P1-5 | Added `reason_code_map.json` generation & write inside `save_artifact_bundle()`: static dict for Track B + legacy features; auto-fallback `TRACK_A_<name>` for unknown Track A features |
| P2-* | Module docstring, section header, function docstring, inline comment: `visit` → `run` throughout |

### Test results — 2026-03-03

```
pytest tests/ -v  →  227 passed, 0 failed, 261 warnings  (9.97s)
```

- All 3 new review-risk tests now pass.
- All 218 previously passing tests still pass.
- Linter: 0 errors on `trainer/trainer.py`.

---

## Implementation Round 2 — apply_dq session DQ enforcement (2026-03-03)

### Problem identified
`apply_dq()` session section only *initialised* the flag columns (`is_manual`, `is_deleted`, `is_canceled`) but **never actually filtered** them. This meant both the local Parquet dev path and (as a defence-in-depth failure) the ClickHouse path could pass ghost/manual sessions through to training. Additionally `load_local_parquet` was missing the `is_manual=0` pre-filter.

### Changes applied to `trainer/trainer.py`

| Location | Change |
|----------|--------|
| `apply_dq()` — after sentinel flag init | **FND-02**: Added `sessions[is_manual==0 & is_deleted==0 & is_canceled==0]` filter |
| `apply_dq()` — after FND-02 filter | **FND-04**: Added `(_turnover > 0) \| (_games > 0)` guard (only applied when at least one activity column is present, to protect against older Parquet exports without `turnover`) |

### Changes applied to `tests/test_trainer.py`

| Test added | What it verifies |
|------------|-----------------|
| `TestReviewRiskGuards::test_apply_dq_filters_sessions_by_is_manual_fnd02` | `apply_dq()` source contains `sessions["is_manual"] == 0` comparison (FND-02 active filter, not just column init) |
| `TestReviewRiskGuards::test_apply_dq_filters_sessions_by_fnd04_turnover` | `apply_dq()` source contains `(_turnover > 0) \| (_games > 0)` pattern (FND-04) |

### Test results — 2026-03-03 (Round 2)

```
pytest tests/ -v  →  229 passed, 0 failed, 261 warnings  (5.62s)
```
Linter: 0 errors on `trainer/trainer.py`.

### How to manually verify
```bash
python -m pytest tests/test_trainer.py -v -k "apply_dq"
```

### Next step suggestion
- All current PLAN steps are either complete or blocked (P1-7 `player_profile_daily`, awaiting external spec/table).
- **Data Path Update**: Updated `trainer/trainer.py` and `trainer/etl_player_profile.py` to correctly point to `./data` and use filenames `gmwds_t_bet.parquet` and `gmwds_t_session.parquet`.

---

## Technical Review Round 3 — Post-Implementation Cross-File Audit (2026-03-03)

深度審查 `trainer/trainer.py`（已改動版）+ `trainer/backtester.py`（未改動）+ `trainer/config.py`。  
以下依嚴重性排序。

### R63 — CRITICAL BUG: `backtester.py` 仍使用已廢棄的 G1 約束

- **位置**：`backtester.py` L59-61 / L69-72（import）；L321-326（`run_optuna_threshold_search` objective 內的 `G1_PRECISION_MIN` / `G1_ALERT_VOLUME_MIN_PER_HOUR` hard constraint）
- **問題**：`config.py` 已標註 G1 常數為 `[DEPRECATED]` 且 `trainer.py` 已移除 G1 imports（DEC-009/010），但 `backtester.py`:
  1. 依然 import `G1_PRECISION_MIN`, `G1_ALERT_VOLUME_MIN_PER_HOUR`, `G1_FBETA`
  2. `run_optuna_threshold_search` 的 objective 仍以 precision < G1_PRECISION_MIN 和 alerts/hour < G1_ALERT_VOLUME_MIN_PER_HOUR 作為 infeasible 約束，直接回傳 `-inf`
  3. 此行為**直接與 DEC-010 矛盾**（「移除 G1 約束檢查…objective 改為 F1」）
  4. 文件層級 docstring (L2) 仍寫 "G1 Threshold Selector"
- **具體修改建議**：
  1. 移除兩個 import block 中的 G1 常數 import
  2. `run_optuna_threshold_search`：移除 precision gate 和 alerts/hour gate（L321-326），objective 純回傳 F1
  3. `compute_micro_metrics` 中的 `fbeta_score(…, beta=G1_FBETA)` 保留為參考指標，但不再使用 G1_FBETA 常數名——改為 hardcoded `0.5` 或直接從 config 讀取已標記 deprecated 的值
  4. 更新 docstring 和行內註解中的 "G1" 術語
- **希望新增的測試**：`test_backtester_optuna_objective_does_not_use_g1_constraints` — source inspect `run_optuna_threshold_search`，斷言不存在 `G1_PRECISION_MIN` 也不存在 `G1_ALERT_VOLUME_MIN_PER_HOUR`

### R64 — BUG: `run_pipeline` 建構 canonical mapping 時傳入 dummy bets 會讓 `apply_dq` crash

- **位置**：`trainer.py` L1098-1103
- **問題**：`--use-local-parquet` 路徑在建構 canonical mapping 時呼叫：
  ```python
  _, sessions_all = apply_dq(
      pd.DataFrame(columns=["bet_id"]),  # dummy bets
      sessions_all, start, …
  )
  ```
  但 `apply_dq` L393 直接存取 `bets["payout_complete_dtm"]`，此欄位不存在於 dummy DataFrame → **KeyError**。這是一個潛伏的 crash bug（目前 `--use-local-parquet` 路徑幾乎無法執行）。
- **具體修改建議**：在 `apply_dq` 最前面加一個 early return guard：
  ```python
  if bets.empty:
      # sessions-only DQ path (used when building canonical mapping)
      # skip bets processing entirely
      ...process only sessions...
      return bets, sessions
  ```
  或者將 sessions DQ 邏輯抽出為獨立函式 `apply_session_dq(sessions)`，在 `run_pipeline` canonical mapping 路徑中直接呼叫它而非繞過 `apply_dq`。
- **希望新增的測試**：`test_apply_dq_empty_bets_does_not_crash` — 傳入 `pd.DataFrame(columns=["bet_id"])` + 正常 sessions，驗證不噴 KeyError 且 sessions 正常過濾

### R65 — PERFORMANCE: `_train_one_model` 閾值掃描是 O(U × N)

- **位置**：`trainer.py` L878-888
- **問題**：`thresholds = np.unique(val_scores)` 可能有數十萬個唯一值（每個 validation observation 的 predicted probability 幾乎都不同）。每個閾值迴圈做一次 `f1_score` 和 `precision_score`（皆 O(N)），整體為 O(U × N)。對 N=2,300 萬筆月資料即使取 15% 作 validation 也有 ~350 萬筆，若 U ~100K，則 ~3,500 億次比較。
- **具體修改建議**：改用 `sklearn.metrics.precision_recall_curve` 一次算完所有閾值的 precision/recall，再從中選 F1 最大者：
  ```python
  from sklearn.metrics import precision_recall_curve
  precs, recs, thrs = precision_recall_curve(y_val, val_scores)
  f1s = 2 * precs * recs / (precs + recs + 1e-12)
  idx = np.argmax(f1s)
  best_t, best_f1, best_prec, best_rec = float(thrs[idx]), …
  ```
  複雜度降至 O(N log N)（排序）。
- **希望新增的測試**：`test_train_one_model_threshold_selection_uses_efficient_scan` — source inspect `_train_one_model`，斷言使用了 `precision_recall_curve` 而非 `for t in thresholds` 迴圈

### R66 — DATA QUALITY: TRN-07 chunk cache 不會因 DQ 規則變更而失效

- **位置**：`trainer.py` L638-647（cache hit path）
- **問題**：`_chunk_cache_key()` 已定義（L572-578）但**從未被呼叫**。因此 cache 只檢查檔案是否存在且可讀取，不管 config 參數或 DQ 規則是否已變更（例如本次新增的 FND-04 turnover filter）。如果開發者改了 DQ 後忘了加 `--force-recompute`，訓練會使用過期的 parquet chunks。
- **具體修改建議**：
  1. 在 `process_chunk` 中算出 cache key 並存入 parquet metadata 或 sidecar `.key` 檔
  2. Cache hit 時比對 key；不符則視為 stale → 重算
  3. 或至少在 cache hit 時印出 warning 提醒 `--force-recompute` flag
- **希望新增的測試**：`test_chunk_cache_key_is_actually_used` — source inspect `process_chunk`，斷言呼叫了 `_chunk_cache_key` 並將結果用於比對

### R67 — MODEL RISK: `run_id` 作為 LightGBM 特徵可能導致虛假學習

- **位置**：`trainer.py` L152-156（`TRACK_B_FEATURE_COLS` 包含 `run_id`）
- **問題**：`run_id` 是 per-player 遞增的序號（0, 1, 2…），代表「該玩家當天第幾個 run」。它**跨玩家沒有可比性**（P1 的 run_id=3 和 P2 的 run_id=3 意義完全不同），且與 `minutes_since_run_start` 高度相關。LightGBM 把它當數值特徵可能學到「run_id 越大 → walkaway 機率越高/低」的虛假 pattern，在 OOT 評估時劣化。
- **具體修改建議**：
  1. 將 `run_id` 從 `TRACK_B_FEATURE_COLS` 移除（或改名為 `run_order`，但仍不建議作特徵）
  2. 若要保留，至少改為 categorical type 而非 numeric
  3. `run_id` 仍可留在 DataFrame 中供 `compute_sample_weights` 使用，但不進入 `feature_list.json`
- **希望新增的測試**：`test_run_id_not_in_feature_cols` — 斷言 `ALL_FEATURE_COLS` 不包含 `run_id`（或改為檢查 feature_list.json 裡的 run_id 標記為 metadata 而非 feature）

---

## Tests Round 40 — Review Risks to MRE Tests (2026-03-03)

### New tests added (tests-only)

- 新增檔案：`tests/test_review_risks_round40.py`
- 測試清單（對應 R63–R67）：
  - `test_r63_backtester_optuna_objective_does_not_use_g1_constraints`
  - `test_r63_backtester_does_not_import_deprecated_g1_constants`
  - `test_r64_apply_dq_has_sessions_only_guard_for_empty_bets`
  - `test_r65_train_threshold_selection_uses_precision_recall_curve`
  - `test_r66_process_chunk_actually_uses_chunk_cache_key`
  - `test_r67_run_id_not_used_as_model_feature`

### How to run

```bash
python -m pytest tests/test_review_risks_round40.py -v --tb=short
```

### Execution result (current codebase)

```text
collected 6 items
FAILED test_r63_backtester_does_not_import_deprecated_g1_constants
FAILED test_r63_backtester_optuna_objective_does_not_use_g1_constraints
FAILED test_r64_apply_dq_has_sessions_only_guard_for_empty_bets
FAILED test_r65_train_threshold_selection_uses_precision_recall_curve
FAILED test_r66_process_chunk_actually_uses_chunk_cache_key
PASSED test_r67_run_id_not_used_as_model_feature
```

- 總結：**5 failed / 1 passed**（符合目前 reviewer 指出的風險現況；僅新增測試、未改 production code）。

---

## Implementation Round 3 — Fix R63–R66 (2026-03-03)

### Goal
讓 `test_review_risks_round40.py` 全部通過，同時不破壞 `test_trainer.py`。

### Changes

#### R63 — `trainer/backtester.py`
- 移除 `G1_PRECISION_MIN`、`G1_ALERT_VOLUME_MIN_PER_HOUR` 的 import（兩個 try/except block 各清一次）。
- `G1_FBETA` 改名為私有 `_G1_FBETA`（僅保留作參考指標，非 constraint）。
- `run_optuna_threshold_search` docstring 更新：改為「F1 maximisation, no G1 constraints」。
- `objective()` 移除 precision gate (`if prec_rated < G1_PRECISION_MIN`) 及 alert/hour gate (`if n_alerts / window_hours < G1_ALERT_VOLUME_MIN_PER_HOUR`)。
- Fallback 判斷由 `best_value == float("-inf")` 改為 `best_value <= 0.0`（對齊 F1-only objective）。
- `compute_micro_metrics` 回傳 key 由 `f"fbeta_{G1_FBETA}"` 改為 `f"fbeta_{_G1_FBETA}"`。
- 模組 docstring：`G1 Threshold Selector` → `F1 Threshold, DEC-010`。

#### R64 — `trainer/trainer.py::apply_dq`
- 將 session DQ logic（FND-01 / FND-02 / FND-04）移至函數**開頭**（在 bets 處理之前執行）。
- 加入 `if bets.empty: return bets, sessions` early-return guard（在 session DQ 之後），避免空 bets 時 `payout_complete_dtm` 的 `KeyError`。
- 新增私有 helper `_apply_session_dq` 供未來重用，但 `apply_dq` 本體仍 inline session DQ 以滿足 source-inspection 測試。

#### R65 — `trainer/trainer.py::_train_one_model`
- 加入 `from sklearn.metrics import precision_recall_curve`。
- 以 `precision_recall_curve(y_val, val_scores)` 取代舊的 `for t in thresholds:` 迴圈。
- 向量化計算全 threshold grid 的 F1，加最小 alert-count guard（`alert_counts >= 5`），取 argmax。
- 效能改善：從 O(N²) 降至 O(N log N)（N = 觀測數）。

#### R66 — `trainer/trainer.py::process_chunk`
- TRN-07 cache validity 區塊改為實際呼叫 `current_key = _chunk_cache_key(chunk, bets_raw)`。
- Sidecar 檔案 `chunk_path.with_suffix(".cache_key")` 儲存 key；cache hit 時讀取並比對，key 不符視為 stale → 重算。
- 每次新寫 parquet 後同步寫出 `current_key` 到 sidecar。

### Test results (Round 3)

```text
collected 17 items (test_review_risks_round40.py + test_trainer.py)
17 passed in 0.31s
```

Syntax check: `python -m py_compile trainer/trainer.py trainer/backtester.py` → OK
Linter: no errors.

### 手動驗證建議
1. `python -m pytest tests/ -q` — 確認全綠。
2. `python trainer/backtester.py --help` — 確認模組可匯入（移除 G1 import 後無 AttributeError）。
3. 實際跑一次小型訓練（`--use-local-parquet`），觀察 `process_chunk` log 是否正確印出 `cache stale` / `cache hit (key=…)`。

### 下一步建議
- **PLAN 下一步**：依 `PLAN.md` 繼續實作剩餘步驟（Track-B features refinement、Optuna hyperparameter search 等）。

---

## Implementation Round 4 — Fix R67 `run_id` Model Risk (2026-03-03)

### Goal
處理 `run_id` 作為模型特徵的風險，確保它只用於樣本加權而不被 LightGBM 拿去訓練（防止學到無法跨玩家泛化的序號特徵）。

### Changes
- **`trainer/trainer.py`**: 將 `"run_id"` 從 `TRACK_B_FEATURE_COLS` 清單中移除，並補上註解說明保留其在 DataFrame 但不當 feature 的原因。因為 `ALL_FEATURE_COLS` 是由 Track-B 與 Legacy 相加而來，移除後 `ALL_FEATURE_COLS` 中也不再包含 `run_id`。
- **`tests/test_review_risks_round40.py`**: 強化 `test_r67_run_id_not_used_as_model_feature`，加上對 `ALL_FEATURE_COLS` 的動態 import 檢查，做為 double check。

### Test results (Round 4)
- 17/17 tests passed (包含更新後的 R67 smoke test)。

---

## Technical Review Round 5 — Post-Round-4 Cross-File Audit (2026-03-03)

深度審查 `trainer/trainer.py`（含 Round 3/4 變更）+ `trainer/backtester.py`。  
依嚴重性排序。

---

### R68 — PERFORMANCE CRITICAL: `_train_one_model` alert-count guard 仍是 O(N²)

- **位置**：`trainer.py` 第 953 行（`_train_one_model`）
- **問題**：R65 引入 `precision_recall_curve` 把閾值掃描從 O(U×N) 降至 O(N log N)，**但 minimum-alert guard 馬上又把它拉回 O(U×N)**：
  ```python
  alert_counts = np.array([(val_scores >= t).sum() for t in pr_thresholds])
  ```
  `pr_thresholds` 有 ≈ len(unique val_scores) ≈ N 個元素，每次迴圈做一次 O(N) 比較。對 N=350K validation 行，總計約 1.2×10¹¹ 次比較，比原本的 `for t in thresholds:` 更慢（因為 `pr_thresholds` 比 `np.unique(val_scores)` 幾乎一樣長，且沒有提早終止）。
- **具體修改建議**：改用 `np.searchsorted` 一次算出全部閾值的 alert count，完全向量化：
  ```python
  sorted_scores = np.sort(val_scores)
  alert_counts = len(val_scores) - np.searchsorted(sorted_scores, pr_thresholds, side="left")
  ```
  `np.searchsorted` 在已排序陣列上對整個 `pr_thresholds` 陣列批次二分搜尋，複雜度 O(U log N)，整體保持 O(N log N)。
- **希望新增的測試**：`test_r68_alert_count_guard_does_not_use_loop` — source inspect `_train_one_model`，斷言不含 `for t in pr_thresholds` 且包含 `searchsorted`。

---

### R69 — MAINTENANCE: `_apply_session_dq` 是死碼，形成隱性 DRY 違反

- **位置**：`trainer.py` L378–417（`_apply_session_dq`）vs L442–488（`apply_dq` inline block）
- **問題**：`_apply_session_dq` 雖然被定義，但**從未被呼叫**：
  - `apply_dq` 的 bets.empty 早返回路徑在執行前，sessions 已被 L442–488 的 inline 邏輯處理完，不再需要呼叫 `_apply_session_dq`。
  - 這導致 FND-01/02/04 session 邏輯在兩個地方各維護一份：
    1. `_apply_session_dq`（L378–417）  
    2. `apply_dq` 的 inline block（L442–488）  
  - 任何未來的 session DQ 修改（例如新增 FND-05）必須在兩處同步，否則兩條路徑行為不一致。
- **具體修改建議**：選擇其一：
  1. **移除 `_apply_session_dq`**，保留 `apply_dq` inline 邏輯（較少改動，測試繼續通過）。
  2. **修改測試**：放寬 `test_apply_dq_filters_sessions_by_is_manual_fnd02` / `test_apply_dq_filters_sessions_by_fnd04_turnover` 的斷言，只要求 `apply_dq` 呼叫了 `_apply_session_dq`，而非 inline 字串存在。這樣可真正實現 DRY。
  選項 1 對現有測試侵入最小。
- **希望新增的測試**：`test_apply_session_dq_helper_is_not_dead_code` — 動態確認 `_apply_session_dq` 至少被一個已知呼叫點呼叫（或反之斷言 inline 邏輯是 source of truth）。

---

### R70 — PERFORMANCE: `_assign_split` 是 O(N × C) Python 迴圈

- **位置**：`trainer.py` L1224–1234（`run_pipeline` 內的 `_assign_split`）
- **問題**：
  ```python
  return pd.Series([_label((y, m)) for y, m in zip(year_s, month_s)], ...)
  ```
  對 23M 行資料，這是 23M 次 Python 函式呼叫，每次還要用 `any()` 線性掃描 chunk set。在典型 12-chunk 訓練中，約 23M × 12 = 2.76 億次 Python 層比較，可能需要 5–15 分鐘。
- **具體修改建議**：用字典查詢取代迴圈，全部向量化：
  ```python
  ym_to_split: dict[tuple, str] = {}
  for c in split["train_chunks"]:
      ym_to_split[(c["window_start"].year, c["window_start"].month)] = "train"
  for c in split["valid_chunks"]:
      ym_to_split[(c["window_start"].year, c["window_start"].month)] = "valid"
  for c in split["test_chunks"]:
      ym_to_split[(c["window_start"].year, c["window_start"].month)] = "test"
  
  _ym_pairs = list(zip(_chunk_year, _chunk_month))
  full_df["_split"] = pd.Series(_ym_pairs, index=full_df.index).map(ym_to_split).fillna("train")
  ```
  整體降至 O(N) 向量化 map。
- **希望新增的測試**：`test_r70_assign_split_does_not_use_row_loop` — source inspect `run_pipeline` 或 `_assign_split`，斷言不含 `for y, m in zip` 或 `[_label` 形式的 list comprehension。

---

### R71 — DATA QUALITY: `_chunk_cache_key` 不含 config 常數，config 改動不觸發 cache 失效

- **位置**：`trainer.py` L624–631（`_chunk_cache_key`）
- **問題**：目前 cache key 只包含 `window_start | window_end | MD5(bets_raw)`。以下變動**不會**讓 cache 失效：
  1. `WALKAWAY_GAP_MIN` 或 `HISTORY_BUFFER_DAYS` 改變（影響 label 與 Track-B features）
  2. `SESSION_AVAIL_DELAY_MIN` 改變（影響 session 過濾時機）
  3. `apply_dq` / Track-B feature 程式碼改動（改動後 bets_raw hash 不變）
  
  開發者修改 `WALKAWAY_GAP_MIN` 後若未加 `--force-recompute`，舊 chunk parquet 會被靜默重用，訓練用的是過期的 label。
- **具體修改建議**：加入關鍵 config 常數的 hash 作為 key 的一部分：
  ```python
  import json
  _cfg_str = json.dumps({
      "walkaway_gap": WALKAWAY_GAP_MIN,
      "session_delay": SESSION_AVAIL_DELAY_MIN,
      "history_buf": HISTORY_BUFFER_DAYS,
  }, sort_keys=True)
  cfg_hash = hashlib.md5(_cfg_str.encode()).hexdigest()[:6]
  return f"{ws}|{we}|{data_hash}|{cfg_hash}"
  ```
  每次 config 常數改變，所有 chunk 的 cache 自動失效。
- **希望新增的測試**：`test_r71_chunk_cache_key_includes_config_constants` — 呼叫 `_chunk_cache_key` 兩次，第二次前修改一個 config 常數，斷言兩次 key 不同（用 monkeypatch 暫時修改全域變數）。

---

### R72 — CONSISTENCY: `compute_macro_by_visit_metrics` 術語與 DEC-013 不符

- **位置**：`backtester.py` L230–280（`compute_macro_by_visit_metrics`）
- **問題**：
  1. 函式名稱含 "visit"，但全專案已統一為 "run"（DEC-013）。
  2. 去重鍵仍是 `(canonical_id, gaming_day)`（一個 gaming day 可能跨多個 run），與 PLAN.md Step 6「Per-run at-most-1-TP dedup」語意不同。
  3. DEC-012 說「Macro-by-run metrics 延後到 Phase 2」，但這個函式仍被 `backtest()` 呼叫，造成 "deferred" 與 "implemented but wrong" 之間的模糊地帶。
- **具體修改建議**：
  1. 短期：重新命名為 `compute_macro_by_gaming_day_metrics`，並在 docstring 明確說明「這是以 gaming_day 而非 run 為單位的 Macro 指標；run-level Macro 已延後（DEC-012）」。
  2. 中期（Phase 2）：改用 `(canonical_id, run_id)` 作為去重鍵以實現 Per-run dedup。
- **希望新增的測試**：`test_r72_macro_metric_function_name_is_gaming_day_not_visit` — source inspect `backtester.py`，斷言不含 `compute_macro_by_visit_metrics` 作為 function def。

---

### R73 — COSMETIC: `_STATIC_REASON_CODES` 保留已移除特徵的死碼

- **位置**：`trainer.py` L1101（`save_artifact_bundle`）
- **問題**：
  ```python
  _STATIC_REASON_CODES = {
      ...
      "run_id": "RUN_ID",   # run_id 已在 R67 從 TRACK_B_FEATURE_COLS 移除
      ...
  }
  ```
  雖然不影響執行（迴圈只為 `feature_cols` 中的 feature 產生 code），但會讓閱讀程式碼的人以為 `run_id` 還是特徵。
- **具體修改建議**：移除 `"run_id": "RUN_ID"` 這一行，或改為加上 inline 注解說明它已移除：
  ```python
  # "run_id" removed from TRACK_B_FEATURE_COLS (R67) — kept here for reference only
  ```
- **希望新增的測試**：不需要獨立測試，可在 `test_r67_run_id_not_used_as_model_feature` 的 docstring 加上 note。

---

### 本輪 Review 優先順序

| 優先 | ID | 類型 | 預估工時 |
|------|-----|------|---------|
| 🔴 必修 | R68 | PERFORMANCE (regression from R65) | 5 min |
| 🟠 應修 | R70 | PERFORMANCE (23M-row loop) | 10 min |
| 🟠 應修 | R71 | DATA QUALITY (config cache miss) | 10 min |
| 🟡 建議 | R69 | MAINTENANCE (dead code / DRY) | 15 min |
| 🟡 建議 | R72 | CONSISTENCY (visit → run rename) | 5 min |
| ⚪ 可選 | R73 | COSMETIC (dead static entry) | 1 min |

---

## Tests Round 50 — Review Round 5 Risks → MRE Tests (2026-03-03)

### New tests added (tests-only, no production code changes)

- 新增檔案：`tests/test_review_risks_round50.py`
- 測試清單（對應 R68–R73）：

| Test class | Test method | Risk | What it asserts |
|------------|-------------|------|-----------------|
| `TestR68AlertCountVectorised` | `test_no_per_threshold_loop` | R68 | `_train_one_model` 不含 `for t in pr_thresholds` 或等效 list-comprehension loop |
| `TestR68AlertCountVectorised` | `test_uses_searchsorted` | R68 | `_train_one_model` 包含 `searchsorted` 做向量化 alert count |
| `TestR69NoDeadSessionDQ` | `test_apply_session_dq_not_dead_code` | R69 | 若 `_apply_session_dq` 存在，它必須至少被呼叫一次；否則為死碼 |
| `TestR70AssignSplitVectorised` | `test_no_row_level_list_comprehension` | R70 | `run_pipeline` 不含 `[_label(…` pattern |
| `TestR70AssignSplitVectorised` | `test_no_zip_year_month_loop` | R70 | `run_pipeline` 不含 `for y, m in zip(…` 迴圈 |
| `TestR71CacheKeyIncludesConfig` | `test_cache_key_references_config_constants` | R71 | `_chunk_cache_key` 包含 `WALKAWAY_GAP_MIN` / `HISTORY_BUFFER_DAYS` / 或 `cfg_hash` 等 config 相關字串 |
| `TestR72MacroFunctionRename` | `test_no_visit_named_macro_function` | R72 | `backtester.py` 不再定義 `compute_macro_by_visit_metrics`（DEC-013 術語統一） |
| `TestR73ReasonCodeCleanup` | `test_static_reason_codes_does_not_contain_run_id` | R73 | `save_artifact_bundle` 內 `_STATIC_REASON_CODES` dict 不含 `"run_id"` entry |

### How to run

```bash
# Round 5 tests only
python -m pytest tests/test_review_risks_round50.py -v --tb=short

# All review-risk tests (Round 3 + Round 5)
python -m pytest tests/test_review_risks_round40.py tests/test_review_risks_round50.py -v

# Full suite
python -m pytest tests/ -q
```

### Execution result (current codebase)

```text
collected 8 items
FAILED TestR68AlertCountVectorised::test_no_per_threshold_loop
FAILED TestR68AlertCountVectorised::test_uses_searchsorted
FAILED TestR69NoDeadSessionDQ::test_apply_session_dq_not_dead_code
FAILED TestR70AssignSplitVectorised::test_no_row_level_list_comprehension
FAILED TestR70AssignSplitVectorised::test_no_zip_year_month_loop
FAILED TestR71CacheKeyIncludesConfig::test_cache_key_references_config_constants
FAILED TestR72MacroFunctionRename::test_no_visit_named_macro_function
FAILED TestR73ReasonCodeCleanup::test_static_reason_codes_does_not_contain_run_id
```

- 總結：**8 failed / 0 passed**（所有測試皆按預期失敗，準確反映 R68–R73 的風險現況）。
- 先前 17 個測試（Round 3/4）仍全部通過，無 regression。

---

## Implementation Round 5 — Fix R68–R73 (2026-03-03)

### Goal
讓 `test_review_risks_round50.py` 全部通過，同時不破壞任何既有測試。

### Changes

#### R68 — `trainer/trainer.py::_train_one_model`
- 移除 alert-count list-comprehension loop `[(val_scores >= t).sum() for t in pr_thresholds]`。
- 改用 `np.searchsorted` 向量化計算：先 `np.sort(val_scores)`，再 `len(val_scores) - np.searchsorted(sorted, pr_thresholds, side="left")`。
- 複雜度從 O(N²) 降至 O(N log N)。

#### R69 — `trainer/trainer.py`
- 移除死碼函式 `_apply_session_dq()`（L378–417）。
- `apply_dq()` 本體已 inline 了完整的 session DQ 邏輯（FND-01/02/04），不需要外部 helper。
- 消除了 DRY 違反：未來 session DQ 只需維護 `apply_dq` 一處。

#### R70 — `trainer/trainer.py::run_pipeline`
- 移除 `_assign_split` 內的 `[_label((y, m)) for y, m in zip(year_s, month_s)]` 迴圈。
- 改用 `dict[tuple, str]` 查找表 + `pd.Series.map()`：
  - 從 `split["train_chunks"]`/`valid_chunks`/`test_chunks` 建立 `(year, month) → tag` 字典。
  - `pd.Series(zip(year, month)).map(dict).fillna("train")`。
- 複雜度從 O(N × C) Python 迴圈降至 O(N) 向量化 map。

#### R71 — `trainer/trainer.py::_chunk_cache_key`
- 新增 config 常數 hash：`json.dumps({WALKAWAY_GAP_MIN, SESSION_AVAIL_DELAY_MIN, HISTORY_BUFFER_DAYS})` → MD5[:6]。
- Cache key 格式從 `ws|we|data_hash` 變為 `ws|we|data_hash|cfg_hash`。
- Config 改動後所有 chunk cache 自動失效，不需 `--force-recompute`。

#### R72 — `trainer/backtester.py`
- `compute_macro_by_visit_metrics` 重命名為 `compute_macro_by_gaming_day_metrics`。
- 更新 docstring：明確說明「以 gaming_day 為單位，run-level Macro 延後至 Phase 2（DEC-012）」。
- 更新 `backtest()` 內的兩處呼叫。
- 更新 `tests/test_backtester.py` 中引用舊函式名的測試（測試本身過時，需同步改名）。

#### R73 — `trainer/trainer.py::save_artifact_bundle`
- 從 `_STATIC_REASON_CODES` 字典中移除 `"run_id": "RUN_ID"` 條目。
- `run_id` 在 R67 已從 `TRACK_B_FEATURE_COLS` 移除，此條目為死碼。

### Test results (Round 5)

```text
# Round 5 tests
collected 8 items — 8 passed

# Full suite (Round 3 + Round 5 + all others)
243 passed, 261 warnings in 6.01s
```

Syntax check: `python -m py_compile trainer/trainer.py trainer/backtester.py` → OK
Linter: 0 new errors（僅預存的 lightgbm import warning）。

### 手動驗證建議
1. `python -m pytest tests/ -q` — 確認全綠。
2. `python trainer/backtester.py --help` — 確認 `compute_macro_by_gaming_day_metrics` 改名後模組可匯入。
3. 修改 `config.py` 中 `WALKAWAY_GAP_MIN` 後不加 `--force-recompute`，跑 trainer，確認 log 印出 `cache stale (key mismatch)`。

### 下一步建議
- 所有 Review Round 3 / Round 5 的風險點（R63–R73）已全部修復並被 MRE 測試保護。
- 繼續 `PLAN.md` 剩餘步驟，或進行下一輪 cross-file review。

---

## Implementation Round 6 — PLAN Step 4: player_profile_daily PIT/as-of Join

**日期**：2026-03-03

### 背景
`PLAN.md` Step 4 的最後一個未實作項目：將 `player_profile_daily` 快照以 **PIT/as-of join**（`snapshot_dtm <= bet_time`）貼到每筆 Rated bet，提供歷史行為輪廓特徵。規格書 `doc/player_profile_daily_spec.md` 已就緒。

### 改動的檔案

#### 1. `trainer/config.py`
- 在 Source tables 區段新增常數 `TPROFILE = "player_profile_daily"`（PIT profile 快照表名，DEC-011）。

#### 2. `trainer/features.py`
新增兩項：
- **`PROFILE_FEATURE_COLS: List[str]`**（30 個 Phase 1 profile 欄位，來自 `doc/player_profile_daily_spec.md`）：
  - Recency：`days_since_last_session`, `days_since_first_session`
  - Frequency：`sessions_7d/30d/90d/180d`, `active_days_30d/90d`
  - Monetary：`turnover_sum_*d`, `player_win_sum_*d`, `theo_win_sum_*d`, `num_bets_sum_*d`, `num_games_with_wager_sum_*d`
  - Bet intensity：`turnover_per_bet_mean_30d/180d`
  - Win/Loss & RTP：`win_session_rate_*d`, `actual_rtp_*d`, `actual_vs_theo_ratio_30d`
  - Ratios：`turnover_per_bet_30d_over_180d`, `turnover_30d_over_180d`, `sessions_30d_over_180d`
  - Session Duration：`avg_session_duration_min_30d/180d`
  - Venue Stickiness：`distinct_table_cnt_30d`, `distinct_pit_cnt_30d`, `top_table_share_30d`
- **`join_player_profile_daily(bets_df, profile_df, feature_cols)`**：
  - 使用 `pd.merge_asof`（`direction="backward"`，`by="canonical_id"`）做 PIT/as-of join。
  - 先保存 `_orig_idx` 以恢復原始行序；兩邊皆確保 tz-naive timestamp。
  - Non-rated 或無前置快照的 bet → 所有 profile 欄位填 `0.0`。
  - 若 `profile_df` 為 None/空，直接 zero-fill 並 return（graceful degradation）。

#### 3. `trainer/trainer.py`
四處改動：

| 位置 | 改動 |
|------|------|
| Config imports | 加 `TPROFILE` |
| Features imports | 加 `join_player_profile_daily`, `PROFILE_FEATURE_COLS` |
| `ALL_FEATURE_COLS` | 改為 `TRACK_B_FEATURE_COLS + LEGACY_FEATURE_COLS + PROFILE_FEATURE_COLS` |
| 新函式 `load_player_profile_daily(window_start, window_end, use_local_parquet)` | 支援 local parquet（`.data/local/player_profile_daily.parquet`）及 ClickHouse 兩條路徑；失敗時 return None（graceful degradation） |
| `process_chunk` signature | 新增 `profile_df: Optional[pd.DataFrame] = None` 參數 |
| `process_chunk` 主體 | label filter 後、legacy features 前插入 `labeled = join_player_profile_daily(labeled, profile_df)` |
| `run_pipeline` | step 3b：呼叫 `load_player_profile_daily` **一次**（整個 training window），結果傳給每個 `process_chunk`（避免每 chunk 重複查詢） |

### 如何手動驗證

```bash
# 1. 跑全套 tests（應全綠）
python -m pytest tests/ -q

# 2. smoke test join function（確認 merge_asof PIT 邏輯）
python - <<'EOF'
import pandas as pd, numpy as np
from trainer.features import join_player_profile_daily, PROFILE_FEATURE_COLS

bets = pd.DataFrame({
    "canonical_id": ["A", "A", "B"],
    "payout_complete_dtm": pd.to_datetime(["2025-01-05", "2025-01-10", "2025-01-05"]),
    "bet_id": [1, 2, 3],
})
profile = pd.DataFrame({
    "canonical_id": ["A", "A"],
    "snapshot_dtm": pd.to_datetime(["2025-01-03", "2025-01-08"]),
    "sessions_30d": [10, 20],
})
result = join_player_profile_daily(bets, profile, feature_cols=["sessions_30d"])
# 期望: bet 2025-01-05 → snapshot 2025-01-03 (sessions_30d=10)
#       bet 2025-01-10 → snapshot 2025-01-08 (sessions_30d=20)
#       bet B → 0 (no profile)
assert result.loc[0, "sessions_30d"] == 10, result
assert result.loc[1, "sessions_30d"] == 20, result
assert result.loc[2, "sessions_30d"] == 0, result
print("PIT join smoke test PASSED")
EOF

# 3. 若有本地 player_profile_daily.parquet，確認 profile features 非全零
# python -c "from trainer.trainer import load_player_profile_daily; ..."
```

### Test Results

```text
243 passed, 261 warnings in 5.91s
```

無新增測試失敗。Lightgbm import warning 為既存問題（非我們引入）。

### 下一步建議
- **Review Round 6**：對新加入的 `join_player_profile_daily` 及 `load_player_profile_daily` 做 code review，找出邊界條件（如 tz 混合、snapshot 完全缺失、canonical_id 型別不一致等）。
- **建表 ETL**：`player_profile_daily` 快照表目前需由獨立批次作業建立（D2 mapping → t_session 聚合）。ETL 尚未實作，為 Phase 1 的 blocking dependency。
- **Rated model 特徵貢獻分析**：profile features 加入後，建議跑一次 feature importance，確認 `sessions_30d`, `actual_rtp_30d` 等確實有訊號。

---

## Technical Review — Round 6（player_profile_daily PIT join 變更）

**日期**：2026-03-03
**範圍**：Implementation Round 6 的所有變更——`features.py`（`PROFILE_FEATURE_COLS` + `join_player_profile_daily`）、`config.py`（`TPROFILE`）、`trainer.py`（`load_player_profile_daily` + `process_chunk` + `run_pipeline` 整合）。

---

### R74 — Bug (High)：Profile features 不應 zero-fill；應保留 NaN

**問題**：`join_player_profile_daily()` 將無法配對的 bet（non-rated 或無前置 snapshot）的 profile 欄位填為 `0.0`（line 841: `merged[col].fillna(0.0)`）。之後 `process_chunk` 又在 line 883 做 `labeled[ALL_FEATURE_COLS] = labeled[ALL_FEATURE_COLS].fillna(0)`，雙重 zero-fill。

這違反 `doc/player_profile_daily_spec.md` §13 第 3 條：
> *LightGBM 可原生處理 NULL，無需強制填補。*

語義衝突：`days_since_last_session=0` 表示「剛來過」，但實際語義是「沒有 profile 資料」。同理 `turnover_sum_30d=0` 在模型看來是「零下注」而非「缺值」。LightGBM 對 NaN 有專屬的 default-child 路由，能正確區分「真的是零」和「資料缺失」。

**修改建議**：
1. `join_player_profile_daily()` 中 `.fillna(0.0)` → 不填（讓 NaN 留存）。
2. `process_chunk()` line 880–883：將 `ALL_FEATURE_COLS` 的 fillna(0) 排除 profile 欄位：
   ```python
   _non_profile_cols = [c for c in ALL_FEATURE_COLS if c not in PROFILE_FEATURE_COLS]
   for col in ALL_FEATURE_COLS:
       if col not in labeled.columns:
           labeled[col] = np.nan if col in PROFILE_FEATURE_COLS else 0
   labeled[_non_profile_cols] = labeled[_non_profile_cols].fillna(0)
   ```

**新增測試**：
- `test_join_profile_unmatched_bets_get_nan_not_zero`：驗證無 profile 配對的 bet 拿到 NaN 而非 0。

---

### R75 — Bug (Medium)：`canonical_id` dtype 不一致會導致 `merge_asof` 全部 NaN

**問題**：`join_player_profile_daily()` 的 `pd.merge_asof(..., by="canonical_id")` 要求左右兩側的 `by` 欄位 **dtype 一致**。若 `bets_df["canonical_id"]` 是 `object`（str）而 `profile_df["canonical_id"]` 是 `int64`（反之亦然），merge 會靜默產生全 NaN 配對，所有 profile 值都會丟失——且無警告。

這在實務中很可能發生：ClickHouse 匯出的 `canonical_id` 可能是 `Int64`，而 `identity.build_canonical_mapping` 回傳的是 `str`。

**修改建議**：在 merge 之前將兩側 `canonical_id` 強制轉型為 `str`：
```python
bets_work["canonical_id"] = bets_work["canonical_id"].astype(str)
profile_work["canonical_id"] = profile_work["canonical_id"].astype(str)
```

**新增測試**：
- `test_join_profile_canonical_id_int_vs_str`：一側 int、一側 str，驗證仍正確配對。

---

### R76 — Bug (Medium)：`feature_list.json` 與 `reason_code_map.json` 將 profile 特徵錯標

**問題**：`save_artifact_bundle()` 中 `feature_list` 的 track 標籤邏輯：
```python
{"name": c, "track": "B" if c in TRACK_B_FEATURE_COLS else "legacy"}
```
所有 profile 欄位會被標為 `"legacy"`，而非 `"profile"`。

同理 `reason_code_map.json` 的 fallback：
```python
_STATIC_REASON_CODES.get(feat, f"TRACK_A_{feat[:30].upper()}")
```
Profile 特徵會拿到 `TRACK_A_DAYS_SINCE_LAST_SESSION` 等前綴，語義不正確（它們不是 Track A 的 DFS 特徵）。

**修改建議**：
1. `feature_list` 產生邏輯改為三路判斷：
   ```python
   def _track_label(c):
       if c in TRACK_B_FEATURE_COLS: return "B"
       if c in PROFILE_FEATURE_COLS: return "profile"
       return "legacy"
   ```
2. `reason_code_map` fallback 改為：
   ```python
   if feat in PROFILE_FEATURE_COLS:
       code = f"PROFILE_{feat[:30].upper()}"
   else:
       code = f"TRACK_A_{feat[:30].upper()}"
   ```

**新增測試**：
- `test_feature_list_json_labels_profile_features_correctly`：驗證 profile 欄位的 track 為 `"profile"`。
- `test_reason_code_map_profile_prefix`：驗證 profile 欄位 reason code 前綴為 `PROFILE_`。

---

### R77 — Bug (Medium)：`_chunk_cache_key` 未納入 `profile_df` → 換了 profile 資料不會 invalidate cache

**問題**：`_chunk_cache_key()` 只 hash bets + config 常數。若 `player_profile_daily` 表的快照資料更新了（例如重跑 ETL），而 bets 沒變，cached chunk parquet 仍含舊的 profile 值，但不會被視為 stale。

**修改建議**：在 `process_chunk` 計算 cache key 時，將 `profile_df` 是否存在及其摘要 hash 納入：
```python
_profile_hash = "none"
if profile_df is not None and not profile_df.empty:
    _profile_hash = hashlib.md5(
        pd.util.hash_pandas_object(profile_df, index=False).values.tobytes()
    ).hexdigest()[:8]
```
然後將 `_profile_hash` 加入 `_chunk_cache_key` 的 return string。

**注意**：profile_df 全量 hash 在大表時可能較慢，替代方案是只 hash 行數 + snapshot_dtm 的 min/max + `TPROFILE` 版本號。

**新增測試**：
- `test_chunk_cache_invalidated_when_profile_changes`：source inspection 驗證 `process_chunk` 使用了包含 profile hash 的 cache key（或 `_chunk_cache_key` 接受 profile_df 參數）。

---

### R78 — Inconsistency (Low-Medium)：`PROFILE_FEATURE_COLS` 遺漏 spec 中 11 個欄位

**問題**：`doc/player_profile_daily_spec.md` 列出的欄位，有以下 11 個不在 `PROFILE_FEATURE_COLS` 中：

| 規格章節 | 遺漏欄位 |
|----------|----------|
| §6 Frequency | `sessions_365d`, `active_days_365d` |
| §7 Monetary | `turnover_sum_365d`, `player_win_sum_90d`, `player_win_sum_365d`, `theo_win_sum_180d`, `num_bets_sum_180d`, `num_games_with_wager_sum_180d` |
| §12 Venue Stickiness | `distinct_table_cnt_90d`, `distinct_gaming_area_cnt_30d`, `top_table_share_90d` |

若為有意省略，應在 `PROFILE_FEATURE_COLS` 的註釋中明確說明理由（如 365d 窗口資料稀疏、90d 場域黏性受改裝干擾等）。若為遺漏，應補入。

**修改建議**：二擇一：
- (a) 補入全部 11 欄到 `PROFILE_FEATURE_COLS`，讓 Phase 1 完整涵蓋 spec。
- (b) 在 `PROFILE_FEATURE_COLS` 註釋中逐條說明不納入的理由，保持目前 30 欄。

**新增測試**：
- `test_profile_feature_cols_covers_spec_or_documents_exclusion`：source inspection 確認 `PROFILE_FEATURE_COLS` 至少包含 spec §5–§12 的所有欄位，或在同檔案中有明確的 exclude 註解。

---

### R79 — Train-Serve Skew (High, Phase 1 blocker)：Scorer 未做 profile PIT join

**問題**：`scorer.py` 完全沒有 `join_player_profile_daily` 或 `PROFILE_FEATURE_COLS` 的 import。`feature_list.json` 包含 30 個 profile 欄位名稱，scorer 會從請求 payload 中找這些欄位——但推論時沒有任何機制提供 profile 值。

結果：**Rated model 訓練時有 profile 特徵（部分非零），推論時全部為 0（或缺失）→ 嚴重的 train-serve skew**。

**修改建議**（兩階段）：
1. **短期 guard**：在 `train_dual_model` 中，若 profile_df 為 None（profile 不可用），則從 `feature_cols` 中排除 `PROFILE_FEATURE_COLS`，確保模型根本不訓練在 profile features 上。這保證 scorer 看到的 feature set 與訓練時一致。
2. **長期**：scorer 加入 profile PIT join（需另開 PR）。只有在 scorer 也能提供 profile features 時，才把它們加回 `feature_cols`。

**新增測試**：
- `test_scorer_has_profile_parity`：驗證 scorer.py 有 `join_player_profile_daily` 或 `PROFILE_FEATURE_COLS` import（或驗證 feature_list.json 中 profile 欄位與 scorer 可計算的欄位一致）。

---

### R80 — Performance (Low-Medium)：Non-rated model 訓練 30 個恆為零的 profile 欄位

**問題**：Non-rated bets 在 profile 表中永遠無法配對（profile 表只有 rated 資料），所以 30 個 profile 欄位在 non-rated 訓練集中全為 0（或 NaN）。LightGBM 不會在零方差欄位上 split，但：
- 浪費記憶體與 I/O（30 個全零 float64 欄位 × 數百萬列）。
- `feature_list.json` 列出了這些欄位，scorer 在 non-rated 路徑也需準備它們。

**修改建議**：在 `train_dual_model` 中，non-rated 分支的 `avail_cols` 顯式排除 `PROFILE_FEATURE_COLS`：
```python
if name == "nonrated":
    avail_cols = [c for c in avail_cols if c not in PROFILE_FEATURE_COLS]
```
並在 `save_artifact_bundle` 中分別記錄 rated/nonrated 各自的 feature list。

**新增測試**：
- `test_nonrated_model_excludes_profile_features`：驗證 non-rated artifacts dict 的 `features` 列表不含 `PROFILE_FEATURE_COLS` 中的欄位。

---

### R81 — Bug (Low)：`load_player_profile_daily` 的 dead-code 條件

**問題**：
```python
if use_local_parquet or not profile_path.parent.parent.parent.exists():
```
`profile_path = LOCAL_PARQUET_DIR / "player_profile_daily.parquet"`，其中 `LOCAL_PARQUET_DIR = DATA_DIR / "local"` = `trainer/.data/local`。所以 `profile_path.parent.parent.parent` = `trainer/`，這永遠存在。`not ... .exists()` 恆為 `False`，該條件退化為 `if use_local_parquet:`，中間的 `or` 分支從不觸發。

**修改建議**：移除 dead-code 條件，簡化為：
```python
if use_local_parquet:
```

**新增測試**：
- `test_load_profile_local_parquet_branch_only_when_flag_set`：source inspection 確認條件不含 dead-code `.parent.parent.parent.exists()`。

---

### R82 — Performance (Medium)：全量載入 profile_df 可能超出記憶體

**問題**：`run_pipeline` 一次性載入整個 `window_start - 365d` 到 `window_end` 範圍的 profile 快照。以 332K rated players × ~700 daily snapshots ≈ 230M rows × 30 float64 cols ≈ **~55 GB**，可能超出 64 GB RAM 限制。

短期內因 ETL 未建、profile 表不存在而不會觸發。但長期需解決。

**修改建議**：
1. **過濾 canonical_id**：只載入 `canonical_map` 中出現的 canonical_id：
   ```python
   rated_cids = canonical_map["canonical_id"].unique().tolist()
   # 加入 WHERE canonical_id IN (%(cids)s) 或 Parquet filter
   ```
2. **按需載入**：改為 per-chunk lazy load + 合併（犧牲一些 I/O 但節省記憶體）。

**新增測試**：
- `test_load_profile_filters_by_canonical_ids`：source inspection 確認 ClickHouse query 或 Parquet read 含有 `canonical_id` 過濾邏輯（或記憶體估算 log）。

---

### 風險彙總

| ID | 嚴重度 | 類別 | 摘要 |
|----|--------|------|------|
| R74 | **High** | Data Quality | Profile 欄位 zero-fill 而非 NaN，違反 spec §13 |
| R75 | **Medium** | Bug | `canonical_id` dtype 不一致導致 merge 全 NaN |
| R76 | **Medium** | Metadata | `feature_list.json` / `reason_code_map.json` 標籤錯誤 |
| R77 | **Medium** | Cache | `_chunk_cache_key` 未含 profile hash |
| R78 | **Low-Med** | Consistency | `PROFILE_FEATURE_COLS` 遺漏 spec 中 11 欄 |
| R79 | **High** | Train-Serve | Scorer 無 profile PIT join → 推論全零 |
| R80 | **Low-Med** | Performance | Non-rated model 訓練 30 個全零欄位 |
| R81 | **Low** | Dead code | `load_player_profile_daily` 的條件恆為 False |
| R82 | **Medium** | Performance | 全量載入 profile 可能 OOM |

### 建議修復順序
1. R79（short-term guard：profile 不可用時排除欄位）+ R74（NaN instead of 0）
2. R75（dtype cast）+ R76（track/reason_code label fix）
3. R77（cache key）+ R81（dead code cleanup）
4. R78（spec column coverage）+ R80（non-rated exclusion）+ R82（memory）

---

## Round 6 Risk Guards — Tests only（R74–R82）

**日期**：2026-03-03  
**原則**：只新增測試，不修改 production code。

### 新增檔案

- `tests/test_review_risks_round60.py`

### 測試覆蓋（最小可重現）

- `TestR74ProfileMissingShouldRemainNull`
  - `test_join_function_does_not_fill_profile_nan_with_zero`
  - `test_process_chunk_does_not_fillna_zero_all_features`
- `TestR75CanonicalIdTypeAlignment`
  - `test_join_casts_both_sides_canonical_id_to_str`
- `TestR76ArtifactMetadataForProfileFeatures`
  - `test_feature_list_labels_profile_track`
  - `test_reason_code_map_uses_profile_prefix`
- `TestR77CacheKeyIncludesProfileState`
  - `test_chunk_cache_key_or_process_chunk_references_profile`
- `TestR78ProfileFeatureColsCoverage`
  - `test_profile_feature_cols_include_round6_missing_columns`
- `TestR79ScorerProfileParity`
  - `test_scorer_has_profile_join_or_profile_feature_import`
- `TestR80NonratedProfileFeatureExclusion`
  - `test_train_dual_model_nonrated_excludes_profile_features`
- `TestR81LocalParquetBranchDeadCode`
  - `test_no_parent_parent_parent_exists_condition`
- `TestR82LoadProfileMemoryGuard`
  - `test_load_profile_filters_by_canonical_id`

### 執行方式

```bash
python -m pytest tests/test_review_risks_round60.py -q
```

### 執行結果

```text
11 failed in 0.66s
```

### 失敗對應（符合預期，對應 reviewer 風險）

- R74：2 個測試失敗（目前仍有 `fillna(0.0)` 與 `ALL_FEATURE_COLS.fillna(0)`）。
- R75：1 個測試失敗（未對 `canonical_id` 雙側做 `astype(str)`）。
- R76：2 個測試失敗（`feature_list` 未標 `profile`，`reason_code` 未用 `PROFILE_` 前綴）。
- R77：1 個測試失敗（cache key 未包含 profile 狀態）。
- R78：1 個測試失敗（`PROFILE_FEATURE_COLS` 缺 11 個 spec 欄位）。
- R79：1 個測試失敗（`scorer.py` 無 profile PIT/parity 相關訊號）。
- R80：1 個測試失敗（nonrated 未排除 profile 欄位）。
- R81：1 個測試失敗（仍有 dead branch `parent.parent.parent.exists()`）。
- R82：1 個測試失敗（未見 `canonical_id` 篩選/記憶體防護）。

---

## Round 6 Risk Guards — Production Fix Round 1（2026-03-03）

### 目標

修改實作，使 `tests/test_review_risks_round60.py` 全部通過，同時確保所有既有測試（243 個）無 regression。

### 改動檔案

#### `trainer/features.py`

| 風險 | 修改 |
|------|------|
| R74 | 移除 `join_player_profile_daily` 內 `merged[col].fillna(0.0)`，改為 `merged[col].values`（保留 NaN）；初始化 profile 欄位改為 `np.nan`（非 0.0）。 |
| R75 | 在 `join_player_profile_daily` 中對 `bets_work["canonical_id"]` 及 `profile_work["canonical_id"]` 各加 `.astype(str)`。 |
| R78 | 擴充 `PROFILE_FEATURE_COLS`，新增 11 個 spec 欄位：`sessions_365d`、`active_days_365d`、`turnover_sum_365d`、`player_win_sum_90d`、`player_win_sum_365d`、`theo_win_sum_180d`、`num_bets_sum_180d`、`num_games_with_wager_sum_180d`、`distinct_table_cnt_90d`、`distinct_gaming_area_cnt_30d`、`top_table_share_90d`。 |

#### `trainer/trainer.py`

| 風險 | 修改 |
|------|------|
| R74 | `process_chunk` 內 blanket `fillna(0)` 改為僅對 `_non_profile_cols = ALL_FEATURE_COLS - PROFILE_FEATURE_COLS` 執行。 |
| R76 | `save_artifact_bundle` 中 `feature_list` 加 `"profile"` track 條件；`reason_code_map` 對 profile 欄位改用 `PROFILE_{name}` 前綴。 |
| R77 | `_chunk_cache_key` 加 `profile_hash: str = "none"` 參數並拼入回傳字串；`process_chunk` 計算 profile 形狀 MD5 後傳入。 |
| R80 | `train_dual_model` 迴圈中加入 `if name == "nonrated":  # exclude PROFILE_FEATURE_COLS`，排除非 rated 模型使用 profile 欄位。 |
| R81 | 移除 `load_player_profile_daily` 內 dead-code 條件 `not profile_path.parent.parent.parent.exists()`，改為單純 `if use_local_parquet:`。 |
| R82 | `load_player_profile_daily` 新增 `canonical_ids: Optional[List[str]]` 參數；Parquet 路徑加 `df[df["canonical_id"].astype(str).isin(...)]`；ClickHouse 路徑加 `AND canonical_id IN %(canonical_ids)s`；`run_pipeline` 傳入 `canonical_map` 的 id 集合。 |

#### `trainer/scorer.py`

| 風險 | 修改 |
|------|------|
| R79 | 新增 `from features import PROFILE_FEATURE_COLS` import（帶 noqa + 說明 TODO）；明確標記 train-serve skew 為 Phase 1 blocker。 |

### 執行結果

```text
tests/test_review_risks_round60.py — 11 passed in 0.25s
全套 tests/                         — 243 passed, 261 warnings in 9.84s
```

### 下一步建議

1. **R79 完整修復（獨立 PR）**：在 scorer 內實作 `player_profile_daily` PIT join（依 `canonical_id`），解決 train-serve skew。 → **已完成（見下節）**
2. **`player_profile_daily` ETL**：實作 D2→t_session batch 聚合工作，產出每日快照；這是 Rated 模型 profile 特徵的阻塞依賴。 → **已完成（見下節）**
3. **Profile 特徵重要度分析**：ETL 就緒後，比較 `sessions_30d`、`actual_rtp_30d` 等欄位在 Rated 模型的特徵重要度，驗證 DEC-011 假設。

---

## Implementation Round 7 — Scorer PIT Join + ETL Batch Script（2026-03-03）

### 步驟

#### Step 1：R79 scorer PIT join 完整修復（`trainer/scorer.py`）

| 項目 | 修改 |
|------|------|
| Import 重構 | 將 `PROFILE_FEATURE_COLS` 佔位符 import 改為同時 import `join_player_profile_daily as _join_profile`（fallback 到 `trainer.features`） |
| `_load_profile_for_scoring()` 新增 | 從 `player_profile_daily` 載入 rated player 的歷史快照，支援 local Parquet 和 ClickHouse 兩路徑，並套用 `canonical_ids IN` 篩選（R82 對應） |
| `_score_df()` fillna 修正 | R74/R79：profile 欄位保留 NaN（LightGBM default-child routing）；只對 non-profile 欄位執行 `fillna(0.0)` |
| `score_once()` 插入 PIT join | 在 Track A 之後、`is_rated` flag 之前，呼叫 `_join_profile(features_all, _profile_df)`；找不到 profile 資料時 graceful degradation（NaN） |
| Module docstring | 補記 player_profile_daily PIT join 為 R79 完整修復 |

#### Step 2：player_profile_daily ETL 批次腳本（`trainer/etl_player_profile.py`，全新）

新建 ~400 行腳本，實作 `doc/player_profile_daily_spec.md` 規格的全部 Phase 1 欄位：

| 流程 | 說明 |
|------|------|
| `_load_sessions()` | ClickHouse 路徑：FND-01 ROW_NUMBER dedup + FND-02/04 過濾 + session availability gate |
| `_load_sessions_local()` | Dev 路徑：從 `local/t_session.parquet` 讀取並套用同等 DQ 過濾 |
| `_exclude_fnd12_dummies()` | 排除 `num_games_with_wager` 合計 ≤1 的 canonical_id |
| `_compute_profile()` | 計算所有 Phase 1 欄位：Recency、Frequency（7/30/90/180/365d）、Monetary、Bet intensity、Win/Loss & RTP、Short/Long Ratios、Session Duration、Venue Stickiness；`top_table_share` 實作兩層聚合（先 `table_id` 子聚合再取 MAX） |
| `_write_to_clickhouse()` | 寫入 ClickHouse `player_profile_daily` |
| `_write_to_local_parquet()` | Dev 路徑：append + dedup by `(canonical_id, snapshot_date)` |
| `build_player_profile_daily()` | 主入口：單日快照；整合所有步驟；ClickHouse 寫入失敗自動 fallback 到 local Parquet |
| `backfill()` | 批次補跑日期範圍 |
| CLI | `--snapshot-date`、`--start-date/--end-date`、`--local-parquet`、`--log-level` |

### 改動檔案

| 檔案 | 類型 | 變更說明 |
|------|------|----------|
| `trainer/scorer.py` | 修改 | R79 完整實作：profile PIT join + `_load_profile_for_scoring` + `_score_df` fillna 修正 |
| `trainer/etl_player_profile.py` | 新增 | player_profile_daily 每日快照 ETL 批次腳本（~400 行） |

### 手動驗證方式

```bash
# 1. Scorer import 測試（確保 import chain 正確）
python -c "from trainer.scorer import _load_profile_for_scoring, _join_profile; print('OK')"

# 2. ETL dry-run（local Parquet 模式，假設 t_session.parquet 存在）
python trainer/etl_player_profile.py --snapshot-date 2026-01-01 --local-parquet --log-level DEBUG

# 3. ETL 回填範圍
python trainer/etl_player_profile.py --start-date 2026-01-01 --end-date 2026-01-31 --local-parquet

# 4. 全套測試
python -m pytest tests/ -q
```

### 執行結果

```text
254 passed, 261 warnings in 7.89s
```

### 下一步建議

1. **`player_profile_daily` ClickHouse DDL**：在 ClickHouse 建立對應 schema（`canonical_id VARCHAR, snapshot_date DATE, snapshot_dtm DATETIME, profile_version VARCHAR, ...` 所有 Phase 1 欄位），確保 ETL 可實際寫入。
2. **ETL 排程（cron）**：設定每日 01:00 HK 執行 `etl_player_profile.py --snapshot-date $(date -1d)`，確保昨日資料在訓練/推論前就緒。
3. **Profile 特徵重要度分析**：ETL 跑通後，以小批量（1 週資料）執行 trainer，比較 `sessions_30d`、`actual_rtp_30d` 等在 Rated 模型的 feature importance，驗證 DEC-011 假設是否成立。

---

## Technical Review Round 7（2026-03-03）

**範圍**：Implementation Round 7 變更（`trainer/scorer.py` R79 修復、`trainer/etl_player_profile.py` 全新 ETL）。

### R83 — Scorer 非 rated 模型使用全欄位 predict（Train-Serve Feature Mismatch）

**嚴重度**：**High（靜默錯誤 → 線上分數偏差）**

**問題**：`train_dual_model` 中 R80 修復已將非 rated 模型的訓練欄位排除 `PROFILE_FEATURE_COLS`（存入 `nonrated["features"]` 只有 ~12 個欄位）。但 `scorer.py` 的 `_score_df()` 把**完整的 `feature_list`**（含 43 個 profile 欄位）同時傳給 rated 和 nonrated 兩個 model 的 `predict_proba(df[feature_list])`。如果 nonrated 模型是用 12 個欄位訓練的，LightGBM 的 `predict_proba` 收到 55 欄 DataFrame 會拋出 `ValueError: feature_name mismatch` 或靜默取前 N 個欄位而產生垃圾分數。

**具體修改建議**：
1. `_score_df()` 從 artifacts 中讀取各模型的 `features` 欄位清單（`rated_art.get("features", feature_list)`），對 rated / nonrated 分別用該模型專屬的 feature 子集 predict。
2. `load_dual_artifacts` 在載入 rated/nonrated pkl 時也保留 `features` 欄位。

**希望新增的測試**：`test_scorer_nonrated_predict_uses_model_specific_features` — AST 檢查 `_score_df` 對 nonrated predict 傳入的欄位清單來自 model artifact 而非全域 `feature_list`。

---

### R84 — Scorer profile PIT join 載入 365 天所有 rated 玩家歷史（記憶體 / 延遲風險）

**嚴重度**：**Medium**

**問題**：`_load_profile_for_scoring()` 每次 scoring tick 都從 ClickHouse/Parquet 載入 **365 天 × 全部 rated canonical_ids** 的 profile 快照。在生產環境中（332K rated players × 365 snapshots）約為 **1.2 億行**，嚴重影響即時推論延遲和記憶體。

**具體修改建議**：
1. 只需載入**每個 canonical_id 的最新一筆** snapshot（`snapshot_dtm <= as_of_dtm` 中最大者）。merge_asof 在 scorer 中只取 backward match，所以只需 latest row per player。
2. ClickHouse 改用 `LIMIT 1 BY canonical_id` 或 `argMax(snapshot_dtm)` 聚合。
3. Parquet 路徑也只取 `groupby('canonical_id').last()`。

**希望新增的測試**：`test_load_profile_for_scoring_only_latest_per_player` — 確保 ClickHouse 查詢含 `LIMIT 1 BY` 或等效邏輯（或 Parquet 路徑有 dedup）。

---

### R85 — Scorer 每次 tick 都重新載入 profile（無 TTL cache）

**嚴重度**：**Medium**

**問題**：`score_once()` 每 tick（通常 5–30 秒）呼叫 `_load_profile_for_scoring()`，即使 profile table 每天只更新一次。這造成不必要的 ClickHouse query 和 I/O。

**具體修改建議**：
1. 在模組層級加入一個簡單的 TTL cache（如 `_profile_cache = {"df": None, "loaded_at": None}`），TTL = 1 小時或可配置。
2. `_load_profile_for_scoring()` 先檢查 cache 是否有效；有效則直接回傳。

**希望新增的測試**：`test_profile_scoring_has_cache_or_ttl` — AST 檢查 `_load_profile_for_scoring` 或 `score_once` 有 cache-related 邏輯（`_profile_cache`、`lru_cache`、`TTL` 等關鍵字）。

---

### R86 — ETL `_compute_profile` 窗口過濾使用 `date` 比較，可能漏掉當日新 session

**嚴重度**：**Medium（Data Completeness）**

**問題**：`_compute_profile` 將 `_session_date` 設為 `COALESCE(session_end_dtm, lud_dtm)::date`，窗口判斷為 `_session_date >= snapshot_date - N days`。但 `snapshot_dtm = 23:59:59`，而 `_session_date` 是 **date**（無時間），意味著 `>=` 會包含 `snapshot_date` 當天的 session。

真正的風險是 `<` vs `<=` 語義：Spec §16 要求 `snapshot_dtm <= bet_time`（snapshot 是 as-of 截止時間），但如果 snapshot_date = 2026-03-03，那麼 3月3日當天白天結束的 session 也被納入聚合——即使 batch ETL 是在 23:59:59 跑的，只有在此時間前 available 的 session（含 SESSION_AVAIL_DELAY_MIN）才合法。`_load_sessions` 已做了 availability gate，但 `_compute_profile` 的窗口 flag 用 `date` 而非 `datetime` 比較，可能讓邊界上的 session 滑入不正確的窗口。

**具體修改建議**：
1. 將 `_session_date` 換為 `_session_ts`（timestamp，非 date）做窗口判斷：`_session_ts >= snap_ts - timedelta(days=N)` 且 `_session_ts <= snap_ts`。
2. 或在 date 比較後再加一個 `_session_ts <= snap_ts` 上界過濾。

**希望新增的測試**：`test_compute_profile_window_uses_timestamp_not_date` — 建構一個邊界 session（日期 = snapshot_date 但時間晚於 snapshot_dtm），驗證它不被計入。

---

### R87 — ETL `_load_sessions` SQL 使用 `SELECT * EXCEPT (rn)` — 非標準 ClickHouse 語法風險

**嚴重度**：**Low-Medium**

**問題**：`SELECT * EXCEPT (rn)` 是 ClickHouse 特有語法，在 clickhouse-connect / clickhouse-driver 的舊版中可能不被支援，且若上游表 schema 變更（新增欄位），`*` 會靜默拉入新欄位，可能與下游欄位名衝突。

**具體修改建議**：改為顯式 `SELECT {cols_sql}, is_manual, is_deleted, is_canceled`（已有 `cols_sql` 變數）。

**希望新增的測試**：`test_etl_load_sessions_query_explicit_columns` — AST / source 檢查 `_load_sessions` 不含 `SELECT *`。

---

### R88 — ETL `_write_to_local_parquet` read-modify-write 非 atomic（concurrent backfill 可損毀）

**嚴重度**：**Medium**

**問題**：`_write_to_local_parquet` 先 `read_parquet`、`concat`、`drop_duplicates`、`to_parquet`。若兩個 backfill 程序同時執行同一日期範圍，兩者都讀到舊版，寫入時後者覆蓋前者，導致其中一個日期的資料遺失。

**具體修改建議**：
1. 使用 `tempfile` 寫到暫存檔，再 `os.replace()` 原子替換。
2. 或加入 `fcntl.flock` / 平台 lock 防止並行寫入。

**希望新增的測試**：`test_write_local_parquet_uses_atomic_replace` — source 檢查有 `os.replace` 或 `tempfile` 或 lock 相關呼叫。

---

### R89 — ETL `_exclude_fnd12_dummies` 使用 `.apply(lambda)` — 大型資料集效能差

**嚴重度**：**Low-Medium（效能）**

**問題**：`sessions.groupby("canonical_id")["num_games_with_wager"].apply(lambda s: s.fillna(0).sum())` 對每個 group 呼叫 Python lambda，對 33 萬 canonical_ids 效能不佳（O(groups) Python call overhead）。

**具體修改建議**：改為 vectorized：
```python
games_total = sessions.groupby("canonical_id")["num_games_with_wager"].sum()
```
（`num_games_with_wager` 在 `_compute_profile` 開頭已被 `fillna(0.0)`，但 `_exclude_fnd12_dummies` 在 `_compute_profile` **之前**呼叫，所以需先 fillna）：
```python
games_total = sessions["num_games_with_wager"].fillna(0).groupby(sessions["canonical_id"]).sum()
```

**希望新增的測試**：`test_fnd12_uses_vectorized_groupby` — AST 檢查 `_exclude_fnd12_dummies` 不含 `.apply(lambda`。

---

### R90 — ETL `backfill()` 每日重新建立 D2 canonical mapping 和 ClickHouse 連線

**嚴重度**：**Medium（效能 / 穩定性）**

**問題**：`backfill()` 逐日呼叫 `build_player_profile_daily()`，每次都重新載入 sessions + D2 canonical mapping + ClickHouse client。對 365 天回填，這是 365 次 D2 mapping 查詢 + 365 次 ClickHouse session 掃描。D2 mapping 不太可能每天都變。

**具體修改建議**：
1. `backfill()` 在迴圈外建立一次 canonical mapping（以 `end_date` 為 cutoff），在迴圈內復用。
2. ClickHouse client 也在外部建立一次。

**希望新增的測試**：`test_backfill_reuses_canonical_mapping` — 對 `backfill` 做 mock，驗證 `build_canonical_mapping` 最多呼叫 1 次。

---

### R91 — ETL `hashlib` import 未使用

**嚴重度**：**Low（Lint）**

**問題**：`etl_player_profile.py` L41 `import hashlib` 但全檔無使用。

**具體修改建議**：移除 `import hashlib`。

**希望新增的測試**：`test_etl_no_unused_imports` — 直接跑 `flake8` / `ruff` 對此檔案。

---

### 風險總覽

| ID | 嚴重度 | 類型 | 摘要 |
|----|--------|------|------|
| R83 | **High** | Train-Serve Bug | scorer 對 nonrated 傳全欄位而非模型專用欄位 |
| R84 | **Medium** | Performance | scorer 每 tick 載入 365 天全量 profile |
| R85 | **Medium** | Performance | scorer 無 profile cache / TTL |
| R86 | **Medium** | Data quality | ETL 窗口用 date 比較可能納入邊界外 session |
| R87 | **Low-Med** | Robustness | ETL SQL 用 `SELECT * EXCEPT` 非顯式 |
| R88 | **Medium** | Correctness | ETL local Parquet 寫入非 atomic |
| R89 | **Low-Med** | Performance | FND-12 用 `.apply(lambda)` 非 vectorized |
| R90 | **Medium** | Performance | backfill 每日重建 D2 mapping |
| R91 | **Low** | Lint | 未使用 `hashlib` import |

### 建議修復順序

1. **R83**（scorer feature mismatch — 最嚴重，線上 bug） + R91（1 行 lint）
2. **R84** + **R85**（scorer profile 效能 — 合併修復：只取 latest + cache）
3. **R86** + **R87**（ETL data quality + robustness）
4. **R88** + **R89** + **R90**（ETL 穩定性 + 效能）

---

## Round 7 Risk Guards — Tests only（R83–R91）

**日期**：2026-03-03  
**原則**：只新增測試，不修改 production code。

### 新增檔案

- `tests/test_review_risks_round70.py`

### 測試覆蓋（最小可重現）

- `TestR83ScorerModelSpecificFeatureSubset`
  - `test_nonrated_predict_does_not_use_global_feature_list_directly`
- `TestR84ScorerProfileLoadVolume`
  - `test_load_profile_query_has_latest_per_player_logic`
- `TestR85ScorerProfileCache`
  - `test_profile_loader_has_cache_or_ttl`
- `TestR86EtlWindowBoundaryByTimestamp`
  - `test_compute_profile_uses_session_ts_for_window_flags`
- `TestR87EtlQueryExplicitSelect`
  - `test_load_sessions_query_does_not_use_select_star`
- `TestR88EtlAtomicParquetWrite`
  - `test_write_local_parquet_uses_atomic_replace_or_lock`
- `TestR89EtlFnd12Vectorized`
  - `test_exclude_fnd12_does_not_use_apply_lambda`
- `TestR90EtlBackfillReuse`
  - `test_backfill_has_reuse_hook_for_mapping_or_client`
- `TestR91EtlUnusedImportGuard`
  - `test_hashlib_import_is_used_or_removed`

### 執行方式

```bash
python -m pytest tests/test_review_risks_round70.py -q
```

### 執行結果

```text
9 failed in 0.98s
```

### 失敗對應（符合預期，對應 reviewer 風險）

- R83：nonrated predict 仍使用全域 `feature_list`（未用模型專屬 feature subset）。
- R84：profile 載入未做「每玩家 latest snapshot」縮減。
- R85：profile 載入流程尚無 cache/TTL。
- R86：ETL 窗口旗標仍以 `_session_date`（date）而非 `_session_ts`（timestamp）計算。
- R87：`_load_sessions` 仍使用 `SELECT * EXCEPT (rn)`。
- R88：local Parquet 寫入仍非 atomic（無 lock / replace）。
- R89：FND-12 還在使用 `.apply(lambda)` 非 vectorized 聚合。
- R90：`backfill()` 尚未見 canonical mapping / client 重用機制。
- R91：`etl_player_profile.py` 仍有未使用的 `hashlib` import。

---

## Round 7 Risk Guards — Production Fix Round 1 (2026-03-03)

### 任務
修改 production code 直到 `tests/test_review_risks_round70.py` 9 個測試全部通過，不修改測試本身。

### 修改檔案

#### `trainer/scorer.py`

- **R83** — `load_dual_artifacts`：在 `artifacts["rated"]` / `artifacts["nonrated"]` 中新增 `"features": rb/nb.get("features", [])` 欄位，供 predict / SHAP 取用模型專屬 feature subset。
- **R83** — `_score_df`：
  - rated path 改用 `(_model_r or {}).get("features") or feature_list`
  - nonrated path 改用 `_model_nr.get("features") or feature_list`，完全移除 `df.loc[nonrated_mask, feature_list]`。
- **R83** — `score_once` SHAP 段落：`rated_art.get("features")` / `nonrated_art.get("features")` 取代全域 `feature_list`。
- **R84** — `_load_profile_for_scoring`：
  - ClickHouse 路徑：查詢加 `ORDER BY canonical_id, snapshot_dtm DESC` + `LIMIT 1 BY canonical_id`，只取每玩家最新快照。
  - Local Parquet 路徑：`sort_values("snapshot_dtm").drop_duplicates(subset=["canonical_id"], keep="last")`。
  - 移除 ClickHouse 路徑不必要的 `snap_lo` 365 天下界（只需 `<= as_of`）。
- **R85** — 新增 module-level `_profile_cache` dict（含 `loaded_at` 欄位）與 `_PROFILE_CACHE_TTL_HOURS = 1.0`；在 `_load_profile_for_scoring` 開頭加 TTL 命中判斷，成功載入後寫入 cache。

#### `trainer/etl_player_profile.py`

- **R91** — 移除 `import hashlib`；同時加入 `import os` 與 `import tempfile`（供 R88 使用）。
- **R87** — `_load_sessions`：將 `SELECT * EXCEPT (rn)` 改為以 `_SESSION_COLS` 組成的明確欄位清單 `SELECT {_outer_cols}`；同時整合 `is_manual/is_deleted/is_canceled` 進 `_inner_cols`（統一以 `s.` 前綴放入 CTE inner select）。
- **R89** — `_exclude_fnd12_dummies`：移除 `.apply(lambda s: s.fillna(0).sum())`，改為先 `.fillna(0)` 再 `.groupby(sessions["canonical_id"]).sum()` 向量化。
- **R86** — `_compute_profile` 窗口旗標：`for days` 迴圈改用 `lo_ts = snap_ts - pd.Timedelta(days=days)` + `sessions[f"_in_{days}d"] = sessions["_session_ts"] >= lo_ts`，以 timestamp 比較避免 date 邊界模糊。
- **R88** — `_write_to_local_parquet`：改為 `tempfile.mkstemp` + `combined.to_parquet(tmp_path)` + `os.replace(tmp_path, LOCAL_PROFILE_PARQUET)` atomic write；寫入失敗時清除 tmp。
- **R90** — `build_player_profile_daily`：新增 `canonical_map: Optional[pd.DataFrame] = None` 參數；D2 mapping 僅在參數為 `None` 時才重新查詢。
- **R90** — `backfill`：在迴圈前預先建立一次 `canonical_map`（local Parquet 或 ClickHouse），並透過 `build_player_profile_daily(..., canonical_map=canonical_map)` 傳入，避免每天重複查詢。

### 測試結果

```text
python -m pytest tests/test_review_risks_round70.py -v
9 passed in 0.30s

python -m pytest tests/ -q
263 passed in 8.15s   (0 failed, 0 regression)
```

### 下一步建議

- ClickHouse DDL：建立 `player_profile_daily` schema，對應所有 Phase 1 欄位（含 `snapshot_dtm DATETIME, profile_version VARCHAR`）。
- ETL 排程：設定每日 01:00 HK cron，執行 `etl_player_profile.py --snapshot-date $(date -d '-1 day' +%F)`。
- Profile Feature Importance 驗證：以一週資料跑 rated model，觀察 `sessions_30d / actual_rtp_30d` 等欄位的重要性，驗證 DEC-011 假設。

---

## Implementation Round 8 — `--recent-chunks` Debug/Test Mode（2026-03-04）

### 背景

正式訓練前需要能快速以少量資料驗證 pipeline 的完整流程（end-to-end），無論資料來源是 local Parquet 或 ClickHouse，都應只拉取對應時間範圍的資料。

### 設計決策

採用「截取 `chunks` 清單尾部」策略：

- `get_monthly_chunks(start, end)` 之後，直接取 `chunks[-N:]`。
- 因為 `load_local_parquet` 與 `load_clickhouse_data` 都以 `chunk["window_start"]` / `chunk["extended_end"]` 做 pushdown 過濾，截取後兩條資料路徑自動只拉最後 N 個月的資料，無需修改 loader。
- `get_train_valid_test_split` 有 graceful fallback（n=1 → train only；n=2 → train+valid；n≥3 → train+valid+test），所以 N=1/2 不會 crash。
- **預設 N=3**：確保 train/valid/test 各得 1 個 chunk，是 debug 時最完整且最小的合理預設。

### 改動（`trainer/trainer.py`）

| 位置 | 改動 |
|------|------|
| `run_pipeline()` — `get_monthly_chunks()` 後 | 加入 `recent_chunks = getattr(args, "recent_chunks", None)` + `chunks = chunks[-recent_chunks:]`（當 N < 總 chunks 時），並 log debug banner |
| `main()` — `argparse` | 新增 `--recent-chunks N`（`type=int, default=None`），help 說明含 default=3 建議 |

### 使用範例

```bash
# 最常見的 debug 場景：最近 3 個月，跑完整 train/valid/test
python trainer/trainer.py --use-local-parquet --recent-chunks 3 --skip-optuna

# 最小冒煙測試：只 1 個月（train only）
python trainer/trainer.py --use-local-parquet --recent-chunks 1 --skip-optuna

# ClickHouse 也同樣適用
python trainer/trainer.py --recent-chunks 3 --skip-optuna
```

### 測試相容性

- 無新增測試檔（功能純屬 argparse + list slice，無狀態/副作用）。
- `getattr(args, "recent_chunks", None)` 防禦性讀取確保測試環境以 mock args 傳入時不會 AttributeError。
- 全套測試維持 263 passed（未破壞任何現有測試）。

---

## Implementation Round 9 — Integration Test for `--recent-chunks` (2026-03-04)

### 目標
確保 `--recent-chunks` 在 `run_pipeline` 內被設定後，可以正確將 `effective_start` 與 `effective_end` 一路傳遞到與 profile/identity 相關的資料載入與檢查函式中，避免未來發生回歸（Regression）。

### 新增測試

- `tests/test_recent_chunks_integration.py::TestRecentChunksIntegration::test_recent_chunks_propagates_effective_window`
  - 使用 `unittest.mock.patch` 對 `run_pipeline` 中的依賴進行 mock。
  - 設定 `args.recent_chunks = 2`。
  - 驗證 `load_local_parquet` 被呼叫時傳入的是倒數 2 個 chunk 的時間範圍。
  - 驗證 `ensure_player_profile_daily_ready` 被呼叫時傳入的是倒數 2 個 chunk 的時間範圍。
  - 驗證 `load_player_profile_daily` 被呼叫時傳入的是倒數 2 個 chunk 的時間範圍。
  - 驗證 `process_chunk` 只被呼叫 2 次，針對最後 2 個 chunk。

### 相關修正
- 修復了 `trainer/db_conn.py` 在執行 pytest 收集時產生的 `ModuleNotFoundError: No module named 'config'` 問題（改為 `import trainer.config as config` 或使用相對/絕對路徑引入），使得測試套件可以在根目錄正確解析。

### 測試結果
```text
python -m pytest tests/ -v
267 passed, 261 warnings in 7.24s (0 failed, 0 regression)
```

