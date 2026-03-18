# Phase 2 P0.2：Provenance 查詢 Runbook

> 如何用 `model_version` 在 MLflow 查詢訓練溯源（provenance）。  
> 依據：`doc/phase2_provenance_schema.md`、`.cursor/plans/PLAN_phase2_p0_p1.md` T2/T3。

---

## 前提

- **MLFLOW_TRACKING_URI** 已設定且可連線（GCP 或本地 tracking server）。
- 訓練完成後，trainer 會以 `run_name=model_version` 建立 MLflow run，並將 provenance 寫入該 run 的 **params**（見 `doc/phase2_provenance_schema.md`）。

---

## 方法一：MLflow UI

1. 開啟 MLflow UI（例如 `http://<tracking-server>:5000` 或 GCP 對應 URL）。
2. 在 **Experiments** 中選取對應 experiment（預設為 "Default"）。
3. 在 run 列表中：
   - 以 **Run Name** 搜尋或篩選：輸入 `model_version` 字串（格式通常為 `YYYYMMDD-HHMMSS-<git7>`，例如 `20260318-120000-abc1234`）。
   - 或依 **Start Time** 排序，對照訓練時間找到對應 run。
4. 點進該 run：
   - **Parameters** 中可看到：`model_version`、`git_commit`、`training_window_start`、`training_window_end`、`artifact_dir`、`feature_spec_path`、`training_metrics_path`。
   - **Artifacts** 若有上傳，可在此檢視或下載。

---

## 方法二：MLflow API（Python）

```python
import mlflow
from mlflow.tracking import MlflowClient

# 若尚未設定
# mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])

client = MlflowClient()
# 依 run_name 查詢（model_version 即 run_name）
# 注意：可能有多個同名 run，依 start_time 或 run_id 區分
runs = client.search_runs(
    experiment_ids=["0"],  # 或實際 experiment_id
    filter_string="tags.`mlflow.runName` = '20260318-120000-abc1234'",  # 替換為目標 model_version
    order_by=["start_time DESC"],
    max_results=10,
)
if runs:
    run = runs[0]
    params = run.data.params
    print("model_version:", params.get("model_version"))
    print("training_window_start:", params.get("training_window_start"))
    print("training_window_end:", params.get("training_window_end"))
    print("git_commit:", params.get("git_commit"))
```

- **Experiment ID**：可從 UI 取得，或 `client.get_experiment_by_name("Default").experiment_id`。
- 若 run 是以 **params** 寫入（本專案 T2 實作），用 `run.data.params`；若為 tags，用 `run.data.tags`。

---

## 方法三：CLI

```bash
# 列出 run（需 jq 或手動解析）
mlflow runs list --experiment-name Default

# 單一 run 詳情（需 run_id）
mlflow runs describe --run-id <run_id>
```

取得 `run_id` 後，可用 API 或 UI 查 params。

---

## 鍵名對照

| 鍵名 | 說明 |
|------|------|
| `model_version` | 與 artifact 目錄內 `model_version` 檔案一致。 |
| `training_window_start` / `training_window_end` | 訓練實際使用之視窗（effective window）。 |
| `git_commit` | 訓練當下 repo commit（可為 `nogit`）。 |
| 其餘 | 見 `doc/phase2_provenance_schema.md`。 |

---

## 手動驗證建議

1. 執行一次訓練（或使用既有 run），記下產出的 `model_version`。
2. 在 MLflow UI 以該字串搜尋 Run Name，確認可找到對應 run 且 Parameters 含上述鍵。
3. 用 API 腳本查詢同一 `model_version`，確認 `run.data.params` 一致。
