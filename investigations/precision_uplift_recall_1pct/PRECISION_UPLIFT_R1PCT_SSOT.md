# Precision Uplift R1PCT SSOT

> 檔名與角色一致：本檔為 `PRECISION_UPLIFT_R1PCT_SSOT.md`（勿與 `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md` 混淆）。  
> 角色：本文件是此調查專案唯一真相來源（SSOT）。  
> 作用：定義「現況可做什麼、目標是什麼、四份文件如何分工」。  
> 邊界：不放操作細節與逐項工程任務（分別下放到 Execution Plan 與 Implementation Plan）。

---

## 1. 文件分工（固定契約）

| 文件 | 唯一職責 | 是否可放命令 |
| :--- | :--- | :--- |
| `PRECISION_UPLIFT_R1PCT_SSOT.md` | **SSOT**：範圍、術語、現況能力、Gate 契約、文件治理 | 否 |
| `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` | **Implementation Plan**：工程任務、DoD、里程碑、狀態 | 否 |
| `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md` | **Execution Plan**：專案推進節奏、階段輸入輸出、決策節點 | 可（高層） |
| `PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md` | **Orchestrator Runbook**：實際 CLI、旗標、故障排查 | 可（操作層） |

治理規則：
- 任何同主題內容只允許有一個原始來源（single owner）。
- 若文件衝突，優先序：`SSOT > Implementation Plan > Execution Plan > Runbook`。
- Runbook 不得把 `Implementation Plan` 標示為未完成的能力寫成「已可用」。

---

## 2. 專案目標與非目標

### 2.1 目標
- 在固定評估契約下提升 `precision@recall=1%`，並建立可重現證據鏈。
- 每個階段結論都必須有工件（report + metrics + run state），禁止口頭結論。
- 兼顧筆電資源限制：預設保守並行、避免 OOM、允許 fail-fast。

### 2.2 非目標
- 不在此輪導入分散式排程系統。
- 不在此輪把最終商業決策自動化（Go/No-Go 仍需人工簽核）。

---

## 3. 名詞與契約

- **Run 契約**：`run_id`、`model_version/model_dir`、時間窗、時區、標籤與 censored 規則、資料來源路徑。
- **Gate 狀態**：`PASS` / `BLOCKED` / `FAIL` / `PRELIMINARY`（依 phase 定義）。
- **結論強度**：`exploratory` / `comparative` / `decision_grade`。
- **證據鏈**：`run_state.json` + phase reports + metrics artifacts + stdout/stderr logs。

---

## 4. 現況能力快照（2026-04）

| 能力 | 現況 |
| :--- | :--- |
| `--phase phase1` | 可完整跑（含 gate 與報表） |
| `--phase phase2` | 可跑 MVP（含可選訓練/回測、gate、報表） |
| `--phase all --dry-run` | 可用（readiness 檢查） |
| `--phase all` 非 dry-run | **尚未實作** |
| `--phase phase3` / `phase4` full run | **尚未實作** |
| `--mode autonomous` | **尚未實作** |

硬性說明：
- 任何文件不得再出現「目前可直接使用 `--phase all --mode autonomous`」之描述。
- 若要宣稱 Phase 2 為決策級結論，必須有多窗與策略生效證據，不可只看單一 PASS 標籤。

---

## 5. Gate 契約（跨文件共用）

### 5.1 Phase 1
- 最低要件：樣本量、觀測時長、R1/R6 一致性、主因排序。
- PIT parity 若為 `STRICT`，違規須阻斷 PASS；`WARN_ONLY` 可警示不阻斷。

### 5.2 Phase 2
- 需比較 uplift 與波動（至少可比 baseline/challenger）。
- 若證據不足（例如僅 plan-only），應為 `BLOCKED`，不可升級為 `PASS`。

### 5.3 Phase 3/4
- 目前僅定義目標，不宣稱已有可執行 full-run gate 引擎。

---

## 6. 效能與風險原則（筆電優先）

- 預設 `max_parallel_jobs=1`，逐步放寬，不一次開並行。
- 先做 dry-run，再做長跑，避免中後段才發現路徑/權限問題。
- 任一步驟若缺證據或輸出缺檔，應 fail-fast 並保留可恢復狀態。
- 若觀測窗/試驗數過大，先縮窗做 smoke，通過後再擴大。

---

## 7. 變更流程（文件維護）

每次調整流程或能力時，必須同步：
1. 先改本檔（`PRECISION_UPLIFT_R1PCT_SSOT.md`）的「現況能力快照」與「契約」。
2. 再改 `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` 任務狀態。
3. 最後更新 `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md` 與 `PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md` 的操作敘述。

禁止做法：
- 只改 Runbook 命令而不改上游契約。
- 把 roadmap 內容寫成現況。

