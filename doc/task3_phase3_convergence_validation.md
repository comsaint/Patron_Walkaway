# Task 3 / Phase 3 收斂驗證 Runbook

對齊 `Task 3 — Phase 3` 的兩個剩餘 DoD：

1. scorer 週期 p95 前後對照（固定資料窗/閾值）
2. 同資料集 alert 集合與 schema 下游相容

## 前置原則（固定）

- 固定同一批資料窗、同一模型 artifacts、同一 threshold 設定。
- baseline 與 candidate 分開跑，避免共用同一個 `state.db`。
- 測試期間建議固定 `SCORER_ENABLE_SHAP_REASON_CODES=0`，避免額外波動。
- 所有比對都用 JSON 落地，確保可追溯與可重跑。

## A. p95 前後對照

### A1. 產生 log

- baseline 版本與 candidate 版本各自跑 scorer（至少 20 個 cycle，讓 p95 穩定）：
  - `python -m trainer.serving.scorer --log-level INFO`
- 分別保存 log：
  - `artifacts/task3_phase3/baseline_scorer.log`
  - `artifacts/task3_phase3/candidate_scorer.log`

### A2. 產生 p95 報告

- 執行：
  - `python trainer/scripts/task3_phase3_compare_p95.py --baseline-log artifacts/task3_phase3/baseline_scorer.log --candidate-log artifacts/task3_phase3/candidate_scorer.log --out-json artifacts/task3_phase3/p95_compare.json`

### A3. 驗收建議

- 主要 stage（通常是 `feature_engineering` / `sqlite` / `clickhouse`）：
  - `candidate_median_p95_sec <= baseline_median_p95_sec`
- 樣本數 `baseline_count` / `candidate_count` 建議都 >= 20。

## B. alert 集合與 schema 相容比對

### B1. 產生 baseline/candidate state DB

- 在同資料窗條件下分別跑出兩份 `state.db`，例如：
  - `artifacts/task3_phase3/baseline_state.db`
  - `artifacts/task3_phase3/candidate_state.db`

### B2. 執行自動比對

- 執行：
  - `python trainer/scripts/task3_phase3_compare_alerts.py --baseline-db artifacts/task3_phase3/baseline_state.db --candidate-db artifacts/task3_phase3/candidate_state.db --score-tol 1e-6 --margin-tol 1e-6 --out-json artifacts/task3_phase3/alerts_compare.json`

### B3. 驗收建議

- `schema.baseline_only_columns` / `schema.candidate_only_columns` 為空。
- `alerts.baseline_only_bet_ids` / `alerts.candidate_only_bet_ids` 趨近 0（或在已知原因下有紀錄）。
- `numeric_drift.score_diff_rows_over_tolerance`、`numeric_drift.margin_diff_rows_over_tolerance` 在容忍範圍內。

## C. 建議提交物

- `artifacts/task3_phase3/p95_compare.json`
- `artifacts/task3_phase3/alerts_compare.json`
- 將本次結果摘要追加至 `STATUS.md`（檔案、命令、結果、下一步）。
