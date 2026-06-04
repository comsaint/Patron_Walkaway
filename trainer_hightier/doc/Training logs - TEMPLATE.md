# Training Logs - TEMPLATE (v2)

本文件定義訓練流程輸出的 **`run_report.json`** 巢狀 schema。自 2026-06 起，原獨立檔 **`run_summary.json`**、**`metrics_detailed.json`**、**`pipeline_debug.json`** 已合併進 **`run_report.json`**，不再單獨落盤。

同 bundle 目錄內另見：

- **`training_metrics.json`** — Step 5 評估契約（flat keys；deploy / backtest 讀取）
- **`split_report.json`** — Step 4 時間切分
- **`feature_parity_verification.json`** / **`deploy_e2e_gate_report.json`** — Step 6 gate（若啟用）

---

## `run_report.json` 頂層

| 欄位 | 用途 |
|------|------|
| `schema` | 固定 `trainer_hightier.run_report.v1` |
| `run_id` | 同 `model_version` |
| `status` | `SUCCESS` / `FAILED` |
| `error` | 失敗時例外摘要；成功為 `null` |
| `summary` | 跨 run 比較（原 `run_summary.json`） |
| `evaluation_detail` | 調參／報表（原 `metrics_detailed.json`） |
| `pipeline_debug` | 工程除錯（原 `pipeline_debug.json`） |
| `gates` | Step 4.5 / Step 6 gate 路徑與 verdict |
| `artifacts` | 主要 artifact 絕對路徑指標 |

```json
{
  "schema": "trainer_hightier.run_report.v1",
  "run_id": "20260517-015601-891d420",
  "status": "SUCCESS",
  "error": null,
  "summary": { "...": "見下節" },
  "evaluation_detail": { "...": "見下節" },
  "pipeline_debug": { "...": "見下節" },
  "gates": {
    "pre_train_feature_gate": { "path": "...", "verdict": "pass" },
    "step6_parity": { "path": ".../feature_parity_verification.json", "n_failed_slow_gate": 0 },
    "step6_deploy_e2e": { "path": ".../deploy_e2e_gate_report.json", "verdict": "pass" }
  },
  "artifacts": {
    "training_metrics_path": ".../training_metrics.json",
    "model_path": ".../model.pkl",
    "split_report_path": ".../split_report.json",
    "model_bundle_dir": ".../out/models_high_tier_mvp/<run_id>"
  }
}
```

---

## 1) `summary`（跨 run 比較，必看）

```json
{
  "run_id": "20260517-015601-891d420",
  "started_at": "2026-05-17T01:56:01Z",
  "finished_at": "2026-05-17T03:52:21Z",
  "duration_sec": 6980.2,
  "data_scope": {
    "population_scope": "adt_filtered_bets_when_enabled",
    "patron_sampling_ratio": 0.1,
    "patron_sampling_ratio_source": "explicit"
  },
  "model": {
    "algorithm": "lightgbm",
    "n_features_used": 30,
    "feature_list_sha256_hex": "abc..."
  },
  "thresholding": {
    "policy": "min_precision",
    "policy_param": { "min_precision": 0.6 },
    "selected_threshold": 0.5589
  },
  "metrics": {
    "val": { "ap": 0.5031, "precision": 0.6, "recall": 0.0948, "f1": 0.1638, "alerts_per_hour": 63.51 },
    "test": { "ap": 0.4959, "precision": 0.5939, "recall": 0.0904, "f1": 0.1569, "alerts_per_hour": 56.49 }
  },
  "optimization": {
    "enabled": true,
    "backend": "optuna",
    "max_time_sec_configured": 1800,
    "wall_time_sec_actual": 1693.2,
    "trials_completed": 121,
    "stopping_reason": "time_budget_exhausted",
    "best_value": 0.0948
  },
  "git_commit_short": "891d420",
  "run_profile": "default",
  "split_periods": { "...": "Step 4 gaming_day 邊界" }
}
```

### `summary` 必填欄位（精簡版）

- `run_id`
- `data_scope.patron_sampling_ratio`
- `thresholding.selected_threshold`
- `metrics.val` + `metrics.test` 的 `ap/precision/recall/f1`
- `optimization.max_time_sec_configured` / `wall_time_sec_actual` / `trials_completed` / `stopping_reason`

---

## 2) `evaluation_detail`（分析檔）

結構同原 `metrics_detailed.json`：`split_metrics`、`threshold_analysis`、`budget_points`、`feature_columns`、`candidate_registry`、`split_periods`。

---

## 3) `pipeline_debug`（工程除錯）

結構同原 `pipeline_debug.json`：`cache`、`partition`、`timings_sec`、`feast_auto_apply`、`artifacts`（repo-relative 路徑）、dedup bucket 設定等。

---

## Include / Exclude 規則

### Include

- 與可比較性直接相關：抽樣比例、threshold policy、核心 metrics、Optuna 時間預算
- 與 debug 直接相關：各階段耗時、cache 命中、gate verdict

### Exclude

- 重複 flat metrics（數值指標以 **`training_metrics.json`** 為準）
- 機器綁定絕對路徑在 `pipeline_debug.artifacts` 內改為 repo-relative

---

## 落盤位置

- Step 5 有跑：`out/models_high_tier_mvp/<model_version>/run_report.json`
- 僅 `--skip-step5`：`{output_dir}/run_report.json`（versions 根）

失敗 run 也會 finalize 已完成的 JSON（`status=FAILED`，`error` 有值）。
