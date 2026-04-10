# 一次性 Ad-hoc 執行方案與腳本實作計畫

> 適用情境：已有訓練完成模型，但尚未開始 backtest / scorer / validator 蒐證。  
> 目標：以**單次調查 run**收齊證據，並落地腳本化執行（逐步替代手動流程）。

---

## 1) 一次性 Ad-hoc 執行方案（原 PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN §8）

### 1.1 Run 定義（先固定，不可中途漂移）

請先建立一個 run 識別（例如 `phase1_adhoc_YYYYMMDD`），並固定：

- `model_version` / `model_dir`
- `STATE_DB_PATH`
- `PREDICTION_LOG_DB_PATH`
- 調查觀測窗（`start_ts`, `end_ts`, 時區統一 HKT）
- 主要契約：`precision@recall=1%`、censored 排除規則、validator 口徑

> 原則：run 期間不更換模型、不改 threshold 策略、不改標籤契約；避免結論不可比較。

### 1.2 執行順序（一次跑完）

1. **Dry-run 快檢（production 前 2~10 分鐘）**
  - 先跑 orchestrator `--dry-run`，確認 config / 路徑 / DB / 命令可啟動性。
  - 僅做 readiness 檢查，不產生正式結論。
2. **Preflight（15~30 分）**
  - 驗證路徑與連線可用（model / state DB / prediction DB / ClickHouse）。
  - 先跑一個短窗 backtest（可先 `--skip-optuna`）確認輸出正常。
3. **啟動線上蒐證（長跑）**
  - 啟動 `scorer`（持續寫 prediction/alerts）。
  - 啟動 `validator`（持續寫 validation 與 precision 快照）。
  - 兩者保持同一 run 設定，不中途切換模型與 DB。
4. **中途健康檢查（建議 T+6h）**
  - 手動執行一次 `run_r1_r6_analysis.py --mode all`。
  - 目的：提早發現空樣本、censored 欄位缺失、window/DB 指向錯誤。
5. **結束前採樣（終點）**
  - 再執行一次 `run_r1_r6_analysis.py --mode all` 取得最終 payload。
  - 跑一次固定窗口 backtest 產出最終 `backtest_metrics.json`。
6. **彙整工件**
  - 將輸出填入 `phase1/` 六份工件，完成 `phase1_gate_decision.md`。

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
2. **建議用於 Phase 1 Gate：72~~120 小時（3~~5 天）**
  - 用途：可做較可靠主因排序與是否重排判斷。
  - 建議資料量：
    - finalized alerts >= 800（理想 >= 1000）
    - 主要切片各有足夠樣本（避免切片結論只由噪音驅動）
3. **若要跨週期穩定性結論：>= 7 天**
  - 用途：納入工作日/週末行為差異，降低單窗偏誤。

> 評語：若僅跑 6~12 小時就判定「模型不行/資料不行」，風險非常高；在 recall=1% 稀疏場景，這通常會導致錯誤決策。

### 1.5 停止條件與延長條件

- **可停止並進 Gate**（全部滿足）：
  - 達到 1.4 的「建議用於 Gate」時長與資料量
  - `run_r1_r6_analysis` 兩次結果方向一致（非劇烈反轉）
  - censored / delayed label 指標波動進入可解釋範圍
- **必須延長觀測**（任一成立）：
  - finalized alerts 不足（< 300 初判門檻）
  - 切片樣本嚴重不均、top-band 幾乎無可用標記
  - scorer/validator 中途改參數或中斷，造成 run 契約破壞

### 1.6 資源與效能保護（筆電/有限資源必做）

- `run_r1_r6_analysis.py` 先用較保守參數（例如 `sample_size` 從小到大）。
- `autolabel` 的 player chunk 不要一次拉太大，避免 ClickHouse 壓力尖峰。
- backtest 日常蒐證優先 `--skip-optuna`，將重型搜索留到補充實驗。
- 每次 ad-hoc 命令要保留輸出 payload（JSON）與 run_id，避免不可追溯。

---

## 2) 腳本化實作計畫（Implementation Plan）

### 2.1 目標與範圍

- 目標：將目前手動流程改為可重跑、可中斷續跑、可生成工件的 orchestrator。
- 範圍：優先自動化 Phase 1；Phase 2~4 以同一框架擴充。

### 2.2 建議檔案結構

- `investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/config_loader.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/runner.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/collectors.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/evaluators.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/report_builder.py`
- `investigations/precision_uplift_recall_1pct/orchestrator/templates/*.md.j2`
- `investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml`
- `investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/run_state.json`

### 2.3 CLI 設計

- `python run_pipeline.py --phase phase1 --config config/run_phase1.yaml`
- `python run_pipeline.py --phase phase2 --config config/run_phase2.yaml`
- `python run_pipeline.py --phase all --config config/run_full.yaml`
- `python run_pipeline.py --phase phase1 --resume --run-id <run_id>`
- `python run_pipeline.py --phase phase1 --collect-only`
- `python run_pipeline.py --phase phase1 --dry-run --config config/run_phase1.yaml --run-id <run_id>`

### 2.4 核心流程（Phase 1 MVP）

1. （可選）`--dry-run`：只做 readiness 檢查，輸出 `READY / NOT_READY`。
2. 載入 config + 驗證 schema（缺欄位直接 fail）。
3. 執行 preflight（路徑、DB、必要表、模型 artifact）。
4. 啟動 scorer/validator 子程序（或 attach 現有程序）。
5. 監控觀測時長與樣本量門檻（時間 + finalized alerts + TP）。
6. 在 T+6h 跑中途 `run_r1_r6_analysis --mode all`。
7. 在終點跑最終 `run_r1_r6_analysis --mode all` + backtest。
8. 收集 JSON/CSV/DB 指標，產出 `phase1/*.md`。
9. 計算 Gate 狀態（PASS / PRELIMINARY / FAIL）並寫入 `phase1_gate_decision.md`。

### 2.5 Gate 引擎（建議）

- `PRELIMINARY`：達到最短時長（48h）但未達建議樣本量。
- `PASS`：達到建議時長與樣本量，且兩次 R1/R6 方向一致。
- `FAIL`：關鍵資料缺失、口徑衝突、或指標明確不達條件。

### 2.6 錯誤碼與 fail-fast

- `E_CONFIG_INVALID`
- `E_DB_UNAVAILABLE`
- `E_NO_DATA_WINDOW`
- `E_EMPTY_SAMPLE`
- `E_GATE_NOT_READY`
- `E_ARTIFACT_MISSING`

### 2.7 效能與穩定性（必做）

- production 長跑前先做 `--dry-run`，避免連線/路徑問題延遲到長跑中後段才發現。
- 預設保守參數：
  - `sample_size` 先小（如 1000）
  - `player_chunk_size` 100~200
  - backtest 預設 `--skip-optuna`
- 大檔優先 parquet；避免全量讀入記憶體。
- 所有步驟寫入 `run_state.json`（可 resume）。

### 2.8 分階段落地里程碑

1. **MVP（2~3 天）**：Phase 1 自動化（可跑、可產工件、可判 Gate）
2. **V2（3~5 天）**：Phase 2 track runner + gate 決策
3. **V3（3~5 天）**：Phase 3/4 串接與最終 Go/No-Go 包輸出
4. **V4（1~2 天）**：健壯性（resume、重試、錯誤碼、模板優化）

---

## 3) 與主計畫文件的分工

- `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md`：保留高層目標、Gate、里程碑、最終 runbook摘要。
- 本文件：承接 ad-hoc 細節與腳本實作藍圖，作為「執行手冊 + 開發規格」。

