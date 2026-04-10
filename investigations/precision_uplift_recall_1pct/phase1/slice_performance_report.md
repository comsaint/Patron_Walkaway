# slice_performance_report

## Run metadata (orchestrator)

- **run_id**: `prod_phase1_20260409`
- **Window**: `2026-04-09T00:00:00+08:00` → `2026-04-15T00:00:00+08:00`
- **model_dir**: `out/models/20260408-173809-e472fd0`
- **state_db_path**: `local_state/state.db`
- **prediction_log_db_path**: `local_state/prediction_log.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/prod_phase1_20260409/collect_bundle.json`


## validation_results aggregates (state DB)

```json
{
  "state_db_path": "C:\\Projects\\Patron_Walkaway\\local_state\\state.db",
  "window_start_ts": "2026-04-09T00:00:00+08:00",
  "window_end_ts": "2026-04-15T00:00:00+08:00",
  "validation_results_rows_in_window": 674,
  "finalized_alerts_count": 674,
  "finalized_true_positives_count": 235,
  "note": null
}
```


## R2 prediction_log vs alerts

```json
{
  "status": "ok",
  "state_db_path": "C:\\Projects\\Patron_Walkaway\\local_state\\state.db",
  "n_prediction_log_is_alert_rows": 763,
  "n_alerts_table_rows_ts_window": 760,
  "difference_pl_minus_alerts": 3,
  "alerts_to_prediction_log_ratio": 0.9960681520314548,
  "note": "Compares counts in the same [start_ts, end_ts) string window on scored_at vs alerts.ts. Mismatch may reflect duplicate suppression (R2) or timestamp semantics differences."
}
```


## Collector errors (if any)

*None.*