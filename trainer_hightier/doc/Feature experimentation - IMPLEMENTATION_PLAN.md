# trainer_hightier - Feature Experimentation Implementation Plan

本文件屬於 **Implementation Plan 層**，對齊 `Data pipeline - SSOT.md` 中新增的特徵候選治理需求，定義「如何落地」候選生成、候選篩選、長窗低成本物化與訓練視窗策略實驗。  
本文件不展開 ticket 級任務拆分；任務拆解應在後續 working plan 文件承接。

## 0) 對齊範圍與非目標

- 對齊來源：`trainer_hightier/doc/Data pipeline - SSOT.md`
- 實作邊界：
  - 包含：候選生成、群組化實驗、篩選 gate、長窗快取治理、訓練視窗策略比較
  - 不包含：模型家族替換、線上 serving 架構改造、產品策略決策

## 1) 設計原則（Realization Principles）

- **Group-first**：feature group 為最小實驗單位，不做預設全欄位組合暴力搜尋。
- **PIT-first**：所有候選都必須先通過時間語義與可觀測性檢查，再進入模型評估。
- **Quality-first**：在進入 **Gate 1／ablation 訓練** 前，候選欄位預設通過 **FQG（L1 全量 + L2 候選）**；僅 **allowlist** 內欄位可進訓練；**BLOCK** 預設 **fail-fast**（見 Working Plan §1.5）。
- **Cost-aware**：每輪實驗都需同時比較效能與成本（runtime / peak RAM / cache 命中）。
- **Fair-compare**：訓練視窗策略比較時固定 eval 區間（val/test），僅改訓練資料策略。
- **Deterministic artifacts**：每次實驗產出可追溯 manifest，支援重跑與審計。

## 2) 實作目標（What to Realize）

### 2.1 候選生成治理

- 建立 `feature group registry`（可用 YAML/JSON/Python 結構化維護）：
  - group_id
  - entity_key
  - cadence
  - compute_pattern
  - dependency
  - lookback_days_or_window
  - anchor_rule（若為長窗快照）
  - owner / status / version
- 候選特徵命名採可組合規則，支援同族視窗 ladder（15m/30m/60m/.../180d）。

### 2.2 長窗低成本策略

- 視窗分級定義（按資料量與未來擴規風險）：
  - 短窗：`<= 1d`
  - 中窗：`> 1d 且 <= 30d`
  - 長窗：`> 30d`
- 中窗候選預設採 daily snapshot 物化。
- 長窗候選（例如 180d）預設採 monthly snapshot 物化。
- 以 as-of join 連回 bet-grain 樣本。
- 額外產出 freshness/staleness 欄位（例如 `days_since_snapshot_anchor`）供模型感知訊號新鮮度。
- 快取鍵需語意完整（來源指紋、lookback、anchor、程式版本、schema 版本、group signature）。

### 2.3 候選篩選流程

- **FQG（Feature Quality Gate，欄位級，預設在 Gate 0 之前或與 Gate 0 串接）**
  - **L1**：對**所有**擬評估之候選欄位必跑（輕量：缺失、常數、非法值、型別跨 split 一致性、粗分位與極端值、分類新類別占比、可配置的洩漏/禁止欄位規則等）；產出逐欄 **PASS / WARN / BLOCK**。
  - **L2**：僅對 **L1 為 PASS 或已核准之 WARN** 且**擬進入 Gate 1 訓練**之欄位執行（較重：PSI、跨時間切片穩定性等）；預設以 **月** 切片；計算資源預設 **每 split 抽樣 ≤200k 列**、**固定 seed**、**float32** 統計（與 Working Plan §1.5 一致）。
  - **決策**：**BLOCK** → 該欄不得進任何訓練，且管線預設 **fail-fast**；**WARN** → 不預設擋，但須 **顯式核准**（寫入 allowlist metadata，含核准人、理由、**可選過期日**，預設建議 **14 天**）方可進 Gate 1。
  - **產物**：`feature_quality_report.json`、`feature_allowlist.json`、`feature_blocklist.json`（路徑慣例由實作決定，契約欄位見 Working Plan §1.5）。
- 形成固定 **FQG + 三層** gate：
  - **FQG**：欄位級品質與穩定性（L1/L2）
  - Gate 0（資料/契約，**仍以 group 為單位**）：DQ、PIT、可計算性；其輸入特徵集合 **不得含 FQG BLOCK 欄位**
  - Gate 1（群組增量）：baseline vs baseline+group
  - Gate 2（群內去冗餘）：相關群聚 + 代表特徵保留
- 只允許通過 **FQG 與**各 gate 的候選進入下一層，避免大規模無效訓練。

### 2.4 訓練視窗策略

- 納入固定策略集合：
  - all history
  - rolling 365d / 180d / 90d / 60d / 30d
  - recency weighting（可配置半衰期）
- 規範比較方式：固定 val/test 時段，僅改 train policy。
- 訓練視窗策略只限制「訓練樣本列」時間範圍；不限制特徵可回看的歷史深度（前提：PIT 正確）。
- 需要解耦兩個邊界並強制落盤：
  - `feature_compute_range`：預設 `full_available_history`
  - `training_sample_range`：例如 `no earlier than 2025-01-01`
- `training_sample_range` 的預設語意採「no earlier than <date>」，避免誤用「no later than <date>」導致排除近期資料。

## 3) 實作工作流（Workstreams）

### Workstream A: Registry 與語意契約

- 建立 feature group registry schema。
- 建立 group signature 計算規格（欄位、語義、依賴、版本）。
- 對接現有 Feast feature service，確保 group 與 feature views 的映射可追溯。

### Workstream B: 長窗物化與快取

- 建立短/中/長窗分級調度策略（短窗事件級、中窗日快照、長窗月快照）。
- 將長窗群組改為 monthly snapshot 物化流程，將中窗群組改為 daily snapshot 流程。
- 建立 cache manifest 規格與命中/失效判定規則。
- 建立 dirty-date 擴張規則（來源變更如何影響後續 anchor 月份）。

### Workstream C: Screening 引擎

- 實作 **FQG**（L1 全量、L2 候選、抽樣與 fail-fast／WARN 核准語意）及三份契約 JSON 輸出。
- 將 **feature_allowlist** 與 Gate 0/1 runner **強制串接**（訓練欄位 ⊆ allowlist；BLOCK 不可 silent drop）。
- 實作 Gate 0/1/2 標準報表欄位。
- 建立 group-level 對照實驗 runner（baseline 與增量比較）。
- 建立群內去冗餘流程（相關群聚、代表特徵挑選、淘汰紀錄）。
- **Baseline 盤點**：對現行常用特徵全集至少跑一次 **FQG L1**，建立初始品質基線（可併入 Wave 0/1）。

### Workstream D: Window Strategy Runner

- 建立固定 eval 區間的比較框架。
- 支援 hard window 與 recency weighting 並行比較。
- 支援 `training_sample_range` 配置（預設 no earlier than date）並固定 `feature_compute_range=full_available_history`。
- 產出統一對照報表（效能 + 成本 + 決策建議）。

### Workstream E: 治理與報表

- 建立每輪固定輸出格式：
  - **FQG 產物路徑**（`feature_quality_report` / `feature_allowlist` / `feature_blocklist`）與 `fqg_status`
  - 實驗配置（feature list version、group set、window policy）
  - 指標（AP、Recall@Precision floor、alerts/hour）
  - 成本（runtime、peak RAM、cache hit ratio）
  - 決策（go/no-go + reason codes）
- 納入營運承接護欄：`val alerts/hour` 平均上限為 **120**（約 2 alerts/min），
  超限時必須在報表標示 `capacity_alarm=true` 並列入升級阻擋原因。
- 串接 run report 與 artifacts retention。

## 4) 里程碑（Milestones）

- **M1 - Registry Ready**
  - Feature group registry 與 signature 可用，並可對應現有特徵清單。
- **M2 - Long Window Efficient Path**
  - 短/中/長窗分級策略上線；中窗具 daily snapshot，長窗具 monthly snapshot + cache + as-of join 能力。
- **M3 - Screening Pipeline Online**
  - **FQG** 可重現並輸出 `feature_quality_report.json` / `feature_allowlist.json` / `feature_blocklist.json`；Gate 0/1/2 流程可重現，並可輸出標準篩選報表；Gate 1 僅能使用 allowlist 內欄位。
- **M4 - Window Strategy Benchmark**
  - all history vs rolling vs weighting 可在固定 eval 區間公平比較。
- **M5 - Governance Closure**
  - 每輪實驗具完整輸出與 go/no-go 記錄，可支援審計與回放。

## 5) 交付物（Deliverables）

- D1: `feature group registry` 規格與首版內容
- D2: 長窗 snapshot + cache manifest 規格
- D2b: **FQG** 契約與 JSON schema（`feature_quality_report` / `feature_allowlist` / `feature_blocklist`）及 `config.py`（或單一 YAML）內 **FQG v0 閾值**集中設定
- D3: Gate 0/1/2 篩選報表模板
- D4: 訓練視窗策略比較報表模板
- D5: 每輪實驗決策紀錄模板（含 reason codes）

## 6) 風險與緩解（Risks & Mitigations）

- 風險：Feature view / group 邊界設計不當，導致維護成本升高。
  - 緩解：先以 group registry 驗證，再映射到 Feast view；避免先拆大量 view。
- 風險：長窗快取誤命中（語意變更未失效）。
  - 緩解：cache key 納入語意版本與 schema 版本，且每次 run 落盤 manifest。
- 風險：只看單次 split 得出錯誤結論。
  - 緩解：固定 eval 區間 + 多切點對照，並記錄統計穩健性指標。
- 風險：FQG 抽樣導致統計與全量略有偏差。
  - 緩解：固定 seed、樣本量與版本寫入 report；重大決策可選全量覆核路徑並記錄。
- 風險：筆電資源不足造成實驗排程阻塞。
  - 緩解：採 wave 節奏（S/L/Mix），先跑 group-level 快篩，再做精篩。

## 7) 驗證與升級準則（Validation & Promotion）

- 候選群組升級條件（**v0**，與 `Feature experimentation - WORKING_PLAN.md` §1.4 / §1.5 / §4 對齊）：
  - 通過 **FQG**：擬訓練欄位均為 **PASS** 或已核准之 **WARN**；無未處理之 **BLOCK**（詳 §1.5）
  - 通過 Gate 0（資料品質、非法值、常數率、PIT、單 group 物化時間上限）
  - Gate 1：相對同一 baseline 之 `ΔAP ≥ +0.003` 且 `ΔR@Pmin > 0`（Recall 必須嚴格上升；單次固定 val/test 口徑與 Gate 1 報表定義一致）
  - Gate 2：完成群內去冗餘並落盤保留/淘汰理由
  - 容量護欄：`val alerts/hour ≤ 120`；若超限，必須告警並標記不通過升級（除非另有核准豁免與紀錄）
  - 單 round **runtime ≤ 60 分鐘**（或已核准豁免並記錄）；決策紀錄完整（含可重現參數）
- 訓練視窗策略升級條件（**v0**）：
  - **Baseline**：benchmark 之標準對照為 **all history** 訓練政策（與候選策略共用同一 `feature_compute_range`、同一 eval fingerprint）。
  - **穩健性切片**：在固定 **val** 日期區間內依 `gaming_day` 切成 **K=4** 個連續等天數子區間；若 val 太短則 **K = min(4, 可切之最大整數)** 且 **K≥2**，並於報表註明；**K<2** 時不得依 P25 規則升級（見 Working plan §1.4）。
  - **指標計算方式（v0）**：候選策略與 baseline **各訓練單一模型**（同一搜尋 budget／固定超參，擇一並落盤）；對 val **全體**推論後，再依 K 子區間分別計算 `AP` 與 `R@Pmin`（`min_precision` 與 `config.py` 一致），於每段對 baseline 取差得到 K 組 `ΔAP`、`ΔR@Pmin`（**不要求 K 次重訓**）。
  - **升級需同時滿足**：`median(ΔAP) ≥ +0.003`、`P25(ΔAP) ≥ -0.003`、`median(ΔR@Pmin) > 0`、`P25(ΔR@Pmin) > 0`（與 `Feature experimentation - WORKING_PLAN.md` §1.4 一致）。
  - **成本**：上述「單次訓練 + 全 val 推論」之 wall clock **≤ 60 分鐘**；候選策略之 runtime 不得高於 baseline 同口徑 runtime **+600 秒**（v0 相對容忍）。
  - **回退（v0）**：新標準策略相對舊標準策略，於同一 K 切片上若 `P25(Δ′AP) < -0.003` 或 `P25(Δ′R@Pmin) < 0`（新減舊），則應回退；細節見 Working plan §1.4。
  - 維持 `feature_compute_range=full_available_history` 且 `training_sample_range` 設定可重現；可回退（保留前一版策略與報表證據）。

## 8) 與其他文件邊界

- SSOT：定義「要做什麼與治理準則」。
- 本文件：定義「如何在架構與工作流層落地」。
- Working plan（後續）：定義「任務拆解、順序、依賴與 DoD」；**FQG v0 門檻**見該檔 **§1.5**。
