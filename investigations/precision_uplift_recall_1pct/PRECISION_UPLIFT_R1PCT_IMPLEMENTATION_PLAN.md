# Precision Uplift R1PCT Implementation Plan

> 角色：工程實作計畫（Implementation Plan）。  
> 來源契約：名詞、能力現況、治理規則以 `PRECISION_UPLIFT_R1PCT_SSOT.md`（SSOT）為準。  
> 原則：本文件只管「要做什麼、做到哪裡、完成定義是什麼」。

---

## 1. 版本與狀態快照

- 最後更新：`2026-04-17`
- 目前重點：補齊 Phase 1 RCA 的 decision-grade 證據鏈，避免「PASS 但結論不可審核」。
- 嚴禁誤導：`--phase all` 非 dry-run、`phase3/phase4` full run、`--mode autonomous` 仍屬未完成能力。

---

## 2. 路線圖（Roadmap）

| Wave | 目標 | 狀態 |
| :--- | :--- | :--- |
| W1-A | Phase 1 MVP 穩定可跑（含 gate/report） | 已完成 |
| W1-B | Phase 1 RCA decision-grade（5 項 RCA 可下結論） | 進行中 |
| W2 | Phase 2 MVP（plan -> runner -> gate -> report） | 進行中 |
| W3 | Phase 3 full run | 未開始 |
| W4 | Phase 4 full run + go/no-go pack | 未開始 |
| W5 | `--phase all` 非 dry-run 串接 + phase-level resume | 未開始 |
| W6 | autonomous supervisor（長跑自動化） | 未開始 |

---

## 3. 工作分解（WBS）

### 3.1 W1-A - Phase 1 MVP（Completed）

- [x] `run_pipeline.py --phase phase1` 可執行主流程
- [x] config schema / preflight / collector / evaluator / report builder
- [x] `run_state.json` + `--resume` + `--dry-run`
- [x] PIT parity `WARN_ONLY` / `STRICT` 模式入口
- [ ] 測試補強（最少 3 個關鍵測試案例）

**DoD**
- 同一 `run_id` 可重跑、可追溯、可產出完整 phase1 工件。

### 3.2 W1-B - Phase 1 RCA decision-grade（In Progress, P0）

目標：讓 Phase 1 的 5 項 RCA（歷史對照、切片、標籤品質、PIT、上限重現）都能輸出可審核結論，而不是只有描述性報告。

#### W1-B1 歷史對照（status history）結構化
- [ ] 新增 `status_history_registry`（YAML/JSON 皆可）作為歷史議題來源：
  - `issue_id`, `prior_decision`, `defer_reason`, `unblock_criteria`, `owner`, `current_state`
- [ ] `status_history_crosscheck.md` 改為「orchestrator 區塊 + 自動判定表 + 人工補充段」
- [ ] 產出 `status_history_crosscheck.json`（machine-readable）
- [ ] Gate 整合：若有 `current_state=unresolved_blocker`，Phase 1 至少 `BLOCKED`

#### W1-B2 細緻切片（slice RCA）可直接排序
- [ ] collector 增加 slice 聚合（先低成本維度）：
  - `date`, `player_tier`, `bet_amount_bucket`, `activity_bucket`
- [ ] 報告輸出 `top_drag_slices`（至少 Top 10），每列含：
  - `n`, `tp/fp/fn`, `precision_at_target_recall`, `delta_vs_global`, `confidence_flag`
- [ ] fail-fast：任一必要維度缺失時，`slice_data_incomplete` 寫入 `blocking_reasons`
- [ ] 效能限制：先做 SQL 聚合 + 分桶，避免 row-level 全量 materialize（筆電 RAM 優先）

#### W1-B3 標籤品質（label noise）從統計升級到判定
- [ ] 新增 lag 分桶統計（`<=1d`, `1-3d`, `3-7d`, `>7d`）
- [ ] 新增 `label_bottleneck_assessment`：`none | minor | major | blocking`
- [ ] 高分 FP 抽樣結果輸出結構化欄位（避免只留 narrative）
- [ ] Gate 整合：`major/blocking` 且無已核准修復計畫時，觸發 timeline 重排建議

#### W1-B4 PIT parity 補齊 critical checks
- [ ] 補 `window_timezone_mismatch_count` 可觀測能力（不再固定 null）
- [ ] 新增 leakage sentinel（feature_ts 不可晚於 decision_ts）
- [ ] `pit_contract_checks[]` 結構化輸出（`check_id/status/reason/evidence`）
- [ ] `STRICT` 模式下任何 critical check fail => `FAIL`

#### W1-B5 上限重現（upper bound）可比性契約
- [ ] 報告分離 `comparable_metrics` 與 `reference_only_metrics`
- [ ] 新增 `comparison_contract`：
  - `same_window_definition`, `same_recall_target`, `same_label_contract`, `comparable`
- [ ] 若 `comparable=false`，Phase 1 結論僅允許 `PRELIMINARY`（不可 decision-grade）

#### W1-B6 結論強度與主因排序
- [ ] 為 Phase 1 增加 `phase1_conclusion_strength`：
  - `exploratory | comparative | decision_grade`
- [ ] 新增 `root_cause_ranking`（Top 3 + evidence）
- [ ] `phase1_gate_decision.md` 固定輸出：
  - 主因排序、阻塞原因、可執行行動項（owner + due）

**DoD（W1-B 最終）**
- 5 項 RCA 皆有結構化輸出 + 明確判定欄位（PASS/WARN/FAIL 或 equivalent）。
- `phase1_gate_decision.md` 可回答「結論是什麼、為何成立、還缺什麼」。
- 在固定契約下，同 run 重跑結論一致（允許數值微小漂移，不允許結論翻轉）。

### 3.3 W2 - Phase 2 MVP（In Progress）

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

### 3.4 W3 - Phase 3 Full Run（Not Started）

- [ ] CLI：`--phase phase3`
- [ ] config schema（非 minimal）
- [ ] runner（只允許 phase2 winner route）
- [ ] collector + evaluator + `phase3_gate_decision.md`
- [ ] phase3 專屬錯誤碼與 log 契約

**DoD**
- 不靠人工拼接即可完成 phase3 證據鏈。

### 3.5 W4 - Phase 4 Full Run（Not Started）

- [ ] CLI：`--phase phase4`
- [ ] config schema（非 minimal）
- [ ] 多窗回放 runner
- [ ] impact estimation collector
- [ ] `go_no_go_pack.md` 自動輸出

**DoD**
- 可直接產出可審核的 go/no-go 決策包。

### 3.6 W5 - Multi-phase Orchestration（Not Started）

- [ ] `--phase all` 非 dry-run 流程
- [ ] gate-driven phase progression
- [ ] phase-level resume
- [ ] 統一 `artifacts_index.json`

**DoD**
- 從 phase1 到 phase4 可在單次長跑中依 gate 推進，且中斷可恢復。

### 3.7 W6 - Autonomous Supervisor（Not Started）

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
| W1-B（Phase 1 RCA decision-grade） | P0 | W1-A 已完成 |
| W2 真多窗矩陣 | P0 | W2-A 已穩定 |
| W2 fail-fast 補齊 | P0 | W2-A 已穩定 |
| W3 phase3 full run | P1 | W2 完成 |
| W4 phase4 full run | P1 | W3 完成 |
| W5 all-phase 非 dry-run | P1 | W3/W4 可用 |
| W6 autonomous | P2 | W5 可用 |

---

## 5. 實作順序（Actionable）

### 5.1 兩週落地順序（建議）

1. **第 1 週上半**：W1-B1 + W1-B2（先補結構化歷史對照與低成本切片）
2. **第 1 週下半**：W1-B3 + W1-B4（label 判定與 PIT critical checks）
3. **第 2 週上半**：W1-B5 + W1-B6（可比性契約 + 主因排序 + conclusion strength）
4. **第 2 週下半**：補測試 + 小窗 smoke + 正式 Phase 1 re-run 驗收

### 5.2 最低驗收證據（W1-B）

- 一個完整 run 下，六份 phase1 報告都包含「機器可讀欄位 + 人類可讀結論」。
- `phase1_gate_decision.md` 必有：
  - `phase1_conclusion_strength`
  - `root_cause_ranking`
  - `blocking_reasons`（若無則明示空）
- 至少 5 個關鍵測試：
  - status unresolved blocker -> gate blocked
  - slice 維度缺失 -> fail-fast/block
  - label bottleneck major -> timeline reorder signal
  - PIT strict violation -> fail
  - upper bound incomparable -> preliminary

---

## 6. 驗收與風險

### 6.1 驗收準則
- 每個已完成任務都要有可重現的 CLI 路徑與對應工件。
- 每個 gate 必須產生 `blocking_reasons` 或明確 pass evidence。
- 所有新能力必須先經 dry-run 或小窗 smoke 驗證。

### 6.2 主要風險（含資源）
- **OOM/長跑失控風險**：多窗 + 多實驗同時開會爆 RAM；預設並行維持 1。
- **假陽性結論風險**：只有單窗或 plan-only 卻下決策級結論。
- **契約漂移風險**：run 中途更換 model/window/label 規則造成不可比。
- **切片計算放大風險**：slice 維度一次開太多導致 SQL/記憶體暴增，先低成本維度再擴充。

---

## 7. 更新規則

- 任務狀態只在本檔更新（不要在 runbook 重複打勾）。
- 每次狀態變更要附「為何變更」一句描述，避免只改勾選。
- 若能力未完成，`PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md` 必須標示為「限制」，不可寫成可用。
