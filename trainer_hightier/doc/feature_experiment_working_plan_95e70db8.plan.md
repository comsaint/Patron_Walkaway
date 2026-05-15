---
name: Feature Experiment Working Plan
overview: 新增一份專屬於 feature experimentation 的 Working Plan 文件，承接現有 SSOT 與 Implementation Plan，並落地為可執行任務清單與 DoD，不修改既有 trainer working plan。
todos:
  - id: confirm-working-plan-target
    content: 確認新 working plan 檔名與路徑，並鎖定不修改既有 trainer working plan 檔案
    status: pending
  - id: extract-decisions
    content: 從 SSOT 與 Feature experimentation Implementation Plan 抽取已定決策並形成 working plan 前置章節
    status: pending
  - id: draft-wave-execution
    content: 撰寫 wave-based 任務拆解、依賴、DoD 與升級/回退 gate
    status: pending
  - id: add-reporting-template
    content: 補上每輪實驗固定輸出模板與驗收條件
    status: pending
isProject: false
---

# Feature Experimentation Working Plan Plan

## Goal
新增一份獨立的 Working Plan 文件，專門承接 feature experimentation（候選生成/篩選、長中短窗、訓練視窗策略）實施細節，並明確不修改既有文件 [C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/trainer-hightier-working-plan_c12558b9.plan.md](C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/trainer-hightier-working-plan_c12558b9.plan.md)。

## Inputs To Anchor
- SSOT: [C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/Data pipeline - SSOT.md](C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/Data pipeline - SSOT.md)
- Implementation Plan: [C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/Feature experimentation - IMPLEMENTATION_PLAN.md](C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/Feature experimentation - IMPLEMENTATION_PLAN.md)

## Planned Output
- New dedicated working plan file in `trainer_hightier/doc` (proposed: `Feature experimentation - WORKING_PLAN.md`), containing execution-level tasks only.

## Structure To Implement
1. **Scope And Guardrails**
   - 明確本 working plan 只處理 feature experimentation。
   - 明確記錄已決策項（AP 主指標、Gate 門檻、短中長窗定義、中窗 daily snapshot、長窗 monthly snapshot、`feature_compute_range` 與 `training_sample_range` 解耦）。

2. **Execution Waves**
   - Wave 0: Registry 與 baseline 對齊。
   - Wave 1: Gate 0/1/2 最小可運作流程。
   - Wave 2: 中窗（daily）與長窗（monthly）快取路徑。
   - Wave 3: 訓練視窗策略比較（all/rolling/recency weighting）。
   - 每個 wave 都有 entry criteria / exit criteria。

3. **Task Breakdown (Actionable)**
   - 每個 workstream 拆成 task/subtask。
   - 每個 task 帶 owner placeholder、依賴、DoD、產出 artifact。

4. **Decision Gates And Promotion Rules**
   - Gate 0/1/2 轉成可操作檢查清單。
   - group 升級/淘汰規則、回退規則、最大升級數量。

5. **Operational Constraints**
   - 成本限制：單 round runtime <= 60 分鐘。
   - 建議資源設定（記憶體/並行）與失敗處理策略。

6. **Reporting Template**
   - 固定每輪報表欄位：feature list version、group set、window policy、AP、R@Pmin、runtime、peak RAM、cache hit ratio、go/no-go reason。

## Acceptance Criteria
- 新文件完整承接 SSOT + Implementation Plan，且無範圍漂移。
- 內容為 task-level 可執行格式（不是原則重述）。
- 明確標示不修改既有 [C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/trainer-hightier-working-plan_c12558b9.plan.md](C:/Users/longp/Patron_Walkaway/trainer_hightier/doc/trainer-hightier-working-plan_c12558b9.plan.md)。