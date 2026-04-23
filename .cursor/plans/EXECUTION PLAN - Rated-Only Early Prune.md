# Rated-Only Early Prune Execution Plan

> 文件層級：Execution Plan（Working / Execution Plan）  
> 目的：將「**non-rated patrons 盡早排除出主 FE / label / scoring 路徑**」落成可執行工作計畫，降低不必要的運算與記憶體成本，並維持 trainer / backtester / scorer 契約一致。  
> 邊界：本檔**不重寫 SSOT**、**不重寫 implementation plan**；若需變更產品語義或特徵定義，應先回寫上游文件或 `DECISION_LOG.md`。  
> 上游依據：`ssot/trainer_plan_ssot.md`、本輪對話結論、既有 `.cursor/plans/DECISION_LOG.md` / `.cursor/plans/STATUS.md`

### 任務狀態標記（本檔）

| 標記 | 意義 |
| :--- | :--- |
| **✅** | 本列 DoD 已滿足 |
| **🟡** | 部分完成 |
| **⏳** | 進行中 |
| **⬜** | 未開始 |

---

## 0. 本輪執行共識

- SSOT 已明確規定：**non-rated 觀測不參與訓練與推論，只做 volume 統計**。
- 目前訓練 / 回測仍存在「**先做 FE，最後才以 `is_rated` 切掉**」的成本浪費。
- 本輪要做的是：把 **rated-only boundary 前移** 到 heavy FE 與 label 主路徑之前。
- `table_hc` 若未來啟用，**不得沿用舊語義**；若改為只計算 rated patrons，必須視為**新語義特徵**，採新名稱與新文件契約。
- `table_hc` 目前**不在主訓練 / 主 serving 路徑內**，因此**不應與本輪 early-prune 綁成同一個交付物**。

---

## 1. 目標與非目標

### 1.1 本輪目標

1. 在 `trainer/training/trainer.py` 將 rated-only 切點前移，避免 non-rated 進入 Track Human / Track LLM / labels / profile join 主路徑。
2. 在 `trainer/training/backtester.py` 同步前移切點，保持 train-backtest parity。
3. 在 `trainer/serving/scorer.py` 讓 non-rated 不再進入正式 FE / model 路徑，只保留 volume telemetry。
4. 產出足夠的測試與執行證據，證明：
   - 主路徑口徑一致；
   - non-rated 不再吃掉 heavy FE 成本；
   - 沒有引入新的 label / score 語義偏移。

### 1.2 明確非目標

- 本輪**不**啟用 `table_hc` 到主路徑。
- 本輪**不**新增 `t_game` 依賴。
- 本輪**不**改動 SSOT 中關於 rated-only policy 的產品定義。
- 本輪**不**同時重做 Track LLM feature spec 或 feature screening 策略。
- 本輪**不**把 telemetry 契約擴充成新的分析系統；僅維持 non-rated volume 可追蹤。

---

## 2. 現況基線（Execution Baseline）

### 2.1 已確認現況

- `trainer/training/trainer.py`
  - 先 attach `canonical_id`
  - 對整份 `bets` 做 Track Human
  - 對整份 `bets` 做 Track LLM
  - 對整份 `bets` 做 `compute_labels`
  - 最後才在 `labeled` 上標 `is_rated`
- `trainer/training/backtester.py`
  - 與 trainer 類似，先做 FE / labels，之後才進 rated-only score path
- `trainer/serving/scorer.py`
  - 已有「在 Track LLM / profile join 前切 rated-only」的雛形
  - 但 non-rated 仍先經過 `build_features_for_scoring()`，仍有不必要成本

### 2.2 已確認風險

- **記憶體風險**：non-rated 量大時，Track LLM（DuckDB window）與中間 DataFrame merge 容易放大 RAM 峰值。
- **時間風險**：對 non-rated 計算 per-player feature 與 labels，純屬浪費。
- **一致性風險**：若只改 trainer、不改 backtester / scorer，會出現 train-backtest-serving parity 破裂。
- **語義風險**：若把 `table_hc` 直接改為 rated-only 卻沿用舊名，會造成特徵意義漂移。

---

## 3. 執行策略

### 3.1 核心策略

在 attach identity 後，建立明確的 rated-only 邊界：

1. 將 `player_id -> canonical_id` mapping attach 到 bets。
2. 立即建立 `is_rated_obs`（或等價布林欄）作為**早期路由欄位**。
3. 對 non-rated rows：
   - trainer / backtester：直接排除，不進主 FE / labels / profile join。
   - scorer：不進正式 FE / model path，只保留 volume telemetry。
4. 對 rated rows：
   - 保留歷史 lookback 與 extended-zone 所需 rows；
   - 照原契約進入 Track Human / Track LLM / labels / profile join。

### 3.2 設計原則

- **契約先行**：先定義 rated-only boundary 與 telemetry 邊界，再改程式。
- **三路徑同步**：trainer / backtester / scorer 必須一起收斂。
- **先減成本，再談新特徵**：本輪優先解除 non-rated FE 浪費，不把 `table_hc` 新語義一起摻入。
- **保守變更 label 契約**：label 邏輯仍以 rated patron 自身 bet 序列為準，不引入跨玩家依賴。

---

## 4. 工作分解（Work Breakdown）

### 4.1 Batch A：契約凍結與切點設計（P0）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **⬜** | `A1 rated-only boundary freeze` | 1. 明確定義 `is_rated_obs` / 等價布林欄位的生成位置。 2. 定義 trainer / backtester / scorer 各自的 early-prune 切點。 3. 明確列出 non-rated 允許保留的唯一用途（volume telemetry）。 | `DS/Eng owner` | 現有程式路徑盤點 | 邊界定義摘要、需修改函式清單 | 三條主路徑的切點位置與 non-rated 允許行為皆明確，無待定語義 |
| **⬜** | `A2 decision bookkeeping` | 1. 將「`table_hc` rated-only 視為新語義特徵」記為待補 decision。 2. 明確標註其不屬於本輪交付範圍。 | `DS/Eng owner` | `A1` | decision note 或待辦條目 | 後續 coding 不會誤把 `table_hc` 當成本輪一起落地 |

### 4.2 Batch B：Trainer / Backtester 主路徑調整（P0）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **⬜** | `B1 trainer early prune` | 1. attach `canonical_id` 後立刻標示 rated rows。 2. 僅保留 rated rows 進入 Track Human / Track LLM / labels / profile join。 3. 檢查 chunk cache / prefeatures cache key 是否需納入新邊界契約。 4. 更新 log / telemetry，清楚記錄被 early-prune 的 non-rated row 數。 | `Eng owner` | `A1` | trainer 主路徑補丁 | non-rated 不再進入 trainer heavy FE path，且 cache / log 契約無歧義 |
| **⬜** | `B2 backtester parity` | 1. 同步套用與 trainer 相同的 early-prune 邊界。 2. 確保回測 labels / profile / score 口徑一致。 3. 補齊必要的 debug log。 | `Eng owner` | `B1` | backtester 主路徑補丁 | backtester 與 trainer 在 rated-only boundary 上一致，無額外 full-data FE 殘留 |

### 4.3 Batch C：Scorer 路徑拆分（P0）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **⬜** | `C1 scorer telemetry split` | 1. 將 non-rated volume telemetry 從正式 FE path 中拆出。 2. 確認 telemetry 不再依賴 `features_all` 的 full FE 結果。 3. 保持既有 volume log 指標意義可追蹤。 | `Eng owner` | `A1` | scorer telemetry 補丁 | non-rated volume 統計可保留，但不需先跑完整 FE |
| **⬜** | `C2 scorer rated-only FE path` | 1. 僅將 rated bets 丟入 `build_features_for_scoring()` 或等價正式 FE path。 2. 保持 Track LLM / profile join / scoring 僅對 rated rows 生效。 3. 更新 log，明確分開 telemetry rows 與 scored rows。 | `Eng owner` | `C1` | scorer 主路徑補丁 | scorer 真正符合「non-rated 不呼叫模型、不做正式 FE」 |

### 4.4 Batch D：測試與證據（P0）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **⬜** | `D1 trainer/backtester parity tests` | 1. 增加或更新測試，確認 early-prune 發生在 heavy FE 前。 2. 驗證 trainer / backtester 對 mixed rated/unrated 輸入的輸出 row 集合一致。 | `Eng owner` | `B1`,`B2` | 單元/契約測試 | 測試能抓到「non-rated 混入 FE」的回歸 |
| **⬜** | `D2 scorer behavior tests` | 1. 驗證 non-rated 不進正式 FE / model path。 2. 驗證 rated rows 正常進 score。 3. 驗證 telemetry 仍保有 non-rated volume 計數。 | `Eng owner` | `C1`,`C2` | 單元/契約測試 | scorer 路徑拆分有測試保護 |
| **⬜** | `D3 runtime evidence` | 1. 以至少一組代表性資料窗比較變更前後的 row counts / runtime / memory signals。 2. 將結果記錄到 `STATUS.md`。 | `DS/Eng owner` | `B1`,`B2`,`C2` | before/after evidence、狀態紀錄 | 能說明此變更確實降低 FE 成本，非純結構重排 |

### 4.5 Batch E：`table_hc` 後續銜接（P1，非本輪實作）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **⬜** | `E1 new-semantic feature design` | 1. 若未來啟用 rated-only 桌況特徵，重新命名（如 `rated_table_hc_*`）。 2. 與未來 `t_game` 的 total-player 特徵分離。 3. 明確文件化新語義與 non-goal。 | `DS owner` | 本輪 early-prune 完成 | decision / implementation input | `table_hc` 不會以舊名偷換語義進主路徑 |

---

## 5. 實作順序（Priority / Sequence）

1. `A1`：先凍結 rated-only boundary 與 scorer telemetry 邊界。
2. `B1`：先改 trainer，因為它是主資料生成與最大資源消耗點。
3. `B2`：立即跟進 backtester，避免 parity 破裂。
4. `C1` + `C2`：再拆 scorer，收斂 serving 契約。
5. `D1` + `D2`：補測試，鎖住回歸風險。
6. `D3`：最後補 before/after 證據。
7. `E1`：不阻擋本輪 coding，只作為下一輪新語義特徵入口。

---

## 6. 逐項 Definition of Done

### 6.1 功能 DoD

- trainer / backtester / scorer 三條主路徑皆已明確 early-prune non-rated。
- non-rated 不再進入 heavy FE 與 model path。
- volume telemetry 仍存在，且語義未失真。
- `table_hc` 本輪未被偷偷接線，也未沿用舊名改語義。

### 6.2 測試 DoD

- 有至少一組測試能防止：
  - trainer 在 heavy FE 前未切 rated-only；
  - backtester 與 trainer 邊界不一致；
  - scorer 對 non-rated 仍執行正式 FE / model。
- 新測試不依賴 production 資料、不引入高價值維護成本的脆弱 fixture。

### 6.3 效能 DoD

- 至少一組代表性 run 顯示：
  - FE 前後 row count 收斂符合預期；
  - heavy path 處理列數下降；
  - runtime 或 memory signal 有可解釋改善。

---

## 7. 風險與阻塞點

### 7.1 主要風險

- **Label coverage 風險**：若 early-prune 寫錯位置，可能誤刪 rated patron 的 extended-zone rows，導致 label 計算不完整。
- **Parity 風險**：若 trainer / backtester / scorer 只改其中之一，結果將不可比較。
- **Cache 風險**：若 chunk cache / prefeatures cache 未納入新契約，可能命中舊資料。
- **Telemetry 漂移風險**：若 scorer telemetry 仍隱含依賴 full FE output，拆分時可能造成數字不一致。

### 7.2 阻塞規則

- 若 `A1` 未明確定義 boundary，不得直接進 `B1` / `C1` coding。
- 若 `B1` 完成但 `B2` 未同步，不得宣告 main path parity 完成。
- 若 `C1` 拆分後 telemetry 定義變動卻未說明，不得 merge。
- 若 `table_hc` 範圍在實作中膨脹進本輪 PR，應拆回下一輪。

---

## 8. 建議的首波 coding 切面

> 目的：讓下一輪實作可以先做最小、可驗證、可回退的一刀。

### Slice 1（最小可落地）

- `trainer/training/trainer.py`
  - attach `canonical_id` 後立即產生 rated mask
  - 在 Track Human 前切為 rated-only
  - 保留必要的 rated history / extended-zone rows
- `trainer/training/backtester.py`
  - 同步相同切點
- 測試：
  - mixed rated/unrated input 時，確認 heavy FE 只吃 rated rows

### Slice 2（收斂 serving）

- `trainer/serving/scorer.py`
  - 將 non-rated telemetry 從正式 FE 路徑拆出
  - 讓正式 FE / model 僅處理 rated rows
- 測試：
  - non-rated 只記 telemetry，不進 score path

### Slice 3（證據與清尾）

- 補 runtime / row-count before-after 比較
- 將結果記錄到 `.cursor/plans/STATUS.md`

---

## 9. Assumptions

- `compute_labels()` 僅依賴單一 patron 的 bet 序列；non-rated rows 不影響 rated patron 的 label 語義。
- 目前 active Track Human / Track LLM 主路徑不需要 non-rated rows 才能正確計算 rated patron 的既有特徵。
- `table_hc` 既然尚未主路徑接線，本輪不應為它延後 early-prune。

---

## 10. 下一步

本檔完成後，下一步應直接按 `Slice 1 -> Slice 2 -> Slice 3` 開工；若 coding 過程中發現 `table_hc` 或 telemetry 契約需要升級為架構決策，先回寫 `.cursor/plans/DECISION_LOG.md`，再繼續實作。
