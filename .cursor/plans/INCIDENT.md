# INCIDENT — Validator Rolling KPI Under-count

Date: 2026-03-25  
Scope: `trainer/serving/validator.py` (rolling KPI for 15m / 1h)

## Summary

在 deploy runtime 中觀察到：
- `validation_results` 最近幾分鐘已累積大量 finalized rows；
- 主控台 `Cumulative Precision (15m/1h)` 的分母卻偏小，且 15m / 1h 經常相同；
- `validator_metrics` 每分鐘寫入值與上述 KPI 幾乎一致（看起來像近 1 分鐘增量，而非 15m / 1h 滾動窗）。

## Impact

- 線上 KPI 低估（under-count）且可能誤導判讀。
- 15m / 1h 失去分辨力（常相同）。
- 依 `validator_metrics` 做營運監控時，會誤以為近期 precision 只由少量樣本構成。

## Root Cause

根因是「incremental watermark + 空 cache」的組合：

1. `validate_once` 每輪將空 dict 傳給 `load_existing_results_incremental`。  
2. `load_existing_results_incremental` 依 `validator_runtime_meta` 的 rowid watermark 僅載入 `rowid > last_loaded_rowid`。  
3. 因呼叫端每輪都從空 dict 開始，KPI 計算基底退化成「本輪增量（或少量增量）」而非完整 in-memory 狀態。  
4. 後續 `_rolling_precision_by_validated_at` 在縮小集合上計算，導致分母偏小與 15m/1h 同值現象。

## Why not aggregate from `validator_metrics`

`validator_metrics` 的每一列是「該分鐘當下的 15m 滾動窗快照」，列與列高度重疊。  
直接把多列 `total/matches` 相加會重複計數，不可用於事件層級 precision 回算。  
它適合做 trend/monitoring，不適合做 window re-aggregation。

## Decision

採用「保留 cache 路線」：
- 啟動時做 full bootstrap（即便存在 watermark）；
- 後續每輪沿用同一份 in-memory cache，再以 watermark 增量更新；
- KPI 仍以 `validated_at` 的時間窗語意計算（15m / 1h）。

## Remediation Plan

### Phase 1 — Correctness guardrails (immediate)

1. 在 incremental loader 加入 `bootstrap_full` 判斷：當傳入 cache 為空時，強制全表載入。  
2. 在 `run_validator_loop` / `main` 維持 process-level `existing_results_cache`，避免每輪重建空 dict。  
3. 保持現有 KPI 計算語意（`validated_at` in `[now-window, now]`）不變。

DoD:
- 在同一 runtime 連續多輪下，15m total <= 1h total（通常嚴格小於）；  
- KPI 分母與 `validation_results` 同窗查詢量級一致，不再長期偏小。

### Phase 2 — Performance hardening (short-term)

1. 監測 cache 尺寸與每輪耗時（DEBUG telemetry）。  
2. 若 retention 視窗較大，評估：
   - 以 retention cutoff 主動剔除 cache 舊鍵；
   - 只保留 KPI 所需欄位，降低記憶體占用。

DoD:
- 在既有輪詢頻率下無明顯延遲回歸；
- 記憶體 footprint 可觀測且穩定。

### Phase 3 — Regression tests (short-term)

1. 測試：有 persisted watermark + 空 cache 啟動，第一輪需 full bootstrap。  
2. 測試：第二輪僅吃增量，且 KPI 與「全量重算」結果一致。  
3. 測試：15m / 1h 窗格資料分布不同時，不可長期輸出相同分母。

DoD:
- 上述測試全綠，並覆蓋過去 incident path。

## Operational Verification Checklist

1. 重啟 validator process。  
2. 觀察 2-3 個週期：
   - `This cycle: ... verified` 行數合理；
   - `Precision 15m` 與 `Precision 1h` 的 `total` 不再長期相同；
   - `validator_metrics.total` 與當輪 15m KPI 對齊。  
3. 隨機抽樣數筆 `validation_results`，對照其 `validated_at` 是否被納入對應窗格。

## Follow-up

- 若後續仍發現 KPI 與 DB 同窗查詢有偏差，新增一個可開關的 DEBUG 對帳：
  - `db_window_total_15m/1h` vs `kpi_window_total_15m/1h`。
- 文件同步更新：`PATCH_20260324.md` Task 4 追記 incident 與修復狀態。

