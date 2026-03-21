# 計畫：Pipeline 診斷檔、MLflow System Metrics 與訓練 Artifacts

> 狀態：**Planned**（待實作）  
> 摘要：將自訂 pipeline／資源診斷自 `training_metrics.json` 拆出至 `pipeline_diagnostics.json`；部署 bundle 一併打包；透過環境變數啟用 MLflow 內建 system metrics；訓練成功路徑上傳小檔至 MLflow Artifacts。

---

## 目標

| # | 項目 | 說明 |
|---|------|------|
| 1 | **本機拆檔** | 自訂 pipeline／資源／耗時等診斷寫入 **`pipeline_diagnostics.json`**；**`training_metrics.json` 維持**僅模型效能與既有審計欄位（`rated`、`neg_sample_frac`、`spec_hash` 等）。 |
| 2 | **MLflow 內建 system metrics** | 透過 **`MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING=true`** 寫入 `credential/mlflow.env.example`（並在文件註明實際 `mlflow.env` 可開）；確保訓練環境已安裝 **`psutil`**。 |
| 3 | **MLflow Artifacts（訓練 run）** | 訓練成功路徑對當次 run 呼叫 **`log_artifact_safe`**，讓 UI **Artifacts** 分頁有內容。 |

### 非目標（避免 scope creep）

- 預設**不**將大型 **`model.pkl`** 上傳為 MLflow artifact（儲存與上傳時間需另案評估）。
- **不**改變 **`/model_info` 契約**（仍只讀本機 `training_metrics.json`）。

---

## 1. 新檔 `pipeline_diagnostics.json`

### 建議欄位（初版）

- **`model_version`**：與 artifact bundle 一致，便於對照。
- **時間**：`pipeline_started_at` / `pipeline_finished_at`（ISO8601，若易取得）；至少保留與現有程式一致的總耗時語意。
- **步驟耗時**（與 `run_pipeline` 內變數對齊）：`total_duration_sec`、`step7_duration_sec`、`step8_duration_sec`、`step9_duration_sec`。
- **記憶體／系統／OOM**：`step7_rss_start_gb`、`step7_rss_peak_gb`、`step7_rss_end_gb`、`step7_sys_available_min_gb`、`step7_sys_used_percent_peak`、`oom_precheck_est_peak_ram_gb`、`oom_precheck_step7_rss_error_ratio` 等。

### 序列化約定

- 團隊需二選一並寫死：**省略 `None` 鍵** 或 **寫入 JSON `null`**；建議與 `training_metrics.json` 風格一致。

### 產出時機

- 在 **`run_pipeline` 成功路徑**、Step 10 完成且與寫入 **`training_metrics.json`** 同一流程內組 dict 並寫入 **`MODEL_DIR / "pipeline_diagnostics.json"`**。
- **順序**：建議在 **`save_artifact_bundle`** 完成後再寫診斷檔，或將診斷 dict 由 `run_pipeline` 傳入 `save_artifact_bundle` 末尾一併寫入——實作時取**改動最小**處。

### 相容性

- 既有只讀 `training_metrics.json` 的下游：**無需**修改即可運作；新檔為**附加**產物。

---

## 2. 部署打包

- **`package/build_deploy_package.py`**：常數 **`BUNDLE_FILES`** 新增 **`pipeline_diagnostics.json`**（與 `training_metrics.json` 並列）。
- 確認 **`copy_model_bundle`** 對來源目錄**缺檔**時的行為（略過 vs 警告），與現有其他可選檔一致。

---

## 3. MLflow 內建 system metrics

依 [MLflow System metrics](https://mlflow.org/docs/latest/system-metrics/)：

- **`credential/mlflow.env.example`**：新增註解區塊，說明  
  `MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING=true`  
  及依賴 **`psutil`**（GPU 指標另可選 **`nvidia-ml-py`**）。
- **依賴**：檢查 **`pyproject.toml`**、**`package/build_deploy_package.py`** 的 `REQUIREMENTS_DEPS`、以及實際訓練環境；若未宣告 **`psutil`**，於會執行訓練且啟用 MLflow 的環境補上，避免變數已開卻無採樣。
- **實作備註**：無需修改 **`safe_start_run`** 簽名即可依賴全域 env（`start_run` 時 MLflow 讀取環境變數）。若日後需「僅 train 開、export 關」，可另議在 **`safe_start_run`** 轉發 **`log_system_metrics`**。

---

## 4. MLflow Artifacts（訓練 run）

### 建議上傳（小檔、高價值）

- **`training_metrics.json`**
- **`pipeline_diagnostics.json`**
- **`feature_spec.yaml`**（若路徑固定且體積可接受）
- **`model_version`**（純文字）

### 實作要點

- 在訓練成功、**已有 active run** 時，於 **`warm_up_mlflow_run_safe`／provenance／metrics** 區塊附近呼叫 **`log_artifact_safe`**。
- **`artifact_path`**：建議前綴例如 **`bundle/`** 或 **`training/`**，避免與其他流程的 artifact 混在根目錄。
- **錯誤策略**：與現有 MLflow 區塊一致——**best-effort**、**不影響訓練成功**；單檔失敗可 **`logger.warning`** 並繼續其餘檔案。

---

## 5. Provenance（建議）

- **`doc/phase2_provenance_schema.md`**：表格新增 **`pipeline_diagnostics_path`**；可補一句「訓練 run 可能上傳至 MLflow Artifacts 的檔案清單」。
- **`_log_training_provenance_to_mlflow`**（`trainer/training/trainer.py`）：`params` 增加 **`pipeline_diagnostics_path`**，值為 `str(MODEL_DIR / "pipeline_diagnostics.json")`。

---

## 6. 測試

- **單元／整合**：迷你 pipeline 或 `save_artifact_bundle` 路徑——斷言 **`pipeline_diagnostics.json`** 存在、為合法 JSON、含 **`model_version`** 與至少一項 duration 或 OOM 相關鍵（依實作調整 assert）。
- **Mock MLflow（可選）**：驗證成功路徑對 **`log_artifact_safe`** 的呼叫次數或路徑列表。
- **打包**：若有測試涵蓋 **`BUNDLE_FILES`**，更新預期清單。

---

## 7. 文件

- **README**（或 trainer／deploy 小節）：說明 **`models/pipeline_diagnostics.json`** 用途；部署 bundle 含此檔。
- **`credential/mlflow.env.example`**：system metrics 與 **psutil** 說明（見 §3）。

---

## 8. 驗收清單（手動）

- [ ] 本機訓練完成後：`trainer/models/pipeline_diagnostics.json`（或 `out/models/`，依 `MODEL_DIR`）存在且欄位合理。
- [ ] `python -m package.build_deploy_package` 產物之 `models/` 含 **`pipeline_diagnostics.json`**（來源目錄有該檔時）。
- [ ] MLflow 該 run：**Metrics** 出現內建 **`system/*`**（已設 env + **psutil**）；**Artifacts** 可見 **`training_metrics.json`** 及計畫中其他小檔。

---

## 參考

- 現有 MLflow 輔助：`trainer/core/mlflow_utils.py`（`log_artifact_safe`、`safe_start_run` 等）。
- 訓練 run 與 metrics：`trainer/training/trainer.py`（`run_pipeline`、`_log_training_provenance_to_mlflow`）。
- 既有溯源鍵名：`doc/phase2_provenance_schema.md`。
