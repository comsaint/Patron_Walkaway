# trainer_hightier - Data Pipeline Implementation Plan

本文件為 **Implementation plan 層**，定義如何落實 `Data pipeline - SSOT.md` 的既定決策。  
範圍聚焦 realization strategy、工作流、里程碑、風險與驗證；不展開 ticket 級任務清單。

## 對齊基準

- SSOT: `trainer_hightier/doc/Data pipeline - SSOT.md`
- Scorer runtime SSOT: `trainer_hightier/doc/Scorer Runtime Contract - SSOT.md`
- 時間語意 registry: `schema/time_semantics_registry.yaml`（升版為 v2 後作為機讀真相）

## 實作目標

完成 `gaming_day` -> `gaming_day_event` 的 **Day 1 full migration**，並保證 train/serve/parity/gate 全鏈路一致：

- `gaming_day_event` 成為唯一日級語意欄位。
- cleansing 階段將所有 timestamp（含 Included 與 Excluded）轉為 `Asia/Hong_Kong` 且以 **HK tz-aware** 落盤。
- `DATE` 欄位不做 timezone 轉換；僅由事件時間衍生 `gaming_day_event`。
- 事件時間白名單：
  - `t_bet.event_time = payout_complete_dtm`（NULL drop）
  - `t_session.event_time = session_end_dtm`（NULL ignore，不 fallback）
- 早期 hard-fail 規則：`__etl_insert_Dtm < event_time` 直接中止；`>=` 合法。
- 全歷史回填、分區鍵切換、cache/manifest 一次性重建。

## 非目標（明確排除）

- 不做雙寫/雙讀過渡，不保留 legacy key。
- 不保留舊分區鍵輸出作為新語意流程輸入。
- 不在本計畫中討論模型家族更換或演算法策略改造。

## 模組邊界（Realization Boundaries）

- **Contract/Registry**
  - 將 `schema/time_semantics_registry.yaml` 升版，補齊 timezone、event-day、sanity 規則與 timestamp 白名單。
- **Cleansing/Preprocess**
  - 統一時區轉換與 `gaming_day_event` 衍生。
  - 實作 idempotent 防重轉與早期 sanity gate。
- **Feature/Training**
  - Step 3/3.5/4 與 mid/slow 相關 ASOF 全面切到 `*_gaming_day_event*`。
  - 強制重訓並重校 gate 門檻。
- **Serving/Refresh/Readiness**
  - scorer、refresh、readiness、prediction log、parity、deploy gate 同步切 key。
  - 保留 anchor 事件時間語意為 HK 23:59:59（day-end）。
- **Ops/Monitoring**
  - 外部腳本與看板同日更新新 key。
  - cache/manifest 清空重建流程制度化。

## 工作流與階段（Workstreams / Phases）

### Phase 0: Contract Freeze（先鎖規則）

- 凍結 v2 時間語意契約：timezone、欄位白名單、event_time 規則、sanity gate 規則。
- 明確宣告 legacy key/legacy partition 不再接受。

### Phase 1: Time Standardization in Cleansing

- 在 `t_bet` / `t_session` 清洗步驟完成全 timestamp HK tz-aware 轉換。
- 衍生 `gaming_day_event`，並驗證轉換 idempotent。
- 導入早期 sanity gate（`__etl_insert_Dtm < event_time` hard-fail）。

### Phase 2: Full-History Repartition & Cache Reset

- 以 `gaming_day_event` 重建全歷史分區。
- 一次性清空並重建舊 cache/manifest。
- 產出新語意分區 inventory 與 fingerprint。

### Phase 3: Feature Pipeline Migration

- mid-term snapshot、slow feature、short PIT 上游輸入全面改讀 `gaming_day_event`。
- ASOF anchor 與 audit 欄位命名改為 `*_gaming_day_event*`。
- 與 Feast 定義同步（含 readiness keys）。

### Phase 4: Training / Split / Backtest / Parity

- Step 3 輸出 split keys 改為 `canonical_id + gaming_day_event`。
- Step 4 切分與報表改用 `gaming_day_event`。
- offline backtest、parity 驗證與 deploy gate 全改新語意。
- 完成強制重訓，建立新基線指標。

### Phase 5: Serving & Deploy Cutover

- scorer runtime、refresh supervisor、readiness、prediction log 全部切到新 key。
- 外部監控腳本/看板同日切換與驗證。
- 執行 deploy e2e gate，未通過不得進 production scoring。

## 里程碑與交付物（Milestones / Deliverables）

- **M1 Contract Freeze**
  - 時間語意 registry v2 完成並與兩份 SSOT 對齊。
- **M2 Data Foundation Ready**
  - 全 timestamp HK tz-aware、`gaming_day_event` 衍生、sanity gate 上線。
- **M3 History Rebuild Complete**
  - 全歷史新分區完成；cache/manifest 全量重建完成。
- **M4 Model & Validation Complete**
  - 重訓完成；backtest/parity/gate 以新語意全部通過。
- **M5 Runtime Cutover Complete**
  - serving/readiness/monitoring 切換完成，無 legacy key 依賴。

## 資源與執行護欄

以本機規格（24 vCPU、約 62 GiB RAM）設定保守護欄，優先避免 OOM：

- DuckDB `memory_limit` 設定固定上限（建議先 36GB）。
- `threads` 保守起步（建議先 6）。
- 固定 `temp_directory` 與足夠 spill 配額。
- bet/session 重型步驟採分桶或分批策略，避免單次全量峰值。
- 長任務保留可續跑檢查點（分區級）。

## 風險與緩解

- **全歷史回填耗時過長**
  - 緩解：分區批次化、固定資源上限、可續跑 checkpoint。
- **時區二次轉換污染**
  - 緩解：idempotent 檢查、轉換前後 schema 與樣本雙驗證。
- **新舊 key 混用造成觀測斷裂**
  - 緩解：同日切換 + 事前 dry-run 看板查核。
- **train/serve 語意漂移**
  - 緩解：強制重訓 + parity + deploy gate 作為硬門檻。

## 驗證與上線策略

- **資料驗證**
  - sentinel 檢查（包含日界線與已知 bet 範例）。
  - `__etl_insert_Dtm < event_time` 0 容忍 hard-fail。
- **模型驗證**
  - 重訓後跑完整 backtest / parity / gate，並重校門檻。
- **切換驗證**
  - runtime/readiness/prediction log 僅含 `*_gaming_day_event*`。
  - 舊分區與舊 cache 不可被新流程讀入。

## 治理與責任（High-level Ownership）

- Data Contract Owner：維護時間語意 registry 與 SSOT 一致性。
- Pipeline Owner：全歷史回填、分區鍵切換、cache/manifest 重建。
- ML Owner：重訓、parity、gate 重校。
- Serving Owner：runtime/readiness/refresh/監控切換與驗證。

## 文件邊界

- 本文件：Implementation plan（realization strategy）。
- `Data pipeline - SSOT.md`：定義做什麼與治理真相。
- Working plan：後續拆分為任務、依賴、執行順序與 DoD。
