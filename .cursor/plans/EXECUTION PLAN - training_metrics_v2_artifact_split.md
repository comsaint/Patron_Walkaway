# Execution Plan：`training_metrics` v2 Artifact Split

> 文件層級：**Working / Execution Plan**（只定義可執行任務、順序、DoD、風險與驗收；不重寫產品 SSOT）。  
> 目的：依 `doc/training_metrics_v2_artifact_split_implementation_plan.md`，將目前 `training_metrics.json` 混合承載的 **run contract、train/val/test 指標、comparison family 明細、feature importance** 拆分為穩定 artifact 契約。  
> 上游對齊：`doc/training_metrics_v2_artifact_split_implementation_plan.md`。  
> 契約代號：`training-metrics-v2` + `feature-importance-v1` + `comparison-metrics-v1`
> 本輪範圍限制：**只改 metric report object / artifact 契約，不改 negative sampling、split、或 test 真實分布管線行為**；後者在後續 pipeline 工作中處理。

---

## 0. 協作約定

- 本檔路徑：`.cursor/plans/EXECUTION PLAN - training_metrics_v2_artifact_split.md`
- Implementation Plan：`doc/training_metrics_v2_artifact_split_implementation_plan.md`
- 架構／契約變更的決策摘要：追加至 `.cursor/plans/DECISION_LOG.md`（新 DEC 編號或沿用既有 W2 契約擴充段落，擇一，**不可雙軌敘述**）
- 執行流水與驗證結果：`.cursor/plans/STATUS.md` 簡記（含影響腳本與 pytest 範圍）

### 0.1 狀態標記（本檔）

| 標記 | 意義 |
| :--- | :--- |
| **✅** | 該項 DoD 已滿足 |
| **🟡** | MVP 已落地，但尚未滿足完整 DoD |
| **⬜** | 未開始 |

---

## 1. 問題陳述與目標

### 1.1 現況痛點

- **維度混用**：`train_*` / `val_*` / `test_*` 扁平 prefix 與巢狀 `rated` / `gbm_bakeoff` 並存，讀取端易誤判層級（歷史已在 `archive/PLAN_phase2_p0_p1.md` 的 T-TrainingMetricsSchema 註記）。
- **責任過載**：同檔同時承載 KPI、選模分數、候選 bakeoff、explainability（`feature_importance`），不利 diff、審計與下游 allow-list。
- **語意風險**：`val_field_test_primary_score` 為 **selection** 用途；test 端若未對稱封裝，易被誤讀為「沒評 test」（實際上 test 指標存在但分散）。

### 1.2 本計畫要達成的目標（DoD 總覽）

1. **Phase A/B 雙寫**：保留 `training_metrics.json`（legacy v1），新增 `training_metrics.v2.json`。
2. **大 blob 外移**：`feature_importance` 改寫 **`feature_importance.json`**，不再塞入 v2 主檔。
3. **候選比較集中**：comparison family 明細改寫 **`comparison_metrics.json`**，採單一總檔 + `families` registry。
4. **向後相容**：過渡期內既有 consumer **不破**；reader 優先讀 v2，失敗 fallback v1。
5. **canonical field-test metric 命名清楚**：直接使用 `field_test.precision` / `field_test.precision_type`，不使用 `primary_score` / `proxy_score` 類術語。
6. **驗證**：相關 unit tests / review_risks tests 更新；至少一次 smoke 訓練產物目錄結構符合新契約。

---

## 2. 目標 artifact 佈局（執行採納版）

以下檔案皆位於同一 model bundle 目錄（與現況 `out/models/<model_version>/` 一致）：

| 檔案 | 職責 |
| :--- | :--- |
| `training_metrics.json` | **legacy v1**：Phase A/B 過渡期保留；供尚未遷移 consumer 使用 |
| `training_metrics.v2.json` | **v2**：最終勝出模型之核心 KPI / selection 摘要 / datasets 巢狀指標；**不含**長列表 importance、comparison families 全量 |
| `feature_importance.json` | **v1**：winner 之 importance（method、backend、items、可選 summary） |
| `comparison_metrics.json` | **v1**：所有 comparison family 的總表；以 `families.<family_name>` 區隔 |
| `feature_list.json` | **維持現狀**：`[{name, track}]` train-serve parity 契約；**不**塞入 run-specific importance 數值 |
| `model_metadata.json` | **擴充**：增加 `artifacts.training_metrics_v2_path` / `feature_importance_path` / `comparison_metrics_path` pointer，**不**承載長陣列 |

---

## 3. Implementation 對齊與凍結決策

本 execution plan **不再承載完整 schema**；完整結構責任分界與模組邊界以上游 Implementation Plan 為準。  
本檔只凍結下列執行決策：

1. **檔名與版本策略**
   - Phase A/B：`training_metrics.json` 保持 legacy v1，新增 `training_metrics.v2.json`
   - Phase A/B 不採「沿用 `training_metrics.json` + 檔內 `schema_version` 判斷 v1/v2」方案
   - Phase C/D：待 reader 全數遷移後，再評估是否切換 `training_metrics.json` 指向 v2
2. **comparison artifact 策略**
   - 單一檔名：`comparison_metrics.json`
   - 單一總檔 + `families` registry
   - 不採 `candidate_metrics.json`
   - 不採每個 family 一檔的爆量策略
   - candidate `datasets.*` 與 `training_metrics.v2.json` **同名 schema、允許子集、不允許換名**
3. **run-contract 相容策略**
   - v2 在 Phase D 前保留頂層 denormalized keys：
     - `selection_mode`
     - `selection_mode_source`
     - `production_neg_pos_ratio`
4. **`feature_list.json` 契約不變**
   - 維持 `[{name, track}]`
   - 不加入 run-specific importance 數值
5. **winner / field-test reporting 關鍵鍵名凍結**
   - comparison family 的 winner 一律記於：
     - `comparison_metrics.json["families"][<family_name>]["winner_id"]`
   - 若需記錄本次選模使用的 metric，應以直白 metadata 表達，例如 `selection_metric="field_test_precision"`；不得再使用 `primary_metric`
   - field-test canonical metric 一律直接命名為 `precision`
   - precision 的口徑一律記於 `precision_type`，值只允許：
     - `raw`
     - `prod_adjusted`
   - validation 端對應 selection-side field-test 指標一律記於：
     - `training_metrics.v2.json["datasets"]["val"]["field_test"]["precision"]`
     - `training_metrics.v2.json["datasets"]["val"]["field_test"]["precision_type"]`
   - winner 在 test set 的 canonical field-test reporting 一律記於：
     - `training_metrics.v2.json["datasets"]["test"]["field_test"]["precision"]`
     - `training_metrics.v2.json["datasets"]["test"]["field_test"]["precision_type"]`
   - 舊的 `precision@recall=1%` 僅作 convenience / reference metric，不再以 `primary` / `proxy` 命名表達
6. **本輪 scope 控制**
   - 本輪只改 metric report object / artifact 契約
   - 不改 Step 6 negative sampling 與 Step 7 test split 行為
   - test 必須 unsampled true distribution 的要求，留待後續 pipeline follow-up

---

## 4. 分階 rollout（降低一次性破壞面）

### Phase A — 只新增、不改舊（預設關閉或環境變數開啟）⬜

**目標**：訓練結束後 **額外寫出** `training_metrics.v2.json`、`feature_importance.json`、`comparison_metrics.json`；**保留**既有 `training_metrics.json`（v1）內容不變。

**DoD**

- 新檔可由一次標準訓練產生
- `model_metadata.json` 增加可選 `artifacts` paths（不破舊 consumer）
- Phase A deploy bundle 視三個新檔為 required

### Phase B — 讀取端切換（feature flag）⬜

**目標**：`bundle_run_contract`、報表腳本、investigations collectors **優先讀 `training_metrics.v2.json`**；若不存在則 fallback v1。

**DoD**

- `trainer/core/bundle_run_contract.py` 具備單一入口讀取 contract（避免各腳本各寫一套 path）
- `trainer/scripts/report_w2_objective_parity.py`、`build_w1_freeze_evidence.py`、baseline_models 讀取邏輯更新或包一層 helper

### Phase C — v1 `training_metrics.json` 瘦身（deprecation）⬜

**目標**：預設不再把 `feature_importance` / `gbm_bakeoff` 大塊寫入 v1；或 v1 僅保留 **向後相容最小子集** + `superseded_by`。

**DoD**

- 文件與 DECISION_LOG 明確 **EOL 日期** 或 **版本門檻**（例如「自 `training-metrics.v2` 全面預設啟用後 N 個 sprint」）
- 全 repo grep 無裸讀舊路徑之殘留（或殘留列為允許清單）

### Phase D — 預設只寫 v2（v1 移除或 stub）⬜

**目標**：`training_metrics.json` 直接為 v2；若仍需 v1，改為 `training_metrics.legacy.json`（僅 debug）。

**DoD**

- CI / pytest 全綠
- 至少一輪實機 bundle 審閱通過（人力 sign-off）

---

## 5. 工作分解結構（WBS）

### W1 — Schema 規格定稿與 migration map ⬜

- 產出「**v1 flat key → v2 path**」對照表（machine-readable JSON 更佳，位置建議：`trainer/core/training_metrics_v2_map.json` 或 doc 下附錄，**擇一**）
- 定義 **null / missing** 語意（與 backtester `None -> reason_code` 精神一致處要寫清楚）
- 將下列 key 視為 **frozen shortlist**，不得在實作中任意改名：
  - `comparison_metrics.json["families"][<family_name>]["winner_id"]`
  - `training_metrics.v2.json["datasets"]["val"]["field_test"]["precision"]`
  - `training_metrics.v2.json["datasets"]["val"]["field_test"]["precision_type"]`
  - `training_metrics.v2.json["datasets"]["test"]["field_test"]["precision"]`
  - `training_metrics.v2.json["datasets"]["test"]["field_test"]["precision_type"]`
- comparison family 內 candidate `datasets.*` 一律與 `training_metrics.v2.json` 同名；允許子集，不允許換名

**DoD**：對照表 reviewed；無「同一語意兩個欄位」之重複

### W2 — Writer：`save_artifact_bundle` / trainer metrics assembly ⬜

- `trainer/training/trainer.py`：組裝 v2、寫出新檔、從 v1 metrics dict **抽離** importance / bakeoff
- `trainer/training/gbm_bakeoff.py`：將既有 `gbm_bakeoff` 明細導向 `comparison_metrics.json.families.gbm_bakeoff`
- 維持 `feature_list.json` 生成邏輯不變（見 `trainer.py` 現有 `feature_list` 寫入）
- 本輪不改 test sampling / split 行為；僅重整 report object 的命名、結構與 artifact 位置

**DoD**：單次訓練目錄內可見四檔；v1 行為在 Phase A 不變

### W3 — Reader：單一 helper + fallback ⬜

- 新增或擴充 `trainer/core/` 下小模組（命名建議：`training_metrics_bundle.py`）：`load_training_metrics_bundle(root) -> {v2?, v1?, paths}`

**DoD**：全 repo 讀 metrics 的腳本優先改呼叫 helper（grep 清單見 §7）

### W4 — Tests ⬜

- 更新 `tests/unit/test_report_w2_objective_parity.py`、`tests/unit/test_build_w1_freeze_evidence.py`、baseline_models smoke、review_risks 內依賴 fixture
- 新增最小 fixture：含 v1+v2 雙檔，驗證 fallback 與欄位對齊

**DoD**：相關 pytest 子集綠；必要時標註「需 DB 的測試」不納入本計畫 gate

### W5 — 文件與運維 ⬜

- `trainer/training/trainer.py` 頂部註解更新（現仍寫 importance 在 `training_metrics.json`）
- `.cursor/plans/STATUS.md`：記一次實跑驗證路徑與範例 bundle

**DoD**：新同事只看計畫 + trainer 註解即可找到正確檔案

---

## 6. 已知 consumer 盤點（實作必 grep 補齊）

以下為 **非窮盡** 清單；實作 PR 前必須再跑一次全 repo `training_metrics.json` / `training_metrics.v2.json` / `comparison_metrics.json` / `feature_importance` grep。  
為避免遷移時遺漏，本清單分為四類：

### 6.1 Direct schema readers

- `trainer/core/bundle_run_contract.py`
- `trainer/serving/scorer.py`（contract 讀取偏好 `training_metrics.json`）
- `trainer/scripts/report_w2_objective_parity.py`
- `trainer/scripts/build_w1_freeze_evidence.py`
- `baseline_models/src/eval/reference_model.py`
- `baseline_models/src/baseline_config.py`

### 6.2 Filename / path convention users

- `investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/runner.py`

### 6.3 Artifact packagers / deploy readers

- `package/build_deploy_package.py`（bundle 檔案列表）
- 其他 deploy / package 相關腳本（實作前需再 grep 補齊）

### 6.4 Test fixtures / risk tests

- 既有 `tests/review_risks/*` 與 `tests/unit/*` 內含 metrics fixture 者

---

## 7. 與既有 Precision Uplift 計畫的連結

本變更會直接修改既有 precision uplift 路線中對 artifact 位置的描述，實作時必須同步對齊，避免文件互相衝突。

| 既有工作項 | 目前依賴 | 本計畫對應變更 |
| :--- | :--- | :--- |
| A1 | `training_metrics` 含 `selection_mode` / `optuna_hpo_*` 等契約欄位 | v2 在 Phase D 前保留頂層 denormalized contract keys；canonical field-test metric 改以 `field_test.precision` 表達 |
| A3 | `training_metrics["rated"]["gbm_bakeoff"]` | 改為 `comparison_metrics.json.families.gbm_bakeoff` |
| C1 | parity report 讀取 `training_metrics.json` | Phase B 改走 helper，優先讀 v2 |
| C2 | calibration / bundle contract 讀取 legacy metrics 路徑的風險 | Phase B 盤點並明確切換讀取入口 |
| C3 | `ensemble_bridge` 與 `gbm_bakeoff` 同域 | 納入 `comparison_metrics.json` 對應 family 區塊，不再回寫主 metrics |

---

## 8. 風險與緩解

| 風險 | 緩解 |
| :--- | :--- |
| 下游腳本大量依賴扁平鍵 | Phase A 雙寫 + Phase B helper fallback；對照表單一來源 |
| MLflow / logging 對非數值欄位敏感 | v2 主檔保持「可 log 的純量為主」；importance 永遠外置 |
| 舊 bundle 無 v2 | helper 明確回傳 `schema_version` 與 `source_path` |
| 命名漂移 | `training_metrics.v2.json` + `comparison_metrics.json` 於本輪先凍結；DECISION_LOG 一次性定案 |
| precision uplift 文件與 artifact 位置描述不一致 | 在同一實作 PR 內同步更新 `.cursor/plans` / `trainer/precision_improvement_plan` 對應敘述 |
| test 若仍為 sampled distribution，canonical field-test metric 可能不是未抽樣 test 的直接量測 | 本輪先把 field-test metric 命名與口徑標清；unsampled true-distribution test 留作後續 pipeline follow-up |

---

## 9. 驗收清單（Release gate）

1. 新訓練產物目錄含：`training_metrics.json`（legacy v1）、`training_metrics.v2.json`、`feature_importance.json`、`comparison_metrics.json`
2. `feature_list.json` **仍為** `[{name, track}]`，且無 importance 數值欄位
3. `bundle_run_contract` 在 v2 bundle 上仍可讀出 `selection_mode` / `selection_mode_source` / `production_neg_pos_ratio`
4. winner 與 field-test reporting 的 key 可由固定路徑讀取：
   - `comparison_metrics.json["families"][<family_name>]["winner_id"]`
   - `training_metrics.v2.json["datasets"]["test"]["field_test"]["precision"]`
   - `training_metrics.v2.json["datasets"]["test"]["field_test"]["precision_type"]`
5. comparison family 內 candidate `datasets.*` 與 `training_metrics.v2.json` 同名、可子集、不換名
6. `pytest` 相關子集綠（範圍由實作 PR 註記）
7. `DECISION_LOG.md` 有單一段落描述 **檔案契約與 EOL 策略**

---

## 10. 假設

- 本計畫 **不** 改 label 定義、不改 DEC-026 選阈演算法本身；僅調整 **artifact 形狀與寫入位置**。
- schema 為 **rated-only**；不引入 non-rated section。

---

## 11. 後續開放問題（不阻擋本輪實作）

1. 後續 pipeline follow-up 是否要強制：canonical `datasets.test.field_test.precision` 僅能在 unsampled true-distribution test 上產出；若無法滿足，需明確標示為 non-canonical。

---

## 12. 下一步（本檔後續維護原則）

本檔已以下列文件作為上游實作決策來源：`doc/training_metrics_v2_artifact_split_implementation_plan.md`。  
後續若需調整模組邊界、命名策略或 artifact 責任分界，應先更新 Implementation Plan，再回寫本檔的 task / rollout / DoD。
