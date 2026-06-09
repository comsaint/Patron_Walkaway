# Step 3 提早裁月 Patch Plan

**層級：** Working / execution patch（承接 [Training Acceleration and Scope - WORKING_PLAN.md](Training%20Acceleration%20and%20Scope%20-%20WORKING_PLAN.md) 之 **TA-WP-2.3**）

**狀態：** completed（2026-06-09）

**實作 commit：** `9506c34` — `fix(step3): prune month-batch assembly to training target scope`

**概述：** 以最小改動把 target scope 提前到 Step 3 的 month batching / assembly 階段，只 assemble selected target months，同時保留 short/mid/slow/labels 的 source history 與既有 primitive cache key。

---

## 目標

- 修正目前 `build_training_data()` 先全量 month-batch assemble、再由 `trainer.py` 做 horizon filter 的 implementation drift。
- 讓 `recent_full_months=3` 時，Step 3 僅對 selected target months 建 entity parquet、Feast group retrieval、slow-snap attach 與 merged training rows。
- 保留既有 feature source history：不能因 target scope 變窄而截短 cleaned bet/session lookback、slow 180d snapshot、labels 決定視窗。

## 最小改動路徑

- 在 [`trainer.py`](../../../trainer.py) 的 `_maybe_build_training_dataset()` 先取得 `ResolvedTrainingScope`，並把它傳入 Step 3。
- 在 [`03_build_training_data.py`](../../../03_build_training_data.py) 的 `BuildTrainingDataArgs` 新增 `target_scope: ResolvedTrainingScope | None = None`。
- 在 `build_training_data()` 中，保留 `_prediction_visible_month_starts()` 讀全量可見月份，但在月迴圈前新增過濾 helper，只保留 `target_scope.target_months`。
- 在 `_write_entity_parquet()` 新增可選的 partial-month 上界（例如 `target_end_date`），讓 current partial month 不會整月都被 assemble。
- 保留 [`trainer.py`](../../../trainer.py) 的 `_prepare_training_features_parquet()` / `_apply_training_scope_horizon_to_parquet()`：它改為第二道 safety net，用 `gaming_day_event` 做最終對齊與 completeness audit，而不是主要裁月點。

## 具體修改

### `03_build_training_data.py`

- 擴充 `BuildTrainingDataArgs`。
- 新增小型 helper，例如 `_filter_month_starts_for_target_scope(months, resolved)`。
- 在 `build_training_data()` 的 `months = _prediction_visible_month_starts(...)` 之後立即過濾 target months。
- 對 partial target month，把 `target_end_date` 傳給 `_write_entity_parquet()`，只產出截至 `as_of_date` 的 entity rows。
- 將 resolved target scope 寫進 training-set manifest / metrics block 作 run audit，但不要把它放入 Feast primitive cache key。

### `trainer.py`

- 在 `_maybe_build_training_dataset()` 解析或重用 `metrics["_resolved_training_scope"]`。
- 把 `target_scope` 傳入 `BuildTrainingDataArgs`。
- 保留 `_prepare_training_features_parquet()` 現有 horizon filter、completeness 與 reporting。

### `config.py`

- 只重用既有 `TrainingScopePolicy` / `ResolvedTrainingScope`；原則上不新增新 policy 物件。

## Cache / 正確性護欄

- 不改 `feast_month_group_v1` primitive cache key；它仍只看 cleaned fingerprint、group id、code/derived stat，不看 target scope。
- 不改 short-PIT month shard 或 slow 180d monthly primitive 的 key；target scope 改變應只影響 Step 3 assembly、Step 4 split、sampled train、feature selection、model artifacts。
- 必須保留 feature source history：
  - short/mid 仍可從全量 cleaned bet 讀 lookback
  - slow 仍讀完整 canonical monthly snapshot
  - labels 仍保留現有 forward-lookahead / censoring 判定
- 不能只用 `prediction_visible_ts_cf` 的月份過濾取代 `gaming_day_event` horizon filter；兩者時間軸不同，最終仍要保留 `gaming_day_event` 對齊。

## 驗證

### 單元 / focused tests

- 更新 [`tests/test_training_scope_policy.py`](../../../tests/test_training_scope_policy.py)：新增 recent horizon 下 Step 3 month pruning 驗證。
- 新增或擴充 Step 3 測試，驗證 `build_training_data()` 只 iterate target months，且非 target months 不觸發 Feast retrieval / slow-snap attach。
- 驗證 partial month 只 assemble 到 `target_end_date`。
- 驗證 horizon policy 變更不會污染 primitive cache key，但會影響 assembly/split 下游。

### 代表性 smoke

- 以 `recent_full_months=3` 跑一次訓練，確認 Step 3 log / manifest 只出現 `202603`–`202606`。
- 比對 `run_report` 中 target scope、row reduction、cache reuse 是否一致。

```bash
pytest trainer_hightier/tests/test_training_scope_policy.py \
       trainer_hightier/tests/test_feast_retrieval_cache_helpers.py -q
python -m trainer_hightier.trainer --skip-optuna
# Step 3 log 預期：target scope month prune: 18 -> 4 month(s) ['202603', '202604', '202605', '202606']
```

## 風險

- `prediction_visible_ts_cf` 月與 `gaming_day_event` 月不一定完全一致；若 partial month 邏輯處理不完整，可能仍會多 assemble 少量 rows，因此需要保留現有 horizon filter 作最終防線。
- 若把 target scope 錯誤納入 primitive cache key，會違反 SSOT 中「target scope 只影響 target rows，不誤 invalidate L0–L5 primitives」的要求。
- 若 Step 3 裁得太早且誤傷 source history，可能導致 short/mid/slow 特徵值錯誤，這是本 patch 必須避免的主要 correctness 風險。

## 執行 checklist（已完成）

| ID | 任務 | 狀態 |
|----|------|------|
| wire-target-scope-into-step3 | 在 trainer 的 Step 3 呼叫鏈傳入 `ResolvedTrainingScope` 至 `BuildTrainingDataArgs` | done |
| prune-step3-month-batches | 在 `build_training_data` 的 month batching 前只保留 selected target months，並處理 partial month entity 上界 | done |
| preserve-cache-and-history | 確認 primitive cache key 不納入 target scope，並保留 source history 語義 | done |
| add-regression-tests | 補 focused tests 防止 Step 3 再出現先全量 assemble 再裁月的回歸 | done |
| run-target-scope-smoke | 以 `recent_full_months=3` 跑代表性 smoke，驗證 Step 3 log、artifacts 與 run report | done |

## 相關文件

- [Training Acceleration and Scope - SSOT.md](../../ssot/Training%20Acceleration%20and%20Scope%20-%20SSOT.md)
- [Training Acceleration and Scope - IMPLEMENTATION_PLAN.md](../../implementation/active/Training%20Acceleration%20and%20Scope%20-%20IMPLEMENTATION_PLAN.md)
- [Training Acceleration and Scope - WORKING_PLAN.md](Training%20Acceleration%20and%20Scope%20-%20WORKING_PLAN.md)（TA-WP-2.3）
