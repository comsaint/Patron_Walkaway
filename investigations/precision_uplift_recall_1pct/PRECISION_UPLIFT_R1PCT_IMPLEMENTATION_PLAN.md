# Precision Uplift R1PCT Implementation Plan

> 角色：工程實作計畫（Implementation Plan）。  
> 來源契約：名詞、能力現況、治理規則以 `PRECISION_UPLIFT_R1PCT_SSOT.md`（SSOT）為準。  
> 原則：本文件只管「要做什麼、做到哪裡、完成定義是什麼」。

---

## 1. 版本與狀態快照

- 最後更新：`2026-04-16`
- 目前重點：穩定 Phase 2 MVP，補齊科學有效性與可追溯性。
- 嚴禁誤導：`--phase all` 非 dry-run、`phase3/phase4` full run、`--mode autonomous` 仍屬未完成能力。

---

## 2. 路線圖（Roadmap）

| Wave | 目標 | 狀態 |
| :--- | :--- | :--- |
| W1 | Phase 1 MVP 穩定可跑（含 gate/report） | 已完成 |
| W2 | Phase 2 MVP（plan -> runner -> gate -> report） | 進行中 |
| W3 | Phase 3 full run | 未開始 |
| W4 | Phase 4 full run + go/no-go pack | 未開始 |
| W5 | `--phase all` 非 dry-run 串接 + phase-level resume | 未開始 |
| W6 | autonomous supervisor（長跑自動化） | 未開始 |

---

## 3. 工作分解（WBS）

### 3.1 W1 - Phase 1 MVP（Completed）

- [x] `run_pipeline.py --phase phase1` 可執行主流程
- [x] config schema / preflight / collector / evaluator / report builder
- [x] `run_state.json` + `--resume` + `--dry-run`
- [x] PIT parity `WARN_ONLY` / `STRICT` 模式入口
- [ ] 測試補強（最少 3 個關鍵測試案例）

**DoD**
- 同一 `run_id` 可重跑、可追溯、可產出完整 phase1 工件。

### 3.2 W2 - Phase 2 MVP（In Progress）

#### W2-A 基礎執行鏈
- [x] `run_pipeline.py --phase phase2`
- [x] plan bundle 輸出（含 `job_specs`）
- [x] runner smoke
- [x] 可選 trainer jobs
- [x] 可選 per-job backtest jobs
- [x] 可選 shared backtest jobs
- [x] phase2 gate report + track reports

#### W2-B 契約與可稽核性
- [x] `trainer_params` 白名單化
- [x] 非空 `overrides` 拒絕（防止 silent unapplied）
- [x] `resolved_trainer_argv` + `argv_fingerprint` 落地
- [ ] 完整 per-track/per-window 統一結果結構
- [ ] fail-fast 規則覆蓋缺口補齊

#### W2-C 科學有效性（T11A 方向）
- [x] strategy-effective 證據檢查
- [x] `conclusion_strength` 輸出
- [x] winner 欄位與雙窗硬 gate 入口
- [ ] 真多窗矩陣落地（非 bridge）
- [ ] 淘汰理由與 evidence 的結構化對齊

**DoD（W2 最終）**
- 任一 phase2 run 都能明確判定為 `PASS/BLOCKED/FAIL` 並給可追溯證據。
- 報表不只描述結果，還能回答「為什麼這個結論成立」。

### 3.3 W3 - Phase 3 Full Run（Not Started）

- [ ] CLI：`--phase phase3`
- [ ] config schema（非 minimal）
- [ ] runner（只允許 phase2 winner route）
- [ ] collector + evaluator + `phase3_gate_decision.md`
- [ ] phase3 專屬錯誤碼與 log 契約

**DoD**
- 不靠人工拼接即可完成 phase3 證據鏈。

### 3.4 W4 - Phase 4 Full Run（Not Started）

- [ ] CLI：`--phase phase4`
- [ ] config schema（非 minimal）
- [ ] 多窗回放 runner
- [ ] impact estimation collector
- [ ] `go_no_go_pack.md` 自動輸出

**DoD**
- 可直接產出可審核的 go/no-go 決策包。

### 3.5 W5 - Multi-phase Orchestration（Not Started）

- [ ] `--phase all` 非 dry-run 流程
- [ ] gate-driven phase progression
- [ ] phase-level resume
- [ ] 統一 `artifacts_index.json`

**DoD**
- 從 phase1 到 phase4 可在單次長跑中依 gate 推進，且中斷可恢復。

### 3.6 W6 - Autonomous Supervisor（Not Started）

- [ ] scorer/validator lifecycle 管理
- [ ] checkpoint-based mid/final snapshots
- [ ] health-check + restart policy
- [ ] runtime/resource guard rails

**DoD**
- 長時間運行不需人工守護，且遇錯可回復不靜默失敗。

---

## 4. 優先級與依賴

| 任務 | 優先級 | 依賴 |
| :--- | :--- | :--- |
| W2 真多窗矩陣 | P0 | W2-A 已穩定 |
| W2 fail-fast 補齊 | P0 | W2-A 已穩定 |
| W3 phase3 full run | P1 | W2 完成 |
| W4 phase4 full run | P1 | W3 完成 |
| W5 all-phase 非 dry-run | P1 | W3/W4 可用 |
| W6 autonomous | P2 | W5 可用 |

---

## 5. 驗收與風險

### 5.1 驗收準則
- 每個已完成任務都要有可重現的 CLI 路徑與對應工件。
- 每個 gate 必須產生 `blocking_reasons` 或明確 pass evidence。
- 所有新能力必須先經 dry-run 或小窗 smoke 驗證。

### 5.2 主要風險（含資源）
- **OOM/長跑失控風險**：多窗 + 多實驗同時開會爆 RAM；預設並行維持 1。
- **假陽性結論風險**：只有單窗或 plan-only 卻下決策級結論。
- **契約漂移風險**：run 中途更換 model/window/label 規則造成不可比。

---

## 6. 更新規則

- 任務狀態只在本檔更新（不要在 runbook 重複打勾）。
- 每次狀態變更要附「為何變更」一句描述，避免只改勾選。
- 若能力未完成，`PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md` 必須標示為「限制」，不可寫成可用。
