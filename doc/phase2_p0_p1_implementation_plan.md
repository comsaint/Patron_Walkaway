# Phase 2 P0–P1 實施計畫

> 依據：`ssot/phase2_p0_p1_ssot.md`。  
> 本文為架構與實作決策層級，不包含逐項任務或檔案級修改。

---

## 設計原則

- **以 MLflow 為單一中心**：溯源、模型版本、部署預測日誌皆透過 MLflow；Phase 2 **不實作 sidecar**（無檔案型 provenance fallback）。
- **其他能力優先採用現成工具**：DQ、drift、skew 用 Evidently 開源；告警傳遞列為未來項目。
- **資料不送廠商雲端**：僅 on-prem 或**自己的 GCP**；不對外連 Evidently Cloud / WhyLabs 等。
- **MLflow**：Phase 2 **僅在 GCP** 架設 Tracking Server；所有可連 GCP 的機器直接寫入，監控日誌與模型版本集中一處。若未來 production 需 MLflow 且無法連網，再另行實作**獨立本地版（SQLite）**，不納入 Phase 2。Evidently 仍**本地為主、可選 sync 報告到 GCS**（見 §1）。

---

## 1. MLflow：僅 GCP Tracking Server 與 Evidently

### 1.1 MLflow：GCP 上跑 Tracking Server（Phase 2 僅此）

- **採用方案**：在 GCP 上架設 **MLflow Tracking Server**，供多機與 production（可連 GCP 時）直接寫入，監控日誌與模型版本**集中一處**。
- **Phase 2 不實作本地 MLflow**：不支援離線 fallback、不實作本地→GCP sync，以簡化實作。若未來 production 環境無法連網卻需 MLflow，再另行實作 standalone 本地版（SQLite backend + 本地 artifact）。
- **最低成本設定**：**Compute Engine e2-micro**（GCP Always Free 額度，1GB RAM；適用區域如 us-central1 / us-east1 / us-west1）+ **SQLite** 為 backend store（`--backend-store-uri sqlite:///path/mlflow.db`，存於 e2-micro 本機磁碟）+ **GCS** 為 artifact store（`--default-artifact-root gs://your-bucket/mlflow-artifacts`）。**Artifact 由客戶端直傳 GCS**，不經 e2-micro 記憶體，避免 OOM。成本約 **$0**（e2-micro 與 30GB 磁碟在免費額度內）+ GCS 儲存與流量。
- **必要時升級**：可改為 **Cloud Run + Cloud SQL**（scale-to-zero、託管 DB），成本較高（約 Cloud SQL 最小規格起跳）。升級觸發可考慮：寫入併發或查詢變慢、SQLite lock、e2-micro 記憶體不足、或需更高可用性與權限控管。

### 1.2 MLflow 具體約定

- **連線**：訓練機與 scorer 將 `MLFLOW_TRACKING_URI` 指向 **GCP Tracking Server**（例如 `http://<e2-micro-ip>:5000`），直接寫入 run、params/tags、artifact（artifact 存 GCS）。無 GCP 連線時不寫入 MLflow（不 fallback 本地）。
- **Artifact 上傳路徑**：e2-micro 僅 1GB RAM，**artifact 必須由客戶端直傳 GCS**，不經 Tracking Server 記憶體（避免 OOM）。設定 MLflow 的 `--default-artifact-root` 為 GCS，客戶端依 MLflow 回傳的 artifact URI 直接上傳。
- **部署預測日誌**：Scorer **僅將每筆預測寫入本地 SQLite**（專用 table），不阻塞主路徑、不累積於記憶體。**匯出與上傳**由**獨立程式／排程**負責：週期性（例如每 5–15 分鐘，可依負載調整）自 SQLite 讀取、匯出為壓縮檔（建議 **Parquet 壓縮**如 gzip/snappy，或 gzip CSV 以省頻寬）、上傳至 MLflow run 的 artifact（GCS）。匯出在**獨立 process** 執行，避免 GIL 阻塞 scorer。
- **Production artifact**：仍為 **deploy 包內完整 artifact 目錄**；runtime 不從 GCS 拉取模型。

### 1.3 Evidently：本地為主、可選 sync 報告到 GCS

- **執行**：在**本地**跑 Evidently（batch 或小型 API），報告／結果寫**本地目錄**。有網路且需要時，將報告 sync 到 **GCS**（檢視時下載 HTML 或透過 GCS 連結開啟）。
- **Reference**：**Reference = 訓練資料**。**訓練結束時**產出一份 **reference profile**（或 snapshot），**隨模型版本**存放（與 artifact 同目錄或同 MLflow run），供 drift 比對。
- **觸發**：Phase 2 以**手動／ad-hoc** 執行；長期需規劃對 **production 進場資料流之持續監控**（例如定期抽樣或串流式 drift 檢查）。
- **原始資料**：僅在 on-prem（ClickHouse 或本地 Parquet）。Evidently 可讀寫 **logs／報告** 至 GCS；不從 GCS 讀 raw data。
- **用途**：資料品質、**distribution drift**（生產 vs 訓練，為主）、**feature consistency / training–serving skew**（為輔）。
- **告警傳遞**：列為**未來項目**；Phase 2 不實作 Slack/email 等傳遞。
- **⚠️ 記憶體風險**：Evidently 分析時可能將大量資料載入記憶體，有 **OOM 風險**；實作時須依資料量與機器資源評估並採取適當措施，具體策略（例如抽樣、聚合、分批等）留待實作時決定。

### 1.4 Production artifact

- Production 的 model artifact **一律**來自 **deploy 包內完整 artifact 目錄**；建包時在可連網環境將 artifact 打進包內，runtime 不從 GCS 拉取。

---

## 2. 架構總覽

### 2.1 目標狀態（Phase 2 P0–P1 完成後）

- **訓練**：`run_pipeline` 產出 artifact 目錄後，將 P0.1 溯源寫入 **MLflow**（GCP Tracking Server）；需可連 GCP。
- **部署**：Scorer 寫 state.db alerts，並將每筆預測寫入**本地 SQLite**（預測日誌中央儲存）；**匯出程式**週期性自 SQLite 匯出並上傳至 MLflow（GCP）。Artifact 來自 deploy 包內目錄。
- **Validator**：不變；輸出為 drift 基準之一。
- **Evidently**：本地執行 DQ、drift、skew；結果寫本地，可選 sync 報告到 GCS。Reference profile 於訓練結束時產出，隨模型版本。
- **溯源查詢**：給定 model_version，從 MLflow（GCP Tracking Server）查 run 的 params/tags。

### 2.2 與現狀的對應

- Model 來源：短期仍以「單一 artifact 目錄」為 deploy 事實來源；訓練與日誌以 MLflow 記錄於 GCP。
- 推論日誌：Scorer 寫入本地 SQLite；匯出程式週期性匯出並上傳至 MLflow（GCP）。離線時資料留 SQLite，恢復後可補傳。
- 告警：Phase 2 產出 Evidently 報告／狀態，並文件化**告警條件與 runbook**、**human-oriented 說明**；傳遞（Slack/email 等）列為未來。

---

## 3. 模組邊界與職責

### 3.1 訓練側

| 模組／範圍 | 職責 | Phase 2 變更 |
|------------|------|----------------------|
| **trainer**（`run_pipeline`） | 產出 artifact 目錄。 | **擴充**：寫完 artifact 後，將 P0.1 溯源寫入 **MLflow run**（GCP Tracking Server）；需可連 GCP。 |
| **溯源** | 與 model_version 綁定且可查。 | 以 MLflow 為準（GCP）。Phase 2 不實作 sidecar。 |

### 3.2 部署側（Scorer 與預測日誌匯出）

| 模組／範圍 | 職責 | Phase 2 變更 |
|------------|------|----------------------|
| **scorer** | 載入 artifact（包內目錄）、寫 state.db alerts。 | **擴充**：每筆預測寫入**本地 SQLite**（預測日誌專用 table），不阻塞主路徑、不累積於記憶體。 |
| **預測日誌儲存** | 本地 **SQLite** 為中央儲存；其他應用（匯出、分析）自 SQLite 讀取。 | 離線時資料仍寫入 SQLite；GCP 恢復後由匯出程式補傳。 |
| **匯出程式** | 自 SQLite 週期性讀取、匯出為壓縮檔（如 Parquet 壓縮或 gzip CSV）、上傳至 MLflow（GCP）。 | **獨立 process**（如 cron 或獨立腳本）執行，週期可調（如 5–15 分鐘）；失敗不影響 scorer。 |

### 3.3 溯源查詢

- 給定 model_version，從 MLflow（GCP Tracking Server）查 run 的 params/tags/artifact_uri。

### 3.4 資料品質、Drift、Skew（Evidently）

| 模組／範圍 | 職責 | Phase 2 變更 |
|------------|------|----------------------|
| **Evidently** | DQ、drift、skew。 | **本地**執行；報告寫本地目錄；可選 sync 到 GCS。**Drift**（生產 vs 訓練分佈）為主；**skew**（同 key 特徵一致）為輔。Reference 於訓練結束時產出，隨模型版本。 |
| **告警傳遞** | 將觸發結果送達人。 | **未來項目**；Phase 2 不實作。 |

### 3.5 Drift 根因調查（P1.6）

- **觸發**：Validator precision 或 Evidently drift 報告異常時啟動調查。
- **假說方向**：Data quality 異常、data drift（輸入分佈）、concept drift（X→Y 關係）、validator 與 hold-out 評估口徑差異、training–serving skew。
- **檢驗**：使用 P1.1 部署預測日誌、P1.4 DQ 報告、Evidently drift（reference vs current）、P0 溯源與 training_metrics.json；必要時做 slice 分析（時段、群組等）。回答：What changed? When? Why does it affect precision@recall=1%?
- **產出**：根因報告（假說、檢驗結果、結論、建議動作）。**正式紀錄**存於 **doc/**（markdown）；可選將摘要或連結存於 MLflow artifact。Phase 2 至少完成一輪並留下紀錄。

---

## 4. 資料流

### 4.1 訓練

```
run_pipeline(args)
  → 產出 artifact 目錄
  → 寫入 MLflow run（params/tags：溯源）→ GCP Tracking Server（artifact 存 GCS）
  （需可連 GCP）
```

### 4.2 部署推論與日誌

```
Scorer：artifact（包內）→ 算分 → state.db alerts
      → 每筆預測 → 本地 SQLite（預測日誌 table）

匯出程式（獨立 process）：SQLite → 週期性匯出（如 Parquet 壓縮）→ 上傳 MLflow artifact（GCS）
  （可連 GCP 時上傳；離線時保留於 SQLite，恢復後補傳）
```

### 4.3 Evidently

```
On-prem 資料（ClickHouse / Parquet）→ 本地跑 Evidently → 報告寫本地
                                    → [可選] sync 報告 → GCS
```

### 4.4 Validator（不變）

```
state.db alerts → validator (ClickHouse) → validation_results
```

---

## 5. 主要取捨（Trade-offs）

### 5.1 GCP Tracking Server（e2-micro + SQLite + GCS）

- **優點**：多機與 production 可連 GCP 時單一視圖、查詢穩定；backend 用 DB（SQLite）符合 MLflow 建議；artifact 存 GCS 可擴充；e2-micro 在免費額度內，成本約 $0 + GCS。
- **代價**：需在 GCP 維護一台 e2-micro（或升級為 Cloud Run + Cloud SQL）；無 GCP 連線時不寫入 MLflow（Phase 2 不實作本地 fallback）。若未來 production 無法連網卻需 MLflow，再實作 standalone 本地版（SQLite）。

### 5.2 預測日誌（SQLite 中央儲存 + 獨立匯出）

- Scorer 僅寫入**本地 SQLite**，不阻塞、不累積於記憶體。**匯出程式**在**獨立 process** 執行（避免 GIL 阻塞 scorer），週期性（如 5–15 分鐘，可調）自 SQLite 讀取、匯出為壓縮檔（建議 Parquet 壓縮或 gzip CSV 以省頻寬）、上傳至 MLflow artifact（GCS），以控制單檔大小與檔案數量。

### 5.3 Evidently 與原始資料

- 原始資料僅在 on-prem。Evidently 在本地時直接讀 ClickHouse/Parquet；若日後在 GCP 跑 Evidently 服務，僅接收 on-prem 送來的彙總／抽樣，不從 GCS 讀 raw data。Evidently 可讀寫 **logs／報告** 至 GCS。

### 5.4 定價（Evidently）

- Phase 2 使用 **Evidently 開源**，無授權費。若僅 sync 報告至 GCS、不跑 Evidently 服務於 GCP，則僅 **GCS 費用**。Evidently Cloud（資料會離境）不採用。

---

## 6. 失敗情境與處理（Failure Modes）

| 情境 | 影響 | 建議處理 |
|------|------|----------|
| GCP Tracking Server 不可用 | 無法上傳 MLflow。 | 訓練記錄 warning、跳過寫入。Scorer 預測日誌仍寫入**本地 SQLite**；匯出程式跳過上傳、保留資料於 SQLite，連線恢復後補傳。不讓主流程崩潰。 |
| Evidently 報告 sync 到 GCS 失敗 | 報告未上傳 GCS。 | 本地仍有報告；可重試或手動上傳。 |
| Evidently 執行失敗 | 當次無 DQ/drift 報告。 | 不影響 scorer/validator；可設計「報告缺失」提醒（未來）。 |
| 本地磁碟空間不足 | SQLite、MLflow 或 Evidently 寫入失敗。 | 監控磁碟；清理舊 run/報告、預測日誌或擴充容量。 |
| Rollback 時錯用不同 feature spec | Training–serving skew。 | P0.2：rollback 僅能「整目錄」或整包替換，禁止只換 model.pkl。 |

---

## 7. 遷移與相容性

### 7.1 既有部署

- **MODEL_DIR**：仍指向 artifact 目錄（包內）；scorer 不依賴 MLflow 連線即可運行。
- **STATE_DB_PATH**：不變；必要時以 ADD COLUMN 相容。預測日誌可存於同一 DB 或另一 SQLite 檔（schema 待訂）。

### 7.2 訓練產物

- 既有 artifact 檔名與格式不變；溯源僅透過 MLflow（GCP Tracking Server）。

### 7.3 不變更範圍

- Validator 輸入輸出與 state.db 結構（除必要欄位外）不變。
- Scorer 的 scoring、artifact 載入、state.db 寫入不變；僅新增預測寫入本地 SQLite；匯出與上傳由獨立程式負責。
- CLI 入口與必選參數相容；新功能以可選參數或環境變數啟用（如 `MLFLOW_TRACKING_URI`、是否啟用預測日誌、sync 開關）。

---

## 8. 與 SSOT 的對照

| SSOT 項目 | 本計畫對應 |
|-----------|------------|
| P0.1 溯源 | §3.1 寫入 MLflow run（GCP）；§3.3 查詢；Phase 2 不實作 sidecar。 |
| P0.2 特徵與模型版本化 | 同目錄即同版本；rollback = 整包/整目錄；§6。 |
| P1.1 部署預測日誌 | §3.2 Scorer 寫本地 SQLite；匯出程式週期性匯出並上傳 MLflow（GCP）；離線時保留於 SQLite、恢復後補傳。 |
| P1.2 告警與 runbook | Phase 2 文件化告警條件與 runbook；傳遞（Slack/email）＝未來。 |
| P1.3 Human-oriented 告警 | Phase 2 定義告警訊息格式與觸發原因說明；傳遞＝未來。 |
| P1.4 資料品質 | §3.4 Evidently 本地；可選 sync 報告到 GCS。 |
| P1.5 Skew 驗證 | §3.4 Evidently（為輔）；同 key 特徵一致。 |
| P1.6 Drift 調查 | §3.5 調查流程（觸發、假說、檢驗、產出）；正式紀錄存 doc/（markdown）；Evidently + MLflow + validator。 |

---

## 9. 待確認與實作備註（Clarifications）

以下為實作時需再釐清或決策的點，請依實際環境確認：

1. **GCP Tracking Server 部署**：e2-micro 區域、防火牆、開機自動啟動 MLflow server、GCS bucket 與服務帳號權限；**artifact 由客戶端直傳 GCS**，不經 e2-micro。
2. **MLFLOW_TRACKING_URI**：訓練機與匯出程式設為 GCP URL；Phase 2 不實作 fallback，無 GCP 時不寫 MLflow；預測日誌仍寫 SQLite。
3. **預測日誌 SQLite**：schema（table、欄位）、與 state.db 同檔或分檔、保留策略。
4. **匯出程式**：觸發方式（cron vs 內建 timer）、週期（5–15 分鐘可調）、匯出格式（Parquet 壓縮建議 gzip/snappy，或 gzip CSV 省頻寬）。
5. **Evidently 報告格式**：本地目錄結構與檔名約定，以便設計「sync 報告到 GCS」的腳本或路徑。
6. **Evidently 輸入**：若日後在 GCP 上提供 Evidently API，on-prem 端送「彙總／抽樣」的 payload 格式與頻率（batch 檔、或小批次 API 呼叫）。

若上述有既定決策或偏好，可補充至本節或 SSOT，以利實作對齊。

---

*本文件為 Phase 2 P0–P1 實施之架構與決策依據；若與 SSOT 衝突，以 SSOT 為準。*
