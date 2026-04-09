# phase1_gate_decision

## Run metadata (orchestrator)

- **run_id**: `pytest_resume_skip`
- **Window**: `2026-01-01T00:00:00+08:00` → `2026-01-08T00:00:00+08:00`
- **model_dir**: `C:\Users\longp\AppData\Local\Temp\pytest-of-longp\pytest-139\test_resume_skips_preflight_wh0\missing_models`
- **state_db_path**: `s.db`
- **prediction_log_db_path**: `p.db`
- **collect_bundle**: `investigations/precision_uplift_recall_1pct/orchestrator/state/pytest_resume_skip/collect_bundle.json`


## Gate 結論 (orchestrator)

- **status**: `FAIL`

### blocking_reasons

- `collect_error:E_COLLECT_BACKTEST_METRICS`
- `collect_error:E_COLLECT_R1_PAYLOAD`
- `collect_error:E_COLLECT_STATE_DB`

### evidence_summary

window_h=168.00; finalized_alerts=0; finalized_tp=0

### metrics

```json
{
  "window_hours": 168.0,
  "finalized_alerts_count": 0,
  "finalized_true_positives_count": 0,
  "precision_at_target_recall_final": null,
  "precision_at_target_recall_mid": null,
  "has_backtest_metrics": false
}
```


## 人工維護區（下方可續寫）

- 與 `slice_performance_report.md`、`label_noise_audit.md` 等交叉比對後補主因與行動項。
