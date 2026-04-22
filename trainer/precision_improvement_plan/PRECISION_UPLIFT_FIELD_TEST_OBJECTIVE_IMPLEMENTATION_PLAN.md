# Precision Uplift Field-Test Objective Implementation Plan（ROI 優先，動態重排版）

> 文件層級：Implementation Plan（實現策略層）  
> 目的：定義「如何落地」precision uplift 路線，並以 ROI 由高到低安排優先順序；R1 起始 operating objective 以 field test 可觀測指標為主，並將 objective contract 收斂成可執行版本。  
> 邊界：本檔不寫 ticket 級執行清單（那是 execution plan），不重寫 SSOT 需求。  
> 更新原則：**允許每輪證據後動態重排**（見第 8 節）。

---

## 1) 目標與範圍

- 主目標：在既有評估契約下，將 **`precision@recall=1%`** 由約 0.4x 持續提升並逼近/達成 `>=0.6`；同時在實作上，模型選擇與 operating point 優先對齊 field-test objective。
- 目標屬性：硬指標、尾段排序導向（top-band quality first）。
- 範圍包含：
  - 訓練目標對齊、排序導向訓練、模型家族比較、二階段架構。
  - 特徵擴張（含 `table_hc`、Track 候選擴張、離線 embedding）。
  - 資料語意修復（D3 PIT-correct）、多窗治理、線上校準閉環、集成策略。
- 非範圍：
  - 重寫標籤語意。
  - 改寫 SSOT 定義的業務 KPI。

---

## 2) 設計原則

- **field-test operating objective 對齊優先**：模型選擇與 winner-pick 優先對齊 `**min_alerts_per_hour >= 50` 下的 precision**；主 KPI 與其 prod-adjusted 版本保留為 guardrail / shadow metrics。
- **可比性優先**：同資料窗、同切分、同口徑比較；拒絕 apples-to-oranges。
- **多窗證據優先**：單窗 uplift 不可直接升級為決策級結論。
- **train-serve parity 優先**：特徵可得性與時間可得性不成立則直接降級或退回。
- **資源可控優先**：任何方案需明示 RAM/runtime 影響，不可默默放大運算負擔。

---

## 3) 實施路線（依 ROI 由高到低）

### R1. HPO / 模型選擇目標對齊 field-test operating objective（ROI 16，DEC-043 對齊）

- 方案：將 HPO objective 與 winner-pick 語意從 AP 對齊到 field-test operating objective：在 rated-eligible 樣本上，以 `**min_alerts_per_hour >= 50`**（及既有最小 alert guards）為主要可行條件，最大化 precision；若 validation / test 存在 negative sampling，優先使用 **production-adjusted precision proxy** 做排序。主 KPI 與其 prod-adjusted 版本保留為 guardrail / shadow metrics，而非唯一 driver。**本輪依 DEC-043 採 field-test-only：不接受 AP fallback 作可比結果。**
- 啟動前置條件（Precondition）：
  - 在修改 `run_optuna_search()` 或等價 HPO 路徑前，必須先統計現行 time-fold / validation folds 的 **walkaway 正例數 / finalized TP 數量級**、**總 rated bet 數與 `fold_duration_hours`**、**baseline 模型的可行 threshold 集合大小**，以及 **test neg/pos ratio**（若 validation / test 有負採樣）。
  - 若任一 fold 顯示 field-test objective 的可行 threshold 太少、`T_feasible` 常為空、尾段指標支撐不足，或 `PRODUCTION_NEG_POS_RATIO` 假設不穩，則本輪 run 狀態為 **`BLOCKED` / `gate_blocked`**（不納入可比）；先調整驗證窗或資料契約後再重跑。
- 關鍵交付：
  - 目標函數設計說明（含 field-test mode、何時用單一目標/複合目標）。
  - 新舊 objective 對照報告（多窗）。
- 前置條件產物：
  - 機器可讀：`out/precision_uplift_field_test_objective/field_test_objective_precondition_check.json`
  - 人類可讀摘要：`trainer/precision_improvement_plan/field_test_objective_precondition_check.md`
- 主要風險：validation 正例過稀、可行 threshold 稀少，或 `prod_adjusted` 對 production neg/pos ratio 假設過度敏感，導致高方差。
- 緩解：
  - 先補齊 precondition 與 validation span 證據，再啟動 constrained objective；不以 AP fallback 掩蓋不可行 run。
  - 若某 fold 的 `T_feasible` 為空或可行域不成立，實作上應輸出 **`BLOCKED` / `gate_blocked`** 與可稽核 reason code，避免 trainer / backtester / calibration 三邊語意分裂。

### R2. 排序導向訓練 + Hard Negative + Top-band Reweighting（ROI 14）

- 方案：直接壓制高分 FP，提升頂段排序純度。
- 關鍵交付：
  - weighting/mining 策略矩陣與版本化設定。
  - 主指標 uplift + 穩定性證據。
- 主要風險：過度針對 FP 造成 recall 破壞。
- 緩解：同一 field-test operating contract 下比較，保留等價 alert volume 與閾值語意。

### R3. LightGBM/CatBoost/XGBoost 公平 Bakeoff（ROI 11）

- 方案：在相同特徵與切分下做模型族公平比較。
- 關鍵交付：
  - 模型族比較報告（主指標、波動、成本）。
  - 優勝者與保留條件。
- 主要風險：實驗不可比。
- 緩解：固定實驗契約與統一報表欄位；「single winner」定義為 phase-level 收斂策略（便於驗證可比性與上限），不是最終架構限制。

### R4. 二階段模型（Stage-1 + Stage-2 FP detector/reranker）（ROI 10）

- 方案：Stage-1 保召回，Stage-2 專注 top-band TP/FP 重排。
- 啟動條件（Activation Criteria）：
  - R1 與 R2 已完成，且最佳單階段路線至少達到 **comparative** 等級的多窗證據。
  - 單階段最佳方案與目標仍存在明顯殘餘 gap，而非僅剩微幅尾差。
  - top-band error analysis 顯示 FP 污染仍是主瓶頸，且問題型態更像「高分帶重排不夠乾淨」而非標籤/契約失真。
  - serving 延遲、artifact 複雜度與 train-serve parity 成本在可接受範圍內。
- 關鍵交付：
  - 二階段 PoC（訓練/推論/監控全鏈路）。
  - 與單階模型的公平對照。
- 主要風險：工程複雜度與延遲上升。
- 緩解：PoC 階段先做離線全鏈路與 artifact/parity 驗證，暫不直接接入 production serving；僅在離線主指標、多窗穩定性與 train-serve parity 同時成立後，再評估線上部署方式、延遲預算與回滾方案。

### R5. 離線序列嵌入（SSL）作為 GBDT 特徵（ROI 9）

- 方案：建 embedding pipeline，回灌至 tabular 特徵。
- 關鍵交付：embedding 版本化與 ablation 證據。
- 主要風險：驗證鏈長、資源成本高。
- 緩解：先小規模驗證後擴展。

### R6. 分群建模 + Learned Gating（ROI 8）

- 方案：2~4 experts + gate 路由，降低單模型欠擬合。
- 關鍵交付：群別效能、路由穩定性、整體 uplift 證據。
- 主要風險：資料切碎導致過擬合。
- 緩解：從少量 experts 起步，強制多窗驗證。

### R7. 接線 `compute_table_hc` 與桌況特徵（ROI 7）

- 方案：將既有 `table_hc` 正式接到訓練與 serving 主路徑。
- 關鍵交付：接線完成、parity 驗證、成本評估。
- 主要風險：延遲與記憶體增加。
- 緩解：先 smoke 再擴窗，觀測 RAM/runtime。

### R8. 擴張 Track LLM/Human 候選特徵（ROI 6）

- 方案：擴充候選特徵並透過既有 screening 收斂。
- 關鍵交付：候選版本、入選清單、ablation。
- 主要風險：候選爆量造成噪音與成本。
- 緩解：嚴格篩選門檻、冗餘剔除。

### R9. D3 identity PIT-correct mapping（ROI 5）

- 方案：修正整窗 mapping 的時間洩漏風險。
- 關鍵交付：小窗 ablation + 全量替換建議。
- 主要風險：改動面廣。
- 緩解：先小窗、再漸進 rollout。

### R10. 真多窗 Phase 2 矩陣 + gate（ROI 5，治理型 / enabling）

- 方案：將勝負判定升級為多窗統計（均值/波動/最差窗）；本項屬驗證治理能力，而非單獨的 uplift 技術。
- 關鍵交付：多窗 gate 標準化輸出。
- 治理地位：
  - R10 是 R1~R9 任一路線從 **comparative** 升級到 **decision-grade** 的必要前置能力。
  - 若缺少 R10，多數路線最多只能維持 exploratory / comparative，不得以單窗證據直接做最終保留/淘汰決策。
- 主要風險：實驗成本增加。
- 緩解：先固定核心實驗矩陣，控制組合數。

### R11. Profile history-depth bundle（ROI 4）

- 方案：按歷史深度/完整度做特徵或模型 bundle。
- 關鍵交付：bundle 規則與分層效能。
- 主要風險：分桶過細導致樣本不足。
- 緩解：最小樣本門檻與合併策略。

### R12. DEC-032 線上校準閉環（ROI 3）

- 方案：成熟標籤回流與 runtime threshold 治理自動化，並支援 `DEC-026 field_test mode` 的 threshold 重估。
- 關鍵交付：校準審計、一致性報告、mode-labeled threshold selection 輸出。
- 主要風險：被誤解為排序能力提升，或因 prior ratio 假設漂移而把 runtime threshold 帶偏。
- 緩解：報告中強制區分排序提升 vs operating point 修正，並對 `precision_raw` / `precision_prod_adjusted` / `alerts_per_hour` 做並列審計；雖然本項 ROI 排序不變，但不可被視為可無限延後，因為 `#1` 若依賴 `prod_adjusted precision` 作 proxy，DEC-032 / prediction log 將成為其可信度的重要支撐。

### R13. Stacking/Blending/Ensemble（ROI 2）

- 方案：僅在基模型錯誤型態互補時導入。
- 關鍵交付：集成收益-成本分析與保留決策。
- 主要風險：複雜度高、增益小。
- 緩解：明確保留門檻與回滾路徑；僅在增益足以覆蓋 serving 延遲與記憶體成本時升級，避免把 ensemble 當預設終點。

---

## 4) 模組邊界與責任

- 訓練策略層：HPO objective、weighting/mining、winner-pick。
- 特徵層：feature spec、計算函式、screening 與可得性契約。
- 模型層：模型家族比較、二階段/分群架構。
- 評估治理層：多窗 gate、結論等級、可比性檢查。
- serving/校準層：runtime threshold、線上一致性、審計。

### 4.1 DEC-026 擴充方向（field-test mode）

- 保留既有 `DEC-026` 作為 shared threshold-selection family，不直接覆寫其研究 / legacy 語意。
- 建議新增明確 mode：
  - **research / legacy mode**：`recall_floor` 導向，在可行點中最大化 raw precision。
  - **field_test mode**：以 `**min_alerts_per_hour >= 50`** 為主要可行條件，保留 `min_alert_count`，並優先最大化 `**prod_adjusted precision**`；`recall_floor` 可退為 guardrail 或次級約束。
- trainer / backtester / 後續 calibration 應顯式輸出 `selection_mode` 與 chosen threshold 對應的 `precision_raw`、`precision_prod_adjusted`、`recall`、`alerts_per_hour`，避免不同 operating contract 混用；本輪 `selection_mode` 固定 `field_test`。
- 若某次 threshold search 的 `T_feasible` 為空或可行域不成立，shared contract 應輸出可稽核 **`BLOCKED` / `gate_blocked`** 與 reason code（不以 AP fallback 視為可比），避免各模組各自定義「不可行」。

---

## 5) 驗證與決策規則

- objective contract（本輪已依 DEC-043 凍結，implementation / execution 需一致）：
  - **feasibility constraint**：`alerts_per_hour >= 50`（長期平均）。
  - **optimization target**：`prod_adjusted precision`。
  - **soft guardrails / shadow metrics**：主 KPI 與其 prod-adjusted 版本。
- 若 precondition check 顯示 constrained objective 支撐不足（例如任一 fold 的 `T_feasible` 過小或常為空），run 應標記 **`BLOCKED` / `gate_blocked`**，不納入可比；先調整驗證窗設計後再重跑。
- `None` 指標需遵循 `None -> 單一 reason_code` 契約（`empty_subset` / `single_class` / `invalid_input_nan` / `infeasible_constraint` / `missing_required_column`）。
- 不設最小 uplift gate：**微小 uplift 也可保留**，但需滿足：
  - 同契約可重跑。
  - 多窗方向一致或可解釋。
  - 無明顯 parity 破壞。
- 決策等級：
  - exploratory：僅方向性訊號。
  - comparative：可比性成立且有穩定趨勢。
  - decision-grade：可作路線保留/淘汰依據。
- 升級 / 到期規則：
  - 任一路線若連續 **2 次以上 evidence review / reorder review** 仍停留在 exploratory，且沒有新增可比性證據，應自動觸發降級投入或淘汰評估。
  - 任一路線要升級到 decision-grade，除自身證據充分外，必須同時滿足 R10 的多窗治理前置能力。

---

## 6) 風險與依賴（跨路線）

- 指標高方差風險：尾段 metric 與 volume-constrained objective 都可能對樣本數與可行 threshold 數量敏感。
- 資源風險：特徵與模型複雜化帶來 RAM/runtime 上升。
- 契約風險：切分、標籤、時間可得性若漂移，結論失效。
- prior 假設風險：`PRODUCTION_NEG_POS_RATIO` 若估錯，`prod_adjusted` 排序可能被帶偏。
- 治理風險：單窗漂亮但多窗失敗。

---

## 7) 交付治理

- 每輪必交：
  - 方案版本（配置與契約）
  - 指標報告（主指標 + 穩定性）
  - 風險與限制說明
  - 保留/淘汰建議
- R1 額外交付：
  - `out/precision_uplift_field_test_objective/field_test_objective_precondition_check.json`：fold 正例數、finalized TP 數量級、rated bet 數、`fold_duration_hours`、可行 threshold 支撐度、test neg/pos ratio（若有負採樣）、`PRODUCTION_NEG_POS_RATIO` 假設摘要、是否允許單一 constrained objective。
  - `trainer/precision_improvement_plan/field_test_objective_precondition_check.md`：前置條件檢查摘要與採用/不採用單一 objective 的理由。
- 文檔同步順序：
  1. 更新本 implementation plan 的排序與狀態註記。
  2. 更新 execution plan（任務拆分與順序）。
  3. 更新 runbook（操作與故障處理）。

---

## 8) 動態重排規則（本版核心）

- **允許每輪證據後動態重排 ROI 順序**，但需遵守：
  1. 重排依據必須可稽核（報告/指標/成本證據）。
  2. 必須保留「前序版本 -> 新版本」的排序變更紀錄。
  3. 不可因單一偶然窗結果做大幅跳排；至少需多窗或重跑支持。
  4. 若重排牽涉架構邊界變更，先回寫 SSOT/上游治理文件再生效。
- 建議重排觸發：
  - 某路線連續多輪僅增複雜度、無可驗證收益。
  - 某低順位路線展現穩定 uplift + 可控成本。
  - 資料契約或 serving 約束改變，導致原 ROI 假設失效。

---

## 9) 版本資訊

- 版本：v3 (dynamic-reorder enabled, field-test objective)
- 建立日期：2026-04-21
- 適用文件：
  - `trainer/precision_improvement_plan/PRECISION_UPLIFT_FIELD_TEST_OBJECTIVE_RECOMMENDED_ITEMS_ROI.md`
  - `.cursor/plans/PLAN_precision_uplift_sprint.md`
  - `trainer/`

