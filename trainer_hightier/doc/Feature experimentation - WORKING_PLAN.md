# trainer_hightier - Feature Experimentation Working Plan（執行計畫）

本文件屬於 **Working / execution plan 層**，承接：

- SSOT：`doc/Data pipeline - SSOT.md`
- Implementation Plan：`doc/Feature experimentation - IMPLEMENTATION_PLAN.md`

內容僅包含 **feature experimentation** 的可執行任務拆解、wave 順序、DoD、gates 清單、營運限制與每輪報表模板。

> **不重疊／不修改**：本計畫 **不取代、不編輯** 既有端到端計畫 `doc/trainer-hightier-working-plan_c12558b9.plan.md`；該檔維持原用途，本檔為特徵實驗之獨立執行面。

---

## 1) 範圍與護欄（Scope & Guardrails）

### 1.1 本 working plan 的範圍

- **In**：候選生成與 group 化管理、**特徵品質閘門 FQG（§1.5）**、Gate 0/1/2 篩選、外部事件來源之 experiment-only materialization 接入、短中長窗分級與快照物化、`feature_compute_range` / `training_sample_range` 解耦、固定 eval 下之訓練視窗策略比較、每輪實驗之報表與 go/no-go。
- **Out**：模型家族替換決策、線上 serving infra、ClickHouse 生產變更、單一 raw source 的欄位級清洗規則。

### 1.2 已鎖定的設計決策（對齊 SSOT + Implementation Plan）

| 項目 | 決策 |
|------|------|
| 主評估指標 | **Average Precision（AP）** 作為不均衡目標之主指標；搭配 precision floor 下之 **Recall@Pmin（R@Pmin）**，其 `min_precision` 對齊 `config.py` 之 `HighTierObjectiveConfig.min_precision`（現行預設 **0.60**）。 |
| 篩選節奏 | **Group-first**：以 feature group 為最小實驗單位；通過 Gate 才進下一層。 |
| 視窗分級 | **短窗** `≤ 1d`；**中窗** `> 1d 且 ≤ 30d`（預設 **daily snapshot**）；**長窗** `> 30d`（預設 **monthly snapshot** + cache + as-of join）。 |
| 歷史邊界 | **解耦並落盤**：`feature_compute_range` = `full_available_history`（PIT 正確）；`training_sample_range` = **no earlier than &lt;date&gt;**（僅縮 training 列，不把特徵回溯裁掉）。 |
| 視窗策略比較 | 固定同一 **val/test** 評估區間；只改 train 政策：`all history` / `rolling 365|180|90|60|30d` / `recency weighting`。 |
| 高相關視窗族 | **語意群組治理**：Gate 2 群內去冗餘（群聚 + 保留代表），不得以單一相關係數閾值粗暴全刪而未記錄語意決策。 |
| 資料覆蓋 | 縮 training 準備範圍時遵守 SSOT：`train_start - max_lookback - safety_buffer` 覆蓋規則並可稽查。 |
| Gate / 視窗 v0 | **定量門檻**見 §1.4（與 SSOT、Implementation Plan §7 對齊）。 |
| **特徵品質閘門 FQG v0** | **L1 全量 + L2 候選**；**BLOCK = fail-fast**；**WARN 需顯式核准** 方可進 Gate 1；閾值與產物見 **§1.5**。 |
| 外部事件來源接入 | **Source-contract-first**：experiment runner 不得直接讀 raw source table 進訓練；必須先透過 source-specific materializer 產出 cleaned / PIT-safe artifact，並在 report 落盤 source contract reference、cleaning policy id、raw input fingerprint、materialized artifact fingerprint。 |
| `t_casino_txn` 清洗邊界 | 本 working plan 只定義執行步驟與 DoD；`t_casino_txn` 欄位級清洗依 `doc/FINDINGS.md` **[FND-19]** 與 `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` **§5**，不在本檔重複。 |

### 1.4 定量門檻 v0（Gate 0/1/2 與訓練視窗穩健性）

本節為 **v0 預設值**；調整需 bump 版本並寫入決策紀錄。數值與 SSOT、`Feature experimentation - IMPLEMENTATION_PLAN.md` 對齊（AP 主指標、Gate 1 之 Δ 門檻、**R@Pmin 嚴格上升**、**val alerts/hour 容量護欄**、Spearman 0.92）；**P25 容忍**為本節補上之可執行起點。

#### Gate 0（資料／契約）

| 檢查項 | v0 門檻 | 說明 |
|--------|---------|------|
| 數值欄缺失率（per column） | **&lt; 40%** | 超過則該欄位不得進 Gate 1；整 group 若無可用欄則 fail |
| 常數率（單值占比） | **&lt; 99.5%** |  |
| 非法值率（NaN / Inf / 超界） | **&lt; 0.5%** | 以數值欄為準；類別欄另用 schema 檢查 |
| PIT | **0 leakage** | 任一確認之 leakage → fail |
| 單 group 物化時間 | **≤ 24 min** | 即不超過單 round 預算（§5：**60 min**）之 **40%** |
| 外部來源 metadata | **必填** | 若 group 來自外部事件來源，必須有 source contract reference、cleaning policy id、raw input fingerprint、materialized artifact fingerprint |

#### Gate 1（群組增量：baseline vs baseline+group，單次固定 val/test）

| 指標 | v0 門檻 |
|------|---------|
| ΔAP（candidate − baseline） | **≥ +0.003** |
| ΔR@Pmin（candidate − baseline） | **> 0**（Recall 必須嚴格上升；不得僅以容忍下降過關） |
| val `alerts/hour`（與 Step 5 報表口徑一致） | **≤ 120**（約 2 alerts/min）；超限須 `capacity_alarm=true` 並預設 **no-go**（除非業務核准豁免並記錄） |
| 單 round wall time | **≤ 60 min**（§5） |

#### Gate 2（群內去冗餘）

| 檢查項 | v0 門檻 |
|--------|---------|
| 高相關分群 | Spearman **ρ 絕對值 > 0.92** |
| 每群保留 | **1–2 個代表欄位** + **至少 1 個** ratio/delta 形狀訊號（若該族有 window ladder） |
| 代表優先序 | **R@Pmin 貢獻 &gt; AP 貢獻 &gt; 可解釋性／成本** |

#### 訓練視窗策略：穩健性切片與 P25（升級 v0）

**Baseline（benchmark）**：訓練政策為 **all history**（與候選策略共用同一 `feature_compute_range`、同一 `eval_fingerprint`）。

**Val 子區間（計算 P25 用）**：在固定 **val** 的 `gaming_day` 範圍內切成 **K=4** 個連續、不重疊、**等天數**子區間；若天數不足 4 段則 **K = min(4, ⌊val 天數⌋)** 且 **K ≥ 2**（仍不足則僅報告 **單一 val 指標** 並標 `p25_not_applicable`，**不得**依 P25 規則升級—需補資料或改 eval）。

**每策略流程**：對該策略與 baseline **各訓練一個模型**（同一超參／搜尋 budget 政策並落盤）；對 val **全體**推論一次；再將 val 列依子區間切片，於每段計算 **AP** 與 **R@Pmin**（`min_precision` 同 `config.py`）。對每個子區間 *i* 計算：**ΔAP_i** = AP_strategy_i − AP_baseline_i；**ΔR_i** = R@Pmin_strategy_i − R@Pmin_baseline_i。

**升級需同時滿足**：

| 統計量 | v0 門檻 |
|--------|---------|
| median(ΔAP_i) | **≥ +0.003** |
| P25(ΔAP_i) | **≥ −0.003** |
| median(ΔR_i) | **> 0** |
| P25(ΔR_i) | **> 0** |
| 單策略「訓練 + 全 val 推論」wall time | **≤ 60 min** |
| 相對 baseline 同口徑 runtime | **≤ baseline + 600 s** |

#### 回退（rollback）觸發 v0

在與被取代策略**相同** `eval_fingerprint` 與 **K 切片定義** 下，令 **Δ′** 為「新標準策略 − 舊標準策略」於各子區間之差值序列。若 **P25(Δ′AP_i) < −0.003** 或 **P25(Δ′R_i) < 0**（Recall 尾部切片不得劣化為負），則啟動回退並保留報表證據。

---

### 1.5 特徵品質閘門 v0（FQG：L1 / L2）

本節為 **欄位級** 品質檢查，預設在 **Gate 1／任何 ablation 訓練之前** 執行（可與 Gate 0 清單 **串接**：Gate 0 所檢之欄位集合 **⊆ FQG allowlist**）。調整門檻需 bump 版本並寫入 SSOT **決策紀錄**。

#### 定位與決策語意

| 狀態 | 行為 |
|------|------|
| **PASS** | 可進入 Gate 0/1（仍須滿足 §1.4 Gate 0 之 group 級門檻，例如每欄缺失 **< 40%** 方可進 Gate 1）。 |
| **WARN** | **不預設擋訓練**；僅在 **feature_allowlist**（或等價欄位）中載明 **核准人、理由、timestamp_utc**；建議可選 **過期日**（預設慣例 **14 天**，過期需重新跑 FQG 或續批）。 |
| **BLOCK** | 該欄 **不得** 納入訓練 feature 集合；管線預設 **fail-fast**（不得 silent drop 後仍訓練）。 |

#### 計算資源預設（避免 OOM）

- **每 split 抽樣列數上限**：**200,000**（固定 **random seed** 落盤）。
- 數值統計預設以 **float32** 路徑計算；逐欄或分批計算，避免一次展開全矩陣。
- 筆電預設避免多個 heavy FQG job 盲目並行（與 §5.1 精神一致）。

#### L1（所有候選欄位必跑）

| 檢查項 | v0 門檻 | 狀態 |
|--------|---------|------|
| 型別／schema 跨 split 一致 | 不一致 | **BLOCK** |
| 數值欄 **inf** 占比 | **> 0** | **BLOCK** |
| 缺失率（任一 split） | **> 98%** | **BLOCK** |
| 缺失率（任一 split） | **70%–98%** | **WARN** |
| 缺失率跨 split 最大差 | **> 20%**（百分點） | **WARN** |
| 常數（`n_unique ≤ 1`） | 是 | **BLOCK** |
| 近常數（單值占比） | **> 99.5%** | **WARN**（與 §1.4 常數率敘述對齊時，BLOCK 優先於 WARN） |
| 數值極端長尾 | `p99_abs / max(\|p50\|, eps) > 1e4` | **WARN** |
| 分類欄：val/test **unseen** 類別列占比 | **> 10%** | **WARN** |
| PIT／洩漏 | 規則命中（禁止欄位名單、未來資訊、與 label 之硬洩漏鍵等，由實作維護名單） | **BLOCK** |

> **與 §1.4 Gate 0 的關係**：FQG L1 之 **BLOCK** 欄位不進 Gate 0 後續訓練路徑；FQG **PASS** 欄位仍須滿足 Gate 0「每欄缺失 **< 40%**」方可進 Gate 1（FQG 不取代該較嚴格之建模門檻）。

#### L2（僅對「L1 為 PASS 或已核准 WARN」且擬進 Gate 1 之欄位）

| 檢查項 | v0 門檻 | 狀態 |
|--------|---------|------|
| PSI（train vs val、train vs test；分箱數由實作固定並落盤） | **≤ 0.1** | **PASS**；**0.1–0.25** **WARN**；**> 0.25** **BLOCK** |
| 跨時間穩定性（預設 **月** 切片；切片內仍遵守抽樣上限） | 關鍵統計（缺失率、p50、p95）相對全 split 基準波動 **> 2×** 之切片數過半 | **WARN**（閾值可於實作 first pass 後校準） |
| 單變量與 label 關係之時間切片方向一致性 | 顯著 uplift 正負翻轉比例 **> 40%** 切片 | **WARN** |
| 缺失指標與 label 關聯 | 跨 split 不穩定且強關聯（MNAR 風險 heuristics，由實作定義） | **WARN** |

#### 必備產物（契約）

以下檔名為 **邏輯名稱**；實際路徑可掛在 `experiments/<run_id>/quality/` 或與 training set manifest 同層，但 **欄位語意** 必須一致：

1. **`feature_quality_report.json`**：每欄 `status`、`failed_checks`、`warn_checks`、各 split 摘要統計、**fqg_version**、**sample_policy**（含 seed、每 split 列上限）。
2. **`feature_allowlist.json`**：允許進 Gate 1 之欄位；WARN 核准須附 **approval** 物件（核准人、理由、時間、可選過期日）。
3. **`feature_blocklist.json`**：BLOCK 欄位與 **reason_code** 清單。

#### 一次性／週期工作

- **現行常用特徵全集**：至少執行一次 **FQG L1** 盤點以建立品質基線（建議併 **Wave 0 exit** 或 Wave 1 entry，見 §2）。

### 1.6 外部事件來源 artifact 契約 v0

本節定義 external event source 進 feature experimentation 的最低執行契約；source-specific 清洗細節由來源文件維護，不在本 working plan 重複。

#### Materialized artifact 最低欄位

| 欄位類型 | 必填內容 |
|----------|----------|
| Join key | 與 training grain 可連接之 entity key（例如 `player_id` 或 `canonical_id`，由 source contract 指定） |
| PIT anchor | 每列特徵對應之 event / prediction 可見時間欄位 |
| Feature columns | Registry 宣告之 candidate feature 欄位；欄名需可追溯至 `feature_id` / `group_id` |
| Source metadata | `source_name`、`source_contract_ref`、`cleaning_policy_id`、`raw_input_fingerprint`、`materializer_code_version`、`materialized_artifact_fingerprint` |
| Audit counts | raw rows、dedup/cleaned rows、joined rows、dropped rows by reason（可在 sidecar JSON，而非寬表欄位） |

#### Fail-fast 條件

- 缺 source contract reference 或 cleaning policy id。
- materialized artifact 沒有 PIT anchor 或 join key。
- artifact fingerprint / raw input fingerprint 未落盤。
- runner 嘗試直接將 raw source table join 入 training set。
- source-specific cleaning contract 標示有 unresolved P0 DQ issue。

---

## 2) Execution waves（進場／出場條件）

### Wave 0 — Registry 與 baseline 對齊

**目的**：先有「可枚举、可追溯」的 group 與 baseline 實驗 signature，後續 wave 不重複爭論語意。

**Entry criteria**

- Implementation Plan Milestone **M1** 方向明確（registry schema + baseline 對應現有 FeatureService）。
- 現有可跑通的最小端到端：`walkaway_bet_trial_v1` 對應之訓練／切分／指標鏈可被指名為 baseline run id。

**Exit criteria**

- `feature_group_registry` 首版可查（YAML/JSON/Python 擇一，與 Implementation Plan Workstream A 一致）：每筆 group 具 `group_id`、`entity_key`、`cadence`、`compute_pattern`、`dependency`、`lookback_days_or_window`、`anchor_rule`（若適用）、`owner`、`status`、`version`。
- **group_signature** 規則已定稿文件化（計入：欄位集合、語意版本、來源契约版本、快照錨規則）。
- Baseline：**一筆已落盤的 baseline experiment report**（見 §6），含完整 manifest 可追溯欄位與決策紀錄欄（即使標記為 `baseline/no-decision`）。
- **FQG L1 基線盤點**：對**現行常用特徵全集**（或 registry 列舉之全候選欄位）至少完成一次 **FQG L1**，並落盤 §1.5 三份產物（可掛 `quality/baseline_l1_<training_set_fingerprint>/`）；作為後續回歸比對基線。

**產出 artifacts（最低）**

- `feature_group_registry` v0.1
- `group_signature_spec` v0.1
- `experiments/baseline_<run_id>/experiment_report.json`（或等價路徑）
- `quality/baseline_l1_<fingerprint>/feature_quality_report.json`（及同目錄之 `feature_allowlist.json`、`feature_blocklist.json`，L2 可為空或標 `not_run`）

---

### Wave 1 — Gate 0/1/2 最小可運作流程

**目的**：任何新 group 必須可重現地通過或淘汰，避免「直接全量訓練」。

**Entry criteria**

- Wave 0 **exit** 達成。
- **FQG**：對本輪擬訓練之候選欄位已完成 **L1**；擬進 Gate 1 之欄位已完成 **L2**（或已文件化豁免原因並不得包含 BLOCK）；§1.5 三份 JSON 已落盤且 **無未處理 BLOCK**（或已選擇 **fail-fast 中止**）。
- Screening 報表模板欄位已定（可先用 markdown/JSON schema；見 §6）。

**Exit criteria**

- **FQG**：Gate 1 runner 僅使用 **feature_allowlist** 內欄位；與 `gate1_compare_*.json` 中 `feature_columns` 可雙向追溯。
- **Gate 0**：DQ + PIT + 可計算性檢查清單可人工或半自動跑完並產出 pass/fail + reason code（檢查欄位 ⊆ allowlist）。
- **Gate 1**：baseline vs baseline+**單一 group** 的對照 runner 可跑；產出 AP、R@Pmin、runtime、peak RAM、cache hit（若適用）。
- **Gate 2**：群內去冗餘流程可跑（相關群聚 + 代表保留 + 淘汰紀錄）；產出「保留清單 / 淘汰清單 / 理由」。
- 至少 **1 個** 非 baseline group 完成 **0→1→2** 全流程 dry-run（可用小型時間切片或抽樣子集，但必須在報表註明 **subset policy** 與 **不可直接與全量比較** 之警語）。

**產出 artifacts（最低）**

- `quality/fqg_<run_id>/feature_quality_report.json`（及 `feature_allowlist.json`、`feature_blocklist.json`）
- `screening/gate0_checklist_run_<id>.md`（或 JSON）
- `screening/gate1_compare_<baseline>_vs_<group>_<id>.json`
- `screening/gate2_redundancy_<group>_<id>.json`

---

### Wave 1b — 外部事件來源最小接入（Experiment-only）

**目的**：讓非既有 Feast / `fe_derived` 路徑的 raw event source 能以 source-specific materializer 進入 isolated feature experiment，而不影響 production trainer / serving。

**Entry criteria**

- Wave 1 **exit** 達成，或至少 FQG + Gate 0/1 runner 已可對單一 group 產出標準報表。
- Source-specific findings / schema dictionary 已存在，並清楚標示該來源的 P0 DQ 與清洗邊界。
- Registry 已能標記 experiment-only source（例如 `source=<external_source_name>`）與 `enabled_for: ablation` / `candidate`。

**Exit criteria**

- Source-specific materializer 可產出 cleaned / PIT-safe feature parquet；runner 不直接讀 raw source table。
- Materialized artifact 具備 §1.6 最低欄位與 sidecar metadata，含 source contract reference、cleaning policy id、raw input fingerprint、materializer code version、artifact fingerprint。
- Gate 0 能驗證 external source metadata 完整，且訓練欄位集合仍為 FQG allowlist 子集。
- 至少 **1 個** external-source group 完成 baseline vs baseline+group dry-run；若使用小型時間切片或 subset，報表須標註 subset policy 與不可直接與全量比較。
- External source 的欄位級清洗規則未被複製進本 working plan；報表只引用來源文件與 policy id。

**產出 artifacts（最低）**

- `external_sources/<source_name>/materialized_features.parquet`（或 run-scoped 等價路徑）
- `external_sources/<source_name>/materialization_report.json`
- `external_sources/<source_name>/source_metadata.json`
- `screening/gate1_compare_<baseline>_vs_<external_group>_<id>.json`

---

### Wave 2 — 中窗（daily）與長窗（monthly）快取路徑

**目的**：把「昂贵」聚合從預設全量即時計算迁到 **分 cadence snapshot + manifest 驗證**，並保留 staleness 訊號。

**Entry criteria**

- Wave 1 **exit** 達成。
- Registry 已定義至少 1 個中窗組、1 個長窗組（可沿用現行 slow patron 180d 作為長窗樣板）。

**Exit criteria**

- 中窗：daily anchor 之物化／快取鍵規則可驗證（至少 **manual replay**：同輸入同 key → 同輸出指紋）。
- 長窗：monthly anchor + as-of join 路徑可驗證；**staleness／freshness 欄位**（例：`days_since_snapshot_anchor`）進入训练特征表或可查之 side manifest。
- **cache_manifest** ：每次 materialization/run 落盤，包含 SSOT 要求之語意：**來源指紋、lookback、anchor、程式版本、schema 版本、group signature**。
- dirty-date／分區变动之 **失效傳播規則** 至少文件化（若尚未全自动，須明示人工介入點）。

**產出 artifacts（最低）**

- `materialization/cache_manifest_<group>_<anchor>.json`
- `materialization/asof_join_validation_<id>.md`（含抽樣 PIT spot-check 紀錄）
- 更新後之 registry：**cadence/compute_pattern 與快取语义**對應到新流程

---

### Wave 3 — 訓練視窗策略比較（Window Strategy Runner）

**目的**：在 **固定 eval** 下可比較 all / rolling / recency weighting；並強制區分 `feature_compute_range` 與 `training_sample_range`。

**Entry criteria**

- Wave 2 **exit**（或至少長窗組已 stable；若並行需在報表標註風險）。
- Runner 能接受：`training_sample_range=no earlier than YYYY-MM-DD` 且 `feature_compute_range=full_available_history`。

**Exit criteria**

- 策略集合最少跑過：`all`、`rolling {365,180,90,60,30}d`、`recency weighting ≥1 組半衰期`（半衰期指定期初值並落盤）。
- 每一策略產出 **§6** 之標準報表列 + manifest（含 **`val_slice_robustness_v0`** 或等價欄位，對齊 §1.4）；並有 **coverage 稽查**：`train_start - max_lookback - safety_buffer` 對該策略是否滿足（不滿足者標 `fail-fast` reason）。
- **決策紀錄**：推薦策略須註明是否通過 **§1.4** 之 median／P25 與 runtime 門檻；並載明 **後選**、**回退策略**（上一份通過之策）引用路徑。

**產出 artifacts（最低）**

- `window_strategy/benchmark_<eval_fingerprint>_<id>.json`
- `window_strategy/decision_record_<id>.md`

---

## 3) 任務拆解（Task breakdown）

以下依 Implementation Plan **Workstream A–E**（含 **C2 外部事件來源接入**）拆分；**Owner** 以占位表示，開始執行前指派。

### Workstream A — Registry 與語意契約

| Task ID | Task | Owner | 依賴 | DoD | 產出 |
|---------|------|-------|------|-----|------|
| A1 | 定稿 registry schema（欄位必填/選填、enum） | TBD | — | schema 審查通過、例項 ≥3 groups | `registry_schema.md` + 範例檔 |
| A2 | 定稿 group_signature 計算規則 | TBD | A1 | 同兩次計算 signature 一致；變更必 bump version | `group_signature_spec.md` |
| A3 | 映射現有 Feast `FeatureView`/`FeatureService` → group_id | TBD | A1 | 每個現行 production demo group 有唯一 group_id | registry 內 `mapping` 區塊 |
| A4 | Baseline run 封版與 report | TBD | A3 | §6 欄位齊、可重跑 | `experiments/baseline_*/` |

### Workstream B — 長窗物化與快取

| Task ID | Task | Owner | 依賴 | DoD | 產出 |
|---------|------|-------|------|-----|------|
| B1 | 短/中/長窗排程表（何時全量/增量） | TBD | A1 | 與 SSOT 分區增量敘述一致 | `cadence_schedule.md` |
| B2 | 中窗 daily snapshot 管線雛型 | TBD | B1 | 產出 manifest + 可重跑 | 程式變更 + manifest 樣本 |
| B3 | 長窗 monthly snapshot + cache key | TBD | B1 | key 含完整語意欄位；miss/hit 可解釋 | `cache_manifest_*.json` |
| B4 | as-of join + staleness 欄位 | TBD | B2,B3 | spot-check 紀錄通過 | 驗證筆記 +欄位入表或 sidecar |
| B5 | dirty-date / 失效傳播文件 | TBD | B3 | 至少覆蓋「新分區抵達」與「契约版本 bump」 | `invalidation_rules.md` |

### Workstream C — Screening 引擎

| Task ID | Task | Owner | 依賴 | DoD | 產出 |
|---------|------|-------|------|-----|------|
| C0 | **FQG** L1/L2 工具化與三份 JSON 契約 | TBD | A4 | §1.5 門檻可重現；BLOCK 觸發 fail-fast；WARN 需 approval 才可進 allowlist | `quality/.../feature_*.json` |
| C0b | **config 集中閾值**：`FEATURE_QUALITY_GATE`（或單一 YAML）與版本欄位 | TBD | C0 | 不仰賴環境變數切行為 | `config.py` 或約定之設定檔 |
| C1 | Gate 0 檢查清單工具化 | TBD | A4, C0 | 可產出 pass/fail + reason codes；輸入欄位 ⊆ allowlist | script 或 notebook + 範例輸出 |
| C2 | Gate 1 對照 runner（單 group 增量） | TBD | A4,B* | 固定 seed/參數可重現；輸出 §6 | `gate1_compare_*.json` |
| C3 | Gate 2 去冗餘（群聚+代表） | TBD | C2 | 淘汰清單含語意理由 | `gate2_redundancy_*.json` |
| C4 | reason code 字典 | TBD | C1–C3 | 團隊 review | `reason_codes.md` |

### Workstream C2 — 外部事件來源 Materialization 接入

| Task ID | Task | Owner | 依賴 | DoD | 產出 |
|---------|------|-------|------|-----|------|
| CX1 | 定義 external source artifact schema 與 sidecar metadata | TBD | A1,C0 | §1.6 欄位可由 runner 驗證；缺欄 fail-fast | `external_source_artifact_schema.md` 或等價 schema |
| CX2 | 實作 materializer 插槽 / runner hook | TBD | CX1,C2 | 可在 isolated run_dir 產出 cleaned feature parquet；不直接 join raw table | 程式變更 + 範例 artifact |
| CX3 | Registry source mapping | TBD | A1,CX1 | external-source 欄位可用 `source`、`group_id`、`enabled_for` 被選入 candidate/ablation | registry 範例 + loader 測試 |
| CX4 | Gate 0 external source metadata check | TBD | C1,CX1 | 缺 source contract / policy id / fingerprint 時 fail-fast | `gate0_checklist` 更新 |
| CX5 | First external-source dry-run | TBD | CX2,CX3,CX4 | 至少 1 個 external group 完成 FQG + Gate 0 + Gate 1 dry-run | `materialization_report.json` + `gate1_compare_*.json` |

### Workstream D — Window Strategy Runner

| Task ID | Task | Owner | 依賴 | DoD | 產出 |
|---------|------|-------|------|-----|------|
| D1 | 固定 eval fingerprint 定義 | TBD | A4 | val/test 邊界單一真相 | `eval_fingerprint.json` |
| D2 | 實作 hard window 集合 | TBD | D1 | 五組 rolling + all 可跑 | runner 參數 + 報表 |
| D3 | 實作 recency weighting | TBD | D1 | 半衰期與權重分佈落盤 | 報表 + 權重摘要 |
| D4 | coverage 稽查 | TBD | D2,D3 | 違反者 fail-fast | 稽查 log |
| D5 | 策略比較總表 | TBD | D2–D4 | Pareto（AP vs cost）初版 | `window_strategy/benchmark_*.json` |

### Workstream E — 治理與報表

| Task ID | Task | Owner | 依賴 | DoD | 產出 |
|---------|------|-------|------|-----|------|
| E1 | 實驗 ID / run id 命名規範 | TBD | — | 全 wave 採用 | `naming_convention.md` |
| E2 | artifacts retention（最短保留期） | TBD | E1 | 與磁碟预算一致 | `retention_policy.md` |
| E3 | 串接既有 `run_report` | TBD | C2,D5 | 欄位不缺 | 範例 run 目錄 |

---

## 4) Decision gates 與升級／回退規則

### 4.0 Feature Quality Gate（FQG，欄位級）

**目標**：在 **Gate 1 訓練／ablation** 前擋下結構性壞欄位與高漂移欄位；產物可審計。**數值與產物**以 **§1.5** 為準。

檢查項（逐項 tick + 證據連結）：

- [ ] **L1** 已對本輪**所有候選欄位**執行並落盤 `feature_quality_report.json`。
- [ ] **L2** 已對「擬進 Gate 1」之欄位子集執行（或已文件化 **L2 豁免** 且不包含任何 BLOCK 欄位於訓練）。
- [ ] **`feature_allowlist.json`** 與 **`feature_blocklist.json`** 已產出；**WARN** 僅在具 **approval** 時列入 allowlist。
- [ ] 若存在未處理之 **BLOCK**：管線 **fail-fast** 中止（不得 silent 繼續訓練）。
- [ ] **抽樣政策**（seed、每 split 上限）已寫入 report。

**輸出**：`fqg_status` ∈ {`pass`,`fail`}；`fail` 必須有 **reason_code**（含 `blocked_columns_present` 等）。

### 4.1 Gate 0 — 資料／契約（可操作清單）

**目標**：未通過者 **不得** 進模型訓練（僅允許修資料或修契约後重送）。**數值門檻**以 §1.4 為準。檢查所涵蓋之欄位集合 **必須 ⊆ FQG allowlist**（§4.0）。

檢查項（逐項 tick + 證據連結）：

- [ ] **Schema／型別**：與 Feast contract 一致；新增欄位已版本化。
- [ ] **數值欄缺失率**：每欄 **< 40%**（§1.4）；超標欄位不得進 Gate 1。
- [ ] **常數率** **< 99.5%**；**非法值率** **< 0.5%**（§1.4）。
- [ ] **PIT**：entity_df 時間戳、特徵 event timestamp、ttl 配置合理；抽樣 spot-check **0 leakage**。
- [ ] **單 group 物化時間** **≤ 24 min**（§1.4，佔 60 min round 之 40%）。
- [ ] **文件**：registry 中該 group 之 dependency、lookback、anchor 完整。
- [ ] 若為外部事件來源 group：已提供 source contract reference、cleaning policy id、raw input fingerprint、materialized artifact fingerprint；訓練僅使用 cleaned / PIT-safe artifact（§1.6）。

**輸出**：`gate0_status` ∈ {`pass`,`fail`}；`fail` 必須有 **reason_code**。

### 4.2 Gate 1 — 群組增量（baseline + group）

**目標**：證明「加上整包 group」帶來 **AP 與 R@Pmin** 之淨利，且成本可接受。**數值門檻**以 §1.4 為準。

檢查項：

- [ ] 訓練與推論使用之 **feature 欄位 ⊆ `feature_allowlist.json`**（§4.0）。
- [ ] 若本輪含外部事件來源：`gate1_compare_*.json` 必須引用 materialized artifact path 與 source metadata；不得只記 raw source path。
- [ ] 與 **同一 baseline** 對照（同 eval fingerprint、同模型搜尋 budget 或同固定超參—擇一並落盤）。
- [ ] **ΔAP ≥ +0.003** 且 **ΔR@Pmin > 0**（單次固定 val/test；Recall 嚴格上升）。
- [ ] **val `alerts/hour` ≤ 120**；若 **> 120**，報表必須 `capacity_alarm=true` 且預設 **no-go**（除非業務核准豁免並記錄）。
- [ ] 報表含 §6 成本欄位；**單 round runtime ≤ 60 分鐘**（§5）或已核准豁免並記錄。
- [ ] 若 **ΔR@Pmin ≤ 0**（即使 ΔAP 上升），預設 **no-go** 除非業務書面豁免。

**輸出**：`gate1_decision` + `uplift_ap`、`uplift_r_at_pmin`、`val_alerts_per_hour`、`capacity_alarm`、`cost_delta`。

### 4.3 Gate 2 — 群內去冗餘

**目標**：處理高相關 ladder（如 15m/30m/60m），保留 **語意代表**，避免穩定選模飄移。**門檻**以 §1.4 為準。

檢查項：

- [ ] 相關矩陣／群聚方法與參數落盤；高相關閾值 **Spearman ρ 絕對值 > 0.92**。
- [ ] 每個 cluster **保留 1–2 代表** + **至少 1 個** ratio/delta（若該族有 ladder）。
- [ ] 代表優先序：**R@Pmin 貢獻 > AP 貢獻 > 可解釋性／成本**。
- [ ] 淘汰清單附 **語意理由**（非僅相關係數）。

**輸出**：`promoted_features` / `pruned_features` / `record`.

### 4.4 Group 升級／淘汰／回退

| 規則 | 內容 |
|------|------|
| 升級（promote group） | **§4.0 FQG pass**（無未處理 BLOCK；WARN 已核准）；Gate 0–2 全通過且符合 §1.4 數值（含 **ΔR@Pmin > 0** 與 **val alerts/hour ≤ 120**）；成本不超 **§5**；決策紀錄齊全。 |
| 淘汰（reject group） | **FQG BLOCK** 或 Gate 0 fail；或 Gate 1 未達 §1.4；或 Gate 2 顯示可由既有代表完全替代且無邊際貢獻。 |
| 同時升級數量上限 | **每個實驗 wave 最多正式 promote 2 個新 group**（其餘標 `deferred` 排隊）；若需突破，需記錄理由與資源預算。 |
| 回退（rollback） | 保留上一檔 `promoted_groups` 清單與完整 report；觸發條件見 **§1.4 回退（rollback）觸發 v0**（與訓練視窗策略之 P25 規則一致精神：尾部切片不可顯著劣化）。 |
| 外部事件來源升級限制 | v0 只允許升級到 **experiment candidate / further investigation**；不得直接進 production baseline 或 serving，除非另有 production supplyability / serving implementation plan。 |

### 4.5 訓練視窗策略升級（與 group 分開）

| 規則 | 內容 |
|------|------|
| Baseline | **all history** 訓練政策。 |
| 穩健性 | §1.4：val 內 **K** 子區間、median 與 **P25** 門檻、runtime 絕對與相對上限。 |
| 回退 | §1.4：新標準相對舊標準之 P25 劣化觸發。 |

---

## 5) 營運限制（Operational constraints）

### 5.1 成本與資源

- **營運承接護欄（v0）**：以 **val** 上與 Step 5 一致口徑計算之 **`alerts/hour` 平均 ≤ 120**（約 2 alerts/min）。超過則報表必須標 **`capacity_alarm=true`**，並預設阻擋升級／需人工確認（與 §1.4 Gate 1、§6 報表欄位一致）。
- **單 round（單一 Gate 1 或單一 window 策略 run）目標 wall time ≤ 60 分鐘**。超時需：縮小 `training_sample_range`、降低搜尋 budget、或改走 snapshot 命中路徑—擇一並記錄。
- **Peak RAM**：預設以 `config.py` 中既有 DuckDB / profile 設定為起點；若需提高，必須在報表 `resource_notes` 註明。
- **並行**：筆電環境避免預設多組 heavy materialization 並行；registry 標記 `compute_tier` 協調排程。

### 5.2 失敗處理

- **OOM / 被殺 process**：該 round 標 `fail`；下輪必須先降載（較短 rolling、較少特徵、較小 chunk）再重試。
- **Cache miss 風暴**：禁止 silent fallback；需顯式選擇「延長 runtime 重算」或「中止」並記錄。
- **PIT 疑慮**：一律 **block promote**，僅允許 `investigate` 狀態。
- **FQG BLOCK 仍存在卻繼續訓練**：視為流程錯誤；該 round **fail**，需修正 allowlist／資料後重跑。
- **外部來源 metadata 缺失**：缺 source contract reference、cleaning policy id、raw input fingerprint 或 artifact fingerprint 時，該 round **fail-fast**。
- **Raw source 直連訓練集**：視為流程錯誤；該 round **fail**，需改由 cleaned / PIT-safe materialized artifact 進入 enrichment。

---

## 6) 每輪實驗報表模板（固定欄位）

以下欄位為 **必填**；型式可 JSON + 匯出 md。

```json
{
  "experiment_id": "<naming per E1>",
  "run_id": "<git sha or pipeline id>",
  "timestamp_utc": "<ISO8601>",
  "feature_list_version": "<registry version + promoted feature hash>",
  "group_set": ["<group_id>", "..."],
  "baseline_group_set": ["<group_id>", "..."],
  "window_policy": {
    "feature_compute_range": "full_available_history",
    "training_sample_range": "no earlier than YYYY-MM-DD",
    "train_window_mode": "all|rolling_365d|...|recency_half_life_Xd"
  },
  "eval_fingerprint": "<hash or path to eval_fingerprint.json>",
  "external_sources": [
    {
      "source_name": "<source_name>",
      "source_contract_ref": "<doc path + section or contract id>",
      "cleaning_policy_id": "<policy id/version>",
      "raw_input_path": "<path>",
      "raw_input_fingerprint": "<hash>",
      "materializer_code_version": "<hash or module version>",
      "materialized_artifact_path": "<path>",
      "materialized_artifact_fingerprint": "<hash>",
      "raw_rows": 0,
      "cleaned_rows": 0,
      "joined_rows": 0,
      "dropped_rows_by_reason": {}
    }
  ],
  "feature_quality": {
    "fqg_version": "v0",
    "feature_quality_report_path": "<path>",
    "feature_allowlist_path": "<path>",
    "feature_blocklist_path": "<path>",
    "fqg_status": "pass|fail",
    "blocked_feature_count": 0,
    "warn_approved_feature_count": 0
  },
  "metrics": {
    "ap_val": 0.0,
    "ap_test": 0.0,
    "r_at_pmin_val": 0.0,
    "r_at_pmin_test": 0.0,
    "pmin_used": 0.6,
    "val_alerts_per_hour": 0.0,
    "capacity_alerts_per_hour_cap": 120,
    "capacity_alarm": false,
    "val_slice_robustness_v0": {
      "k_slices": 4,
      "delta_ap_per_slice": [],
      "delta_r_at_pmin_per_slice": [],
      "median_delta_ap": null,
      "p25_delta_ap": null,
      "median_delta_r_at_pmin": null,
      "p25_delta_r_at_pmin": null,
      "p25_not_applicable": false
    }
  },
  "cost": {
    "runtime_sec": 0,
    "peak_ram_mb": 0,
    "cache_hit_ratio": null
  },
  "decision": {
    "result": "go|no-go|defer",
    "reason_codes": ["<code>"],
    "notes": "<human readable>"
  }
}
```

### 6.1 驗收（本 working plan 文件層級）

- [ ] **§1.5 FQG v0** 已定義（L1/L2、PASS/WARN/BLOCK、抽樣與產物）。
- [ ] 已涵蓋 Wave 0–3 之 entry/exit 與 artifacts（含 Wave 0 **FQG L1 基線**、Wave 1 **FQG 產物**）。
- [ ] 已涵蓋 **Wave 1b 外部事件來源最小接入** 之 entry/exit、artifacts 與 fail-fast 條件。
- [ ] **§4.0 FQG** 與 Gate 0/1/2 已轉為可勾選清單與必填輸出，且與 **§1.4 v0** 數值一致。
- [ ] 訓練視窗比較報表可填 **§6** 之 `val_slice_robustness_v0`（或等價欄位）以支撐 median／P25 門檻。
- [ ] 升級／回退／同 wave promote 上限已載明。
- [ ] 單 round runtime ≤ 60 分鐘與資源注意事項已載明。
- [ ] 報表模板含：`feature_quality`（FQG 路徑與狀態）、`feature_list_version`、`group_set`、`window_policy`、`AP`、`R@Pmin`、`val_alerts_per_hour`、`capacity_alarm`、`runtime`、`peak RAM`、`cache_hit_ratio`、`go/no-go` + reason。
- [ ] 報表模板含：`external_sources` metadata（source contract、cleaning policy、raw/materialized fingerprint、artifact path、row counts）。
- [ ] **未修改** `trainer-hightier-working-plan_c12558b9.plan.md`。

---

## 7) Traceability（文件追溯）

| 層級 | 文件 |
|------|------|
| SSOT | `Data pipeline - SSOT.md` |
| Implementation Plan | `Feature experimentation - IMPLEMENTATION_PLAN.md` |
| Working Plan（本檔） | `Feature experimentation - WORKING_PLAN.md` |
| Source findings | `doc/FINDINGS.md`（例如 `t_casino_txn` [FND-19]） |
| Raw schema dictionary | `schema/GDP_GMWDS_Raw_Schema_Dictionary.md`（例如 §5 `t_casino_txn`） |

---

*文件版本：依 registry / experiment 慣例自行 bump；本檔對齊 SSOT 與 Implementation Plan，並已納入 **FQG v0（§1.5、§4.0）** 與 **外部事件來源 experiment-only 接入（§1.6、Wave 1b）** 之執行與驗收敘述。*
