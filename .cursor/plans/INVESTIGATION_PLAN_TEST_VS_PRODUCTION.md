# Test vs Production 性能落差 — 正式調查計畫

> **目的**：針對「test set 與 production 間出現巨大性能落差」建立結構化根因清單與調查步驟，供依序排查與記錄結論。
>
> **相關**：DEC-030（Validator–Trainer 標籤／常數對齊，已完成）、Phase 2 P1.1（prediction log 寫入 MLflow）、ssot/phase2_p0_p1_ssot.md（precision@recall=1% 監控）。

---

## 1. 範圍與前置條件

### 1.1 調查範圍

- **現象**：訓練／回測的 test 指標（例如 `test_precision_at_recall_0.01`、test precision/recall）與 production 端觀測到的表現有明顯落差。
- **目標**：識別根因（閾值、指標口徑、label、特徵／資料 parity、分佈漂移等），並以可重現方式驗證或排除。

### 1.2 調查前置條件

以下具備後，調查才能完整進行：

| 條件 | 說明 | 對應計畫 |
|------|------|----------|
| Prediction log | Scorer 寫出每筆預測（bet_id, score, ts, model_version 等）至獨立 SQLite 或 Parquet | P1.1（T4） |
| 離線標註或 below-threshold 抽樣 | 能對「未 alert」樣本取得 true label（與 validator 同一套邏輯），以計算嚴謹 recall / precision@recall | 先前討論：離線用 validator 邏輯對全量或抽樣標註 |
| DEC-030 已部署 | Validator 常數來自 config、僅 bet-based，與 trainer/labels.py 一致 | doc/validator_trainer_parity_plan.md |

若 prediction log 尚未上線，仍可先進行「指標口徑釐清」「label 一致性比對」「特徵／資料 parity」等項目的調查。

---

## 2. 根因清單（風險項）

以下為可能導致 test vs production 性能落差的根因，每項附編號、簡述與調查方式。調查時建議依序進行，並在 STATUS.md 或本文件末尾記錄「已排查／已確認／已排除」。

---

### R1. 閾值固定、無動態校準

**問題**  
閾值在 offline（validation / backtester）上選「recall ≥ 1%、precision 最大」後寫入 artifact；production 沿用同一閾值，**不做即時或定期校準**。真實流量、時段、玩家組成與歷史不同時，同一閾值在 production 對應的 recall 可能偏離 1%（例如 0.5% 或 2%），precision 隨之偏離 test 所見。

**調查方式**

1. 使用 **prediction log** ＋ **離線標註**（或 below-threshold 抽樣）還原 production 的 (score, label)。
2. 繪製 **PR 曲線**，讀取「**目前固定閾值**」在 production 對應的 **實際 recall** 與 **precision**。
3. 在 PR 曲線上取「**recall ≈ 1%**」的閾值，記錄該點的 precision。
4. 與 test 的 `test_precision_at_recall_0.01` 比較：若「同一閾值下 prod recall 明顯 ≠ 1%」或「recall=1% 時 prod precision 明顯低於 test」，可歸因於閾值／分佈，再評估是否需動態校準或重選閾值。

**依賴**：Prediction log、離線標註或 below-threshold 抽樣。

---

### R2. 指標口徑不一致

**問題**  
- **Test**：報告的是 **precision@recall=1%**（PR 曲線上、recall ≥ 1% 時的最大 precision）。  
- **Production**：Validator 僅報告「目前這批 alert 中 MATCH 的比例」＝在**當前實際 recall** 下的 precision，**不是**「在 recall=1% 那一點」的 precision。  
兩者不可直接比較；若誤比會以為 production 變差，實則量的是不同指標。

**調查方式**

1. 在文件／runbook 中明確定義：test 指標 = precision@recall=0.01；production Validator = precision at current threshold（current recall）。
2. 使用 prediction log ＋ 離線 label，在 **production 資料**上計算：  
   - 同一閾值下的 (precision, recall)；  
   - **precision@recall=1%**（在 PR 曲線上取點）。
3. 比較時採用 **同口徑**：要么都比「同一閾值下的 P/R」，要么都比「recall=1% 時的 precision」。

**依賴**：Prediction log、離線標註。

---

### R3. Label 定義或覆蓋範圍不一致

**問題**  
- **Trainer**：label 來自 `trainer/labels.py`（bet-based、config 常數）。  
- **Validator**：DEC-030 後改為 bet-based ＋ config；若 **尚未部署** 或部分環境仍用舊版，同一筆 bet 可能得到不同 label（1 vs 0，或 MATCH vs MISS）。  
- 此外，Validator 只對「**有被 alert 的**」給 label → 僅有 **TP/FP**，沒有 **FN/TN**；若僅用「已 alert 樣本」估計指標，會高估 precision、無法得到真實 recall。

**調查方式**

1. 確認 **DEC-030 已上線**（validator 常數來自 config、僅 bet-based）；若未上線，先部署再比較。
2. 在 **同一批 bet** 上：以 trainer 的 `compute_labels` 與 validator 的邏輯（或離線複用同一套）各算一次 label，**比對不一致率**，並檢查是否集中在邊界（例如 gap 接近 30 min）。
3. 若要嚴謹計算 recall / precision@recall：必須取得 **FN**（與可選 TN）→ 透過 **below-threshold 抽樣驗證** 或 **全量離線標註**（與 validator 同一套邏輯），再與 prediction log 合併計算 PR。

**依賴**：DEC-030 部署狀態、必要時離線標註或 below-threshold 抽樣。

---

### R4. 特徵或資料管線在 train 與 serve 不一致

**問題**  
- **Profile**：trainer 以 `window_start−365d`～`window_end` 多筆 snapshot ＋ PIT join；scorer 以「每 canonical_id 最新一筆 snapshot ≤ as_of_dtm」＋ **1h TTL cache**。若 ETL 或 cache 時機不同，同一玩家在 train 與 prod 的 profile 特徵可能不同 → score 漂移。  
- **Rated / canonical**：trainer 以 full-history 或 chunk 建 `canonical_map`；scorer 可從 `canonical_mapping.parquet`（含 cutoff）或當窗重建。若 parquet 過期或重建時機不同，**誰是 rated**、**canonical_id 對應**可能與訓練時不一致 → 漏評或評錯對象。  
- **資料來源與時序**：trainer 多用 DuckDB/Parquet；production 用 `fetch_recent_data`（ClickHouse、FND-01、session_avail_dtm）。若延遲或可用性規則不同，同一「時間點」看到的 bets/sessions 可能不同 → 特徵與 label 時序不一致。

**調查方式**

1. **Profile**：對同一 `(canonical_id, as_of_dtm)` 比對「訓練／backtest 用到的 profile 快照」與「scorer 在該時間會用到的快照」是否一致；必要時在 scorer 加 log（snapshot_dtm、profile 來源）以便對照。
2. **Rated / canonical**：以與 production **相同**的 `canonical_mapping.parquet` 與 cutoff 邏輯跑一次 backtest 或離線重放，觀察 metrics 是否更接近 production。
3. **資料窗與 FND-01**：對同一時間窗，以相同 FND-01／session 過濾在「訓練用 pipeline」與「fetch_recent_data」各跑一次，比對 bet/session 筆數與關鍵欄位是否一致。

**依賴**：Backtest／離線重放能力、必要時 scorer 日誌或除錯輸出。

---

### R5. 時間與分佈漂移（temporal / distribution shift）

**問題**  
Test set 為**過往一段時間**的靜態切分；production 為**即時串流**。時段、星期、季節、活動、玩家 mix 不同 → **score 分佈**與 **positive rate** 不同 → 同一閾值在 prod 對應的 recall/precision 與 test 不同。

**調查方式**

1. 使用 prediction log 繪製 production 的 **score 分佈**（over time、by 時段／星期），與 test set 的 score 分佈比較；若有 Evidently 等工具，可做 **score distribution drift**。
2. 分 **時段／群體**（例如 by 桌、by 時段）計算 precision/recall，檢視是否特定時段或群體明顯較差。
3. 以多個時間窗跑 **backtest**（不同 6h 窗、不同星期），觀察 precision@recall=1% 的 **變異**；若變異大，表示單一 backtest 窗代表性不足，應以 production log 為準再驗證。

**依賴**：Prediction log、必要時 Evidently 或自訂分佈比較腳本。

---

### R6. Production 無法直接量「真實 recall」與「precision@recall=1%」

**問題**  
在沒有全體 (score, label) 的情況下，無法在 production 計算真實 recall 或 precision@recall=1%，僅能觀察「當前閾值下的 precision」。若以「僅 alert 樣本」估計，會漏掉 FN，高估 recall 或得到偏誤的 PR。

**調查方式**

1. 落實 **P1.1 prediction log** ＋ **離線標註**（或 below-threshold 抽樣），在 **同一批 production 資料**上計算：真實 recall、precision、以及 **precision@recall=1%**。
2. 與 test 的 `test_precision_at_recall_0.01`、`test_recall` 做 **同口徑比較**（見 R2）。

**依賴**：Prediction log、離線標註或 below-threshold 抽樣。

---

### R7. Backtest 視窗代表性不足

**問題**  
閾值若以 **backtest 單一 6h 窗**選出，而該 6h 較「好預測」或時段特殊，選出的閾值在 live 可能偏樂觀。

**調查方式**

1. 以 **多個時間點與長度**跑 backtest（例如多個 6h、不同星期／時段），觀察 precision@recall=1% 的 **分佈與變異**。
2. 若變異大，結論為「單一 backtest 窗不足以代表 production」，應以 **prediction log ＋ 離線指標** 為準進行閾值與監控決策。

**依賴**：Backtester、多窗執行能力。

---

### R8. 校準與閾值 fallback（uncalibrated）

**問題**  
Trainer 在 **沒有 validation set** 時會使用 **threshold=0.5** fallback，並設定 `test_threshold_uncalibrated`。若 scores 未做 calibration（例如 Platt scaling），同一閾值在不同環境可能對應不同實際 precision/recall。

**調查方式**

1. 檢查 artifact 與訓練 log：是否出現 **uncalibrated threshold** 或 **test_threshold_uncalibrated=True**。
2. 若有，視為高風險：該次訓練的閾值不宜直接與「有 validation 的 run」或 production 數字比較。
3. 可選：在 test 與 production 樣本上繪製 **reliability diagram** 或進行 calibration 評估，確認 score 是否 well-calibrated。

**依賴**：Artifact 與訓練 log、可選的 calibration 評估腳本。

---

## 3. 根因與調查方式總表

| 編號 | 根因（風險項） | 調查方式摘要 |
|------|----------------|--------------|
| R1 | 閾值固定、無動態校準 | Prediction log ＋ 離線 label → PR 曲線；看固定閾值對應的 prod recall；看 recall=1% 時 precision 是否接近 test。 |
| R2 | 指標口徑不一致（precision@recall=1% vs 當前閾值 precision） | 明確定義兩邊指標；在 prod 上算同口徑（同一閾值 P/R，或 recall=1% 的 precision）再比較。 |
| R3 | Label 定義／覆蓋不一致（trainer vs validator；僅 TP/FP 無 FN） | 確認 DEC-030 已部署；同批 bet 比對 trainer label 與 validator 結果；用 below-threshold 抽樣或全量標註補 FN 再算 PR。 |
| R4 | 特徵／資料管線 parity（profile、rated、資料源） | 比對 profile 快照、canonical/rated 來源、同一時間窗下 FND-01 輸出是否一致。 |
| R5 | 時間／分佈漂移 | Score 分佈比較（test vs prod、by 時段）；分群算 P/R；多窗 backtest 看指標變異。 |
| R6 | Production 無法直接算 recall／precision@recall=1% | 落實 prediction log ＋ 離線標註，在 prod 資料上還原 PR 與嚴謹指標。 |
| R7 | Backtest 視窗代表性不足 | 多時間窗 backtest，看 precision@recall=1% 變異；必要時以 prod log 為準。 |
| R8 | Uncalibrated 閾值 fallback | 檢查 artifact／log 的 uncalibrated 旗標；必要時做 calibration 評估。 |

---

## 4. 建議調查順序

1. **R2（指標口徑）**：先釐清 test 與 production 各自量的是什麼，避免誤比。
2. **R8（uncalibrated）**：快速檢查 artifact／log，排除 fallback 閾值造成的假落差。
3. **R3（label 一致性）**：確認 DEC-030 已部署，並在樣本上比對 trainer 與 validator 的 label。
4. **R1、R6（閾值與嚴謹指標）**：在具備 prediction log ＋ 離線標註後，還原 production PR、與 test 同口徑比較。
5. **R4（特徵／資料 parity）**：若 R1/R3 無法解釋落差，再查 profile、rated、資料源與 FND-01。
6. **R5、R7（分佈與 backtest 代表性）**：分時段／群體與多窗 backtest，評估分佈與視窗代表性。

---

## 5. 調查結果記錄（待填）

完成各項調查後，可於此節或 STATUS.md 記錄結論，例如：

| 編號 | 狀態 | 結論摘要 |
|------|------|----------|
| R1 | 待調查 / 已確認 / 已排除 | （填寫） |
| R2 | … | … |
| … | … | … |

---

**文件版本**：初版  
**最後更新**：依 .cursor/plans 慣例由執行者更新。
