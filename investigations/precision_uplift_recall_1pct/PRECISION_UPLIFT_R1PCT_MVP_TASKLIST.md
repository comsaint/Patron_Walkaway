# Orchestrator 開發任務清單（Phase 1~4）

> 來源：`PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md` §2  
> 目標：先完成 Phase 1 MVP，並以同一框架擴充到 Phase 2~4 與 `--phase all` 無人值守自動化。

---

## 0. 範圍與版本策略

### 0.1 已交付（MVP）

- [x] 只做 `--phase phase1` 的可用 MVP
- [x] 支援 config 載入/驗證、preflight、R1/R6 + backtest 執行
- [x] 支援 collect、Gate 判斷、`phase1/*.md` 報告輸出
- [x] 支援 `run_state.json`、`--resume`、`--dry-run`

### 0.2 待擴充（V2 / V3 / V4 / V5）

- [ ] `--phase phase2` 自動化（Track A/B/C runner + Phase 2 Gate）
- [ ] `--phase phase3` 自動化（勝者路線加深 + Phase 3 Gate）
- [ ] `--phase phase4` 自動化（freeze/multi-window/impact/go-no-go）
- [ ] `--phase all` 多 phase 串接（依 Gate 決定是否進下一階段）
- [ ] Autonomous supervisor（長跑狀態機、checkpoint、故障自復）
- [ ] 中途/期末 snapshot 全自動（不需手動落 `r1_r6_mid.stdout.log`）
- [ ] scorer/validator 生命週期全自動（啟動、健康檢查、重啟、回收）

### 0.3 明確不在近期範圍（先不做）

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
  - [x] `phase1/point_in_time_parity_check.md`
  - [x] `phase1/phase1_gate_decision.md`
- [x] `status_history_crosscheck.md` 保留人工維護，附 orchestrator 區塊

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

- [ ] runner 支援 A/B/C 路線批次執行（至少每路線 1 組 baseline + 1 組候選）
- [ ] 產出統一結果結構（每 track 的主指標、切片、跨窗統計）
- [ ] collector 讀取 track 產物並彙整為 `phase2_bundle.json`
- [ ] fail-fast 保護：
  - [ ] 任一路線輸出缺檔 → `E_ARTIFACT_MISSING`
  - [ ] 窗口無資料 → `E_NO_DATA_WINDOW`

**完成定義**
- [ ] `phase2_bundle.json` 可重現同一輸入下同一輸出
- [ ] stdout/stderr 與每路線 artifacts 可追溯

#### T11 - Phase 2 Evaluator + 報表

- [ ] `evaluators.py` 新增 `evaluate_phase2_gate(...)`
- [ ] Gate 規則最小版：
  - [ ] 至少 1 條路線相對基線達成 uplift（預設 +3~5pp 可配置）
  - [ ] 跨窗波動未超門檻（例如 std/tolerance 可配置）
- [ ] `report_builder.py` 新增輸出：
  - [ ] `phase2/track_a_results.md`
  - [ ] `phase2/track_b_results.md`
  - [ ] `phase2/track_c_results.md`
  - [ ] `phase2/phase2_gate_decision.md`

**完成定義**
- [ ] `phase2_gate_decision.md` 含勝者路線、淘汰理由、evidence

---

### V3 - Phase 3 自動化（勝者路線加深）

#### T12 - Phase 3 Config + Runner

- [ ] `run_pipeline.py` 支援 `--phase phase3`
- [ ] 新增 `orchestrator/config/run_phase3.yaml`
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
- [ ] 新增 `orchestrator/config/run_phase4.yaml`
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

- [ ] `run_pipeline.py` 支援 `--phase all`
- [ ] 預設順序：phase1 -> phase2 -> phase3 -> phase4
- [ ] Gate 未達時：
  - [ ] 預設 stop（可選 `--force-next`，但需高風險警告）
- [ ] 每 phase 寫入 `run_state` 子節點，支援 resume 到 phase 級別

#### T16A - All-phase dry-run（上線前必做）

- [ ] `run_pipeline.py` 支援 `--phase all --dry-run`
- [ ] dry-run 覆蓋 phase1~4 config schema、phase dependency、路徑可寫、CLI smoke
- [ ] dry-run 輸出統一 `READY / NOT_READY` 與 blocking reasons
- [ ] `run_state` 寫入 all-phase readiness 摘要（供 CI / 人工審核）
- [ ] `run_full.yaml` 定義 checklist 欄位：
  - [ ] `validate_phase_configs_exist`
  - [ ] `validate_phase_schemas`
  - [ ] `validate_phase_dependencies`
  - [ ] `validate_contract_consistency`
  - [ ] `validate_paths_readable`
  - [ ] `validate_writable_targets`
  - [ ] `validate_cli_smoke_per_phase`
  - [ ] `validate_resource_limits`
  - [ ] `fail_on_any_check`

**完成定義**
- [ ] 一條命令可完整跑完 all-phase（或在 gate block 點可恢復）
- [ ] full run 前可先執行 all-phase dry-run，並明確得知是否可安全啟動長跑

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

### 下一輪建議
- Sprint A0（3~5 天）：T8A~T8D（Phase 1 Autonomous 閉環）
- Sprint A（3~5 天）：T9~T11（Phase 2）
- Sprint B（3~5 天）：T12~T13（Phase 3）
- Sprint C（3~5 天）：T14~T15（Phase 4）
- Sprint D（2~3 天）：T16 + T16A（`--phase all` Gate 串接 + all-phase dry-run）
- Sprint E（2~4 天）：T17~T20（E2E autonomous 驗收與穩定性）

---

## 4. 全階段驗收清單（DoD）

### Phase 1（已完成）
- [x] 一條命令可完成 Phase 1 主流程
- [x] 可產出 phase1 工件與 Gate
- [x] `run_state` / `resume` / `dry-run` 可用

### Phase 2~4（待完成）
- [x] `--phase phase2` 可獨立執行（T9：config 驗證 + preflight + `run_state` + `phase2_scaffold`；T10+ track runner／bundle／報表待補）
- [ ] `--phase phase3|phase4` 可獨立執行
- [ ] 每 phase 都有 config schema + preflight + collector + evaluator + report
- [ ] 每 phase 都有明確錯誤碼與可追蹤 logs
- [ ] `--phase all` 可 Gate-driven 串接，且可 resume
- [ ] 所有 phase 報告均含 run_id、window、model_version、evidence
- [ ] autonomous mode 可無人工介入連跑 72~120h（含中斷恢復）
- [ ] phase1 mid/final snapshot 由程式自動產生（不需手動補檔）

---

## 5. 開發注意事項（全階段）

- 先以小矩陣/短窗 smoke，再擴到正式觀測窗
- 任何 phase 都要固定 run 契約，禁止中途漂移
- 大檔優先 parquet；避免一次載入全量資料到記憶體
- 明確資源上限（windows/trials/chunk size）避免筆電 OOM
- backtest 與報表引用路徑需與該 run 綁定，避免讀到舊 `backtest_metrics.json`
- Gate 一律要帶 evidence，避免黑箱結論
