# 一次性 Ad-hoc 執行方案與腳本實作計畫（Autonomous-first）

> 適用情境：已有訓練完成模型，但尚未開始 backtest / scorer / validator 蒐證。  
> 目標：以**單次調查 run**收齊證據，並落地腳本化執行。  
> 執行原則：**Autonomous 為預設，Ad-hoc/手動僅作 fallback**（除錯或緊急接手）。

---

## 1) 一次性執行方案（Autonomous 預設，原 EXECUTION_PLAN §8）

### 1.1 Run 定義（先固定，不可中途漂移）

請先建立一個 run 識別（例如 `phase1_adhoc_YYYYMMDD`），並固定：

- `model_version` / `model_dir`
- `STATE_DB_PATH`
- `PREDICTION_LOG_DB_PATH`
- 調查觀測窗（`start_ts`, `end_ts`, 時區統一 HKT）
- 主要契約：`precision@recall=1%`、censored 排除規則、validator 口徑

> 原則：run 期間不更換模型、不改 threshold 策略、不改標籤契約；避免結論不可比較。

### 1.2 執行順序（一次跑完，無人工介入）

1. **Dry-run 快檢（production 前 2~10 分鐘）**
  - 跑 orchestrator `--phase all --dry-run`，確認 phase1~4 config / 路徑 / DB / 相依 / 命令可啟動性。
  - 僅做 readiness 檢查，不產生正式結論。
2. **啟動 autonomous run（單一命令）**
  - 跑 `run_pipeline.py --phase all --mode autonomous --run-id ... --config ...`。
  - 由 orchestrator 接管長跑，不再要求人工分段執行。
3. **自動 preflight + 觀測啟動**
  - 自動驗證路徑與連線可用（model / state DB / prediction DB / ClickHouse）。
  - 自動啟動並監控 `scorer` / `validator` 子程序（健康檢查、重啟、回收）。
4. **自動 checkpoint 蒐證（mid/final）**
  - 依設定（例如 `t+6h`、`t+24h`、`end`）自動執行 `run_r1_r6_analysis.py --mode all`。
  - 自動產生 mid/final snapshots（不可互相覆寫）。
5. **自動終點採樣與回測**
  - 在終點自動產生 final R1/R6 payload。
  - 自動執行固定窗口 backtest，產出 run 綁定的 `backtest_metrics`。
6. **自動彙整工件與 Gate**
  - 自動生成 `phase1/` 六份工件與 Gate 結論。
  - 若流程中斷，可 `--resume` 從 checkpoint 接續。

### 1.3 輸出對應（Phase 1 工件 -> 證據來源）

- `phase1/status_history_crosscheck.md`
  - 來源：`STATUS.md` 歷史對照 + 本輪人工判定（沿用/重驗/已失效）
- `phase1/slice_performance_report.md`
  - 來源：`prediction_log` + `alerts` + `validation_results` 切片統計
- `phase1/label_noise_audit.md`
  - 來源：`run_r1_r6_analysis.py` payload（`n_censored_excluded`、`precision_at_recall_target`）+ 高分 FP 抽樣
- `phase1/point_in_time_parity_check.md`
  - 來源：scorer/validator 時戳與標籤成熟規則對照
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

### 1.7 Dry-run 指令與判讀（全流程）

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase all \
  --dry-run \
  --mode autonomous \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_full.yaml \
  --run-id <run_id>
```

- `READY`：可啟動 full run。
- `NOT_READY`：不得啟動 full run，先依 `blocking_reasons` 修復。
- 建議：每次變更 config、model_dir、window 或 DB 路徑後都重跑 dry-run。

---

## 2) 腳本化實作計畫（Implementation Plan）

### 2.1 目標與範圍（更新）

- 目標：將目前手動流程改為可重跑、可中斷續跑、可生成工件的 orchestrator，最終覆蓋 Phase 1~4，並達成單一命令 E2E。
- 範圍：
  - 已完成：Phase 1 MVP（`--phase phase1` + `--dry-run` + `--resume`）。
  - 下一步：先完成 Phase 1 Autonomous 閉環，再擴充 Phase 2~4（track runner、phase gate、go/no-go pack）。
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

- `python run_pipeline.py --phase phase1 --config config/run_phase1.yaml`
- `python run_pipeline.py --phase phase2 --config config/run_phase2.yaml`
- `python run_pipeline.py --phase phase3 --config config/run_phase3.yaml`
- `python run_pipeline.py --phase phase4 --config config/run_phase4.yaml`
- `python run_pipeline.py --phase all --config config/run_full.yaml`
- `python run_pipeline.py --phase all --mode autonomous --config config/run_full.yaml --run-id <run_id>`
- `python run_pipeline.py --phase phase1 --resume --run-id <run_id>`
- `python run_pipeline.py --phase phase1 --collect-only`
- `python run_pipeline.py --phase phase1 --dry-run --config config/run_phase1.yaml --run-id <run_id>`

### 2.3.1 最小 config schema 草稿（可直接做為實作起點）

> 原則：先求「可跑可追溯」，再加欄位；所有路徑預設用 repo root 相對路徑。  
> 注意：以下為規格草稿，需在 `config_loader.py` 實際落地 schema 驗證。

#### A) `run_phase2.yaml`（Track A/B/C）

```yaml
phase: phase2
run_id: "phase2_20260410"

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
      - exp_id: a_hard_negative_v1
        overrides:
          hard_negative_weight: 2.0
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

#### B) `run_phase3.yaml`（Winner route 加深）

```yaml
phase: phase3
run_id: "phase3_20260412"

upstream:
  phase2_run_id: "phase2_20260410"
  winner_track: track_a
  winner_exp_id: a_hard_negative_v1

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
  - 執行 A/B/C 路線（至少 baseline + candidate），彙整跨窗結果。
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

