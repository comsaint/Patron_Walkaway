# 預測模型近期未來穩健性：完整指南與行動計畫

> **專案適用範圍：** Patron Walkaway  
> **文件目的：** 整合原文獻整理、驗證結果與補充建議，形成可執行的實施路線圖  
> **最後更新：** 2026-03-15

---

## 執行摘要

本文件整合了 Patron Walkaway 專案「預測模型近期未來穩健性」文獻整理的驗證結果與前沿補充建議。原文獻整理的**七大核心維度**已通過文獻交叉驗證，方法論紮實且具實用性。同時，本文件新增**六項前沿補充建議**（2024-2025 最新研究），涵蓋 Conformal Prediction、Test-Time Adaptation、時序基礎模型、評估指標強化、因果特徵選擇與層級對齊。

---

## 第一部分：原文獻整理驗證結果

### ✅ 已驗證的核心維度

#### 1. 多切點、多期評估（Walk-Forward Validation）

**原文獻觀點：**
- 單一切點、單一 horizon 會高估穩健性
- 需使用嚴格時間順序的 expanding/rolling window backtest
- 避免 random k-fold（違反時間順序）
- 切點應事先固定或規則化（避免 data snooping）
- 多 horizon 評估應使用 Quaedvlieg 的 Uniform SPA / Average SPA

**驗證結果：** ✅ **完全支持**
- Walk-forward 是最接近生產環境的評估方式，嚴格遵守時間因果性
- Random k-fold 確實會造成「用未來預測過去」的資訊洩漏
- 文獻一致支持 expanding window（穩定長期模式）vs rolling window（快速變化環境）的情境劃分

**補充建議：**
- 除平均誤差外，應報告**跨切點的標準差與 95 百分位數**（worst-case robustness 比平均表現更重要）
- 每個切點應報告 multi-horizon 誤差（如 MAPE/sMAPE by h=1,2,...,H）

---

#### 2. 結構性斷裂與 Regime 變化

**原文獻觀點：**
- 忽略結構性斷裂會導致近期未來表現崩潰
- 應使用 Bai–Perron、PELT、ICSS 等檢測算法
- 可採動態切換（不同 regime 用不同模型/權重）
- 貝氏方法可建模「未來可能再發生斷裂」的不確定性
- Break 日期的置信集平均優於單一點估計

**驗證結果：** ✅ **完全支持**
- 2025 研究：PELT + Wavelet + TCN 混合架構在碳價預測上降低 RMSE 22.35%、MAE 18.63%
- Bai–Perron 在無斷裂情境下有高達 88.3% 的誤判率，確認「置信集平均」的必要性

**補充建議：**
- 在 backtest 設計中**刻意跨越已知斷裂點**（如 COVID-19、政策變動），觀察模型在斷裂前後的誤差變化
- 結合 **online regime detection**（如 CUSUM、ADWIN），在生產環境中即時標記新興 regime，無需等到下一次排程 backtest

---

#### 3. 預測組合與集成

**原文獻觀點：**
- M4 競賽：12/17 頂尖方法為組合方法
- 純 ML 方法普遍不如組合
- 組合可降低單一模型誤設風險
- 可使用 FFORMA（依特徵自動權重）或簡單平均

**驗證結果：** ✅ **完全支持**
- M4 冠軍 Slawek Smyl 的 ES-RNN 是統計 + ML 的單一混合模型
- 文獻確認組合同時降低 bias（誤設）與 variance（不確定性）

**補充建議：**
- 組合權重應依**最近 N 個切點的表現**動態調整（recency-weighted combination），而非全歷史平均
- 考慮納入**時序基礎模型**（TSFMs，如 Chronos、TimesFM）作為集成成員（見第二部分補充建議 3）

---

#### 4. 預測穩定性（Vertical & Horizontal）

**原文獻觀點：**
- 垂直穩定性：同一目標日的預測不應隨 origin 往後移而劇烈修正
- 水平穩定性：同一 origin 下預測在 horizon 上不應震盪
- 過度頻繁重訓可能放大短期噪音
- 可用動態 loss 權重同時考慮誤差與穩定性

**驗證結果：** ✅ **支持**
- 穩定性對供應鏈、排程、人力配置等營運決策的信任度至關重要
- 更少重訓有時反而更穩定

**補充建議：**
- **將穩定性正式化為 KPI**：例如「同一目標日在連續兩次預測間的平均絕對修正量」
- 在 backtest 中除誤差指標外，也量測「同一目標日在不同切點下預測的變異」

---

#### 5. 概念漂移、共變量偏移與重訓策略

**原文獻觀點：**
- Covariate shift（X 分布變了）vs Concept drift（P(Y|X) 變了）
- 偵測方式：誤差監控、KS 檢定、自編碼器異常、CUSUM
- 重訓策略：事件驅動（drift 達閾值）+ 週期性檢查
- 選擇性重訓（只重訓受影響的模型/區段）可提升 ROI

**驗證結果：** ✅ **支持**
- 文獻確認固定週期重訓（如每季）既可能浪費（穩定期）也可能延遲（快速漂移）
- 基於不確定性與預期效能的動態重訓優於簡單規則

**補充建議：**
- 明確使用 **PSI（Population Stability Index）** 作為生產監控指標：
  - PSI < 0.10：穩定
  - 0.10–0.25：需關注
  - \> 0.25：顯著漂移
- 建立**兩層應對機制**（見第二部分補充建議 2）：
  1. **RevIN** 作為常設架構層（吸收小幅分布偏移）
  2. **Test-Time Adaptation（TTA）** 作為中度漂移應對（PSI 0.10–0.25）
  3. **完整重訓** 作為嚴重漂移應對（PSI > 0.25）

---

#### 6. 生產環境與 MLOps

**原文獻觀點：**
- 資料統計層檢查（常數序列、突跳、過期資料）
- 特徵品質常比模型選擇更重要
- 特徵與模型一起版本化，確保 rollback 一致性
- 訓練與 serving 一致（避免 training–serving skew）
- Staged rollout、canary deployment 降低風險

**驗證結果：** ✅ **完全支持**
- 與 Microsoft Forecasting、AWS SageMaker、TSFM 等實務建議一致
- 2024 年 AI incidents 上升 56.4%，強調監控重要性

**補充建議：**
- 除了 per-feature PSI，應加入**多變量漂移偵測**（如 MMD、Wasserstein distance），偵測特徵間聯合分布的微妙偏移
- CI/CD pipeline 應包含自動化 multi-cutpoint backtest，作為合併前的必要檢查

---

#### 7. 機率預測與區間

**原文獻觀點：**
- M4 頂尖方法在 95% 區間覆蓋上表現也好
- 評估時除覆蓋率外也應看銳度（區間寬度）

**驗證結果：** ✅ **正確但不夠深入**
- M4 確實驗證點預測與區間預測可一起優化

**補充建議：**
- 此維度的主要缺口：應採用 **Conformal Prediction**（見第二部分補充建議 1）

---

## 第二部分：六項前沿補充建議（2024-2025）

### 補充建議 1：採用 Conformal Prediction 進行不確定性量化

#### 背景
原文獻在機率預測部分著墨不深。**Conformal Prediction（CP）** 是目前理論最嚴謹的區間估計方法：
- 無需假設分布（distribution-free）
- 提供有限樣本覆蓋保證（finite-sample coverage guarantee）
- 適用任何黑箱模型

#### 關鍵技術
- **EnbPI**（Ensemble Batch Prediction Intervals, 2021）：首個針對時序資料的 CP 方法，使用 bootstrap ensemble 構造非平穩與時空資料的近似邊際覆蓋區間
- **2025 統一綜述**（arXiv:2511.13608）：整理所有當前 CP 時序方法，包括重加權校準資料、動態更新殘差分布、即時適應覆蓋水準等
- **TSFMs + CP**：因基礎模型只需少量訓練資料，可釋出更多資料用於校準集，顯著提升區間品質

#### 實施建議
✅ **Action Item 1.1：** 在 Patron Walkaway 生產模型中加入 EnbPI 或 adaptive split-conformal 作為首選不確定性量化方法  
✅ **Action Item 1.2：** 替換或補強現有的經驗分位數區間，無需重訓底層預測器即可獲得嚴格覆蓋保證  
✅ **Action Item 1.3：** 在 multi-cutpoint backtest 中同時評估覆蓋率（calibration）與銳度（sharpness）

---

### 補充建議 2：Test-Time Adaptation（TTA）作為輕量漂移應對

#### 背景
原文獻的重訓策略是「偵測到 drift → 完整重訓」。**TTA** 提供中間層選項：只更新模型中少量參數（如正規化層的仿射參數），使用最近的無標籤生產資料，無需觸發完整重訓流程。

#### 關鍵技術
- **DynaTTA**（ICML 2025）：
  - 即時估計分布偏移嚴重程度（追蹤預測誤差與 embedding drift）
  - 動態適應率與偏移嚴重度成正比
  - Shift-conditioned gating 機制避免穩定期的不必要更新
  - 模組化設計，可疊加於任何既有預訓練模型

- **RevIN**（Reversible Instance Normalization, ICLR 2022）：
  - 引用超 1,340 次
  - 使用 instance-level 統計對輸入正規化、輸出反正規化
  - 模型無關的即插即用層，顯著降低分布偏移的性能落差

#### 實施建議
✅ **Action Item 2.1：** 將 **RevIN 作為常設架構層**整合至所有時序模型  
✅ **Action Item 2.2：** 建立**兩層漂移應對機制**：
- **Tier 1（小幅偏移）：** RevIN 自動吸收
- **Tier 2（中度偏移，PSI 0.10–0.25）：** 觸發 TTA（如 DynaTTA），更新正規化參數
- **Tier 3（嚴重偏移，PSI > 0.25）：** 完整模型重訓

✅ **Action Item 2.3：** 在 backtest 中比較「固定週期重訓」vs「TTA + 事件驅動重訓」的穩健性與成本

---

### 補充建議 3：時序基礎模型（TSFMs）作為集成基準

#### 背景
原文獻的模型集中在 ARIMA/ETS、樹模型、簡單神經網路、Prophet。未涵蓋 **Time Series Foundation Models（TSFMs）**——在大規模異質時序語料上預訓練的大型模型。

#### 關鍵模型
- **Chronos**（Amazon）：Transformer-based，在多領域時序資料上預訓練
- **TimesFM**（Google）：在約 1,000 億時序資料點（Google Trends、Wikipedia pageviews）上訓練
- **評估結果**：零樣本與微調設定下降低 15–30% 預測誤差，歷史資料有限時尤為有效
- **ELF**（ICML 2025）：輕量線上適配器，可在新資料到達時增量改進 TSFM 預測，無需重訓大模型

#### 實施建議
✅ **Action Item 3.1：** 在 multi-cutpoint backtest 中加入至少一個 TSFM（建議 Chronos-Bolt 速度快，或 TimesFM 零樣本穩健性高）  
✅ **Action Item 3.2：** 將 TSFM 作為集成成員，特別用於**冷啟動場景**（新門市/產品/顧客區段，歷史資料不足）  
✅ **Action Item 3.3：** 評估 TSFM + ELF 的組合，作為低資料成本的持續適配方案

---

### 補充建議 4：強化評估指標體系

#### 背景
原文獻以 MAPE/sMAPE 為主，但兩者有已知弱點：
- **MAPE**：在近零值時未定義或爆炸
- **sMAPE**：懲罰不對稱，0–200% 範圍不直觀

#### 推薦指標矩陣

| 指標 | 優勢 | 劣勢 | 最適用場景 |
|------|------|------|-----------|
| **MASE** | 無量綱、對零值穩健、跨序列可比 | 相對於 naïve baseline 的表現 | 跨序列比較、間歇需求 |
| **sMAPE** | 無量綱百分比 | 懲罰不對稱、零值未定義 | 標準競賽基準 |
| **CRPS** | 評估完整預測分布 | 需要機率預測 | 區間與分布評估 |
| **PSI** | 偵測特徵/預測分布漂移 | 單特徵、非聯合分布 | 生產監控 |
| **Worst-case %ile** | 捕捉尾部穩健性 | 對典型表現不敏感 | 營運 SLA 設定 |

#### 實施建議
✅ **Action Item 4.1：** 採用 **MASE 作為主要無量綱準確度指標**（取代有零值序列的 MAPE）  
✅ **Action Item 4.2：** 一旦引入機率輸出，加入 **CRPS** 評估完整預測分布  
✅ **Action Item 4.3：** 追蹤生產輸入與預測的 **PSI**，在準確度下降前偵測分布漂移  
✅ **Action Item 4.4：** 報告**第 95 百分位數誤差**（worst-case robustness），作為 SLA 設定依據

---

### 補充建議 5：因果特徵選擇提升協變量穩健性

#### 背景
原文獻提及協變量特徵但未涉及選擇方法。**因果特徵選擇**使用 PCMCI（PC algorithm + Momentary Conditional Independence）等算法識別對目標有直接因果路徑的變數，而非僅相關變數。

#### 為何重要
- **穩健性**：因果特徵在 OOD（out-of-distribution）條件下更穩健，因相關性特徵可能在分布偏移時失效，而因果機制通常更穩定
- **抗過擬合**：減少擬合訓練窗內存在但斷裂後消失的虛假相關性
- **實證支持**：2025 年電力負荷預測研究顯示 PCMCI 選出的特徵在 OOD 天氣推論場景下，於 GRU、TCN、PatchTST 上均優於非因果選擇

#### 實施建議
✅ **Action Item 5.1：** 對候選外生特徵集應用因果發現（如 PCMCI，使用 `tigramite` Python 套件）  
✅ **Action Item 5.2：** 優先使用因果選出的特徵作為生產模型主要輸入  
✅ **Action Item 5.3：** 將相關性特徵作為補充輸入保留於集成成員中  
✅ **Action Item 5.4：** 在跨區段（如有行為訊號的顧客群）應用此方法時特別留意

---

### 補充建議 6：層級預測的穩健對齊（若有聚合需求）

#### 背景
若 Patron Walkaway 專案需在多個聚合層次產出預測（如個別顧客 → 區段 → 整體），原文獻未涵蓋**層級預測一致性**（確保低層預測加總等於高層預測）。

#### 關鍵技術
- **標準 MinT**（Minimum Trace reconciliation）：假設預測誤差協方差矩陣估計良好，但在校準資料有限時會失效
- **穩健最佳化框架**（2026 TMLR）：明確考慮協方差矩陣的估計不確定性，將問題建模為半正定規劃（semidefinite program），最小化 worst-case 加權殘差
- **M-estimation 對齊**：使用 Huber/LAD loss 達成對離群序列的穩健性

#### 實施建議
✅ **Action Item 6.1：** 若專案有層級聚合需求，實施穩健對齊步驟  
✅ **Action Item 6.2：** 以 MinT estimator 作為 baseline，在 multi-cutpoint backtest 中評估半正定或 M-estimation 變體  
✅ **Action Item 6.3：** 即使無層級需求，考慮 bottom-up coherence（加總細粒度預測）通常優於 top-down 方法

---

## 第三部分：實施優先級與時程建議

### 立即實施（0-1 個月）

| 項目 | 工作量 | 影響 | 風險 |
|------|--------|------|------|
| **RevIN 架構層整合** | 低 | 中-高 | 低 |
| **MASE 作為主要指標** | 低 | 中 | 低 |
| **PSI 生產監控** | 低-中 | 高 | 低 |
| **Multi-cutpoint backtest 自動化** | 中 | 高 | 低 |
| **Worst-case percentile 報告** | 低 | 中 | 低 |

### 短期實施（1-3 個月）

| 項目 | 工作量 | 影響 | 風險 |
|------|--------|------|------|
| **TTA（DynaTTA）中度漂移應對** | 中 | 高 | 中 |
| **TSFM（Chronos/TimesFM）集成** | 中 | 中-高 | 中 |
| **Conformal Prediction（EnbPI）** | 中 | 高 | 低-中 |
| **CRPS 評估機率預測** | 低 | 中 | 低 |
| **Streaming break detection** | 中 | 中 | 中 |

### 中期實施（3-6 個月）

| 項目 | 工作量 | 影響 | 風險 |
|------|--------|------|------|
| **PCMCI 因果特徵選擇** | 中-高 | 中-高 | 中 |
| **Multi-variate drift（MMD/Wasserstein）** | 中 | 中 | 中 |
| **穩定性 KPI 正式化** | 低-中 | 中 | 低 |
| **Forecast combination recency weighting** | 低-中 | 中 | 低 |

### 長期實施（6+ 個月）

| 項目 | 工作量 | 影響 | 風險 |
|------|--------|------|------|
| **層級穩健對齊（若需要）** | 高 | 高（若有層級） | 中-高 |
| **Regime-conditioned ensemble** | 高 | 高 | 高 |
| **Self-Adaptive Forecasting（SAF）** | 中-高 | 中 | 中 |

---

## 第四部分：評估框架完整 Checklist

### 評估設計

- [ ] **時間順序嚴格保持**（無 random k-fold）
- [ ] **Multi-cutpoint backtest**（至少 5-10 個歷史切點）
- [ ] **Multi-horizon 評估**（h=1,2,...,H，每個 horizon 單獨報告）
- [ ] **Expanding vs Rolling window**（依據資料特性選擇）
- [ ] **切點事先固定或規則化**（避免 data snooping）
- [ ] **刻意跨越已知斷裂點**（如 COVID-19、政策變動）

### 評估指標

**準確度：**
- [ ] **MASE**（主要無量綱指標）
- [ ] sMAPE（次要，競賽基準）
- [ ] RMSE（若需要絕對尺度）
- [ ] **95 百分位數誤差**（worst-case robustness）

**機率預測：**
- [ ] **CRPS**（完整分布評估）
- [ ] **覆蓋率 + 銳度**（calibration + sharpness）

**穩定性：**
- [ ] **垂直穩定性**（同一目標日連續預測的平均絕對修正）
- [ ] **水平穩定性**（相鄰 horizon 預測的平滑度）

**生產監控：**
- [ ] **PSI**（特徵與預測分布漂移）
- [ ] **MMD/Wasserstein**（多變量聯合分布偏移）

### 模型組合

- [ ] 統計方法（ARIMA/ETS）
- [ ] 樹模型（XGBoost/LightGBM）
- [ ] 神經網路（LSTM/GRU/TCN）
- [ ] Prophet（業務日曆）
- [ ] **TSFM**（Chronos/TimesFM，零樣本基準）
- [ ] **Recency-weighted combination**（最近 N 個切點的表現）

### 特徵工程

- [ ] Lag features
- [ ] Rolling aggregations
- [ ] 日曆特徵（月、週、假日）
- [ ] 業務指標
- [ ] **PCMCI 因果特徵選擇**（優先使用因果特徵）

### 架構增強

- [ ] **RevIN 正規化層**（吸收分布偏移）
- [ ] **Conformal Prediction 區間**（EnbPI）
- [ ] **TTA 模組**（DynaTTA，中度漂移應對）

### 重訓策略

- [ ] **PSI 監控**（< 0.10 穩定；0.10–0.25 關注；> 0.25 嚴重）
- [ ] **兩層應對機制**：
  - Tier 1: RevIN 自動吸收
  - Tier 2: TTA 觸發（PSI 0.10–0.25）
  - Tier 3: 完整重訓（PSI > 0.25）
- [ ] **Streaming break detection**（CUSUM/ADWIN）
- [ ] **選擇性重訓**（只重訓受影響的模型/區段）

### 生產 MLOps

- [ ] 特徵與模型共版本化
- [ ] 訓練-serving 一致性檢查
- [ ] 資料品質統計監控（常數序列、突跳、過期資料）
- [ ] Staged rollout / Canary deployment
- [ ] CI/CD 自動化 multi-cutpoint backtest
- [ ] 健康 / SLA 監控
- [ ] Artifact 溯源（可追溯至特定訓練切點與特徵版本）

---

## 第五部分：技術堆疊建議

### Python 套件

**評估與 Backtest：**
- `skforecast`：Walk-forward backtesting
- `sktime`：時序模型統一介面、MASE 等指標

**因果發現：**
- `tigramite`：PCMCI 因果特徵選擇

**Conformal Prediction：**
- `MAPIE`：包含 EnbPI、split conformal 等方法

**時序基礎模型：**
- `chronos-forecasting`（Amazon）
- `timesfm`（Google，需 TensorFlow）

**Drift 偵測：**
- `evidently`：PSI、MMD、data drift 監控
- `alibi-detect`：CUSUM、ADWIN 等 streaming detection

**RevIN：**
- 自行實作（PyTorch/TensorFlow，約 20-30 行）
- 參考：https://github.com/ts-kim/RevIN

**結構斷裂：**
- `ruptures`：Bai–Perron、PELT、ICSS 等算法

### 監控與 MLOps

- **Evidently AI**：Drift 監控、model quality reports
- **MLflow / Weights & Biases**：Experiment tracking、model versioning
- **Grafana + Prometheus**：生產指標監控（PSI、MASE、穩定性 KPI）
- **Great Expectations**：資料品質檢查

---

## 第六部分：風險與注意事項

### 已知限制

1. **RevIN 限制**：無法處理條件輸出分布偏移或空間異質性（2025 研究中）
2. **TSFM 成本**：推論時間與記憶體需求較傳統模型高（需評估生產環境可行性）
3. **Conformal Prediction**：覆蓋保證僅限於校準分布；若生產分布劇變仍需重校準
4. **因果發現**：PCMCI 假設線性或非線性加法雜訊模型；強非線性因果關係可能遺漏

### 成本效益權衡

| 技術 | 實施成本 | 維護成本 | 預期收益 | 建議 |
|------|---------|---------|---------|------|
| RevIN | 極低 | 極低 | 中-高 | ✅ 立即實施 |
| PSI 監控 | 低 | 低 | 高 | ✅ 立即實施 |
| MASE 指標 | 極低 | 極低 | 中 | ✅ 立即實施 |
| TTA（DynaTTA） | 中 | 中 | 高 | ✅ 短期實施 |
| Conformal Prediction | 中 | 低-中 | 高 | ✅ 短期實施 |
| TSFM 集成 | 中 | 中-高 | 中-高 | ⚠️ 評估後決定 |
| PCMCI 因果選擇 | 中-高 | 中 | 中-高 | ⚠️ 中期實施 |
| 層級穩健對齊 | 高 | 中 | 高（若有層級） | ⚠️ 視需求決定 |

---

## 第七部分：成功指標與驗收標準

### 短期目標（3 個月）

- [ ] Multi-cutpoint backtest 自動化（5-10 個歷史切點）
- [ ] MASE 成為主要評估指標
- [ ] PSI 生產監控上線（輸入與預測）
- [ ] RevIN 整合至所有模型
- [ ] Worst-case percentile 誤差報告

### 中期目標（6 個月）

- [ ] TTA 兩層漂移應對機制上線
- [ ] TSFM 納入集成（至少一個）
- [ ] Conformal Prediction 區間估計上線
- [ ] CRPS 評估機率預測
- [ ] 穩定性 KPI 正式追蹤

### 長期目標（12 個月）

- [ ] PCMCI 因果特徵選擇於所有新模型
- [ ] Multi-variate drift 監控（MMD）
- [ ] Regime-conditioned ensemble（若資料支持）
- [ ] 完整 MLOps pipeline（CI/CD、staged rollout、artifact 溯源）

### 量化目標

| 指標 | Baseline | 3 個月目標 | 6 個月目標 | 12 個月目標 |
|------|---------|-----------|-----------|------------|
| **MASE（avg across folds）** | [待定] | -5% | -10% | -15% |
| **95%ile error** | [待定] | -10% | -15% | -20% |
| **垂直穩定性（avg revision）** | [待定] | -20% | -30% | -40% |
| **Drift detection latency** | 週 | 天 | 小時 | 即時 |
| **False retraining rate** | [待定] | -30% | -50% | -70% |

---

## 總結：核心行動計畫

### 立即行動（本週）

1. ✅ 將 RevIN 整合為所有模型的標準架構層
2. ✅ 切換主要評估指標為 MASE（取代 MAPE）
3. ✅ 設定 PSI 生產監控儀表板（特徵 + 預測）

### 短期行動（本月）

4. ✅ 實施 multi-cutpoint backtest 自動化
5. ✅ 報告 worst-case percentile 誤差
6. ✅ 開始 TTA（DynaTTA）技術驗證

### 中期行動（本季）

7. ✅ 評估 Chronos/TimesFM 作為集成基準
8. ✅ 實施 Conformal Prediction（EnbPI）區間估計
9. ✅ 上線兩層漂移應對機制（RevIN + TTA + 重訓）

### 持續優化

10. ✅ 每季評估新前沿方法（TSFM、因果發現、層級對齊）
11. ✅ 每月檢視穩健性指標趨勢（MASE、穩定性、PSI）
12. ✅ 每次重大斷裂後進行 post-mortem（模型在斷裂前後的表現）

---

**文件版本：** 1.0  
**最後更新：** 2026-03-15  
**維護者：** Patron Walkaway 專案團隊  
**下次檢視：** 2026-06-15（3 個月後）