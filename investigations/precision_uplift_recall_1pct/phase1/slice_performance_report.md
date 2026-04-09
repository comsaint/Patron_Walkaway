# slice_performance_report

## Run metadata (orchestrator)

- **run_id**: `pytest_resume_skip`
- **Window**: `2026-01-01T00:00:00+08:00` → `2026-01-08T00:00:00+08:00`
- **model_dir**: `C:\Users\longp\AppData\Local\Temp\pytest-of-longp\pytest-139\test_resume_skips_preflight_wh0\missing_models`
- **state_db_path**: `s.db`
- **prediction_log_db_path**: `p.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/pytest_resume_skip/collect_bundle.json`


## validation_results aggregates (state DB)

```json
{
  "state_db_path": "C:\\Users\\longp\\Patron_Walkaway\\s.db",
  "window_start_ts": "2026-01-01T00:00:00+08:00",
  "window_end_ts": "2026-01-08T00:00:00+08:00",
  "validation_results_rows_in_window": null,
  "finalized_alerts_count": null,
  "finalized_true_positives_count": null,
  "note": null
}
```


## R2 prediction_log vs alerts

*No `r2_prediction_log_vs_alerts` in R1 final payload.*

## Collector errors (if any)

```json
[
  {
    "code": "E_COLLECT_BACKTEST_METRICS",
    "message": "file not found: C:\\Users\\longp\\Patron_Walkaway\\trainer\\out_backtest\\backtest_metrics.json",
    "path": "C:\\Users\\longp\\Patron_Walkaway\\trainer\\out_backtest\\backtest_metrics.json"
  },
  {
    "code": "E_COLLECT_R1_PAYLOAD",
    "message": "r1_r6 log not found: C:\\Users\\longp\\Patron_Walkaway\\investigations\\precision_uplift_recall_1pct\\orchestrator\\state\\pytest_resume_skip\\logs\\r1_r6.stdout.log",
    "path": "C:\\Users\\longp\\Patron_Walkaway\\investigations\\precision_uplift_recall_1pct\\orchestrator\\state\\pytest_resume_skip\\logs\\r1_r6.stdout.log"
  },
  {
    "code": "E_COLLECT_STATE_DB",
    "message": "state DB path is not a file",
    "path": "C:\\Users\\longp\\Patron_Walkaway\\s.db"
  }
]
```
