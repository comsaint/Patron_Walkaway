# Phase 2 P0–P1：Provenance Schema（MLflow 溯源欄位）

> 本文件定義訓練完成後寫入 MLflow run 的 **provenance 鍵名與語義**，供 trainer 與 export script 共用，並供查詢／rollback runbook 參考。
> 依據：`.cursor/plans/PLAN_phase2_p0_p1.md` T1、T2；`doc/phase2_p0_p1_implementation_plan.md`。

---

## 鍵名與說明

| 鍵名 | 類型 | 說明 |
|------|------|------|
| `model_version` | string | 模型版本識別（與 artifact 目錄內 `model_version` 檔案一致）。 |
| `git_commit` | string | 訓練當下 repo 的 git commit（可選，用於重現）。 |
| `training_window_start` | string | 訓練資料窗口起始（ISO 或 YYYY-MM-DD）。 |
| `training_window_end` | string | 訓練資料窗口結束。 |
| `artifact_dir` | string | 本機或 deploy 上 artifact 目錄路徑（僅供記錄，查詢時以 MLflow artifact 為準）。 |
| `feature_spec_path` | string | Feature spec 檔案路徑或識別（如 `feature_spec.yaml`）；可含 feature schema version。 |
| `training_metrics_path` | string | 訓練指標檔案路徑（如 `training_metrics.json`）。 |

以上欄位以 **MLflow params 或 tags** 寫入 run；具體以 `trainer.core.mlflow_utils` 實作為準。  
給定 `model_version`，可於 MLflow UI 或 API 以 tag/param 查詢對應 run 與 artifact。
