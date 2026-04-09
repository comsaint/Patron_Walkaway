# Code Review: ClickHouse 臨時表方案 (load_player_profile)

> **歸檔**：較舊段落已移至 [.cursor/plans/archive/STATUS_archive.md](.cursor/plans/archive/STATUS_archive.md)。

---

## 2026-04-09 CYCLE — precision_uplift Phase1 Orchestrator MVP（T1+T2，STEP 1 Builder）

> 計畫來源：repo 根目錄無 `PLAN.md`；本輪依 `investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` Day 1（T1 骨架+CLI、T2 config+preflight）。已讀 `.cursor/plans/DECISION_LOG.md`（架構決策參考）。

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

> 計畫：`PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` T3。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

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

> 計畫：`PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` **T4**。根目錄無 `PLAN.md`；架構決策見 `.cursor/plans/DECISION_LOG.md`。

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

> 計畫：`PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` **T5**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

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

> 計畫：`PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` **T6**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

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

> 計畫：`PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` **T7**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

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

- DoD 核銷：`PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` §3 驗收清單逐項實測；必要時補 **dry-run** 或 **--force** 旗標。

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

> 計畫：`PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md` **T8**。根目錄無 `PLAN.md`；`DECISION_LOG.md` 見 `.cursor/plans/DECISION_LOG.md`。

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
