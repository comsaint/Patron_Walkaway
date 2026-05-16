# trainer_hightier - Feature Candidate Registry Implementation Plan

本文件屬於 **Implementation Plan 層**，定義如何在 `trainer_hightier` 導入一份可治理的候選特徵 YAML 台帳，並讓 feature experiment pipeline 以該台帳作為「啟用特徵」來源。  
本文件聚焦實作策略與里程碑，不展開 ticket 級工作拆解（該部分應由後續 Working Plan 承接）。

## 0) 範圍與邊界

- **目標範圍**
  - 在 `trainer_hightier/contracts/` 新增候選特徵台帳 YAML（下稱 registry YAML）
  - pipeline 讀取 registry YAML，決定 baseline/candidate/ablation 的啟用欄位
  - 保留「曾嘗試但停用」欄位的人工備註（原因、關聯 experiment）
  - 建立 registry YAML 與現有程式（`feature_registry.py`、`run_pipeline.py`、FQG 輸出）之過渡治理
- **非目標**
  - 不改寫 `materialize_fe_derived.py` 的 SQL 物化策略（先不做 SQL 動態生成）
  - 不與 `trainer/feature_spec/feature_candidates.yaml` 做雙向同步
  - 不一次重構所有歷史 artifact schema

## 1) 實作目標（What to Realize）

### 1.1 單一可審計台帳（針對 hightier）

- 於 `trainer_hightier/contracts/` 落地 registry YAML，作為 hightier 實驗候選欄位治理入口
- 同時覆蓋三類欄位：
  - baseline 欄位（既有模型欄位）
  - `fe__*` 實驗欄位
  - 已停用欄位（保留歷史與原因）

### 1.2 機器可判斷狀態

- 每個特徵需具備可程式判斷欄位，而非僅人工文字備註
- 至少支援：
  - `status`: `active` / `disabled` / `experimental`
  - `enabled_for`: `baseline` / `candidate` / `ablation`（可多值）
  - `drop_reason_code`: 可空；停用時必填

### 1.3 Pipeline 讀檔決策

- `run_pipeline` 不再依賴硬編碼實驗欄位清單，而是由 registry YAML 載入 active features
- `feature_registry.py` 轉為過渡層（shim）或逐步退場，避免雙重維護

## 2) 方案設計（How to Realize）

### 2.1 邏輯架構

- **配置層（contracts）**：registry YAML（人工維護 + 程式讀取）
- **解析層（feature_experiment）**：registry loader（schema 驗證、狀態過濾、群組映射）
- **執行層（pipeline）**：依 `enabled_for` + `status` 組出 baseline/candidate/ablation 欄位
- **治理層（report）**：run report 回寫使用版本、啟用欄位、停用欄位摘要

### 2.2 registry YAML 建議欄位（v0）

- `registry_version`
- `updated_at`
- `features[]`（每欄位一筆）
  - `feature_id`
  - `group_id`
  - `source`（例如 `baseline_model`, `fe_derived`, `feast_trial_1h`, `feast_slow_180d`）
  - `status`
  - `enabled_for`
  - `drop_reason_code`
  - `first_seen_experiment`
  - `last_updated_experiment`
  - `note`

> 設計原則：先維持扁平與可讀，避免 v0 就導入過度巢狀與多層繼承。

### 2.3 與既有模組整合策略

- `feature_registry.py`
  - v0 先改為由 YAML 載入後輸出既有常數介面（降低對下游衝擊）
  - 後續再逐步移除靜態常數
- `run_pipeline.py`
  - 新增「registry 版本與檔案路徑」回寫到 `feature_experiment_report.json`
  - 將 FQG `warn_pending` 與 registry `status/notes` 串接（人工決策仍保留）
- `ablation.py`
  - group 集合由 YAML 的 `group_id` + `enabled_for` 決定

### 2.4 長期方向：宣告式特徵生成（v1+）

- 長期可將 registry YAML 擴展為「可宣告生成邏輯」，但建議分階段：
  - **v0**：只驅動欄位啟用/停用（selection）
  - **v1+**：才引入生成規則（generation spec）
- 生成規則建議採「結構化欄位」而非自由 SQL 字串，避免可維護性與測試困難：
  - `compute_family`（例如 `window_agg`, `ratio`, `zscore`, `snapshot_join`）
  - `source_table` / `entity_key` / `order_by`
  - `window`（`15m` / `1d` / `30d` / `180d_monthly_anchor`）
  - `expression_template_id`（對應程式內已核可模板）
- 原則：registry YAML **可宣告**「要算什麼」，實際 SQL 仍由模板引擎或既有模組生成，避免把大量 SQL 直接搬進 contracts。

### 2.5 與 `trainer_hightier/contracts` 現有文件邊界（避免衝突/重疊）

- 既有文件職責：
  - `time_semantics_and_feast_mapping.md`：時間語意與 PIT/Feast 對照總契約
  - `trial_bet_behavior_1h_features.yaml`：trial 1h 特徵契約（語意 + 實作入口 + Feast 映射）
  - `slow_patron_180d_monthly_features.yaml`：slow patron 180d 月快照契約
- 新增 registry YAML（本計畫）職責：
  - 管理「實驗候選是否啟用」與「歷史決策註記」
  - 不重複定義上述文件已存在之時間語意與 Feast mapping 細節
- **去重規則（必遵守）**：
  - 若特徵屬於既有契約（如 `bet__*`, `patron__*`），registry YAML 只保留引用鍵（如 `feature_id`, `source_contract_id`），不複寫語意段落
  - 若特徵為 `fe__*`（目前無獨立 contracts YAML），可在 registry YAML 放 selection 資訊；生成語意仍以實作模組與後續專屬契約文件為準
  - 同一 `feature_id` 只允許一個「語意主檔（semantic owner）」：避免多檔同時成為真相來源

## 3) 主要工作流（Workstreams）

### Workstream A: Registry 規格與契約

- 定義 YAML schema（欄位、型別、必要條件、枚舉值）
- 建立最小驗證器（讀檔、必填欄位、重複 `feature_id` 檢查）
- 建立與 `feature_experiment_report.json` 對齊的 metadata 欄位

### Workstream B: Pipeline 讀取與路由

- 在 `feature_experiment` 新增 registry loader
- 替換 baseline/candidate/ablation 欄位來源為 YAML 過濾結果
- 在輸出報表中落盤 `registry_version` 與 `active_feature_count`

### Workstream C: 過渡與相容

- 保留舊介面（常數名稱）一段時間，避免一次性大改呼叫端
- 補最小測試：載入、狀態過濾、group 組欄、空值與未知狀態防呆
- 確保舊實驗可重現：同一 YAML + seed + config 應得到同一欄位集合

### Workstream D: 治理與操作

- 定義人工維護規範：
  - 停用必填 `drop_reason_code`
  - 更新需填 `last_updated_experiment`
  - `note` 建議記錄 FQG/Gate1 依據
- 形成每輪實驗後更新清單（可手動）

## 4) 里程碑（Milestones）

- **M1 - Schema Ready**
  - registry YAML 草案與驗證規則完成，並可覆蓋現有 baseline + `fe__*`
- **M2 - Read Path Online**
  - pipeline 已由 YAML 讀取 active features，且報表可落盤 registry metadata
- **M3 - Backward Compatibility Verified**
  - 舊有測試與既有 run profile 正常，欄位集合可重現
- **M4 - Governance Operational**
  - 實驗後可依固定流程更新 `status/drop_reason_code/note`

## 5) 交付物（Deliverables）

- D1: `trainer_hightier/contracts/` 下 registry YAML（v0）
- D2: registry loader 與 schema 驗證邏輯
- D3: `run_pipeline` 整合與報表 metadata 回寫
- D4: 回歸測試與最小操作指引（可放在既有 RUNBOOK 章節）

## 6) 風險與緩解（Risks & Mitigations）

- 風險：YAML、Python 常數、SQL 三處定義漂移  
  - 緩解：v0 先做到「YAML 驅動 candidate 選欄」，避免多入口可寫
- 風險：人工註記品質不一，造成審計失真  
  - 緩解：停用時強制 `drop_reason_code`，`note` 以模板約束
- 風險：誤停用導致候選欄位過少、效能下降  
  - 緩解：每次 run 輸出 active/disabled 摘要，並保留回退機制
- 風險：一次切換造成既有程式大面積變更  
  - 緩解：保留 shim 介面，分階段移除舊常數

## 7) 驗證與升級準則（Validation & Promotion）

- 功能驗證
  - YAML 欄位集合與 pipeline 實際訓練欄位一致
  - `enabled_for` 可正確影響 baseline/candidate/ablation
- 回歸驗證
  - 既有 `feature_experiment` 測試通過
  - FQG 報表與 gate 報表未出現 schema 破壞
- 升級準則（v0）
  - 新增/停用欄位須附實驗依據（run id + reason code）
  - 變更後至少完成一次 end-to-end run 並留存報告

## 8) 文件邊界

- 本文件：定義「YAML 台帳如何落地到實驗管線」
- SSOT：定義為何需要特徵治理與審計
- Working Plan：承接 ticket 分解、順序、DoD 與排程
