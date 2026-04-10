# point_in_time_parity_check

## Run metadata (orchestrator)

- **run_id**: `pytest_resume_skip`
- **Window**: `2026-01-01T00:00:00+08:00` → `2026-01-08T00:00:00+08:00`
- **model_dir**: `C:\Users\longp\AppData\Local\Temp\pytest-of-longp\pytest-191\test_resume_skips_preflight_wh0\missing_models`
- **state_db_path**: `s.db`
- **prediction_log_db_path**: `p.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/pytest_resume_skip/collect_bundle.json`


## MVP 範圍（scaffold）

本段由 orchestrator 產生；請人工核對：

- Scorer `scored_at` 與 bet 延遲 / 桌台政策
- Validator `validated_at` 與標籤成熟 / censored 規則
- 與 R1/R6 觀測窗同一時區契約（runbook：HKT）

## 資料來源路徑（供 reviewer）

- Prediction log DB: `p.db`
- State DB: `s.db`
