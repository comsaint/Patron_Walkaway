# Phase 2 Orchestrator 操作 Runbook

> **用途**：從 repo 根目錄執行 `--phase phase2`，產出 `phase2_bundle.json`、Gate 報表與各 track 結果。  
> **文件契約**：實作狀態與 DoD 以 `PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md`（Implementation SSoT）為準；跨 Phase 操作與錯誤碼長表見 `PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md`（Operations SSoT，尤其 §1.8）。Phase 2 策略背景見 `phase2/README.md`。

---

## 1. 你會得到什麼

| 產出 | 路徑（相對 repo 根） |
|------|----------------------|
| 執行狀態 | `investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/run_state.json` |
| Bundle（計畫 + runner 回填） | `…/orchestrator/state/<run_id>/phase2_bundle.json` |
| Gate 結論 | `investigations/precision_uplift_recall_1pct/results/<run_id>/reports/phase2/phase2_gate_decision.md` |
| 各軌道報表 | `…/results/<run_id>/reports/phase2/track_{a,b,c}_results.md` |
| Job 日誌 | `…/orchestrator/state/<run_id>/logs/phase2/<track>/<exp_id>/` |

**重要**：預設不帶訓練／回測旗標時，bundle 多為 `plan_only`，Gate 通常為 **BLOCKED**（僅計畫、無 per-job uplift 證據）。要機械化的 **PASS／FAIL／uplift／勝者**，必須跑齊 §4「完整科學結論」中的三個旗標（與 Tasklist §0.2 一致）。

---

## 2. 前置條件

1. **工作目錄**：repo 根目錄（以下命令均假設於此執行）。
2. **設定檔**：複製並編輯 `orchestrator/config/run_phase2.yaml`（或指向你的副本）。
   - **模型 bundle（擇一）**：（1）`common.models_root`（預設語意 `out/models`）+ `common.model_version`（單一路徑段，與 `trainer.core.model_bundle_paths` 版本目錄一致）；或（2）直接寫 `common.model_dir` 指向含 `model.pkl` 的目錄。兩者不可同時設定。
   - `common.state_db_path`、`common.prediction_log_db_path`：SQLite 檔路徑；**preflight 會檢查檔案存在且含必要表**（與 Phase 1 相同契約）。**離線 backtest 的標籤與分數來自 bets／sessions（Parquet 或 ClickHouse），不是依賴 `prediction_log` 內已有 production 列。**
   - `common.window.start_ts` / `end_ts`：須與訓練資料截止對齊（評估窗起點應在訓練最後可見日之後，避免洩漏）；時區與 `contract.timezone` 一致。
3. **資料**：
   - 若實驗的 `trainer_params` 含 `use_local_parquet: true`（或 trainer 預設讀本機）：需有 `data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`（及相關檔）覆蓋視窗與 label 延伸區間。
   - 否則需可連線 **ClickHouse** 並能拉取對應視窗。
4. **能力矩陣**：僅使用 Tasklist **T10B** 標為 `supported` 的 `trainer_params`；`planned`／`blocked` 欄位不得當成已生效策略（見 ADHOC §1.8）。

---

## 3. 建議流程

### 3.1 改完 YAML 後先做 dry-run

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --dry-run \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id>
```

- `READY`：可進行 §4 完整跑或 §3.2 僅計畫。
- `NOT_READY`：依終端與 `run_state.json` 內 `blocking_reasons` 修正路徑／DB／model_dir。

### 3.2 僅計畫／smoke（輕量；Gate 多為 BLOCKED）

不帶 `--phase2-run-trainer-jobs` 等旗標時：會寫 `phase2_bundle.json`、建立 log 目錄、可選 `trainer --help` smoke。適合驗證 config 與目錄權限。

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id>
```

可選：`--skip-phase2-trainer-smoke`（略過冷啟動 `trainer --help`）、`--skip-backtest-smoke`（略過 backtester CLI smoke；見 `run_pipeline.py` help）。

---

## 4. 完整科學結論（訓練 + per-job 回測 + 共享回測）

若要 **uplift／勝者** 等 Gate 邏輯有機會得到 **PASS／FAIL**（而非僅 plan_only BLOCKED），請**同一輪**帶齊：

- `--phase2-run-trainer-jobs`
- `--phase2-run-per-job-backtests`
- `--phase2-run-backtest-jobs`

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id> \
  --phase2-run-trainer-jobs \
  --phase2-run-per-job-backtests \
  --phase2-run-backtest-jobs
```

此流程可能**極耗時**且吃 RAM／磁碟；`resources.max_parallel_jobs` 等請依機器調整。

### 4.1 可選：用 exit code 反映 Gate

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id> \
  --phase2-run-trainer-jobs \
  --phase2-run-per-job-backtests \
  --phase2-run-backtest-jobs \
  --phase2-fail-on-gate-fail \
  --phase2-fail-on-gate-blocked
```

- **注意**：`plan_only` 或證據不足時 Gate 常為 **BLOCKED**；若加 `--phase2-fail-on-gate-blocked`，CI 會失敗。正式實驗判讀以 `phase2_gate_decision.md` 為準。

### 4.2 中斷後續跑

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id> \
  --resume \
  --phase2-run-trainer-jobs \
  --phase2-run-per-job-backtests \
  --phase2-run-backtest-jobs
```

**必須**與首次相同的 `--run-id`，且 `phase2_bundle.json` 可載入。

---

## 5. 判讀 Gate 與雙窗

- **雙窗**：預設 `gate.min_pat_windows_for_pass: 2`（可在 YAML 的 `gate` 區塊設定）。uplift 通過後若 PAT 序列最長長度不足，最終可能 **BLOCKED**（`phase2_insufficient_pat_windows_for_pass`）。merge 行為見 ADHOC §1.8.1。
- **結論強度**：`phase2_gate_decision.md` 與 `run_state.phase2_gate_decision` 內的 `conclusion_strength`（`exploratory` / `comparative` / `decision_grade`）請與 `evidence_summary` 一併閱讀；**不可**只看單一 PASS 標籤。

---

## 6. 常見問題（精簡）

| 現象 | 處理方向 |
|------|----------|
| Gate 永遠 BLOCKED、`plan_only` | 未開 §4 三旗標，或共享回測未成功 ingest（`status` 未變為 `metrics_ingested`）。 |
| `E_CONFIG_INVALID` | 非空 `overrides`、或 `trainer_params` 含白名單外鍵名（T10A）。 |
| `E_NO_DATA_WINDOW` | 視窗內無足夠注單／標籤，或 metrics 無可解析 PAT@1%（見 ADHOC §1.8.2）。 |
| per-job 全 skip | `job_specs` 缺 `training_metrics_repo_relative` 且訓練未成功寫出路徑；先確認 `--phase2-run-trainer-jobs` 與 log 目錄內產物。 |

**退出碼**：Phase 2 專用整數見 `orchestrator/phase2_exit_codes.py`；與 Phase 1 共用碼（如 2=config、3=preflight）勿混用語意。詳表見 ADHOC §1.8.2。

---

## 7. 已知限制（與 Tasklist 對齊）

- 真多窗實驗矩陣、統一結果結構、部分 fail-fast 仍屬 **T10** 收尾項。
- `--phase all` **非 dry-run** 長跑串接尚未實作；多 Phase 請分開執行或僅用 `run_full.yaml` 做 dry-run readiness。

---

## 8. 相關路徑速查

- Orchestrator 入口：`investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`
- 範例設定：`investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml`
- 實作任務清單：`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md`
