# 計畫：Pipeline 診斷檔、MLflow System Metrics 與訓練 Artifacts

> 狀態：**部分實作**（§1–§5 已完成；**§6 自動化測試已覆蓋計畫主力**—含靜態／AST 契約與 Reviewer MRE；**可選**整合項仍見 §6；**§7 已實作**；**§8 仍待人工**）  
> 摘要：將自訂 pipeline／資源診斷自 `training_metrics.json` 拆出至 `pipeline_diagnostics.json`；部署 bundle 一併打包；透過環境變數啟用 MLflow 內建 system metrics；訓練成功路徑上傳小檔至 MLflow Artifacts。`step7_rss_*` 與 OOM 預檢欄位的來源明確對齊現有 `run_pipeline` 實作。

---

## 目標

| # | 項目 | 說明 |
|---|------|------|
| 1 | **本機拆檔** | ✅ **已實作**：自訂 pipeline／資源／耗時等診斷寫入 **`pipeline_diagnostics.json`**；**`training_metrics.json` 維持**僅模型效能與既有審計欄位（`rated`、`neg_sample_frac`、`spec_hash` 等）。 |
| 2 | **MLflow 內建 system metrics** | ✅ **已實作**：`credential/mlflow.env.example` 註解區塊（`MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING`、`psutil`、GPU 可選、`train`/`export` 行程說明）；**`pyproject.toml`** 增加 **`[project.optional-dependencies] mlflow-system-metrics`**（`psutil`）。純 API 部署可不裝該 extra。 |
| 3 | **MLflow Artifacts（訓練 run）** | ✅ **已實作**：`run_pipeline` 成功路徑在寫入 **`pipeline_diagnostics.json`** 後，若 **`has_active_run()`** 則 **`log_artifact_safe`** 上傳 `training_metrics.json`、`pipeline_diagnostics.json`（若存在）、`feature_spec.yaml`、`model_version` 至 **`bundle/`** 前綴（best-effort，與 `log_artifact_safe` 一致）。 |

### 非目標（避免 scope creep）

- 預設**不**將大型 **`model.pkl`** 上傳為 MLflow artifact（儲存與上傳時間需另案評估）。
- **不**改變 **`/model_info` 契約**（仍只讀本機 `training_metrics.json`）。
- 不在本計畫中重構 OOM helper（`_oom_check_and_adjust_neg_sample_frac` 等）的 log 介面，只在需要時在 `run_pipeline` 端重算必要欄位。

---

## 1. 新檔 `pipeline_diagnostics.json`

### 建議欄位（初版）

- **`model_version`**：與 artifact bundle 一致，便於對照。
- **時間**：  
  - `pipeline_started_at`：ISO8601；在 `run_pipeline` 入口、進入 `with safe_start_run` 前後記錄（例如 `datetime.now(timezone.utc)` 或 HK 時區），代表整段 pipeline 開始時間。  
  - `pipeline_finished_at`：ISO8601；在成功路徑尾端寫入。
- **步驟耗時**（與 `run_pipeline` 內變數對齊；以現有 `time.perf_counter()` 為準）：  
  - `total_duration_sec`  
  - `step7_duration_sec`  
  - `step8_duration_sec`  
  - `step9_duration_sec`
- **記憶體／系統／OOM（來源對齊現有實作）**：  
  - `step7_rss_start_gb`、`step7_rss_peak_gb`、`step7_rss_end_gb`：來自 `run_pipeline` 中使用 `psutil.Process().memory_info().rss` 的採樣（Step 7 起點、期間 peak、Step 9 結束），**不是**來自 OOM helper 的回傳值。  
  - `step7_sys_available_min_gb`、`step7_sys_used_percent_peak`：如現有程式有記錄系統層級最小 available / 最大 used%，則一併納入；若目前僅在 log 中出現，可評估是否在 Step 7/9 區塊補一次 `psutil.virtual_memory()` 採樣。  
  - `oom_precheck_est_peak_ram_gb`：沿用 `run_pipeline` 目前在呼叫 `_oom_check_and_adjust_neg_sample_frac` 後所計算的估計峰值（約 4629–4652 行），**來源是該段估算邏輯，而非 helper return**。  
  - `oom_precheck_step7_rss_error_ratio`：與現有 `trainer.py` 及 MLflow metrics **同名同義**，定義為 **`step7_rss_peak_gb / oom_precheck_est_peak_ram_gb`**（實測 RSS 尖峰 ÷ 預檢估計峰值；>1 表示實際尖峰高於預檢估計）。欄位名含 `error` 但語意為 **observed/estimated 比**，與既有 run 對齊；若任一側不可得則可寫 `null` 或略過該鍵。

> 若未來希望 JSON 內還要包含 OOM print 裡目前才有的細節（例如當下 `total_ram`、`available`、`ram_budget`、`size_source` 字串），則需另案將 `_oom_check_and_adjust_neg_sample_frac` / `_oom_check_after_chunk1` 改為回傳 dataclass／dict，或在 `run_pipeline` 端重算一次這些欄位。

### 序列化約定

- 團隊需二選一並寫死：**省略 `None` 鍵** 或 **寫入 JSON `null`**。  
- 建議與 `training_metrics.json` 風格一致（如目前是省略 `None`，此檔也採同樣策略）。

### 產出時機

- 在 **`run_pipeline` 成功路徑**、Step 10 完成且與寫入 **`training_metrics.json`** 同一流程內組 dict 並寫入 **`MODEL_DIR / "pipeline_diagnostics.json"`**。
- **順序**：  
  - 建議在 **`save_artifact_bundle`** 完成後再寫診斷檔，避免 bundle 寫入失敗但診斷檔顯示成功；  
  - 或將診斷 dict 由 `run_pipeline` 傳入 `save_artifact_bundle` 末尾一併寫入——實作時取**改動最小**處。

### 相容性

- 既有只讀 `training_metrics.json` 的下游：**無需**修改即可運作；新檔為**附加**產物。

---

## 2. 部署打包

- **`package/build_deploy_package.py`**：常數 **`BUNDLE_FILES`** 新增 **`pipeline_diagnostics.json`**（與 `training_metrics.json` 並列）。
- **缺檔行為說明**：  
  - ✅ **已實作**：`copy_model_bundle` 對 **`pipeline_diagnostics.json`**：若來源缺檔則 **`logger.warning`** 一次（不 raise、不拷貝）；其餘 **`BUNDLE_FILES`** 仍為 `exists` 才 `copy2`、缺檔靜默略過。

---

## 3. MLflow 內建 system metrics

依 [MLflow System metrics](https://mlflow.org/docs/latest/system-metrics/)：

- **`credential/mlflow.env.example`**：新增註解區塊，說明  
  - `MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING=true`  
  - 依賴 **`psutil`**（GPU 指標另可選 **`nvidia-ml-py`**）。  
  - 若需要在 GPU 環境收集 GPU 指標，再評估加入 `nvidia-ml-py` 或相關套件。
- **依賴（分場景）**：檢查 **`pyproject.toml`**、**`package/build_deploy_package.py`** 的 `REQUIREMENTS_DEPS`、以及實際部署／訓練環境：  
  - **訓練映像**：若會開 `MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING`，需安裝 `psutil`。  
  - **export／批次匯出映像**：使用相同 `safe_start_run`，若也開啟 system metrics，則該行程也需要 `psutil`。  
  - **純 API 部署映像**：若不跑訓練、不開啟 system metrics，可不將 `psutil` 列入 `REQUIREMENTS_DEPS`，避免不必要依賴。
- **實作備註**：  
  - 無需修改 **`safe_start_run`** 簽名即可依賴全域 env（`start_run` 時 MLflow 讀取環境變數）。  
  - **Export run 也會吃到全域 `MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING`**：  
    - 目前 `export_predictions_to_mlflow.py` 使用同一個 `safe_start_run`，若 env 開啟，export run 也會有 `system/*` 指標。  
    - 若日後需「僅 train 開、export 關」，可另議在 **`safe_start_run`** 加入 `log_system_metrics` 參數，或分環境配置 env。

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
- **Tracking 不可用時的行為（明文化）**：  
  - `is_mlflow_available()` 為 False 時，`safe_start_run` 會退化為 `nullcontext()`，沒有 active run。  
  - 後續依賴 `has_active_run()` 的 artifact 上傳與 metrics logging 會**靜默跳過、不 raise**。  
  - 此行為屬**預期**，避免訓練流程因 tracking server 問題中斷；應在文件與驗收標註。

---

## 5. Provenance（建議）

- **`doc/phase2_provenance_schema.md`**：  
  - ✅ **已實作**：表格新增 **`pipeline_diagnostics_path`**、**`pipeline_diagnostics_rel_path`**；並簡述訓練 run 可能上傳至 Artifacts 的小檔清單（見該 doc 末段）。
- **`_log_training_provenance_to_mlflow`**（`trainer/training/trainer.py`）：  
  - ✅ **已實作**：`params` 含 **`pipeline_diagnostics_path`**（預設 `artifact_dir/pipeline_diagnostics.json`）、**`pipeline_diagnostics_rel_path`**（預設 `{MODEL_DIR.name}/pipeline_diagnostics.json`）；`run_pipeline` 以 **`MODEL_DIR`** 明確傳入兩者。
- **`doc/phase2_provenance_query_runbook.md`**：✅ **已實作**：Parameters 列表補上上述兩鍵。

---

## 6. 測試

> **進度（2026-03-21）**：已新增 **`tests/review_risks/test_review_risks_pipeline_diagnostics_mlflow_review.py`**（契約／MRE），涵蓋：`log_artifact_safe` 無重試、`run_pipeline` 內 bundle 上傳在 `log_metrics_safe(mlflow_metrics)` 前、bundle 迴圈無 bytes 門檻、`mlflow.env.example`／`pyproject` optional-deps、`has_active_run` 包住 bundle 等。另 **`tests/review_risks/test_review_risks_pipeline_provenance_review.py`**（STATUS Code Review §5 對應）：`run_pipeline` 內 provenance 與 `_write_pipeline_diagnostics_json` 之原始碼順序、`pipeline_diagnostics_rel_path` 空 basename MRE、bundle `is_file` 守衛、doc 含 `bundle/`；**`tests/integration/test_phase2_trainer_mlflow.py`** 延伸極長 `pipeline_diagnostics_path` 之 `log_params_safe` 單次呼叫契約；**`tests/review_risks/test_review_risks_readme_pipeline_artifacts_doc_contract.py`**（STATUS Pipeline §7 文件 Review）：`DEFAULT_MODEL_DIR` MRE、README 連結 plan doc、`bundle/`＋條件上傳語意、三語 `pipeline_diagnostics`／`bundle/` 計數、runbook Parameters／Artifacts。**`tests/unit/test_pipeline_diagnostics_build_and_bundle.py`**（2026-03-21）：`_write_pipeline_diagnostics_json` 產出 JSON 形狀（必要鍵、省略 `None`）、**`0.0` 保留**、**`default=str` 型別寬鬆 MRE**；`copy_model_bundle` 缺 **`pipeline_diagnostics.json`** 時 **`logger.warning`**（**僅一則** WARNING）。**`tests/review_risks/test_review_risks_pipeline_diagnostics_write_review.py`**（STATUS Code Review 寫檔／建包）：`_write_pipeline_diagnostics_json` 原始碼 **`write_text`** 與 **`default=str`** 契約、**無 `os.replace`**（非原子寫入 MRE，日後改原子須同步測試）；`copy_model_bundle` 僅 **`pipeline_diagnostics.json`** 缺檔分支 **`logger.warning`**。**`tests/review_risks/test_review_risks_pipeline_plan_section6_contract.py`**（2026-03-21）：`BUNDLE_FILES` 含 **`pipeline_diagnostics.json`** 且位於 **`training_metrics.json`** 之後；`run_pipeline` bundle 四檔名順序契約、迴圈內**單一** `log_artifact_safe(_ap`；`step7_rss_*` 由 **`memory_info().rss`** 採樣、`step7_rss_peak_gb = max(start,end)`、`oom_precheck_step7_rss_error_ratio` 除法 **`peak / oom_precheck_est_peak_ram_gb`**。單元 **`tests/unit/test_pipeline_diagnostics_build_and_bundle.py`** 延伸：同時寫入 RSS 全鍵與 **`oom_precheck_step7_rss_error_ratio`**。**仍缺（可選，非 CI 闸門）**：執行期依存在檔數 mock `log_artifact_safe` **呼叫次數**；端到端迷你 pipeline／凍結時鐘整合（見下列 bullet）。**已補（靜態／AST）**：`run_pipeline` 內 **`log_artifact_safe` Call 次數＝1**（`test_review_risks_pipeline_plan_section6_contract.py`），與「每檔存在則各呼叫一次」的迴圈語意相容。

- **單元／整合：pipeline_diagnostics 內容**  
  - 迷你 pipeline 或 `save_artifact_bundle` 路徑——斷言 **`pipeline_diagnostics.json`** 存在、為合法 JSON，包含：  
    - `model_version`  
    - `pipeline_started_at`、`pipeline_finished_at`（可用固定 fake clock 或 time-freezing）  
    - 至少一項 duration 鍵（如 `step7_duration_sec`）  
    - 至少一項 OOM 相關鍵（`oom_precheck_est_peak_ram_gb` 或 `step7_rss_peak_gb` 等，依實作調整 assert）。
- **OOM 預檢與 RSS 採樣路徑**  
  - 若有針對 `_oom_check_and_adjust_neg_sample_frac` 的測試，應確認：  
    - `oom_precheck_est_peak_ram_gb` 的計算路徑與該 helper 使用的估算邏輯一致。  
    - RSS 相關欄位（`step7_rss_*`）來自 `run_pipeline` 的 `psutil.Process()` 採樣，而非假設 helper 會 return。  
  - 如需更細的 OOM 詳細欄位，可另案抽出「估算 peak GB」為純函式方便單元測試。
- **Mock MLflow（可選）**  
  - 驗證成功路徑對 **`log_artifact_safe`** 的呼叫次數或路徑列表，確保 `training_metrics.json`、`pipeline_diagnostics.json` 等被上傳到預期的 `artifact_path`。
- **打包**  
  - 若有測試涵蓋 **`BUNDLE_FILES`**，更新預期清單，並驗證缺檔時對 `pipeline_diagnostics.json` 會產生一條 warning（但不影響流程）。

---

## 7. 文件

- **README**（或 trainer／deploy 小節）：  
  - ✅ **已實作**（2026-03-21）：根目錄 **`README.md`** 繁中／簡中「產物」與英文 **Artifacts** 小節已列 **`pipeline_diagnostics.json`**（欄位語意摘要：耗時、RSS、OOM 預檢比）、**部署建包**與 **MLflow `bundle/`** 說明，並連結本 doc 與 **`doc/phase2_provenance_schema.md`**；並註明預設 **`MODEL_DIR`／`out/models/`**（`DEFAULT_MODEL_DIR`）與環境變數覆寫（對齊 STATUS Code Review §7）。  
- **`credential/mlflow.env.example`**：  
  - ✅ **已實作**：§3 既有 system metrics／`psutil`／train+export 行程說明；另補 **MLflow UI → run → Metrics 分頁** 可檢視 **`system/*`** 時序之註解。  

---

## 8. 驗收清單（手動）

- [ ] 本機訓練完成後：`trainer/models/pipeline_diagnostics.json`（或 `out/models/`，依 `MODEL_DIR`）存在且欄位合理。（程式已寫入；請以實際跑訓練勾選。）  
- [ ] `python -m package.build_deploy_package` 產物之 `models/` 含 **`pipeline_diagnostics.json`**（來源目錄有該檔時）；缺檔時只出現 warning、不導致打包失敗。（`BUNDLE_FILES` 已納入；請以實際建包勾選。）  
- [ ] MLflow 該訓練 run：  
  - **Metrics** 出現內建 **`system/*`**（已設 env + 在該行程安裝 **psutil** 並允許 system metrics）。  
  - **Artifacts** 可見 **`training_metrics.json`**、`pipeline_diagnostics.json` 及計畫中其他小檔，路徑落在預期的 `bundle/` 或 `training/` 子目錄。  
- [ ] MLflow **export run**：  
  - 依預期策略確認：  
    - 若設計為也收 system metrics：`system/*` 存在。  
    - 若設計為關閉：在 export 行程未設 `MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING` 或改用 `log_system_metrics=False`，`system/*` 不應出現。

---

## 參考

- 現有 MLflow 輔助：`trainer/core/mlflow_utils.py`（`log_artifact_safe`、`safe_start_run` 等）。
- 訓練 run 與 metrics：`trainer/training/trainer.py`（`run_pipeline`、`_log_training_provenance_to_mlflow`；`oom_precheck_step7_rss_error_ratio` 定義約 5924–5926 行）。
- 既有溯源鍵名：`doc/phase2_provenance_schema.md`。
