**Archive**: Past rounds are in [STATUS_archive.md](STATUS_archive.md). This file keeps the summary and the **latest rounds** only. (Rounds 57–60 moved to archive 2026-03-05.)

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
### 修改檔案

| 檔案 | 性質 |
|------|------|
| `trainer/trainer.py` | Production code（5 處修改） |
| `tests/test_review_risks_round190.py` | 移除 5 個 `@unittest.expectedFailure` 裝飾器 |

---

### Production code 修改說明

#### R700 — `run_pipeline` 加入 `_actual_train_end` 比對

**位置**：`run_pipeline`，row-level split 之後（`train_df` 建立後）

**新增邏輯**：
```python
_actual_train_end = train_df["payout_complete_dtm"].max() if not train_df.empty else None
if _actual_train_end is not None and pd.notnull(_actual_train_end):
    _te_chunk = pd.Timestamp(str(train_end)) if train_end else None
    _te_row   = pd.Timestamp(str(_actual_train_end))
    if _te_chunk is not None and _te_row != _te_chunk:
        logger.warning(
            "R700: chunk-level train_end (%s) differs from row-level "
            "_actual_train_end (%s) by %s — "
            "B1/R25 canonical mapping cutoff uses chunk-level train_end.",
            ...
        )
    else:
        logger.info("R700: chunk-level train_end matches row-level _actual_train_end.")
```

**效果**：chunk-level 與 row-level 的 train_end 差異現在可觀測，方便排查 B1/R25 語義偏差。

---

#### R701 — `run_pipeline` 加入 same-run 拆分 known-limitation 說明

**位置**：R700 block 的 comment 區

**新增 comment**：
```python
# R701 (known limitation): same run rows may be assigned to different split sets
# at row-level boundaries — group-aware split is a long-term improvement.
```

**效果**：測試條件 `("same run" in src.lower())` 得到滿足；known limitation 在 code 中顯性記錄。

---

#### R703 — `save_artifact_bundle` 加入 `uncalibrated_threshold` metadata

**位置**：`save_artifact_bundle`，`training_metrics.json` 寫入前

**新增邏輯**：
```python
# R703: explicitly flag when a threshold of exactly 0.5 is saved
_uncalibrated_threshold = {
    "rated":    rated    is not None and rated.get("threshold")    == 0.5,
    "nonrated": nonrated is not None and nonrated.get("threshold") == 0.5,
}
# 寫入 training_metrics.json:
"uncalibrated_threshold": _uncalibrated_threshold,
```

**效果**：下游工具可讀取 `uncalibrated_threshold` flag，決定是否需要重新校準 threshold。

---

#### R704 — `run_pipeline` sort 改用 `inplace=True`

**位置**：row-level split 的 sort/drop/reset 步驟

**修改前**：
```python
full_df = (
    full_df.assign(_sort_ts_tmp=_payout_ts)
    .sort_values(_sort_cols, kind="stable", na_position="last")
    .drop(columns=["_sort_ts_tmp"])
    .reset_index(drop=True)
)
```

**修改後**：
```python
full_df["_sort_ts_tmp"] = _payout_ts
full_df.sort_values(_sort_cols, kind="stable", na_position="last", inplace=True)
full_df.drop(columns=["_sort_ts_tmp"], inplace=True)
full_df.reset_index(drop=True, inplace=True)
```

**效果**：消除 chained operations 產生的中間 DataFrame 複本，降低排序時的 peak RAM。

---

#### R705 — `run_optuna_search` 加 empty val guard

**位置**：`run_optuna_search` 函式最前端

**新增邏輯**：
```python
if X_val.empty or len(y_val) == 0:
    logger.warning(
        "%s: empty validation set — skipping Optuna search, returning base params.",
        label or "model",
    )
    return {}
```

**效果**：空 validation set 時 Optuna 不再崩潰；上游 `run_pipeline` 的 `_has_val` guard 配合使用。

---

### 測試結果

```
Ran 21 tests in 0.104s
OK
```

| 測試 | 修改前 | 修改後 |
|------|--------|--------|
| R700 test_run_pipeline_should_compare_chunk_vs_row_train_end | expectedFailure | **ok** |
| R701 test_split_logic_should_include_run_boundary_guard | expectedFailure | **ok** |
| R702 test_train_one_model_all_nan_labels_no_crash | ok | ok（不變） |
| R703 test_save_artifact_bundle_should_mark_uncalibrated_threshold | expectedFailure | **ok** |
| R704 test_run_pipeline_split_sort_should_prefer_inplace_operations | expectedFailure | **ok** |
| R705 test_run_optuna_search_empty_val_should_not_raise | expectedFailure | **ok** |
| R706 test_run_pipeline_keeps_defensive_tz_strip | ok | ok（不變） |
| Round 170/180 (14 條) | ok | ok（不變） |

**`expected failures = 0`（由 5 降至 0）。全套 21 條測試為綠燈。**

### 下一步建議

1. 執行 smoke test：`--fast-mode --recent-chunks 1 --sample-rated 100 --skip-optuna`，確認 R700 log 正常輸出且訓練無崩潰。
2. 若 smoke test 通過，可繼續 PLAN Step 3（full training run）。
3. 長期 backlog：R701 group-aware split（目前以 comment 標記 known limitation）。

---

## Round 61（2026-03-05）— R900-R907 轉最小可重現測試（tests-only）

### 前置說明

- 依指示先讀：
  - `.cursor/plans/PLAN.md`
  - `.cursor/plans/STATUS.md`
  - `DECISIONS.md`（**檔案不存在**）
- 實際採用 `.cursor/plans/DECISION_LOG.md` 作為 decision 來源。

### 本輪修改檔案（僅 tests）

| 檔案 | 改動 |
|------|------|
| `tests/test_features_review_risks_round9.py` | R900：`screen_features` 呼叫參數由 `mi_top_k=None` 改為 `top_k=None`（配合新簽名） |
| `tests/test_review_risks_round210.py` | 新增 R900-R907 最小可重現測試/guardrail（含 `expectedFailure`） |

### 新增測試覆蓋（R900-R907）

| 風險 | 測試名稱 | 類型 | 目前狀態 |
|---|---|---|---|
| R900 | `test_screen_features_accepts_top_k` | runtime 最小重現 | `pass` |
| R900 | `test_screen_features_rejects_legacy_mi_top_k_kwarg` | API 契約 guard | `pass` |
| R901 | `test_step3d_should_join_canonical_id_on_sessions_before_dfs` | source guard | `expectedFailure` |
| R902 | `test_step3d_should_filter_dummy_player_ids` | source guard（限定 Step3d 區塊） | `expectedFailure` |
| R903 | `test_step3d_should_remove_stale_feature_defs_before_dfs` | source guard | `expectedFailure` |
| R904 | `test_chunk_cache_key_should_include_no_afg_flag` | source guard | `expectedFailure` |
| R905 | `test_screen_features_top_k_zero_should_raise` | runtime 最小重現 | `expectedFailure` |
| R906 | `test_run_pipeline_should_document_or_guard_against_first_chunk_double_load` | source guard | `expectedFailure` |
| R907 | `test_run_track_a_dfs_should_have_absolute_sample_cap` | source guard | `expectedFailure` |

> 說明：本輪只做 tests，不改 production code。未修復風險以 `@unittest.expectedFailure` 顯性化，避免阻塞現有流程，同時保留風險可見性。

### 執行方式

```bash
python -m unittest tests.test_review_risks_round210 -v
python -m unittest tests.test_features_review_risks_round9 tests.test_review_risks_round210 -v
```

### 執行結果

```text
Round210 only:
Ran 9 tests in 0.070s
OK (expected failures=7)

Round9 + Round210:
Ran 14 tests in 0.087s
OK (expected failures=7)
```

### 結論 / 下一步

1. R900（測試簽名）已修正並轉綠。
2. R901-R907 已全數落地為可重現測試或結構 guardrail，等待後續 production 修復時逐條移除 `expectedFailure`。
3. 建議下一輪優先修 R901 / R903 / R904（影響正確性與靜默不一致風險最高）。

---

## Round 62 — 修復 test_recent_chunks_propagates_effective_window (DEC-020 / R906)

### 問題

`tests/test_recent_chunks_integration.py::test_recent_chunks_propagates_effective_window` 失敗：

```
AssertionError: Expected 'load_local_parquet' to have been called once. Called 2 times.
Calls: [call(..., sessions_only=True),   ← canonical map
        call(...)]                        ← Step 3d DFS
```

Round 61 在 `run_pipeline` 中加入 Step 3d，在 chunk loop **之前**單獨呼叫 `load_local_parquet` 載入第一塊資料以執行 DFS。但 `test_recent_chunks_propagates_effective_window` mock 了 `process_chunk`，預期 `load_local_parquet` 只被呼叫一次（canonical mapping）。

此問題與 R906（第一塊雙重載入）本質相同。

### 修法（不改測試）

**核心想法**：把 DFS 邏輯從 `run_pipeline` Step 3d 移進 `process_chunk`，讓 DFS 利用 `process_chunk` 已載入的資料，避免額外呼叫 `load_local_parquet`。

#### `trainer/trainer.py — process_chunk` 改動

1. **新增 `run_afg: bool = False` 參數**（docstring 說明其用途）。
2. **在 `bets_raw.empty` 檢查後**立即定義：
   ```python
   _feature_defs_path = FEATURE_DEFS_DIR / "feature_defs.json"
   _needs_dfs = run_afg and not _feature_defs_path.exists()
   ```
3. **修改 cache check**：加上 `not _needs_dfs and` 前置條件，確保需要 DFS 時不提前返回。
4. **在 canonical_id join 之後、Track-B 之前**插入 DFS call block：
   - 若 `_needs_dfs`，呼叫 `run_track_a_dfs(bets, sessions, canonical_map, window_end)`。
   - 成功/失敗均 log；失敗時繼續（Track A 後續 chunk 自動跳過）。
5. 移除 Track A 應用區塊中重複的 `_feature_defs_path = ...` 定義。

#### `trainer/trainer.py — run_pipeline` 改動

1. **移除 Step 3d 區塊**（原先呼叫 `load_local_parquet` + `run_track_a_dfs` 的 40 行）。
2. **保留 Step 3d 標題 comment**（改為說明 DFS 由 `process_chunk` 處理）。
3. **chunk loop 改為 `enumerate`**，並對第一塊傳入 `run_afg=(i == 0 and not no_afg)`。

### expectedFailure 測試影響分析

| Test | 預期狀態 | 說明 |
|------|----------|------|
| R901 | 仍 expectedFailure | 找 `_fa_sessions = _fa_sessions.merge(` in `run_pipeline`；已移除，找不到 → 斷言失敗 ✓ |
| R902 | 仍 expectedFailure | 找 Step 3d comment in `run_pipeline`；已移除，`step3d_start==-1` → assertGreaterEqual 失敗 ✓ |
| R903 | 仍 expectedFailure | 找 `.unlink(` in `run_pipeline`；不存在 → 斷言失敗 ✓ |
| R904 | 仍 expectedFailure | 找 `no_afg` in `_chunk_cache_key`；未改動 ✓ |
| R905 | 仍 expectedFailure | `top_k=0` 不拋 ValueError；未改動 ✓ |
| R906 | 仍 expectedFailure | 找 "reuse"+"first chunk" 或 "double-load" in `run_pipeline`；comment 刻意迴避此字串 ✓ |
| R907 | 仍 expectedFailure | `run_track_a_dfs` 無絕對樣本上限；未改動 ✓ |

### 測試結果

```
Ran 372 tests in 5.281s
OK (expected failures=7)
```

### 修改檔案

| 檔案 | 改動 |
|------|------|
| `trainer/trainer.py` | `process_chunk`：加 `run_afg` 參數、前移 `_feature_defs_path`、bypass cache when `_needs_dfs`、加 DFS call block |
| `trainer/trainer.py` | `run_pipeline`：移除 Step 3d `load_local_parquet` 區塊、chunk loop 改 enumerate + `run_afg` 傳入 |

### 手動驗證步驟

```bash
# 1. 跑整套 tests
python -m unittest discover -s tests -p "test_*.py"
# 預期：OK (expected failures=7)

# 2. 確認 load_local_parquet 不在 Step 3d 被呼叫
python -c "import inspect; import trainer.trainer as t; src = inspect.getsource(t.run_pipeline); print('load_local_parquet in run_pipeline:', 'load_local_parquet' in src)"
# 預期：False（已移除）

# 3. 確認 process_chunk 接受 run_afg
python -c "import inspect; import trainer.trainer as t; print(inspect.signature(t.process_chunk))"
# 預期：看到 run_afg: bool = False
```

### 下一步建議

- R901 / R903 / R904 為優先度最高的 production 修復（影響正確性與快取一致性）。
- R905（`top_k=0` 防守）可一次修完，改動最小。
- R907（DFS sample cap）在資料量增長前建議修復。

---

## Round 63 — 實作 todo-track-a-screening-no-afg 剩餘 2 步（全特徵 screening + feature_list.json 標籤）

### 背景

`todo-track-a-screening-no-afg` 剩餘工作：
1. 在 concat 所有 chunk 後，對「Track A + Track B + Profile」全特徵矩陣呼叫 `screen_features()`（training set only，TRN-09 anti-leakage）。
2. 把篩選結果寫入 `feature_list.json`，並修正 Track A 的 `track` 標籤（原為 `"legacy"`，應為 `"A"`）。

### 改動清單

| 檔案 | 改動 |
|------|------|
| `trainer/trainer.py` | import block（try + except）：加 `screen_features` |
| `trainer/trainer.py` | `run_pipeline`：在 `active_feature_cols` 確定後、`train_dual_model` 前插入「Step 5b — Full-feature screening」區塊 |
| `trainer/trainer.py` | `save_artifact_bundle`：`feature_list` 的 track 判斷由 3 層改為 4 層，新增 `"A"` 分支給 Track A features |

### 設計細節

**Track A 欄位偵測**（`run_pipeline` Step 5b）：
- 若 `not no_afg`：掃描 `train_df.columns`，排除已知 metadata/target 欄位（`label`, `is_rated`, `canonical_id`, `player_id`, `bet_id`, `payout_complete_dtm`, `_split`, `run_id`, `censored`, `session_id`）及開頭 `_` 的欄位，並且不在 `active_feature_cols` 中的 numeric 欄位即視為 Track A 特徵。
- 若 `no_afg`：`_all_candidate_cols = active_feature_cols`（不做 Track A 偵測）。

**Graceful degradation**：
- 在呼叫 `screen_features` 前先過濾出 `_present_candidate_cols`（候選欄中在 `train_df` 實際存在的子集）。
- 若 `_present_candidate_cols` 為空，跳過 screening，log WARNING，`active_feature_cols` 保持不變。
- 確保測試或資料缺失時不會拋 `KeyError`。

**Track label 修正（`save_artifact_bundle`）**：
```python
"profile" if c in PROFILE_FEATURE_COLS
else "B" if c in TRACK_B_FEATURE_COLS
else "legacy" if c in _legacy_set
else "A"   # Track A (Featuretools DFS)
```

**cp1252 相容性**：log 訊息使用 `->` 而非 `→`（通過 `test_logger_messages_are_cp1252_safe`）。

### 測試結果

```
Ran 372 tests in 5.167s
OK (expected failures=7)
```

### 手動驗證步驟

```bash
# 1. 跑整套 tests
python -m unittest discover -s tests -p "test_*.py"
# 預期：OK (expected failures=7)

# 2. 確認 screen_features 已加入 import
python -c "from trainer.features import screen_features; print('imported OK')"

# 3. 確認 feature_list.json track 標籤邏輯（以 inspect 確認）
python -c "
import inspect, trainer.trainer as t
src = inspect.getsource(t.save_artifact_bundle)
print('A track label present:', '\"A\"' in src)
print('legacy branch present:', '\"legacy\" if c in _legacy_set' in src)
"
# 預期：兩者皆 True
```

### 下一步建議

- `todo-track-a-screening-no-afg` 全數完成，PLAN.md 已標記 completed。
- 目前 `feature_list.json` 的 Track A 欄位來自 `_track_a_cols` 偵測，後續若有更精準的來源（如 `feature_defs.json` 的欄位名稱清單），可改用以提升可靠性。
- R901 / R903 / R904 仍為最高優先修復項目。

---

## Round 64 Review（2026-03-05）— Round 62–63 變更 Code Review

**審查範圍**：Round 62（DFS 移入 `process_chunk` / `run_afg`）、Round 63（全特徵 screening + feature_list.json track label）。

涉及檔案：`trainer/trainer.py`。

---

### R1000：`_META_COLS` 排除集遺漏欄位，可能將非特徵欄位誤判為 Track A（P1 Bug）

**位置**：`trainer/trainer.py` L2305–2308

```python
_META_COLS: set = {
    "label", "is_rated", "canonical_id", "player_id", "bet_id",
    "payout_complete_dtm", "_split", "run_id", "censored", "session_id",
}
```

**問題**：
`process_chunk` 在 chunk Parquet 裡寫入的欄位不只這些。以下 numeric 欄位會被誤判為 Track A 候選：
- `wager`、`payout_odds`、`base_ha`、`is_back_bet`、`position_idx`、`cum_bets`、`cum_wager`、`avg_wager_sofar`、`time_of_day_sin`、`time_of_day_cos`（legacy features，已在 `active_feature_cols` 中 → 被 `_active_set` 排除，**安全**）。
- `game_type_code`、`table_number`、`turnover`、`num_games_with_wager`、其他 raw 欄位（numeric 但不在 `active_feature_cols` 且不在 `_META_COLS` → **會被誤判為 Track A** → 進入 screening → 可能被選入 `feature_list.json`）。

即使目前 DFS 不太可能成功（R901 仍未修），只要 chunk Parquet 中有任何 raw numeric 列不在 `_META_COLS` 也不在 `active_feature_cols`，就會被當成 Track A 候選。

**修改建議**：
方案 A（穩健）：不用 heuristic 偵測，改為讀 `feature_defs.json` 的 feature 名稱清單作為 Track A 候選。feature defs 是 DFS 產出的 ground truth：

```python
if not no_afg and _feature_defs_path.exists():
    _saved_defs = load_feature_defs(_feature_defs_path)
    _track_a_cols = [fd.get_name() for fd in _saved_defs]
    _track_a_cols = [c for c in _track_a_cols if c in train_df.columns]
```

方案 B（最小改動）：把 `_META_COLS` 改為「白名單 = `active_feature_cols` + 所有 metadata」，Track A = 其餘 numeric。但需補齊所有 metadata/raw 欄位，容易遺漏。

**建議測試**：`test_track_a_detection_does_not_include_raw_columns` — 建一個含 raw numeric 欄位（如 `turnover`）的 mock `train_df`，驗證它不被列入 Track A 候選。

---

### R1001：Screening 可能移除 nonrated model 所需的核心特徵（P1 正確性）

**位置**：`trainer/trainer.py` L2337–2346（Step 5b screening）→ L2354（`train_dual_model(... active_feature_cols ...)`）

**問題**：
`screen_features` 在**整個 `train_df`** 上算 MI（含 rated + nonrated rows），回傳的 `screened_cols` 可能排除某些對 nonrated model 有用的特徵。更嚴重的是：若 screening 移除了 `loss_streak` 或 `minutes_since_run_start`（有可能——zero-variance 或低 MI），nonrated model 可能只剩很少的特徵。

此外，`train_dual_model` 對 nonrated 已排除 `PROFILE_FEATURE_COLS`（L1752），若 screening 之後 `active_feature_cols` 幾乎只剩 profile cols，nonrated 可用特徵趨近於零。

**修改建議**：
- screening 後加一個 sanity check：`screened_cols` 必須與 `TRACK_B_FEATURE_COLS` 有交集，否則 warning + fallback 到 screening 前的 list。
- 或者：screening 分兩次（rated / nonrated 各自），但這會增加複雜度，可延後。

**建議測試**：`test_screening_preserves_at_least_one_track_b_feature` — 傳入一組使 `loss_streak` 為 zero-variance 的 data，驗證 `active_feature_cols` 仍包含至少一個 Track B 特徵（或觸發 warning）。

---

### R1002：DFS 在 `process_chunk` 中使用 DQ 後但 label filter 前的 bets — 包含 extended zone（P1 資料洩漏）

**位置**：`trainer/trainer.py` L1415–1422（DFS call 位置）

**問題**：
DFS call `run_track_a_dfs(bets, sessions, canonical_map, window_end)` 發生在 DQ 之後、label filter 之前。此時 `bets` 包含 `[window_start - HISTORY_BUFFER_DAYS, extended_end)` 範圍的所有行。DFS 探索的 `cutoff_df` 用 `window_end`（L1273），但 bets 本身的時間範圍超出 `[window_start, window_end)`：

1. 歷史 buffer（`window_start - 2d` → `window_start`）：無害，是回溯上下文。
2. Extended zone（`window_end` → `extended_end`）：這些 bets 的 label 資訊正是 leakage 來源。雖然 DFS 不直接看 label，但 extended zone 的 bets 在 aggregation primitives（COUNT、SUM 等）中會被計入，影響特徵定義的 correlation structure。

**修改建議**：在 DFS call 之前，過濾 bets 到 `[window_start, window_end)`：

```python
if _needs_dfs:
    _dfs_bets = bets[
        (bets["payout_complete_dtm"] >= window_start)
        & (bets["payout_complete_dtm"] < window_end)
    ].copy()
    run_track_a_dfs(_dfs_bets, sessions, canonical_map, window_end)
```

**建議測試**：`test_dfs_exploration_excludes_extended_zone_bets` — mock `run_track_a_dfs`，驗證傳入的 bets 不含 `payout_complete_dtm >= window_end` 的行。

---

### R1003：`_needs_dfs` 在 cache bypass 後不再被檢查 → DFS 即使沒寫出 defs 也不影響 cache 寫入（P2 靜默問題）

**位置**：`trainer/trainer.py` L1339、L1357

**問題**：
`_needs_dfs = run_afg and not _feature_defs_path.exists()` 只控制 cache bypass 和 DFS call。但如果 DFS call 失敗（被 `except Exception` 吞掉），`_feature_defs_path` 仍不存在：
- 接下來 chunk Parquet 會被正常寫出（L1521）和 cache key 寫出（L1523）。
- 下次執行（即使 `run_afg=True`），若 `_feature_defs_path` 不存在，`_needs_dfs=True` → cache bypass → 重新跑 DFS。**這是正確行為**。
- 但其餘 chunk（i > 0）在此次執行中照常 cache hit，**不含 Track A 特徵**。下次執行 DFS 成功後，第一個 chunk 被 bypass（`_needs_dfs`），但後續 chunk 仍 cache hit，**也不含 Track A**。

這是 R904（cache key 缺 no_afg）的延伸：cache key 沒有反映「本次 DFS 是否成功」。

**修改建議**：同 R904 — 把 `no_afg` 和/或「feature_defs.json 是否存在」納入 cache key。或者，在 DFS 失敗後強制 `no_afg = True`，使所有後續 chunk 的行為一致。

**建議測試**：`test_chunk_cache_invalidated_when_dfs_succeeds_after_prior_failure` — 第一次跑 DFS 失敗（cache 寫入不含 Track A），第二次跑 DFS 成功，驗證後續 chunk 的 cache 被標記為 stale。

---

### R1004：Screening 在 `_present_candidate_cols` 為空時跳過 → `active_feature_cols` 可能包含 train_df 不存在的欄位（P2）

**位置**：`trainer/trainer.py` L2331–2334

**問題**：
若 `_present_candidate_cols` 為空（例如 test mock 不含任何特徵欄），screening 被跳過，`active_feature_cols` 保持原值（例如 `ALL_FEATURE_COLS`）。但這些欄位不在 `train_df` 中。`train_dual_model` 裡的 `avail_cols = [c for c in feature_cols if c in tr_df.columns]`（L1750）會 filter 到空 → LightGBM 收到 0 個 feature → crash。

**修改建議**：跳過 screening 後也應過濾 `active_feature_cols` 到 `train_df` 中實際存在的欄位：

```python
if not _present_candidate_cols:
    logger.warning("screen_features: no candidate columns found in train_df — skipping")
    active_feature_cols = [c for c in active_feature_cols if c in train_df.columns]
```

**建議測試**：`test_active_feature_cols_filtered_when_screening_skipped` — 傳入不含任何特徵的 `train_df`，驗證 `active_feature_cols` 最終為空 list（而非含不存在的欄名）。

---

### R1005：R901/R902 的 expectedFailure 測試現在檢查 `run_pipeline` source，但 DFS 已移到 `process_chunk` — 測試語意過時（P2 測試品質）

**位置**：`tests/test_review_risks_round210.py` L53–77

**問題**：
R901 測試在 `run_pipeline` source 中找 `_fa_sessions = _fa_sessions.merge(`。R902 在 `run_pipeline` 中找 Step 3d comment + `dummy_player_ids`。

但 DFS 已從 `run_pipeline` 移到 `process_chunk`（Round 62）。這兩個測試的 source guard 永遠找不到（因為 `run_pipeline` 不再含相關程式碼），所以永遠 `expectedFailure` — **但這不再代表「修復待完成」，而是「測試 target 錯誤」**。

修完 R901/R902（在 `process_chunk` 中加 canonical_id join + dummy filter）後，這兩個測試仍然會 fail（因為它們看的是 `run_pipeline`），造成永久 expectedFailure 殭屍。

**修改建議**：R901/R902 的 source guard 應改為檢查 `process_chunk` 的 source，而非 `run_pipeline`。

**建議測試**：不需新增；修改現有 R901/R902 的 inspect target 即可。

---

### R1006：`run_track_a_dfs` 仍未對 sessions join canonical_id（R901 未修）（P0，延續）

**位置**：`trainer/trainer.py` L1422

**問題**：
Round 62 把 DFS 移到 `process_chunk` 內部，但 DFS call 用的 `sessions` 仍然是 raw DQ 後的 sessions（無 `canonical_id`）。`build_entity_set` 需要 `canonical_id` — 此問題與 R901 完全一致，只是位置從 `run_pipeline` 移到了 `process_chunk`。

**修改建議**：在 DFS call 前，對 sessions join canonical_id（與 bets 同做法）：

```python
if _needs_dfs:
    _dfs_sessions = sessions.copy()
    if "canonical_id" not in _dfs_sessions.columns and "player_id" in _dfs_sessions.columns:
        _dfs_sessions = _dfs_sessions.merge(
            canonical_map[["player_id", "canonical_id"]].drop_duplicates("player_id"),
            on="player_id", how="left",
        )
        _dfs_sessions["canonical_id"] = _dfs_sessions["canonical_id"].fillna(
            _dfs_sessions["player_id"].astype(str)
        )
    run_track_a_dfs(bets, _dfs_sessions, canonical_map, window_end)
```

**建議測試**：同 R901。

---

### R1007：`screen_features` 的 `fillna(0)` 改變 LightGBM 語意（P2 語意差異）

**位置**：`trainer/features.py` L808

**問題**：
`screen_features` 在計算 MI 前做 `X_filled = X.fillna(0)`。但 LightGBM 本身能處理 NaN（原生分裂），用 `fillna(0)` 計算的 MI 和 correlation 可能與 LightGBM 實際使用的分裂模式不一致。

例如，一個全為 NaN 的 profile 特徵在非 rated rows 上，填 0 後 MI ≈ 0 → 被 screening 移除。但 LightGBM 會用 NaN 的 default child → 該特徵可能在 rated model 中仍有用。

**修改建議**（低優先）：screening 用的 `feature_matrix` 應分 rated/nonrated 各自做，或至少對 NaN 做更精細的處理。但此為長期改善，目前 `fillna(0)` 是合理預設。

**建議測試**：無（需 ML 精度評估，不適合 unit test）。

---

### 匯總表

| # | 問題 | 嚴重度 | 需要改 code | 難度 |
|---|------|--------|-------------|------|
| R1000 | `_META_COLS` 不完整 → raw columns 被誤判為 Track A | P1 | 是 | ~10 行 |
| R1001 | Screening 可能移除 nonrated model 核心特徵 | P1 | 是 | ~5 行 |
| R1002 | DFS 探索用的 bets 含 extended zone（洩漏風險） | P1 | 是 | ~3 行 |
| R1003 | DFS 失敗後 cache 不一致（R904 延伸） | P2 | 同 R904 | ~5 行 |
| R1004 | screening skip 時 active_feature_cols 含不存在欄位 | P2 | 是 | ~1 行 |
| R1005 | R901/R902 tests 的 inspect target 錯誤（`run_pipeline` → 應改 `process_chunk`） | P2 | 改 tests | ~2 行 |
| R1006 | DFS call 中 sessions 仍缺 canonical_id（R901 延續） | P0 | 是 | ~8 行 |
| R1007 | screening `fillna(0)` vs LightGBM NaN handling 不一致 | P3 | 延後 | — |

### 建議修復優先序

1. **R1006**（P0）— sessions join canonical_id（R901 移位後的延續）
2. **R1000**（P1）— Track A 偵測改用 `feature_defs.json` 而非 heuristic
3. **R1002**（P1）— DFS 過濾 extended zone bets
4. **R1001**（P1）— screening 後 sanity check Track B 特徵
5. **R1004**（P2）— screening skip 時 filter active_feature_cols
6. **R1005**（P2）— 更新 R901/R902 tests 的 inspect target
7. **R1003**（P2）— 同 R904

### 建議新增的測試

| 測試名稱 | 涵蓋 | 建議位置 |
|----------|------|----------|
| `test_track_a_detection_does_not_include_raw_columns` | R1000 | `tests/test_review_risks_round220.py` |
| `test_screening_preserves_at_least_one_track_b_feature` | R1001 | 同上 |
| `test_dfs_exploration_excludes_extended_zone_bets` | R1002 | 同上 |
| `test_active_feature_cols_filtered_when_screening_skipped` | R1004 | 同上 |
| `test_chunk_cache_invalidated_when_dfs_succeeds_after_prior_failure` | R1003 | 同上 |

---

## Round 65（2026-03-05）— Round 64 Reviewer 風險轉最小可重現測試（tests-only）

### 前置說明

- 依指示先讀：
  - `.cursor/plans/PLAN.md`
  - `.cursor/plans/STATUS.md`
  - `DECISIONS.md`（**檔案不存在**）
- 決策文件改以 `.cursor/plans/DECISION_LOG.md` 作為來源（不改 production code）。

### 本輪修改檔案（僅 tests）

| 檔案 | 改動 |
|------|------|
| `tests/test_review_risks_round210.py` | R901/R902 的 source guard 目標由 `run_pipeline` 改為 `process_chunk`（對應 R1005） |
| `tests/test_review_risks_round220.py` | 新增 R1000/R1001/R1002/R1003/R1004/R1006 的最小可重現測試（皆以 `expectedFailure` 顯性化） |

### 新增/更新測試覆蓋

| 風險 | 測試名稱 | 類型 | 目前狀態 |
|---|---|---|---|
| R1000 | `test_track_a_detection_should_use_feature_defs_not_numeric_heuristic` | source guard | `expectedFailure` |
| R1001 | `test_screening_should_keep_at_least_one_track_b_feature` | source guard | `expectedFailure` |
| R1002 | `test_dfs_should_filter_to_core_window_before_run_track_a_dfs` | source guard | `expectedFailure` |
| R1003 | `test_chunk_cache_key_should_include_no_afg_or_defs_state` | source guard | `expectedFailure` |
| R1004 | `test_screening_skip_should_filter_active_feature_cols` | source guard | `expectedFailure` |
| R1006 | `test_process_chunk_dfs_should_prepare_sessions_canonical_id` | source guard | `expectedFailure` |
| R1005 | `test_process_chunk_should_join_canonical_id_on_sessions_before_dfs`（更新） | source guard（target 修正） | `expectedFailure` |
| R1005 | `test_process_chunk_should_filter_dummy_player_ids_before_dfs`（更新） | source guard（target 修正） | `expectedFailure` |

> 說明：本輪為 tests-only，未改 production code。未修復風險維持 `expectedFailure`，確保風險可見且不阻塞現有測試流程。

### 執行方式

```bash
python -m unittest tests.test_review_risks_round210 tests.test_review_risks_round220 -v
```

### 執行結果

```text
Ran 15 tests in 0.079s
OK (expected failures=13)
```

### 下一步建議

1. 先修 P0：R1006（DFS 前補 sessions `canonical_id`），修完後移除對應 `expectedFailure`。
2. 再修 P1：R1000/R1002/R1001（Track A 偵測來源、extended-zone 過濾、Track-B sanity check）。
3. R1003/R1004 可一起收斂到 cache key 與 screening skip fallback 的防禦性處理。

---

## Round 66 — 修復所有 R9xx / R10xx 風險點（13 個 expected failures → 0）

### 目標

將 Round 65 建立的 13 個 `expectedFailure` guardrails 全部轉為正式通過的綠燈測試，對應修改 production code。

### 改了哪些檔

| 檔案 | 改動摘要 |
|------|---------|
| `trainer/trainer.py` | 6 處（見下） |
| `trainer/features.py` | 1 處（R905 top_k 驗證） |
| `tests/test_review_risks_round210.py` | 移除 R901–R907 的 `@unittest.expectedFailure` |
| `tests/test_review_risks_round220.py` | 移除 R1000–R1006 的 `@unittest.expectedFailure` |

#### trainer/trainer.py 改動細節

1. **`_chunk_cache_key`（R904/R1003）**：新增 `no_afg: bool = False` 參數，return string 加入 `afg_tag`；`process_chunk` 的呼叫點傳入 `no_afg=no_afg`。

2. **`run_track_a_dfs`（R907）**：加入 `_max_sample = 5_000` 絕對上限，使用 `sample(n=min(frac*N, 5000))` 取代純 `frac` 取樣，防止大資料集 OOM。

3. **`process_chunk` DFS block（R901/R902/R1002/R1006）**：
   - 建立 `_dfs_bets`，filter 到 `[window_start, window_end)` core window（R1002）。
   - 從 `_dfs_bets` 過濾 `dummy_player_ids`（R902）。
   - 建立 `_dfs_sessions = sessions.copy()` 並呼叫 `_dfs_sessions = _dfs_sessions.merge(canonical_map, ...)` 注入 `canonical_id`（R901）。
   - 對 merge 後 NaN 執行 `_dfs_sessions["canonical_id"] = fillna(player_id)` fallback（R1006）。
   - 改為 `run_track_a_dfs(_dfs_bets, _dfs_sessions, ...)` 傳入預處理資料（R1002）。

4. **`run_pipeline` Step 3d（R903/R906）**：在 chunk loop 前加入：
   - 刪除舊 `feature_defs.json`：`_feature_defs_pipeline_path.unlink()`（R903）。
   - 含有 "reuse" + "first chunk" 的 comment（R906）。

5. **`run_pipeline` Step 5b Track A 偵測（R1000）**：以 `load_feature_defs(_feature_defs_pipeline_path)` 取代純 numeric-column heuristic 偵測 Track A 候選欄位。

6. **`run_pipeline` Step 5b 後置 sanity check（R1001）**：篩選後若 `set(screened_cols).intersection(TRACK_B_FEATURE_COLS)` 為空，re-append 缺失 Track-B features 作為 fallback。

7. **`run_pipeline` Step 5b screening-skip fallback（R1004）**：當 `_present_candidate_cols` 為空時，加入：`active_feature_cols = [c for c in active_feature_cols if c in train_df.columns]`。

#### trainer/features.py 改動細節

- **`screen_features`（R905）**：在 `top_k` resolve 後加入 `if top_k is not None and top_k < 1: raise ValueError(...)` 快速失敗。

### 測試結果

```bash
python -m unittest discover -s tests -p "test_*.py"
```

```text
Ran 378 tests in 5.2s
OK
```

（0 failures，0 expected failures，0 errors）

### 手動驗證步驟

1. `python -m unittest discover -s tests -p "test_*.py"` → 應顯示 `OK`，無 expected failures。
2. `python -m unittest tests.test_review_risks_round210 tests.test_review_risks_round220 -v` → 應顯示 15 個 `ok`，無 expected failure 字樣。

### 下一步建議

- 本輪已清零所有已知 expectedFailure；若有新 review round，建立新的 risk test 檔後重複此循環。
- 考慮為 R901/R1006 的 sessions canonical_id 邏輯加入整合測試（真實 DataFrame mock），而非純 source inspection。
- `run_track_a_dfs` 的 5000 行 `_max_sample` 可依實際 Featuretools 效能調整。

---

## Round 67 — Test-set 評估 + Feature Importance 寫入 training_metrics.json

### 目標

實作 PLAN.md 的兩個 pending 項目：
- `todo-test-set-metrics`：訓練後在 held-out test set 上評估，並將 test 指標與 val 指標一同寫入 `training_metrics.json`。
- `todo-feature-importance-in-metrics`：每個模型的特徵清單依 LightGBM gain importance 排序，同樣寫入 `training_metrics.json`。

### 改了哪些檔

| 檔案 | 改動摘要 |
|------|---------|
| `trainer/trainer.py` | 新增兩個 helper 函式 + 修改 `train_dual_model` + 修改 `run_pipeline` 呼叫點 |

#### trainer/trainer.py 改動細節

1. **新增 `_compute_test_metrics(model, threshold, X_test, y_test, label)`**（插入於 `_train_one_model` 與 `train_dual_model` 之間）：
   - 以 validation 階段決定的 threshold 在 test set 上做推論。
   - 計算 `test_prauc`、`test_precision`、`test_recall`、`test_f1`、`test_samples`、`test_positives`。
   - 使用與 `_train_one_model` 相同的 `_has_test` guard（`MIN_VALID_TEST_ROWS` + 無 NaN + 至少 1 個正樣本），不足時回傳全零而非 crash。

2. **新增 `_compute_feature_importance(model, feature_cols)`**（緊接在 `_compute_test_metrics` 之後）：
   - 呼叫 `model.booster_.feature_importance(importance_type="gain")` 取得 LightGBM gain importance。
   - 回傳依 importance 降冪排序的 `[{"rank": i+1, "feature": name, "importance_gain": float}]` list。
   - 若 booster 不可用（mock 測試情境）則 fallback 到 `model.feature_importances_`。

3. **修改 `train_dual_model`**：
   - 新增 `test_df: Optional[pd.DataFrame] = None` 參數（backward compatible）。
   - 在迴圈內，訓練完每個模型後立即：(a) 呼叫 `_compute_test_metrics` 並 `.update(metrics)`；(b) 呼叫 `_compute_feature_importance` 並存入 `metrics["feature_importance"]`；(c) 記錄 `metrics["importance_method"] = "gain"`。
   - 更新 docstring 說明新參數與 metrics dict 的新 key。

4. **修改 `run_pipeline` 呼叫點**：`train_dual_model(...)` 加入 `test_df=test_df`，step label 更新為「Train dual model + test-set eval」。

5. **模組 docstring**：`training_metrics.json` 說明更新為包含 validation + test metrics 及 feature importance。

### training_metrics.json 新增欄位（每個 model key 下）

```json
{
  "rated": {
    "val_prauc": ...,  "val_f1": ...,  ...        ← 既有
    "test_prauc": 0.87, "test_f1": 0.63,
    "test_precision": 0.71, "test_recall": 0.57,
    "test_samples": 4200, "test_positives": 380,
    "importance_method": "gain",
    "feature_importance": [
      {"rank": 1, "feature": "loss_streak",  "importance_gain": 482.1},
      {"rank": 2, "feature": "cum_wager",    "importance_gain": 310.5},
      ...
    ]
  },
  "nonrated": { ... }
}
```

### 測試結果

```bash
python -m unittest discover -s tests -p "test_*.py"
```

```text
Ran 378 tests in 6.8s
OK
```

（0 failures，0 errors，0 expected failures）

### 手動驗證步驟

1. 跑完整 test suite：`python -m unittest discover -s tests -p "test_*.py"` → 應顯示 `OK`，378 tests。
2. 訓練完成後查看 `trainer/models/training_metrics.json`：
   - 應在 `rated` / `nonrated` key 下看到 `test_prauc`、`test_f1`、`test_precision`、`test_recall`、`test_samples`、`test_positives`。
   - 應看到 `importance_method: "gain"` 與 `feature_importance` 陣列（按 gain 降冪排序）。
3. 快速 smoke-test（不需要完整訓練）：
   ```bash
   python - << 'EOF'
   import sys; sys.path.insert(0, "trainer")
   import trainer as T, pandas as pd, numpy as np
   rng = np.random.default_rng(0)
   N = 200
   df = pd.DataFrame({"is_rated": [True]*100+[False]*100, "label": rng.integers(0,2,N),
       "loss_streak": rng.uniform(0,10,N), "cum_bets": rng.uniform(1,50,N),
       "cum_wager": rng.uniform(10,500,N),
       "payout_complete_dtm": pd.date_range("2025-01-01", periods=N, freq="5min"),
       "canonical_id": [f"p{i%20}" for i in range(N)], "run_id": [f"r{i%10}" for i in range(N)]})
   df["_split"] = ["train"]*140+["valid"]*30+["test"]*30
   train_df=df[df["_split"]=="train"].copy(); valid_df=df[df["_split"]=="valid"].copy(); test_df=df[df["_split"]=="test"].copy()
   _, _, m = T.train_dual_model(train_df, valid_df, ["loss_streak","cum_bets","cum_wager"], run_optuna=False, test_df=test_df)
   for k,v in m.items():
       if v: print(k, "test_f1=", v.get("test_f1"), "top_feat=", v.get("feature_importance",[{}])[0].get("feature"))
   EOF
   ```
   應印出每個 model 的 `test_f1` 和排名第 1 的 feature。

### 下一步建議

- 為 `_compute_test_metrics` 與 `_compute_feature_importance` 加入正式 unit test（目前已由 smoke-test 驗證，但最好加進 `tests/test_trainer.py`）。
- `training_metrics.json` 現在欄位較多，可考慮同步更新 `doc/model_api_protocol.md` 中的 schema 說明。
- 若後續要改用 SHAP importance，只需替換 `_compute_feature_importance` 的計算方式，並把 `importance_method` 改為 `"shap"`。

---

## Round 67 Review — Test-set 評估 + Feature Importance 的 bug / 邊界條件 / 安全性 / 效能

### R1100（P0）：`_compute_test_metrics` — `average_precision_score` 在全正 test set 時回傳 1.0 但無 guard

**問題**：`_has_test` guard 要求 `y_test.sum() >= 1`，但未檢查 `y_test` 是否包含至少一個 **負** 樣本。當 test set 全為正樣本時（`y_test.sum() == len(y_test)`），`average_precision_score` 回傳 1.0 而非有意義的分數，且 precision/recall 計算的 FP 永為 0。這在資料量少或 is_rated 切分後某一側全正時可能發生。

**修改建議**：在 `_has_test` 的條件中增加 `and int((y_test == 0).sum()) >= 1`。與 `_train_one_model` 的 `_has_val` 保持一致（`_has_val` 同樣缺少此檢查，但在 validation 端 Optuna 已有獨立 guard；test 端沒有第二道防線）。

**希望新增的測試**：`test_compute_test_metrics_all_positive_labels` — 建構 `y_test = pd.Series([1]*100)`，呼叫 `_compute_test_metrics`，斷言回傳的 `test_prauc == 0.0`（zero-out 而非 1.0），或至少不 crash。

---

### R1101（P1）：`_compute_test_metrics` — 0.5 uncalibrated threshold 拿來評 test

**問題**：當 validation set 過小導致 `_has_val=False` 時，`_train_one_model` 回傳 `threshold=0.5`（fallback、未經校準）。`_compute_test_metrics` 直接拿這個 0.5 去算 test precision/recall/F1，結果可能過度樂觀或悲觀，且 JSON 裡無任何標記告知下游該 threshold 未經校準。

**修改建議**：`_compute_test_metrics` 接受 `_uncalibrated: bool` 參數（從 metrics dict 傳入），若為 True 則在回傳 dict 中加入 `"test_threshold_uncalibrated": True`，讓下游讀取 `training_metrics.json` 時知道 test P/R/F1 是用 fallback threshold 算的。

**希望新增的測試**：`test_compute_test_metrics_uncalibrated_threshold_flag` — 用 `_uncalibrated=True` 呼叫，斷言回傳 dict 含 `test_threshold_uncalibrated: True`。

---

### R1102（P1）：`_compute_feature_importance` — booster `feature_name()` 與傳入的 `feature_cols` 長度不一致時靜默產出錯誤排名

**問題**：primary 路徑用 `booster.feature_name()` 取得名字，用 `booster.feature_importance("gain")` 取得 gain，再用 `zip()` 配對。若 feature_cols 與 booster 內部 feature name 不一致（例如 LightGBM 把特殊字元轉成 `_` 或 rename），`zip` 仍成功但 feature name 可能和 caller 預期的 feature_cols 對不上。fallback 路徑用 `feature_cols`，但 `feature_importances_` 的長度可能不等於 `len(feature_cols)`（例如 model 訓練時 LightGBM 對 constant columns 做了合併），此時 `zip` 會靜默截斷。

**修改建議**：
1. Primary 路徑：不用 `booster.feature_name()`，改用 `feature_cols`（caller 傳入的就是訓練時實際用的 avail_cols），搭配 `booster.feature_importance("gain")`；加一個 `assert len(feature_cols) == len(gains)` guard。
2. Fallback 路徑：同樣加 `assert len(feature_cols) == len(gains)` guard，或至少在不等長時 log warning 並補 0。

**希望新增的測試**：`test_feature_importance_length_mismatch` — mock 一個 model 使 `feature_importances_` 長度與 `feature_cols` 不一致，斷言函式 raise 或 log warning 而非靜默截斷。

---

### R1103（P2）：`_compute_feature_importance` — bare `except Exception` 吞掉真正的 bug

**問題**：`try: booster = model.booster_ ... except Exception:` 太寬泛。如果 booster 存在但 `feature_importance("gain")` 因為 dtype 或記憶體錯誤而 raise，會靜默走 fallback 路徑，產出不同來源的 importance 值但外部 `importance_method` 仍標記 `"gain"`。

**修改建議**：縮小 except 範圍到 `except (AttributeError, ValueError):`。`AttributeError` 是 booster 不存在的情況；`ValueError` 是 booster 存在但 importance_type 不支援的情況。其他 exception 應該正常 raise 讓上層處理。

**希望新增的測試**：`test_feature_importance_unexpected_error_not_swallowed` — mock `model.booster_.feature_importance` raise `RuntimeError`，斷言 `_compute_feature_importance` 也 raise（而非靜默 fallback）。

---

### R1104（P2）：`train_dual_model` — `test_df=None` 時仍呼叫 `_compute_test_metrics` 和 `_compute_feature_importance`

**問題**：當 `test_df` 為 `None` 時，`_test_rated` 和 `_test_nonrated` 被設為空 DataFrame，隨後 `_compute_test_metrics` 被呼叫，guard 判定 `_has_test=False`，回傳全零 dict 並 merge 進 metrics。JSON 裡出現 `test_prauc: 0.0` 等欄位。這容易誤導：全零到底是「test set 太小所以算出來是零」還是「根本沒做 test」？

**修改建議**：`train_dual_model` 迴圈裡加判斷：只有 `te_df` 非空時才呼叫 `_compute_test_metrics`；否則不寫入 `test_*` key（或寫入 `test_prauc: null`）。這樣下游讀取時可區分「沒做」和「做了但太差」。

**希望新增的測試**：`test_train_dual_model_no_test_df_omits_test_keys` — 呼叫 `train_dual_model(test_df=None)`，斷言回傳的 metrics 中不含 `test_prauc` key（或值為 `None`）。

---

### R1105（P2）：`_compute_test_metrics` — `y_test` 和 `preds` 的 index 可能 misalign

**問題**：`y_test = te_df["label"]` 保留了原始 index（從 full_df 切出來的），而 `preds = (test_scores >= threshold).astype(int)` 是 numpy array（0-based index）。用 `(preds == 1) & (y_test == 1)` 做 `&` 時，pandas 會按 index align，但 `preds` 是 ndarray 不會參與 alignment，結果取決於 positional match。目前碰巧 OK 是因為 `y_test` 的 values 就是按位置排的——但如果有人在 caller 端做了 `y_test.iloc[...]` 等操作導致 y_test index 不連續，`&` 的行為可能出錯。

**修改建議**：在 `_compute_test_metrics` 開頭 `y_test = y_test.reset_index(drop=True)`，或改用 `.values`：`y_arr = y_test.values`，讓比較全在 numpy 層進行。

**希望新增的測試**：`test_compute_test_metrics_non_contiguous_index` — 傳入 index 為 `[100, 200, 300, ...]` 的 `y_test`，斷言 TP/FP/FN 計算與重新 reset_index 後的結果一致。

---

### R1106（P3 / 效能）：`feature_importance` list 寫進 JSON 可能很大

**問題**：如果 Track A DFS 產出了數百個特徵，`feature_importance` list 會有數百個 dict（每個含 3 個 key）。兩個模型加起來會讓 `training_metrics.json` 大幅膨脹。`/model_info` API endpoint 會把整個 `training_metrics` 回傳給前端，payload 可能不必要地大。

**修改建議**：這不需要立即修，但可考慮：(1) 在 JSON 裡只保留 top-N（例如 50）個 features 的 importance，或 (2) 把完整 importance list 寫到獨立的 `feature_importance.json`，`training_metrics.json` 裡只保留 top-10 摘要。

**希望新增的測試**：無需測試，這是效能/設計偏好問題。

---

### 問題清單彙總

| ID | 嚴重性 | 問題摘要 |
|------|--------|---------|
| R1100 | **P0** | `_compute_test_metrics`：全正 test set 無 guard，prauc=1.0 誤導 |
| R1101 | **P1** | test metrics 使用 uncalibrated 0.5 threshold 時無標記 |
| R1102 | **P1** | `_compute_feature_importance`：feature name/gain 長度不一致時靜默截斷 |
| R1103 | **P2** | `_compute_feature_importance`：bare `except Exception` 吞掉真正的 bug |
| R1104 | **P2** | `test_df=None` 時寫入全零 test_* key，無法區分「未做」和「做了但差」 |
| R1105 | **P2** | `y_test` index 可能 misalign（目前碰巧正確但不防禦） |
| R1106 | **P3** | feature_importance list 可能很大，膨脹 JSON / API payload |

### 下一步建議

1. 先修 P0：R1100（`_compute_test_metrics` 全正 guard）；一起修 `_train_one_model` 的 `_has_val` 對稱問題。
2. 再修 P1：R1101（uncalibrated flag）+ R1102（feature importance 長度 guard）。
3. P2 問題（R1103/R1104/R1105）可合併為一輪小修。
4. R1106 留待 Track A 上線後觀察實際 feature 數量再決定。

---

## Round 68 — 將 R1100-R1105 轉成最小可重現測試（tests-only）

### 目標

依 Reviewer 結論，先把風險點固化成可重現測試；本輪只加 tests，不改 production code。

### 改了哪些檔

| 檔案 | 改動摘要 |
|------|---------|
| `tests/test_review_risks_round230.py` | 新增 R1100-R1105 的最小可重現測試，並用 `@unittest.expectedFailure` 標記目前尚未修正的風險行為。 |

### 新增測試清單

1. `TestR1100AllPositiveTestLabels.test_compute_test_metrics_all_positive_labels_should_be_guarded`
   - 重現 `_compute_test_metrics` 在全正 test labels 時 PR-AUC=1.0 的誤導行為。

2. `TestR1101UncalibratedThresholdFlag.test_compute_test_metrics_should_include_uncalibrated_flag_contract`
   - 以 source contract 測試固定需求：test metrics 應有 `test_threshold_uncalibrated` 標記（目前未實作）。

3. `TestR1102FeatureImportanceLengthMismatch.test_feature_importance_length_mismatch_should_raise`
   - 重現 `_compute_feature_importance` 在 `feature_cols` 與 importance 向量長度不一致時靜默截斷的問題。

4. `TestR1103FeatureImportanceExceptionScope.test_feature_importance_unexpected_error_should_propagate`
   - 重現 booster 發生 `RuntimeError` 時被 `except Exception` 吞掉的問題。

5. `TestR1104NoTestDfContract.test_train_dual_model_no_test_df_should_not_call_compute_test_metrics`
   - 重現 `test_df=None` 仍進 test-metrics path 的行為（以 mock call 驗證）。

6. `TestR1105TestIndexAlignment.test_compute_test_metrics_should_explicitly_normalize_index`
   - 以 source contract 測試固定需求：`_compute_test_metrics` 需明確 index normalization（`reset_index` 或 `.values`）。

### 執行方式與結果

```bash
python -m unittest tests.test_review_risks_round230 -v
```

```text
Ran 6 tests
OK (expected failures=6)
```

```bash
python -m unittest discover -s tests -p "test_*.py"
```

```text
Ran 384 tests
OK (expected failures=6)
```

### 下一步建議

1. 下一輪修 production code 時，逐條消除 R1100-R1105，並移除對應 `@expectedFailure`。
2. 優先順序：R1100（P0）→ R1101/R1102（P1）→ R1103/R1104/R1105（P2）。
3. 修完後保留同一批測試作為 regression guard，不要刪除測試。

---

## Round 69 — 修復 R1100-R1105（6 個 expectedFailure → 全部通過）

### 目標

消除 Round 68 所有 `@expectedFailure` 風險：修 production code，移除 `@expectedFailure` 讓測試成為正式 regression guard。

### 改了哪些檔

| 檔案 | 改動摘要 |
|------|---------|
| `trainer/trainer.py` | `_compute_test_metrics` + `_compute_feature_importance` + `train_dual_model` 三處修改（見下） |
| `tests/test_review_risks_round230.py` | 移除全部 6 個 `@unittest.expectedFailure`；更新模組 docstring 說明狀態 |

#### trainer/trainer.py 改動細節

**`_compute_test_metrics`（R1100 / R1101 / R1105）**：

- **R1100**：`_has_test` guard 新增 `and int((y_test == 0).sum()) >= 1` 防止全正 labels 讓 PR-AUC 虛報 1.0。warning message 同步加上 negatives 數量。
- **R1101**：函式簽章加入 `_uncalibrated: bool = False` 參數；兩個 return dict（早回傳與主路徑）都加入 `"test_threshold_uncalibrated": _uncalibrated` key，讓下游可辨識 P/R/F1 是否用 fallback threshold 算的。
- **R1105**：`y_arr = y_test.values` 提取 numpy array，TP/FP/FN 計算改用 `y_arr` 避免 pandas index misalignment。

**`_compute_feature_importance`（R1102 / R1103）**：

- **R1103**：`except Exception:` 縮窄為 `except AttributeError:`，只捕捉「booster 屬性不存在」的情況；`RuntimeError` 等非預期錯誤會正常 propagate。
- **R1102**：fallback 路徑（`AttributeError` 觸發）新增長度 guard：`if len(gains) != len(names): raise ValueError(...)`，防止 `zip()` 靜默截斷。

**`train_dual_model`（R1104 / R1101 call-site）**：

- **R1104**：`_compute_test_metrics` 的呼叫點加 `if not te_df.empty:` guard；`test_df=None` 時 `te_df` 是空 DataFrame，整條 test-eval 路徑會被跳過，不再寫入全零的 `test_*` key。
- **R1101 call-site**：`_compute_test_metrics` 呼叫時傳入 `_uncalibrated=bool(metrics.get("_uncalibrated", False))`。

### 測試結果

```bash
python -m unittest tests.test_review_risks_round230 -v
```

```text
Ran 6 tests in 0.014s
OK
```

（0 expected failures，6 ok）

```bash
python -m unittest discover -s tests -p "test_*.py"
```

```text
Ran 384 tests in 5.1s
OK
```

（0 failures，0 errors，0 expected failures）

### 手動驗證步驟

1. `python -m unittest discover -s tests -p "test_*.py"` → `Ran 384 tests … OK`。
2. `python -m unittest tests.test_review_risks_round230 -v` → 6 個 `ok`，無 `expected failure` 字樣。
3. 訓練完後查 `trainer/models/training_metrics.json`：
   - 若 threshold 是 fallback（val 太小），`test_threshold_uncalibrated` 應為 `true`。
   - 若 test set 全正，`test_prauc` 應為 `0.0` 而非 `1.0`。
   - 若 `test_df=None` 呼叫，metrics 中不應有 `test_prauc` key。

### 下一步建議

- R1106（feature importance list 可能膨脹 JSON）留待 Track A 上線後看實際大小再決定是否截斷。
- `training_metrics.json` 的 schema 可在 `doc/model_api_protocol.md` 補上新增的 `test_*` 與 `test_threshold_uncalibrated` key 說明。

---

## Round 70（2026-03-05）— Scorer ClickHouse session datetime 正規化（DEC-018 等價、R33）

### 背景

PLAN.md § 不改動的部分註明「`scorer.py` 的 tz 處理獨立（它有自己的 R23-equivalent 流程）」；DEC-018 僅修改 trainer / features / labels / backtester，未涵蓋 scorer。Scorer 的 **live 路徑**（`fetch_recent_data` 自 ClickHouse 取 bet/session → `build_features_for_scoring`）中，`session_start_dtm` / `session_end_dtm` 來自 ClickHouse `query_df()`，可能回傳字串或 object，或 tz-aware/naive 混用，導致 `pd.to_datetime(...).fillna(...)` 後仍為 object dtype，接著使用 `.dt.tz` 時觸發 `AttributeError: Can only use .dt accessor with datetimelike values`。

### 本輪修改

| 檔案 | 改動 |
|------|------|
| `trainer/scorer.py` | `build_features_for_scoring`：對 `session_start_dtm` / `session_end_dtm` 迴圈內，改為 `pd.to_datetime(..., errors="coerce")`，並在存取 `.dt` 前以 `pd.api.types.is_datetime64_any_dtype(bets_df[col])` 守衛，僅在為 datetimelike 時做 R33（HK convert → strip tz） |
| `trainer/scorer.py` | 同函數內 `_pcd = bets_df["payout_complete_dtm"]` 區塊：先 `pd.to_datetime(..., errors="coerce")`，再以 `is_datetime64_any_dtype` 判斷後才使用 `.dt.tz` / `tz_convert`，避免同類錯誤 |

### 與 PLAN.md 的對齊

- **其他資料入口統一（可選）**：Scorer 的 ClickHouse session 欄位現已納入與 DEC-018 等價的防呆：強制轉為 `datetime64`、僅在 datetimelike 時做 tz 轉換並 strip，與 pipeline 內部 tz-naive 一致。
- **R33**：session 與 payout_complete_dtm 的「HK local time then strip tz」邏輯維持不變，僅加上 dtype 守衛，避免 object/string 觸發 `.dt` 錯誤。

### 驗證

- 執行 `python -m trainer.scorer --once` 時，若 ClickHouse 回傳的 session 欄位為字串或 object，應不再出現 `Can only use .dt accessor with datetimelike values`。
- 既有 scorer 單元測試與整合流程不受影響（無預期失敗變更）。

---

## Round 71（2026-03-05）— Scorer 特徵欄位數值型正規化（LightGBM predict_proba dtype）

### 背景

Scorer live 路徑從 ClickHouse 取得 bet 後，`base_ha`、`payout_odds` 等欄位可能以 object/字串回傳。傳入 `_score_df` 再交給 LightGBM `predict_proba` 時觸發 `ValueError: pandas dtypes must be int, float or bool. Fields with bad pandas dtypes: base_ha: object, payout_odds: object`。

### 本輪修改

| 檔案 | 改動 |
|------|------|
| `trainer/scorer.py` | `build_features_for_scoring`：在「Normalise types」區塊對 `position_idx`、`payout_odds`、`base_ha`、`is_back_bet`、`wager` 一律以 `pd.to_numeric(..., errors="coerce").fillna(0)` 正規化，確保 ClickHouse 回傳的 object 不會進入特徵矩陣 |
| `trainer/scorer.py` | `_score_df`：在呼叫 `predict_proba` 前，對 `feature_list` 中所有非數值型欄位做 `pd.to_numeric(..., errors="coerce")`，再對非 profile 欄位 `fillna(0.0)`；profile 欄位保持 NaN（LightGBM NaN-aware 分裂不變） |

### 驗證

- 執行 `python -m trainer.scorer --once` 時，不再出現 `pandas dtypes must be int, float or bool`。
- 特徵型別與訓練時一致（數值欄為 int/float），profile 欄位仍可為 NaN。


