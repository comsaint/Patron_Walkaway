# 預測模型「近期未來穩健性」文獻整理與最佳實務

> 本文整理在**資料、時間與算力皆不構成限制**的前提下，如何讓預測模型在**近期未來**保持穩健的線上文獻與實務建議，供專案日後參考。

---

## 一、評估與驗證：用「多切點、多期」逼出穩健性

**核心結論：單一切點、單一 horizon 的評估會高估穩健性；要用多切點、多 horizon、時間順序嚴格的 backtest。**

- **時間順序不可打破**  
  必須嚴格保持時間順序做 train/test 分割，否則會產生「用未來預測過去」的洩漏。Random k-fold 不適用；應使用**單向時間分割**或 **walk-forward** 類方法。

- **三種 backtest 策略**（skforecast、Walk-Forward Analysis 等實務常用）  
  1. **Expanding window（擴展視窗）**：每次切點用「從頭到該切點」的歷史訓練，測試下一段。適合長期模式穩定、想充分利用歷史的場景。  
  2. **Rolling window（固定訓練長度）**：固定訓練長度、隨時間滑動。適合非平穩、近期行為更重要的情境。  
  3. **No-refit**：只訓練一次、一路預測下去。可作為「最樂觀」對照，實務上通常會隨時間衰退。

- **切點選擇本身會影響結論**  
  若把「樣本分割點」當成可調的選擇（data snooping），傳統檢定會有 size distortion，尤其在**評估樣本很短**時。因此：  
  - 切點應**事先固定**或透過明確規則決定（例如「最後 20% 為 holdout」）。  
  - 穩健性應在**多個切點**上重複評估，而不是只報一個切點的數字。  
  - 參考：*Choice of Sample Split in Out-of-Sample Forecast Evaluation* (RePEc).

- **多期預測的評估**  
  若同時評估多個 horizon（h=1,2,…,H），不應把整條路徑當成一個觀測來做 Diebold–Mariano；應區分「每個 horizon 單獨比較」或使用 **multi-horizon** 的檢定（例如 Quaedvlieg 的 Uniform SPA / Average SPA），才能得到一致且可解釋的結論。  
  - 參考：*Multi-Horizon Forecast Comparison*；*Statistical tests for multiple forecast comparison* (Elsevier).

**實務建議（資源充足時）**  
- 做 **multi-cutoff backtest**：多個歷史切點（例如每月或每季一個），每個切點用 expanding 或 rolling 重訓、產出多步預測。  
- 每個切點上報 **multi-horizon 誤差**（如 MAPE/sMAPE by h），並檢視誤差隨 horizon 與隨切點是否穩定。  
- 超參數與模型選擇應基於**多切點平均或最差表現**，而不是單一切點，以提升「近期未來」的穩健性。

---

## 二、結構性斷裂（structural break）與 regime 變化

**核心結論：忽略結構性斷裂會讓「近期未來」表現崩潰；若算力與資料足夠，應顯式處理斷裂或 regime 切換。**

- **問題本質**  
  分布或關係在時間上發生變化（level、trend、波動率等）。若只用在「斷裂前」主導的資料訓練，對斷裂後的近期未來預測會不穩。

- **實務做法（文獻中常見）**  
  - **動態切換**：用時間序列聚類或 break 檢測（如 Bai–Perron、PELT）辨識斷點，在不同區間用不同模型或權重（例如穩健 vs 非穩健模型）。在短 horizon 上可顯著優於忽略斷裂的基準。  
    - 參考：Oxford 動態切換；*Using structural break inference for forecasting* (Springer).  
  - **貝氏 / 不確定性**：把「未來可能再發生斷裂」建模進預測（例如 hierarchical hidden Markov），在較長 horizon 上優於假設無斷裂的方法。  
    - 參考：*Forecasting Time Series Subject to Multiple Structural Breaks* (IZA).  
  - **Break 日期的不確定性**：用**置信集平均**（對 break 日期的置信區間做平均）而非單一點估計，在 variance break 與係數 break 並存時特別有幫助。  
  - **混合架構**：結構斷裂檢測（Bai–Perron、ICSS、PELT）+ 去噪（如 wavelet）+ 深度模型（LSTM/GRU/TCN）的組合，在部分應用（如碳價）上顯著降低誤差。  
    - 參考：*Hybrid Deep Learning with Structural Breakpoints* (arXiv).

**實務建議**  
- 在 backtest 設計中刻意包含「跨越可能斷裂點」的切點，看模型在斷裂前後誤差是否劇變。  
- 若資源允許：引入 break 檢測或 regime 識別，並做 **regime-conditioned 訓練或預測組合**（不同 regime 用不同權重或不同模型）。

---

## 三、預測組合（forecast combination）與集成

**核心結論：在「近期未來穩健」目標下，組合多個異質模型往往比單一「最優」模型更穩；M4 等競賽與文獻一致支持此點。**

- **M4 競賽**  
  - 前段方法中，多數是**組合方法**（組合多個統計或統計+ML 模型）。  
  - **純 ML 在該賽中普遍不如組合**；表現最好的之一為「多個統計方法 + 一個 ML，用 ML 學權重」的混合。  
  - 預測區間（prediction intervals）表現最好的方法，同時在點預測上也很強，說明**機率校準與點預測穩健性可並存**。  
  - 參考：*The M4 Competition* (International Journal of Forecasting)；Google *The M4 Forecasting Competition – A Practitioner's View*.

- **組合為何有助穩健**  
  - 不同模型在不同 regime、不同 horizon、不同序列類型上各有所長；組合可降低**單一模型誤設（misspecification）** 的風險。  
  - 實務上常搭配 **FFORMA（Feature-based Forecast Model Averaging）** 等依特徵自動權重的方法，或簡單的權重平均/中位數。

**實務建議**  
- 在資源充足下：訓練多類模型（例如 ARIMA/ETS、樹模型、簡單神經網路、Prophet 等），用 **multi-cutoff backtest** 估計各模型在不同切點、不同 horizon 的表現，再學權重或簡單平均。  
- 權重可依「最近 N 個切點」或「最近一段時間」的表現來算，以貼近「近期未來」。

---

## 四、預測穩定性（forecast stability）：垂直與水平

**核心結論：穩健性不僅是「誤差小」，還包括「預測隨時間更新時不要劇烈抖動」；這會影響決策信任與營運成本。**

- **兩類穩定性**  
  - **垂直穩定性**：同一目標日，隨預測時點（forecast origin）往後移，預測不要大幅修正。  
  - **水平穩定性**：同一 origin 下，預測在 horizon 上不要不合理地劇烈震盪。  
  - 參考：*Using dynamic loss weighting to boost forecast stability* (arXiv 2409.18267)；*Analyzing retraining frequency of global forecasting models* (arXiv 2506.05776).

- **不穩定的代價**  
  供應鏈、排程、人力配置等會因預測頻繁大幅修正而增加成本、降低信任。

- **可採用的做法**  
  - **動態 loss 權重**：訓練時同時考慮誤差與穩定性（例如垂直/水平變動的懲罰），在不大幅犧牲準確度下提升穩定性。  
  - **閉環控制**：對自迴歸式預測，開環會造成誤差累積；有研究用閉環 + 殘差估計器來限制誤差發散。  
    - 參考：*Closing the Loop: Provably Stable Time Series Forecasting with LLMs* (arXiv).  
  - **重訓頻率**：**更少重訓有時反而更穩**；過度頻繁重訓可能放大短期噪音，導致預測抖動。穩定性應納入「何時重訓」的決策。

**實務建議**  
- 在 backtest 中除了誤差指標，也量測「同一目標日在不同切點下預測的變異」或「相鄰 horizon 預測的平滑度」。  
- 若業務對「改來改去」很敏感，可考慮：較長重訓間隔、或納入穩定性項的 loss、或對預測做輕量後處理（如平滑）。

---

## 五、概念漂移、共變量偏移與重訓策略

**核心結論：穩健的「近期未來」需要偵測分布/概念變化，並用「何時重訓」與「重訓範圍」來平衡穩定性與適應性。**

- **定義簡述**  
  - **Covariate shift**：輸入 X 的分布變了，但 P(Y|X) 不變。  
  - **Concept drift**：P(Y|X) 變了。時序預測文獻中還會區分 **temporal shift** 與 **concept drift**，且後者常被忽略。  
  - 參考：*Tackling Time-Series Forecasting Generalization via Concept Drift* (arXiv)；*DriftGuard* (arXiv).

- **偵測方式**  
  文獻與實務常用：誤差監控、統計檢定（如 KS）、自編碼器異常、CUSUM 等變點偵測。

- **重訓策略**  
  - 固定每 3–6 個月重訓可能既浪費（穩定期）又延遲（快速漂移）。  
  - **不一定要「一偵測到 drift 就重訓」**：用不確定性與預期的效能演進來決定是否重訓、何時重訓，可優於簡單規則。  
    - 參考：*Evaluating Model Retraining Strategies* (Towards Data Science).  
  - **選擇性重訓**：只重訓受影響的模型或區段（如 DriftGuard 的分層診斷 + 選擇性重訓），可大幅提升 ROI。

**實務建議**  
- 在資源充足下：建立 **drift 監控**（誤差趨勢、輸入/輸出分布、依 segment 的誤差），並用 **multi-cutoff backtest** 觀察「若在該切點重訓，接下來一段時間表現如何」。  
- 重訓策略可設為：**事件驅動（drift 達閾值）+ 週期性檢查**，並在 backtest 中比較「固定週期重訓」vs「drift 觸發重訓」的穩健性。

---

## 六、生產環境與 MLOps 的穩健性

**核心結論：模型在「近期未來」穩健，除了演算法還需要資料品質、版本一致、監控與漸進式部署。**

- **Microsoft Forecasting、AWS、TSFM 等實務建議**  
  - **資料**：除了 schema，要做**統計層檢查**（常數序列、突跳、過期資料）；生產環境傾向**按需重新產生資料**而非只版本化舊資料集。  
  - **特徵**：特徵品質常比模型選擇更重要；lag、rolling、日曆、業務指標等要與模型一起**版本化**，rollback 時特徵與模型一致。  
  - **訓練與 serving 一致**：同一套特徵與邏輯在訓練與推論使用，避免 training–serving skew。  
  - **多模型**：依需求（機率 vs 點預測、延遲、領域）選不同模型，必要時按 series 或 segment 路由。  
  - **極端值與厚尾**：金融、能源、部分營運指標常有厚尾；可考慮穩健損失或專門處理尾部的架構（如 TCN + 厚尾分布）。  
    - 參考：*Robust time series forecasting with MLOps* (AWS SageMaker).  
  - **MLOps**：CI/CD、自動重訓、**模型與資料漂移監控**、canary/比例流量部署，以降低「一次換錯」的風險。

與本專案 Phase 2 規劃（`phase2_planning.md`）中的 drift 觸發重訓、staged rollout、artifact 溯源、健康/SLA、資料品質監控等一致，可視為同一套最佳實務。

---

## 七、機率預測與區間

**簡要結論：若決策依賴不確定性，應產出並評估區間或分位數；校準良好、銳度合理的區間有助於「近期未來」的穩健決策。**

- M4 中頂尖方法在 **95% 區間覆蓋** 上表現也好；點預測與區間預測可一起優化。  
- 評估時除覆蓋率（calibration）外，也應看**銳度**（區間寬度），避免用過寬區間換取覆蓋率。

---

## 八、綜合建議（資源不受限時如何保證「近期未來」穩健）

| 維度 | 建議 |
|------|------|
| **評估** | 多切點、多 horizon backtest；嚴格時間順序；切點事先固定或規則化；報多切點平均/分位數而非單一切點。 |
| **結構變化** | 做結構斷裂/regime 檢測；必要時 regime-conditioned 模型或權重；backtest 刻意跨斷裂點。 |
| **模型** | 採用**預測組合/集成**（統計+ML、多種模型），用多切點表現學權重或平均。 |
| **穩定性** | 監控垂直/水平預測穩定性；必要時納入穩定性項或降低重訓頻率。 |
| **漂移與重訓** | 監控 drift；事件驅動 + 週期性重訓；可選選擇性重訓；用 backtest 評估重訓策略。 |
| **生產** | 特徵與模型共版本、訓練/serving 一致、資料與模型漂移監控、staged rollout、健康/SLA。 |
| **機率** | 若有決策需求，產出並校準區間/分位數預測。 |

---

## 參考來源摘要

- **評估與 backtest**：skforecast backtesting、Walk-Forward Analysis (Medium)、Choice of Sample Split (RePEc)、Multi-Horizon Forecast Comparison、Diebold–Mariano multi-horizon (Stats Stack Exchange).  
- **結構斷裂**：Oxford 動態切換、Structural break inference (Springer)、IZA 多斷裂貝氏、Hybrid Deep Learning Structural Breakpoints (arXiv).  
- **組合與 M4**：M4 Competition (IJF)、Google M4 practitioner view、FFORMA.  
- **穩定性**：Dynamic loss weighting (arXiv 2409.18267)、Retraining frequency (arXiv 2506.05776)、Closing the Loop LLM (arXiv).  
- **漂移與重訓**：Concept drift mitigation (arXiv)、DriftGuard (arXiv)、Evaluating retraining strategies (Towards Data Science).  
- **生產**：Microsoft Forecasting、AWS SageMaker robust forecasting、TSFM production pipelines.

*文件建立日期：2025-03-15。供 Patron Walkaway 專案日後參考。*
