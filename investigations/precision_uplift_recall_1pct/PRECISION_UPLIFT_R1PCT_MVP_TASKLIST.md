# MVP 開發任務清單（Phase 1 Orchestrator）

> 來源：`PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md` §2  
> 目標：在 2~3 天內交付可用 MVP（可跑、可產工件、可判 Gate）。

---

## 0. MVP 範圍（先鎖定）

- 只做 `--phase phase1`（不含 Phase 2~4 自動化）
- 支援：
  - config 載入與驗證
  - preflight 檢查
  - 呼叫既有流程（R1/R6 + backtest）
  - 收集輸出並渲染 `phase1/*.md`
  - Gate 狀態判斷（`PASS / PRELIMINARY / FAIL`）
  - `run_state.json` 落地（供續跑）

不在 MVP 內：

- 長時間 daemon 管理（scorer/validator 完整生命週期管理）
- 多 phase DAG
- 複雜重試策略與並行排程

---

## 1. 任務分解（可直接開工）

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
- 可執行 `python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py --phase phase1 --config ...`
- 未提供必要參數時，錯誤訊息可讀且 exit code 非 0

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

**完成定義**
- config 不合法時可 fail-fast
- preflight 成功/失敗都會寫進狀態檔

### T3 - 流程執行器（高優先）

- [x] `runner.py` 封裝命令執行（subprocess）
- [x] MVP 先接兩個既有命令：
  - [x] `run_r1_r6_analysis.py --mode all --pretty`
  - [x] `python -m trainer.backtester ...`
- [x] 支援落地 stdout/stderr 到 run 目錄
- [x] 失敗映射錯誤碼：
  - [x] `E_NO_DATA_WINDOW`
  - [x] `E_EMPTY_SAMPLE`
  - [x] `E_ARTIFACT_MISSING`

**完成定義**
- 兩個命令都能被 orchestrator 呼叫
- 任一命令失敗可回傳可追蹤錯誤碼與訊息

### T4 - Collector 與中繼資料（高優先）

- [x] `collectors.py` 讀取：
  - [x] `trainer/out_backtest/backtest_metrics.json`
  - [x] R1/R6 JSON payload（中途 + 最終）
  - [x] `state.db` 內基本統計（finalized alerts、TP）
- [x] 輸出統一 dict（供 report_builder/evaluator 共用）

**完成定義**
- 缺檔案時明確回報（非 silent fail）
- 中繼資料含 Gate 判斷必要欄位

### T5 - Gate Evaluator（高優先）

- [x] `evaluators.py` 實作：
  - [x] `PRELIMINARY`：達 48h 但未達建議樣本量
  - [x] `PASS`：達建議門檻且方向一致
  - [x] `FAIL`：資料缺失/口徑衝突/明確不達標
- [x] 產出：
  - [x] `status`
  - [x] `blocking_reasons[]`
  - [x] `evidence_summary`

**完成定義**
- 同一輸入可重現同一判定
- 判定結果可直接寫入 `phase1_gate_decision.md`

### T6 - 報表渲染（高優先）

- [x] `report_builder.py` 寫入（或更新）：
  - [x] `phase1/upper_bound_repro.md`
  - [x] `phase1/label_noise_audit.md`
  - [x] `phase1/slice_performance_report.md`
  - [x] `phase1/point_in_time_parity_check.md`
  - [x] `phase1/phase1_gate_decision.md`
- [x] `status_history_crosscheck.md` 在 MVP 可先保留人工維護，僅附提醒段落

**完成定義**
- 單次跑完可看到上述檔案有非模板內容
- 檔案中有 run_id、時間窗、資料來源註記

### T7 - run_state 與 Resume（中優先，MVP 末段）

- [x] 每步落地：`orchestrator/state/<run_id>/run_state.json`
- [x] 記錄：
  - [x] step 狀態（pending/running/success/failed）
  - [x] 輸入參數摘要
  - [x] 產物路徑
  - [x] 錯誤碼/訊息
- [x] `--resume` 會跳過已成功步驟

**完成定義**
- 中途失敗後可續跑，不重跑成功步驟

### T8 - Dry-run 上線前快檢（中優先，生產前必做）

- [x] `run_pipeline.py` 新增 `--dry-run`
- [x] dry-run 模式會執行：
  - [x] config 載入 + schema 驗證
  - [x] preflight（路徑 / DB / 必要表）
  - [x] R1/R6 可啟動性快檢（不做長跑）
  - [x] backtest 可啟動性快檢（不做長跑）
  - [x] 輸出路徑可寫入性檢查（state/log/report）
- [x] dry-run 模式不做：
  - [x] 正式 Gate 結論覆寫（僅輸出 readiness）
  - [x] 長時間資料掃描 / 重型步驟
- [x] `run_state.json` 新增 dry-run 摘要：
  - [x] `mode: dry_run`
  - [x] `readiness.status: READY / NOT_READY`
  - [x] `readiness.checks[]`（每項 pass/fail 與錯誤訊息）

**完成定義**
- production 環境可在 10 分鐘內完成 dry-run
- 失敗可定位到具體資源（路徑/DB/表/命令）
- dry-run 通過後可明確判定是否可進入正式 investigation 長跑

---

## 2. 建議實作順序（Day 1~3）

### Day 1
- T1 + T2（骨架、CLI、config、preflight）

### Day 2
- T3 + T4（命令執行、資料收集）

### Day 3
- T5 + T6 + T7 + T8（Gate、報表、resume、dry-run）

---

## 3. 驗收清單（DoD）

- [ ] 一條命令可完成 Phase 1 MVP 主流程
- [ ] 能產出至少 5 份 phase1 工件（`status_history_crosscheck.md` 可人工）
- [ ] Gate 有明確 `PASS / PRELIMINARY / FAIL`
- [ ] 有 `run_state.json`，且 `--resume` 可用
- [ ] 失敗時可定位（錯誤碼 + stdout/stderr 檔案）
- [ ] 有 `--dry-run`，且能在 production 上快速給出 READY / NOT_READY

---

## 4. 開發注意事項（避免踩雷）

- 先以小樣本/短窗驗證流程，再拉長觀測時間
- 預設保守參數（`sample_size`、`player_chunk_size`）
- 大檔優先 parquet，不要一次載全量到記憶體
- 任何判定都要帶 evidence，避免黑箱 Gate
