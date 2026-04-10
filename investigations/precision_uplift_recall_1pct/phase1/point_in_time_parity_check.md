# point_in_time_parity_check

## Run metadata (orchestrator)

- **run_id**: `prod_phase1_20260409`
- **Window**: `2026-04-09T00:00:00+08:00` → `2026-04-15T00:00:00+08:00`
- **model_dir**: `out/models/20260408-173809-e472fd0`
- **state_db_path**: `local_state/state.db`
- **prediction_log_db_path**: `local_state/prediction_log.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/prod_phase1_20260409/collect_bundle.json`


## MVP 範圍（scaffold）

本段由 orchestrator 產生；請人工核對：

- Scorer `scored_at` 與 bet 延遲 / 桌台政策
- Validator `validated_at` 與標籤成熟 / censored 規則
- 與 R1/R6 觀測窗同一時區契約（runbook：HKT）

## 資料來源路徑（供 reviewer）

- Prediction log DB: `local_state/prediction_log.db`
- State DB: `local_state/state.db`
