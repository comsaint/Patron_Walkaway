# Production Flight Recorder — Runbook

> 操作手冊（Runbook 層）。架構與元件定義見
> [Production Flight Recorder - IMPLEMENTATION_PLAN.md](Production%20Flight%20Recorder%20-%20IMPLEMENTATION_PLAN.md)；
> 任務拆解見 [Production Flight Recorder - WORKING_PLAN.md](Production%20Flight%20Recorder%20-%20WORKING_PLAN.md)。
> Scorer / validator 業務語意仍以
> [Scorer Runtime Contract - SSOT.md](Scorer%20Runtime%20Contract%20-%20SSOT.md) 為準。

本 Runbook 說明如何在 **deploy bundle 機器**上啟用 flight recorder、與既有 `collect_debug_bundle` 並行運作，以及事後打包與離線分析。

## 適用時機

- 新模型上線前後，需證明 production precision 落差來自 **資料晚到、FINAL 語意、feature supplier、或 validator 口徑**，而非僅 aggregate warning。
- 已有 `prod_diag_*.zip`，但缺少 row-level 證據鏈（ClickHouse 當下所見列 → stage → score → validation）。

## 與 debug bundle 的關係

| 產物 | 用途 |
|------|------|
| `collect_debug_bundle` → `prod_diag_*.zip` | 事後快照：SQLite 匯出、audit、identity（輕量） |
| Flight recorder → `local_state/flight_recording/` | 完整證據鏈：每 cycle Parquet、CH query manifest、time-machine diff |

`collect_debug_bundle` 若偵測到 `local_state/flight_recording/MANIFEST.json`，會在 debug zip 內附 **`flight_recording/MANIFEST.json` 指標**（非整包 TB 錄製目錄）。

## 目錄與設定

| 路徑 | 說明 |
|------|------|
| `local_state/flight_recording_config.yaml` | 錄製行為 SSOT（**不用 env 控制**） |
| `local_state/flight_recording/` | 錄製根目錄（預設） |
| `local_state/flight_recording/MANIFEST.json` | artifact 清單與 sha256 |
| `cycles/scorer/cycle_NNNNNN/` | Scorer 每輪證據 |
| `cycles/validator/cycle_NNNNNN/` | Validator 每輪證據 |
| `ch_time_machine/window_NNNNNN/` | CH 重查與 diff |

首次啟用若無 YAML，執行 init 會寫入預設 `flight_recording_config.yaml`。

### 建議設定片段

```yaml
enabled: true
recording_root: local_state/flight_recording
capture_scorer_stages: true
capture_validator_stages: true
capture_ch_diagnostic_requery: true
requery_schedule_minutes: [0, 15, 60, 360, 1440, 4320]
include_non_final_diagnostics: true
include_system_table_probes: true
```

關閉錄製：設 `enabled: false` 或不要傳 `--record-production-flight`。

## 啟用錄製（推薦：deploy 一鍵）

在 **deploy bundle 根目錄**：

```bash
export PYTHONUTF8=1
cd /path/to/deploy_bundle

python main.py --bundle-dir . --mode all \
  --record-production-flight \
  --recording-config local_state/flight_recording_config.yaml
```

效果：

- 初始化 `local_state/flight_recording/`（identity、manifest、可選 SQLite 匯出）
- Attach **scorer + validator** shadow recorders（依 YAML 的 `capture_*` 開關）
- 照常啟動 API / validator thread / scorer foreground

### 替代：僅 init / shadow attach（除錯）

```bash
python -m trainer_hightier.serving.production_flight_recorder \
  --bundle-dir . \
  --mode shadow \
  --config local_state/flight_recording_config.yaml
```

再手動啟動 `main.py`（需同一 process 才保留 attach；**生產環境請用 `--record-production-flight`**）。

## Terminal 2：ClickHouse time machine

與 deploy **並行**的獨立 process，對已登記的 CH window 依排程重查並寫 diff：

```bash
python -m trainer_hightier.serving.ch_time_machine \
  --bundle-dir . \
  --recording-root local_state/flight_recording \
  --config local_state/flight_recording_config.yaml \
  --once
```

長期 incident 調查（建議去掉 `--once`，預設每 300s 掃描 pending capture）：

```bash
python -m trainer_hightier.serving.ch_time_machine \
  --bundle-dir . \
  --recording-root local_state/flight_recording
```

**限制（第一版）**：allowlist `external_input` JOIN 查詢暫不重查（僅記錄 t0 Parquet）；global / validator canonical 路徑可重查。

## 建議錄製時長

| 目的 | 建議 |
|------|------|
| 最短可驗證鏈 | ≥ 1 scorer cycle + 1 validator cycle，且覆蓋 `LABEL_LOOKAHEAD`（約 45–60 分鐘活躍 + 2–4 小時 buffer） |
| Late arrival / CH 穩定性 | time-machine 持續 **24–72 小時** |
| 深度 incident | **2–3 天**（磁碟需自行監控；第一版不抽樣） |

## 打包錄製目錄

```bash
python -m trainer_hightier.serving.pack_flight_recording \
  --recording-root local_state/flight_recording \
  --output local_state/flight_recording_$(date -u +%Y%m%dT%H%M%SZ).zip
```

## 離線分析（分析機 / 調查員工作站）

將 zip 解壓或直接使用目錄，並指定 **與錄製時相同的 `model.pkl` 目錄**（通常在 bundle 的 `models/`）：

```bash
python -m trainer_hightier.serving.replay_recording_bundle \
  --recording-root /path/to/flight_recording \
  --output-dir /path/to/analysis \
  --model-bundle-dir /path/to/deploy_bundle/models \
  --full-analysis
```

產出（在 `--output-dir` 下）：

| 檔案 | 說明 |
|------|------|
| `score_replay_diff_report.json` | stage_08 重算分數 vs production stage_09 |
| `validator_replay_diff_report.json` | 重跑 `validate_alert_row` vs decision trace |
| `high_score_casebook.parquet` / `false_positive_casebook.parquet` | 案例簿 |
| `clickhouse_late_arrival_report.json` | time-machine diff 彙總 |
| `feature_root_cause_rank.json` | feature null reason 排序 |
| `analysis_summary.json` | 總覽 |

僅驗證 manifest 與目錄結構（不跑 replay）：

```bash
python -m trainer_hightier.serving.replay_recording_bundle \
  --recording-root /path/to/flight_recording \
  --output-dir /path/to/analysis
```

## Production dry-run 檢查表

在正式 incident 錄製前，可於 **staging bundle** 跑一輪：

- [ ] `flight_recording_config.yaml` 存在且 `enabled: true`
- [ ] `python main.py ... --record-production-flight` 啟動無錯
- [ ] 至少產生 `cycles/scorer/cycle_000001/`（含 `clickhouse/`、`stages/stage_08_*.parquet`）
- [ ] 至少產生 `cycles/validator/cycle_000001/`（含 `decisions/decision_trace.parquet`）
- [ ] `MANIFEST.json` 中 `recorder_partial` 為 false（或已知失敗步驟可接受）
- [ ] `replay_recording_bundle --full-analysis` 在測試機成功
- [ ] debug zip 內可選看到 `flight_recording/MANIFEST.json` 指標
- [ ] 確認錄製根目錄 **無** password / connection string（僅 redacted query）

## 故障排除

| 現象 | 處置 |
|------|------|
| 無 `cycles/scorer/` | 確認 `--record-production-flight` 與 `capture_scorer_stages: true`；scorer 是否有增量 bet |
| 無 `cycles/validator/` | 確認 `capture_validator_stages: true`；是否有 pending alert 過 freshness |
| `recorder_partial: true` | 查 deploy log 中 `[flight_recorder]` warning；磁碟滿或 Parquet 寫入失敗 |
| Score replay 缺報告 | 需 `--model-bundle-dir` 指向含 `model.pkl` 的目錄 |
| Validator replay 0 compared | 無 decision trace 或 pending 與 trace 的 `bet_id` 對不上 |
| Time-machine 無 diff | window 尚未到期（見 `registered_at_utc` + schedule）；或 allowlist external 路徑 |

## 安全與合規

- 錄製 bundle **不得**含 ClickHouse 帳密；僅 connection alias、redacted SQL、permission probe 結果。
- 第一版 **不抽樣**；請監控 `local_state/flight_recording` 磁碟。
- Recorder I/O 為 **fail-open**：寫入失敗不應停止 scorer，但會標 `recorder_partial`。

## 相關指令速查

```bash
# Init only
python -m trainer_hightier.serving.production_flight_recorder --bundle-dir . --mode init

# Debug bundle（含 flight recording 指標）
python -m trainer_hightier.serving.collect_debug_bundle --bundle-dir .

# 單元測試（開發機）
pytest trainer_hightier/tests/test_flight_recorder_*.py -q
```
