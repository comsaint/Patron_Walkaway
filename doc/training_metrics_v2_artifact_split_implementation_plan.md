# `training_metrics` v2 Artifact Split Implementation Plan

> 文件層級：**Implementation Plan**。  
> 目的：定義 `training_metrics` v2 artifact 拆分的**模組邊界、命名策略、版本遷移、consumer 對齊與 validation 策略**；不展開 ticket 級 task breakdown。  
> 下游執行檔：`.cursor/plans/EXECUTION PLAN - training_metrics_v2_artifact_split.md`。  
> 對齊背景：`trainer/training/trainer.py` 現有 bundle 寫入契約、`trainer/core/bundle_run_contract.py` 的 run-contract 讀取、以及 precision uplift 路線中 A1 / A3 / C1 / C2 / C3 對 `training_metrics.json` 的既有依賴。
> 本輪範圍限制：**只調整 metric report object / artifact 契約，不改 negative sampling、split、或 test 真實分布管線行為**；後者另列為後續 pipeline follow-up。

---

## 1. 目標與範圍

### 1.1 目標

本計畫要把目前混合在 `training_metrics.json` 的四類責任拆開：

1. **核心模型評估與選模摘要**
2. **feature importance / explainability 輸出**
3. **候選模型或候選 family 比較結果**
4. **run contract 與 artifact manifest**

拆分後，artifact 必須滿足：

- **可讀**：train / val / test 不再以鬆散扁平鍵混雜表示
- **可比較**：selection 指標與 held-out reporting 指標明確分離
- **可演進**：允許新增多個 comparison family，而不造成檔名爆炸
- **可相容**：在 Phase A/B 不破壞既有 `training_metrics.json` consumer

### 1.2 非目標

- 不改 label 定義
- 不改 DEC-026 選阈演算法
- 不改 scorer 線上決策邏輯
- 不在本輪改 train / valid / test split 流程
- 不在本輪改 negative sampling 對 test distribution 的影響
- 不在本輪重寫全部歷史 bundle
- 不把 `feature_list.json` 擴充成 explainability 檔

---

## 2. 凍結決策

### 2.1 檔名與版本策略

本計畫採以下凍結策略：

- **Phase A/B**
  - 保留既有 `training_metrics.json` 作為 **legacy v1**
  - 新增 `training_metrics.v2.json` 作為 **v2 主候選格式**
- **Phase C/D**
  - 待 reader 遷移完成後，再評估是否將 `training_metrics.json` 切換為 v2
  - 若需要保留 v1，改為 `training_metrics.legacy.json`

此策略的理由：

- `trainer/core/bundle_run_contract.py` 目前直接讀 `training_metrics.json` 頂層 `selection_mode`
- `investigations/.../collectors.py` 目前明確以 `training_metrics.json` 作為路徑與 bundle hint
- `package/build_deploy_package.py` 目前固定把 `training_metrics.json` 視為 bundle 檔案之一

因此，**先雙寫、後切換** 是風險最低的遷移方式。

本輪亦**明確不採**「Phase A/B 直接沿用 `training_metrics.json`，僅靠檔內 `schema_version` 區分 v1/v2」的方案；避免：

- 既有 reader 誤把 v2 當 legacy v1 讀取
- grep / triage / deploy 審核時無法從檔名直接判斷 artifact 世代
- dual-write 過渡期的 bundle contract 變得不透明

### 2.1.1 Phase A deploy bundle 決策

Phase A 起，下列新檔案一律視為 **required bundle artifacts**：

- `training_metrics.v2.json`
- `feature_importance.json`
- `comparison_metrics.json`

缺任一檔案，視為 bundle contract violation，而非 optional omission。

### 2.2 Comparison artifact 策略

候選比較結果採 **單一總檔 + family registry**，不採：

- 單一泛稱但只容納一個 family 的 `candidate_metrics.json`
- 每個 family 一個獨立檔案的爆量策略

凍結檔名：

- `comparison_metrics.json`

凍結內容形狀：

- 單一檔案
- 頂層 `families`
- 每個 family 自帶：
  - `comparison_family`
  - `selection_rule`
  - `winner_id`
  - `candidates`

candidate `datasets.*` 的結構規則凍結如下：

- 與 `training_metrics.v2.json` 採 **同名 schema**
- **允許子集**
- **不允許換名**

也就是說，comparison family 內 candidate 可少部分欄位，但相同語意的欄位名稱必須與 `training_metrics.v2.json` 保持一致。

### 2.3 Feature importance 策略

- `feature_importance` 自 `training_metrics` 主檔外移
- 專屬檔名：`feature_importance.json`
- `feature_list.json` 維持為 `[{name, track}]` 的 **train-serve parity / feature catalog 契約**
- `model_metadata.json` 僅新增 artifact pointers，不承載長陣列

### 2.4 Run-contract 相容策略

在 **Phase D 前**，v2 仍保留頂層 denormalized contract keys：

- `selection_mode`
- `selection_mode_source`
- `production_neg_pos_ratio`

理由：

- `trainer/core/bundle_run_contract.py` 目前就是以這組頂層鍵作為 artifact-side SSOT
- 這三個欄位本質上是 **bundle-level contract**，保留在 v2 頂層有語意合理性，不只是向後相容 hack

### 2.5 Metric naming 與 canonical metric 策略

canonical field-test metric 直接使用**人類可讀命名**，避免 `primary_score` / `proxy_score` / `mode` 這類抽象術語。

凍結命名：

- `datasets.val.field_test.precision`
- `datasets.val.field_test.precision_type`
- `datasets.test.field_test.precision`
- `datasets.test.field_test.precision_type`

其中：

- `precision` = 該 operating point 下的 precision 數值本身
- `precision_type` = precision 的口徑；本輪只允許：
  - `raw`
  - `prod_adjusted`

語意凍結：

- `datasets.val.field_test.precision` 與 `datasets.test.field_test.precision` 都是 **field-test metric**
- 其定義是：**在 `alerts_per_hour >= 50` 可行條件下的 precision**
- 舊的 `precision at 1% recall` 不再視為 canonical metric，只保留為 convenience / reference metric

selection 相關 metadata 若需記錄「本次用哪種 metric 選模」，應以**直白命名**表達，例如：

- `selection_metric: "field_test_precision"`

本輪**不**再使用 `primary_metric` 作為 schema 名稱。

本輪**不**引入下列命名：

- `primary_score`
- `primary_score_mode`
- `proxy_score`
- `proxy_score_mode`

---

## 3. 目標 artifact 版圖

### 3.1 `training_metrics.v2.json`

責任：

- 最終勝出模型的核心 metrics
- selection 摘要
- train / val / test datasets 巢狀指標
- artifact pointers
- 頂層 run contract denormalized keys（過渡期）

不承載：

- 完整 feature importance list
- 多 family 候選比較大表

對 field-test metric 的最低要求：

- `datasets.val.field_test.precision`
- `datasets.val.field_test.precision_type`
- `datasets.test.field_test.precision`
- `datasets.test.field_test.precision_type`

其中 `datasets.test.field_test.*` 為 winner 在 held-out test 上的 canonical field-test reporting 位置。

本計畫將 `datasets.val.field_test.*` 視為 validation-side canonical field-test metric 位置；若另有 selection metadata，應視為 mirror / convenience metadata，不得以不同命名重複表達同一語意。

`precision at 1% recall` 類指標應改以直白 convenience 命名表達，例如：

- `datasets.test.recall_0_01.precision`
- `datasets.test.recall_0_01.precision_prod_adjusted`

而不得再與 canonical field-test metric 混名。

### 3.2 `feature_importance.json`

責任：

- winner 模型 importance 輸出
- `importance_method`
- `items`
- 可選 summary

### 3.3 `comparison_metrics.json`

責任：

- 所有 comparison family 的統一容器

建議形狀：

```json
{
  "schema_version": "comparison-metrics.v1",
  "model_version": "<model_version>",
  "families": {
    "gbm_bakeoff": {
      "comparison_family": "gbm_bakeoff",
      "selection_rule": "...",
      "winner_id": "xgboost",
      "candidates": {
        "lightgbm": {},
        "catboost": {},
        "xgboost": {},
        "soft_vote_equal": {}
      }
    }
  }
}
```

### 3.4 `model_metadata.json`

新增：

- `artifacts.training_metrics_v2_path`
- `artifacts.feature_importance_path`
- `artifacts.comparison_metrics_path`

可選保留：

- `artifacts.training_metrics_legacy_path`

---

## 4. 模組邊界與責任

### 4.1 Writer 側

**主責模組**

- `trainer/training/trainer.py`
- `trainer/training/gbm_bakeoff.py`
- 相關 artifact bundle save logic

**責任**

- 生成 legacy v1 `training_metrics.json`
- 生成 `training_metrics.v2.json`
- 生成 `feature_importance.json`
- 生成 `comparison_metrics.json`
- 將 artifact pointers 寫入 `model_metadata.json`

**本輪限制**

- 只改 artifact / metric report object
- 不改 Step 6 negative sampling 與 Step 7 split 的實際行為

### 4.2 Reader 側

**主責模組**

- 新增或擴充 `trainer/core/` 下的統一 loader helper

**責任**

- 提供單一入口讀 bundle metrics
- 優先讀 `training_metrics.v2.json`
- fallback 到 `training_metrics.json`
- 對 comparison / importance 路徑做一致解析

### 4.3 Contract reader

**關鍵模組**

- `trainer/core/bundle_run_contract.py`

**決策**

- Phase A/B 先維持可從 legacy v1 讀取
- 新 helper 導入後，改為：
  - 先嘗試 v2
  - 失敗則 fallback v1

### 4.4 Packaging / deploy

**關鍵模組**

- `package/build_deploy_package.py`

**決策**

- Phase A/B 起，deploy bundle **必帶**：
  - `training_metrics.v2.json`
  - `feature_importance.json`
  - `comparison_metrics.json`

理由：deploy / 調查 / 支援性審核時，應能讀到完整新契約；避免部署包只帶 legacy 造成後續雙軌調試。

---

## 5. Consumer 分級與遷移策略

### 5.1 Consumer 分級

#### A. Direct schema readers

最容易被 schema 變動打斷：

- `trainer/core/bundle_run_contract.py`
- `baseline_models/src/eval/reference_model.py`
- `baseline_models/src/baseline_config.py`
- `trainer/scripts/report_w2_objective_parity.py`
- `trainer/scripts/build_w1_freeze_evidence.py`

#### B. Filename / path convention users

最容易被檔名變動打斷：

- `investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/runner.py`

#### C. Artifact packagers / deploy readers

- `package/build_deploy_package.py`
- `trainer/serving/scorer.py`（若載入或審計仍偏好 `training_metrics.json`）

#### D. Test fixtures / review risks

- `tests/unit/*`
- `tests/review_risks/*`

### 5.2 遷移原則

- A 類：優先改成走 helper
- B 類：先維持 legacy 預設檔名存在，待 Phase C/D 再改預設
- C 類：先擴充 bundle contents，再切 reader
- D 類：用雙格式 fixture 覆蓋 fallback 行為

---

## 6. 與既有 Precision Uplift 計畫的對齊

本計畫不是孤立的 artifact refactor；它直接影響既有 precision uplift execution / reporting 契約。

### 6.1 A1（field-test objective）

目前 `PRECISION_UPLIFT_DELIVERY_PLAN.md` A1 已把：

- `selection_mode`
- `optuna_hpo_*`
- `training_metrics`

視為 artifact 契約的一部分。

因此本計畫要求：

- `selection_mode` 在 Phase D 前於 v2 頂層仍可直接讀取
- field-test canonical metric 在 v2 中有穩定位置，且名稱直接為 `field_test.precision`
- `precision@recall=1%` 僅作 convenience / reference metric，不再以 `primary` 類命名表達

### 6.2 A3（GBM bakeoff）

目前 A3 明確依賴：

- `training_metrics["rated"]["gbm_bakeoff"]`

本計畫將其重定位為：

- `comparison_metrics.json.families.gbm_bakeoff`

因此 A3 文件、reporting 腳本與後續審閱邏輯必須同步更新，不可讓兩份計畫對 artifact 位置描述互相矛盾。

### 6.3 C1（parity / report）

`report_w2_objective_parity.py` 屬於高風險 reader，必須在 Phase B 一併切到 helper。

### 6.4 C2（calibration）

若 calibration 流程依賴 bundle contract 或 metrics 路徑，需確認其讀取來源是否仍為 `training_metrics.json`；若是，Phase B 必須同步納入 helper。

### 6.5 C3（ensemble / comparison bridge）

既有 `ensemble_bridge` 與 `gbm_bakeoff` 關係密切。  
本 Implementation Plan 建議：

- `ensemble_bridge` 保留在 `comparison_metrics.json` 對應 family 區塊內
- 不再散落回主 `training_metrics.v2.json`

---

## 7. 驗證與 rollout

### 7.1 Phase A

- dual write：
  - `training_metrics.json`（legacy v1）
  - `training_metrics.v2.json`
  - `feature_importance.json`
  - `comparison_metrics.json`
- `model_metadata.json` 帶 pointers
- Phase A deploy bundle 視上述三個新檔為 required

### 7.2 Phase B

- 導入 helper
- reader 改優先讀 v2
- 重要腳本與 baseline tooling 遷移

### 7.3 Phase C

- legacy v1 瘦身
- 明確標示 deprecation

### 7.4 Phase D

- `training_metrics.json` 切換為 v2
- legacy 改名或移除

---

## 8. 風險與緩解

### 8.1 主要風險

- reader 直接抓扁平鍵，造成靜默錯讀
- 路徑與檔名硬編碼，導致 investigations / packaging 斷裂
- precision uplift 文件與新 artifact 位置描述不一致
- comparison family 愈來愈多時，單檔 schema 再次膨脹
- test set 若仍沿用 sampled distribution，canonical field-test metric 可能不是「真實未抽樣 test」下的直接量測

### 8.2 緩解策略

- helper 單一入口
- dual write 過渡
- `comparison_metrics.json` 採 `families` registry，而非平鋪欄位
- 在 `DECISION_LOG.md` 一次性定案命名與版本策略
- 本輪先把 field-test metric 的命名與口徑標示清楚；test 必須 unsampled true distribution 的要求留作後續 pipeline follow-up

---

## 9. 成功定義

1. 新 bundle 可同時輸出：
   - `training_metrics.json`
   - `training_metrics.v2.json`
   - `feature_importance.json`
   - `comparison_metrics.json`
2. `bundle_run_contract` 能在新 bundle 上穩定讀取 contract
3. A3 / C1 / baseline_models / investigations 等高風險 consumer 已導向 helper 或完成 fallback
4. `feature_list.json` 維持純 feature catalog，不混入 importance
5. precision uplift 相關計畫文件對 artifact 位置的描述無互相衝突
6. canonical field-test metric 直接以 `field_test.precision` 命名，無 `primary_score` / `proxy_score` 類術語殘留
7. comparison family 內 candidate `datasets.*` 與 `training_metrics.v2.json` 同名、可子集、不換名

---

## 10. 開放問題

1. 後續 pipeline follow-up 是否要強制：canonical `datasets.test.field_test.precision` 僅能在 unsampled true-distribution test 上產出；若無法滿足，需明確標示為 non-canonical

