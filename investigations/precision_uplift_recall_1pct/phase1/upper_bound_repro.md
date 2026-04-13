# upper_bound_repro

## Run metadata (orchestrator)

- **run_id**: `pytest_resume_skip`
- **Window**: `2026-01-01T00:00:00+08:00` → `2026-01-08T00:00:00+08:00`
- **model_dir**: `C:\Users\longp\AppData\Local\Temp\pytest-of-longp\pytest-86\test_resume_skips_preflight_wh0\missing_models`
- **state_db_path**: `s.db`
- **prediction_log_db_path**: `p.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/pytest_resume_skip/collect_bundle.json`


## Offline / backtest snapshot

```json
{
  "window_start": "2026-02-06T00:00:00",
  "window_end": "2026-02-13T00:00:00",
  "window_hours": 168.0,
  "observations": 1,
  "rated_obs": 1,
  "unrated_obs": 0,
  "track_llm_degraded": false,
  "model_default": {
    "test_ap": 0.0,
    "test_precision": 0.0,
    "test_recall": 0.0,
    "test_f1": 0.0,
    "test_fbeta_05": 0.0,
    "threshold": 0.5,
    "test_samples": 1,
    "test_positives": 0,
    "test_random_ap": 0.0,
    "alerts": 1,
    "alerts_per_hour": 0.005952380952380952,
    "test_precision_at_recall_0.001": null,
    "threshold_at_recall_0.001": null,
    "alerts_per_minute_at_recall_0.001": null,
    "test_precision_at_recall_0.01": null,
    "threshold_at_recall_0.01": null,
    "alerts_per_minute_at_recall_0.01": null,
    "test_precision_at_recall_0.1": null,
    "threshold_at_recall_0.1": null,
    "alerts_per_minute_at_recall_0.1": null,
    "test_precision_at_recall_0.5": null,
    "threshold_at_recall_0.5": null,
    "alerts_per_minute_at_recall_0.5": null,
    "rated_threshold": 0.5
  }
}
```


## Training artifact baseline (from R1 payload)

*No `training_artifact_baseline` in R1 final payload.*
