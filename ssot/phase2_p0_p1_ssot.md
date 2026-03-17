# Phase 2 實施計畫（P0–P1）— SSOT

> **範圍**：P0（可追溯性與版本一致）、P1（記錄與調查，找出 deployment performance drift 根因）。其餘項目列於 P2 佔位，稍後再規劃。  
> **用途**：作為 Phase 2 實作的單一依據（SSOT）。

---

## Performance drift 定義

| 項目 | 內容 |
|------|------|
| **指標** | **Precision@recall=1%** |
| **基準（baseline）** | **Validator 在部署階段、使用即時資料的輸出** vs **訓練階段 hold-out test set 的評估結果** |
| **說明** | 當部署環境下量到的 precision@recall=1% 明顯低於 hold-out 上的表現，即視為發生 performance drift；Phase 2 目標為記錄發生情形並調查根因。 |

---

## 現狀（As-Is）

（依專案程式現況整理，供判斷 P0/P1 為新建或擴充。）

| 項目 | 現況 |
|------|------|
| **Model registry / 版本** | 無獨立 model registry。模型版本由 `trainer/training/trainer.py` 的 `get_model_version()` 產生（格式 `YYYYMMDD-HHMMSS-<git7>`），寫入 `trainer/models/model_version` 及 `training_metrics.json`。Artifact 為檔案目錄（`model.pkl`、`feature_list.json`、`features_active.yaml`、`reason_code_map.json`、`training_metrics.json`、`model_version`），scorer 從該目錄載入。 |
| **Pipeline 編排** | 訓練為單一 Python 入口 `run_pipeline(args)`，CLI 驅動（如 `--days`、`--use-local-parquet`）；**無** Airflow / Kubeflow 等編排器。 |
| **推論日誌** | Scorer 將 alert 寫入 SQLite（`state.db` 的 `alerts` 表），欄位含 `model_version`、`score` 等。**無**部署階段預測日誌寫入 MLflow 或外部儲存（DEC-029 已決策採用 MLflow，尚未實作）。 |
| **Validator** | `validator.py` 自 `state.db` 讀取待驗證 alert，向 ClickHouse 拉取即時 bet/session 做驗證，結果寫入 `state.db` 的 `validation_results`（含 precision、match 數等）。即「基準」中的 validator 部署輸出；hold-out 為 trainer 的 test set 指標（`training_metrics.json` 內 `test_precision_at_recall_0.01` 等）。 |
| **告警管道** | Alert 存於 `state.db`；api_server 提供 `get_alerts`、`get_validation`。**無**外部告警管道（Slack、email、PagerDuty）之程式碼，可能為應用層或手動查看。 |
| **資料品質** | Trainer 與 scorer/validator 內有 DQ 邏輯（schema、filter、normalize_bets_sessions 等）。**無**獨立的 DQ dashboard 或定期 DQ 檢查腳本。 |
| **特徵與模型綁定** | 特徵清單與 spec 已隨 artifact 一併輸出（`feature_list.json`、`features_active.yaml`）；`training_metrics.json` 可含 Feature Spec hash。Scorer 自 artifact 目錄載入；**無**中央 registry 的 model–feature 對應或正式 rollback 流程。 |

---

## 一、P0：可追溯性與版本一致

**目標**：每個上線模型都可追溯到對應的 code、data、特徵版本與 pipeline run；rollback 時 model 與特徵一致，避免 training–serving skew。

### P0.1 Artifact 與 pipeline 溯源

| 項目 | 內容 |
|------|------|
| **產出** | 每個 production model 具備可查詢的溯源資訊。 |
| **紀錄內容** | 對應的：git commit（或 code version）、訓練資料路徑/版本/取用時間範圍、特徵清單或 feature pipeline 版本、pipeline run id（如 Airflow/Kubeflow run id）、產出 artifact 路徑。 |
| **存放位置** | 與 model 綁定：寫入 MLflow run 的 params/tags（Phase 2 為 **GCP Tracking Server**），或可透過 MLflow 查到的唯一連結（如 pipeline run id）。 |
| **可接受標準** | (1) 給定任一 production model version，能在 10 分鐘內查出上述欄位；(2) 審計或事後分析時能據此重現「該 model 從哪來」。 |
| **依賴** | MLflow Tracking Server 於 GCP；訓練 pipeline 可連 GCP 並在產出 artifact 後寫入上述 metadata。 |

### P0.2 特徵與模型一起版本化

| 項目 | 內容 |
|------|------|
| **產出** | 特徵計算邏輯/清單與 model 版本綁定；deploy/rollback 時 serving 使用的特徵與該 model 訓練時一致。 |
| **實作方向** | **採用 (A)**：特徵清單或 feature spec 隨 model artifact **同目錄**一併輸出；同目錄即同版本。Rollback 僅能**整包／整目錄**替換，禁止只換 model.pkl，以避免 training–serving skew。 |
| **可接受標準** | (1) 回滾到某 model version 時，文件或機制上保證使用該 version 對應的特徵；(2) 新上線或回滾不需人工記憶「這版該配哪版特徵」。 |
| **依賴** | 特徵計算有版本或 commit 可指涉（例如專案內 feature 程式或 config 的 version）；P0.1 的溯源可一併記錄 feature version。 |

### P0 實施順序與產出

1. 定義「溯源必填欄位」與存放格式（例如 MLflow tags + 一組 key 命名規則），並在現有訓練 pipeline 中寫入（P0.1）。
2. 定義 model–feature 對應方式並實作綁定與 deploy/rollback 流程（P0.2）。
3. **產出**：溯源欄位與格式說明（doc/README）、pipeline 與 registry 串接、可選 runbook（如何查詢溯源、如何依版本回滾並帶對應特徵）。
4. **預估**：_（例：約 1–2 週，依現狀調整）_

---

## 二、P1：記錄與調查 — 找出 deployment drift 根因

**目標**：在部署階段記錄「發生了什麼」，並透過監控與告警驅動調查，找出 precision@recall=1% 在部署相對於 hold-out 惡化的根因。

### P1.1 部署階段預測日誌（MLflow）

| 項目 | 內容 |
|------|------|
| **產出** | 部署階段推論日誌先寫入**本地 SQLite**（中央儲存）；再由**匯出程式**週期性匯出並上傳至 MLflow（**GCP**），供事後查詢與分析。 |
| **紀錄欄位** | 至少：request_id、score、model_version、timestamp；以及足以識別 patron/session 的欄位（供極端尾部抽查）；若已有 trace id 可一併寫入。 |
| **用途** | (1) 極端尾部人工抽查（如高分 FP）；(2) 依時間/模型版本做 score 分佈、drift、precision@recall=1% 分析。 |
| **可接受標準** | (1) 日誌寫入 SQLite 且可經匯出上傳至 MLflow，可依 model_version、時間範圍查詢；(2) 不引入額外日誌產品，符合 DEC-029；(3) 留存策略與隱私/合規一致（如 GCP、保留天數）。離線時資料保留於 SQLite，恢復後可補傳。 |
| **寫入策略** | **Scorer**：每筆預測寫入**本地 SQLite**，不阻塞主路徑、不累積於記憶體。**匯出程式**（**獨立 process**，如 cron 或獨立腳本）：週期性（如每 5–15 分鐘，可調）自 SQLite 讀取、匯出為壓縮檔（建議 Parquet 壓縮或 gzip CSV 以省頻寬）、上傳至 MLflow run 的 artifact（GCS）；匯出與上傳不與 scorer 共用 process，避免 GIL 阻塞。 |
| **依賴** | 本地 SQLite（預測日誌 schema，建議啟用 WAL mode 以利匯出讀取時 scorer 仍可寫入）；MLflow Tracking Server 於 GCP；匯出程式可連 GCP 時上傳；artifact 由客戶端直傳 GCS（不經 Tracking Server 記憶體）。 |

### P1.2 告警與 runbook

| 項目 | 內容 |
|------|------|
| **產出** | 與部署/模型相關的告警有明確接收者與處理步驟（runbook）。 |
| **內容** | (1) 告警清單：哪些情境會告警（例如錯誤率、延遲、score 分佈異常、precision@recall=1% 偏離）；(2) 誰收警、通知管道；(3) runbook：收到警後第一步做什麼、何時升級、何時考慮回滾。 |
| **可接受標準** | 每條與 drift/穩定性相關的告警都對應到一頁 runbook 或等同文件；可用 checklist 逐條勾選驗證。 |
| **依賴** | P1.1 有日誌/指標可觸發告警；現有或將建的告警管道（e-mail、Slack、PagerDuty 等）。 |

### P1.3 Human-oriented 告警原因

| 項目 | 內容 |
|------|------|
| **產出** | 告警訊息包含「為什麼觸發」的簡短說明，讓人能判斷是否與 drift 有關、要不要深入查。 |
| **實作方向** | 告警 payload 或說明欄位包含：觸發條件、閾值、當前值、受影響的 model_version 或時間範圍；可選：連結到 runbook 或 dashboard。 |
| **可接受標準** | 收到告警的人不需查 code 或手動對照閾值即可理解「觸發原因」。 |

### P1.4 資料品質監控

| 項目 | 內容 |
|------|------|
| **產出** | 對模型輸入相關的資料做定期檢查或 DQ dashboard，及早發現 null、schema、volume 異常。 |
| **範圍** | 至少涵蓋：推論所用特徵的來源資料、或與 drift 分析相關的關鍵輸入（依現有架構列舉）。 |
| **檢查項** | 例如：null 比例、關鍵欄位型別/schema、每日/每小時 volume 與簡單分佈；異常時可觸發告警或納入既有告警。 |
| **可接受標準** | (1) 有定期（如每日）檢查或可視化；(2) 異常能通報（告警或週報）；(3) 分析 drift 時可對照「是否曾發生 DQ 異常」。 |
| **依賴** | 知道推論與訓練用的資料來源；可讀取該來源的權限與查詢方式。 |
| **⚠️ 記憶體風險** | Evidently 分析時可能將大量資料載入記憶體，有 **OOM 風險**；實作時須依資料量與機器資源評估並採取適當措施，具體策略留待實作時決定。 |

### P1.5 Training–Serving Skew 驗證

| 項目 | 內容 |
|------|------|
| **產出** | 能偵測「同一批 input 在訓練 pipeline 與 serving 端產出的特徵是否一致」，以排除/確認 skew 為 drift 根因之一。 |
| **實作方向** | 一次性或定期：對同一批 request/識別鍵，比對 training 特徵計算結果 vs. serving 特徵計算結果；若有差異則記錄並納入根因分析。 |
| **可接受標準** | (1) 至少執行過一次 skew 檢查並有紀錄；(2) 若發現 skew，納入 drift 調查報告。 |

### P1.6 Drift 根因調查

| 項目 | 內容 |
|------|------|
| **產出** | 明確的調查流程與產出：假說檢驗、分析步驟、根因報告或行動建議。**正式紀錄**存於 **doc/**（markdown）；可選將摘要或連結存於 MLflow artifact。 |
| **假說方向** | 例如：input/covariate drift、label 定義或分佈變動、training–serving skew、資料品質異常、資料量/季節性、validator 與 hold-out 評估口徑差異等。 |
| **流程** | 誰負責分析、使用哪些資料（P1.1 日誌、P1.4 DQ、P0 溯源）、產出格式（簡短報告、結論、建議動作）、可選 checkpoint 或 deadline。 |
| **可接受標準** | 有文件化之調查流程，且至少完成一輪針對當前 observed drift 的根因分析並留下紀錄（存於 doc/）。 |

### P1 實施順序與產出

1. **P0 與 P1.1 並行**：不強制等 P0 全部完成才開始 P1.1；只要 log 中能寫入當前 `model_version` 即可開始收集資料，P0 完成後可再 enrich 日誌中的版本資訊。
2. **P1.1** 部署預測日誌上線；**P1.4** 資料品質監控上線（可與 P1.1 並行或緊接），以便區分「drift 來自資料品質」或「來自分佈/模型」。
3. **P1.2、P1.3** 告警與 runbook、human-oriented 說明，與 P1.1/P1.4 的指標對齊。
4. **P1.5** Training–serving skew 驗證（一次性或定期）。
5. **P1.6** 依日誌、DQ、溯源執行至少一輪 drift 根因調查並文件化。
6. **產出**：部署預測日誌（MLflow GCP）、DQ 檢查/dashboard、告警清單 + runbook、human-oriented 告警、skew 驗證紀錄、drift 調查流程與至少一份根因分析紀錄（正式紀錄存 doc/）。
7. **預估**：_（例：P1.1+P1.4 約 2–3 週，其後 P1.2–P1.6 依現狀調整）_

---

## 三、P0–P1 總體順序與依賴

```
P0.1 溯源欄位與 pipeline 寫入     ──┐
                                     ├── 並行（P1.1 不必等 P0 完成）
P1.1 部署預測日誌（SQLite + 匯出→MLflow GCP） ──┘
P1.4 資料品質監控（可與 P1.1 並行）
    ↓
P0.2 model–feature 版本綁定與 deploy/rollback 流程
    ↓
P1.2 告警與 runbook
P1.3 Human-oriented 告警
    ↓
P1.5 Training–Serving Skew 驗證
P1.6 Drift 根因調查（使用 P1.1 / P1.4 / P0 產出）
```

---

## 四、成功標準（Phase 2 P0–P1 完成時）

| 項目 | 可驗證條件 |
|------|------------|
| **P0** | 任一 production model 可追溯（10 分鐘內查出必填欄位）；回滾時 model 與特徵版本一致；有溯源與回滾的說明或 runbook。 |
| **P1.1** | Scorer 將預測寫入本地 SQLite；匯出程式週期性匯出並上傳 MLflow（GCP）；可依 model_version、時間範圍查詢；離線時保留於 SQLite、恢復後補傳；上線 N 天後回查日誌涵蓋率達預期（N 與涵蓋率由團隊訂）。 |
| **P1.2** | 所有與 drift/穩定性相關的告警均有對應 runbook（checklist 可逐條勾選）。 |
| **P1.3** | 告警訊息含觸發原因，無需查 code 即可理解。 |
| **P1.4** | 有定期 DQ 檢查或 dashboard，異常可通報。 |
| **P1.5** | 至少執行過一次 skew 驗證並有紀錄。 |
| **P1.6** | 至少完成一輪 drift 根因分析並留下紀錄（含假說、分析、結論/建議）；正式紀錄存 doc/（markdown）。 |

---

## 五、P2（後續，暫不細化）

以下項目已知、刻意排後，待 P0–P1 完成後再規劃：

- 部署與風險控制：Staged rollout（#4）、Health/SLA（#9）、設定與閾值／回滾（#8）
- 評估：Run-level / Macro-by-run 評估（#6）
- 特徵：Game table / table_hc（#7）
- 穩定性與資源：memory/computation 優化（#11）、環境自動偵測（#12）、ClickHouse 負載（#13）
- 可選：Drift 觸發重訓、自動化 pipeline、Time-to-event、線上學習、Online Feature Store 等

---

*本文件為 Phase 2 P0–P1 實施之 SSOT；若與其他文件衝突，以本文件為準。*
