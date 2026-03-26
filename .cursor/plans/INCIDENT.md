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

---

# INCIDENT — Validator「No bet data」與 TBET `player_id` 漂移

Date: 2026-03-26  
Scope: `trainer/serving/validator.py`（CH bet fetch、no-bet retry）、ClickHouse `TBET`（`FINAL`）、`state.db` / `prediction_log.db`、canonical mapping 語意

## Executive summary

生產環境中，部分 alerts 在 validator 端長期出現 `No bet data … leaving PENDING`，而同一筆注單在 scorer 週期內曾成功自 TBET 讀出並寫入 `prediction_log`。  
調查結論：**TBET 中同一 `bet_id` 的列會隨 ETL / merge（例如 ReplacingMergeTree + `FINAL`）在時間上演化，`player_id` 可能從「匿名桌位 id」被改寫為與 rated 身份相關的另一數值**。Validator 以 `player_id` + 時間窗查詢時，若已漂移則回傳 0 列。業務上 **`bet_id` 視為穩定主鍵**，與「僅依凍結的 `player_id` 查詢」形成對照。  
長期應重新檢視 canonical mapping 的建構方式（見「Long-term」）。**修復實作計畫**（含方案 C：`bet_id` 錨定查詢）見 [`PATCH_20260324.md`](PATCH_20260324.md) **Task 9C**。

## Symptoms

- 主控台反覆出現：  
  `[validator] No bet data for casino_player_id=None player_id=… bet_id=… — leaving PENDING (cannot verify late arrivals)`
- 受影響列常見模式（**非充分條件**）：`alerts.casino_player_id IS NULL`，且 `canonical_id != str(player_id)`（歷史上曾由 session 的 `casino_player_id` 解析出 rated canonical）。
- 同一 `player_id` 在 `prediction_log` 中可見較早列帶 `casino_player_id`，最後一手 walkaway 列為 `NULL`，與「最後一手未刷卡」一致，但 **單靠此模式無法解釋為何 CH 查不到注單**（見下節統計）。

## Impact

- 這些 alerts 長期維持 `PENDING`，無法進入 rolling precision 分母或延遲納入。
- 營運上誤以為 validator 或 canonical 邏輯「壞掉」；實際上 **player 維度查詢與 TBET 當下列內容不一致**。
- 若未修復，任何僅依 `player_id` + 時間窗的 retry 都會在 `player_id` 已漂移時永久失敗。

## Evidence（2026-03-26 快照：`.tmp/local_state` + `.tmp/log.txt`）

### SQLite：`state.db`

- `alerts` 共 1028 筆；其中 **748 筆** `casino_player_id IS NULL`（約 73%）。
- 在 **NULL** 子集合中，`canonical_id != player_id` 共 **748 / 748**（此 deploy 下 NULL 即伴隨非自反 canonical，屬資料形態而非故障訊號）。
- 同批 NULL-cpid alerts 中，**711 筆**已有 `validation_results`（MATCH/MISS），僅 **37 筆**尚未驗證；問題列為其中 **6 名 player、7 筆 alert**（含 `165948477` 兩筆同 `bet_ts`）。
- 範例（問題列）：`bet_id` / `player_id` / `canonical_id` / `bet_ts`  
  `558517939` / `141378111` / `607388` / `2026-03-25T22:16:53`  
  （其餘類推，見調查紀錄。）
- **對照**：`player_id=174572180`, `bet_id=602049383` 最終在 `validated_at=2026-03-26T09:37:41` 寫入 **MATCH**，`gap_minutes=45.0`；顯示 **CH 後來又能對上該注單**，與「列內容隨時間變化」一致。

### SQLite：`prediction_log.db`

- `player_id=141378111`：同一日先有多筆 `casino_player_id=607388` 的非 alert 列，後有 **一筆** alert 列 `bet_id=558517939`, `casino_player_id=NULL`。  
  證明 **scorer 曾從 TBET 以當時可見的資料讀到該 `bet_id` 與 `player_id`**。
- 多個「已成功驗證」的 NULL-cpid 玩家，在 `prediction_log` 中同樣長期僅有 `canonical_id != player_id`，與問題列 **形態相同**，故 **形態不是根因**。

### 日誌：主 fetch 窗口與 retry

- **主 fetch**（`hard_floor = now - max_lookback`）在 07:29 附近約為  
  `fetch_start ≈ 2026-03-26 04:29+08` ~ `fetch_end ≈ 07:29+08`，  
  無法涵蓋 `bet_ts` 在 **03-25 22:xx ~ 03-26 06:xx** 的待驗列；屬 **設計上的 lookback 限制**，依賴 retry。
- **No-bet retry** 對 `player_id=141378111` 等逐筆查詢，窗口例如  
  `start=2026-03-25 21:16:53+08:00`, `end=2026-03-25 23:18:53+08:00`，**幾何上完全覆蓋** `bet_ts=22:16:53`，但 **連續多輪 `rows=0`**（`failed_queries=0`，非查詢例外）。
- `174572180` 在 **07:29–09:32** 間 retry 均為 `rows=0`；DB 上 **09:37** 已 MATCH。日誌檔止於 09:32，未收錄成功當輪，但與「稍後同一查詢語意下資料重現」相容。

### Canonical mapping（`.tmp/canonical_mapping.parquet`）

- 問題玩家均存在 mapping；每個 `canonical_id` 僅對應 **單一** `player_id`。
- 這些 `canonical_id` **未**作為 `player_id` 出現在 mapping 中；故 **僅擴充「依 canonical 反查多 player_id」無法覆蓋「TBET 已改寫為第三種 player_id」** 的情況（若發生）。

## Root cause（結論）

**主因：TBET 在 `FINAL` 視角下，同一 `bet_id` 對應列的 `player_id`（及其他欄位）會隨後續 ETL / merge 更新；validator 僅以 alert 上凍結的 `player_id` 查詢時，可能在後續時間點得到空集。**

**輔因：**

1. **主 fetch 時間窗**受 `VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES` 等限制，舊 `bet_ts` 常落在窗外，必須靠 retry；retry 仍用 `player_id` 時會與主因疊加。
2. **`casino_player_id=NULL` 與 `canonical_id != player_id` 高度相關**為 scorer 資料形態（最後一手未刷卡仍沿用歷史 canonical），**與「查不到 TBET」無直接因果**；成功驗證的 NULL-cpid 樣本已反證。

**信心度：**  
- 「retry 窗口正確卻 `rows=0`」+「scorer 曾讀到同 `bet_id`」+「一筆最終在數小時後 MATCH」——強烈支持 **TBET 列演進** 假說。  
- 未在當次環境直接對 CH 做 `bet_id` 與 `player_id` 交叉比對；**`bet_id` 補查上線後**應以 production 抽樣驗證該查詢是否穩定回傳列（見 PATCH Task 9C）。

## Long-term（canonical mapping）

目前 mapping 多來自 session / 歷史拼接，**未必與 TBET 在某一時刻的 `player_id` 完全一致**。長期建議（另開設計項，本 incident 不實作）：

- 以 **`bet_id` 或 (session_id, 時間) 等與 TBET 對齊的鍵** 作為 identity 解析的一級來源；
- 或維護「`player_id` ↔ TBET 當前列」的 **版本化 / 觀測時點** 語意，避免單一靜態 mapping 假設；
- 與 DEC-028（deploy 下 canonical 檔重用）一併檢討：**檔案不更新時，TBET 側 drift 只能靠每筆注單級查詢補齊**。

## Reference（修復計畫，非本檔範圍）

- **決策**：以 **`bet_id` 定向查 TBET（方案 C）** 補強 no-bet retry，將 `payout_complete_dtm` 併入 `bet_cache`；不以「僅把 `canonical_id` 當 `player_id` 查」作為唯一修復。  
- **完整目標、設計取捨、實作步驟、DoD、開放問題**：見 [`PATCH_20260324.md`](PATCH_20260324.md) **Task 9C — Validator：No-bet 補查以 `bet_id` 錨定 TBET（方案 C）**。

