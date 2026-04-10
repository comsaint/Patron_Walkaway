# label_noise_audit

## Run metadata (orchestrator)

- **run_id**: `prod_phase1_20260409`
- **Window**: `2026-04-09T00:00:00+08:00` → `2026-04-15T00:00:00+08:00`
- **model_dir**: `out/models/20260408-173809-e472fd0`
- **state_db_path**: `local_state/state.db`
- **prediction_log_db_path**: `local_state/prediction_log.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/prod_phase1_20260409/collect_bundle.json`


## Unified sample (R1/R6)

```json
{
  "description": "Merged union of stratified below-threshold + stratified alert labeled samples. NOT an i.i.d. or cohort-unbiased estimate of full production PR � use for unified score�label diagnostics and model_version splits only.",
  "n_rows_below_branch": 3106,
  "n_rows_alert_branch": 624,
  "n_duplicate_bet_id_overlap": 7,
  "n_rows_merged_unique": 3695,
  "n_censored_excluded_below": 0,
  "n_censored_excluded_alert": 0,
  "current_threshold_metrics": {
    "tp": 255.0,
    "fp": 361.0,
    "fn": 550.0,
    "precision": 0.413961038961039,
    "recall": 0.3167701863354037
  },
  "precision_at_recall_target": {
    "target_recall": 0.01,
    "precision_at_target_recall": 0.6470588235294118,
    "threshold_at_target": 0.9406361541275423,
    "achieved_recall": 0.02732919254658385
  }
}
```


## By model_version (head)

```json
{
  "20260408-173809-e472fd0": {
    "n_rows": 3695,
    "current_threshold_metrics": {
      "tp": 255.0,
      "fp": 361.0,
      "fn": 550.0,
      "precision": 0.413961038961039,
      "recall": 0.3167701863354037
    },
    "precision_at_recall_target": {
      "target_recall": 0.01,
      "precision_at_target_recall": 0.6470588235294118,
      "threshold_at_target": 0.9406361541275423,
      "achieved_recall": 0.02732919254658385
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
      "n_total": 148043,
      "n_rated": 148043,
      "n_alert_rated": 758,
      "n_below_rated": 147285,
      "candidate_filter": "below_threshold",
      "sample_size_requested": 4000,
      "sample_size_written": 3117,
      "bins": 10,
      "per_bin_target": 400
    },
    "output_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_below_threshold_sample_20260409_191944_798717_1775733584798717200_106e90f7_below_threshold.csv",
    "note": "Run offline labeling on output_csv bet_id, then use evaluate mode with labeled CSV. Use candidate_filter='alert' to diagnose precision drop; use candidate_filter='below_threshold' to diagnose missed positives."
  },
  "autolabel": {
    "mode": "autolabel",
    "window": {
      "start_ts": "2026-04-09T00:00:00+08:00",
      "end_ts": "2026-04-15T00:00:00+08:00"
    },
    "db_path": "C:\\Projects\\Patron_Walkaway\\local_state\\prediction_log.db",
    "sample_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_below_threshold_sample_20260409_191944_798717_1775733584798717200_106e90f7_below_threshold.csv",
    "output_labels_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_labeled_20260409_191944_798717_1775733584798717200_106e90f7_below_threshold.csv",
    "summary": {
      "n_sample_input": 3116,
      "n_sample_rows_input": 3117,
      "n_unique_bet_id": 3116,
      "n_duplicate_bet_id": 1,
      "n_players": 1354,
      "n_bets_fetched": 312236,
      "n_labeled_rows": 3078,
      "n_censored": 0,
      "n_unmatched_sample_bet_id": 38,
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
    "labels_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_labeled_20260409_191944_798717_1775733584798717200_106e90f7_below_threshold.csv",
    "n_labeled_input": 3078,
    "n_labeled_matched": 3106,
    "n_censored_excluded": 0,
    "current_threshold_metrics": {
      "tp": 0.0,
      "fp": 0.0,
      "fn": 556.0,
      "precision": 0.0,
      "recall": 0.0
    },
    "precision_at_recall_target": {
      "target_recall": 0.01,
      "precision_at_target_recall": 0.34177215189873417,
      "threshold_at_target": 0.8458334714864263,
      "achieved_recall": 0.048561151079136694
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
          "n_total": 148043,
          "n_rated": 148043,
          "n_alert_rated": 758,
          "n_below_rated": 147285,
          "candidate_filter": "below_threshold",
          "sample_size_requested": 4000,
          "sample_size_written": 3117,
          "bins": 10,
          "per_bin_target": 400
        },
        "output_csv": "C:\\Projects\\Patron_Walkaway\\investigations\\test_vs_production\\snapshots\\latest_r1_r6_below_threshold_sample_20260409_191944_798717_1775733584798717200_106e90f7_below_threshold.csv",
        "note": "Run offline labeling on outp
... (truncated for file size)
```
