# upper_bound_repro

## Run metadata (orchestrator)

- **run_id**: `prod_phase1_20260414`
- **Window**: `2026-04-09T00:00:00+08:00` → `2026-04-15T00:00:00+08:00`
- **model_dir**: `out/models/20260408-173809-e472fd0/`
- **state_db_path**: `local_state/state.db`
- **prediction_log_db_path**: `local_state/prediction_log.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/prod_phase1_20260414/collect_bundle.json`


## Offline / backtest snapshot

```json
{
  "window_start": "2026-03-12T03:07:11.851087",
  "window_end": "2026-03-12T09:07:11.851087",
  "window_hours": 6.0,
  "observations": 133326,
  "rated_obs": 107871,
  "unrated_obs": 25455,
  "track_llm_degraded": false,
  "model_default": {
    "test_ap": 0.2223823197784581,
    "test_precision": 0.4419642857142857,
    "test_recall": 0.013111714455996291,
    "test_f1": 0.025467875747636502,
    "test_fbeta_05": 0.05860415556739478,
    "threshold": 0.95550174620155,
    "test_samples": 107871,
    "test_positives": 15101,
    "test_random_ap": 0.13999128588777335,
    "alerts": 448,
    "alerts_per_hour": 74.66666666666667,
    "test_precision_at_recall_0.001": 0.68,
    "threshold_at_recall_0.001": 0.9835371983647799,
    "alerts_per_minute_at_recall_0.001": 0.06944444444444445,
    "test_precision_at_recall_0.01": 0.453781512605042,
    "threshold_at_recall_0.01": 0.9597615826727451,
    "alerts_per_minute_at_recall_0.01": 0.9916666666666667,
    "test_precision_at_recall_0.1": 0.310253807106599,
    "threshold_at_recall_0.1": 0.8313554287540448,
    "alerts_per_minute_at_recall_0.1": 13.680555555555555,
    "test_precision_at_recall_0.5": 0.20277091612071743,
    "threshold_at_recall_0.5": 0.6132905296386935,
    "alerts_per_minute_at_recall_0.5": 103.45555555555555,
    "rated_threshold": 0.95550174620155
  },
  "optuna": {
    "test_ap": 0.2223823197784581,
    "test_precision": 0.4537313432835821,
    "test_recall": 0.01006555857227998,
    "test_f1": 0.01969422130085514,
    "test_fbeta_05": 0.046225898667964234,
    "threshold": 0.9608505494488366,
    "test_samples": 107871,
    "test_positives": 15101,
    "test_random_ap": 0.13999128588777335,
    "alerts": 335,
    "alerts_per_hour": 55.833333333333336,
    "test_precision_at_recall_0.001": 0.68,
    "threshold_at_recall_0.001": 0.9835371983647799,
    "alerts_per_minute_at_recall_0.001": 0.06944444444444445,
    "test_precision_at_recall_0.01": 0.453781512605042,
    "threshold_at_recall_0.01": 0.9597615826727451,
    "alerts_per_minute_at_recall_0.01": 0.9916666666666667,
    "test_precision_at_recall_0.1": 0.310253807106599,
    "threshold_at_recall_0.1": 0.8313554287540448,
    "alerts_per_minute_at_recall_0.1": 13.680555555555555,
    "test_precision_at_recall_0.5": 0.20277091612071743,
    "threshold_at_recall_0.5": 0.6132905296386935,
    "alerts_per_minute_at_recall_0.5": 103.45555555555555,
    "rated_threshold": 0.9608505494488366
  }
}
```


## Training artifact baseline (from R1 payload)

```json
{
  "status": "ok",
  "path": "C:\\Projects\\Patron_Walkaway\\out\\models\\20260408-173809-e472fd0\\training_metrics.json",
  "model_version": "20260408-173809-e472fd0",
  "test_precision_at_recall_0.01": 0.7443921852387844,
  "threshold_at_recall_0.01": 0.8567793547038658,
  "test_threshold_uncalibrated": false,
  "uncalibrated_threshold": {
    "rated": false
  },
  "rated_threshold": 0.8613259987192541
}
```

