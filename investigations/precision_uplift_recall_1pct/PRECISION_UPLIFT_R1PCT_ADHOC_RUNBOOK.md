# 一次性 Ad-hoc 執行方案與腳本實作計畫（Autonomous-first）

> 適用情境：已有訓練完成模型，但尚未開始 backtest / scorer / validator 蒐證。  
> 目標：以**單次調查 run**收齊證據，並落地腳本化執行。  
> 執行原則：**長期目標**為 Autonomous 單一命令閉環；**目前 orchestrator 可執行**者為 `--phase phase1` / `phase2` 的 full run 與 `--phase all --dry-run`（見 §1.2a）。其餘 Autonomous 步驟為 **Tasklist 待辦**（T8A–T8D、T16–T17），Ad-hoc／手動為現況補位。

## 0) 文件契約與同步規則（避免 singleton plans）

- 本文件是 **Operations SSoT**（怎麼跑、怎麼檢查、失敗怎麼處理）。
- `PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` 是 **Implementation SSoT**（功能是否已實作、限制、DoD）。
- 若兩文件敘述衝突，以 Tasklist 為準；本文件不得把 Tasklist 中 `planned` / `blocked` 的功能寫成可直接執行。
- Phase 2 的策略欄位（如 `hard_negative_weight`）是否可用，必須先對照 Tasklist 的 **T10A/T10B/T11A**。
- **`.cursor/plans/STATUS.md`**：整 repo 狀態與歷史輪次（trainer／deploy 等）；**不含**本調查 orchestrator 的權威細項。**本調查執行現況**以 Tasklist **§0.2 快照**與 `orchestrator/run_pipeline.py` 為準。

---

## 1) 執行方案（原 EXECUTION_PLAN §8；分「現況可跑」與「目標 Autonomous」）

### 1.1 Run 定義（先固定，不可中途漂移）

請先建立一個 run 識別（例如 `phase1_adhoc_YYYYMMDD`），並固定：

- `model_version` / `model_dir`
- `STATE_DB_PATH`
- `PREDICTION_LOG_DB_PATH`
- 調查觀測窗（`start_ts`, `end_ts`, 時區統一 HKT）
- 主要契約：`precision@recall=1%`、censored 排除規則、validator 口徑

> 原則：run 期間不更換模型、不改 threshold 策略、不改標籤契約；避免結論不可比較。

### 1.2a 目前可執行（orchestrator 現況；與 Tasklist §0.2 一致）

1. **All-phase dry-run（建議每次改 config 後執行）**
   - `run_pipeline.py --phase all --dry-run --config .../run_full.yaml --run-id <id>`
   - **注意**：CLI **無** `--mode`；`--phase all` **必須**帶 `--dry-run`，否則 exit **2**（長跑串接未實作）。
   - 僅 readiness，不產正式調查結論。
2. **Phase 1 full run**
   - `run_pipeline.py --phase phase1 --config .../run_phase1.yaml --run-id <id>`（可加 `--dry-run` / `--resume` / `--collect-only`）
3. **Phase 2 full run**
   - `run_pipeline.py --phase phase2 --config .../run_phase2.yaml --run-id <id>`（可加 `--dry-run`、`--resume`、**`--phase2-run-trainer-jobs`**、**`--phase2-run-per-job-backtests`**、**`--phase2-run-backtest-jobs`**、`--skip-backtest-smoke`、`--skip-phase2-trainer-smoke` 等）
   - **逐步操作 Runbook（專文）**：[`PRECISION_UPLIFT_R1PCT_PHASE2_RUNBOOK.md`](PRECISION_UPLIFT_R1PCT_PHASE2_RUNBOOK.md)（產出路徑、完整結論所需旗標、資料與 preflight 說明）。
   - **跨窗**：單次 run 仍以 `common.window` 為主；多窗可比需 **多次 run** 或 YAML **`precision_at_recall_1pct_by_window`**（見 Tasklist T10／T11），全自動多窗矩陣仍待 T10 收尾。

### 1.2b 目標流程（Autonomous 單一命令；對應 Tasklist T8A–T8D、T16–T17，**尚未實作**）

以下為**規格願景**，勿當成現有 CLI 可直接跑通：

1. Dry-run 後啟動 **單一 autonomous 命令**（orchestrator 代管長跑）。
2. 自動 preflight、觀測、checkpoint（mid/final R1/R6）、終點 backtest、彙整工件與 Gate；中斷可 `--resume`。

> 實作完成後，應回寫本節與 Tasklist §0.2，並補上**實際**旗標與 exit code。

### 1.3 輸出對應（Phase 1 工件 -> 證據來源）

- `phase1/status_history_crosscheck.md`
  - 來源：調查用 **`STATUS.md`（或同等歷史對照文件）** + 本輪人工判定（沿用/重驗/已失效）。**勿與** `.cursor/plans/STATUS.md`（全 repo 技術狀態日誌）混淆；兩者用途不同。
- `phase1/slice_performance_report.md`
  - 來源：`prediction_log` + `alerts` + `validation_results` 切片統計
- `phase1/label_noise_audit.md`
  - 來源：`run_r1_r6_analysis.py` payload（`n_censored_excluded`、`precision_at_recall_target`）+ 高分 FP 抽樣
- `phase1/point_in_time_parity_check.md`
  - 來源：scorer/validator 時戳與標籤成熟規則對照
  - **現況說明（MVP）**：orchestrator 目前預設輸出 scaffold + 人工核對清單；不會自動填入 parity 指標
  - **判讀規則**：若只有 checklist、無量化指標，僅可視為「待人工審核」，不得視為 parity pass
  - **開發目標（下一步）**：新增 `PIT parity metrics (auto)` 區塊，至少含 `scored_at_in_window_ratio`、`validated_at_non_null_ratio`、`alerts_vs_prediction_log_gap`、`status`、`reasons`

**PIT parity mode（建議先落地）**
- `WARN_ONLY`（預設）：parity 異常不阻斷 gate，但 `phase1_gate_decision.md` 必須標註 `pit_status=warn/fail`
- `STRICT`：parity 異常直接阻斷，Phase 1 Gate 不得 `PASS`
- `phase1/upper_bound_repro.md`
  - 來源：`trainer/out_backtest/backtest_metrics.json` + baseline 指標
- `phase1/phase1_gate_decision.md`
  - 來源：以上五份工件彙總與主因排序

### 1.4 運行時長建議（請明確採納）

1. **最短可做初判：48 小時**
  - 用途：只能做方向性判斷，不可做最終 Gate。
  - 最低資料量建議：
    - finalized alerts >= 300
    - finalized true positives >= 30
2. **建議用於 Phase 1 Gate：72~120 小時（3~5 天）**
  - 用途：可做較可靠主因排序與是否重排判斷。
  - 建議資料量：
    - finalized alerts >= 800（理想 >= 1000）
    - 主要切片各有足夠樣本（避免切片結論只由噪音驅動）
3. **若要跨週期穩定性結論：>= 7 天**
  - 用途：納入工作日/週末行為差異，降低單窗偏誤。

> 評語：若僅跑 6~12 小時就判定「模型不行/資料不行」，風險非常高；在 recall=1% 稀疏場景，這通常會導致錯誤決策。

### 1.5 停止條件與延長條件（autonomous 規則）

- **可停止並進 Gate**（全部滿足）：
  - 達到 1.4 的「建議用於 Gate」時長與資料量
  - `run_r1_r6_analysis` 兩次結果方向一致（非劇烈反轉）
  - censored / delayed label 指標波動進入可解釋範圍
- **必須延長觀測**（任一成立）：
  - finalized alerts 不足（< 300 初判門檻）
  - 切片樣本嚴重不均、top-band 幾乎無可用標記
  - scorer/validator 中途改參數或中斷，造成 run 契約破壞

> Autonomous 模式建議：上述條件由 evaluator 自動判斷並寫入 `run_state.json`，避免人工憑印象提前停止。

### 1.6 資源與效能保護（筆電/有限資源必做）

- `run_r1_r6_analysis.py` 先用較保守參數（例如 `sample_size` 從小到大）。
- `autolabel` 的 player chunk 不要一次拉太大，避免 ClickHouse 壓力尖峰。
- backtest 日常蒐證優先 `--skip-optuna`，將重型搜索留到補充實驗。
- 每次 ad-hoc 命令要保留輸出 payload（JSON）與 run_id，避免不可追溯。

### 1.7 Dry-run 指令與判讀（all-phase readiness）

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase all \
  --dry-run \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_full.yaml \
  --run-id <run_id>
```

（可選）略過 backtest CLI smoke：`--skip-backtest-smoke`

- `READY`：靜態／preflight／（可選）smoke 通過；可繼續跑 **`phase1` / `phase2` full run**（見 §1.2a）。**不代表** `--phase all` 非 dry-run 已可執行。
- `NOT_READY`：先依 `blocking_reasons` 修復。
- 建議：每次變更 config、model_dir、window 或 DB 路徑後都重跑 dry-run。

### 1.8 Phase 2 科學可判讀前置清單（必做）

> 目的：避免「流程有跑」但無法回答哪條策略最有希望。

- [ ] **策略參數已生效**：每個候選實驗可產生 `resolved_trainer_argv`（對照 Tasklist `T10A`）。
- [ ] **能力矩陣已確認**：實驗使用欄位在 Tasklist `T10B` 為 `supported`，非 `planned`/`blocked`。**禁止**將 `T10B` 表中標為 `planned` 或 `blocked` 的欄位（例如 `hard_negative_weight`、`gating_strategy`、`objective_variant` 在未支援前）寫進可執行 Phase 2 實驗參數並當成已生效策略；若 YAML 僅表達「意圖」而 trainer 無入口，結論必須降級為探索性敘述。
- [ ] **契約一致**：與 Phase 1 同 `metric/timezone/censored`，且比較窗可對齊。
- [ ] **至少雙窗**：每路線至少 2 個時間窗可比結果，避免單窗幻覺（對照 Tasklist `T11A`）。
- [ ] **結論強度標註**：報告需標 `exploratory` / `comparative` / `decision_grade`，不可省略。

#### 1.8.1 Phase 2 Gate 機械檢查（`evaluate_phase2_gate` / T11A）

以下與 **`orchestrator/evaluators.py`** 行為對齊；判讀 **`phase2/phase2_gate_decision.md`** 與 **`run_state.phase2_gate_decision`** 時請一併閱讀 **`evidence_summary`** 與 **`conclusion_strength`**，**不可**只看 **PASS**／**FAIL**／**BLOCKED** 標籤。

1. **雙窗硬 Gate（預設開）**：在 per-job uplift 已滿足 **`gate.min_uplift_pp_vs_baseline`** 且（若適用）std gate 未否決後，若要維持 **`PASS`**，bundle 內 **`phase2_pat_series_by_experiment`** 須存在至少一條 PAT@1% 序列，且**最長序列長度** ≥ **`gate.min_pat_windows_for_pass`**（預設 **2**）。否則狀態為 **BLOCKED**，blocking code **`phase2_insufficient_pat_windows_for_pass`**。
2. **序列從哪來**：full **`run_pipeline.py --phase phase2`** 在寫入 gate 報表前會呼叫 **`collectors.merge_phase2_pat_series_from_shared_and_per_job`**（條件滿足時把共享 PAT 與 per-job 預覽併成兩點序列）。若未觸發 merge 或 YAML 未提供足夠長的手寫序列，仍可能觸發上一項 **BLOCKED**。
3. **關閉雙窗檢查（僅限 smoke／除錯）**：Phase 2 YAML 的 **`gate.min_pat_windows_for_pass: 0`**（或 ≤0）可關閉上述硬 Gate；**不應**複製到宣稱可下產品結論的正式實驗設定。
4. **勝者欄位**：uplift 路徑曾判定「達標」時，**metrics** 可能含 **`phase2_winner_*`**；若最終因雙窗或其他理由變為 **BLOCKED**，勝者欄位仍可能保留以利除錯——**以 `status` 與 `blocking_reasons` 為準**。

#### 1.8.2 Phase 2 orchestrator 錯誤碼速查（runner／ingest／bundle）

下列字串常見於 **`runner.run_logged_command` 回傳的 `error_code`**、**`run_pipeline.py` Phase 2 步驟的 `error_code`**，或 **`phase2_bundle.json` 的 `errors[].code`**（後者若存在，**`evaluate_phase2_gate`** 會將 bundle 判為 **FAIL** 並把 code 列入 **`blocking_reasons`**）。與 **§1.8.1** 的 **gate 專用 `blocking_reasons`（如 `phase2_insufficient_pat_windows_for_pass`）** 不同：gate 理由以 **`phase2_gate_decision.md`／`run_state.phase2_gate_decision`** 為準。

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

**觀測**：**`run_state.phase2_collect.phase2_pat_matrix_yaml_experiment_count`** 僅統計 YAML 已宣告 **`precision_at_recall_1pct_by_window`** 的實驗數，與 runner 是否已產出真多窗矩陣無必然相等關係（見 Tasklist **T10** 收尾項）。

---

## 2) 腳本化實作計畫（Implementation Plan）

### 2.1 目標與範圍（更新）

- 目標：將目前手動流程改為可重跑、可中斷續跑、可生成工件的 orchestrator，最終覆蓋 Phase 1~4，並達成單一命令 E2E。
- 範圍：
  - **已完成**：Phase 1 MVP；**`--phase phase2` MVP**（plan／可選訓練與回測／gate／報表）；**`--phase all --dry-run`**（T16A）。
  - **進行中**：Phase 2 完整矩陣與科學 Gate（T10 收尾、T10A/T10B/T11A）。
  - **未開始**：Phase 3/4 獨立 full run；`--phase all` 非 dry-run 串接；Autonomous supervisor（T8A–T8D、T17）。
  - 保留人工決策：最終上線裁決仍由 reviewer/owner 簽核，orchestrator 提供證據與建議。

### 2.2 建議檔案結構

- `investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/config_loader.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/runner.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/evaluators.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/report_builder.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/templates/*.md.j2`
- `investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml`
- `investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml`
- `investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase3.yaml`
- `investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase4.yaml`
- `investigations/precision_uplift_recall_1pct/orchestrator/config/run_full.yaml`
- `investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/run_state.json`

### 2.3 CLI 設計

**已實作（`run_pipeline.py`）**（路徑請自 repo 根調整；均需 `--run-id`）：

- `--phase phase1` + `--config .../run_phase1.yaml`（可選 `--dry-run`、`--resume`、`--collect-only`、`--skip-backtest-smoke`）
- `--phase phase2` + `--config .../run_phase2.yaml`（可選 `--dry-run`、`--resume`、`--skip-backtest-smoke`、`--skip-phase2-trainer-smoke`、`--phase2-run-trainer-jobs`、`--phase2-run-per-job-backtests`、`--phase2-run-backtest-jobs`、`--phase2-fail-on-gate-fail`、`--phase2-fail-on-gate-blocked`）
- `--phase all` + `--config .../run_full.yaml` + **`--dry-run` 必備**（可選 `--resume`、`--skip-backtest-smoke`）

**規劃中（尚未實作；勿寫進操作 runbook 當現況命令）**：

- `--phase phase3` / `--phase phase4` 獨立 full run
- `--phase all` 非 dry-run、Gate-driven 串接
- `--mode autonomous`（目前 **不存在**）

### 2.3.1 最小 config schema 草稿（可直接做為實作起點）

> 原則：先求「可跑可追溯」，再加欄位；所有路徑預設用 repo root 相對路徑。  
> **Phase 2 已落地**：`orchestrator/config_loader.py` 的 **`validate_phase2_config`**（**T10A**）— 每個實驗的 **`overrides` 必須為空 mapping（`{}`）**；非空鍵會 **`E_CONFIG_INVALID`**。可執行訓練 CLI 參數請寫在 **`trainer_params`**，且鍵名必須落在白名單內（與 repo 內 **`PHASE2_TRAINER_PARAM_KEYS`** 一致，例如 `use_local_parquet`、`skip_optuna`、`recent_chunks`、`sample_rated`、`lgbm_device`）。  
> 諸如 **`hard_negative_weight`**、`objective_variant` 等仍屬 Tasklist **T10B** 之 `blocked`／`planned` 者，**不可**透過 `overrides` 繞過；待 trainer 有明確 CLI／契約後再納入白名單與 `trainer_params`。

#### A) `run_phase2.yaml`（Track A/B/C）

```yaml
phase: phase2
run_id: "phase2_20260410"

common:
  # 版本化 bundle：與 trainer 產物 layout 一致（見 trainer/core/model_bundle_paths.py）
  models_root: out/models
  model_version: "20260408-173809-e472fd0"
  # 或改為單一路徑： model_dir: out/models/20260408-173809-e472fd0（與 model_version 二擇一）
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
      # T10A：第二個實驗用 whitelist「trainer_params」對齊 trainer CLI（示例：較短 chunk 窗）
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
  # T11A 可選：雙窗硬 Gate（見 §1.8.1；省略時 evaluator 預設視為 2）
  # min_pat_windows_for_pass: 2
```

> 與 **`orchestrator/config/run_phase2.yaml`** 實檔對齊：僅 **`overrides: {}`** + 可選 **`trainer_params`**；勿在範例中復活非空 **`overrides`**。

#### B) `run_phase3.yaml`（Winner route 加深）

```yaml
phase: phase3
run_id: "phase3_20260412"

upstream:
  phase2_run_id: "phase2_20260410"
  winner_track: track_a
  # 示意：請替換為 Phase 2 實際勝者 exp_id（勿預設為 hard-negative 名稱，除非 T10B 已標為 supported）
  winner_exp_id: a_candidate_winner

common:
  model_dir: out/models/20260408-173809-e472fd0
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
  max_feature_sets: 5
  max_ensemble_candidates: 4
  max_parallel_jobs: 1
  backtest_skip_optuna: true

workstreams:
  feature_uplift:
    enabled: true
    sets: [behavior_v1, slice_pack_v1]
  slice_targeted:
    enabled: true
    target_slices: [high_roller, new_player]
  ensemble_ablation:
    enabled: true
    candidates: [single_model, light_ensemble]
  top_band_calibration:
    enabled: true
    method: isotonic

gate:
  min_incremental_uplift_pp_vs_phase2_winner: 1.0
  max_regression_pp_on_key_slice: 1.0
```

#### C) `run_phase4.yaml`（Freeze + Multi-window + Go/No-Go）

```yaml
phase: phase4
run_id: "phase4_20260415"

candidate:
  model_dir: out/models/20260408-173809-e472fd0
  source_phase3_run_id: "phase3_20260412"
  threshold_strategy: calibrated_top_band

evaluation:
  windows:
    - id: w1_recent_weekday
      start_ts: "2026-04-09T00:00:00+08:00"
      end_ts: "2026-04-11T00:00:00+08:00"
    - id: w2_recent_weekend
      start_ts: "2026-04-11T00:00:00+08:00"
      end_ts: "2026-04-13T00:00:00+08:00"
    - id: w3_recent_mix
      start_ts: "2026-04-13T00:00:00+08:00"
      end_ts: "2026-04-15T00:00:00+08:00"
  contract:
    metric: precision_at_recall_1pct
    timezone: Asia/Hong_Kong
    exclude_censored: true

resources:
  max_parallel_jobs: 1
  backtest_skip_optuna: true

gate:
  target_precision_at_recall_1pct: 0.60
  max_allowed_slice_regression_pp: 1.0
  max_allowed_alert_volume_ratio: 1.3
  decision_levels: [GO, CONDITIONAL_GO, NO_GO]
```

#### D) `run_full.yaml`（All-phase 串接）

```yaml
phase: all
run_id: "all_20260415"

execution:
  phase_order: [phase1, phase2, phase3, phase4]
  stop_on_gate_block: true
  allow_force_next: false

dry_run:
  # 啟動 full run 前的 readiness checklist（全部為 true 才可回報 READY）
  validate_phase_configs_exist: true
  validate_phase_schemas: true
  validate_phase_dependencies: true
  validate_contract_consistency: true
  validate_paths_readable: true
  validate_writable_targets: true
  validate_cli_smoke_per_phase: true
  validate_resource_limits: true
  # 若 true，任一項失敗即 NOT_READY
  fail_on_any_check: true

phase_configs:
  phase1: orchestrator/config/run_phase1.yaml
  phase2: orchestrator/config/run_phase2.yaml
  phase3: orchestrator/config/run_phase3.yaml
  phase4: orchestrator/config/run_phase4.yaml
```

#### E) schema 驗證最低要求（建議）

- 共通必填：`phase`、`run_id`、`contract.metric`、`window(s)`、核心路徑（model/DB）。
- phase 相依驗證：
  - phase3 必須有 `upstream.phase2_run_id` 與 `winner_track`。
  - phase4 必須有 `candidate.source_phase3_run_id` 與 `evaluation.windows`。
- 資源保護驗證：
  - `max_parallel_jobs >= 1` 且預設為 1。
  - `max_trials_per_track`、`max_feature_sets` 設上限，避免筆電 OOM。
- 可追溯性驗證：
  - `run_id` 不可空，且所有 artifacts 要落在 `orchestrator/state/<run_id>/...`。
- all-phase dry-run 驗證：
  - `dry_run.*` 欄位需完整可解析（缺值採安全預設 `true`）。
  - `validate_phase_dependencies` 必須檢查 phase2->phase3->phase4 上游關係。
  - `validate_contract_consistency` 必須檢查 metric/timezone/censored 契約跨 phase 一致。
  - `validate_resource_limits` 必須檢查 `max_parallel_jobs` 等上限避免筆電 OOM。

### 2.4 核心流程（Phase 1 Autonomous）

1. （可選）`--dry-run`：只做 readiness 檢查，輸出 `READY / NOT_READY`。
2. 載入 config + 驗證 schema（缺欄位直接 fail）。
3. 執行 preflight（路徑、DB、必要表、模型 artifact）。
4. 由 supervisor 啟動 scorer/validator 並進行健康監控（含自動重啟）。
5. 監控觀測時長與樣本量門檻（時間 + finalized alerts + TP）。
6. 依 `phase1.checkpoints` 自動跑 mid snapshots（至少 1 個 mid）。
7. 在終點自動跑 final snapshot + backtest。
8. 收集 JSON/CSV/DB 指標，產出 `phase1/*.md`。
9. 計算 Gate 狀態（PASS / PRELIMINARY / FAIL）並寫入 `phase1_gate_decision.md`。

### 2.5 核心流程（Phase 2~4 擴充）

1. **Phase 2（Track A/B/C）**
  - 讀取 phase2 config（固定 run 契約 + track 實驗矩陣 + 資源上限）。
  - 先跑「科學可判讀前置清單」（見 §1.8）；未通過者只能做 exploratory，不可進決策級結論。
  - 執行 A/B/C 路線（至少 baseline + candidate）；**跨窗**在現況 orchestrator 需多次 run 或手寫 `precision_at_recall_1pct_by_window`，全自動多窗彙整見 Tasklist T10 未完成項。
  - 產出 `phase2/*.md` 與 `phase2_gate_decision.md`。
2. **Phase 3（勝者路線加深）**
  - 僅接受 Phase 2 winner track 作為輸入（防止範圍漂移）。
  - 執行特徵加深、切片定向、集成消融與高分段校準。
  - 產出 `phase3/*.md` 與 `phase3_gate_decision.md`。
3. **Phase 4（定版與 Go/No-Go）**
  - 鎖定 freeze candidate，執行多窗回放與影響估算。
  - 輸出 `phase4/*.md`，形成 `go_no_go_pack.md` 供人工簽核。
4. **All-phase 串接（可選）**
  - `--phase all` 依 gate 結果控制是否進下一階段。
  - 預設 gate block 即停止；可選 `--force-next`（需高風險提示）。

### 2.6 Gate 引擎（建議）

- `PRELIMINARY`：達到最短時長（48h）但未達建議樣本量。
- `PASS`：達到建議時長與樣本量，且自動產生的 mid/final R1 方向一致。
- `FAIL`：關鍵資料缺失、口徑衝突、或指標明確不達條件。

**Phase 1 parity 補充（避免誤判）**
- 目前 MVP Gate 可在 parity 檔僅為 scaffold 時仍回傳 `PASS`；此 `PASS` 僅代表現行 gate 規則通過。
- 若要升級為決策級結論，請先補齊可機械驗證的 parity 指標，再判定是否可進下一階段。

**Phase 1 parity 建議閾值（初版）**
- `min_scored_at_in_window_ratio = 0.995`
- `min_validated_at_non_null_ratio = 0.995`
- `max_alert_prediction_gap_abs = 100`

> 這三個閾值是「先可運行」版本，後續可依實際資料穩定度調整；不要在未收斂前過度收緊，避免在筆電長跑時頻繁誤阻斷。

補充（Phase 2~4）：

- Phase 2：至少 1 條 track 達 uplift（例如 +3~5pp）且跨窗波動在容忍內。
- Phase 3：相對 Phase 2 winner 再提升，且不犧牲穩定性/切片健康。
- Phase 4：多窗與風險指標達標，輸出 `GO / CONDITIONAL_GO / NO_GO` 建議。

### 2.7 錯誤碼與 fail-fast

- `E_CONFIG_INVALID`
- `E_DB_UNAVAILABLE`
- `E_NO_DATA_WINDOW`
- `E_EMPTY_SAMPLE`
- `E_GATE_NOT_READY`
- `E_ARTIFACT_MISSING`
- `E_PHASE_CONFIG_MISMATCH`
- `E_PHASE_DEPENDENCY_MISSING`

### 2.8 效能與穩定性（必做）

- production 長跑前先做 `--dry-run`，避免連線/路徑問題延遲到長跑中後段才發現。
- 預設保守參數：
  - `sample_size` 先小（如 1000）
  - `player_chunk_size` 100~200
  - backtest 預設 `--skip-optuna`
- 大檔優先 parquet；避免全量讀入記憶體。
- 所有步驟寫入 `run_state.json`（可 resume）。
- Phase 2~4 預設限制 windows/trials/parallelism，避免筆電 OOM 或過長等待。
- Backtest/報表路徑需與 `run_id` 綁定，避免讀到舊 `backtest_metrics.json`。

### 2.9 分階段落地里程碑（更新）

1. **MVP（已完成）**：Phase 1 MVP（可跑、可產工件、可判 Gate）
2. **A0（3~5 天）**：Phase 1 Autonomous 閉環（supervisor + 自動 checkpoints + 自動 mid/final）
3. **V2（3~5 天）**：Phase 2 自動化（track runner + phase2 gate + phase2 報表）
4. **V3（3~5 天）**：Phase 3 自動化（winner route + phase3 gate + phase3 報表）
5. **V4（3~5 天）**：Phase 4 自動化（freeze/multi-window/impact/go-no-go）
6. **V4+（2~3 天）**：`--phase all` 串接與 phase 級 resume/穩定性強化
7. **V5（2~4 天）**：E2E 長跑穩定性與故障注入驗收（零人工介入）

---

## 3) 與主計畫文件的分工

- `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md`：保留高層目標、Gate、里程碑、最終 runbook摘要。
- 本文件：承接 ad-hoc 細節與腳本實作藍圖，作為「執行手冊 + 開發規格」。

