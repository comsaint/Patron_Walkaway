# Phase 2 P1.2/P1.3：Alert Runbook

> 告警與異常的 triage 程序：誰看、看哪裡、怎麼處理。  
> 依據：`.cursor/plans/PLAN_phase2_p0_p1.md` T7。Phase 2 不實作 Slack/email 傳遞，本 runbook 供人工查閱與處理。

---

## 原則

1. **Scorer / Export / Validator / Evidently** 各自可能產生需關注的異常或報告；處理時先區分來源與嚴重度。
2. **誰看**：營運或維運人員依本 runbook 與 `doc/phase2_alert_message_format.md` 的訊息格式，判斷是否需介入或升級。
3. **看哪裡**：依下表對應的 DB、artifact、報告路徑查證。

---

## 常見異常與對應查證位置

| 來源 | 常見異常 | 誰看 | 看哪個 DB / artifact / report |
|------|----------|------|--------------------------------|
| **Scorer** | 無法載入 artifact、特徵對齊錯誤、推論失敗、state.db 寫入失敗 | 維運 | `MODEL_DIR`（artifact 目錄）、scorer log；`state.db`（alerts 表）；`PREDICTION_LOG_DB_PATH`（prediction_log 表） |
| **Export** | MLflow 不可達、上傳失敗、watermark 未前進、prediction_log 表不存在 | 維運 | `PREDICTION_LOG_DB_PATH`（prediction_export_meta、prediction_export_runs）；MLflow UI（GCP）；export script log |
| **Validator** | precision 掉落、大量 PENDING 未結案、驗證逾時 | 營運／維運 | Validator 輸出或 API（alerts、verdict）；`state.db` 或 validator 專用 DB；validator log |
| **Evidently** | DQ / drift 報告異常、OOM、reference 與 current 差異過大 | 維運／資料 | 本地報告（HTML/JSON）路徑；`out/` 或 doc 下 Evidently 輸出；`doc/phase2_evidently_usage.md` |

---

## Triage 情境與步驟

### 情境零：Scorer 無法載入 artifact

1. **現象**：Scorer 啟動失敗或無法載入 artifact、特徵對齊錯誤。
2. **查證**：  
   - 檢查 `MODEL_DIR` 是否存在、是否為完整 bundle（含 feature_list、model 檔）。  
   - 確認 `model_version` 與 feature_list 一致；查 scorer log。
3. **處理**：若為 artifact 缺檔或損壞，依 `doc/phase2_model_rollback_runbook.md` 還原或重新部署。

### 情境一：Export 失敗

1. **現象**：匯出程式 log 出現上傳失敗或 watermark 未更新。
2. **查證**：  
   - 檢查 `MLFLOW_TRACKING_URI` 是否可連、GCS 權限是否正常。  
   - 查 `prediction_export_runs` 表最後一筆是否 `success=0` 或無新筆。  
   - 確認 `prediction_log` 表存在且 scorer 有寫入。
3. **處理**：修復連線或權限後重新執行 export（同一批可重試，watermark 未前進則不會漏資料）。必要時以 `--no-cleanup` 先停清理、僅做匯出。

### 情境二：Validator precision 掉落

1. **現象**：Validator 回報或儀表顯示 precision 明顯低於預期。
2. **查證**：  
   - 對照近期 alerts 與 verdict（MATCH / MISS / PENDING）分布。  
   - 確認 scorer 使用的 `model_version` 與訓練／回測一致；必要時查 `doc/phase2_provenance_query_runbook.md`。
3. **處理**：若為模型或特徵對齊問題，依 `doc/phase2_model_rollback_runbook.md` 評估回滾；若為標註或業務條件變更，需與業務端確認閾值或流程。

### 情境三：Drift report 異常

1. **現象**：Evidently drift 報告顯示特徵或目標分布與 reference 差異大。
2. **查證**：  
   - 確認 reference 對應的 `model_version` 與目前 production 是否一致。  
   - 檢視報告內具體 drift 的欄位與幅度；區分資料品質問題與分布自然變化。
3. **處理**：若為資料 pipeline 或 upstream 問題則修正來源；若為合理分布變化，可更新 reference 或記錄為已知變更。Evidently 執行有 OOM 風險，見 `doc/phase2_evidently_usage.md`。調查時可依 **`doc/drift_investigation_template.md`** 填寫正式紀錄並存於 `doc/`。若報告含敏感資訊（如真實 run ID、主機名、內部連結），應脫敏或僅存於內部儲存，**勿 commit 至可對外 repo**。

---

## 手動驗證建議

1. 模擬 **export 失敗**：暫時關閉 MLflow 或設錯 URI，跑一次 export，依 runbook 查 `prediction_export_runs` 與 watermark。
2. 模擬 **validator precision 掉落**：以測試資料或歷史資料比對 verdict 與預期。
3. 模擬 **drift report 異常**：產出一份 Evidently 報告，依 runbook 對照 reference 與 current、判斷 triage 步驟是否可跟隨。

---

## 相關文件

- **訊息格式**：`doc/phase2_alert_message_format.md`（human-oriented 訊息應包含欄位）
- **Provenance 查詢**：`doc/phase2_provenance_query_runbook.md`
- **Model rollback**：`doc/phase2_model_rollback_runbook.md`
- **Evidently 使用**：`doc/phase2_evidently_usage.md`
- **Drift 調查模板與範例**：`doc/drift_investigation_template.md`、`doc/phase2_drift_investigation_example.md`
