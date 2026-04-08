# Precision 提升調查指南

本資料夾用於執行並留存「`precision@recall=1%` 提升計畫」的全部調查證據與決策紀錄。

## 目標

- 將 `precision@recall=1%` 由約 40% 提升至 `>=60%`。
- 提升結果需在多時間窗（forward/purged）下仍穩定成立。

## 先看哪兩份文件

- 總計畫：`.cursor/plans/PLAN_precision_uplift_sprint.md`
- 執行儀表板：`investigations/precision_uplift_recall_1pct/EXECUTION_PLAN.md`

---

## 要怎麼做（照順序）

1. 先打開 `EXECUTION_PLAN.md`，更新「進度儀表板」與「當前 Phase」。
2. 進入對應 `phaseX/` 目錄，依 checklist 填寫該階段所有工件。
3. 每完成一項工件，立刻回填 `EXECUTION_PLAN.md` 的勾選狀態與里程碑。
4. 每週做一次 checkpoint：更新主指標、切片排名、保留/淘汰決策。
5. 只有在當前 Phase Gate 達成後，才可進入下一階段。

---

## 各階段重點與必交工件

- `phase1/`：根因診斷（RCA）
  - 重點：先判斷瓶頸在模型，還是標籤/資料契約。
  - 必交：`status_history_crosscheck.md`、`label_noise_audit.md`、`phase1_gate_decision.md` 等。

- `phase2/`：模型路線並行比較（A/B/C）
  - 重點：至少一條路線達成相對基線顯著 uplift（+3~5pp）。
  - 必交：`track_a_results.md`、`track_b_results.md`、`track_c_results.md`、`phase2_gate_decision.md`。

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
- 若有 blocker，請在 `EXECUTION_PLAN.md` 的儀表板即時標記 `🔴 阻塞` 與解除條件。
