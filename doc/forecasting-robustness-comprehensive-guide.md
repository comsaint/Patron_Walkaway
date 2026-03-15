# Patron Walkaway 風險模型近期穩健性完整指南

> **問題設定：** 給定當前與近期歷史行為特徵，預測 patron 在未來一段時間內是否會 walk away（Y ∈ {0,1}），輸出為機率  
> **目標：** 讓風險分數在時間推進、環境變化與資料延遲下，仍維持穩定的排序能力、合理機率刻度與可控維運成本  
> **最後更新：** 2026-03-16

---

## 1. 評估設計：用時間和分群逼出真實穩健性

### 1.1 嚴格維持時間順序

- 不使用 random k-fold，因為會混淆時間，產生「用未來預測過去」的資訊洩漏。  
- 採用多切點的時間切分：  
  - 對每個切點 \(T_i\)：用 \(t \le T_i\) 訓練，用 \(T_i < t \le T_i + \Delta\) 評估。  
  - 使用 expanding 或 rolling window，模擬真實上線場景。

### 1.2 多切點、多指標

對每個切點，至少記錄：

- **Discrimination**  
  - AUC  
  - PR-AUC（在正例稀少時更敏感）

- **Calibration**  
  - Brier score  
  - Calibration curve + ECE（Expected Calibration Error）

- **Decision metrics**  
  - 在實際使用閾值（或多個候選閾值）下的 precision / recall / FPR / lift

跨切點時，不只看平均，也要看標準差與「最差幾個切點」表現，因為 robustness 本質上是對「壞情況」的控制，而不是只看平均。

### 1.3 分群穩健性

對以下 segment，在每個切點上分開評估上面指標：

- 時段：peak / off-peak、weekday / weekend、特殊活動期間  
- 場域：不同店、樓層、設備群  
- 客群：新客 / 老客、高價值 / 一般 patron  
- 其他關鍵情境：某些促銷或營運策略啟用期間

**判斷準則：** 如果整體 AUC 很好，但某些重要 segment 的 AUC/校準或 decision metrics 一直很差，那模型「整體看起來穩健」只是錯覺。

---

## 2. 結構性變化（Regime）與行為轉換

### 2.1 什麼算 regime change

在這裡，regime change 通常來自：

- 標籤定義或計算邏輯的更改（例如 walkaway 時間窗變了）  
- 客群組成的變化（不同國家、不同年齡、不同博彩偏好）  
- 流程或環境調整（等候策略、機台佈局、獎勵機制、營運時間）  
- 外部衝擊（大型活動、疫情、政策調整）

這些會讓 \(P(Y\mid X)\) 改變，而不只是 \(P(X)\) 或 \(P(Y)\) 改變。

### 2.2 Regime-aware 分析

- 在設計時間切分時，刻意包含「事件前 vs 事件後」的切點。  
- 分別觀察 regime 前後的：AUC、PR-AUC、Brier、ECE。  
- 若在 regime 轉換前後，performance 有斷崖式變化，這是明確訊號，意味該 regime 需要區別對待。

### 2.3 模型層應對

- 將明確的 regime（例如新系統上線後）編碼為特徵，如 `is_new_system`, `days_since_policy_change`。  
- 若 regime 之間差異巨大，考慮：  
  - 分別訓練多個子模型，  
  - 或用 gating 機制在 inference 時選擇適合的子模型。

---

## 3. 模型與集成（Ensemble）

### 3.1 模型族群

實際可用模型類型：

- Logistic / penalized GLM：強校準、可解釋，是穩定 baseline。  
- Tree-based：XGBoost、LightGBM、Random Forest，擅長非線性與交互作用。  
- 深度模型：  
  - MLP over aggregated features  
  - 若有行為序列：RNN / Transformer over event sequences，再接分類頭

### 3.2 為什麼 ensemble 有助穩健性

不同模型對 drifts、噪音與特徵偏移的敏感度和錯誤模式不同。透過 ensemble：

- 可以降低單一模型 misspecification 的風險。  
- 在不同時間與 segment 上，通常表現會「少踩雷」，而不是只在某個 window 特別好。

### 3.3 實作建議

- 先確保每個 base 模型本身有合理 performance 與 calibration。  
- 使用簡單的機率平均作為 baseline ensemble。  
- 再進一步：  
  - 依最近 K 個切點的 AUC/Brier 為模型加權（recency-weighted ensemble）。  
  - 或用 stacking（使用 second-stage logistic regression 來整合各模型輸出的 logit/probability）。

重新 ensemble 之後，記得再做一次 calibration（下一節）。

---

## 4. 機率校準與分數穩定性

### 4.1 校準（Calibration）

風險分數會直接被用來管理告警、人工介入、人力配置與營運決策，因此需要「可以拿來當機率用」，而不只是排序分數。

**建議流程：**

1. 對訓練期最後一段時間保留作為 calibration window。  
2. 在該 window 上，對模型預測進行：  
   - Platt / logistic scaling，或  
   - Isotonic regression，或  
   - Beta calibration（如有必要）。  
3. 每次 retrain 或大版本升級時，都重做這一步。

在多切點評估時，校準指標（Brier、ECE、reliability curve）的穩定性應被視為與 AUC 同等重要。

### 4.2 分數與排名穩定性

為了避免版本更新或小修小補造成現場混亂，可引入兩個指標：

- **Score stability**：  
  - 在同一批樣本上，比較舊版與新版分數的絕對差 \(|p_{\text{old}}(x) - p_{\text{new}}(x)|\)。  
  - 觀察平均、分位數（例如 90% 樣本差異小於 0.05）。

- **Ranking stability**：  
  - 比較不同版本 top-k high-risk 名單的重疊比例。  
  - 可以分不同 segment 評估。

在 release checklist 中，設定可接受的最大分數變動與排名變動門檻，有助於降低「模型更新造成操作策略崩壞」的風險。

---

## 5. 漂移監控與重訓策略

### 5.1 監控什麼

從三個面向監控 drift：

1. **輸入特徵分布**  
   - PSI（Population Stability Index）  
   - 缺失率、值域、極端值比例  
   - 重要特徵的直方圖或分位數隨時間變化

2. **預測分數分布**  
   - 平均預測風險  
   - top-k 比例（例如 predicted risk > 0.8 的比例）  
   - 分數分布是否突然集中在 0 或 1

3. **延遲回流的真實表現**  
   - AUC / PR-AUC 隨時間的趨勢  
   - Brier / ECE 隨時間的趨勢

### 5.2 應對順序

一個穩健的應對順序：

1. **監控與告警**：一旦 PSI 或預測分布異常，先標記再查原因。  
2. **Recalibration 優先**：  
   - 如果 discrimination（AUC）還可以，但 Brier/ECE 明顯變差，先考慮在最近一段有標籤的資料上重新 fit calibration map。  
3. **選擇性重訓**：  
   - 若 drift 很集中在某些店別、segment，先做局部重訓或 segment-specific model。  
4. **完整重訓**：  
   - 當 drift 是全局性的且長期持續，才進行完整 retrain，並重新 calibrate。

### 5.3 Test-Time Adaptation（TTA）

TTA 是中階選項：  
- 不動整個模型，只調整少數層（例如 normalization 層或最後幾層）。  
- 利用最近一小段無標籤資料，讓模型更好對齊新的輸入分布。  

建議把它視為「drift 已出現但還沒嚴重到必須 retrain 時」的工具，而不是所有場景預設啟用。

---

## 6. Label Delay（標籤延遲）

### 6.1 問題本質

對 walkaway 預測而言：

- 你預測「未來 X 分鐘內會走人」，但要等這個窗口過完（或 patron 實際走人）才知道標籤。  
- 這意味著：  
  - model performance（AUC/Brier）永遠是「落後現在」的一段時間。  
  - 用 performance 來監控 drift 時，會免不了有 delay。

### 6.2 實務對策

- 把監控面板切成兩層：  
  - 即時層：只看特徵分布與預測分布的 drift，偵測潛在異常。  
  - 延遲層：等標籤到齊後再計算 performance 指標。

- 避免在標籤尚未回流的期間，頻繁基於未驗證的 performance 直覺對模型做大幅更新。

### 6.3 Pseudo-labeling：條件式使用

Pseudo-labeling 可以在標籤延遲時提供「代理標籤」支援增量更新，但前提是：

- 模型目前在該區段並沒有被強烈 drift 破壞；  
- 只使用高信心、且經過漂移偵測檢查仍相對穩定的樣本；  
- 有明確 rollback 機制。

建議在文件中將它定位為「可選用、需要完整實驗驗證的進階方案」，而不是核心機制。

---

## 7. 動態類別不平衡與 Prior Shift

### 7.1 類別比例會隨時間漂移

Walkaway rate 並非常數：

- 尖峰 vs 離峰  
- 活動日 vs 平日  
- 不同季節、不同比例新客

單一訓練時期估出的 prior \(P(Y)\) 若被硬套到所有未來時段，會破壞校準，特別是在正例率大幅偏離時。

### 7.2 動態處理建議

- 定期估計最近一段時間的實際走人率。  
- 在決策層（例如 threshold setting、資源分配）考慮現在的正例率，而不是固定使用訓練時期的想定。  
- 在理論上瞭解 **prior shift** 的前提下，可以：  
  - 將模型視為估計 \(P(X\mid Y)\) 或某種打分，再根據新的 \(P(Y)\) 用 Bayes 更新後驗機率。  
  - 但要小心，這只在「主要變的是 P(Y)，而非 P(Y\mid X)」時成立。

### 7.3 實務落地方式

- 把 dynamic prior update 定位為「低成本、適合處理純粹 prior drift 的工具」。  
- 在文件裡明寫前提：如果分析顯示不同時期給定同樣特徵的走人行為仍類似，dynamic prior 很值得採用；若不是，就不能只改 prior，需要回到 drift / retrain 機制。

---

## 8. 生產 MLOps 與治理

### 8.1 資料與模型版本管理

每次訓練都應記錄：

- 特徵 schema 與 ETL/特徵工程邏輯版本  
- 訓練資料時間範圍、樣本篩選條件  
- 標籤定義（window、規則）  
- 模型參數與架構版本  
- 校準方法與參數  
- 上線時間與對應版本號

這讓你可以在出現異常時追溯「這一批預測是用什麼訓練資料、什麼特徵與什麼校準」做出來的。

### 8.2 部署流程

建議部署包含：

- Shadow / canary：新模型先在小流量或陰影模式運行，與現行模型並行比較。  
- 上線前自動跑多切點 backtest。  
- 在新模型上線的前後，特別關注 score stability 與 ranking stability，以及主要決策數字（例如被標為高風險的人數比例）是否合理。

### 8.3 監控儀表板

至少要有：

- 特徵分布與 PSI  
- 預測分數分布  
- 延遲 performance（AUC、PR-AUC、Brier、ECE），按時間與 segment 分解  
- 模型版本切換點標註在所有圖上

---

## 9. 執行優先級（對本問題最重要的層）

### 核心必做（建議第一個月內完成）

- 多切點時間切分評估 + 分群穩健性  
- AUC / PR-AUC / Brier / ECE 作為標配指標  
- 校準流程（Platt / Isotonic / Beta）與定期 recalibration  
- PSI + 分數分布 drift 監控  
- 特徵 / 標籤 / 模型 / 校準的版本化  
- 基礎 ensemble（至少 logistic + tree-model）

### 建議做（第二階段）

- score / ranking stability 指標  
- regime-aware 分析與必要時的 regime-specific model  
- selective retraining policy（按 segment 或時間）  
- Label Delay 的雙層 dashboard（即時 vs 延遲）

### 進階／條件式採用

- Test-Time Adaptation  
- pseudo-labeling 更新機制  
- dynamic prior update（在 prior shift 假設成立時）

---

## 10. Summary：面對「近期未來」的思路

對 Patron Walkaway 這個問題，「穩健」可以拆成幾件具體可操作的事情：

- 用多切點、多 segment 的時間驗證，**逼出所有該在未來出現的壞情況**。  
- 用 ensemble + 校準，把模型輸出的機率變成**可信的風險刻度**。  
- 用 drift 監控 + recalibration + selective retrain，把模型維持在**不會突然崩壞**的狀態。  
- 把 label delay 和 prior shift 的現實納入設計，而不是事後補救。  
- 用 MLOps 把所有變化都變成**可回溯、可管控**的版本化事件，而不是「黑盒裡換了一個模型」。

這份文件的目的，就是讓上面這些原則具體化，變成你可以直接實作與 review 的 checklist。
