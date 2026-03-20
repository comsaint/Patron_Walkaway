# Investigation Runbook

## 0) DB Path Consolidation (One-time)

目標：將 runtime DB 路徑統一到同一個 `local_state/` 目錄，避免 `state.db` 與 `prediction_log.db` 分散。

- `STATE_DB_PATH=<runtime_root>/local_state/state.db`
- `PREDICTION_LOG_DB_PATH=<runtime_root>/local_state/prediction_log.db`

### bash (Linux/macOS/Git Bash)

```bash
# 1) 停服務（依你的部署方式）
# systemctl stop <your_service>  或手動停止 scorer/validator/api/export

# 2) 設定 .env（示例）
echo 'STATE_DB_PATH=/opt/patron/local_state/state.db' >> credential/.env
echo 'PREDICTION_LOG_DB_PATH=/opt/patron/local_state/prediction_log.db' >> credential/.env

# 3) 確認目錄
mkdir -p /opt/patron/local_state

# 4) （可選）搬移舊 DB 到新路徑
# cp <old_state_db> /opt/patron/local_state/state.db
# cp <old_prediction_db> /opt/patron/local_state/prediction_log.db

# 5) 啟動服務（先 scorer，再 validator/api/export）
# systemctl start <your_service>
```

### CMD (Windows)

```bat
REM 1) 停服務（依你的部署方式）
REM net stop <your_service>  或手動停止 scorer/validator/api/export

REM 2) 設定 .env（手動編輯 credential\.env 加入以下兩行）
REM STATE_DB_PATH=C:\patron\local_state\state.db
REM PREDICTION_LOG_DB_PATH=C:\patron\local_state\prediction_log.db

REM 3) 確認目錄
mkdir C:\patron\local_state

REM 4) （可選）搬移舊 DB 到新路徑
REM copy <old_state_db> C:\patron\local_state\state.db
REM copy <old_prediction_db> C:\patron\local_state\prediction_log.db

REM 5) 啟動服務（先 scorer，再 validator/api/export）
REM net start <your_service>
```

### 驗收（兩平台一致）

```bash
python investigations/test_vs_production/checks/preflight_check.py --pretty
python investigations/test_vs_production/checks/investigate_r2_window.py --pretty
```

驗收重點：

- `preflight_check.py` 顯示 `prediction_log` freshness 正常
- `investigate_r2_window.py` 的 `resolution` 顯示 `pred_db_path` / `state_db_path` 同屬目標 `local_state/`

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
