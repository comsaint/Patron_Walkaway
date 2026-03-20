# Investigation Runbook

## 1) Preflight

在 production 環境執行 `checks/preflight_check.py`，確認：

- prediction log 持續寫入
- `PREDICTION_LOG_DB_PATH` 與 `DATA_DIR` 有效
- `player_profile.parquet`、`canonical_mapping.parquet`、`canonical_mapping.cutoff.json` 可用

## 2) Snapshot Collection

執行 `checks/collect_snapshot.py`，將結果寫入：

- `snapshots/prod_YYYYMMDD_HHMM/env_sanitized.json`
- `snapshots/prod_YYYYMMDD_HHMM/prediction_log_health.json`
- `snapshots/prod_YYYYMMDD_HHMM/data_dir_health.json`
- `snapshots/prod_YYYYMMDD_HHMM/notes.md`

## 3) Root-Cause Analysis

依 `.cursor/plans/INVESTIGATION_PLAN_TEST_VS_PRODUCTION.md` 的 R1–R9 順序，將分析記錄放入 `analysis/` 對應子目錄。

## 4) Reporting

完成後彙整到 `reports/investigation_report_v1.md`，並同步更新 `.cursor/plans/STATUS.md`。
