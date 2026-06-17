# trainer_hightier - Feature Experimentation Implementation Plan

本文件屬於 **Implementation Plan 層**，對齊 `Data pipeline - SSOT.md` 中新增的特徵候選治理需求，定義「如何落地」候選生成、候選篩選、長窗低成本物化與訓練視窗策略實驗。  
本文件不展開 ticket 級任務拆分；任務拆解應在後續 working plan 文件承接。

> **狀態（2026-06-10）— `t_casino_txn` / txn_lite**  
> 因上游 **data source incident**，`t_casino_txn` 之 **L0 接入與清洗**已改由獨立 workstream 處理：`t_casino_txn Source Integration - IMPLEMENTATION_PLAN.md`（對齊 SSOT §5.2）。  
> 本文件 §2.5 / Workstream C2 仍描述 **L1 feature experimentation** 架構，但 **txn_lite 實作與 Gate 1 promote 暫 defer**；quarantine 期間 cleaned artifact 為 **`not_model_eligible`**，歷史 ablation 結果**不得**作 registry 或 model 決策依據。incident 關閉且 L0 可信後，再恢復 Working Plan §1.7。

## 0) 對齊範圍與非目標

- 對齊來源：`trainer_hightier/doc/ssot/Data pipeline - SSOT.md`
- 實作邊界：
  - 包含：候選生成、群組化實驗、篩選 gate、**Time-CV feature selection（pruning）**、長窗快取治理、訓練視窗策略比較、外部事件來源之實驗接入框架
  - 不包含：模型家族替換、線上 serving 架構改造、產品策略決策、單一來源表的欄位級清洗細節

### 0.1 決策紀錄（Decision Log）

| ID | 決策 | 理由 / 邊界 |
|----|------|-------------|
| D-001 | 沿用既有 `feature_experiment` pipeline，不另建平行 ablation pipeline。 | FQG、Gate 1、ablation、isolated artifacts 已集中在此路徑；新增資料源應擴充現有框架。 |
| D-002 | 外部事件來源第一版採 **experiment-only** 接入，不直接改 production trainer / serving。 | 避免未經 Gate 驗證的資料源影響 baseline 或線上供應契約。 |
| D-003 | Registry v0 仍只負責 **selection / governance**，不承載 SQL 或來源表清洗邏輯。 | 與 `Feature Candidate Registry - IMPLEMENTATION_PLAN.md` 對齊；source-specific 清洗由來源契約或 materializer 實作負責。 |
| D-004 | 第一輪仍採 **group-first** 評估，不引入任意 feature-combo 搜尋。 | 符合本文件設計原則，並控制運算成本與結論可解釋性。 |
| D-005 | Implementation Plan 只定義外部事件來源如何進入 feature experimentation；來源表清洗細節以來源契約 / findings / schema dictionary 為準。 | 避免本文件變成 data dictionary；例如 `t_casino_txn` 清洗依 `doc/FINDINGS.md` [FND-19] 與 `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` §5。 |
| D-006 | Time-CV 與 single-split Gate 1 **並行**，不取代。 | Single-split Gate 1 作為 quick screen 降低成本；Time-CV 用於邊際特徵的穩健性確認與 pruning 決策。 |
| D-007 | Time-CV 主決策指標為 **P@1hr**（fixed alert rate 下之 precision），非 AP。 | Production 目標是在給定 recall（alert rate）下最大化 precision；AP 為 ranking quality proxy，不直接對應 operational KPI。 |
| D-008 | Time-CV 預設 **expanding window**，非 rolling window。 | Expanding window 資料效率最高，符合「用所有可用歷史訓練」的 production 語意。 |
| D-009 | Time-CV **v0 決策閾值**採文件預設值先行試跑；noise level 未知，**首輪不得**據此直接 registry promote，須以首輪 report 校準。 | 避免在無 empirical baseline 下過度承諾；閾值集中於 `config.py` 之 `FEATURE_SELECTION_TIME_CV_*`。 |
| D-010 | Time-CV v0 **固定超參**（所有 fold 同一組 LightGBM 超參）；不做 per-fold Optuna。 | 避免超參 leak 污染 feature contribution 估計；與 production 一致性優先。 |
| D-011 | Time-CV v0 **首輪 pruning 起點**為 baseline **LOO**（非 add-one promote）。 | 當前目標為 prune unuseful baseline features；add-one 新 group 留待 LOO wave 後。 |
| D-012 | Time-CV v0 **coding prototype** 先 minimal（**K=3**、**2–3** feature/group arms）驗證 fold／metrics／report，再擴至 **K=5** 全量 LOO。 | 控制首輪成本與學習速度；full wave 仍對齊 Working Plan Wave 4 exit。 |
| D-013 | `config.py` 常數命名前綴 **`FEATURE_SELECTION_TIME_CV_*`**；未來若訓練政策亦採 Time-CV，另用 **`TRAINING_TIME_CV_*`**（本輪不實作）。 | 區分 feature selection 與 training window 兩種 Time-CV 用途，避免 config 語意混淆。 |

## 1) 設計原則（Realization Principles）

- **Group-first**：feature group 為最小實驗單位，不做預設全欄位組合暴力搜尋。
- **PIT-first**：所有候選都必須先通過時間語義與可觀測性檢查，再進入模型評估。
- **Source-contract-first**：外部事件來源不得由實驗 runner 直接讀 raw table 進訓練；必須先經來源契約定義的清洗、去重、PIT event-time 與有效事件規則，並產出可追溯的 cleaned/materialized artifact。
- **Quality-first**：在進入 **Gate 1／ablation 訓練** 前，候選欄位預設通過 **FQG（L1 全量 + L2 候選）**；僅 **allowlist** 內欄位可進訓練；**BLOCK** 預設 **fail-fast**（見 Working Plan §1.5）。
- **Cost-aware**：每輪實驗都需同時比較效能與成本（runtime / peak RAM / cache 命中）。
- **Fair-compare**：訓練視窗策略比較時固定 eval 區間（val/test），僅改訓練資料策略。
- **Deterministic artifacts**：每次實驗產出可追溯 manifest，支援重跑與審計。
- **Operational-precision-first**：特徵升級／淘汰以 fixed alert rate 下之 precision（例如 **P@1hr**）為主；AP 與 Recall@Pmin 為輔助 sanity check。

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

### 2.6 Time-CV Feature Selection

- 引入 **時間交叉驗證（Time-CV）** 作為 Gate 1 的穩健性增強，用於評估既有 baseline 特徵之貢獻與 pruning（remove unuseful features）。
- **核心設計**：
  - **Expanding window**：train 自左向右延伸；val 為固定長度、non-overlapping 的連續時間窗口。
  - **K folds**：預設 **K=5**（可配 3–8）；成本與穩定性 trade-off 由 runner 配置集中管理（見 D8）。
  - **Val 窗口長度**：預設 **30 天**（`gaming_day_event`），對齊既有 test split 習慣，使 operational 指標可比較。
  - **最小 train 長度**：預設 **90 天**；不足時降低 K 並於報表註明。
- **指標對齊（operational objective）**：
  - **主指標**：`P@1hr` — 在 val 上依各 arm **各自 score 分布** 找 threshold，使 alert rate = **1 alert/hour**，再比較 precision（percentage points, pp）。
  - **輔助指標**：`P@2hr`（capacity 參考）、`AP`（ranking quality）、`Recall@Pmin`（`min_precision` 與 `config.py` 一致）。
  - **Delta 定義**：`ΔP@1hr_k = P@1hr(arm, val_k) − P@1hr(baseline, val_k)`；跨 fold 聚合 mean / std / cv_ratio。
- **與 single-split Gate 1 的分工**：
  - **Single-split Gate 1**：quick screen（低成本）。
  - **Time-CV**：邊際特徵（single-split `ΔP@1hr ∈ [-0.5pp, +1.0pp]`）或 pruning 候選之深入評估；亦可用於 baseline 全量 LOO pruning。
- **成本控制**：
  - **Group-first**：先 group-level add-one Time-CV；通過後才做 group 內 leave-one-out。
  - **Early stop**：前 3 fold 已一致 `ΔP@1hr < 0` → 跳過剩餘 fold（STRONG DROP）。
  - **Shared baseline**：同一 fold 的 baseline fit 可被所有 arm 共享。
  - **固定超參（v0，D-010）**：所有 fold 使用**同一組** LightGBM 超參；不做 per-fold Optuna。
- **v0 實作鎖定（2026-06-17，對齊 D-009–D-013）**：
  - **Pruning 起點**：baseline **leave-one-out**（非 add-one promote）。
  - **Coding prototype**：先 **K=3**、**2–3** arms 驗證 pipeline；通過後擴至 **K=5** 全量 LOO。
  - **輸入 parquet**：`training_set_fe_enriched.parquet`（或等價 enriched training parquet）。
  - **決策閾值**：採下方預設試跑；首輪 report 用於 noise 校準，**不得**在未校準前直接 registry promote。
- **`config.py` 常數（前綴 `FEATURE_SELECTION_TIME_CV_*`；未來 training Time-CV 另用 `TRAINING_TIME_CV_*`）**：

| 常數 | v0 預設 | 說明 |
|------|---------|------|
| `FEATURE_SELECTION_TIME_CV_N_FOLDS` | `5`（prototype 可覆寫為 `3`） | Expanding-window fold 數 |
| `FEATURE_SELECTION_TIME_CV_VAL_WINDOW_DAYS` | `30` | Val 窗口（`gaming_day_event`） |
| `FEATURE_SELECTION_TIME_CV_MIN_TRAIN_DAYS` | `90` | 最小 train 長度 |
| `FEATURE_SELECTION_TIME_CV_EARLY_STOP_FOLDS` | `3` | 前 N fold 一致 `ΔP@1hr < 0` → STRONG DROP |
| `FEATURE_SELECTION_TIME_CV_MEAN_DELTA_P1HR_PP` | `1.0` | KEEP 門檻（pp） |
| `FEATURE_SELECTION_TIME_CV_MAX_CV_RATIO` | `0.5` | `std / \|mean\|` 上限 |
| `FEATURE_SELECTION_TIME_CV_DROP_THRESHOLD_PP` | `-0.5` | DROP 門檻（pp） |
| `FEATURE_SELECTION_TIME_CV_MARGINAL_LOW_PP` | `-0.5` | MARGINAL 下界（pp） |
| `FEATURE_SELECTION_TIME_CV_WALL_TIME_LIMIT_SEC` | `1200` | 單 group full run（K=5）≤ 20 min |

- **輸入**：既有 enriched training parquet（含 baseline + candidate 欄位）；**不需**重跑特徵物化。
- **輸出**：`time_cv_ablation_report.json`（per-group / per-feature：mean/std/cv_ratio、per-fold detail、decision code）。
- **程式落點**：`trainer_hightier/feature_experiment/time_cv/`（fold 定義、runner、report）；由既有 `feature_experiment` runner 以 optional mode 呼叫，不另建平行 pipeline。

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
  - 指標（**P@1hr**、P@2hr、AP、Recall@Precision floor、alerts/hour）
  - 成本（runtime、peak RAM、cache hit ratio）
  - 決策（go/no-go + reason codes）
- 納入營運承接護欄：`val alerts/hour` 平均上限為 **120**（約 2 alerts/min），
  超限時必須在報表標示 `capacity_alarm=true` 並列入升級阻擋原因。
- 串接 run report 與 artifacts retention。

### Workstream F: Time-CV Feature Selection

- 實作 expanding-window **fold 生成器**（基於 `gaming_day_event` 排序；輸出 `TimeFold` manifest）。
- 實作 **Time-CV ablation runner**（`feature_experiment/time_cv/runner.py`）：
  - 自 enriched training parquet 依 fold 切 train / val subset（不重物化特徵）。
  - 每 fold：fit shared baseline；fit baseline + arm（group add-one 或 feature LOO）。
  - 每 fold 計算 `P@1hr`、`P@2hr`、`AP`、`Recall@Pmin` 及對 baseline 之 delta。
- 實作 **cross-fold 聚合與決策框架**（KEEP / REVIEW / MARGINAL / DROP / STRONG DROP；閾值見 §7）。
- 實作 **early stop**（前 3 fold 一致負向 → abort 該 arm）。
- 實作 **`time_cv_ablation_report.json`** schema 與 run manifest 串接。
- 與 single-split Gate 1 **路由整合**：依 single-split 結果決定是否進入 Time-CV（見 §7 Time-CV 升級條件）。

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
- **M6 - Time-CV Feature Selection Online**
  - Expanding-window fold 生成器可用；Time-CV ablation runner 可重現；
    決策框架（KEEP / REVIEW / MARGINAL / DROP / STRONG DROP）可輸出標準報表；
    至少完成一輪 group-level Time-CV pruning 並記錄 go/no-go。

## 5) 交付物（Deliverables）

- D1: `feature group registry` 規格與首版內容
- D2: 長窗 snapshot + cache manifest 規格
- D2b: **FQG** 契約與 JSON schema（`feature_quality_report` / `feature_allowlist` / `feature_blocklist`）及 `config.py`（或單一 YAML）內 **FQG v0 閾值**集中設定
- D2c: 外部事件來源 materialization 接入契約（cleaned artifact schema、source metadata、fingerprint 與 report 欄位）
- D3: Gate 0/1/2 篩選報表模板
- D4: 訓練視窗策略比較報表模板
- D5: 每輪實驗決策紀錄模板（含 reason codes）
- D6: Time-CV fold 定義規格（fold 生成邏輯、val 窗口長度、K 預設、min train 天數）
- D7: Time-CV ablation 報表模板（`time_cv_ablation_report.json` schema；含 per-fold `P@1hr` / delta）
- D8: Time-CV 決策閾值集中設定（`config.py`：`FEATURE_SELECTION_TIME_CV_*` 常數族；含 fold 幾何、決策 pp 門檻、early-stop、wall time；**v0 預設試跑、首輪校準**）

## 6) 風險與緩解（Risks & Mitigations）

- 風險：Feature view / group 邊界設計不當，導致維護成本升高。
  - 緩解：先以 group registry 驗證，再映射到 Feast view；避免先拆大量 view。
- 風險：長窗快取誤命中（語意變更未失效）。
  - 緩解：cache key 納入語意版本與 schema 版本，且每次 run 落盤 manifest。
- 風險：只看單次 split 得出錯誤結論。
  - 緩解：固定 eval 區間 + 多切點對照，並記錄統計穩健性指標；邊際特徵與 pruning 決策須走 **Time-CV**（§2.6 / Workstream F）。
- 風險：FQG 抽樣導致統計與全量略有偏差。
  - 緩解：固定 seed、樣本量與版本寫入 report；重大決策可選全量覆核路徑並記錄。
- 風險：外部事件來源清洗規則漂移，導致同一 feature group 在不同 run 中語意不一致。
  - 緩解：每次 run 落盤 source contract reference、cleaning policy id、raw input fingerprint 與 materializer code version；source-specific 細節不得只存在程式註解。
- 風險：實驗 runner 直接讀 raw source table，繞過去重、取消/刪除處理或 PIT event-time 規則。
  - 緩解：外部來源只能透過 materialized cleaned artifact 進入 enrichment；Gate 0 檢查須驗 source metadata 是否存在。
- 風險：筆電資源不足造成實驗排程阻塞。
  - 緩解：採 wave 節奏（S/L/Mix），先跑 group-level 快篩，再做精篩。
- 風險：Time-CV 成本過高（K=5 → 約 5× fits per arm），導致實驗排程阻塞。
  - 緩解：Group-first + early stop + shared baseline；single-split Gate 1 作 quick screen，僅邊際或 pruning 候選進 Time-CV。
- 風險：Time-CV val 窗口太短導致 noise 過大，或太長導致 fold 數不足。
  - 緩解：預設 val 30d 對齊 test split；K 可配；首次 run 落盤 per-fold variance 供後續調整。
- 風險：Time-CV 每 fold 獨立 Optuna 導致超參不一致，污染 feature contribution 估計。
  - 緩解：v0 採固定超參（或第一 fold tune 後套用到其餘 fold）；tuning budget 寫入 report。

## 7) 驗證與升級準則（Validation & Promotion）

- 候選群組升級條件（**v0**，與 `Feature experimentation - WORKING_PLAN.md` §1.4 / §1.5 / §4 對齊）：
  - 通過 **FQG**：擬訓練欄位均為 **PASS** 或已核准之 **WARN**；無未處理之 **BLOCK**（詳 §1.5）
  - 通過 Gate 0（資料品質、非法值、常數率、PIT、單 group 物化時間上限）
  - 若候選來自外部事件來源：必須有 source contract reference、cleaning policy id、raw input fingerprint、materialized artifact fingerprint，且 Gate 0 能確認訓練只使用 cleaned/PIT-safe output。
  - Gate 1（single-split quick screen）：相對同一 baseline 之 **`ΔP@1hr ≥ +1.0pp`**（主）；輔助 **`ΔAP ≥ +0.003`** 且 **`ΔR@Pmin > 0`**（單次固定 val 口徑與 Gate 1 報表定義一致）。邊際或 pruning 候選須再跑 Time-CV（見下）。
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
- **Time-CV feature selection** 升級／淘汰條件（**v0**，對齊 D-006 / D-007）：
  - **主指標（operational）**：`mean(ΔP@1hr) ≥ +1.0pp` 且 `cv_ratio = std(ΔP@1hr) / |mean(ΔP@1hr)| < 0.5` → **KEEP**。
  - **輔助**：`mean(ΔAP) ≥ 0`（不得所有 fold 一致為負）；`Recall@Pmin` 不得在所有 fold 一致劣化（詳細門檻見 Working Plan，後續承接）。
  - **REVIEW**：`mean(ΔP@1hr) ≥ +1.0pp` 但 `cv_ratio ≥ 0.5`（平均正向但不穩定）。
  - **MARGINAL**：`mean(ΔP@1hr) ∈ [-0.5pp, +1.0pp]` → 需人工審查；可參考 AP / Recall@Pmin。
  - **DROP**：`mean(ΔP@1hr) < -0.5pp`。
  - **STRONG DROP**：所有 fold `ΔP@1hr < 0` → 直接 DROP，不進 Gate 2。
  - **成本（v0）**：單 group Time-CV（K=5）wall clock **≤ 20 分鐘**（或已核准豁免並記錄）；超時須記錄並評估 train subsample。
  - **閾值校準（v0）**：`FEATURE_SELECTION_TIME_CV_*` 為**試跑預設**；首輪完成後須檢視 per-fold `ΔP@1hr` 分布再決定是否 bump 常數或調整 K／val 窗口（見 D-009）。
  - **與 single-split Gate 1 路由**：
    - Single-split `ΔP@1hr > +1.0pp` 且無爭議 → 可 **跳過 Time-CV**，直接進 Gate 2。
    - Single-split `ΔP@1hr ∈ [-0.5pp, +1.0pp]` → **必須** Time-CV。
    - Single-split `ΔP@1hr < -0.5pp` → 直接 DROP，不進 Time-CV。
  - **Pruning（LOO）語意**：對 baseline 中既有 feature / group 做 leave-one-out Time-CV；若 STRONG DROP 或 DROP，列為 registry `disabled` 候選（reason code 落盤）。

## 8) 與其他文件邊界

- SSOT：定義「要做什麼與治理準則」。
- 本文件：定義「如何在架構與工作流層落地」。
- Working plan（後續）：定義「任務拆解、順序、依賴與 DoD」；**FQG v0 門檻**見該檔 **§1.5**；**Time-CV pruning wave** 待本文件 M6 落地後於 Working Plan 新增專章。
- 程式落點：`trainer_hightier/feature_experiment/time_cv/`（fold / runner / report）；不取代 `ablation.py` 與 `run_pipeline.py`，以 optional mode 擴充。
- Source-specific findings / schema dictionary：定義單一 raw source 的資料發現、欄位字典與清洗注意事項；例如 `t_casino_txn` 依 `doc/FINDINGS.md` **[FND-19]** 與 `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` **§5**。本文件不得重複其欄位級清洗規則。
