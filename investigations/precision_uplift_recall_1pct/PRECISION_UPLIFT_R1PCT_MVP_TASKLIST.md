# Orchestrator 開發任務清單（Phase 1~4）

> 來源：`PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md` §2  
> 目標：先完成 Phase 1 MVP，並以同一框架擴充到 Phase 2~4 與 `--phase all` 無人值守自動化。

## 文件契約（避免 singleton plans）

- 本文件是 **工程實作真相（Implementation SSoT）**：已完成/未完成、限制、DoD 以此為準。
- `PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md` 是 **操作真相（Operations SSoT）**：流程步驟、執行命令、故障處理。
- 若兩文件敘述衝突，以本文件的實作狀態為準；Runbook 必須引用本文件對應任務（例如 T10A/T10B/T11A）。
- 任何新增 Phase 2 策略參數（如 hard negative/focal/gating）必須先在本文件登記「trainer 是否已支援」與對應任務狀態。
- **與 `.cursor/plans/STATUS.md` 的關係**：該檔為整 repo（trainer／deploy／validator 等）的**總覽與歷史輪次**，**通常不含**本調查 orchestrator 的細項；**precision uplift orchestrator 的實作狀態以本文件 + `orchestrator/run_pipeline.py` 為準**。

---

## 0. 範圍與版本策略

### 0.1 已交付（MVP）

- [x] 只做 `--phase phase1` 的可用 MVP
- [x] 支援 config 載入/驗證、preflight、R1/R6 + backtest 執行
- [x] 支援 collect、Gate 判斷、`phase1/*.md` 報告輸出
- [x] 支援 `run_state.json`、`--resume`、`--dry-run`

### 0.2 實作現況快照（與 `run_pipeline.py` 對齊）

| `--phase` | 現況（2026-04-11） |
| :--- | :--- |
| `phase1` | **Full run 可用**：preflight → R1/R6 → backtest → collect → Gate → `phase1/*.md` |
| `phase2` | **Full run 可用（部分能力）**：preflight → plan bundle → runner smoke →（可選）`trainer.trainer` 每 job、harvest、（可選）per-job / shared backtest → gate → `phase2/*.md`。**T10A** `trainer_params`／拒非空 `overrides` 已落地；**T11A** 科學 Gate／勝者／雙窗／`conclusion_strength` 已落地；**T11 目標**之「淘汰理由」敘事已部分落地（**`phase2_elimination_rows`** + gate md **Uplift elimination** + **`phase2_collect`** PAT 序列覆蓋摘要）。真多窗實驗矩陣、統一結果結構、fail-fast 仍見 **T10** 未完成項 |
| `all` | **僅 `--dry-run`**：all-phase readiness（T16A）。**非 dry-run 的 `--phase all` 會 exit 2**，`--mode autonomous` **不存在**於現有 CLI |
| `phase3` \| `phase4` | **無獨立 full run**；僅 all-phase dry-run 下的 **minimal schema** 驗證（T16A） |

### 0.3 待擴充（V2 / V3 / V4 / V5）

- [ ] **`phase2` 完整**自動化：T10 未完成項（每實驗跨窗矩陣、統一結果結構、fail-fast）、**T10A/T10B/T11A**（參數白名單、能力矩陣、科學 Gate）
- [ ] `--phase phase3` 自動化（勝者路線加深 + Phase 3 Gate）
- [ ] `--phase phase4` 自動化（freeze/multi-window/impact/go-no-go）
- [ ] `--phase all` **非 dry-run** 多 phase 串接（依 Gate 決定是否進下一階段；**T16**）
- [ ] Autonomous supervisor（長跑狀態機、checkpoint、故障自復；**T8A–T8D / T17**）
- [ ] 中途/期末 snapshot 全自動（不需手動落 `r1_r6_mid.stdout.log`）
- [ ] scorer/validator 生命週期全自動（啟動、健康檢查、重啟、回收）

### 0.4 明確不在近期範圍（先不做）

- 複雜分散式排程與高度並行化 DAG（本專案先維持單機單流程）
- 全自動最終商業決策（Go/No-Go 仍需人工簽核）

---

## 1. Phase 1 MVP 任務（已完成）

### T1 - 建立骨架與 CLI（高優先）

- [x] 新增目錄：`orchestrator/`
- [x] 新增檔案：
  - [x] `orchestrator/run_pipeline.py`
  - [x] `orchestrator/config_loader.py`
  - [x] `orchestrator/runner.py`
  - [x] `orchestrator/collectors.py`
  - [x] `orchestrator/evaluators.py`
  - [x] `orchestrator/report_builder.py`
  - [x] `orchestrator/config/run_phase1.yaml`
- [x] `run_pipeline.py` 支援參數：
  - [x] `--phase phase1`
  - [x] `--config <path>`
  - [x] `--run-id <id>`
  - [x] `--collect-only`
  - [x] `--resume`

**完成定義**
- [x] 可執行 `python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py --phase phase1 --config ...`
- [x] 未提供必要參數時，錯誤訊息可讀且 exit code 非 0

### T2 - Config Schema + Preflight（高優先）

- [x] `config_loader.py` 定義 Phase 1 必要欄位
  - [x] `model_dir`
  - [x] `state_db_path`
  - [x] `prediction_log_db_path`
  - [x] `window.start_ts`
  - [x] `window.end_ts`
  - [x] `thresholds`（Gate 相關門檻）
- [x] 缺欄位拋 `E_CONFIG_INVALID`
- [x] `runner.py` 實作 preflight：
  - [x] 路徑存在檢查
  - [x] DB 可開啟
  - [x] 必要表存在（`prediction_log`、`alerts`、`validation_results`）
  - [x] backtest smoke test 命令可跑

### T3 - 流程執行器（高優先）

- [x] `runner.py` 封裝命令執行（subprocess）
- [x] 接兩個既有命令：
  - [x] `run_r1_r6_analysis.py --mode all --pretty`
  - [x] `python -m trainer.backtester ...`
- [x] 支援落地 stdout/stderr 到 run 目錄
- [x] 失敗映射錯誤碼：
  - [x] `E_NO_DATA_WINDOW`
  - [x] `E_EMPTY_SAMPLE`
  - [x] `E_ARTIFACT_MISSING`

### T4 - Collector 與中繼資料（高優先）

- [x] `collectors.py` 讀取：
  - [x] `trainer/out_backtest/backtest_metrics.json`
  - [x] R1/R6 JSON payload（中途 + 最終）
  - [x] `state.db` 基本統計（finalized alerts、TP）
- [x] 輸出統一 dict（供 report_builder/evaluator 共用）

### T5 - Gate Evaluator（高優先）

- [x] `evaluators.py` 實作：
  - [x] `PRELIMINARY`：達 48h 但未達建議樣本量
  - [x] `PASS`：達建議門檻且方向一致
  - [x] `FAIL`：資料缺失/口徑衝突/明確不達標
- [x] 產出：`status`、`blocking_reasons[]`、`evidence_summary`

### T6 - 報表渲染（高優先）

- [x] `report_builder.py` 寫入（或更新）：
  - [x] `phase1/upper_bound_repro.md`
  - [x] `phase1/label_noise_audit.md`
  - [x] `phase1/slice_performance_report.md`
  - [x] `phase1/point_in_time_parity_check.md`（**目前為 MVP scaffold + 人工核對清單**，非自動 parity 指標）
  - [x] `phase1/phase1_gate_decision.md`
- [x] `status_history_crosscheck.md` 保留人工維護，附 orchestrator 區塊

**MVP 限制（需明確告知 reviewer）**
- `point_in_time_parity_check.md` 現況僅輸出資料來源與人工核對項，未自動計算 PIT/時區/成熟度一致性指標。
- `evaluate_phase1_gate(...)` 現況不以 PIT parity 自動檢核作為 PASS 的必要條件。
- 因此 `phase1_gate_decision.md` 的 `PASS` 代表「MVP Gate 規則通過」，不等同「PIT parity 已完成機械驗證」。

**後續任務（P1 parity 補強）**
- [ ] 新增 parity collector：輸出可機械判讀的 PIT 指標（`scored_at`、`validated_at`、時區/窗界一致性）。
- [ ] 擴充 `point_in_time_parity_check.md`：除 checklist 外，附 JSON 指標摘要與 FAIL 條件。
- [ ] 擴充 `evaluate_phase1_gate(...)`：新增可配置 parity blocking 規則（至少 `STRICT` / `WARN_ONLY` 兩種模式）。

**P1 parity 最小規格（可直接開工）**
- [x] `collectors.py` 新增 `collect_phase1_pit_parity(...) -> dict`，輸出鍵：
  - [x] `status`（`ok` / `warn` / `fail`）
  - [x] `scored_at_in_window_ratio`（`prediction_log` 中 `scored_at` 落在 `[start_ts, end_ts)` 的比例）
  - [x] `validated_at_non_null_ratio`（`validation_results` 中 `validated_at` 非空比例）
  - [x] `window_timezone_mismatch_count`（時區/窗界不一致筆數；無法判讀時至少回傳 `note`）
  - [x] `alerts_vs_prediction_log_gap`（沿用 R2 差值，供 parity 區塊交叉引用）
  - [x] `reasons[]`（違規原因代碼）
- [x] `report_builder.py` 在 `point_in_time_parity_check.md` 新增 `## PIT parity metrics (auto)` JSON 區塊。
- [x] `config/run_phase1.yaml` 新增：
  - [x] `thresholds.pit_parity_mode`（`STRICT` / `WARN_ONLY`，預設 `WARN_ONLY`）
  - [x] `thresholds.min_scored_at_in_window_ratio`（預設 `0.995`）
  - [x] `thresholds.min_validated_at_non_null_ratio`（預設 `0.995`）
  - [x] `thresholds.max_alert_prediction_gap_abs`（預設 `100`）
- [x] `evaluators.py` 新增 gate 規則：
  - [x] `STRICT`：任一 parity threshold 不達標 -> `FAIL`（blocking reason: `pit_parity_violation`）
  - [x] `WARN_ONLY`：不阻斷 gate，但 `evidence_summary` 必須附 `pit_status=warn/fail`

**P1 parity 完成定義（DoD）**
- [x] 若 DB 缺欄位（如無 `validated_at`），collector 不崩潰，回傳 `status=warn` + `reasons[]`。
- [x] `phase1_gate_decision.md` 的 `metrics` 含 `pit_parity_status` 與主要 ratio。
- [ ] 新增最少 3 個單元測試：`STRICT fail`、`WARN_ONLY pass with warning`、`missing column -> warn`。

### T7 - run_state 與 Resume（中優先）

- [x] 每步落地：`orchestrator/state/<run_id>/run_state.json`
- [x] 記錄 step 狀態、輸入摘要、產物路徑、錯誤碼/訊息
- [x] `--resume` 跳過已成功步驟（含 config fingerprint 防呆）

### T8 - Dry-run 上線前快檢（中優先）

- [x] `run_pipeline.py` 新增 `--dry-run`
- [x] dry-run 檢查 config / preflight / CLI smoke / 路徑可寫
- [x] `run_state.json` 新增 `mode: dry_run` 與 `readiness.*`
- [x] `READY / NOT_READY` 與 exit code 可明確判讀

---

## 2. 全階段自動化任務（新增）

### V2.5 - Phase 1 無人工介入閉環（Autonomous P1）

#### T8A - 長跑 Supervisor（Phase 1）

- [ ] `run_pipeline.py` 新增 autonomous mode（例如 `--mode autonomous`）
- [ ] 內建 phase1 狀態機：`init -> observe -> mid_snapshot -> final_snapshot -> collect -> report`
- [ ] 支援可恢復 checkpoint（程序重啟後可從最近 step 接續）

**完成定義**
- [ ] 單一命令可在 72~120h 觀測期內持續運作，不需人工介入
- [ ] 程式非正常終止後可 `--resume` 回復到 checkpoint

#### T8B - scorer/validator 自動生命週期

- [ ] orchestrator 代管 `trainer.scorer` 與 `trainer.validator` 子程序
- [ ] 週期性健康檢查（DB row 成長、stderr 關鍵字、心跳時間）
- [ ] 異常自動重啟（可配置最大重試次數與冷卻時間）
- [ ] run 結束時自動優雅回收子程序

**完成定義**
- [ ] 不需人工手動開/關 scorer/validator
- [ ] 重試耗盡時輸出明確 blocking reason 與錯誤碼

#### T8C - 自動 mid/final R1 snapshot

- [ ] 新增 `phase1.checkpoints` 設定（例：`t+6h`、`t+24h`、`end`）
- [ ] 自動執行 R1/R6 並按 checkpoint 產生檔名（不得覆寫彼此）
- [ ] collector 改為讀取「最新有效 mid」與「final」進行方向檢查

**完成定義**
- [ ] 不需手動建立 `r1_r6_mid.stdout.log`
- [ ] Gate 可直接使用自動產生的 mid/final 證據

#### T8D - 資源守門（筆電保護）

- [ ] heavy 任務並行上限（預設 1）與總運行時限（`max_runtime_hours`）
- [ ] 窗口/試驗數上限（`max_windows`、`max_trials`）
- [ ] 記憶體/磁碟壓力保護（大檔分塊、非必要 payload 不常駐記憶體）

**完成定義**
- [ ] 在筆電環境連跑數天不因 OOM 或過度 swap 失控
- [ ] 超限時 fail-fast，並保留可恢復狀態

### V2 - Phase 2 自動化（Track A/B/C）

#### T9 - CLI 與 Config 擴充（phase2）

- [x] `run_pipeline.py` 支援 `--phase phase2`
- [x] 新增 `orchestrator/config/run_phase2.yaml` schema：
  - [x] 固定 run 契約（model/version、window、label/censored 契約）
  - [x] tracks 設定（A/B/C 每條路線的實驗矩陣）
  - [x] 資源限制（max windows、max trials、skip_optuna 預設）

**完成定義**
- [x] 可單獨執行 `--phase phase2` 並寫入 `run_state`
- [x] config 缺欄位時 fail-fast（`E_CONFIG_INVALID`）

#### T10 - Phase 2 Track Runner + Collector

- [x] runner **smoke 與 log 目錄**（`phase2_runner_smoke`）：依 `job_specs` 建 **`investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/logs/phase2/...`**（與 `run_state`／`phase2_bundle` 同樹；相對路徑見 bundle 內 `logs_subdir_relative`）；可選 `python -m trainer.trainer --help`（`--skip-phase2-trainer-smoke` 略過）；結果寫入 `phase2_bundle.runner_smoke`；失敗 exit **5**
- [x] runner **可選**對每個 `job_specs` 呼叫 `python -m trainer.trainer`（`--phase2-run-trainer-jobs`；`common.window` + `resources.backtest_skip_optuna`／可選 `trainer_use_local_parquet`、可選 `phase2_trainer_job_timeout_sec`）；預設略過並在 bundle 寫入 `trainer_jobs.executed: false`；任務失敗 exit **7**；**T10A**：非空 `overrides` 於載入設定時 **`E_CONFIG_INVALID`**；可執行參數見 **`trainer_params` 白名單**（`build_phase2_trainer_argv`）；成功列記 **`resolved_trainer_argv`**／**`argv_fingerprint`**；訓練**成功**後自 log **推斷**產物目錄並回填 **`job_specs[].training_metrics_repo_relative`**（僅當 YAML 未手動指定；見 **`runner.infer_training_metrics_repo_relative_from_trainer_logs`**／**`merge_inferred_training_metrics_paths_into_phase2_bundle`**）
- [x] runner **每實驗回測**（`phase2_per_job_backtest_jobs`）：**`--phase2-run-per-job-backtests`** 對有 **`training_metrics_repo_relative`** 的 `job_specs` 各跑 **`trainer.backtester`**（`phase2_cfg_to_backtest_cfg` 覆寫 `model_dir`）；**`--output-dir`** 指向 `…/_per_job_backtest/`，成功後讀同目錄 **`backtest_metrics.json`**（**`collectors.phase2_per_job_backtest_metrics_repo_relative`**）寫入 **`per_job_backtest_jobs.results[].shared_precision_at_recall_1pct_preview`**；共享回測仍寫 **`resources.backtest_metrics_path`**／預設路徑；步驟順序仍為 **per-job 在前、共享在後**（利於 gate 合併 shared PAT 與 per-job 預覽）；無 hint 的列 **skip**；失敗 **exit 8** **`E_PHASE2_PER_JOB_BACKTEST_JOBS`**；預設略過 **`per_job_backtest_jobs.executed: false`**
- [x] runner **共享回測**（`phase2_backtest_jobs`）：**`--phase2-run-backtest-jobs`** 跑單次 `trainer.backtester`（`phase2_cfg_to_backtest_cfg`）；log 於 `state/<run_id>/logs/phase2/_shared_backtest/`；ingest `resources.backtest_metrics_path` 或預設 `trainer/out_backtest/backtest_metrics.json`；成功則 **`status: metrics_ingested`**；子程序失敗／缺檔 ingest **exit 8** + **`E_ARTIFACT_MISSING`**（bundle `errors`）；預設略過並寫 **`backtest_jobs.executed: false`**
- [x] **`phase2_job_metrics_harvest`**（於 `phase2_trainer_jobs` 之後、`phase2_backtest_jobs` 之前）：`collectors.harvest_phase2_job_training_metrics` 對每個 `job_specs` 讀取訓練指標：**優先**實驗選填 **`training_metrics_repo_relative`**（repo 相對路徑：檔案或含 `training_metrics.json` 的目錄；拒絕絕對路徑與逃出 repo 根）；**否則** **`{logs_subdir_relative}/training_metrics.json`**；寫入 **`phase2_bundle.job_training_harvest`**；`phase2_collect`／gate **`metrics`** 附 **`job_training_harvest_*`**；**`--resume`** 成功步驟可跳過
- [ ] runner **完整** A/B/C：**每實驗**回測鏈、指標彙整、跨窗統計；缺檔／無資料等 fail-fast（與共享回測並行演進）
- [ ] 產出統一結果結構（每 track 的主指標、切片、跨窗統計）
- [x] collector 自 phase2 YAML 產出 **plan-only** `phase2_bundle.json`（`bundle_kind: phase2_plan_v1`、`status: plan_only`；寫入 `orchestrator/state/<run_id>/`；含 **`job_specs`**（啟用實驗 + 建議 `logs_subdir_relative`，供 T10 runner 掛 stdout／stderr）；**best-effort 讀取** job log 下 **`training_metrics.json`** 見上列 **`job_training_harvest`**；**`phase2_collect`** 含 **`job_specs_training_metrics_hint_count`**（已填 **`training_metrics_repo_relative`** 的 job 數）；完整每實驗訓練產物矩陣仍待 runner／契約）
- [ ] fail-fast 保護：
  - [ ] 任一路線輸出缺檔 → `E_ARTIFACT_MISSING`
  - [ ] 窗口無資料 → `E_NO_DATA_WINDOW`

#### T10A - Strategy Parameters Wiring（trainer ↔ orchestrator）

- [x] Phase 2 config 新增 `trainer_params` 白名單（取代任意 `overrides` 直接宣稱可用）
  - [x] 初始白名單先對齊現有 trainer CLI：`use_local_parquet`、`skip_optuna`、`recent_chunks`、`sample_rated`、`lgbm_device`
  - [ ] 進階策略（如 `hard_negative_weight`）僅在 trainer 已實作且有明確入口時加入白名單
- [x] `runner.build_phase2_trainer_argv` 將白名單參數映射為實際 CLI argv
- [x] config 驗證：白名單外 key 一律 `E_CONFIG_INVALID`；非空 `overrides` 一律 `E_CONFIG_INVALID`（禁止 silently unapplied）
- [x] `trainer_jobs.results[]` 落地 `resolved_trainer_argv` 與 `argv_fingerprint`（可稽核、可重現）
- [x] `track_*_results.md` 顯示「參數已套用」證據（**`## Trainer CLI evidence (T10A)`**：YAML `trainer_params` + planned／recorded argv／fingerprint）；`phase2_bundle.json` 本身已含 `tracks.*.trainer_params`

#### T10B - Trainer Capability Matrix（策略能力矩陣）

- [x] 在本文件維護「策略欄位 -> trainer 狀態」矩陣（`supported` / `planned` / `blocked`）
- [x] matrix 至少包含：`objective_variant`、`hard_negative_weight`、`gating_strategy`、`time_cv_policy`
- [x] 若欄位為 `planned` 或 `blocked`，Runbook 不得把該欄位當作可執行實驗參數（見 **`PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md` §1.8**）

**Trainer Capability Matrix（v0，2026-04-11）**

| 欄位 / 策略能力 | 狀態 | trainer 現況（摘要） | orchestrator Phase 2 現況 | 下一步（任務） |
| :--- | :--- | :--- | :--- | :--- |
| `use_local_parquet` | `supported` | `trainer.trainer` CLI 已支援 `--use-local-parquet` | `trainer_params`／resources → argv；`track_*_results.md` T10A 區塊 | 進階策略鍵、T11A |
| `skip_optuna` | `supported` | `trainer.trainer` CLI 已支援 `--skip-optuna` | 同上 | 同上 |
| `recent_chunks` | `supported` | `trainer.trainer` CLI 已支援 `--recent-chunks` | 同上 | 同上 |
| `sample_rated` | `supported` | `trainer.trainer` CLI 已支援 `--sample-rated` | 同上 | 同上 |
| `lgbm_device` | `supported` | `trainer.trainer` CLI 已支援 `--lgbm-device` | 同上 | 同上 |
| `objective_variant` | `planned` | 尚無通用 CLI 入口切換 objective（需明確設計） | YAML 可表達意圖，但目前不保證生效 | T10A 定義白名單語意 + trainer 端接口 |
| `hard_negative_weight` | `blocked` | 未見對應 CLI 或訓練邏輯（僅 YAML 意圖） | 非空 `overrides` 已拒載；需 trainer 實作後納入白名單 | 先實作 trainer 能力，再納入 T10A |
| `gating_strategy`（Track B） | `blocked` | trainer 目前無明確「分群路由/子模型 gating」接口 | YAML 尚無可執行語意映射 | 先定義訓練/推論契約，再接 orchestrator |
| `time_cv_policy`（Track C） | `planned` | backtester/驗證可做時序評估，但尚無完整「每實驗多窗策略」契約 | gate 目前有 MVP bridge（兩點序列），非完整多窗 | T10 + T11A 補齊 per-exp per-window 結構 |

> 狀態定義：`supported` = trainer 已有可用入口且可由 orchestrator 穩定映射；`planned` = 設計方向存在但未落地完整接口；`blocked` = 目前無 trainer 能力或契約不足，不能當作可執行實驗參數。

**完成定義**
- [x] **（plan_only 階段）** 同一 phase2 YAML + 固定 `run_id` 下，`phase2_bundle.json`（`bundle_kind: phase2_plan_v1`）內容可重現（僅由 config 展開；不含 trainer 隨機性）
- [ ] **（runner 階段）** 含訓練／回測產物後仍滿足可重現與可追溯
- [ ] stdout/stderr 與每路線 artifacts 可追溯

#### T11 - Phase 2 Evaluator + 報表

- [x] `evaluators.py` 新增 `evaluate_phase2_gate(...)`（**plan_only** → `BLOCKED`；bundle 含 `errors` → `FAIL`；其餘 status 暫 `BLOCKED` 直至 runner 產物契約落地）
- [x] Gate（`metrics_ingested`）：**`extract_phase2_shared_precision_at_recall_1pct`**（`model_default.test_precision_at_recall_0.01`）寫入 **`gate.metrics`** 與 **evidence**；仍 **BLOCKED**（`phase2_shared_metrics_no_per_track_uplift`）
- [x] Gate／報表消化 **`per_job_backtest_jobs`**：**`evaluators.phase2_per_job_backtest_metrics`**、**`plan_only`／`metrics_ingested`** 的 **evidence** 附 **per-job PAT@1% preview** 摘要；**`report_builder`** 每軌道 **`## Per-job backtest preview`**
- [x] Gate 規則（**uplift／std**，MVP；**真多窗矩陣資料源**仍待強化／取代兩點 bridge）：
  - [x] **Uplift（per-job 預覽）**：啟用軌道內 **YAML 順序**第一個有 PAT@1% 預覽之實驗為 baseline；後續有預覽者之 uplift（**pp** = 差值×100）達 **`gate.min_uplift_pp_vs_baseline`**（預設 3.0）→ **`evaluate_phase2_gate` `PASS`**；否則 **`FAIL`**（`phase2_uplift_below_min_pp_vs_baseline`）或 **`BLOCKED`**（`phase2_uplift_insufficient_comparisons`）；未跑 per-job 回測時仍 **`phase2_shared_metrics_no_per_track_uplift`**
  - [x] **Std（MVP）**：**`phase2_pat_series_by_experiment`** + **`max_std_pp_across_windows`**；**`merge_phase2_pat_series_from_shared_and_per_job`**（兩點序列 bridge）；手寫／collector 多窗序列可再強化
- [x] `report_builder.py` 軌道結果（每軌道 `phase2/track_{a,b,c}_results.md`：實驗清單、共享 PAT@1%、**Per-job training_metrics harvest**、**Per-job backtest preview**、**Uplift vs baseline**、**PAT@1% series & std (gate)**、**Gate snapshot**；註明共享 **model_dir** 與 per-job **`--output-dir`** 語意）
- [x] `report_builder.py` 新增 **`phase2/phase2_gate_decision.md`**（與 `run_state.phase2_gate_decision` 同步；**plan_only 僅能記錄 BLOCKED 與 blocking reasons**）
- [x] **`run_pipeline.py`**：可選 **`--phase2-fail-on-gate-fail`**（**FAIL** → **exit 9**／**`E_PHASE2_GATE_FAIL`**）、**`--phase2-fail-on-gate-blocked`**（**BLOCKED** → **exit 10**／**`E_PHASE2_GATE_BLOCKED`**）；**`phase2_gate_report`** **failed** 時利於 **`--resume`** 重跑
- [x] **Std gate（MVP）**：bundle 可選 **`phase2_pat_series_by_experiment`**（`track -> {exp_id: [PAT@1% 每窗]}`）；**`statistics.stdev`×100** 與 **`gate.max_std_pp_across_windows`** 比較；僅在 **uplift 已 PASS** 時 std 超標 → **`FAIL`**（**`phase2_std_exceeds_max_pp_across_windows`**）；否則 std 結果僅 **informational**

#### T11A - Scientific Validity Gate（避免「流程有跑但不可下結論」）

- [x] Gate 新增 `strategy_effective` 檢查：對宣告 **`trainer_params`** 之實驗，在 **`trainer_jobs.executed`** 時要求 **`argv_fingerprint` + `resolved_trainer_argv` + `ok`**（見 `evaluators._phase2_evaluate_strategy_effective`）
- [x] 缺證據時 **`evaluate_phase2_gate` → `BLOCKED`**，`blocking_reasons` 含 **`phase2_strategy_params_not_effective`**
- [x] **winner track** 自動輸出與「至少雙窗」之硬 Gate（`evaluate_phase2_gate`：`metrics` 勝者欄位 + 預設 **`min_pat_windows_for_pass: 2`**；`phase2_gate_decision.md` **Winner** 區塊；`run_state.phase2_gate_decision` 鏡像鍵）；**`conclusion_strength`** 仍為輔助標籤（`decision_grade` 另需 trainer audit + per-job 回測等）
- [x] **`conclusion_strength`**：`exploratory` / `comparative` / `decision_grade`（`gate` 回傳與 `metrics.conclusion_strength`）；**`phase2/phase2_gate_decision.md`** 與 **`run_state.phase2_gate_decision`** 附錄 T11A 欄位

**完成定義**
- [x] **（現況）** `phase2_gate_decision.md` 含 gate status、blocking reasons、evidence 摘要；**T11A** 後含勝者欄位、**Uplift elimination**（`metrics.phase2_elimination_rows`）、T11A 科學欄位
- [ ] **（目標）** 含勝者路線、淘汰理由、可稽核 evidence（PAT／uplift 等）— **仍待**：每實驗**完整多窗矩陣**與統一結果表（**T10**）；現行 elimination 僅覆蓋 **uplift gate 可比之 challenger**（per-job preview 口徑）

---

### V3 - Phase 3 自動化（勝者路線加深）

#### T12 - Phase 3 Config + Runner

- [ ] `run_pipeline.py` 支援 `--phase phase3`
- [x] 新增 `orchestrator/config/run_phase3.yaml`（**T16A 最小範例** + `config_loader.validate_phase3_config_minimal`；完整 T12 schema／runner 仍待）
- [ ] 只允許 Phase 2 勝者路線作為輸入（避免全域盲試）
- [ ] runner 支援特徵加深與集成消融工作流

**完成定義**
- [ ] Phase 3 只能在指定 winner_track 上運行（契約防漂移）

#### T13 - Phase 3 Collector/Evaluator/報表

- [ ] collector 彙整 `feature_uplift`、`slice_targeted`、`ensemble_ablation`、`top_band_calibration`
- [ ] evaluator 產出 `phase3` Gate（是否在不犧牲穩定性下再提升）
- [ ] 報表輸出：
  - [ ] `phase3/feature_uplift_table.md`
  - [ ] `phase3/slice_targeted_features.md`
  - [ ] `phase3/ensemble_ablation.md`
  - [ ] `phase3/top_band_calibration_report.md`
  - [ ] `phase3/phase3_gate_decision.md`

**完成定義**
- [ ] 可清楚看到「相對 Phase 2 勝者」是否再提升

---

### V4 - Phase 4 自動化（定版與 Go/No-Go）

#### T14 - Phase 4 Config + Multi-window Runner

- [ ] `run_pipeline.py` 支援 `--phase phase4`
- [x] 新增 `orchestrator/config/run_phase4.yaml`（**T16A 最小範例** + `config_loader.validate_phase4_config_minimal`；完整 T14 schema／runner 仍待）
- [ ] 支援 candidate freeze metadata 與多窗回放矩陣
- [ ] runner 執行多窗回放並限制資源上限（避免 OOM/長跑失控）

**完成定義**
- [ ] 多窗回放可在單 run 內完成且有完整 artifact 索引

#### T15 - Phase 4 Evaluator + Go/No-Go Pack

- [ ] evaluator 產出 `GO / CONDITIONAL_GO / NO_GO` 建議狀態（可配置）
- [ ] report_builder 輸出：
  - [ ] `phase4/candidate_freeze.md`
  - [ ] `phase4/multi_window_backtest.md`
  - [ ] `phase4/impact_estimation.md`
  - [ ] `phase4/go_no_go_pack.md`
- [ ] 明確註記：最終業務決策需人工審批（orchestrator 只提供證據與建議）

**完成定義**
- [ ] 可直接拿 `go_no_go_pack.md` 進評審會議

---

### V4+ - 全流程串接（`--phase all`）

#### T16 - Multi-phase DAG 與 Gate-driven 流程控制

- [x] `run_pipeline.py` 支援 `--phase all`（**目前僅 `--dry-run`**，見 T16A；非 dry-run 長跑串接仍待下列項目）
- [ ] 預設順序：phase1 -> phase2 -> phase3 -> phase4
- [ ] Gate 未達時：
  - [ ] 預設 stop（可選 `--force-next`，但需高風險警告）
- [ ] 每 phase 寫入 `run_state` 子節點，支援 resume 到 phase 級別

#### T16A - All-phase dry-run（上線前必做）

- [x] `run_pipeline.py` 支援 `--phase all --dry-run`（未帶 `--dry-run` 之 `--phase all` 回報 exit 2，直至 T16 長跑實作）
- [x] dry-run 覆蓋 phase1~4：**phase1／2 完整 schema**；**phase3／4 為 minimal schema**（`validate_phase3_config_minimal`／`validate_phase4_config_minimal`，完整形狀見 T12／T14）、phase dependency、路徑可讀、可寫目標、CLI smoke（含 `validate_cli_smoke_per_phase` 與 `--skip-backtest-smoke` 互動）、resource limits
- [x] dry-run 輸出統一 `READY`／`NOT_READY` 與 `blocking_reasons`（見 `run_all_phases_dry_run_readiness`）
- [x] `run_state` 寫入 `phase: all`、`mode: dry_run`、`readiness`（含 checks／blocking／artifacts 摘要，供 CI／人工審核）
- [x] `orchestrator/config/run_full.yaml` + `load_run_full_config`：可選 `dry_run` 區塊覆寫預設 checklist（預設鍵如下，SSOT：`config_loader.DRY_RUN_FLAG_DEFAULTS`）
  - [x] `validate_phase_configs_exist`
  - [x] `validate_phase_schemas`
  - [x] `validate_phase_dependencies`
  - [x] `validate_contract_consistency`
  - [x] `validate_paths_readable`
  - [x] `validate_writable_targets`
  - [x] `validate_cli_smoke_per_phase`
  - [x] `validate_resource_limits`
  - [x] `fail_on_any_check`

**完成定義**
- [ ] 一條命令可**完整執行** all-phase 長跑（或在 gate block 點可恢復）— **屬 T16**，非 T16A
- [x] full run 前可先執行 **`--phase all --dry-run`**，並以 `READY`／`NOT_READY` 得知是否通過靜態／preflight／smoke 關卡（**長跑本體仍須 T16 就緒**）

### V5 - 全自動 E2E（單一命令，零人工介入）

#### T17 - `--phase all --mode autonomous` 契約

- [ ] 單一 config 指定 model / window / label 契約 / 資源上限
- [ ] 預設 phase1->2->3->4 依 Gate 推進（可選 `--force-next`）
- [ ] 每 phase 自動執行訓練、回測、蒐證、報告，不需人工補跑

#### T18 - 統一 artifacts index

- [ ] 每 run 產生 `artifacts_index.json`（phase/step/path/checksum/timestamp）
- [ ] 報告引用只允許來自 index（避免誤讀舊檔）
- [ ] 保留 stdout/stderr 與錯誤碼索引，便於稽核

#### T19 - 長跑穩定性與故障注入測試

- [ ] 測試中斷恢復、子程序 crash、自動重試、checkpoint 回復
- [ ] 測試長窗（>=72h）下 run_state 一致性與工件完整性

#### T20 - 全自動驗收（E2E DoD）

- [ ] 單一命令連跑後產出 phase1~4 全部工件
- [ ] 自動輸出 `go_no_go_pack.md` 與 Gate 證據鏈
- [ ] 若中途阻塞，輸出可執行的停止原因與下一步建議（非靜默失敗）
- [ ] 任何 full run 前，all-phase dry-run 必須先達 `READY`

---

## 3. 建議實作順序（更新）

### 已完成
- Day 1~3：T1~T8（Phase 1 MVP）
- **T16A**（2026-04-10）：`--phase all --dry-run`、`run_full.yaml`、`run_state.readiness`、phase3／4 **minimal** 範例與驗證
- **T9**（phase2）：`--phase phase2`、schema、`run_phase2.yaml`
- **T10 部分**（2026-04-10 起）：plan bundle、`job_specs`、`phase2_plan_bundle`、**`phase2_runner_smoke`**、**可選** `--phase2-run-trainer-jobs`（每 job `trainer.trainer`）、**`phase2_job_metrics_harvest`**、可選 **`--phase2-run-per-job-backtests`** / **`--phase2-run-backtest-jobs`**
- **T11 部分**（2026-04-10 起）：`evaluate_phase2_gate`、`phase2_gate_report`、`phase2/phase2_gate_decision.md`、`phase2/track_*_results.md`（MVP uplift/std；真多窗與 **T11A** 仍待）

### 下一輪建議
- Sprint A0（3~5 天）：T8A~T8D（Phase 1 Autonomous 閉環）
- Sprint A（3~5 天）：T9 ✅、**T10 收尾**（跨窗矩陣、fail-fast、統一結果結構）+ **T10A/T10B**（白名單參數、能力矩陣落地）、**T11A**（科學有效性 Gate）
- Sprint B（3~5 天）：T12~T13（Phase 3）
- Sprint C（3~5 天）：T14~T15（Phase 4）
- Sprint D（2~3 天）：**T16A ✅**；**T16** 剩餘（`--phase all` 非 dry-run Gate 串接、resume 到 phase 級）
- Sprint E（2~4 天）：T17~T20（E2E autonomous 驗收與穩定性）

---

## 4. 全階段驗收清單（DoD）

### Phase 1（已完成）
- [x] 一條命令可完成 Phase 1 主流程
- [x] 可產出 phase1 工件與 Gate
- [x] `run_state` / `resume` / `dry-run` 可用

### Phase 2~4（待完成）
- [x] `--phase phase2` 可獨立執行（T9–T11 MVP）：config、preflight、`phase2_bundle`、`job_specs`、runner smoke、**可選**訓練／回測旗標、gate、`phase2/*.md`。**未完成**：YAML `overrides`→trainer、真多窗自動彙整、T10A/T10B/T11A、T10 列為未勾之 fail-fast／統一結果結構
- [ ] `--phase phase3|phase4` 可獨立執行
- [x] **`--phase all --dry-run`** 可跑 all-phase readiness（T16A；含 phase3／4 **minimal** schema）
- [ ] 每 phase 都有 config schema + preflight + collector + evaluator + report
- [ ] 每 phase 都有明確錯誤碼與可追蹤 logs
- [ ] `--phase all` 可 Gate-driven **長跑**串接，且可 resume（T16；dry-run 已部分支援 `--resume` 跳過 preflight，見 orchestrator 實作）
- [ ] 所有 phase 報告均含 run_id、window、model_version、evidence
- [ ] autonomous mode 可無人工介入連跑 72~120h（含中斷恢復）
- [ ] phase1 mid/final snapshot 由程式自動產生（不需手動補檔）

---

## 5. 開發注意事項（全階段）

- 範例組態（如 `orchestrator/config/run_phase2.yaml`）內 **`model_dir` 須為本機存在的目錄**、`state_db_path`／`prediction_log_db_path` 須為可開啟的 SQLite 檔；否則 preflight 失敗（路徑錯誤非 orchestrator bug）。
- 解讀 `phase2_bundle.json` 時必看 **`status`**：`plan_only` 僅代表實驗清單由 YAML 展開，**不代表** track 訓練已完成。
- Phase 2 預設在 `phase2_runner_smoke` 會跑 **`python -m trainer.trainer --help`**（冷啟動可能較慢）；受限環境可 **`--skip-phase2-trainer-smoke`**（仍會建立各 job 的 log 目錄）。
- 先以小矩陣/短窗 smoke，再擴到正式觀測窗
- 任何 phase 都要固定 run 契約，禁止中途漂移
- 大檔優先 parquet；避免一次載入全量資料到記憶體
- 明確資源上限（windows/trials/chunk size）避免筆電 OOM
- backtest 與報表引用路徑需與該 run 綁定，避免讀到舊 `backtest_metrics.json`
- Gate 一律要帶 evidence，避免黑箱結論
