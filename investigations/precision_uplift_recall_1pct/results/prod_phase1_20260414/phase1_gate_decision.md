# phase1_gate_decision

## Run metadata (orchestrator)

- **run_id**: `prod_phase1_20260414`
- **Window**: `2026-04-09T00:00:00+08:00` → `2026-04-15T00:00:00+08:00`
- **model_dir**: `out/models/20260408-173809-e472fd0/`
- **state_db_path**: `local_state/state.db`
- **prediction_log_db_path**: `local_state/prediction_log.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/prod_phase1_20260414/collect_bundle.json`


## Gate 結論 (orchestrator)

- **status**: `PRELIMINARY`

### blocking_reasons

- `missing_mid_r1_snapshot_for_direction_check`

### evidence_summary

window_h=144.00; finalized_alerts=8582; finalized_tp=3286; pat@r_final=0.5909

### metrics

```json
{
  "window_hours": 144.0,
  "finalized_alerts_count": 8582,
  "finalized_true_positives_count": 3286,
  "precision_at_target_recall_final": 0.5909090909090909,
  "precision_at_target_recall_mid": null,
  "has_backtest_metrics": true
}
```


## 人工維護區（下方可續寫）

- 與 `slice_performance_report.md`、`label_noise_audit.md` 等交叉比對後補主因與行動項。
