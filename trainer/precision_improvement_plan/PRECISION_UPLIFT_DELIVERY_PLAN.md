# Precision Uplift Field-Test Objective 48H Full Delivery Execution Plan

> 文件層級：Working / Execution Plan  
> 目的：在不到 48 小時內，將 `trainer/precision_improvement_plan/PRECISION_UPLIFT_FIELD_TEST_OBJECTIVE_RECOMMENDED_ITEMS_ROI.md` 的 13 項建議全部推進到「已實作、可跑、可檢查」狀態。  
> 本檔立場：**實作優先，文件退後**。除非會阻塞整合，否則本輪不以 freeze / parity / decision-grade 工件作為前置條件。  
> 風險聲明：**此目標在單人或單機條件下屬極高風險排程**。若要在 48 小時內全做完，必須假設多工作流並行、快速決策、允許短期技術債，並以「先做進主路徑」優先於「先把證據鏈補齊」。

---

## 0. 工作項狀態總覽（對照程式碼）

**狀態圖例**

| 符號 | 意義 |
| :---: | :--- |
| **✅** | **已落地**：主路徑或腳本已達本節 DoD 之核心（可執行、可核對）。 |
| **🟡** | **部分**：有相關模組／註解／單點能力，但未達本節全文 DoD。 |
| **⬜** | **未落地**：repo 內無對應實作，或僅存在於計畫／註解。 |

**最後對照 repo 日期**：2026-04-26（以 `trainer/` 內 Python 與腳本為準；本輪重核 B1／R7 與 §0 表內過時敘述）。

**本輪修正**：B1 先前「`trainer.py` 無引用」已不符現況；`table_hc` 已在 Track Human 管線與 scorer 對稱計算，並見於 repo 候選 YAML（詳下表 B1）。

| 區塊 | 代碼 | 工作項（摘要） | 狀態 | 已做摘要（本 repo 實際落地） | 主要程式依據 |
| :--- | :--- | :--- | :---: | :--- | :--- |
| A | **A1** | R1 HPO / 選模對齊 field-test objective | **✅** | rated Optuna 在 gate 允許且 validation 有 payout span 時，trial 目標改為 DEC-026 下之 validation precision（可 prod-adjust）；precondition 擋路或無 span 時 **GATE BLOCKED**（無 AP fallback）；refit 與 HPO 共用 `FIELD_TEST_HPO_MIN_ALERTS_PER_HOUR` 與同一 payout span；`training_metrics` 含 `optuna_hpo_*`、`selection_mode` 等契約欄位。 | `trainer/training/trainer.py`：`run_optuna_search`、`pick_threshold_dec026`、`GATE BLOCKED`；`trainer/core/config.py`：`SELECTION_MODE` |
| A | **A2** | R2 排序導向 + HNM + top-band reweighting | **✅** | 四種 `--ranking-recipe`／環境變數 recipe；**預設**（CLI 與 env 皆未設）為 **`r2_top_band_light`**（DEC-044）；顯式 `--ranking-recipe baseline` 關閉 A2 風格加權；在 `compute_sample_weights` 之上對 rated 訓練列做 top-band／pseudo-HNM；`r2_hnm_light`／`combined` 於純 in-memory 路徑可多一次淺層 LGBM；`training_metrics` 與 **`model_metadata.json` → `training_params.ranking_recipe`** 寫入所用 recipe；Plan B CSV export 同步調權重；LibSVM 最終 on-disk 權重與 in-memory 可能不一致時 **WARNING**。 | `trainer/training/ranking_recipe_weights.py`；`--ranking-recipe`／`PRECISION_UPLIFT_RANKING_RECIPE`；`train_single_rated_model`／`train_dual_model`（rated）；shallow HNM 不適用 LibSVM／`train_from_file` 最終權重（見 log WARNING） |
| A | **A3** | R3 LGBM / CatBoost / XGBoost / soft-vote bakeoff | **🟡** | **預設**於 `run_pipeline` 啟用 bakeoff（可用 `--no-gbm-bakeoff` 關閉）：LightGBM 主訓練仍可走 LibSVM / `train_from_file` 省 RAM 路徑，但 A3 會額外在**同一 rated feature matrix、同一時間切分、同一評估 helper** 下比較 **LightGBM / CatBoost / XGBoost**，並把三者 **equal-weight soft-vote** 視為**第 4 個候選**一起評估；`winner_backend` 以 **field-test objective** 為第一排序鍵。四個候選的 validation / test 指標已寫入 `training_metrics["rated"]["gbm_bakeoff"]` 與相關 metadata，供後續審閱；`model.pkl` 契約已升級可保存單模型或 soft-vote winner。**尚未**把多窗穩定性 guardrail 內建到 A3 自動選勝，因此狀態維持 🟡。 | `trainer/training/gbm_bakeoff.py`；`trainer/training/trainer.py`；`trainer/training/trainer_argparse.py`；`trainer/serving/scorer.py`；`trainer/training/backtester.py` |
| A | **A4** | R4 二階段 Stage-1 + Stage-2 FP / reranker | **🟡** | 已落地 A4 MVP：Stage-1 先產生候選，Stage-2（LightGBM）僅在候選池訓練 FP detector，線上/離線以 **`product`** 融合（`p_final = p1 * p2`）；`model.pkl` 契約支援可選 `stage2_model` 與 `a4_*` metadata，並提供開關可回退 Stage-1 only。尚未完成進階 reranker 策略、多融合模式與大規模線上驗證。 | `trainer/training/trainer.py`（A4 訓練與 metrics）；`trainer/training/two_stage.py`；`trainer/serving/scorer.py`；`trainer/training/backtester.py`；`trainer/core/config.py` |
| B | **B1** | R7 `table_hc` 訓練／serving 主路徑 | **🟡** | **`add_track_human_features`** 與 **`build_features_for_scoring`** 已對稱寫入 **`table_hc`**（同源 **`compute_table_hc`**，缺 `table_id`／`player_id` 等欄時為 0 並 WARNING）。**`trainer/feature_spec/features_candidates.yaml`** 已登記 **`table_hc`**（`screening_eligible: true`），可經 Step 5b screening 進 Step 9。**仍標 🟡**：模型是否實際吃到該欄取決於 screening／active 結果；**`package/deploy/models/feature_spec.yaml`** 範本之 `track_human.candidates` 尚未逐字含 `table_hc`，與「僅依 repo 候選預設訓練」路徑可能不一致，bundle 凍結 spec 時需留意。 | `trainer/training/trainer.py`：`add_track_human_features`；`trainer/serving/scorer.py`；`trainer/features/features.py`：`compute_table_hc`／`compute_table_hc_features`；`trainer/feature_spec/features_candidates.yaml` |
| B | **B2** | R8 Track LLM／Human 候選擴張 | **🟡** | 候選 YAML + screening／訓練管線已存在；ROI 表所列「逐項擴張是否全完成」尚未在此檔逐欄核銷。 | `trainer/feature_spec/features_candidates.yaml` + screening 管線存在；本計畫所列「擴張清單是否全數入欄」未在此單次對照中逐欄核完 |
| B | **B3** | R9 D3 PIT-correct identity mapping | **🟡** | 程式與註解已標示 D3／Phase 2 PIT 方向；預設仍為 cutoff／整窗 mapping，未切換為 PIT-correct 主路徑。 | `trainer/identity.py` 註明 Phase 2 PIT-correct；預設仍為整窗／cutoff mapping 路徑 |
| B | **B4** | R11 Profile history-depth bundle | **⬜** | 尚未依 history depth／完整度分 bundle 並分流特徵或模型。 | 無依 history depth 分 bundle 之訓練／推論路由實作 |
| B | **B5** | R5 離線序列 embedding → GBDT | **⬜** | 尚未建立離線 embedding 產線與 join 回訓練特徵。 | 無 embedding 產線與 join 回主特徵矩陣之實作 |
| B | **B6** | R6 分群 + learned gating | **⬜** | 尚未實作 multi-expert + gate 訓練與推論。 | 無 multi-expert + gate 訓練／推論 |
| C | **C1** | R10 真多窗矩陣 + gate 報表 | **🟡** | 已有多 run 彙總腳本（CSV/MD）與固定調查窗 train+backtest 腳本；報表內**自動 gate 規則**（最差窗／閾值）尚未內建。 | `trainer/scripts/report_w2_objective_parity.py`（多 run CSV/MD）；`trainer/scripts/run_train_backtest_investigation_windows.py`（固定調查窗）；**自動 gate（最差窗／閾值規則）**未內建於報表腳本 |
| C | **C2** | R12 DEC-032 校準閉環 | **🟡** | 已有批次校準 CLI、DB 寫入與 `selection_mode` 等契約；排程化與長期監控仍缺。 | `trainer/scripts/calibrate_threshold_from_prediction_log.py`：`--run-batch-calibration` 等；排程化／長期監控仍屬產品化缺口 |
| C | **C3** | R13 Stacking / blending / ensemble | **🟡** | A3 已寫入 `gbm_bakeoff.ensemble_bridge` 與 `per_backend` 指標契約；**尚未** OOF 匯出、meta-learner 訓練或 scorer 線上 blend。 | `gbm_bakeoff.py` 之 `ensemble_bridge`；C3 全線仍待實作 |
| C | **C4** | 整體整合 summary | **⬜** | 尚未產出 13 項一覽之 `full_delivery_summary` 類工件。 | 未見 `out/precision_uplift_full_delivery/full_delivery_summary.*` 或等價彙總產物 |

---

## 1. 執行原則

- **先實作，再補治理**：本輪不把 precondition JSON、freeze evidence、多窗 decision-grade 報告當成實作 blocker。
- **所有項目都要落地到程式或可執行腳本**：不接受只更新文件、只寫 stub、只留下 TODO。
- **直接走既有主路徑**：優先改 `trainer/training/trainer.py`、`trainer/training/backtester.py`、`trainer/serving/scorer.py`、`trainer/features/*`、`trainer/scripts/*`，避免另起平行框架。
- **固定單一主契約**：`selection_mode=field_test`、`min_alerts_per_hour >= 50`、`prod_adjusted precision` 優先。
- **資源保護**：所有實作先以小窗 / 小 trials / 序列化驗證；避免在筆電上同時開多個重訓練工作導致 OOM 或超長 runtime。
- **輸出最小化**：每一項只要求最少可核對產物，例如程式改動、run 產物、簡短摘要；不要求長篇分析。

---

## 2. 成功定義

本輪「交付完成」定義如下：

1. 13 項建議全部在 repo 中有對應實作落點，而不是僅停留在計畫層。
2. 每一項至少有一個可執行入口、配置開關、或主路徑接線。
3. 所有新增能力能與既有 `trainer` / `backtester` / `scorer` 契約共存。
4. 至少完成一次整體整合跑，能產出可閱讀結果。
5. 文檔只補最少必要說明，不再擴張成新的治理主線。

---

## 3. 非現實之處與強制前提

這份計畫只有在下列前提成立時才可能落地：

- **至少 3 條並行工作流** 同時開工：
  - 工作流 A：訓練目標 / 排序訓練 / 模型 bakeoff / 二階段
  - 工作流 B：特徵 / embedding / gating / identity / bundle
  - 工作流 C：治理 / 校準 / 多窗 / ensemble / 整合
- 允許本輪先做 **MVP-full implementation**：
  - 功能完整接上
  - 配置可開關
  - 有最少 smoke 驗證
  - 不要求每一項都做到 production-hardened
- 允許短期技術債：
  - 報表與 runbook 後補
  - 多窗報告先求可跑，不求精美
  - 線上監控先用最小可用方案

若以上前提不成立，**「48 小時內全項目 full delivery」是不切實際的**。本檔不是在粉飾風險，而是在極限條件下給出最直接的執行序列。

---

## 4. 48 小時總體策略

### 4.1 三大波次

1. **Wave 1：先把所有高 ROI 改動做進主路徑**
   - R1, R2, R3, R7, R8
2. **Wave 2：補齊高複雜度但不可缺項目**
   - R4, R5, R6, R9, R11
3. **Wave 3：補上守門與產品化項目**
   - R10, R12, R13

### 4.2 強制規則

- 不得先寫新計畫再做實作。
- 不得因缺少 freeze / parity / precondition 文件而停止主實作。
- 不得把「研究結論不足」誤當作「不需要實作」。
- 不得同時大窗跑所有路線；先 smoke，再擴大。

---

## 5. 工作分流

### 5.1 工作流 A：訓練主幹

負責項目：R1、R2、R3、R4

#### A1. R1 HPO / 模型選擇目標對齊 field-test objective

**Status（對照 repo）：✅ 已落地**

- 實作內容：
  - 確認 `run_optuna_search()`、winner-pick、refit 後驗證選點都以 `field_test` 為主契約。
  - 移除任何主路徑對 AP fallback 的依賴。
  - 確保 `training_metrics` / `backtest_metrics` 都有 `selection_mode` 與 field-test 指標。
- DoD：
  - 單指令訓練可直接跑 field-test objective。
  - 無 precondition JSON 時，仍可跑 exploratory training。
  - 不可行時明確 `GATE BLOCKED`。

#### A2. R2 排序導向訓練 + Hard Negative + Top-band Reweighting

**Status（對照 repo）：✅ 已落地**（LibSVM／Plan B 僅 CSV 匯出權重與 in-memory Optuna 可能不完全一致，見 trainer WARNING）

- 實作內容：
  - 在現有 `sample_weight` 主路徑加入 top-band reweighting。
  - 加入 hard-negative mining 的最小迭代流程。
  - 保留 recipe/config 切換，不把策略硬寫死。
  - **預設 recipe（DEC-044）**：未指定 `--ranking-recipe` 且未設定 `PRECISION_UPLIFT_RANKING_RECIPE` 時，使用 **`r2_top_band_light`**；需純 DEC-013 基底權重時請 **`--ranking-recipe baseline`**。
  - **Artifact**：`training_metrics.json`（rated 區塊）含 `ranking_recipe`／`ranking_recipe_*`；`model_metadata.json` 的 `training_params.ranking_recipe` 與之一致，供 bundle 審計。
- DoD：
  - baseline、reweight、hard-negative、combined 至少四種 recipe 可跑。
  - metrics 可與 baseline 並列比較。
  - 不出現 silent resource blow-up。

#### A3. R3 LightGBM / CatBoost / XGBoost / soft-vote 公平 bakeoff

**Status（對照 repo）：🟡 部分落地**（四候選預設比較、winner artifact 與 bundle 契約升級已落地；多窗穩定性 guardrail 尚未內建）

- 實作內容：
  - `run_pipeline` **預設**啟用 A3；如需停用須顯式指定 **`--no-gbm-bakeoff`**。
  - LightGBM 主訓練仍可走 **LibSVM / `train_from_file`** 省 RAM 路徑；A3 另外從相同 split 讀出 rated 矩陣，比較 **LightGBM / CatBoost / XGBoost**，並將三者 prediction score 的 **equal-weight average** 作為 **第 4 個候選 soft-vote** 一起評估。
  - `winner_backend` 改以 validation **field-test primary score**（可 prod-adjust）為第一排序鍵，不再以 AP 當主鍵；`selected_backend` / `model_backend` 寫入 metrics 與 metadata。
  - **四個候選**（LGBM、CatBoost、XGBoost、soft-vote）的 validation / test 評估結果都要落到 `training_metrics["rated"]["gbm_bakeoff"]` 與相關 metadata，供之後 review / 回溯比較。
  - `model.pkl` 契約已升級：winner 可為單模型或 **soft-vote wrapper**，`scorer` / `backtester` 以同一 bundle 介面載入。
  - CatBoost / XGBoost 的 train metrics 也改走**分批 predict**，避免因 train metrics 自己造成 RAM 暴衝。
  - 依賴已納入 **`requirements.txt`**（`catboost==1.2.10`、`xgboost==3.2.1`）；import 失敗或訓練失敗之 backend 標為 **reject**（非致命）。
  - **C3 銜接**：`gbm_bakeoff.ensemble_bridge` 記錄同欄序與列數，供後續 stacking／blend 擴充（**不含** OOF 匯出或 meta-learner；見 §C3）。
- DoD：
  - 三個單模型加上一個 **equal-weight soft-vote** 候選都能在同一實驗框架下跑。
  - 四個候選的結果可直接對照，且評估結果完整寫入 metadata。
  - 單模型或 soft-vote winner 都可直接成為最終 artifact（同一 `model.pkl` 契約）。
  - 有明確 winner/hold/reject 欄位。
  - 多窗穩定性 guardrail 仍待 C1 / 後續治理接線。

#### A4. R4 二階段模型

**Status（對照 repo）：🟡 A4 MVP 已落地（product 融合）**

- 實作內容：
  - Stage-1 生成候選高分樣本。
  - Stage-2 僅在候選集上訓練 reranker / FP detector。
  - 離線推論與評估走完整鏈路（含 backtester parity）。
  - `scorer` 支援兩階段載入與分數組合（MVP 固定 `product`），保留 feature parity / artifact 路徑。
  - `model.pkl` 支援可選 `stage2_model`、`a4_enabled`、`a4_fusion_mode`、`a4_candidate_cutoff`、`stage2_features`。
  - 以 config 開關提供 rollback：可直接回到 Stage-1 only 推論。
- DoD：
  - 二階段訓練與離線評估能跑通（MVP 已達成）。
  - 可與最佳單階模型公平比較（MVP 已寫入 A4 指標）。
  - 有 rollback 開關，不影響單階基線可用性（MVP 已達成）。
  - 後續待補：除 `product` 外的融合策略、Stage-2 專屬特徵工程、線上長窗穩定性驗證。

### 5.2 工作流 B：特徵與資料語意

負責項目：R5、R6、R7、R8、R9、R11

#### B1. R7 `table_hc` 與桌況特徵接線

**Status（對照 repo）：🟡 部分**（Track Human／scorer 已對稱產出 `table_hc`；repo 候選 YAML 已註冊；**預設是否進最終欄位清單**與 **deploy 範本 spec 是否含候選** 仍待收斂）

- 實作內容：
  - 將 `compute_table_hc` 正式接入訓練與 serving 主路徑（**已**：`add_track_human_features`／scorer 對稱路徑）。
  - 更新 feature spec 與必要的 screening（**已**：`features_candidates.yaml`；**待**：deploy 範本與各 bundle 凍結 spec 與訓練預設一致化）。
- DoD：
  - `trainer` / `scorer` 都能用同一欄位。
  - 無 train-serve parity 斷裂。

#### B2. R8 擴張 Track LLM / Human 候選特徵

**Status（對照 repo）：🟡 部分**（候選 YAML + screening 管線存在；本節所列擴張是否全部完成需另列欄位核對）

- 實作內容：
  - 在 `features_candidates.yaml` 加入新候選。
  - 讓既有計算與 screening 流程真正吃到這些欄位。
- DoD：
  - 新候選可被訓練流程看見。
  - 至少一輪 run 使用新候選實際訓練。

#### B3. R9 D3 identity PIT-correct mapping

**Status（對照 repo）：🟡 部分**（文件化 D3／PIT 方向；預設 mapping 行為未切換為 PIT-correct 主路徑）

- 實作內容：
  - 將整窗 mapping 改為 PIT-correct 或 chunk-end / rolling 版本。
  - 更新快取與 trainer / scorer 取用點。
- DoD：
  - 新 mapping 能實際產出並被主路徑使用。
  - 舊 mapping 不再是唯一預設。

#### B4. R11 Profile history-depth bundle

**Status（對照 repo）：⬜ 未落地**

- 實作內容：
  - 依 history depth / completeness 分 bundle。
  - 為各 bundle 配不同特徵子集或模型路徑。
- DoD：
  - bundle 規則存在於實作，不只是文件。
  - 訓練或推論至少能辨識並套用 bundle。

#### B5. R5 離線序列 embedding

**Status（對照 repo）：⬜ 未落地**

- 實作內容：
  - 建立離線 embedding 產生流程。
  - 將 embedding join 回訓練資料並註冊至 feature pipeline。
- DoD：
  - embedding 可生成、可 join、可進模型。
  - 缺 embedding 時有明確 fallback。

#### B6. R6 分群建模 + learned gating

**Status（對照 repo）：⬜ 未落地**

- 實作內容：
  - 建立 2~4 experts 與 gate。
  - 在訓練與推論路徑提供 gated inference。
- DoD：
  - expert + gate 可訓練、可推論、可評估。
  - 不只是切片分析，是真的路由到不同模型。

### 5.3 工作流 C：治理、校準、整合

負責項目：R10、R12、R13，並負責整體收斂。

#### C1. R10 真多窗矩陣 + gate

**Status（對照 repo）：🟡 部分**（多 run 彙總與固定調查窗腳本；自動 gate 規則未內建於報表）

- 實作內容：
  - 建立多窗訓練/回測 orchestrator。
  - 輸出每窗核心指標、均值、最差窗、波動。
  - 把 gate 判斷直接寫入報表，不等待人工整理。
- DoD：
  - 可一鍵跑多窗矩陣。
  - 至少有 CSV 或 JSON 匯總。

#### C2. R12 DEC-032 線上校準閉環

**Status（對照 repo）：🟡 部分**（批次校準 CLI 與 DB 寫入路徑存在；「閉環」之排程／監控仍視為產品化缺口）

- 實作內容：
  - `prediction_log` 成熟標籤回流。
  - `runtime_rated_threshold` field-test mode 重估。
  - `calibration_runs` 寫入與最小批次化。
- DoD：
  - 校準腳本可直接跑批次。
  - 可把 selection mode 與新 threshold 寫回狀態儲存。

#### C3. R13 Stacking / Blending / Ensemble

**Status（對照 repo）：🟡 部分**（A3 已寫入 `ensemble_bridge` 與多後端指標；**尚未** OOF／meta-model／線上 blend）

- 實作內容：
  - 產生 OOF 或時間切分預測。
  - 訓練 meta-model 或 blend 邏輯。
  - 接上推論與 artifact。
- **A3 已提供之前置**：`training_metrics["rated"]["gbm_bakeoff"]["ensemble_bridge"]`（`feature_columns`、`train_rows`／`valid_rows`／`test_rows`、`same_splits`）；`per_backend` 內含各後端 val／test 指標與 `bakeoff_disposition`，可作 C3 實作時對齊欄位與列數之契約。
- DoD：
  - ensemble 可訓練、可推論、可評估。
  - 不只是 notebook 內手工融合。

#### C4. 整體整合

**Status（對照 repo）：⬜ 未落地**

- 實作內容：
  - 把 A/B 工作流的結果匯入同一個比較入口。
  - 產出最少交付摘要。
- DoD：
  - 有一份最終 summary，能回答 13 項是否都已落地。

---

## 6. 48 小時時間盒

### T+0 ~ T+4 小時：主路徑先打通

- A1 完成並確認 `trainer` 可直接跑 field-test objective。
- B1 / B2 同時開工：`table_hc` 已進 Track Human／scorer 計算與 repo 候選 YAML；續追 screening 入選與 deploy／bundle spec 一致。
- C1 建立多窗 orchestrator 骨架，先能吃 baseline。

### T+4 ~ T+12 小時：第一波高 ROI 全數進場

- A2 完成 ranking-focused recipes。
- A3：預設四候選對照（LGBM / CatBoost / XGBoost / soft-vote）+ `gbm_bakeoff.py`（winner 直接成為 `model.pkl`；可用 `--no-gbm-bakeoff` 關閉）。
- B3 開始替換 D3 mapping。
- B4 加入 history-depth bundle。

### T+12 ~ T+24 小時：高複雜度項目落地

- A4 二階段模型 PoC 全鏈路。
- B5 embedding 產生 + join。
- B6 experts + gate 路由。
- C2 校準批次打通。

### T+24 ~ T+36 小時：整合與補齊

- C1 多窗矩陣正式跑。
- C3 ensemble 建好並接線（A3 已留 `ensemble_bridge` 契約，全線仍待實作）。
- A/B/C 共同修掉整合斷點。

### T+36 ~ T+48 小時：收口與交付

- 跑至少一次端到端整合。
- 產出最少交付摘要：
  - 實作項目
  - 對應 run
  - 核心指標
  - 已知風險

---

## 7. 每項的最小交付格式

每一項只需要以下最少資訊，不再要求長篇文件：

- `item_id`
- `code_path`
- `config_or_flag`
- `artifact_or_run_id`
- `status`
- `known_risk`

建議彙總成：

- `out/precision_uplift_full_delivery/full_delivery_summary.json`
- `trainer/precision_improvement_plan/full_delivery_summary.md`

---

## 8. 明確禁止事項

- 不得先花時間補新的 freeze evidence、decision-grade 報告、長篇 markdown，然後才開始實作。
- 不得因單一項目尚未拿到漂亮 uplift 就停止其實作落地。
- 不得把「還沒完整驗證」誤寫成「先不做」。
- 不得同時在同一台筆電開大量高 RAM 訓練進程。
- 不得把 ensemble 或二階段當成純分析項；若列入本輪，就必須真的做進程式。

---

## 9. 風險與止損

- **最大風險不是理論不足，而是時間與計算資源不足。**
- `R3 + R5 + R6 + R13` 同時推進時，最容易觸發 OOM、依賴衝突與 runtime 爆炸。
- `R9` 若改動 identity 主幹，可能波及訓練與 serving 一致性，必須優先 smoke。
- `R4`、`R6`、`R13` 若 artifact 契約沒有先定住，最後整合最容易失敗。

止損規則只有一條：

- **不砍項，但允許先交 MVP-full implementation，再把 production hardening 留在交付後 24~72 小時內補齊。**

---

## 10. 本檔與既有文件的關係

- 本檔取代「先證明、後實作」的節奏。
- `PRECISION_UPLIFT_FIELD_TEST_OBJECTIVE_IMPLEMENTATION_PLAN.md` 仍保留作上游策略文件。
- `.cursor/plans/EXECUTION PLAN - Precision Uplift.md` 仍保留作既有執行紀錄。
- 本檔是本輪衝刺用的 **硬推進版本**，若與上游文件衝突，**本輪以交付優先**，之後再回補一致性。

---

## 11. 交付結論

這不是低風險計畫，而是極限排程下的強攻計畫。

若要在不到 48 小時內把 13 項全部做到「已實作、可跑、可檢查」，唯一可行方法是：

1. 直接把所有建議拆成並行工作流。
2. 停止把治理工件當主線 blocker。
3. 接受本輪先交 **MVP-full implementation**，而不是 production-perfect implementation。
4. 以端到端整合成功，取代長篇證明文件，作為本輪第一優先。
