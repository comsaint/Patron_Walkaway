# Precision Uplift R1PCT Orchestrator Runbook

> 角色：Orchestrator 總操作手冊（`run_pipeline.py`、旗標、產物路徑、排障）。  
> 邊界：本檔不維護工程任務狀態（請看 `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`）。

---

## 1. 現況能力（先看）

### 1.1 可執行
- `--phase phase1` full run
- `--phase phase2` full run（含可選 trainer/backtest 旗標）
- `--phase all --dry-run`

### 1.2 不可執行（截至目前）
- `--phase all` 非 dry-run
- `--phase phase3` full run
- `--phase phase4` full run
- `--mode autonomous`

---

## 2. 基本操作流程

1. 準備 config 與 `run_id`（固定契約，不可中途改）。
2. 先做 `--dry-run`。
3. dry-run `READY` 才做 full run。
4. 失敗先看 `run_state.json` 與 `blocking_reasons`。
5. 可恢復情境用 `--resume`，契約漂移則開新 run。

---

## 3. 指令範本

### 3.1 All-phase readiness（建議每次改 config 都跑）

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase all \
  --dry-run \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_full.yaml \
  --run-id <run_id>
```

判讀：
- `READY`：可進入 `phase1` 或 `phase2` 實跑。
- `NOT_READY`：先修 `blocking_reasons`。

### 3.2 Phase 1 dry-run / full run

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 \
  --dry-run \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml \
  --run-id <run_id>
```

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml \
  --run-id <run_id>
```

### 3.3 Phase 2 dry-run / plan-only /完整證據跑法

Phase 2 dry-run：
```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --dry-run \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id>
```

Phase 2 plan-only（僅規劃與基本檢查）：
```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id>
```

Phase 2 完整科學證據（建議）：
```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id> \
  --phase2-run-trainer-jobs \
  --phase2-run-per-job-backtests \
  --phase2-run-backtest-jobs
```

可選 gate 失敗即非 0：
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

Resume：
```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id <run_id> \
  --resume
```

---

## 4. 產物位置（固定查核）

- 執行狀態：`investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/run_state.json`
- phase2 bundle：`investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/phase2_bundle.json`
- 報表根目錄：`investigations/precision_uplift_recall_1pct/results/<run_id>/reports/`
- phase2 gate：`.../reports/phase2/phase2_gate_decision.md`

---

## 5. Gate 判讀要點

- 不可只看 `PASS/BLOCKED/FAIL`，要同看 `blocking_reasons` 與 `evidence_summary`。
- `plan-only` 正常情況通常是 `BLOCKED`，這不是 bug，是證據不完整。
- 要下決策級結論，需有可比 uplift 與跨窗資訊，不可只靠單次漂亮指標。

---

## 6. 常見錯誤與處置

| 現象 | 常見原因 | 處置 |
| :--- | :--- | :--- |
| `E_CONFIG_INVALID` | 非空 `overrides` 或白名單外 `trainer_params` | 修 config，不要繞過驗證 |
| `E_NO_DATA_WINDOW` | 視窗資料不足或無可解析 PAT 指標 | 調整視窗/資料覆蓋，先 smoke |
| `E_ARTIFACT_MISSING` | 預期 metrics/log 檔不存在 | 先確認前一步是否成功輸出 |
| Gate 長期 `BLOCKED` | 只跑 plan-only 或證據不足 | 跑完整 phase2 三旗標流程 |

---

## 7. 資源保護（必讀）

- 筆電預設 `max_parallel_jobs=1`；先穩再快。
- 先小窗驗證，再放大正式觀測窗。
- 若出現記憶體壓力、頻繁 swap、或跑時異常延長，立即縮小矩陣。
- 發現 OOM 風險時，停止擴大實驗，優先調整資源參數。

---

## 8. 參考文件

- SSOT：`PRECISION_UPLIFT_R1PCT_SSOT.md`
- Implementation Plan：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`
- Execution Plan：`PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md`

---

## 9. 附錄：與程式碼對照用（Phase 2 Gate／錯誤碼）

以下段落標題與關鍵字刻意與 `orchestrator/evaluators.py`、`common_exit_codes.py`、`phase2_exit_codes.py` 對齊，供除錯與契約測試錨定。

### 9.1 Phase 2 Gate 機械檢查（`evaluate_phase2_gate`）

#### 1.8.1 Phase 2 Gate 機械檢查（`evaluate_phase2_gate` / T11A）

1. **雙窗硬 Gate（預設開）**：在 per-job uplift 已滿足 **`gate.min_uplift_pp_vs_baseline`** 且（若適用）std gate 未否決後，若要維持 **`PASS`**，bundle 內 **`phase2_pat_series_by_experiment`** 須存在至少一條 PAT@1% 序列，且**最長序列長度** ≥ **`gate.min_pat_windows_for_pass`**（預設 **2**）。否則狀態為 **BLOCKED**，blocking code **`phase2_insufficient_pat_windows_for_pass`**。
2. **序列從哪來**：full **`run_pipeline.py --phase phase2`** 在寫入 gate 報表前會呼叫 **`collectors.merge_phase2_pat_series_from_shared_and_per_job`**（條件滿足時把共享 PAT 與 per-job 預覽併成兩點序列）。若未觸發 merge 或 YAML 未提供足夠長的手寫序列，仍可能觸發上一項 **BLOCKED**。
3. **關閉雙窗檢查（僅限 smoke／除錯）**：Phase 2 YAML 的 **`gate.min_pat_windows_for_pass: 0`**（或 ≤0）可關閉上述硬 Gate；**不應**複製到宣稱可下產品結論的正式實驗設定。
4. **勝者欄位**：uplift 路徑曾判定「達標」時，**metrics** 可能含 **`phase2_winner_*`**；若最終因雙窗或其他理由變為 **BLOCKED**，勝者欄位仍可能保留以利除錯——**以 `status` 與 `blocking_reasons` 為準**。
5. 判讀 **`phase2/phase2_gate_decision.md`** 與 **`run_state.phase2_gate_decision`** 時請一併閱讀 **`evidence_summary`** 與 **`conclusion_strength`**，**不可**只看 **PASS**／**FAIL**／**BLOCKED** 標籤。

#### 1.8.2 Phase 2 orchestrator 錯誤碼速查（runner／ingest／bundle）

下列字串常見於 **`runner.run_logged_command` 回傳的 `error_code`**、**`run_pipeline.py` Phase 2 步驟的 `error_code`**，或 **`phase2_bundle.json` 的 `errors[].code`**（後者若存在，**`evaluate_phase2_gate`** 會將 bundle 判為 **FAIL** 並把 code 列入 **`blocking_reasons`**）。與 **§9.1** 的 **gate 專用 `blocking_reasons`（如 `phase2_insufficient_pat_windows_for_pass`）** 不同：gate 理由以 **`phase2_gate_decision.md`／`run_state.phase2_gate_decision`** 為準。

**重要**：**`E_NO_DATA_WINDOW`** 在此專案 orchestrator 中主要表示「**資料或指標契約不足以評估該觀測窗／PAT@1%**」（例如 backtest stderr 暗示窗內無注單、或 **ingest 後 JSON 缺少可解析的 `model_default.test_precision_at_recall_0.01`**）。**子程序逾時**另有專用碼，勿混用。

| Code | 典型情境（摘要） |
|------|------------------|
| **`E_SUBPROCESS_TIMEOUT`** | **`run_logged_command`** 觸發 **`subprocess.TimeoutExpired`**（`timeout_sec` 到限；程序過慢或卡住）。 |
| **`E_NO_DATA_WINDOW`** | Backtest 失敗映射（**`classify_backtest_failure`**，如窗內無注單）；或共享／per-job **metrics 可讀但無可解析 PAT@1%**（與逾時無關）。 |
| **`E_ARTIFACT_MISSING`** | 預期檔案缺路徑／無法載入 JSON、per-job 缺 **backtest_metrics**（檔案層級）等。 |
| **`E_PHASE2_BACKTEST_JOBS`** | 共享 **`phase2_backtest_jobs`** 子程序失敗（非 ingest 缺欄位時見步驟訊息）。 |
| **`E_PHASE2_PER_JOB_BACKTEST_JOBS`** | **`phase2_per_job_backtest_jobs`** 整批未全成功；細因見 **`errors[]`** 或各 job 結果列。 |
| **`E_CONFIG_INVALID`** | Phase 2 YAML／bundle 與 **`config_loader.validate_phase2_config`** 不合（如非空 **`overrides`**、未知 **`trainer_params`**）。 |

**程序退出碼**：與上表**字串** `error_code` 不同。**`orchestrator/common_exit_codes.py`**：**2**（**`EXIT_CONFIG_INVALID`**）、**3**（**`EXIT_PREFLIGHT_FAILED`**）、**6**（**`EXIT_DRY_RUN_NOT_READY`**）跨 **`--phase phase1`／`phase2`／`all`**；Phase 1 另含具名常數 **4**＝**`EXIT_PHASE1_MID_OR_R1_FAILED`**（mid／R1 步驟）、**5**＝**`EXIT_PHASE1_BACKTEST_FAILED`**（Phase 1 backtest 步驟）。**`orchestrator/phase2_exit_codes.py`**：**4**＝**`EXIT_RESUME_BUNDLE_LOAD_FAILED`**（resume 無法載入 **`phase2_bundle.json`**）、**5**＝**`EXIT_PHASE2_RUNNER_SMOKE_FAILED`**（典型失敗步驟名 **`phase2_runner_smoke`**）、**7**／**8**／**9**／**10** 等同上表 Phase 2 列。**整數 4／5** 在 Phase 1 與 Phase 2 語意不同；除錯必讀 **`run_state.steps`** 與 stderr。

**觀測**：**`run_state.phase2_collect.phase2_pat_matrix_yaml_experiment_count`** 僅統計 YAML 已宣告 **`precision_at_recall_1pct_by_window`** 的實驗數，與 runner 是否已產出真多窗矩陣無必然相等關係（見 Implementation Plan **W2** 收尾項）。

### 9.2 Phase 2 YAML 範例（T10A：`trainer_params` 白名單）

> 與 **`orchestrator/config/run_phase2.yaml`** 實檔對齊：僅 **`overrides: {}`** + 可選 **`trainer_params`**；勿在範例中復活非空 **`overrides`**。

### 2.3.1 最小 config schema 草稿（`run_phase2.yaml` Track A/B/C）

```yaml
phase: phase2
run_id: "phase2_20260410"

common:
  models_root: out/models
  model_version: "20260408-173809-e472fd0"
  state_db_path: local_state/state.db
  prediction_log_db_path: local_state/prediction_log.db
  window:
    start_ts: "2026-04-09T00:00:00+08:00"
    end_ts: "2026-04-15T00:00:00+08:00"
  contract:
    metric: precision_at_recall_1pct
    timezone: Asia/Hong_Kong
    exclude_censored: true

resources:
  max_windows: 3
  max_trials_per_track: 6
  max_parallel_jobs: 1
  backtest_skip_optuna: true

tracks:
  track_a:
    enabled: true
    experiments:
      - exp_id: a_baseline
        overrides: {}
      - exp_id: a_recent_chunks_v1
        overrides: {}
        trainer_params:
          recent_chunks: 3
  track_b:
    enabled: true
    experiments:
      - exp_id: b_baseline
        overrides: {}
  track_c:
    enabled: true
    experiments:
      - exp_id: c_baseline
        overrides: {}

gate:
  min_uplift_pp_vs_baseline: 3.0
  max_std_pp_across_windows: 2.5
```
