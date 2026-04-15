# Precision 提升調查指南

本資料夾用於執行並留存「`precision@recall=1%` 提升計畫」的全部調查證據與決策紀錄。

## 目標

- 將 `precision@recall=1%` 由約 40% 提升至 `>=60%`。
- 提升結果需在多時間窗（forward/purged）下仍穩定成立。

## 先看哪些文件

- 總計畫（SSoT）：`.cursor/plans/PLAN_precision_uplift_sprint.md`
- 執行儀表板與 Phase 1~4 runbook：`PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md`
- Ad-hoc 流程與腳本實作藍圖：`PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md`
- Phase 1 Orchestrator MVP 開發任務：`PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md`

---

## 四個 Phase 在做什麼（高階）

四個階段回答**不同層次的問題**：先釐清「值不值得、該往哪裡用力」，再試「哪種建模策略有效」，接著在勝者路線上「加深」，最後「凍結並驗收能否上線」。

### Phase 1：根因診斷（RCA）與上限／契約

**核心問題：** `precision@recall=1%` 上不去或變差時，**主因是模型不夠好，還是標籤／資料／評估契約在拖累？**

**會涵蓋：** 歷史對照（STATUS）、切片拖累、標註品質與 censored／延遲、train／serve／驗證時點一致與洩漏風險、在固定契約下重現 baseline／上限。

**產出意義：** 排出「模型 vs 標籤／資料」主因排序，並決定是否 **先修資料再衝模型**（timeline 重排）。

### Phase 2：高槓桿建模路線（A / B / C）

**核心問題：** 在 Phase 1 鎖定的**同一評估契約**下，**哪一種建模策略**最有機會帶來可重現的 uplift，而非單窗僥倖？

**路線直覺：** Track A 對齊排序與硬負例；Track B 分群與 gating；Track C 時序穩定性過濾。

**產出意義：** 至少一條路線達到可量化 uplift（例如 +3~5pp）且跨窗大致站得住，才進入 Phase 3。

### Phase 3：特徵深化與集成收斂

**核心問題：** 在 **Phase 2 勝者路線**上，還能靠**哪些特徵／哪些切片／哪種融合**再擠出增益，又不把系統變成難維運的黑箱？

**會涵蓋：** 行為類特徵、拖累切片定向特徵包、輕量集成與消融、高分段校準與決策邊界。

**產出意義：** 相對 Phase 2 勝者再提升，且穩定性與切片沒有明顯換爛，才適合定版。

### Phase 4：定版、回放與 Go / No-Go

**核心問題：** **已凍結**的資料窗、特徵、模型與閾值，在**多時間窗／營運條件**下是否仍達標？上線後**告警量、誤報、業務影響**是否可接受？

**會涵蓋：** 候選凍結、多窗回放、影響估算、風險與回滾、最終 Go／No-Go。

**產出意義：** 正式上線與否的決策，以及監控與退路。

### 一句話對照

| Phase | 一句話 |
| :--- | :--- |
| **1** | 先搞清楚是「模型不行」還是「資料／標籤／契約不行」。 |
| **2** | 在對的契約下，試三種建模策略哪條真能帶 uplift。 |
| **3** | 在勝者身上加深特徵與融合，並把高分段壓實。 |
| **4** | 凍結後全面驗收，用營運與風險語言決定上不上線。 |

正式欄位與 Gate 仍以 `.cursor/plans/PLAN_precision_uplift_sprint.md` 與 `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md` 為準；本節為高階詮釋。

### 四階段詳解（補充：內容、依賴與為何可能有效）

以下彙整調查流程中各 Phase 的**具體工作意涵**、**與前後階段的依賴**，以及（適用處）**為何可能帶來** `precision@recall=1%` 的改善。細項門檻與產物檔名仍以總計畫為準。

#### Phase 1：根因診斷（RCA）與上限／契約

- **在做什麼（對齊計畫）**：歷史紀錄對照（STATUS）、錯誤切片分析、標籤品質稽核（含 censored／延遲）、特徵與評估的 point-in-time／洩漏檢查、在固定契約下重現 baseline／上限。
- **Gate 意義**：完成 RCA，明確排出「**模型限制 vs 標籤／資料／契約限制**」的主因排序。
- **與 Phase 2 的分流**：若結論顯示**主因在標籤流程或資料契約**，應啟動計畫中的 **Timeline 重排**（先修標註與契約，**暫緩**大規模 Phase 2～4 模型衝刺）。若主因**不是**以標籤／資料為主，則依預設進入 Phase 2 的建模實驗矩陣。
- **對後續階段的輸入**：切片報告提供 Phase 3「拖累切片定向特徵」的優先清單；契約與上限結論避免在不可比的口徑上調參。

#### Phase 2：高槓桿建模路線（A / B / C）

主指標在固定 `recall=1%` 下，實務上高度依賴**頂端排序**（真陽性靠前、假陽性被擠出頂端）。Phase 2 在**與 Phase 1 一致的評估契約**下並行試三類槓桿：

| Track | 重點 | 為何可能帶來 uplift（直覺） |
| :--- | :--- | :--- |
| **A** | 排序導向訓練（class weighting／focal-like）、Hard Negative Mining | 加權讓梯度更重視難例與邊界；**硬負例**（高分但實為負）直接對準「頂端誤報」來源。 |
| **B** | 分群建模 + gating（2～4 群起步） | 降低單一全域模型的欠擬合；子群內排序變好，彙總後有機會拉高 top 帶的 precision。 |
| **C** | Forward／Purged 等時序驗證 | 主要作用是**篩掉單窗幻覺**與不穩設定，讓通過 Gate 的 uplift **較可複製**到下一個時間窗（偏「可信選擇」而非單次 magic fit）。 |

- **Gate 意義（計畫建議）**：至少一條路線相對基線有顯著 uplift（例如 **+3～5 個百分點**），並搭配跨窗／波動控管（見總計畫 Phase 2 與 orchestrator `evaluate_phase2_gate`）。
- **工時提示**：實際牆鐘取決於 YAML 中**實驗數、時間窗數、是否 per-job 回測**及 `max_parallel_jobs`；可在**非生產、較強機器**上跑，前提是資料與標籤契約與目標環境對齊，且遵守不對 production DB 寫入等合規要求。預設 `orchestrator/config/run_phase2.yaml` 可為起點。

#### Phase 3：特徵深化與集成加碼（在勝者路線上）

- **依賴 Phase 2**：在 **Phase 2 通過 Gate 的勝者路線**（目標函數、分群／gating、驗證策略）上加深，而非另開一條與 Phase 2 無關的模型。
- **依賴 Phase 1**：**拖累切片定向特徵**應對齊 Phase 1 的切片證據，避免全域盲目擴欄。
- **在做什麼（對齊計畫）**：動態行為特徵；切片專用 feature pack；分群後**有設計的**集成與消融；高分段校準與決策邊界檢查。
- **Gate 意義**：相對 Phase 2 勝者**再提升**，且**不犧牲跨窗穩定性**。

#### Phase 4：定版、回放與上線決策

- **在做什麼（對齊計畫）**：**候選凍結**（資料窗、特徵集、模型、閾值／決策規則）；**多時間窗回放**（主指標 + 切片）；**上線影響估算**（告警量、誤報、業務 KPI）；彙總為 **Go／No-Go 會議包**。
- **依賴前面階段**：候選應來自 Phase 2～3 已收斂且穩定性可接受的版本；契約與切片敘事應與 Phase 1 以來的調查一致。
- **Gate 意義**：**主指標達標且跨窗穩定**後，才進入上線流程；最終商業決策仍建議人工簽核（見 MVP 任務清單範圍說明）。

---

## 要怎麼做（照順序）

1. 先打開 `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md`，更新「進度儀表板」與「當前 Phase」。
2. 進入對應 `phaseX/` 目錄，依 checklist 填寫該階段所有工件。
3. 每完成一項工件，立刻回填 `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md` 的勾選狀態與里程碑。
4. 每週做一次 checkpoint：更新主指標、切片排名、保留/淘汰決策。
5. 只有在當前 Phase Gate 達成後，才可進入下一階段。

---

## 各階段重點與必交工件

- `phase1/`、`phase2/`：調查**階段資料夾**（README、人工摘要、輔助腳本等），與 orchestrator **自動產出報表**路徑分開。
- **Orchestrator 報表（依 run 集中）**：`investigations/precision_uplift_recall_1pct/results/<run_id>/reports/phase1/`、`…/reports/phase2/`（見下方「Full-run 產物檢查」）。`run_state.json` 的 `artifacts.phase1_reports_dir` / `phase2_reports_dir` 會指向這些目錄。

- `phase1/`：根因診斷（RCA）
  - 重點：先判斷瓶頸在模型，還是標籤/資料契約。
  - 必交：`status_history_crosscheck.md`、`label_noise_audit.md`、`phase1_gate_decision.md` 等（可由 orchestrator 寫入 `results/.../reports/phase1/`，再同步或連結到本階段資料夾若流程需要）。

- `phase2/`：模型路線並行比較（A/B/C）
  - 重點：至少一條路線達成相對基線顯著 uplift（+3~5pp）。
  - 必交：`track_a_results.md`、`track_b_results.md`、`track_c_results.md`、`phase2_gate_decision.md`（預設同樣寫入 `results/.../reports/phase2/`）。

- `phase3/`：特徵深化與集成收斂
  - 重點：在 Phase 2 勝者基礎上再提升，且不犧牲穩定性。
  - 必交：`feature_uplift_table.md`、`ensemble_ablation.md`、`phase3_gate_decision.md`。

- `phase4/`：定版與 Go/No-Go
  - 重點：多窗回放達標後才可 Go。
  - 必交：`candidate_freeze.md`、`multi_window_backtest.md`、`go_no_go_pack.md`。

---

## 何時要重排時程

若 Phase 1 顯示主要瓶頸是標籤流程/資料契約（不是模型能力）：

- 先修資料與標籤流程，
- 暫緩大規模模型擴張（含大型 ensemble），
- 待 Phase 1 Gate 更新為可通過，再回到 Phase 2。

---

## 文件紀律（重要）

- 不接受口頭結論；每個判斷都要有對應檔案證據。
- 檔案命名與欄位請沿用模板，不要自行改名，避免後續彙整困難。
- 若有 blocker，請在 `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md` 的儀表板即時標記 `🔴 阻塞` 與解除條件。

---

## Orchestrator 實跑指南（Production）

以下只涵蓋 `phase1` orchestrator（`investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py`）。

### 0) 先準備 config（必做）

1. 複製 `investigations/precision_uplift_recall_1pct/orchestrator/config/run_phase1.yaml`。
2. 依環境填好：
   - `model_dir`
   - `state_db_path`
   - `prediction_log_db_path`
   - `window.start_ts` / `window.end_ts`（建議統一 HKT）
   - `thresholds.*`
3. 若你要讀非預設 backtest 檔，設定 `backtest_metrics_path`。

---

### 1) Dry-run：Production 上線前快檢（2~10 分鐘）

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 \
  --config <your_phase1.yaml> \
  --run-id <dry_run_id> \
  --dry-run
```

若環境暫時不能跑 backtester CLI smoke，可先：

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 \
  --config <your_phase1.yaml> \
  --run-id <dry_run_id> \
  --dry-run \
  --skip-backtest-smoke
```

#### Dry-run 成功/失敗怎麼判斷

- **成功（READY）**
  - process exit code = `0`
  - `orchestrator/state/<run_id>/run_state.json` 內：
    - `mode == "dry_run"`
    - `readiness.status == "READY"`
    - `steps.dry_run_readiness.status == "success"`
- **失敗（NOT_READY）**
  - process exit code = `6`
  - stderr 會列出 blocking reasons
  - `run_state.json` 內：
    - `mode == "dry_run"`
    - `readiness.status == "NOT_READY"`
    - `readiness.checks[]` 可定位哪一項失敗（DB、script、writable path 等）

> dry-run 只做 readiness，不產生正式調查結論。

---

### 2) Full-run：正式 investigation 執行

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 \
  --config <your_phase1.yaml> \
  --run-id <run_id>
```

#### Full-run 產物檢查

- `investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/run_state.json`
- `investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/logs/*.log`
- `investigations/precision_uplift_recall_1pct/orchestrator/state/<run_id>/collect_bundle.json`
- `investigations/precision_uplift_recall_1pct/results/<run_id>/reports/phase1/*.md`（六份自動報表；`artifacts.phase1_reports_dir`）

#### Full-run 成功/失敗判讀（快速）

- **成功**：exit code = `0`，`run_state.json` 中 `steps` 關鍵步驟為 `success`。
- **失敗**：非 0，常見：
  - `3`：preflight fail
  - `4`：R1/R6 分析 fail
  - `5`：backtest fail
  - `6`：dry-run NOT_READY

請優先看：

1. `run_state.json` 的 `steps.<step>.error_code / message`
2. 對應 `logs/*.stderr.log`

---

### 3) Resume：中斷後續跑

```bash
python investigations/precision_uplift_recall_1pct/orchestrator/run_pipeline.py \
  --phase phase1 \
  --config <your_phase1.yaml> \
  --run-id <run_id> \
  --resume
```

- 若 `config` 指紋與既有 `run_state` 不同，會自動判定 resume invalid，重新執行 eligible steps（避免錯誤續跑）。
- 重新跑前，請先確認你是否真的要在同 `run_id` 上延續；若要做新的調查契約，建議換新 `run_id`。
