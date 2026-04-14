# label_noise_audit

## Run metadata (orchestrator)

- **run_id**: `prod_phase1_20260414`
- **Window**: `2026-04-09T00:00:00+08:00` → `2026-04-15T00:00:00+08:00`
- **model_dir**: `out/models/20260408-173809-e472fd0/`
- **state_db_path**: `local_state/state.db`
- **prediction_log_db_path**: `local_state/prediction_log.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/prod_phase1_20260414/collect_bundle.json`


## Unified sample (R1/R6)

```json
{
  "description": "Merged union of stratified below-threshold + stratified alert labeled samples. NOT an i.i.d. or cohort-unbiased estimate of full production PR � use for unified score�label diagnostics and model_version splits only.",
  "n_rows_below_branch": 3163,
  "n_rows_alert_branch": 780,
  "n_duplicate_bet_id_overlap": 15,
  "n_rows_merged_unique": 3901,
  "n_censored_excluded_below": 0,
  "n_censored_excluded_alert": 0,
  "current_threshold_metrics": {
    "tp": 304.0,
    "fp": 459.0,
    "fn": 494.0,
    "precision": 0.3984272608125819,
    "recall": 0.38095238095238093
  },
  "precision_at_recall_target": {
    "target_recall": 0.01,
    "precision_at_target_recall": 0.5909090909090909,
    "threshold_at_target": 0.9493577551777754,
    "achieved_recall": 0.016290726817042606
  }
}
```


## By model_version (head)

```json
{
  "20260408-173809-e472fd0": {
    "n_rows": 3901,
    "current_threshold_metrics": {
      "tp": 304.0,
      "fp": 459.0,
      "fn": 494.0,
      "precision": 0.3984272608125819,
      "recall": 0.38095238095238093
    },
    "precision_at_recall_target": {
      "target_recall": 0.01,
      "precision_at_target_recall": 0.5909090909090909,
      "threshold_at_target": 0.9493577551777754,
      "achieved_recall": 0.016290726817042606
    }
  }
}
```


## Full R1 final payload (reference)

```json
{
  "mode": "all",
  "sample": {
    "mode": "sample",
    "window": {
      "start_ts": "2026-04-09T00:00:00+08:00",
      "end_ts": "2026-04-15T00:00:00+08:00"
    },
    "db_path": "C:\\Projects\\Patron_Walkaway\\local_state\\prediction_log.db",
    "summary": {
      "n_total": 2139898,
      "n_rated": 2139898,
      "n_alert_rated": 8673,
      "n_below_rated": 2131225,
      "candidate_filter": "below_threshold",
      "sample_size_requested": 4000,
      "sample_size_written": 3200,
      "bins": 10,
      "per_bin_target": 400
    },
    "output_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_below_threshold_sample_20260409_165603_892821_1776156963892821800_0034e3df_below_threshold.csv",
    "note": "Run offline labeling on output_csv bet_id, then use evaluate mode with labeled CSV. Use candidate_filter='alert' to diagnose precision drop; use candidate_filter='below_threshold' to diagnose missed positives."
  },
  "autolabel": {
    "mode": "autolabel",
    "window": {
      "start_ts": "2026-04-09T00:00:00+08:00",
      "end_ts": "2026-04-15T00:00:00+08:00"
    },
    "db_path": "C:\\Projects\\Patron_Walkaway\\local_state\\prediction_log.db",
    "sample_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_below_threshold_sample_20260409_165603_892821_1776156963892821800_0034e3df_below_threshold.csv",
    "output_labels_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_labeled_20260409_165603_892821_1776156963892821800_0034e3df_below_threshold.csv",
    "summary": {
      "n_sample_input": 3200,
      "n_sample_rows_input": 3200,
      "n_unique_bet_id": 3200,
      "n_duplicate_bet_id": 0,
      "n_players": 2090,
      "n_bets_fetched": 1872858,
      "n_labeled_rows": 3136,
      "n_censored": 0,
      "n_unmatched_sample_bet_id": 64,
      "player_chunk_size": 200
    },
    "note": "Labels generated via trainer.labels.compute_labels(). Rows with censored=1 should be excluded from strict evaluation."
  },
  "evaluate": {
    "mode": "evaluate",
    "window": {
      "start_ts": "2026-04-09T00:00:00+08:00",
      "end_ts": "2026-04-15T00:00:00+08:00"
    },
    "db_path": "C:\\Projects\\Patron_Walkaway\\local_state\\prediction_log.db",
    "labels_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_labeled_20260409_165603_892821_1776156963892821800_0034e3df_below_threshold.csv",
    "n_labeled_input": 3136,
    "n_labeled_matched": 3163,
    "n_censored_excluded": 0,
    "current_threshold_metrics": {
      "tp": 0.0,
      "fp": 0.0,
      "fn": 496.0,
      "precision": 0.0,
      "recall": 0.0
    },
    "precision_at_recall_target": {
      "target_recall": 0.01,
      "precision_at_target_recall": 0.5,
      "threshold_at_target": 0.8589012134995807,
      "achieved_recall": 0.010080645161290322
    },
    "note": "Compare current_threshold_metrics and precision_at_recall_target against offline test metrics with same definition."
  },
  "branches": {
    "below_threshold": {
      "sample": {
        "mode": "sample",
        "window": {
          "start_ts": "2026-04-09T00:00:00+08:00",
          "end_ts": "2026-04-15T00:00:00+08:00"
        },
        "db_path": "C:\\Projects\\Patron_Walkaway\\local_state\\prediction_log.db",
        "summary": {
          "n_total": 2139898,
          "n_rated": 2139898,
          "n_alert_rated": 8673,
          "n_below_rated": 2131225,
          "candidate_filter": "below_threshold",
          "sample_size_requested": 4000,
          "sample_size_written": 3200,
          "bins": 10,
          "per_bin_target": 400
        },
        "output_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_below_threshold_sample_20260409_165603_892821_1776156963892821800_0034e3df_below_threshold.csv",
        "note": "Run offline labeling on output_csv 
... (truncated for file size)
```
