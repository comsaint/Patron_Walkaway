# Test vs Production 性能落差 — 正式調查計畫

> **目的**：針對「test set 與 production 間出現巨大性能落差」建立結構化根因清單與調查步驟，供依序排查與記錄結論。
>
> **相關**：DEC-030（Validator–Trainer 標籤／常數對齊，已完成）、Phase 2 P1.1（prediction log 由 scorer 寫入 **SQLite**，可再經 export 匯出至 MLflow）、ssot/phase2_p0_p1_ssot.md（precision@recall=1% 監控）。

---
快速入口：首次調查從 §0 開始；只查某根因直接跳 §2 對應編號；執行順序見 §4；記錄結果填 §5。
---

## 0. 關鍵先決確認（調查前必做）

**在所有分析開始前**，必須先確認以下三點：

1. **Prediction log 是否在寫入**：查詢 `prediction_log` 表（或 `PREDICTION_LOG_DB_PATH` 指向的 SQLite）的 **最新 `scored_at`** 時間戳，確認與當前時間的差距在預期內（例如數分鐘內有新增）。若未設定或路徑無效，**R1、R2、R6 無法推進**，應優先修復。
2. **Production config — prediction log**：確認 **`PREDICTION_LOG_DB_PATH`** 在 production 環境中**已設定且非空**；scorer 僅在該路徑有效時寫入 prediction log。
3. **Production config — data 路徑**：確認 **`DATA_DIR`** 在 production 環境中**已設定**，且其下的 **`player_profile.parquet`** 與 **`canonical_mapping.parquet`** 均為 production 預期之最新版本。若 `DATA_DIR` 未設定，scorer 會 fallback 到 `PROJECT_ROOT/data/`，可能為開發路徑而非 production，導致 R4 的 profile 與 canonical mapping 使用錯誤版本。

建議執行方式：在 production 或可讀取 production 的環境執行查詢（例如 `SELECT MAX(scored_at) FROM prediction_log`），並檢查 config 或環境變數中的 `PREDICTION_LOG_DB_PATH` 與 `DATA_DIR`，以及上述 parquet 檔的更新時間或版本。

---

## 1. 範圍與前置條件

### 1.1 調查範圍

- **現象**：訓練／回測的 test 指標（例如 `test_precision_at_recall_0.01`、test precision/recall）與 production 端觀測到的表現有明顯落差。
- **目標**：識別根因（閾值、指標口徑、label、特徵／資料 parity、分佈漂移等），並以可重現方式驗證或排除。

### 1.2 調查前置條件

以下具備後，調查才能完整進行：

| 條件 | 說明 | 對應計畫 |
|------|------|----------|
| Prediction log **已啟用且持續寫入** | **程式碼已有實作**：`trainer/serving/scorer.py` 的 `_append_prediction_log()` 會寫入 bet_id, score, margin, model_version, is_rated_obs, scored_at 等至獨立 SQLite（P1.1 T4）。調查前須確認 **`PREDICTION_LOG_DB_PATH`** 在 production 已正確設定且持續寫入，而非視為待建功能。 | P1.1（T4） |
| 離線標註或 below-threshold 抽樣 | 能對「未 alert」樣本取得 true label，以計算嚴謹 recall / precision@recall；**具體執行方案**見下方 §1.3。 | 見 §1.3 |
| DEC-030 已部署 | Validator 常數來自 config、僅 bet-based，與 trainer/labels.py 一致 | doc/validator_trainer_parity_plan.md |

若 prediction log 路徑未設定或尚未寫入，仍可先進行「指標口徑釐清」「label 一致性比對」「特徵／資料 parity」「時區一致性」等項目的調查。

### 1.3 離線標註（below-threshold 抽樣）的具體執行方案

R1、R3、R6 依賴「離線標註或 below-threshold 抽樣」取得 FN（及可選 TN）；validator 僅對**已寫入 alerts 的樣本**（TP/FP）做回顧，無法直接用於 below-threshold 樣本。以下為可執行的標註路徑，使上述根因調查不停留在抽象描述：

1. **取得 below-threshold 候選**：從 prediction log 取出 **`is_alert=0`** 的列，取得 **bet_id**、**canonical_id**（及 **scored_at** 供時間窗邊界）。**僅取 `is_rated_obs=1` 的列**作為 below-threshold 候選——scorer 中 unrated（`is_rated_obs=0`）的 bets 不會被送入模型，其 `score` 可能為空或 null，納入 PR 曲線會引入大量缺失值噪音；限制為 rated 可確保離線標註的樣本母體與模型實際評分的樣本一致。
2. **取得 bet 完整資料**：針對該批 bet_id，查詢對應的 **bets**（含 `payout_complete_dtm` 等欄位），組成與訓練時相同格式的 bet stream。**資料來源選擇依調查目的**：若用於 **R1/R6**（production PR 曲線、驗證 production 真實 recall），優先用 **ClickHouse（production 同源）**，確保 bet stream 與 scorer 看到的相同；若用於 **R3**（label 邏輯一致性比對），優先用 **訓練用 Parquet 或訓練時同源資料**，確保與 `compute_labels()` 歷史輸入一致。
3. **呼叫與訓練一致的 label 邏輯**：對該 bet stream 呼叫 **`trainer/labels.py`** 的 **`compute_labels()`**，使用與訓練時相同的 config（**WALKAWAY_GAP_MIN**、**ALERT_HORIZON_MIN**、**LABEL_LOOKAHEAD_MIN** 及 **extended_end** 等），得到每筆 bet 的 true label（0/1）。
4. **與 prediction log 合併**：將上述 (bet_id, label) 與 prediction log 的 (bet_id, score, …) join，即得 below-threshold 樣本的 (score, label)，再與 alerts 對應的 TP/FP 合併即可繪製完整 PR 曲線、計算嚴謹 recall 與 precision@recall。

若僅做**抽樣**：**分層依據依 R8 結論選擇**——若 R8 確認 score 已校準（非 uncalibrated fallback），優先用 **score 分層抽樣**（例如等距分 5–10 個 score 段各取樣），使 PR 曲線各段有足夠代表；若 R8 判定為 **uncalibrated**（例如 threshold=0.5 fallback），score 分層的統計意義有限，改採 **隨機抽樣** 或依 **bet 時間窗分層**，統計上仍無偏。抽樣時自 `is_alert=0` 且 `is_rated_obs=1` 的列中選取，再對抽中 bet_id 執行步驟 2–4，以較低成本估計 FN 率與 precision@recall。

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
4. **is_alert 與 alerts 表不對稱**：`_append_prediction_log()` 的 `is_alert` 為 `margin >= 0 AND is_rated_obs == 1`；Validator 觀察的是 `state.db` 的 **alerts 表**，其前有 **duplicate suppression**（`alert_history`），故 **prediction_log 中 is_alert=1 的筆數可能多於實際寫入 alerts 的筆數**。若直接用 prediction_log 的 is_alert 估算 production alert 量，會**高估 alert 數、低估 precision**。調查時應**交叉核對** prediction_log 與 alerts 表的記錄數（同一時間窗），避免口徑混用。

**依賴**：Prediction log、離線標註。

---

### R3. Label 定義或覆蓋範圍不一致

**問題**  
- **Trainer**：label 來自 `trainer/labels.py`（bet-based、config 常數，G3 穩定排序與 trainer/scorer 一致）。  
- **Validator**：DEC-030 後改為 bet-based ＋ config；若 **尚未部署** 或部分環境仍用舊版，同一筆 bet 可能得到不同 label（1 vs 0，或 MATCH vs MISS）。  
- **邊界情況**：`labels.py` 對 **censored=True 的 terminal bet**（觀察窗內無下一筆 bet）有特殊處理；若 validator **未同樣排除或對齊 censored 樣本**，會使 precision 估算產生偏差，應列為比對重點。  
- 此外，Validator 只對「**有被 alert 的**」給 label → 僅有 **TP/FP**，沒有 **FN/TN**；若僅用「已 alert 樣本」估計指標，會高估 precision、無法得到真實 recall。

**調查方式**

1. 確認 **DEC-030 已上線**（validator 常數來自 config、僅 bet-based）；若未上線，先部署再比較。
2. 在 **同一批 bet** 上：以 trainer 的 `compute_labels` 與 validator 的邏輯（或離線複用同一套）各算一次 label，**比對不一致率**，並檢查是否集中在邊界（例如 gap 接近 30 min）。
3. **明確納入 censored（terminal bet）比對**：確認 validator 對「觀察窗內無下一筆 bet」的樣本處理是否與 `labels.py` 一致；不一致時記錄對 precision 的影響。**【可提前執行，不依賴 prediction log】**——只需一批 production bets（可自 ClickHouse 或 Parquet 取得）與 `labels.py` 即可進行，對應 §4 第 3 步。
4. 若要嚴謹計算 recall / precision@recall：必須取得 **FN**（與可選 TN）→ 透過 **below-threshold 抽樣驗證** 或 **全量離線標註**（與 validator 同一套邏輯），再與 prediction log 合併計算 PR。**【需 prediction log，見 §1.3】**——對應 §4 第 6 步。

**依賴**：DEC-030 部署狀態；步驟 3 僅需 bets 與 labels.py；步驟 4 需 prediction log 與 §1.3 離線標註。

---

### R4. 特徵或資料管線在 train 與 serve 不一致

**問題**  
- **Profile**：trainer 以 `window_start−365d`～`window_end` 多筆 snapshot ＋ PIT join；scorer 以「每 canonical_id 最新一筆 snapshot ≤ as_of_dtm」＋ **1h TTL cache**。**子項：TTL 快取期間內若 profile 已更新（例如 ETL 重跑），scorer 仍使用舊快照**，而 trainer 的 PIT join 是以每筆 bet 的 `payout_complete_dtm` 為準做 `merge_asof`，兩者可能不一致 → score 漂移。  
- **Rated / canonical**：trainer 以 full-history 或 chunk 建 `canonical_map`；scorer 在 `DATA_DIR` 已設定時會**無條件使用**持久化的 `canonical_mapping.parquet`（use_persisted=True），即使 cutoff 已過時。未執行 `--rebuild-canonical-mapping` 時，**被判定為 rated 的玩家集合可能與訓練時不一致** → 漏評或評錯對象。  
- **資料來源與時序**：trainer 多用 DuckDB/Parquet；production 用 `fetch_recent_data`（ClickHouse、FND-01、session_avail_dtm）。若延遲或可用性規則不同，同一「時間點」看到的 bets/sessions 可能不同 → 特徵與 label 時序不一致。

**調查方式**

1. **Profile**：對同一 `(canonical_id, as_of_dtm)` 比對「訓練／backtest 用到的 profile 快照」與「scorer 在該時間會用到的快照」是否一致。長期建議在 scorer 日誌中加入 **`profile_snapshot_dtm`**（尚未實作前見下）。**臨時驗證路徑（不依賴 scorer 改動）**：從 prediction log 取出某個 `canonical_id` 的 `scored_at`，再直接讀取 **`player_profile.parquet`**（§0 確認的 DATA_DIR 或 fallback 路徑），找出該時間點前該玩家的最新 **`snapshot_dtm`**，與訓練時 PIT join 結果手動比對；可先以此方式驗證 TTL 期間內「profile 已更新仍用舊快照」的影響，無需等待 profile_snapshot_dtm 上線。
2. **Rated / canonical — canonical_mapping 過期**：**具體驗證點**：比對 `canonical_mapping.cutoff.json` 的 **`cutoff_dtm`** 與**最近一次訓練的 `window_end`**，確認差距是否在可接受範圍（例如數小時內）。此項可作為 R4 前的**獨立快速驗證**（約 5 分鐘）。
3. **Rated / canonical（續）**：以與 production **相同**的 `canonical_mapping.parquet` 與 cutoff 邏輯跑一次 backtest 或離線重放，觀察 metrics 是否更接近 production。
4. **資料窗與 FND-01**：對同一時間窗，以相同 FND-01／session 過濾在「訓練用 pipeline」與「fetch_recent_data」各跑一次，比對 bet/session 筆數與關鍵欄位是否一致。**判斷準則**：`fetch_recent_data` 對 bets 套用 **BET_AVAIL_DELAY_MIN**（預設 1 分鐘）、對 sessions 套用 **SESSION_AVAIL_DELAY_MIN**（預設 15 分鐘）延遲門檻，預期兩邊筆數差距**僅反映此二 delay 的邊界效應**；**若差距遠超邊界效應（例如超過 5%）**，視為管線不一致，需釐清訓練與 production 的過濾條件是否對齊。

**依賴**：Backtest／離線重放能力、必要時 scorer 日誌或除錯輸出（含 profile_snapshot_dtm）。

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

1. 落實 **prediction log**（確認 §0 先決）＋ **離線標註**（或 below-threshold 抽樣），在 **同一批 production 資料**上計算：真實 recall、precision、以及 **precision@recall=1%**。
2. 與 test 的 `test_precision_at_recall_0.01`、`test_recall` 做 **同口徑比較**（見 R2）。
3. 使用 prediction_log 估算 alert 量或 precision 時，須注意 **R2 步驟 4** 的 is_alert 與 alerts 表不對稱，必要時以 alerts 表筆數交叉核對，避免高估 alert 數、低估 precision。

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

### R9. 時區轉換一致性（payout_complete_dtm）

**問題**  
`trainer/labels.py` 的 `compute_labels()` 在函數內將時間轉為 **tz-naive HK local time** 後做比較；scorer 的 `build_features_for_scoring()` 也做相同 tz-naive 轉換，但 `fetch_recent_data()` 取回的 bets 在 ClickHouse 路徑是先 **tz_localize(HK_TZ)**，再在 `build_features_for_scoring` 內 **tz_convert(HK_TZ).tz_localize(None)**。**若 ClickHouse 回傳的原始時間戳已帶 UTC offset（而非 naive）**，對同一欄位重複 localize 會累加偏移，導致 **整點偏移（例如 8 小時）**，對 session rolling windows（5m/15m/30m）會造成嚴重計算錯誤，並使 bets 在邊界窗口被分入錯誤窗口。

**調查方式**

1. 對 **同一批 bets**，在 trainer 資料源（DuckDB/Parquet）與 scorer 資料源（ClickHouse 經 `fetch_recent_data`）各取 `payout_complete_dtm`（或等同欄位）。
2. 依雙方實際轉換邏輯還原為 **tz-naive HK** 後比對數值是否相符。
3. **診斷門檻**：**若有整點偏移（例如 8 小時），視為高風險，需立即修正轉換鏈**；偏差在數分鐘內可先記錄，再評估是否影響 rolling 特徵與 label 時序。

**依賴**：可取得同一時間窗的 trainer 與 production 資料樣本。

---

## 3. 根因與調查方式總表

| 編號 | 根因（風險項） | 調查方式摘要 |
|------|----------------|--------------|
| R1 | 閾值固定、無動態校準 | Prediction log ＋ 離線 label → PR 曲線；看固定閾值對應的 prod recall；看 recall=1% 時 precision 是否接近 test。 |
| R2 | 指標口徑不一致（precision@recall=1% vs 當前閾值 precision）；prediction_log is_alert vs alerts 表筆數 | 明確定義兩邊指標；在 prod 上算同口徑；交叉核對 prediction_log 與 alerts 表記錄數，避免高估 alert、低估 precision。 |
| R3 | Label 定義／覆蓋不一致（trainer vs validator；僅 TP/FP 無 FN）；**censored/terminal bet** | 確認 DEC-030 已部署；同批 bet 比對 trainer label 與 validator；**censored 樣本處理對齊**；below-threshold 抽樣或全量標註補 FN。 |
| R4 | 特徵／資料管線 parity（**profile TTL 快照**、**canonical_mapping cutoff**、rated、資料源） | Profile 含 TTL 期間舊快照風險、建議 log profile_snapshot_dtm；**canonical_mapping.cutoff.json vs 訓練 window_end** 快速驗證；profile／rated／FND-01 比對。 |
| R5 | 時間／分佈漂移 | Score 分佈比較（test vs prod、by 時段）；分群算 P/R；多窗 backtest 看指標變異。 |
| R6 | Production 無法直接算 recall／precision@recall=1% | 確認 §0 先決後，prediction log ＋ 離線標註還原 PR；注意 is_alert 與 alerts 表不對稱（見 R2）。 |
| R7 | Backtest 視窗代表性不足 | 多時間窗 backtest，看 precision@recall=1% 變異；必要時以 prod log 為準。 |
| R8 | Uncalibrated 閾值 fallback | 檢查 artifact／log 的 uncalibrated 旗標；必要時做 calibration 評估。 |
| R9 | 時區轉換一致性（payout_complete_dtm） | 同一批 bets 取時間戳比對 tz-naive HK；**整點偏移（如 8h）視為高風險，需立即修正**。 |

---

## 4. 建議調查順序

0. **關鍵先決（§0）**：確認 prediction_log 最新 `scored_at`、`PREDICTION_LOG_DB_PATH` 與 **`DATA_DIR`**（含 player_profile.parquet、canonical_mapping.parquet 版本）在 production 已正確設定；若 prediction log 缺失則 R1/R2/R6 無法推進，應優先修復。
1. **R2（指標口徑）**：先釐清 test 與 production 各自量的是什麼，避免誤比；建立同口徑比較方式。
2. **R8（uncalibrated）**：快速檢查 artifact／log，排除 fallback 閾值造成的假落差。
3. **R3 — censored（terminal bet）比對**：validator 與 `labels.py` 對 censored 樣本處理是否一致，成本低、對 precision 偏差影響大。**本步驟不依賴 prediction log**：即使 §0 發現 prediction log 未啟用，仍可推進——只需自 ClickHouse 或 Parquet 取一批 production bets，直接以 `labels.py` 與 validator 邏輯做 censored 比對（見 R3 調查方式步驟 3）。
4. **R4 前 — canonical_mapping cutoff 快速驗證**：比對 `canonical_mapping.cutoff.json` 的 `cutoff_dtm` 與最近一次訓練的 `window_end`，確認差距是否可接受（約 5 分鐘可完成）。
5. **R9（時區一致性）**：同一批 bets 在 trainer 與 scorer 資料源比對 payout_complete_dtm 的 tz-naive HK 是否一致；**整點偏移（如 8h）視為高風險並優先修正**。時區轉換錯誤屬**系統性資料偏差**，若存在會污染 R1/R4/R5 的分析結果；R9 不依賴 prediction log，成本低、影響面廣，故排在 profile／canonical 費時比對之前執行。
6. **R3（其餘）**：確認 DEC-030 已部署，同批 bet 比對 trainer 與 validator 的 label；below-threshold 抽樣或全量標註補 FN。**前提：R9 已排除時區偏移，或已確認偏移不影響 label 邊界（例如 gap 遠離 30min 臨界）**；否則 R3 的 `compute_labels`／validator 比對結果可能被 `payout_complete_dtm` 時區錯誤污染。
7. **R1、R6（閾值與嚴謹指標）**：在具備 prediction log ＋ 離線標註後，還原 production PR、與 test 同口徑比較；注意 prediction_log 與 alerts 表筆數交叉核對。
8. **R4（其餘）**：若 R1/R3 無法解釋落差，再查 profile TTL（可先用 §R4 步驟 1 的臨時驗證路徑）、rated、資料源與 FND-01；必要時 scorer 加 log profile_snapshot_dtm。
9. **R5、R7（分佈與 backtest 代表性）**：分時段／群體與多窗 backtest，評估分佈與視窗代表性。

---

## 5. 調查結果記錄（待填）

完成各項調查後，可於此節或 STATUS.md 記錄結論。**狀態選項**：待調查 / 進行中 / 已確認根因（待修復）/ 已修復待驗證 / 已排除（例如 R9 時區問題確認存在時為「已確認根因（待修復）」，修復後為「已修復待驗證」）。

| 編號 | 狀態 | 結論摘要 |
|------|------|----------|
| R1 | （見上列選項） | （填寫） |
| R2 | … | … |
| R3 | … | … |
| R4 | … | … |
| R5 | … | … |
| R6 | … | … |
| R7 | … | … |
| R8 | … | … |
| R9 | … | … |

---

## 附錄：與程式碼庫的對應

- **Prediction log 實作**：`trainer/serving/scorer.py` 的 `_append_prediction_log()`，寫入欄位含 bet_id, score, margin, model_version, is_rated_obs, scored_at；是否寫入由 `PREDICTION_LOG_DB_PATH`（config）控制。
- **is_alert 與 alerts 表口徑差異**：prediction_log 的 **is_alert** 定義為 **`margin >= 0 AND is_rated_obs == 1`**（即「達閾值且為 rated」）；**alerts 表**在寫入前經過 **duplicate suppression**（`alert_history`），同一 bet 若已在歷史中則不再寫入 alerts。故 **prediction_log 中 is_alert=1 的筆數可能多於實際 alerts 表筆數**，口徑差異來源在此；分析時應交叉核對兩表，避免高估 alert 數、低估 precision。
- **Label 定義**：`trainer/labels.py` 使用 config 常數（WALKAWAY_GAP_MIN、ALERT_HORIZON_MIN、LABEL_LOOKAHEAD_MIN 等）與 G3 穩定排序；**`extended_end`** 用於控制觀察窗尾端 buffer，離線標註時須與訓練時一致，否則 censored 判定會偏移；censored/terminal bet 處理見該模組。
- **Profile 載入**：scorer 使用 `_profile_cache` 與 1h TTL；trainer 的 `load_player_profile()` 為 window 區間＋PIT join。
- **Canonical mapping**：scorer 在 DATA_DIR 設定時使用 `CANONICAL_MAPPING_PARQUET` 與 `CANONICAL_MAPPING_CUTOFF_JSON`；未設定時 fallback 至 `PROJECT_ROOT/data/`；`--rebuild-canonical-mapping` 可強制重建。

**文件版本**：初版；已納入第一至第五輪程式碼庫點評及最終收尾（含 §1.3 步驟 2 資料來源優先順序、Section 5 狀態選項擴充、附錄 extended_end 說明）。  
**最後更新**：依 .cursor/plans 慣例由執行者更新。
