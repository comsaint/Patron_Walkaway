# trainer_hightier - Feature Experimentation Implementation Plan

本文件屬於 **Implementation Plan 層**，對齊 `Data pipeline - SSOT.md` 中新增的特徵候選治理需求，定義「如何落地」候選生成、候選篩選、長窗低成本物化與訓練視窗策略實驗。  
本文件不展開 ticket 級任務拆分；任務拆解應在後續 working plan 文件承接。

> **狀態（2026-06-10）— `t_casino_txn` / txn_lite**  
> 因上游 **data source incident**，`t_casino_txn` 之 **L0 接入與清洗**已改由獨立 workstream 處理：`t_casino_txn Source Integration - IMPLEMENTATION_PLAN.md`（對齊 SSOT §5.2）。  
> 本文件 §2.5 / Workstream C2 仍描述 **L1 feature experimentation** 架構，但 **txn_lite 實作與 Gate 1 promote 暫 defer**；quarantine 期間 cleaned artifact 為 **`not_model_eligible`**，歷史 ablation 結果**不得**作 registry 或 model 決策依據。incident 關閉且 L0 可信後，再恢復 Working Plan §1.7。

## 0) 對齊範圍與非目標

- 對齊來源：`trainer_hightier/doc/ssot/Data pipeline - SSOT.md`
- 實作邊界：
  - 包含：候選生成、群組化實驗、篩選 gate、長窗快取治理、訓練視窗策略比較、外部事件來源之實驗接入框架
  - 不包含：模型家族替換、線上 serving 架構改造、產品策略決策、單一來源表的欄位級清洗細節

### 0.1 決策紀錄（Decision Log）

| ID | 決策 | 理由 / 邊界 |
|----|------|-------------|
| D-001 | 沿用既有 `feature_experiment` pipeline，不另建平行 ablation pipeline。 | FQG、Gate 1、ablation、isolated artifacts 已集中在此路徑；新增資料源應擴充現有框架。 |
| D-002 | 外部事件來源第一版採 **experiment-only** 接入，不直接改 production trainer / serving。 | 避免未經 Gate 驗證的資料源影響 baseline 或線上供應契約。 |
| D-003 | Registry v0 仍只負責 **selection / governance**，不承載 SQL 或來源表清洗邏輯。 | 與 `Feature Candidate Registry - IMPLEMENTATION_PLAN.md` 對齊；source-specific 清洗由來源契約或 materializer 實作負責。 |
| D-004 | 第一輪仍採 **group-first** 評估，不引入任意 feature-combo 搜尋。 | 符合本文件設計原則，並控制運算成本與結論可解釋性。 |
| D-005 | Implementation Plan 只定義外部事件來源如何進入 feature experimentation；來源表清洗細節以來源契約 / findings / schema dictionary 為準。 | 避免本文件變成 data dictionary；例如 `t_casino_txn` 清洗依 `doc/FINDINGS.md` [FND-19] 與 `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` §5。 |

## 1) 設計原則（Realization Principles）

- **Group-first**：feature group 為最小實驗單位，不做預設全欄位組合暴力搜尋。
- **PIT-first**：所有候選都必須先通過時間語義與可觀測性檢查，再進入模型評估。
- **Source-contract-first**：外部事件來源不得由實驗 runner 直接讀 raw table 進訓練；必須先經來源契約定義的清洗、去重、PIT event-time 與有效事件規則，並產出可追溯的 cleaned/materialized artifact。
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

### 2.5 外部事件來源實驗接入

- 支援將非既有 Feast / `fe_derived` 路徑的事件表，以 **experiment-only source** 方式接入 feature experimentation。
- 外部事件來源必須先通過 source-specific preprocessing contract，至少明確定義：
  - logical key 與去重策略
  - deletion / cancellation / invalid event 處理
  - PIT event timestamp 與資料可見性語意
  - entity key 對齊方式（例如 `player_id` / `canonical_id` / 其他 mapping）
  - 可進入模型的 cleaned output schema 與 source contract version
- `feature_experiment` runner 僅消費已清洗、PIT-safe 的 materialized feature parquet，不直接把 raw source table join 入訓練集。
- Registry v0 對外部事件特徵只宣告 `feature_id`、`group_id`、`source`、`time_horizon`、`max_lookback`、`status`、`enabled_for` 與治理註記；不重複來源表清洗規則或 SQL。
- `t_casino_txn` 為第一個目標外部事件來源案例。**L0 清洗**（source-grain、全 type、quarantine）見 `t_casino_txn Source Integration - IMPLEMENTATION_PLAN.md` 與 SSOT §5.2；**L1 特徵清洗**（例如 BUYIN/CASHOUT only）仍依：
  - `doc/FINDINGS.md` **[FND-19]**
  - `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` **§5. t_casino_txn**
  - quarantine 解除前，L1 materializer 不得 consume 未標記可信的 cleaned artifact

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

### Workstream C2: 外部事件來源 Materialization 接入

- 建立 source-specific materializer 插槽：在 Step 3 / FE enrichment 與 Step 4 split 之前，產出可 join 至訓練 grain 的 cleaned feature parquet。
- Materializer 輸入必須是 raw source + source contract；輸出必須包含 join key、PIT anchor、feature columns、source metadata 與 schema/version fingerprint。
- `run_pipeline.py` 需能在 isolated `run_dir` 下記錄：
  - raw input path / fingerprint
  - source contract reference / version
  - cleaning policy id
  - materializer code version
  - output parquet path / row count / feature columns
- 外部事件來源的 candidate group 仍走既有 FQG、Gate 0、Gate 1、Gate 2；不得跳過 allowlist / blocklist 約束。
- 第一版只要求 group-first add-one / LOO 支援；任意 named arms 或 feature-combo runner 屬後續擴充，不列入本 implementation plan 的 v0 目標。

### Workstream D: Window Strategy Runner

- 建立固定 eval 區間的比較框架。
- 支援 hard window 與 recency weighting 並行比較。
- 支援 `training_sample_range` 配置（預設 no earlier than date）並固定 `feature_compute_range=full_available_history`。
- 產出統一對照報表（效能 + 成本 + 決策建議）。

### Workstream E: 治理與報表

- 建立每輪固定輸出格式：
  - **FQG 產物路徑**（`feature_quality_report` / `feature_allowlist` / `feature_blocklist`）與 `fqg_status`
  - 實驗配置（feature list version、group set、window policy）
  - 外部事件來源 metadata（若本輪使用）：source contract reference、cleaning policy id、raw input fingerprint、materialized feature artifact path
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
- **M3b - External Event Source Experiment Path**
  - 至少一個外部事件來源可透過 source-specific materializer 產出 PIT-safe feature parquet，並在 isolated feature experiment run 中完成 FQG + Gate 1 評估。
- **M4 - Window Strategy Benchmark**
  - all history vs rolling vs weighting 可在固定 eval 區間公平比較。
- **M5 - Governance Closure**
  - 每輪實驗具完整輸出與 go/no-go 記錄，可支援審計與回放。

## 5) 交付物（Deliverables）

- D1: `feature group registry` 規格與首版內容
- D2: 長窗 snapshot + cache manifest 規格
- D2b: **FQG** 契約與 JSON schema（`feature_quality_report` / `feature_allowlist` / `feature_blocklist`）及 `config.py`（或單一 YAML）內 **FQG v0 閾值**集中設定
- D2c: 外部事件來源 materialization 接入契約（cleaned artifact schema、source metadata、fingerprint 與 report 欄位）
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
- 風險：外部事件來源清洗規則漂移，導致同一 feature group 在不同 run 中語意不一致。
  - 緩解：每次 run 落盤 source contract reference、cleaning policy id、raw input fingerprint 與 materializer code version；source-specific 細節不得只存在程式註解。
- 風險：實驗 runner 直接讀 raw source table，繞過去重、取消/刪除處理或 PIT event-time 規則。
  - 緩解：外部來源只能透過 materialized cleaned artifact 進入 enrichment；Gate 0 檢查須驗 source metadata 是否存在。
- 風險：筆電資源不足造成實驗排程阻塞。
  - 緩解：採 wave 節奏（S/L/Mix），先跑 group-level 快篩，再做精篩。

## 7) 驗證與升級準則（Validation & Promotion）

- 候選群組升級條件（**v0**，與 `Feature experimentation - WORKING_PLAN.md` §1.4 / §1.5 / §4 對齊）：
  - 通過 **FQG**：擬訓練欄位均為 **PASS** 或已核准之 **WARN**；無未處理之 **BLOCK**（詳 §1.5）
  - 通過 Gate 0（資料品質、非法值、常數率、PIT、單 group 物化時間上限）
  - 若候選來自外部事件來源：必須有 source contract reference、cleaning policy id、raw input fingerprint、materialized artifact fingerprint，且 Gate 0 能確認訓練只使用 cleaned/PIT-safe output。
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
- Source-specific findings / schema dictionary：定義單一 raw source 的資料發現、欄位字典與清洗注意事項；例如 `t_casino_txn` 依 `doc/FINDINGS.md` **[FND-19]** 與 `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` **§5**。本文件不得重複其欄位級清洗規則。
