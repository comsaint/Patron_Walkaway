# Code Review: ClickHouse 臨時表方案 (load_player_profile)

> **歸檔**：較舊段落已移至 [.cursor/plans/archive/STATUS_archive.md](.cursor/plans/archive/STATUS_archive.md)。

---

## 2026-04-20 — DEC-040：僅載入 `model.pkl`；廢止 walkaway 產出與 legacy 備援

**決策紀錄**：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md) **DEC-040**。

### 變更摘要

- **`trainer/serving/scorer.py`**、**`trainer/training/backtester.py`**：`load_dual_artifacts` 只讀 **`model.pkl`**；缺檔即 `FileNotFoundError`（若目錄內仅有 `rated_model.pkl`／`walkaway_model.pkl`，錯誤訊息會註明 legacy 檔未被載入）。
- **`trainer/training/trainer.py`**：不再寫入 `walkaway_model.pkl`；`run_pipeline` 收尾刪除殘留 **`walkaway_model.pkl`**（與 `nonrated_model.pkl`／`rated_model.pkl` 一併清理）。
- **`package/build_deploy_package.py`**：建包僅接受來源含 **`model.pkl`**；`BUNDLE_FILES` 不再列 legacy pkl。
- **測試**：`tests/integration/test_trainer.py` 改為斷言 `save_artifact_bundle` 不含 walkaway 寫入；`tests/review_risks/test_review_risks_round360.py` 斷言 `run_pipeline` 含 `walkaway_model.pkl` 清理。
- **文件**：`README.md`、`package/*`、`doc/` 內與 fallback／walkaway 相關敘述已對齊 DEC-040。

### 建議驗證

```bash
python -m pytest tests/integration/test_trainer.py tests/review_risks/test_review_risks_round360.py tests/integration/test_scorer.py -q --tb=short
```

---

## 2026-04-10 CYCLE — Phase 2：`precision_at_recall_1pct_by_window`（plan bundle）+ PAT 序列合併行為（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`.cursor/plans/PLAN_precision_uplift_sprint.md`](.cursor/plans/PLAN_precision_uplift_sprint.md)、[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md)；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`orchestrator/config_loader.py`**：實驗層可選 **`precision_at_recall_1pct_by_window`**（非空 list、元素可轉 **float**）。
- **`orchestrator/collectors.py`**：**`build_phase2_pat_series_from_plan_tracks`**；**`collect_phase2_plan_bundle`** 將上述欄位寫入 **`tracks` 快照**並組出 **`phase2_pat_series_by_experiment`**（若有資料）；**`merge_phase2_pat_series_from_shared_and_per_job`** 改為 **deepcopy 合併**，且 **僅在該 (track, exp_id) 尚無非空 list 時** 填入兩點 bridge（避免覆寫 YAML 單點／手寫序列）。
- **`orchestrator/config/run_phase2.yaml`**：註解範例 **`precision_at_recall_1pct_by_window`**。

#### 手動驗證

- 在 phase2 YAML 某實驗下加入 **`precision_at_recall_1pct_by_window: [0.5, 0.51]`**，跑 **`collect_phase2_plan_bundle`**（或 **`run_pipeline --phase phase2`** 產出 plan bundle），確認 bundle 含 **`phase2_pat_series_by_experiment`**。
- 情境：bundle 已有單點 YAML **`c0`**、per-job 結果含 **`c0`+`c1`** → 預期 **`c0` 不變**、**`c1`** 得 **`[shared, preview]`**。

#### 下一步建議

- **T10** 完整 A/B/C 與 **`E_ARTIFACT_MISSING`**／**`E_NO_DATA_WINDOW`** fail-fast；真實多窗矩陣若來自 backtest 產物，再接到同一 **`phase2_pat_series_by_experiment`** 形狀。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 型別非 list 的 YAML | 驗證只檢「有設定時」為 list | 維持現有 **`ConfigValidationError`** | **`test_phase2_config_precision_at_recall_by_window_empty_raises`** |
| 合併略過「髒」既有值 | **`cur[eid]`** 非 list 但 truthy → 視為佔位不覆寫 | 長期可正規化為 list 或清掉鍵 | 可選髒 bundle |
| 整數／字串數字 | **`float(x)`** 與 JSON 往返 | 文件標註 0–1 比例 | **`test_collect_phase2_plan_bundle_propagates_precision_at_recall_by_window`** |
| 僅補新 exp、舊 exp 永遠不補兩點 | 若 YAML 誤留單點且希望被 bridge 取代 | 需明確「清空鍵」或設定旗標 | **`test_merge_phase2_pat_series_preserves_nonempty_yaml_fills_other_exp`** |

### STEP 3 — Tester（僅 tests）

- **`test_phase2_config_precision_at_recall_by_window_*`**、**`test_collect_phase2_plan_bundle_propagates_precision_at_recall_by_window`**、**`test_build_phase2_pat_series_from_plan_tracks_coerces_numeric`**、**`test_merge_phase2_pat_series_preserves_nonempty_yaml_fills_other_exp`**、**`test_merge_phase2_pat_series_noop_when_only_nonempty_yaml_matches_results`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **129 passed**（同上 pytest）。
- **計畫下一步**：T10 矩陣與 fail-fast；若多窗序列來自 artifact pipeline，與 **`merge_*`** 的優先順序寫入 Implementation Plan／sprint 對照表。

---

## 2026-04-10 CYCLE — 文件：Phase 2 Gate（sprint ↔ orchestrator）對照 + Implementation Plan 同步（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`.cursor/plans/PLAN_precision_uplift_sprint.md`](.cursor/plans/PLAN_precision_uplift_sprint.md)、[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md)；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`.cursor/plans/PLAN_precision_uplift_sprint.md`**：在 **Phase 2 Gate** 段落下新增 **「調查 repo 對照」** 小表（uplift／std／產物與 exit 9／10），並鏈結 **Implementation Plan T10／T11** 與 **`evaluate_phase2_gate`**。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 per-job 回測改為 **`--output-dir`** + **`phase2_per_job_backtest_metrics_repo_relative`**；T11 Gate／**`report_builder`** 條目改為已勾選之 **MVP** 敘述並對齊現有 md 小節。
- **`orchestrator/config/run_phase2.yaml`**：**`gate:`** 區塊補註解（對齊 sprint、**`evaluate_phase2_gate`**、欄位語意）。

#### 手動驗證

- 開啟 **`.cursor/plans/PLAN_precision_uplift_sprint.md`** Phase 2 區塊，確認對照表與連結可讀。
- 通讀 **Implementation Plan** T10／T11 與 **`run_phase2.yaml`** gate 註解是否與程式一致。

#### 下一步建議

- **真多窗** `phase2_pat_series_by_experiment` 資料鏈（取代／補強兩點 bridge）；T10「完整 A/B/C」與 fail-fast 細項。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 連結相對路徑 | sprint 內 markdown 鏈結依 repo 佈局 | 搬檔時同步更新 | 下表單測掃描關鍵字 |
| 文字與程式漂移 | 門檻敘述變更未回寫 sprint | 改 gate 預設時跑契約測 | **`test_run_phase2_example_yaml_*`** |
| Implementation Plan 過長 | 讀者漏看 per-job 路徑修正 | 維持 T10 單條為 SSOT | — |

### STEP 3 — Tester（僅 tests）

- **`test_run_phase2_example_yaml_documents_phase2_gate_contract`**、**`test_plan_precision_uplift_sprint_phase2_gate_orchestrator_bridge`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **123 passed**（同上 pytest）；本輪無需改 production 程式邏輯。
- **計畫下一步**：多窗 PAT 序列 collector／runner；T10 完整矩陣與 **`E_ARTIFACT_MISSING`**／**`E_NO_DATA_WINDOW`**。

---

## 2026-04-10 CYCLE — T11：`track_*_results.md` 新增 PAT@1% 序列／std gate 小節（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md)；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`report_builder.py`**：**`_format_phase2_pat_series_values`**、**`_phase2_std_and_pat_series_markdown_for_track`**；**`write_phase2_track_results`** 在 **Uplift** 與 **Metrics (shared backtest)** 之間插入 **`## PAT@1% series & std (gate)`**（bundle 該軌 **`phase2_pat_series_by_experiment`** + **`gate['metrics']`** 之 std／**`phase2_std_per_series`** 篩軌）。
- 移除檔尾誤植註解行（**`# Fix typo: _ORNS...`**）。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t11_trackstd --skip-backtest-smoke \
  --skip-phase2-trainer-smoke
# 檢查 phase2/track_{a,b,c}_results.md 是否含「PAT@1% series & std (gate)」小節
```

#### 下一步建議

- 真多窗 **`phase2_pat_series_by_experiment`** 資料源（取代／補強兩點 MVP）；**`PLAN_precision_uplift_sprint.md`** Phase 2 Gate 實測敘述對齊。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 長序列 md 膨脹 | 實驗多、窗多時列表很長 | 已截斷 **max_elems=8**；之後可改附檔或連結 | **`test_write_phase2_track_results_std_section_*`** |
| gate-wide max 重複 | 三份 track md 都印全域 max pp | 維持自洽；長期可只寫入 `phase2_gate_decision.md` | — |
| 缺 metrics 鍵 | 舊 gate 結果無 std | 顯示「未評估」句 | plan_only 路徑 |
| `float('nan')` 格式化 | 異常數值 | 維持 try/float；失敗回退 `str(x)` | 可選髒資料列 |

### STEP 3 — Tester（僅 tests）

- **`test_write_phase2_track_results_std_section_shows_evaluated_metrics`**；**`test_write_phase2_track_results_writes_three_files`**／**`test_write_phase2_track_results_per_job_backtest_section`** 斷言新標題。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **121 passed**（同上 pytest）。
- **計畫下一步**：多窗矩陣 collector；sprint Phase 2 **Go/證據** 與現有 gate 欄位對照表。

---

## 2026-04-10 CYCLE — T10/T11：per-job `backtest_metrics` 獨立輸出路徑（`--output-dir`）（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md)；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`trainer/training/backtester.py`**：**`backtest(..., output_dir=...)`**；寫入 metrics／parquet／csv 前 **`out_root.mkdir(parents=True, exist_ok=True)`**；CLI **`--output-dir`**。
- **`orchestrator/runner.py`**：**`run_phase1_backtest`** 若 **`cfg["backtest_output_dir"]`** 則附加 **`--output-dir`**（相對路徑對 **repo 根** 解析）；**`run_phase2_per_job_backtests`** 對每 job 設 **`backtest_output_dir`** 為該 job 之 **`_per_job_backtest`** log 目錄，並自 **`collectors.phase2_per_job_backtest_metrics_repo_relative`** 讀取 JSON（不再用共用 **`phase2_backtest_metrics_repo_relative`**）。
- **`collectors.py`**：**`phase2_per_job_backtest_metrics_repo_relative(run_id, track, exp_id)`**。
- **`report_builder.py`**、**`orchestrator/config/run_phase2.yaml`**：更新 per-job 與共用路徑之說明。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_pjb_out --skip-backtest-smoke \
  --skip-phase2-trainer-smoke --phase2-run-per-job-backtests
# 預期：每 job 的 .../_per_job_backtest/backtest_metrics.json 存在且互不覆寫；bundle per_job 列 metrics_repo_relative 指向該檔
```

#### 下一步建議

- **report_builder**：可選 **std／`phase2_pat_series_by_experiment`** 摘要列；真多窗矩陣；Phase 1 若需自訂輸出亦可傳 **`backtest_output_dir`**（進階）。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 磁碟用量 | 每 job 另存 parquet／csv | 文件註明；之後可加「僅 metrics」模式 | — |
| 權限 | `--output-dir` 不可寫 | backtester 既有的 mkdir／write 會丟錯 | 既有 subprocess 失敗路徑 |
| 相對路徑基準 | 與 orchestrator cwd=repo 根一致 | 維持 **`run_phase1_backtest`** 解析方式 | **`test_run_phase1_backtest_includes_output_dir_argv`** |
| CLI 相容 | 舊版 trainer 無 `--output-dir` | 僅 per-job 路徑需新版 backtester | 升級說明可寫 runbook |

### STEP 3 — Tester（僅 tests）

- **`test_phase2_per_job_backtest_metrics_repo_relative`**、**`test_run_phase1_backtest_includes_output_dir_argv`**；擴充 **`test_run_phase2_per_job_backtests_resolves_model_dir_and_preview`**（**`backtest_output_dir`**、**`metrics_repo_relative`**、**`load_json_under_repo`** 路徑）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **120 passed**（同上 pytest）。
- **計畫下一步**：track md **std／序列** 小節；真多窗 **`phase2_pat_series_by_experiment`**；**`.cursor/plans/PLAN_precision_uplift_sprint.md`** Phase 2 Gate 實測對齊。

---

## 2026-04-10 CYCLE — T11：collector 自動 merge `phase2_pat_series_by_experiment`（shared + per-job 兩點 MVP）（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T11**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`collectors.py`**：**`phase2_pat_series_mapping_has_evaluable_series`**；**`merge_phase2_pat_series_from_shared_and_per_job`**（在尚無「可評估」手寫序列時，對每個 **ok** 的 per-job 列組 **`[shared PAT@1%, shared_precision_at_recall_1pct_preview]`**；**MVP bridge**，非真多窗矩陣）；**`collect_summary_phase2_plan_for_run_state`** 可選 **`phase2_pat_series_auto_merge_skipped`**／**`phase2_pat_series_auto_merge_eligible`**。
- **`run_pipeline.py`**：**`phase2_gate_report`** 在 **`evaluate_phase2_gate`** 前呼叫 merge；若變更 bundle 則寫回 **`phase2_bundle.json`** 並刷新 **`phase2_collect`**／**`run_state`**。
- **`orchestrator/config/run_phase2.yaml`**：註解自動 merge 與跳過條件（任一 **`track_*`** 下列長度 ≥2 則不覆寫）。

#### 手動驗證

```bash
# 需：metrics_ingested（共享 backtest_metrics）+ 曾跑 --phase2-run-per-job-backtests 且結果 ok
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t11_autoser --skip-backtest-smoke \
  --skip-phase2-trainer-smoke --phase2-run-per-job-backtests --phase2-run-backtest-jobs
# gate 前檢查 phase2_bundle.json 是否出現 phase2_pat_series_by_experiment（兩點列表）
```

#### 下一步建議

- **每 job 獨立 `backtest_metrics` 路徑**（避免共用檔互蓋）；**report_builder** track md 顯示 std／序列摘要；真多窗矩陣進 bundle 後可關閉或縮小兩點 proxy 依賴。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 兩點 stdev 語意 | `[shared, preview]` 大差異 → std 易超標（像一致性檢查而非時序穩定） | YAML／runbook 註明；真多窗就緒後改資料來源 | 門檻放寬之整合測 |
| 手寫單點序列 | 僅 `[x]` 不視為「已手動提供」，merge 會整包重寫 | 文件註明；可選合併策略 | 現以 len≥2 為「保留」門檻 |
| 僅 resume gate | 舊 bundle 無 merge 欄位時，本輪 gate 前會補寫 | 維持現狀 | gate 前 merge 管線測 |
| `collectors`→`evaluators` 依賴 | 新增 import | 之後可抽共用 PAT 讀取常數 | — |

### STEP 3 — Tester（僅 tests）

- **`test_phase2_pat_series_mapping_has_evaluable_series`**、**`test_merge_phase2_pat_series_*`**、**`test_evaluate_phase2_gate_after_auto_merge_std_evaluated`**、**`test_collect_summary_phase2_pat_series_merge_hints`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- 調整 **`test_evaluate_phase2_gate_after_auto_merge_std_evaluated`** 之 **`max_std_pp_across_windows`**（兩點序列在 preview 遠離 shared 時 stdev 大，此測僅鎖定「merge 後會跑 std」路徑）。
- **118 passed**（同上 pytest）。
- **計畫下一步**：per-job **backtest_metrics** 獨立路徑；**report_builder** 可選 std／序列；**`.cursor/plans/PLAN_precision_uplift_sprint.md`** Phase 2 真多窗證據鏈。

---

## 2026-04-10 CYCLE — T11：Std gate（`phase2_pat_series_by_experiment`）+ `--phase2-fail-on-gate-blocked`（exit 10）（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T11**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`evaluators.py`**：**`import statistics`**；**`_phase2_apply_std_gate`**：讀 **`bundle["phase2_pat_series_by_experiment"]`**（`track_* → { exp_id: [PAT@1% 每窗…] }`）；長度 ≥2 之數值列算 **`statistics.stdev`×100**（pp），取跨序列 **max**，與 **`gate.max_std_pp_across_windows`**（預設 2.5）比較；**uplift 已 PASS** 且 **max_pp > limit** → **FAIL**、**`blocking_reasons`** 含 **`phase2_std_exceeds_max_pp_across_windows`**；uplift 非 PASS 時僅寫入 std **metrics**／**informational evidence**，不因 std 單獨升級 FAIL。**`evaluate_phase2_gate`** 在 **`metrics_ingested`** 且跑 uplift 時：先 **`_phase2_try_uplift_gate_from_per_job`** 再 **`_phase2_apply_std_gate`**；evidence 開頭註明可選 **`phase2_pat_series_by_experiment`**。
- **`run_pipeline.py`**：**`phase2_gate_cli_exit_code`** 新增 **`fail_on_gate_blocked: bool = False`**；**FAIL + fail_on_gate_fail → 9**；**BLOCKED + fail_on_gate_blocked → 10**；兩旗標皆開時 **先判 FAIL（9）**；CLI **`--phase2-fail-on-gate-blocked`**；**`phase2_gate_report`** 非零 exit 時依 **`gate_p2["status"]`** 設 **`E_PHASE2_GATE_FAIL`** 或 **`E_PHASE2_GATE_BLOCKED`**，stderr 區分 FAIL／BLOCKED。
- **`orchestrator/config/run_phase2.yaml`**、**`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：exit 10、bundle 欄位與 **`max_std_pp_across_windows`** 語意；std 子項標為已落地（collector 自動灌入可再強化）。

#### 手動驗證

```bash
# BLOCKED + 可選非零 exit（需 bundle gate 為 BLOCKED）
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t11_blocked --skip-backtest-smoke \
  --skip-phase2-trainer-smoke --phase2-fail-on-gate-blocked
# 預期：gate BLOCKED 時 exit 10（與 --phase2-fail-on-gate-fail 並用時 FAIL 仍優先 9）
```

#### 下一步建議

- **Collector／runner** 自動填入 **`phase2_pat_series_by_experiment`**；**每 job 獨立 `backtest_metrics` 路徑**；**`report_builder`** 在 track md 顯示 std（可選）。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 僅 2 點 stdev | 兩窗變異度估計粗 | 文件註明；之後可改 rolling／IQR | 低／高波動序列單測 |
| 非 `track_*` 鍵 | 若未過濾可能誤入 max | 僅掃 `track_` 前綴或明列 tracks | 可選：非 track 鍵忽略 |
| 空 `{}` 誤判 | 先前空 mapping 曾走「有欄位但無有效序列」分支 | **已修**：`not series_root` 時等同未提供、直接 return uplift | 依賴既有 uplift-only 路徑 |
| exit 10 與 9 並存 | CI 需明確政策 | runbook／yaml 註解 | 雙旗標優先順序單測 |

### STEP 3 — Tester（僅 tests）

- **`test_phase2_gate_cli_exit_code_blocked_exit_10`**、**`test_phase2_gate_cli_fail_precedes_blocked_when_both_flags`**、**`test_evaluate_phase2_gate_std_pass_with_low_variance_series`**、**`test_evaluate_phase2_gate_std_fail_when_series_too_volatile`**、**`test_evaluate_phase2_gate_std_informational_when_uplift_fail`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **`evaluators._phase2_apply_std_gate`**：**`phase2_pat_series_by_experiment` 為空 mapping（`{}`）時視為未提供**，不覆寫 uplift 結果（避免錯誤的「present but invalid」訊息）。
- **112 passed**（同上 pytest）。
- **計畫下一步**：collector 自動灌入 **`phase2_pat_series_by_experiment`**；per-job **backtest_metrics** 獨立路徑；track md 可選 std 欄；對齊 **`.cursor/plans/PLAN_precision_uplift_sprint.md`** Phase 2 Gate 其餘觀測項。

---

## 2026-04-10 CYCLE — T11：`--phase2-fail-on-gate-fail`（exit 9／resume 友善）（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`.cursor/plans/PLAN_precision_uplift_sprint.md`](.cursor/plans/PLAN_precision_uplift_sprint.md)；[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T11**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`run_pipeline.py`**：**`phase2_gate_cli_exit_code`**（**`fail_on_gate_fail`** 且 gate **`FAIL`** → **9**）；CLI **`--phase2-fail-on-gate-fail`**；**`phase2_gate_report`** 在該情境下 **failed** + **`E_PHASE2_GATE_FAIL`**（報表仍寫入）；**`return` 前**補寫 **`merged["artifacts"]`** 與 **`_write_run_state`**；**BLOCKED**／**PASS** 不觸發 exit 9。
- **`orchestrator/config/run_phase2.yaml`**、**`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：註解／條目。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t11_gatefail --skip-backtest-smoke \
  --skip-phase2-trainer-smoke --phase2-fail-on-gate-fail
# 若 gate 為 FAIL：exit 9、run_state.steps.phase2_gate_report.status=failed；修正 bundle 後 --resume 可重跑 gate
```

#### 下一步建議

- **Std gate**（多窗 bundle）；**每 job 獨立 backtest_metrics 路徑**。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 僅 FAIL 觸發 | BLOCKED 在 CI 仍 exit 0 | 文件註明；可另加 `--phase2-fail-on-gate-blocked` | — |
| stderr 冗長 | 整段 evidence 印出 | 之後改只印 blocking 摘要 | — |
| 雙重 `_write_run_state` | gate 區塊先寫後再寫（含 artifacts） | 之後可合併為單一 finalize | — |
| exit 9 與他碼混淆 | 7／8 已用於 trainer／backtest | 在 runbook 列對照表 | helper 單測 |

### STEP 3 — Tester（僅 tests）

- **`test_phase2_gate_cli_exit_code_when_disabled`**、**`test_phase2_gate_cli_exit_code_on_fail_enabled`**、**`test_phase2_gate_cli_exit_code_pass_and_blocked_unchanged`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **107 passed**（同上 pytest）。
- **計畫下一步**：**std gate**；可選 **`--phase2-fail-on-gate-blocked`**；**per-job backtest_metrics** 獨立路徑。

---

## 2026-04-10 CYCLE — T11：uplift gate（per-job 預覽 vs YAML baseline）（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`.cursor/plans/PLAN_precision_uplift_sprint.md`](.cursor/plans/PLAN_precision_uplift_sprint.md)；[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T11**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`evaluators.py`**：**`_parse_float_gate`**、**`_phase2_preview_map_from_bundle`**、**`_phase2_try_uplift_gate_from_per_job`**；**`metrics_ingested`** 時若 **`per_job_backtest_jobs.executed`**：改寫 evidence 開頭、合併 **`phase2_uplift_*`**／**`phase2_std_gate_*`** 指標；**`PASS`**／**`FAIL`**（`phase2_uplift_below_min_pp_vs_baseline`）／**`BLOCKED`**（`phase2_uplift_insufficient_comparisons`）；未執行 per-job 時維持 **`phase2_shared_metrics_no_per_track_uplift`**。
- **`report_builder.py`**：**`_phase2_uplift_rows_markdown_for_track`**；**`write_phase2_track_results`** 新增 **`## Uplift vs baseline (gate)`**。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T11 uplift 子項勾選；std 仍待多窗。

#### 手動驗證

```bash
# phase2 bundle：metrics_ingested + per_job_backtest_jobs.executed + tracks 與預覽列齊後跑 gate 報表
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py --phase phase2 ... 
# 檢查 phase2_gate_decision.md 的 status／blocking；track_*_results.md 的 Uplift 小節
```

#### 下一步建議

- **Std gate**：bundle 契約（多窗 PAT 序列）+ **`max_std_pp_across_windows`** 實評；可選 **pipeline 在 gate FAIL 時非零 exit**（產品政策）。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| baseline 語意 | 第一個「有預覽」非必為實驗 YAML 第一列 | 文件註明；之後可強制 exp_id 排序 | 已有 YAML 順序單測 |
| 多軌同時達標 | 任軌任一 challenger 達標即 PASS | 與 sprint「至少一條路線」一致 | PASS 單測 |
| `ok` 非嚴格 bool | 非 True 列不進 preview map | 維持 `is not True` | **`test_phase2_preview_map_excludes_failed_per_job_rows`** |
| std 未評估但 YAML 有門檻 | 易誤以為已把關 | metrics **`phase2_std_gate_note`** | — |

### STEP 3 — Tester（僅 tests）

- **`test_evaluate_phase2_gate_metrics_ingested_includes_per_job_preview_evidence`**（改為 **PASS** 情境）、**`test_evaluate_phase2_gate_metrics_ingested_uplift_blocked_single_preview`**、**`test_evaluate_phase2_gate_metrics_ingested_uplift_fail_below_min`**、**`test_phase2_preview_map_excludes_failed_per_job_rows`**；track md：**`test_write_phase2_track_results_per_job_backtest_section`**、**`test_write_phase2_track_results_writes_three_files`** 斷言 **Uplift** 標題。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **104 passed**（同上 pytest）。
- **計畫下一步**：**std gate** 與多窗 bundle；可選 **gate FAIL → exit code**；**每 job 獨立 backtest_metrics 路徑**。

---

## 2026-04-10 CYCLE — T11：`per_job_backtest` 預覽進 Gate／track md（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)、[`.cursor/plans/PLAN_precision_uplift_sprint.md`](.cursor/plans/PLAN_precision_uplift_sprint.md)；[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T11**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`evaluators.py`**：**`phase2_per_job_backtest_metrics`**（正規化 **`per_job_backtest_jobs.results`**、**`per_job_backtest_preview_count`**）；**`_phase2_per_job_backtest_evidence_suffix`**；**`evaluate_phase2_gate`** 在 **`plan_only`** 與 **`metrics_ingested`** 分支把上述併入 **`metrics`** 與 **`evidence_summary`**（**`per_job_backtest_jobs.executed`** 為真時）。
- **`report_builder.py`**：**`_phase2_per_job_backtest_markdown_for_track`**；**`write_phase2_track_results`** 新增 **`## Per-job backtest preview`**（含共用 **backtest_metrics** 路徑可能互蓋之說明）。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T11 勾選／敘述更新。

#### 手動驗證

```bash
# 跑過 phase2 且 bundle 含 per_job_backtest_jobs（含 --phase2-run-per-job-backtests）後：
# 檢查 phase2_gate_decision.md / track_*_results.md 是否出現 per-job PAT@1% 預覽與 gate evidence 片段
```

#### 下一步建議

- T11 完整 Gate：**uplift／std**（仍待 per-track 指標矩陣與可配置門檻）；可選 **每 job 獨立 backtest_metrics 輸出路徑** 以移除檔案互蓋。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| evidence 過長 | 實驗多時 preview 字串膨脹 | 之後改附表或截斷 + 指 bundle | 現用少量 job |
| `ok` 非 bool | 怪異列型別 | 維持 **truthy** 與現有 runner 一致 | metrics 單測 |
| 與 shared 段落語意 | 讀者混淆兩種 PAT | md 已註明 **shared vs per-job** | track md 單測 |

### STEP 3 — Tester（僅 tests）

- **`test_phase2_per_job_backtest_metrics_normalizes_rows`**、**`test_evaluate_phase2_gate_plan_only_includes_per_job_preview_evidence`**、**`test_evaluate_phase2_gate_metrics_ingested_includes_per_job_preview_evidence`**、**`test_write_phase2_track_results_per_job_backtest_section`**；**`test_write_phase2_track_results_writes_three_files`** 斷言 **Per-job backtest preview** 標題。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **101 passed**（同上 pytest）。
- **計畫下一步**：T11 **uplift／std PASS 規則**；Phase 2 sprint **gate 數值化**（對齊 `PLAN_precision_uplift_sprint` §Phase 2 Gate）。

---

## 2026-04-10 CYCLE — T10：每實驗回測 `phase2_per_job_backtest_jobs`（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)；[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T10**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`runner.py`**：**`run_phase2_per_job_backtests`**（依 **`training_metrics_repo_relative`** 解析 bundle、`phase2_cfg_to_backtest_cfg` 覆寫 **`model_dir`**、**`run_phase1_backtest`**、log 於 **`collectors.phase2_per_job_backtest_logs_subdir_relative`**；成功後讀 **`phase2_backtest_metrics_repo_relative`** 並填 **`shared_precision_at_recall_1pct_preview`**）；**`_preview_precision_at_recall_1pct_from_metrics`**。
- **`run_pipeline.py`**：**`--phase2-run-per-job-backtests`**；步驟 **`phase2_per_job_backtest_jobs`**（插在 **`phase2_job_metrics_harvest` 與 `phase2_backtest_jobs` 之間**）；**`--resume`** 可跳過；失敗 **exit 8**、**`E_PHASE2_PER_JOB_BACKTEST_JOBS`**；預設 bundle **`per_job_backtest_jobs.executed: false`**。
- **`collectors.py`**：**`collect_summary_phase2_plan_for_run_state`** 增加 **`per_job_backtest_jobs_*`** 摘要欄位（先前已具 **`phase2_per_job_backtest_logs_subdir_relative`**／**`phase2_backtest_metrics_repo_relative`**／**`model_bundle_dir_from_training_metrics_hint`**）。
- **`orchestrator/config/run_phase2.yaml`**、**`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 每實驗回測說明。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_pjb --skip-backtest-smoke --skip-phase2-trainer-smoke \
  --phase2-run-per-job-backtests
# 預期：有 training_metrics_repo_relative 的 job 會跑 backtester；bundle per_job_backtest_jobs.results；共享回測仍由 --phase2-run-backtest-jobs 觸發
```

#### 下一步建議

- T11：gate／報表消化 **per_job** 預覽欄位；可選讓每 job 寫入獨立 **backtest_metrics** 路徑以免共用檔互蓋。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 共用 `backtest_metrics.json` | 多 job 連跑會彼此覆寫檔案；僅結果內 snapshot 可靠 | 文件註明；長期可改 CLI／設定 per-job 輸出路徑 | 已 mock **load** 驗證 preview |
| 缺 hint 全 skip | `all_ok` 仍 true | 維持現狀；報表看 **executed**／**results** | skip 單測 |
| `first_err` 僅第一筆 | 與 trainer batch 一致 | 維持 | — |
| Resume + 改旗標 | 已成功步驟不重跑 | 與既有 phase2 步驟一致 | resume 單測 |

### STEP 3 — Tester（僅 tests）

- **`test_collect_summary_phase2_plan_includes_per_job_backtest_jobs`**、**`test_model_bundle_dir_from_training_metrics_hint_file_and_directory`**、**`test_run_phase2_per_job_backtests_skips_without_hint`**、**`test_run_phase2_per_job_backtests_resolves_model_dir_and_preview`**、**`test_phase2_resume_skips_completed_per_job_backtest_jobs`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **97 passed**（同上 pytest）。
- **計畫下一步**：T11 將 **per_job** 預覽納入 gate／track md；可選獨立 **metrics** 輸出路徑。

---

## 2026-04-10 CYCLE — T10：`phase2_collect` 指標路徑計數 + trainer／orchestrator log 契約（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)；[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T10**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`collectors.py`**：**`collect_summary_phase2_plan_for_run_state`** 新增 **`job_specs_training_metrics_hint_count`**（`job_specs` 中含非空 **`training_metrics_repo_relative`** 的筆數，含 YAML 手填與 runner merge 後）。
- **`runner.py`**：模組常數 **`TRAINER_ARTIFACTS_SAVED_LOGGER_INFO_FORMAT`**（與 trainer 原始碼 **`logger.info`** 行字串對齊，供契約測／文件）。
- **`trainer/training/trainer.py`**：**`save_artifact_bundle`** 內 **`Artifacts saved to`** 行前加 **契約註解**（指向 orchestrator **`runner.py`**）。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_hintcnt --skip-backtest-smoke --skip-phase2-trainer-smoke
# 預期：run_state.phase2_collect.job_specs_training_metrics_hint_count 與 bundle 一致
```

#### 下一步建議

- T10 每實驗回測鏈；T11 uplift／std gate。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 常數字串與 trainer 分行／換行 | 編排工具改格式 → 契約測誤報 | 以 **子字串** 或 **正則** 放寬 | 原始碼掃描單測 |
| `hint_count` 與 harvest found 混淆 | 前者=有路徑提示，後者=檔案讀到 | 文件／欄位命名維持 | 摘要單測 |
| trainer 改 `logger.info` 文案 | regex 與常數雙破 | 單測失敗即提醒同步 **runner** regex | 契約單測 |

### STEP 3 — Tester（僅 tests）

- **`test_trainer_artifacts_saved_log_line_contract_for_orchestrator`**、**`test_collect_summary_phase2_plan_counts_training_metrics_hints`**；**`test_collect_phase2_plan_bundle_shape`** 補 **`job_specs_training_metrics_hint_count == 0`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **92 passed**（`python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short`）。
- 無需改實作；契約測與 **`job_specs_training_metrics_hint_count`** 摘要已通過。
- **計畫下一步**：T10 **每實驗回測** subprocess；T11 **per-track 指標矩陣**與 uplift／std **PASS**。

---

## 2026-04-10 CYCLE — T10：trainer log 推斷並回填 `training_metrics_repo_relative`（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)；[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T10**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`runner.py`**：**`infer_training_metrics_repo_relative_from_trainer_logs`**（掃描 stdout/stderr 尾段，比對 trainer `Artifacts saved to …  (version=`）；**`merge_inferred_training_metrics_paths_into_phase2_bundle`**；**`run_phase2_trainer_jobs`** 每筆 result 附 **`inferred_training_metrics_repo_relative`**；**`import collectors`** 以重用 **`_safe_resolve_under_repo_root`** 驗證推斷路徑。
- **`run_pipeline.py`**：`phase2_trainer_jobs` **實跑**後呼叫 **`merge_inferred_training_metrics_paths_into_phase2_bundle`**，再寫 bundle／`phase2_collect`（利於後續 **harvest**）。
- **`orchestrator/config/run_phase2.yaml`**：註解說明自動回填行為。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 trainer 列補上 log 推斷回填。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_infer --phase2-run-trainer-jobs \
  --skip-backtest-smoke --skip-phase2-trainer-smoke
# 預期：訓練成功後 phase2_bundle.json 的 job_specs 出現 training_metrics_repo_relative（若 YAML 未先設）；
# trainer_jobs.results[].inferred_training_metrics_repo_relative 有值；harvest 可 found
```

#### 下一步建議

- Trainer log 格式變更時同步 regex；T10 每實驗回測鏈；T11 uplift gate。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| Log 格式漂移 | `Artifacts saved to` 文案變更 → 推斷失敗 | 與 trainer **單一 log 行**對齊或整合測 | 合成 log 單測 |
| 多行／多 match | 同一 log 尾段多次保存 | 取 **最後一筆** match | 兩筆路徑單測 |
| 推斷路徑逃出 repo | 惡意或誤設絕對路徑 | **`_safe_resolve`** 過濾 | merge 單測 |
| YAML 已指定路徑 | 不應被推斷覆寫 | merge 跳過已有鍵 | 覆寫防護單測 |

### STEP 3 — Tester（僅 tests）

- **`test_infer_training_metrics_repo_relative_from_trainer_logs_*`**、**`test_merge_inferred_training_metrics_paths_into_phase2_bundle_*`**；**`test_run_phase2_trainer_jobs_invalid_spec`** 斷言 **`inferred_training_metrics_repo_relative`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **90 passed**（`python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short`）。
- 無需再改實作；本輪測試已覆蓋推斷、最後 match、repo 外路徑、merge 回填與 **不覆寫 YAML**。
- **計畫下一步**：T10 **每實驗回測** subprocess 鏈；trainer log 與 regex **契約測**（可選）；T11 **uplift／std PASS**。

---

## 2026-04-10 CYCLE — T10：`training_metrics_repo_relative`（YAML→harvest 契約）（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)；[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T10**（investigation pipeline 產物路徑）；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`config_loader.py`**：實驗可選 **`training_metrics_repo_relative`**（若出現則須為非空字串）。
- **`collectors.py`**：**`_safe_resolve_under_repo_root`**（禁絕對路徑、禁逃出 repo 根）、**`_phase2_job_training_metrics_path`**；**`harvest_phase2_job_training_metrics`** 優先讀該路徑（檔或目錄 + `training_metrics.json`），否則沿用 log 目錄；**`collect_phase2_plan_bundle`** 將欄位寫入 **`tracks.*.experiments`**、**`experiments_index`**、**`job_specs`**；bundle **note** 更新。
- **`orchestrator/config/run_phase2.yaml`**：註解範例。
- **`report_builder.py`**：track results harvest 區塊說明對齊。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 harvest 項改寫為含 **`training_metrics_repo_relative`**。

#### 手動驗證

```bash
# 在 phase2 YAML 某實驗下設定 training_metrics_repo_relative: out/models/<run>/training_metrics.json
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_tmrel --skip-backtest-smoke --skip-phase2-trainer-smoke
# 預期：phase2_bundle job_specs 含該鍵；job_training_harvest 對應列 found=true（檔存在時）
```

#### 下一步建議

- Runner 於訓練成功後**自動**回填 `training_metrics_repo_relative`（或寫入 log 目錄）；T10 每實驗回測鏈仍待。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `resolve()` 語意 | symlink／大小寫（Windows）可能與預期不同 | 文件註明以 **resolve 後** 須落在 repo 內 | escape 路徑單測 |
| 目錄下缺 `training_metrics.json` | `found=false` | 維持現有 load 錯誤訊息 | dir 無檔案 |
| 僅設相對路徑 | 仍依賴人工對齊產物 | 後續 runner 自動寫 bundle | config 傳遞單測 |
| Windows `run_state` replace | 整包 pytest 偶發 **PermissionError** | **`_write_run_state`** 對 replace 短重試 | subprocess 整合測 |

### STEP 3 — Tester（僅 tests）

- **`test_phase2_config_training_metrics_repo_relative_*`**、**`test_collect_phase2_plan_bundle_propagates_training_metrics_repo_relative`**、**`test_harvest_prefers_training_metrics_repo_relative_over_logs`**、**`test_harvest_training_metrics_repo_relative_file_path`**、**`test_harvest_training_metrics_repo_relative_rejects_escape`**、**`test_harvest_training_metrics_repo_relative_rejects_absolute`**（斷言含 absolute 或 escapes，因 OS 對 `/etc/...` 解析差異）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（修實作）

- **85 passed**（同上指令）。
- **`run_pipeline._write_run_state`**：`tmp.replace(run_state.json)` 遇 **PermissionError** 時最多 **8** 次、遞增 sleep（緩解 Windows 鎖檔／防毒）。
- **計畫下一步**：runner 訓練成功後自動寫入 **`training_metrics_repo_relative`** 或複製至 log 目錄；T10 **每實驗回測鏈**；T11 uplift／std **PASS** 規則。

---

## 2026-04-10 CYCLE — T10：`phase2_job_metrics_harvest` + `job_training_harvest` + track md 小節（/cycle_code 全四步）

> 計畫索引：[`.cursor/plans/PLAN.md`](.cursor/plans/PLAN.md)（專案總表）、[`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **T10**；`DECISION_LOG.md`：[`.cursor/plans/DECISION_LOG.md`](.cursor/plans/DECISION_LOG.md)。

### STEP 1 — Builder

- **`collectors.py`**（延續）：`PHASE2_JOB_TRAINING_METRICS_NAME`、`harvest_phase2_job_training_metrics`、`collect_summary_phase2_plan_for_run_state` 之 **`job_training_harvest_*`** 摘要。
- **`evaluators.py`**（延續）：`_job_training_harvest_counts`；`plan_only`／`metrics_ingested` 之 **`gate.metrics`** 與 evidence 帶 harvest 列數／found 數。
- **`run_pipeline.py`**：步驟 **`phase2_job_metrics_harvest`**（`phase2_trainer_jobs` 之後、`phase2_backtest_jobs` 之前）；寫入 **`p2_bundle["job_training_harvest"]`**；**`--resume`** 且該步已成功則跳過。
- **`report_builder.py`**：**`write_phase2_track_results`** 新增 **「Per-job training_metrics harvest」**（**`_phase2_harvest_markdown_for_track`**：每軌道 found／相對路徑／error，不 dump 整份 JSON）。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 勾選 harvest 與 track md 說明。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_harvest --skip-backtest-smoke --skip-phase2-trainer-smoke
# 預期：phase2_bundle.json 含 job_training_harvest.rows（每 job 一列；檔不在 log 目錄則 found=false）；
# run_state.steps.phase2_job_metrics_harvest = success
# phase2/track_*_results.md 含「Per-job training_metrics harvest」
```

#### 下一步建議

- Trainer／runner 將 **`training_metrics.json`** 寫入 **`logs_subdir_relative`**（或複製）以讓 harvest **found**；後續可改為 bundle 只存路徑＋摘要以避免 JSON 膨脹。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `job_training_harvest` 內嵌完整 `training_metrics` | 大實驗使 **`phase2_bundle.json` 過大** | 後續只存路徑＋精簡欄位 | 文件／可選上限 |
| 預設 **found=false** | Trainer 仍寫 model bundle 而非 job log | STATUS／tasklist 已註記；對齊輸出路徑 | harvest 單測 |
| Windows **`run_state.json` 原子 replace** | 偶發 **`PermissionError`**（全 suite 連跑） | 可評估 `_write_run_state` 重試或關閉即 flush | `test_phase2_resume_skips_completed_job_metrics_harvest`（曾單測重跑即過） |

### STEP 3 — Tester（僅 tests）

- **`test_harvest_phase2_job_training_metrics_*`**、**`test_collect_summary_phase2_plan_includes_job_training_harvest`**、擴充 **`test_write_phase2_track_results_writes_three_files`**、**`test_phase2_bundle_includes_job_training_harvest_after_pipeline`**、**`test_phase2_resume_skips_completed_job_metrics_harvest`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- **78 passed**（同上指令；若 Windows 上整包曾出現 `run_state` replace 權限錯誤，可重跑單測或整包確認）。
- **計畫下一步**：Trainer 輸出對齊 job log 目錄；每實驗回測鏈／uplift 矩陣（T10 剩餘項、T11 gate）。

---

## 2026-04-10 CYCLE — T11：`track_*_results.md` + `metrics_ingested` gate 證據（/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T11**；`DECISION_LOG.md`：`.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder

- **`evaluators.py`**：`PHASE2_BACKTEST_PR1_KEY`、`extract_phase2_shared_precision_at_recall_1pct`；**`metrics_ingested`** 改 **blocking** 為 **`phase2_shared_metrics_no_per_track_uplift`**，**evidence**／**`gate.metrics.shared_precision_at_recall_1pct`** 帶共享 PAT@1%（可解析時）。
- **`report_builder.py`**：**`write_phase2_track_results`** → `phase2/track_a_results.md`、`track_b_results.md`、`track_c_results.md`（YAML 實驗清單 + 共享 PAT@1% 免責聲明 + gate 摘要）。
- **`run_pipeline.py`**：`phase2_gate_report` 步驟於 gate md 後寫入三份 track md；**`artifacts`** 含 **`phase2_track_*_results`**；結尾 **`merged.artifacts`** 同步路徑。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T11 track 報表 stub ✅；gate 完整 uplift／std 仍待。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t11_docs --skip-backtest-smoke --skip-phase2-trainer-smoke
# 預期：investigations/precision_uplift_recall_1pct/phase2/track_{a,b,c}_results.md 更新；
# metrics_ingested 時檔內含 PAT@1%；plan_only 時 PAT 列為不可用
```

#### 下一步建議

- Per-track／per-experiment 指標與 **uplift vs baseline**、跨窗 **std**；gate **PASS** 條件。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 三份 track md 內容重複 | 易誤讀為獨立實驗結果 | 已加 **Note**；可再加檔首 warning | `test_write_phase2_track_results_*` |
| `model_default` 缺鍵 | PAT 為 None | evidence 仍 BLOCKED | extractor 單測 |
| `import evaluators` 在 report_builder 函數內 | 循環 import 風險低 | 維持 lazy import | 無 |

### STEP 3 — Tester（僅 tests）

- **`test_extract_phase2_shared_precision_at_recall_1pct`**、**`test_evaluate_phase2_gate_metrics_ingested_includes_pat_in_evidence`**、**`test_write_phase2_track_results_writes_three_files`**；更新 **`test_evaluate_phase2_gate_metrics_ingested_blocked`**（新 blocking code）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- **72 passed**（同上指令）。
- **計畫下一步**：per-experiment 指標矩陣、uplift／std gate、**PASS** 語意。

---

## 2026-04-10 CYCLE — T10：`phase2_backtest_jobs`（共享回測 + `backtest_metrics` ingest）（/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T10**；`DECISION_LOG.md`：`.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder

- **`collectors.py`**：`phase2_shared_backtest_logs_subdir_relative`、`load_json_under_repo`；`phase2_collect` 摘要含 **`backtest_jobs_*`**、**`phase2_has_backtest_metrics`**。
- **`run_pipeline.py`**：`phase2_cfg_to_backtest_cfg`、`_phase2_backtest_timeout_sec`；CLI **`--phase2-run-backtest-jobs`**；步驟 **`phase2_backtest_jobs`**（於 `phase2_trainer_jobs` 之後、`phase2_gate_report` 之前）；預設略過並寫 **`backtest_jobs.executed: false`**；實跑時呼叫 **`runner.run_phase1_backtest`**，再 ingest **`resources.backtest_metrics_path`** 或 **`trainer/out_backtest/backtest_metrics.json`**；成功則 **`bundle.status: metrics_ingested`** 並寫入 **`backtest_metrics`**；子程序失敗或缺檔／無效 JSON → **exit 8**（缺檔時 bundle **`errors`** 附 **`E_ARTIFACT_MISSING`**）；**`--resume`** 且該步已成功則跳過。
- **`evaluators.py`**：`evaluate_phase2_gate` 對 **`metrics_ingested`** → **BLOCKED**（`phase2_uplift_gate_not_implemented`，待 T11）。
- **`orchestrator/config/run_phase2.yaml`**：註解 `phase2_backtest_timeout_sec`、`backtest_metrics_path`。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 共享回測項 ✅；每實驗回測仍待。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_bt --skip-backtest-smoke --skip-phase2-trainer-smoke
# 預期：phase2_bundle.json 含 backtest_jobs.executed=false

python ... --phase2-run-backtest-jobs
# 預期：logs/phase2/_shared_backtest/backtest.*.log；ingest 成功則 status=metrics_ingested；缺 metrics 檔或子程序失敗 → exit 8
```

#### 下一步建議

- 每實驗回測／`training_metrics`、跨窗彙整；T11 uplift／std 與 `track_*_results.md`。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 僅「共享」一回測 | 與多 job 訓練不同步、非每實驗一模型 | 文件標註；後續每 job model_dir 契約 | gate `metrics_ingested` 單測 |
| 預設 metrics 路徑 | backtester 寫入與預設讀取不一致時 ingest 失敗 | 用 `resources.backtest_metrics_path` 對齊 | `load_json_under_repo` |
| exit 8 雙義 | 子程序失敗與缺檔皆為 8 | 看 `run_state` step message / bundle `errors` | pipeline 預設跳過單測 |
| 逾時 | 長回測無上限 | `phase2_backtest_timeout_sec` | 可選 |

### STEP 3 — Tester（僅 tests）

- **`test_phase2_cfg_to_backtest_cfg_maps_window_and_skip_optuna`**、**`test_phase2_shared_backtest_logs_subdir_relative`**、**`test_load_json_under_repo_*`**、**`test_evaluate_phase2_gate_metrics_ingested_blocked`**、**`test_phase2_bundle_backtest_jobs_skipped_after_pipeline`**、**`test_phase2_resume_skips_completed_backtest_jobs`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- **69 passed**（同上指令）。
- **計畫下一步**：每實驗回測與指標、T11 uplift／`track_*_results.md`。

---

## 2026-04-10 CYCLE — T10：`phase2_trainer_jobs`（可選 `trainer.trainer` 批次）（/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T10**；`DECISION_LOG.md`：`.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/orchestrator/runner.py`**：`phase2_experiment_overrides`、`build_phase2_trainer_argv`、`run_phase2_trainer_jobs`（每 `job_specs` 一次子程序；可選 `resources.phase2_trainer_job_timeout_sec`、`trainer_use_local_parquet`）；YAML `overrides` 列於 `unapplied_overrides`。
- **`run_pipeline.py`**：新步驟 **`phase2_trainer_jobs`**（於 `phase2_runner_smoke` 之後、`phase2_gate_report` 之前）；CLI **`--phase2-run-trainer-jobs`** 才實跑；否則 bundle 寫 **`trainer_jobs.executed: false`**；失敗 **exit 7**；**`--resume`** 且該步已成功則跳過。
- **`collectors.py`**：`phase2_collect` 摘要含 **`trainer_jobs_executed`**、**`trainer_jobs_all_ok`**、**`trainer_jobs_count`**。
- **`orchestrator/config/run_phase2.yaml`**：註解可選 `trainer_use_local_parquet`、`phase2_trainer_job_timeout_sec`。
- **`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 拆成「可選 trainer 批次」✅ 與「回測鏈／fail-fast」仍待。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_jobs --skip-backtest-smoke --skip-phase2-trainer-smoke
# 預期：phase2_bundle.json 含 trainer_jobs.executed=false、skip_reason 說明

python ... --phase2-run-trainer-jobs
# 預期：每 job 目錄有 trainer_job_*.stdout.log；任務失敗則 exit 7（訓練可能極久／需 CH 或 local parquet）
```

#### 下一步建議

- 接回測 CLI、指標 ingest、`status` 由 plan_only 進階；`E_ARTIFACT_MISSING` 等；T11 uplift 規則。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 預設不跑仍寫 bundle | 舊 consumer 若嚴格 schema 可能不認 `trainer_jobs` | 視為 bundle 擴充；文件已註明 | 讀 bundle 斷言鍵存在 |
| 每 job 全量訓練 | 筆電／CI 耗時、CH 依賴 | 維持 opt-in；可設 `phase2_trainer_job_timeout_sec` | resume 跳步單測；argv 單元測試 |
| `overrides` 未接 CLI | 實驗矩陣誤以為已套用 | 以 `unapplied_overrides` 明示 | 斷言非空 overrides 鍵出現在列表 |
| exit 7 語意 | 與 dry-run 6、runner 5 並存 | 文件／STATUS 已標 | 可選：mock 失敗子程序 → exit 7 |

### STEP 3 — Tester（僅 tests）

- **`test_build_phase2_trainer_argv_skip_optuna`**、**`test_build_phase2_trainer_argv_unapplied_overrides`**、**`test_build_phase2_trainer_argv_use_local_parquet`**、**`test_run_phase2_trainer_jobs_invalid_spec`**、**`test_run_phase2_trainer_jobs_empty_job_specs`**、**`test_phase2_bundle_trainer_jobs_skipped_after_pipeline`**、**`test_phase2_resume_skips_completed_trainer_jobs`**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- **62 passed**（同上指令）。
- **計畫下一步**：T10 接上 **backtest／指標 ingest**、bundle **`status`** 演進、`E_ARTIFACT_MISSING`／`E_NO_DATA_WINDOW`；T11 uplift／`track_*_results.md`。

---

## 2026-04-10 — Phase2 `job_specs` log 路徑與 state 同樹

- **`collectors.collect_phase2_plan_bundle`**：`logs_subdir_relative` 改為相對 repo 根之 `investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/logs/phase2/...`（與 `phase2_bundle.json`／`run_state.json` 同目錄樹）。
- **搬移**：原誤建在 repo 根 `orchestrator/state/*/logs/` 者，已合併至上述 investigation `state/*/logs/`；repo 根 `orchestrator/` 目錄已移除（若為空）。
- **測試**：`tests/unit/test_precision_uplift_phase1_orchestrator.py` 路徑斷言已更新；fixture `phase2_bundle.json`（t10_smoke 等）已同步。

---

## 2026-04-10 CYCLE — T10：`phase2_runner_smoke`（mkdir + trainer `--help`）（/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T10**；`DECISION_LOG.md`：`.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/orchestrator/runner.py`**：`ensure_phase2_job_log_dirs`、`run_trainer_trainer_help_smoke`（`python -m trainer.trainer --help`，timeout 120s）。
- **`investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`**：`--skip-phase2-trainer-smoke`；`phase2_runner_smoke` 步驟（`plan_bundle` 之後、`gate_report` 之前）；寫回 `phase2_bundle.json` 之 `runner_smoke`；log mkdir 失敗或 trainer smoke 失敗 → **exit 5**；`--resume` 可跳過已完成之該步。
- **`investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`**：`phase2_collect` 摘要含 `runner_log_dirs_ok`、`runner_trainer_help_skipped`、`runner_trainer_help_ok`。
- **`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 區分 smoke／實際訓練 runner；§3 已完成補述。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <yaml> --run-id t10_smoke --skip-backtest-smoke
# 預期：每 job log 目錄被建立；trainer --help 通過；bundle 有 runner_smoke

python ... --skip-phase2-trainer-smoke
# 預期：仍建 log 目錄；trainer_help_skipped
```

#### 下一步建議

- 依 `job_specs` 呼叫真實訓練 CLI、收集指標、替換 `plan_only`／豐富 gate。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `trainer.trainer --help` 冷啟動慢 | 120s timeout | 與 backtest smoke 類似；CI 可 `--skip-phase2-trainer-smoke` | 單測預設 skip |
| exit 5 與 phase1 碼語意 | 僅 phase2 使用 | 文件註明 | 無 |
| 舊 `run_state` 無 `phase2_runner_smoke` | 首次 resume 會補跑 | 預期行為 | `test_phase2_resume_skips_completed_runner_smoke` |

### STEP 3 — Tester（僅 tests）

- **`test_ensure_phase2_job_log_dirs_creates`**、`**test_phase2_resume_skips_completed_runner_smoke**`；phase2 scaffold／resume no bundle 測試加上 `--skip-phase2-trainer-smoke`。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- **55 passed**（同上指令）。

**計畫狀態**：T10 **smoke runner** 已接線；建議下一項 **每實驗真實訓練 subprocess + 指標回填 bundle**。

---

## 2026-04-10 CYCLE — T10：`phase2_bundle.job_specs` + resume 缺檔 exit 4 測試（/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T10**；`DECISION_LOG.md`：`.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`**
  - `collect_phase2_plan_bundle` 新增 **`job_specs`**：僅含 **track 已 enable** 且 **`exp_id` 非空** 之實驗；每筆含 `logs_subdir_relative`（相對 repo 根，與 bundle 同樹：`investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/logs/phase2/<track>/<exp_id>/`）。
  - `collect_summary_phase2_plan_for_run_state` 新增 **`job_specs_count`**。
- **`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10 collector 子項補述 `job_specs`。

#### 手動驗證

- 跑一次 `--phase phase2`（有效 paths）後開啟 `phase2_bundle.json`，確認存在 `job_specs` 且筆數等於啟用軌道上有 `exp_id` 的實驗數。

#### 下一步建議

- T10 runner：依 `job_specs` 建目錄、spawn trainer、回填 bundle。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `logs_subdir_relative` 僅為約定 | runner 未建目錄前路徑不存在 | T10 實作時 `mkdir -p` | 無 |
| `job_specs` 與 `experiments_index` 冗餘 | 維護兩份 | 保留：index 全量、job_specs 為「可執行子集」 | `test_collect_phase2_plan_bundle_shape` 已擴充 |
| `run_id` 特殊字元 | 路徑片段若含 `/` 可能異常 | 現由 YAML／CLI 限制；必要時 sanitize | 可選 |

### STEP 3 — Tester（僅 tests）

- **`tests/unit/test_precision_uplift_phase1_orchestrator.py`**
  - 擴充 `test_collect_phase2_plan_bundle_shape`（`job_specs`／`job_specs_count`／路徑前綴）。
  - `test_phase2_resume_missing_bundle_exits_4`（刪 bundle 後 `--resume` → exit 4）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- **53 passed**（同上指令）。

**計畫狀態**：T10 **列 job + log 路徑契約**已進 bundle；建議下一項 **T10 subprocess runner**。

---

## 2026-04-10 CYCLE — T11 最小：Phase2 gate + `phase2_gate_decision.md`（/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T11**；`DECISION_LOG.md`：`.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/orchestrator/evaluators.py`**：`evaluate_phase2_gate`（`plan_only` → `BLOCKED`；`errors` → `FAIL`；未知 `status` → `BLOCKED`）。
- **`investigations/precision_uplift_recall_1pct/orchestrator/report_builder.py`**：`write_phase2_gate_decision` → `investigations/precision_uplift_recall_1pct/phase2/phase2_gate_decision.md`。
- **`investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`**：`_main_phase2` 在 `phase2_plan_bundle` 後載入 `p2_bundle`（resume 自磁碟）；`phase2_gate_report` 步驟；`run_state.phase2_gate_decision`；resume 可跳過；缺 bundle 之 resume → exit **4**。
- **`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T11／§3／§4 對齊現況（uplift 規則與 track 報表仍 `[ ]`）。

#### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 --config <你的 run_phase2.yaml> --run-id t11_smoke --skip-backtest-smoke
# 預期：phase2/phase2_gate_decision.md 內 status 為 BLOCKED（plan_only）；run_state 有 phase2_gate_decision
```

#### 下一步建議

- **T10 runner** 產物與 bundle 擴充；**T11** uplift／std 規則與 `track_*_results.md`。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `BLOCKED` vs `FAIL` | plan_only 用 BLOCKED；collector errors 用 FAIL | 文件與 gate md 已區分 | `test_evaluate_phase2_gate_*` |
| Resume 缺 `phase2_bundle.json` | exit 4 | Runbook 一句 | 可選：刪 bundle 後 `--resume` |
| 覆寫共用 `phase2_gate_decision.md` | 多 run_id 寫同一 path | 與 phase1 類似以 investigation 目錄為 SSOT；長跑可改 run_id 子目錄（後續） | 無 |

### STEP 3 — Tester（僅 tests）

- **`tests/unit/test_precision_uplift_phase1_orchestrator.py`**：`test_evaluate_phase2_gate_plan_only_blocked`、`test_evaluate_phase2_gate_errors_fail`、`test_evaluate_phase2_gate_unsupported_status_blocked`；擴充 phase2 scaffold CLI 斷言 `phase2_gate_report` 與 gate md。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- **52 passed**（同上指令）。

**計畫狀態**：T11 **骨架**已落地；建議下一項 **T10 track runner**（餵養 bundle 與後續 PASS／FAIL uplift gate）。

---

## 2026-04-10 CYCLE — Implementation Plan 對齊 T10 plan-only + 操作備註（/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；`DECISION_LOG.md`：`.cursor/plans/DECISION_LOG.md`。接續 T10 plan bundle 落地，**只改文件 + 契約單測**。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**
  - **T10 完成定義**：拆成 **plan_only 可重現** `[x]` 與 **runner 階段**仍 `[ ]`；stdout／artifacts 可追溯仍待 runner。
  - **§3**：「已完成」加入 T10 部分（plan bundle／`phase2_plan_bundle`）；Sprint A 改為 T9 ✅、T10 進行中、T11。
  - **§4 DoD**：`--phase phase2` 一行改寫，納入 plan-only bundle 與仍待項。
  - **§5**：`run_phase2.yaml` 的 `model_dir`／DB 本機路徑提醒；讀 `phase2_bundle.json` 必看 `status`（`plan_only` 語意）。

#### 手動驗證

- 通讀 Implementation Plan **T10**、**§3**、**§4**、**§5** 與 orchestrator 行為是否一致。

#### 下一步建議

- T10 runner；T11 evaluator；或 Runbook 單句鏈結 MVP §5。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 「可重現」範圍 | 僅保證 config→bundle；未承諾跨 Python 版本 float 序 | MVP 已寫「僅由 config 展開」 | `test_collect_phase2_plan_bundle_deterministic_json` |
| Sprint A 字樣過長 | 表格換行可讀性 | 維持一行；必要時改 footnote | 無 |
| §5 與 Runbook 重複 | 兩處維護 | Runbook 可「見 MVP §5」 | 無 |

### STEP 3 — Tester（僅 tests）

- **`tests/unit/test_precision_uplift_phase1_orchestrator.py`**：`test_collect_phase2_plan_bundle_deterministic_json`（canonical JSON 相等）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 無需改 production；**49 passed**（同上指令）。

**計畫狀態**：Implementation Plan 與 **T10 plan-only** 邊界已寫清；建議下一項 **T10 track runner**。

---

## 2026-04-10 CYCLE — T10 起手：phase2 plan-only `phase2_bundle.json`（/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；precision uplift：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T10**；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`**
  - `collect_phase2_plan_bundle`：由已驗證 phase2 config 產出可重現 bundle（`bundle_kind: phase2_plan_v1`、`status: plan_only`、`experiments_index`）。
  - `collect_summary_phase2_plan_for_run_state`：寫入 `run_state.phase2_collect` 摘要。
- **`investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`**
  - `_main_phase2`（非 dry-run、非 collect-only）：在 `phase2_scaffold` 後新增 `phase2_plan_bundle` 步驟，原子寫入 `orchestrator/state/<run_id>/phase2_bundle.json`；`artifacts.phase2_bundle`；`--resume` 且該步已 success 時跳過。
- **`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**：T10「collector」子項標記 plan-only 範圍已 `[x]`，並註明 trainer 產物仍待。

#### 手動驗證

```bash
# 與 T9 相同前置（本機 model_dir + 兩 DB 通過 preflight）
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id my_p2_bundle \
  --skip-backtest-smoke
# 預期：orchestrator/state/my_p2_bundle/phase2_bundle.json 存在且 status=plan_only
```

#### 下一步建議

- T10 其餘：A/B/C **runner**、由產物回填 bundle（替換 `plan_only`）、`E_ARTIFACT_MISSING`／`E_NO_DATA_WINDOW`。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `plan_only` 誤當訓練完成 | 下游若只看檔名 `phase2_bundle.json` 可能誤解 | 報表／gate 必讀 `status` 與 `bundle_kind`；Runbook 一句話 | 單測斷言 `plan_only` |
| Resume 跳過寫入 | 改 YAML 後若 fingerprint 未變極少見；一般會因 fingerprint 重跑 | 維持現有 `input_summary` fingerprint | 可選：resume 雙次 CLI |
| 大型 overrides | `json.dumps(common)` 複製整份 common | 可接受；未來 runner 應避免把秘密寫進 yaml | 無 |
| `collect_only` 無 bundle | 與 T9 語意一致（僅 preflight） | 文件註明；若要 collect-only 也出 plan 可另開任務 | 既有 collect_only 測試 |

### STEP 3 — Tester（僅 tests）

- **`tests/unit/test_precision_uplift_phase1_orchestrator.py`**
  - `test_collect_phase2_plan_bundle_shape`、`test_collect_phase2_plan_bundle_raises_on_non_mapping`、`test_collect_phase2_plan_bundle_raises_on_bad_tracks`
  - 擴充 `test_run_pipeline_phase2_scaffold_writes_run_state`：斷言 `phase2_plan_bundle` 步驟與 `phase2_bundle.json`

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 無需額外修 production；**48 passed**（同上指令）。

**計畫狀態**：T10 **plan-only bundle** 已落地；建議下一項 **T10 runner**（或 T11 evaluator 可先設計介面）。

---

## 2026-04-10 CYCLE — Implementation Plan 對齊 T16A（文件 + checklist 契約測試，/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；決策：`.cursor/plans/DECISION_LOG.md`。接續同日 T16A 實作，**同步** `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**
  - **T16A** 已落地項改為 `[x]`，並註明 phase3／4 為 **minimal schema**、`run_full`／`DRY_RUN_FLAG_DEFAULTS` 為 checklist SSOT。
  - **T16** 第一項改為 `[x]`（**僅 `--dry-run`**），其餘長跑／DAG 仍 `[ ]`。
  - **T12／T14**：`run_phase3.yaml`／`run_phase4.yaml` 範例標為 **T16A 最小範例** `[x]`；`--phase phase3|phase4` 仍 `[ ]`。
  - **§3 建議順序**：已完成區塊補 T16A；Sprint D 改為「T16A ✅、T16 剩餘」。
  - **§4 DoD**：新增 `--phase all --dry-run` 勾選；區分 all-phase **長跑** resume（T16）與現況。

#### 手動驗證

- 開啟 `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`，確認 T16A 區塊與 T16／T12／T14 邊界說明可讀、與 `orchestrator/` 實作一致。

#### 下一步建議

- **T10／T11** Phase 2 主線；**T16** 非 dry-run `--phase all`；**T12／T14** 完整 schema 與 `--phase phase3|phase4`。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 文件與程式 checklist 漂移 | MVP 逐條列 `dry_run` 鍵，若程式改名易不一致 | 以 `config_loader.DRY_RUN_FLAG_DEFAULTS` 為 SSOT；task list 指回該常數 | 單測斷言鍵集合與 T16A 契約一致 |
| 「T12 yaml 已勾選」誤解 | 讀者以為 Phase 3 runner 已完成 | 文內已標 **T16A 最小**；必要時在 T12 標題加「runner 仍待」 | 人工掃一眼 T12 第一條仍 `[ ]` |
| Sprint D 字樣 | 「✅」在 markdown 列表可讀性 | 維持簡短；Release note 可再展開 | 無 |

### STEP 3 — Tester（僅 tests）

- **`tests/unit/test_precision_uplift_phase1_orchestrator.py`**：`test_t16a_dry_run_checklist_keys_match_config_loader_contract`（`DRY_RUN_FLAG_DEFAULTS` 鍵集合）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 無需改 production；**45 passed**（`python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short`）。

**計畫狀態**：Implementation Plan 與 **T16A** 實作對齊；建議下一項 **T10（Phase 2 track runner）** 或 **T16（all-phase 長跑）**。

---

## 2026-04-10 CYCLE — precision_uplift T16A（`--phase all` + `run_full.yaml` all-phase dry-run，/cycle_code 全四步）

> 計畫索引：`.cursor/plans/PLAN.md`；決策：`.cursor/plans/DECISION_LOG.md`。對齊 MVP／Runbook **T16A**（長跑前 all-phase dry-run）。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`**
  - `build_run_full_input_summary`、`run_all_phases_dry_run_readiness`（checklist：`dry_run` 旗標、`phase_configs` 解析、schema／依賴／contract／paths／resource_limits、phase1+2 preflight、可寫入與 CLI smoke／writable 透過 `run_dry_run_readiness`）。
  - `skip_phase1_preflight` / `skip_phase2_preflight`：避免與 `_main_all` 開頭 phase1 preflight 重複；`--resume` 且 preflight 已成功時兩段 embedded preflight 皆跳過（與 phase1 resume 風險同型）。
  - `_main_all`：`--phase all` 僅允許 **`--dry-run`**（非 dry-run → exit 2）；不支援 `--collect-only`；載入 `run_full.yaml`、寫入 `run_state.json`（`phase: all`、`mode: dry_run`、`readiness`）。
- **`investigations/precision_uplift_recall_1pct/orchestrator/config/run_full.yaml`**、`run_phase3.yaml`、`run_phase4.yaml`：範例組態（phase3／4 為 T16A 最小形狀，待 T12／T14 完整 schema）。
- **`investigations/precision_uplift_recall_1pct/orchestrator/config_loader.py`**：前序輪已含 `load_run_full_config`、phase3／4 minimal validate（本輪沿用）。

#### 手動驗證

```bash
# 必須帶 --dry-run；否則 exit 2
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase all \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_full.yaml \
  --run-id my_all_dry \
  --dry-run \
  --skip-backtest-smoke
```

（範例 `run_phase1`／`run_phase2` 路徑須在本機存在且通過 preflight；否則預期 `NOT_READY` 或 preflight exit 3。）

#### 下一步建議

- **T12／T14**：以完整 phase3／phase4 schema 取代 minimal validator。
- **Autonomous `all`**：實作非 dry-run 的 phase 序列與 gate 行為後，再放寬 `--phase all` 限制。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| Resume 跳過 phase2 preflight | `--resume` 時 `skip_phase2_preflight=True`，與 phase1 resume 相同「信任舊 state」風險 | 文件註明；若要強化可在 `dry_run` 加旗標強制重跑 | 可選：resume 後改 phase2 路徑仍顯 READY 之 MRE |
| `fail_on_any_check: false` | `_append_check` 不寫 blocking，但 preflight 子項仍手動 `blocking.append` | 若需完整語意，preflight 失敗也應尊重 `fail_on_any_check` | 可選：`dry_run.fail_on_any_check: false` 行為單測 |
| 重複 path 檢查 | `validate_paths_readable` 與 preflight 部分重疊 | 可接受為「顯式 checklist」；留意維護成本 | 現有 integration 路徑已覆蓋 |
| 範例 `run_full.yaml` 相對路徑 | 依 repo 根解析；cwd 非 repo 根時行為依 CLI `cwd` | Runbook 註明於 repo 根執行 | 已有 `_resolve_config_path` 單元情境 |

### STEP 3 — Tester（僅 tests）

- 擴充：`tests/unit/test_precision_uplift_phase1_orchestrator.py` — `run_full` run_id mismatch、`build_run_full_input_summary` fingerprint、`--phase all` 無 dry-run exit 2、`run_all_phases_dry_run_readiness` READY（smoke／phase2 preflight mock）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 實作與上述測試對齊；**44 passed**（同上指令）。

**計畫狀態**：T16A（all-phase dry-run 骨架）已落地；建議下一項 **T10／T12** 依 MVP 優先序推進。

---

## 2026-04-10 CYCLE — precision_uplift Phase 2 T9（CLI + phase2 config + scaffold，/cycle_code 全四步）

> 計畫：`.cursor/plans/PLAN.md`（索引）+ `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T9**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder（本輪變更）

- **`investigations/precision_uplift_recall_1pct/orchestrator/config_loader.py`**
  - 新增 `validate_phase2_config` / `load_phase2_config`（`phase`、`common`、`resources`、`tracks` A/B/C、`gate`；`yaml run_id` 若存在須與 CLI `--run-id` 一致）。
- **`investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml`**
  - Phase 2 範例組態（對齊 runbook 草稿）。
- **`investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`**
  - 支援 `--phase phase2`：`common` → `run_preflight` 所需欄位；`dry-run` 額外檢查 `phase2/` 可寫；`--collect-only` 僅 preflight、不寫 `phase2_scaffold`；完整跑則寫入 `steps.phase2_scaffold`（註明 T10–T11 待實作）。
  - Phase 1 主流程抽成 `_main_phase1`；`main` 分流 `phase1` / `phase2`。
  - `run_dry_run_readiness(..., extra_writable=...)` 可選加寫入目標檢查。
- **`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**
  - T9 完成項勾選；DoD 註記 phase2 T9 scaffold 範圍。

#### 手動驗證

```bash
# Phase 2：需本機 paths + 兩 DB 表通過 preflight（範例路徑請改為實際存在）
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id my_phase2_run \
  --skip-backtest-smoke

# Dry-run（含 phase2 目錄可寫）
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase2.yaml \
  --run-id my_phase2_dry \
  --dry-run \
  --skip-backtest-smoke
```

#### 下一步建議

- **T10**：Phase 2 track runner + `phase2_bundle.json` + collectors。
- **T11**：`evaluate_phase2_gate` + `phase2/*.md` 報表。

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| Phase2 仍無實驗執行 | `phase2_scaffold` 易誤解為「Phase2 已跑完」 | README／runbook 標註 T10 前僅 config+preflight；報表勿引用空 bundle | 已測：run_state 含 scaffold message |
| `run_phase2.yaml` 預設路徑 | 範例 `out/models/...` 多數環境不存在 | 複製後改路徑；或 preflight 失敗屬預期 | 手動或整合測 with tmp_path（單元已覆蓋） |
| `tracks.experiments` 結構 | 目前只驗證 schema，不執行 | T10 實作時對齊 trainer CLI 契約 | golden：最小 phase2 yaml round-trip |
| `extra_writable` 命名碰撞 | 若與既有 `writable_*` 同名會覆蓋 artifacts | 命名空間前綴或禁止重複 key | 已測：`phase2_dir` 獨立 |
| Resume + phase 混用 | 同 `run_id` 先 phase1 後 phase2 可能混淆 | 文件建議不同 run_id；或可選偵測 `prev.phase` | 可選後續測試 |
| `phase2 --collect-only` | 與 phase1「仍跑 collect」語意不同 | 文件註明：phase2 在 T10 前無 collect，collect-only = 僅 preflight | 已測：`phase2_scaffold` 不存在 |

### STEP 3 — Tester（僅 tests）

- 擴充：`tests/unit/test_precision_uplift_phase1_orchestrator.py`
  - Phase2 schema：`track` 缺失、`run_id` mismatch、`build_phase2_input_summary` fingerprint
  - `run_dry_run_readiness(..., extra_writable=...)`
  - CLI：`--phase phase2` 寫入 `run_state` + `phase2_scaffold`
  - CLI：`--phase phase2 --collect-only` 不建立 `phase2_scaffold`

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 補上 phase2 `--collect-only` 行為後再跑 pytest；**40 passed**（同上指令）。

**計畫狀態**：Implementation Plan **T9 完成**；建議下一項 **T10（track runner + phase2_bundle）**。

---

## 2026-04-09 CYCLE — precision_uplift Phase1 Orchestrator MVP（T1+T2，STEP 1 Builder）

> 計畫來源：repo 根目錄無 `PLAN.md`；本輪依 `investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` Day 1（T1 骨架+CLI、T2 config+preflight）。已讀 `.cursor/plans/DECISION_LOG.md`（架構決策參考）。

### 本輪變更

- **`investigations/precision_uplift_recall_1pct/orchestrator/`**（新目錄）
  - `run_pipeline.py`：`--phase phase1`（其餘 phase 非 0）、`--config`、`--run-id`、`--collect-only`、`--resume`、`--skip-backtest-smoke`；修正 repo root 為 `orchestrator` 之 `parents[2]`；preflight 後寫入 `orchestrator/state/<run_id>/run_state.json`。
  - `config_loader.py`：YAML 載入與 Phase1 必要欄位驗證，缺漏拋 `E_CONFIG_INVALID`。
  - `runner.py`：`model_dir` / 兩 DB 路徑存在性；`prediction_log` DB 只讀開啟並檢查 `prediction_log` 表；state DB 檢查 `alerts`、`validation_results`；可選 `python -m trainer.backtester --help` smoke。
  - `config/run_phase1.yaml`：範例組態。
  - `collectors.py` / `evaluators.py` / `report_builder.py`：MVP stub（待 T4–T6）。
  - `__init__.py`：套件標記。

### 手動驗證

```bash
# 不支援的 phase → exit 2
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase2 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml \
  --run-id t0

# 使用範例 config（若本機無 state/prediction DB 則 preflight fail、exit 3，仍應寫入 run_state.json）
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 \
  --config investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml \
  --run-id t_manual \
  --skip-backtest-smoke
```

### 下一步建議

- MVP **T3**：`runner.py` 封裝 `run_r1_r6_analysis.py` 與 `python -m trainer.backtester`；stdout/stderr 落地與錯誤碼映射。
- 可選：將 `resume` 與 preflight 跳過策略文件化（目前僅在 preflight 已 success 時跳過）。

### STEP 2 — Reviewer（precision_uplift orchestrator T1+T2）

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 錯誤碼分類 | `backtester --help` 失敗時標成 `E_DB_UNAVAILABLE` 與 DB 問題混淆 | smoke 失敗改用專用碼（例如 `E_BACKTEST_CLI`）或訊息前綴區分 | 模擬 subprocess 非 0，斷言 error_code |
| Resume 過期狀態 | `--resume` 在舊 `run_state` 上跳過 preflight，DB/檔案已變更時不會重驗 | 記錄 `input_summary` hash 或 mtime；不符則強制重跑 preflight | resume 時改壞 DB path 須偵測 |
| 相對路徑基準 | `--config` 相對路徑固定對 repo root 解析；若使用者預期對 cwd 解析可能誤導 | 文件註明或以第二個 fallback 嘗試 `cwd / config` | 文件或單元測試約束行為 |
| 權限 / 唯讀 DB | `mode=ro` 開啟失敗時與檔案不存在訊息需區分 | 已分層；可再區分 `PermissionError` 文案 | 只讀目錄下 DB 的 MRE（若可移植） |
| Windows 路徑 | `resolve()` 與較長路徑在極少環境有邊角 | 維持 Path；整合測試在 win+posix 各跑一次 | CI matrix |
| `run_state` 合併 | 失敗 preflight 覆寫整段 `steps.preflight`，舊 checks 丟失 | 可 append `history[]` 或保留上一筆於 `previous_attempt` | 連續兩次失敗仍保留第一次 message |
| 佈局假設 | `parents[2]` 假設路徑為 `repo/investigations/.../orchestrator` | 若目錄被 symlink 或單檔複製會錯 root；可偵測 `Path('setup.py')` / `pyproject.toml` 向上尋找 | 以固定 temp tree 測 repo root 解析 |

### STEP 3 — Tester（僅 tests）

- 新增：`tests/unit/test_precision_uplift_phase1_orchestrator.py`
  - config 缺欄位、`prediction_log` 表缺失、min happy-path preflight（skip backtest smoke）、CLI 不支援 phase、subprocess mock backtest smoke、`run_state` 失敗仍寫入、resume 跳過 preflight 行為、`E_BACKTEST_CLI` 契約（需 STEP 4 實作對齊）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作對齊）

- **`runner.py`**：`trainer.backtester --help` 失敗時改為 **`E_BACKTEST_CLI`**（與 `E_DB_UNAVAILABLE` 區分）。
- **實跑**：`8 passed`（同上 pytest 指令）。

**MVP 任務清單後續建議**：T3 流程執行器（R1/R6 + backtest subprocess、錯誤碼映射、日誌落地）；T4 collectors；接續 T5–T7。可將 `resume` 與 `input_summary` 指紋對齊以降低 stale skip 風險。

---

## 2026-04-09 CYCLE 2 — precision_uplift Phase1 Orchestrator MVP（T3 流程執行，STEP 1 Builder）

> 計畫：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` T3。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

### 本輪變更

- **`orchestrator/runner.py`**
  - `run_logged_command`：subprocess stdout/stderr 寫入 `state/<run_id>/logs/{stem}.stdout.log` / `.stderr.log`（無 timeout 預設；可傳 `timeout_sec`）。
  - `run_phase1_r1_r6_all`：`run_r1_r6_analysis.py --mode all --pretty` + config 內 window、三條路徑；腳本預設 `investigations/test_vs_production/checks/run_r1_r6_analysis.py`，可用 YAML `r1_r6_script` 覆寫；失敗分類 → `E_NO_DATA_WINDOW` / `E_EMPTY_SAMPLE` / `E_ARTIFACT_MISSING`。
  - `run_phase1_backtest`：`python -m trainer.backtester --start/--end --model-dir`，預設 `--skip-optuna`（`backtest_skip_optuna: false` 可關）；`backtest_extra_args` 可附加參數。
  - `classify_r1_r6_failure` / `classify_backtest_failure`：純字串規則供測試與對齊。
- **`orchestrator/run_pipeline.py`**：preflight 成功且**未**帶 `--collect-only` 時依序執行 `r1_r6_analysis`、`backtest`；每步寫入 `run_state.json`；exit **4** / **5** 分別對應兩步失敗；`--resume` 可跳過已成功步驟。
- **`orchestrator/config/run_phase1.yaml`**：註解說明可選 `r1_r6_script`、`backtest_skip_optuna`、`backtest_extra_args`。
- **`tests/unit/test_precision_uplift_phase1_orchestrator.py`**：`test_resume_skips_preflight_when_previous_success` 補上 `--collect-only`，避免 T3 預設跑真實 subprocess。

### 手動驗證

```bash
# 預設：preflight → R1/R6 → backtest（若環境無 DB/CH 可能於 R1 或 backtest 失敗，仍應留下 logs/*.log）
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 \
  --config path/to/your_phase1.yaml \
  --run-id my_run

# 僅 preflight + collect stub（與先前行為一致）
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 --config path/to/your_phase1.yaml --run-id my_run --collect-only \
  --skip-backtest-smoke
```

### 下一步建議

- **T4**：實作 `collectors.py`（`backtest_metrics.json`、R1/R6 JSON、state.db 統計）；可將 R1/R6 stdout 另存 `.json` 供 collector 解析。
- 可選：`r1_r6_analysis` / `backtest` 的 wall-clock 上限與可設定 `timeout_sec` 自 YAML。

### STEP 2 — Reviewer（T3）

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 無上限執行時間 | R1/R6 / backtest 可能數小時；`subprocess.run` 預設無 timeout | YAML `step_timeouts_sec` 或環境變數；預設維持 None | 傳短 timeout 斷言 `E_NO_DATA_WINDOW` 或專用 timeout 碼 |
| 錯誤分類誤判 | 未列到的 traceback 一律 `E_EMPTY_SAMPLE` / `E_NO_DATA_WINDOW` 可能误导 | 擴充關鍵字或逐步改為 JSON exit reason | 已知子字串分類表 golden tests |
| Resume 與設定漂移 | 只認 `status==success`，換 config 仍跳過步驟 | 對 `input_summary` 做 hash，不符強制重跑 | 換 window 後 resume 須重跑 |
| 日誌磁碟 | 長跑 stdout 肥大 | 輪轉或 tee 壓縮；MVP 先文件提醒 | — |
| Windows | `newline=""` 已用；路迳含空白 | 維持 list argv、勿 shell=True | CI windows job |
| 敏感資訊 | log 含 DB path / stack | 文件提醒勿 commit `state/` | `.gitignore` 檢查 |

### STEP 3 — Tester（僅 tests）

- 擴充：`tests/unit/test_precision_uplift_phase1_orchestrator.py`
  - `classify_r1_r6_failure` / `classify_backtest_failure` 三類場景（`E_NO_DATA_WINDOW`、`E_EMPTY_SAMPLE`、`E_ARTIFACT_MISSING`）
  - `run_logged_command` 寫入 `.stdout.log`
  - `run_phase1_r1_r6_all` 腳本不存在 → `E_ARTIFACT_MISSING`
  - （契約修正）`test_resume_skips_preflight_when_previous_success` 加上 `--collect-only`，避免預設 pipeline 真實 subprocess。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 本輪無需額外修改實作以通過新測試；本機實跑：**14 passed**（同上 pytest 指令）。

**MVP 任務清單後續**：**T4 collectors**；再 **T5 Gate**、**T6 報表**、**T7 resume 強化**（config 指紋）。

---

## 2026-04-09 CYCLE 3 — precision_uplift Phase1 Orchestrator MVP（T4 Collectors，STEP 1–4）

> 計畫：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T4**。根目錄無 `PLAN.md`；架構決策見 `.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder（本輪變更）

- **`orchestrator/collectors.py`**：`collect_phase1_artifacts` 讀取
  - `backtest_metrics.json`（預設 `trainer/out_backtest/...`，可用 `backtest_metrics_path` 覆寫）
  - `state/<run_id>/logs/r1_r6.stdout.log`（`--pretty` JSON）、可選同目錄 **`r1_r6_mid.stdout.log`**
  - `state.db` 之 `validation_results`：`finalized_alerts_count`（`validated_at` 非空）、`finalized_true_positives_count`（`result=1`，窗內 `alert_ts`）
  - 缺檔／JSON 失敗寫入 **`errors[]`**（`E_COLLECT_BACKTEST_METRICS` / `E_COLLECT_R1_PAYLOAD` / `E_COLLECT_STATE_DB`），不靜默吞掉。
- **`collect_summary_for_run_state`**：`run_state.json` 用精簡摘要。
- **`run_pipeline.py`**：每次成功跑完 preflight（與非 `--collect-only` 時之 R1/backtest）後，寫 **`collect_bundle.json`**，並把 `collect` / `collect_bundle_path` 併入 `run_state.json`。
- **`config/run_phase1.yaml`**：註解 `backtest_metrics_path`。

### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 --config <your.yaml> --run-id my_run --collect-only --skip-backtest-smoke
# 檢查：orchestrator/state/my_run/collect_bundle.json 與 run_state.json 內 collect 摘要
```

### 下一步建議

- **T5**：`evaluators.py` 依 `bundle` + `thresholds` 產出 PASS/PRELIMINARY/FAIL。
- **T6**：`report_builder` 消耗 `collect_bundle` 填 phase1 工件。

### STEP 2 — Reviewer（T4）

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `alert_ts` 字串比較 | SQLite 以文字比較窗邊界，非 ISO-8601 排序可能錯 | 文件要求與 config 相同格式；長遠改 TIMESTAMP | 邊界列 included/excluded |
| `collect_bundle` 體積 | R1 payload 可能極大 | 另存 gzip 或只存路徑＋摘要 | — |
| `validation_results` 無 `alert_ts` | 改為全表計數並設 `note` | 已在實作 | 無 alert_ts 的 schema fixture |
| 雙重錯誤 | 同時缺 metrics 與 r1 時 `errors` 多筆 | 維持；evaluator 取第一個 blocking | — |
| 權限 | `collect_bundle.json` 寫入失敗目前未獨立 catch | try/except 寫入並回 `E_COLLECT_WRITE` | mock 唯讀目錄 |

### STEP 3 — Tester（僅 tests）

- 擴充：`tests/unit/test_precision_uplift_phase1_orchestrator.py`（T4：整合 backtest+r1+state、缺檔 errors、optional mid log、`collect_summary_for_run_state`）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 無需額外修改實作；**18 passed**（同上）。

**MVP 下一項**：**T5 Gate evaluator** → **T6 報表** → **T7 resume 指紋**。

---

## 2026-04-09 CYCLE 4 — precision_uplift Phase1 Orchestrator MVP（T5 Gate，STEP 1–4）

> 計畫：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T5**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder（本輪變更）

- **`orchestrator/evaluators.py`**：`evaluate_phase1_gate(bundle)` 產出 **`PASS` / `PRELIMINARY` / `FAIL`**、`blocking_reasons[]`、`evidence_summary`、精簡 **`metrics`**。
  - **FAIL**：`collect` errors、缺 `r1_r6_final` payload、`r2_prediction_log_vs_alerts` 與 heuristic 不合、mid/final `precision_at_target_recall` 差距 > `gate_pat_abs_tolerance`（預設 0.15，可寫入 thresholds）。
  - **PRELIMINARY**：觀測窗 < `min_hours_preliminary`、樣本低於 preliminary、未達 gate 時/樣本、或達 gate 但缺 **mid** R1 snapshot（無法做方向一致檢查）。
  - **PASS**：達 gate 時長與 `finalized_alerts` / TP 門檻，且 mid+final PAT 在容忍範圍內。
- **工具函數**：`window_duration_hours`、`extract_precision_at_target_recall`（讀 `unified_sample_evaluation` 或 `evaluate`）。
- **`run_pipeline.py`**：每次寫入 `collect_bundle` 後計算 Gate，將 **`gate_decision`** 寫入 `run_state.json`（`--collect-only` 與完整流程皆同）。
- **`config/run_phase1.yaml`**：註解 `gate_pat_abs_tolerance`。

### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 --config <your.yaml> --run-id my_run --collect-only --skip-backtest-smoke
# 檢查 run_state.json → gate_decision.status / blocking_reasons
```

### 下一步建議

- **T6**：`report_builder.py` 渲染 `phase1_gate_decision.md`（與其他工件）並嵌入 `gate_decision` + `collect_bundle` 摘要。

### STEP 2 — Reviewer（T5）

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| R2 門檻啟發式 | `max(50, 0.25*n_pl)` 可能誤判小流量 | 改為僅相對差、或 YAML 可調 | 邊界 n_pl、diff |
| 缺 mid 一律 PRELIMINARY | 與「只跑單次 R1」營運不相容 | 增 `gate_require_mid_snapshot: false` | 旗標開關 |
| ISO 解析 | `fromisoformat` 對非標準尾綴失敗 | 集中一處 parse + 清楚錯誤 | 非法 timestamp |
| PAT 缺欄 | 舊 payload 形狀不同 → FAIL | 擴充 extract 路徑 | branches-only fixture |
| 與 runbook 語意 | 「方向一致」目前僅 PAT 純量差 | 可再加入 censored 或 slice 指標 | 文件對齊 |

### STEP 3 — Tester（僅 tests）

- 擴充：`tests/unit/test_precision_uplift_phase1_orchestrator.py`（T5：窗長、extract、collect error→FAIL、短窗 PRELIMINARY、PASS、PAT 發散 FAIL、缺 mid PRELIMINARY、R2 mismatch FAIL）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 首跑 gate 測試時 `tp=40` 低於預設 `min_finalized_true_positives_gate=50`，已**更正測試期望**（不改 Gate 公式）；**26 passed**（同上 pytest）。

**MVP 下一項（CYCLE 4 結束時）**：**T6 報表渲染** → **T7 resume / bundle 指紋**。

---

## 2026-04-09 CYCLE 5 — precision_uplift Phase1 Orchestrator MVP（T6 報表，STEP 1–4）

> 計畫：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T6**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder（本輪變更）

- **`orchestrator/report_builder.py`**：實作 `write_phase1_reports`，寫入五份工件 + **`status_history_crosscheck.md`** 之 orchestrator 區塊：
  - `upper_bound_repro.md`：`backtest_metrics` JSON、`training_artifact_baseline`（R1 payload）
  - `label_noise_audit.md`：`unified_sample_evaluation`（略去過大 `by_model_version` 可另段）、R1 final 摘要
  - `slice_performance_report.md`：`state_db_stats`、R2、`errors`
  - `point_in_time_parity_check.md`：資料來源路徑 + 人工核對 scaffold
  - `phase1_gate_decision.md`：`gate` status / reasons / evidence / metrics
  - `status_history_crosscheck.md`：以 `<!-- ORCHESTRATOR_RUN_NOTE_* -->` **置換**（保留檔案上方人工內容）；檔案不存在時建立範本
- **`run_pipeline.py`**：每次成功 collect + gate 後**一律**呼叫 `write_phase1_reports`（不再僅 `--collect-only`）。

### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 --config <your.yaml> --run-id my_run --collect-only --skip-backtest-smoke
ls investigations/precision_uplift_recall_1pct/phase1/*.md
```

### 下一步建議

- **T7**：resume 與 config / bundle 指紋、step 細粒度 `pending/running/...`。

### STEP 2 — Reviewer（T6）

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| 覆寫整檔 | 五份 md 每次全寫入，會蓋掉人工在該檔的編輯 | 改為只更新 ORCH 區塊或拆 `phase1/generated/` | 文件政策 + 測試只碰 tmp |
| JSON 體積 | `_json_fence` 截斷仍可能很大 | 再降 `max_chars` 或只列 keys | — |
| 標記腐蝕 | 使用者刪半段 marker 導致重複 append | validate + 修復工具 | 壞檔 fixture |
| 非 UTF-8 舊檔 | `read_text` 失敗 | `errors=` 或略過 merge | — |
| Gate 中文 | `phase1_gate_decision` 混中英 | 統一用繁中區塊標題 | lint 文案 |

### STEP 3 — Tester（僅 tests）

- 擴充：`tests/unit/test_precision_uplift_phase1_orchestrator.py`（六檔皆寫出、`status_history` 重跑仍單一 ORCH 區塊）。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 無需額外修改實作；**28 passed**（同上 pytest）。

**MVP 下一項**：**T7 run_state / resume 強化**（step 狀態細粒度、指紋）。

---

## 2026-04-09 CYCLE 6 — precision_uplift Phase1 Orchestrator MVP（T7 run_state / resume，STEP 1–4）

> 計畫：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T7**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder（本輪變更）

- **`run_pipeline.py`**
  - **`build_input_summary`**：`input_summary` 含完整 `thresholds`、常用可選鍵、`config_path`、**`fingerprint`**（SHA256）。
  - **Resume**：若 `--resume` 且舊 `input_summary.fingerprint` ≠ 目前組態，視為 **mismatch**：stderr 提示、設定 `resume_invalidated`，**不**跳過 preflight / R1 / backtest；一致時維持既有 skip。
  - **Step 狀態**：`r1_r6_analysis` / `backtest` 先 `running`（含 `started_at`）再 `success`/`failed`（含 `finished_at`）；`preflight` 有 `finished_at`；新增終端步驟 **`collect`**、**`reports`**（`success` + `artifacts`）。
  - **`artifacts`**：匯總 `run_state`、`logs_dir`、`collect_bundle`、`phase1_dir`、`config_path`。
  - 移除未使用之 `_config_summary`。

### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 --config <your.yaml> --run-id my_run --collect-only --skip-backtest-smoke
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 --config <your.yaml> --run-id my_run --resume --collect-only --skip-backtest-smoke
# 檢查 run_state.json：input_summary.fingerprint、steps.*.status、artifacts
```

### 下一步建議

- DoD 核銷：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` §3 驗收清單逐項實測；必要時補 **dry-run** 或 **--force** 旗標。

### STEP 2 — Reviewer（T7）

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `config_path` 入指紋 | 同內容換路徑即 mismatch | 改只 hash 內容；或文件要求固定路徑 | 已測：兩路徑同內容指紋不同 |
| `running` 中途崩潰 | 程式被 kill 時 state 停於 running | 下次起 run 清除或視為 failed | 手動 kill integration |
| 舊 run_state 無 fingerprint | 一律視為 mismatch | 與現行行為一致 | resume 測已用新 summary |
| `collect`/`reports` 無 running | 與 T3 subprocess 步驟不对称 | 可選 `running` 包裝 | — |
| `resume_invalidated` | 成功 run 後需清除 | 已 `pop` 當指紋一致 | — |

### STEP 3 — Tester（僅 tests）

- 擴充：`tests/unit/test_precision_uplift_phase1_orchestrator.py`
  - `build_input_summary`：門檻值變更 → 指紋變；**不同 `config_path` → 指紋變**。
  - resume 契約：`run_state` 須含與現行 YAML 一致之 **`input_summary`（含 fingerprint）**。

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- **30 passed**（同上 pytest）。
- **備註**：任務書列 `pending`；目前以 **`running` → `success`/`failed`** 為主，未單獨落地 `pending`（尚未執行之步驟即為不存在或即將被 `running` 覆寫）。

**MVP**：T1–T7 條目已完成；建議進入 **§3 DoD 整體驗收** 與 runbook 對照。

---

## 2026-04-09 CYCLE 7 — precision_uplift Phase1 Orchestrator MVP（T8 dry-run，STEP 1–4）

> 計畫：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **T8**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

### STEP 1 — Builder（本輪變更）

- **`orchestrator/runner.py`**
  - 新增 `run_r1_r6_cli_smoke(...)`：執行 `run_r1_r6_analysis.py --help`，用於 dry-run 可啟動性檢查（非長跑）。
- **`orchestrator/run_pipeline.py`**
  - 新增 CLI：`--dry-run`。
  - 新增 `run_dry_run_readiness(...)`：
    - `r1_r6_cli_smoke`
    - `backtester_cli_smoke`（可被 `--skip-backtest-smoke` 跳過）
    - `state_dir/logs_dir/phase1_dir` 可寫入檢查
  - dry-run 僅輸出 readiness，不執行 R1 全量分析、backtest 長跑、collect/gate/report。
  - `run_state.json` 新增：
    - `mode: dry_run`
    - `readiness.status`（`READY` / `NOT_READY`）
    - `readiness.checks[]`
    - step `dry_run_readiness`
  - 若 `NOT_READY`：stderr 印出 blocking reasons，exit code **6**。

### 手動驗證

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 --config <your.yaml> --run-id dryrun_probe --dry-run

# 若環境不允許 backtester smoke，可先跳過：
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 --config <your.yaml> --run-id dryrun_probe --dry-run --skip-backtest-smoke
```

### 下一步建議

- 進行 §3 DoD 實測核銷：特別是 production 上 dry-run 的 READY/NOT_READY 判定一致性。

### STEP 2 — Reviewer（T8）

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| smoke 仍可能慢 | `--help` 若 import 很重仍可耗時 | 後續可加 timeout config | 模擬 timeout |
| 可寫路徑測試副作用 | 建立/刪除 probe file | 已採最小副作用；可加前綴避免誤判 | 只讀目錄 fixture |
| exit code 6 | 與既有錯誤碼語意需文件化 | 在 runbook 加上 dry-run code 表 | CLI doc 檢查 |
| skip_backtest_smoke | 可能誤以為 READY 完整 | readiness.checks 保留 skipped 訊息 | assert skipped message |

### STEP 3 — Tester（僅 tests）

- 擴充：`tests/unit/test_precision_uplift_phase1_orchestrator.py`
  - `run_r1_r6_cli_smoke` 缺腳本 fail-fast
  - `run_dry_run_readiness` READY / NOT_READY
  - CLI dry-run NOT_READY 回傳碼 `6` 並寫入 `run_state.mode/readiness`

```bash
python -m pytest tests/unit/test_precision_uplift_phase1_orchestrator.py -q --tb=short
```

### STEP 4 — Tester（實作）

- 無需額外修 production；**34 passed**（同上 pytest）。

**MVP**：T1–T8 已完成；建議進入 tasklist §3 DoD 實環境核銷與正式上線前演練。

## 2026-04-07 CYCLE — CONSOLIDATED_PLAN §B 訓練速度與成本（STEP 1 Builder）

### 本輪變更

- **`trainer/training/trainer.py`**：`_parquet_stable_rowgroups_schema_digest` 以 `len(meta.schema)` 作為欄位數（不再讀 `FileMetaData.num_columns`）。部分 PyArrow 建置上 `num_columns` 會連到已移除的 `ParquetSchema.num_columns`，造成 `pq.read_metadata` 後計 digest 失敗、log 出現 `read_metadata failed ... num_columns`，`data_hash` 退化成全零 digest，Chunk / prefeatures cache 指紋失真。

### 手動驗證

```bash
python -m pytest tests/unit/test_task7_chunk_cache_key.py \
  tests/review_risks/test_task7_r5_local_metadata_review_risks_mre.py \
  tests/review_risks/test_task7_dod_chunk_cache_stats_review_risks_mre.py -q --tb=short
```

（本機：`39 passed`。）

### 下一步建議

- §B 續作：Dynamic K Phase-A，或 Chunk Cache 跨機複製命中之實測紀錄；push 前再跑專案 pre-commit（ruff/mypy/pytest 全套）。

### STEP 2 — Reviewer（本輪變更）

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|-----------|
| 極舊 PyArrow | 若某版 `ParquetSchema` 不實作 `__len__`，`len(schema)` 可能失敗 | 與 CI 下限版本對照；必要時 `try/except` 改以 `schema.num_columns` 僅在屬性存在時 fallback | 在最低支援 pyarrow 的 matrix job 跑 `test_task7_chunk_cache_key` |
| 毀損 footer | 異常 metadata 下 `len(schema)` 與 row group 不一致 | 維持現狀：digest 僅用於快取鍵，錯檔更傾向 cache miss | 可選：餵損壞 parquet 預期落入 `_file_token` 警告分支 |
| 雜湊碰撞 | 與既有 fp_v2 文件相同：僅 metadata 層級指紋 | 無須改程式；營運仍以「寧可 miss」為預期 | 既有 bounds / row 變更測試已覆蓋 |

### STEP 3 — Tester（僅 tests）

- 新增：`tests/unit/test_task7_chunk_cache_key.py` 內 `test_parquet_stable_rowgroups_schema_digest_succeeds_on_minimal_file`，直接對 `pq.read_metadata` 呼叫 `_parquet_stable_rowgroups_schema_digest`，斷言 16 hex 且非全零，避免回歸到 `ParquetSchema.num_columns` 路徑。

```bash
python -m pytest tests/unit/test_task7_chunk_cache_key.py::TestTask7ChunkCacheKey::test_parquet_stable_rowgroups_schema_digest_succeeds_on_minimal_file -q
```

### STEP 4 — Tester（實作）

- 無需再改 production（STEP 1 已修）；以 pytest 子集確認。

```bash
python -m pytest tests/unit/test_task7_chunk_cache_key.py -q --tb=short
```

→ **2026-04-07 實跑**：`18 passed`.

**計畫後續（CONSOLIDATED §B）**：可攜式指紋 + R6 預設已與本修對齊；建議下一項優先 **Dynamic K Phase-A** 或 **R6 跨機 cache hit 實測紀錄**；GPU 項補 **CPU vs GPU benchmark** 數據至既有 runbook。

---

## 2026-04-15 CYCLE — Phase1 PIT parity MVP wiring（STEP 1 Builder）

> 計畫索引：`.cursor/plans/PLAN.md`；決策：`.cursor/plans/DECISION_LOG.md`。本輪依 `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` 新增之 **P1 parity 最小規格**，先做前兩步（collector + report），不一次做完 gate 阻斷。

### STEP 1 — Builder

- **`investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`**
  - 新增 ` _collect_phase1_pit_parity(...)`（prediction/state DB 的 PIT 自動診斷，非阻斷）：
    - `scored_at_in_window_ratio`
    - `validated_at_non_null_ratio`
    - `alerts_vs_prediction_log_gap`
    - `status` / `reasons[]` / `note`
  - `collect_phase1_artifacts(...)` 新增 bundle 鍵：`pit_parity`
  - `collect_summary_for_run_state(...)` 新增 `pit_parity_status` 與兩個 ratio 摘要欄位。
- **`investigations/precision_uplift_recall_1pct/orchestrator/report_builder.py`**
  - `point_in_time_parity_check.md` 新增 `## PIT parity metrics (auto)`，自動渲染 `bundle["pit_parity"]` JSON。
- **`investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml`**
  - 補上 parity 相關 threshold/mode 註解（目前作為規格提示；gate enforce 下一步接）。

#### 手動驗證

1. 執行 phase1 collect（可 `--collect-only`），確認 `collect_bundle.json` 出現 `pit_parity` 區塊。  
2. 檢查 `results/<run_id>/reports/phase1/point_in_time_parity_check.md` 是否含 `PIT parity metrics (auto)`。  
3. 檢查 `run_state.json` 的 `phase1_collect` 摘要是否含 `pit_parity_status` 與 ratio 欄位。

#### 下一步建議

- 將 `pit_parity_mode`（`STRICT`/`WARN_ONLY`）與三個 threshold 真正接到 `evaluate_phase1_gate(...)`，完成 gate 阻斷策略。
- 補齊對應測試（`STRICT fail`、`WARN_ONLY pass with warning`、`missing column -> warn`）。

---

### STEP 2 — Reviewer

| 風險 | 說明 | 建議 | 建議測試 |
|------|------|------|----------|
| `validated_at_non_null_ratio` 未套觀測窗 | 目前以 `validation_results` 全表計算，長期資料會稀釋當窗異常 | 改為優先用 `alert_ts` 同窗計算；缺 `alert_ts` 才 fallback 全表並加 `reason` | `test_collect_phase1_pit_parity_uses_windowed_validation_ratio_when_alert_ts_exists` |
| `alerts_vs_prediction_log_gap` 來源不一致風險 | 目前以 `prediction_log.scored_at` vs `alerts.ts` 計數，若語意漂移只會 `warn` | 在 `pit_parity` 補 `counts` 子欄位（兩邊原始 count）方便 reviewer 比對 | `test_collect_phase1_pit_parity_gap_includes_both_counts` |
| 時區 mismatch 尚未機械化 | `window_timezone_mismatch_count` 目前固定 `None` + note | 後續若 schema 有時區欄位，補真正 mismatch 計數；否則維持 `warn` 並文件化限制 | `test_collect_phase1_pit_parity_timezone_count_none_with_reason` |
| `prediction_log` 表缺失情境 | `PRAGMA table_info(prediction_log)` 在缺表時回空，不會丟錯 | 現行 `reason` 可判讀，但建議加一個明確 reason（例如 `prediction_log_table_missing`） | `test_collect_phase1_pit_parity_warns_when_prediction_log_table_missing` |
| Gate 尚未吃 parity 門檻 | 本輪是 collector/report wiring，仍可出現 `PASS + pit warn` | 下一步接 `pit_parity_mode` 進 `evaluate_phase1_gate`，`STRICT` 時阻斷 | `test_evaluate_phase1_gate_strict_blocks_on_pit_parity_violation` |

### STEP 3 — Tester（lint/typecheck 規則化，未改 production）

> 依本 workspace 規範，本輪不新增/改動 `tests/` 內容；先把 Reviewer 風險轉為最小可執行規則，供下一輪落地單元測試。

- **規則 R1（PIT 區塊必存在）**  
  - 目標：避免回退成純 scaffold。  
  - 指令：`rg "## PIT parity metrics \\(auto\\)" investigations/precision_uplift_recall_1pct/orchestrator/report_builder.py`
- **規則 R2（Bundle 必帶 pit_parity）**  
  - 目標：確保 collector wiring 未遺失。  
  - 指令：`rg "\"pit_parity\": pit_parity|pit_parity_status" investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`
- **規則 R3（Phase1 config 文件需含 parity threshold 註解）**  
  - 目標：避免 config 契約漂移。  
  - 指令：`rg "pit_parity_mode|min_scored_at_in_window_ratio|min_validated_at_non_null_ratio|max_alert_prediction_gap_abs" investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml`
- **規則 R4（Gate 仍未 enforce parity，需明示）**  
  - 目標：避免 reviewer 誤判 `PASS` 含 parity 通過。  
  - 指令：`rg "pit_parity_mode|pit_parity_violation" investigations/precision_uplift_recall_1pct/orchestrator/evaluators.py`（目前預期無命中，作為待辦提醒）

#### 建議下一輪新增測試（待 `tests/` 開窗後）

- `test_collect_phase1_pit_parity_uses_windowed_validation_ratio_when_alert_ts_exists`
- `test_collect_phase1_pit_parity_warns_when_prediction_log_table_missing`
- `test_write_point_in_time_parity_includes_auto_metrics_block`
- `test_evaluate_phase1_gate_strict_blocks_on_pit_parity_violation`

### STEP 4 — Tester（修實作，不改 tests）

- **`investigations/precision_uplift_recall_1pct/orchestrator/evaluators.py`**
  - `evaluate_phase1_gate(...)` 新增 parity threshold 讀取與 mode 行為：
    - `pit_parity_mode`（`STRICT` / `WARN_ONLY`，預設 `WARN_ONLY`）
    - `min_scored_at_in_window_ratio`
    - `min_validated_at_non_null_ratio`
    - `max_alert_prediction_gap_abs`
  - `STRICT` 下 parity violation 會 `FAIL`（`pit_parity_violation` + 細項 reason）。
  - `WARN_ONLY` 下不阻斷，但 evidence 與 metrics 會附 `pit_status`/violation 訊息。
- **`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`**
  - P1 parity 子項目狀態更新：collector / report / config / gate wiring 已勾選；測試項維持待辦。

#### 檢查結果

- `ReadLints`：本輪修改檔案 **無 IDE lint 錯誤**。
- 依 workspace 規範，本輪未執行 `pytest/ruff/mypy`（quality tools 建議在 push 前統一跑）。

#### Plan item 狀態更新（本輪）

- **P1 parity 最小規格**：主要程式 wiring（collector + report + gate mode）已完成。
- **P1 parity DoD**：測試項尚未完成（待下一輪補測）。

#### 下一步建議（Plan）

1. 補上 3 個 parity 單元測試（`STRICT` / `WARN_ONLY` / missing column）。  
2. 將 `validated_at_non_null_ratio` 調整為「優先窗內計算，缺欄才 fallback 全表」。  
3. 在 `pit_parity` 加入雙邊原始 count（alerts / prediction_log）提升稽核可讀性。


