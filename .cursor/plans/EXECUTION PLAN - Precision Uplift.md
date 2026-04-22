# Precision Uplift Execution Plan（Field-Test Objective，Step 1）

> 文件層級：Execution Plan（Working / Execution Plan）。  
> 目的：只定義**本輪實際執行任務**，範圍限於 `3.1 第一層：主戰場` 的前四項。  
> 邊界：本檔不重寫需求與架構；上游以 `trainer/precision_improvement_plan/PRECISION_UPLIFT_FIELD_TEST_OBJECTIVE_IMPLEMENTATION_PLAN.md` 與 `trainer/precision_improvement_plan/PRECISION_UPLIFT_FIELD_TEST_OBJECTIVE_RECOMMENDED_ITEMS_ROI.md` 為準。  
> 契約版本：`field-test-objective-v1`

### 任務狀態標記（本檔）

| 標記 | 意義 |
| :--- | :--- |
| **✅** | 本檔該列 **DoD** 已滿足（或等價交付）。 |
| **🟡** | **部分完成**：已有可跑產物／MVP，但尚未滿足本列 DoD。 |
| **⏳** | **進行中**：已開工、尚未結案。 |
| **⬜** | **未開始**。 |

**狀態維護**：與 Implementation Plan 衝突時，先釐清事實再同步兩檔；本檔只記錄前四項主戰場任務的執行狀態。

---

## 0. 協作約定（本輪）

- 在後續對話中提到 `PLAN.md`，一律指本檔：`.cursor/plans/EXECUTION PLAN - Precision Uplift.md`。
- 決策紀錄一律寫入：`.cursor/plans/DECISION_LOG.md`。
- 進度與執行流水一律寫入：`.cursor/plans/STATUS.md`。
- 本檔只維持 Working / Execution 層內容；若需改 scope 或架構，先回寫上游 SSOT / Implementation Plan。

---

## 1. 目前基線（Execution Baseline）

### 1.1 已可用能力（可直接執行）

- `trainer/training/trainer.py` 已具備 Optuna HPO、AP objective、validation scoring 與 artifact 輸出主路徑。
- `trainer/training/threshold_selection.py` 已具備 DEC-026 shared selector、`min_alert_count` / `min_alerts_per_hour` guards 與 fallback semantics。
- `trainer/training/backtester.py` 已能輸出主要離線指標與多窗回測基礎結果。
- `trainer/serving/scorer.py` 與 `prediction_log` / `runtime_rated_threshold` 已提供後續 DEC-032 對齊落點。

### 1.2 尚未完成能力（執行時視為限制）

- HPO objective 尚未對齊 field-test objective；目前仍以 AP 為主。
- `DEC-026 field_test mode` 尚未正式落地為 trainer/backtester/calibration 共用契約。
- CatBoost / XGBoost bakeoff 尚未形成固定報表與單模公平比較基線。
- 二階段模型尚未有 PoC artifact / serving / evaluation 契約。

### 1.3 當前任務狀態摘要（對齊 §4）

| 任務 | 狀態 | 備註 |
| :--- | :--- | :--- |
| `W1 / R1 Optuna precondition + objective freeze` | **🟡** | precondition JSON 工具 + trainer 讀檔寫入 `training_metrics` 已部分落地；objective contract 全文凍結與 orchestration 自動餵檔仍待補 |
| `W2 / R1 Optuna objective implementation parity` | **⬜** | 尚未完成 `run_optuna_search` / winner-pick / 報表欄位對齊 |
| `W3 / R2 ranking-focused training matrix` | **⬜** | 尚未形成可重跑的 weighting / HNM 配置矩陣 |
| `W4 / R3 fair bakeoff` | **⬜** | 尚未建立單模公平比較與 winner / hold policy |
| `W5 / R4 entry-gate decision` | **⬜** | 尚未判定二階段 PoC 是否應進場 |
| `W6 / R4 PoC task stub` | **⬜** | 僅在 `W5=GO` 時建立，不預設啟動 |

---

## 2. 執行目標（全程 + 當前重點）

本檔覆蓋 **第一層主戰場前四項**，執行優先順序：

1. 先凍結 `R1` 的 **Optuna / HPO objective contract**，避免後續各路線在不同目標上比較。
2. 用 `R2` 驗證高分帶 FP 壓制是否真能帶動 field-test objective。
3. 用 `R3` 建立單模公平比較基線，確認是否存在明顯較強的模型家族。
4. 僅在 `R1`~`R3` 有可比性證據後，才啟動 `R4` PoC entry。

當前重點：**先把前四項變成可重跑、可比較、可 gate 的執行序列，而不是同時開多條大路線。**

---

## 3. Run 契約凍結（每次正式 run 前必做）

### 3.1 必凍結欄位

- `run_id`
- `model_version/model_dir`
- window（`start_ts/end_ts`）與時區
- 標籤契約（含 censored 規則）
- 主要資料路徑（state / prediction_log / warehouse）
- `selection_mode`（`legacy` / `field_test`）
- `objective_definition`（單一 constrained objective / 複合 objective）
- `PRODUCTION_NEG_POS_RATIO`
- `min_alert_count`
- `min_alerts_per_hour`
- `fold scheme / validation window definition`

### 3.2 契約漂移處理

- 契約任一項在 run 中途改動：該 run 視為失效，重新起 run。
- `--resume` 僅允許在同契約（fingerprint 一致）下使用。

---

## 4. 執行排程（實際工作）

### 4.1 Batch A：先凍結 objective 與高分帶訓練（P0）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **🟡** | `W1 / R1 Optuna precondition + objective freeze` | 1. 盤點 folds 的正例數、rated bet 數、`fold_duration_hours`、baseline `T_feasible` 集合大小、test neg/pos ratio。 2. 明確定義 Optuna / HPO 要採用的 constrained objective、fallback fold semantics、guardrails。 3. 決定是否允許單一 objective 或需複合 objective。 | `DS owner` | 現有 trainer/backtester 基線可用、validation folds 與評估資料可取得 | `out/precision_uplift_field_test_objective/field_test_objective_precondition_check.json`、`trainer/precision_improvement_plan/field_test_objective_precondition_check.md`、objective 設計摘要 | 有完整 precondition 產物；明確寫出 `selection_mode`、Optuna objective 定義、fallback semantics、是否允許單一 constrained objective；不存在未定義欄位 |
| **⬜** | `W2 / R1 Optuna objective implementation parity` | 1. 在 `run_optuna_search()`、winner-pick / early stopping 與 trainer / backtester 報表中對齊 objective 定義。 2. 確認欄位輸出含 `precision_raw` / `precision_prod_adjusted` / `recall` / `alerts_per_hour`。 3. 以同一契約的多個資料窗比較 AP objective vs field-test objective，形成多窗對照報告。 | `DS owner` | `W1` | objective 對照報告（多窗）、欄位對照表、run config 凍結紀錄 | 至少完成同一契約下的多窗可重跑比較；新舊 objective 結果可跨窗並列比較；`run_optuna_search()` 與報表欄位語意一致；fallback / infeasible 情況可明確辨識 |
| **⬜** | `W3 / R2 ranking-focused training matrix` | 1. 定義 weighting / hard-negative / top-band reweighting 的最小矩陣。 2. 跑小矩陣實驗。 3. 保留版本化配置與結果。 4. 輸出主指標 uplift 與穩定性摘要。 | `DS owner` | `W1`、`W2` | ranking config matrix、實驗報告（含主指標 uplift / 穩定性摘要）、保留/淘汰建議 | 至少一組配置完成同契約比較；結果能回答是否值得進一步擴展，且有主指標 uplift 與穩定性摘要；無 silent resource blow-up |

#### 4.1.1 `W1` 當日最小可執行切面（Kickoff Checklist）

> 目標：在不改 architecture 的前提下，先把 `W1` 做到「可重跑、可審核、可阻擋跳關」。

| 狀態 | 子項 | 內容 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- |
| **⬜** | `W1-C1 precondition schema freeze` | 凍結 precondition JSON 最小欄位：`run_id`、`window`、`fold_stats[]`、`t_feasible_stats`、`test_neg_pos_ratio`、`production_neg_pos_ratio_assumption`、`single_objective_allowed`、`blocking_reasons[]`。 | `field_test_objective_precondition_check.json` schema 區塊或等價欄位 | 欄位定義完整且無待定 placeholder |
| **🟡** | `W1-C2 fold evidence collect` | 依同一契約收集各 fold：正例數、finalized TP 數量級、rated bet 數、`fold_duration_hours`、baseline `T_feasible` 集合大小。 | precondition JSON `fold_stats[]` | 腳本可聚合手動餵入之 fold metrics；尚未接 investigation / full run 自動餵檔 |
| **🟡** | `W1-C3 objective decision` | 依 `W1-C2` 證據判斷：`single constrained objective` 或 `composite objective`；明確寫 fallback fold semantics。 | `field_test_objective_precondition_check.md` 決策段落 | 腳本可輸出 `objective_decision` / `blocking_reasons`；MD 決策模板與 fallback 段落仍待加強 |
| **🟡** | `W1-C4 gate readiness` | 將 `selection_mode`、objective 定義、fallback semantics 寫回 run contract，並生成本輪 objective freeze 摘要。 | objective 設計摘要 + run contract 凍結紀錄 | 已可經 `FIELD_TEST_OBJECTIVE_PRECONDITION_JSON` 將 precondition 摘要寫入 `training_metrics.json`；完整 run contract 凍結紀錄與 orchestration 自動產檔仍待補 |

#### 4.1.2 `W1` 阻擋規則（Fail-fast）

- 任一 fold 的關鍵欄位缺失且無 reason code：標記 `GATE BLOCKED`，不得進 `W2`。
- 任一 fold 的 `T_feasible` 過小、常為空，或尾段支撐不足：不得硬切單一 constrained objective（需改採複合目標、fold 聚合分數，或先調整驗證窗設計）。
- `PRODUCTION_NEG_POS_RATIO` 假設無法交代來源或敏感度：不得把 `prod_adjusted` 作唯一 driver。
- precondition 只產生 markdown、無 machine-readable JSON：視為未完成 `W1`。

### 4.2 Batch B：單模比較與二階段 entry gate（P0/P1）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **⬜** | `W4 / R3 fair bakeoff` | 1. 在同一特徵、切分、objective、報表下訓練 LGBM / CatBoost / XGBoost。 2. 輸出含主指標、波動、成本的單模公平比較。 3. 明確給出 single winner / hold / reject。 | `DS owner` | `W1`、`W2` | bakeoff report（含主指標 / 波動 / 成本）、單模對照表、winner/hold policy | 三個模型家族結果可直接對照；比較欄位一致且含主指標、波動、成本；結論明確寫出 single winner 只是 phase-level 收斂策略 |
| **⬜** | `W5 / R4 entry-gate decision` | 1. 依 `W2`~`W4` 結果做 top-band FP error analysis。 2. 判定是否符合 stage-2 PoC entry criteria，並明確記錄是否仍存在明顯殘餘 gap。 3. 盤點 serving 延遲、artifact 複雜度與 train-serve parity 成本是否在可接受範圍。 4. 若不符合，明確記錄不啟動理由。 | `DS owner + reviewer` | `W2`、`W3`、`W4` | entry-gate memo（含殘餘 gap / 複雜度評估）、top-band FP analysis、go/no-go decision | 有 go/no-go 決策，且明確標示此決策僅代表是否進入 stage-2 PoC、不是最終架構定案；若 go，列出 PoC 邊界、殘餘 gap 依據與 serving / artifact / parity 可接受性；若 no-go，列出阻塞證據與回頭任務 |
| **⬜** | `W6 / R4 PoC task stub` | 僅在 `W5=GO` 時建立下一步 PoC 任務骨架：artifact、train/eval、單階基線公平對照、serving parity、rollback。 | `DS owner` | `W5=GO` | stage-2 PoC stub（含單階基線公平對照計畫） | 只產生 PoC stub，不直接視為已開發；stub 必須包含與單階最佳基線的公平對照設計；若 `W5=NO GO`，本列維持未開始 |

---

## 5. 逐任務執行規範（Definition of Ready / Done）

### 5.1 Ready to Merge（每項任務都要達成）

- 有機器可讀輸出（JSON 或固定欄位段落）。
- 有可讀摘要（Markdown / 固定欄位報告），且能回連到對應 run。
- failure path 必寫 `blocking_reasons` 或等價欄位（不得 silent degrade）。
- 不額外放大資源風險（預設並行可控）。

### 5.2 本輪關鍵任務額外硬條件

- `W1`：若任一 fold 的 `T_feasible` 過小或常為空，不得硬切單一 constrained objective。
- `W2`：trainer / backtester 必須對齊同一 objective contract 與 fallback semantics。
- `W3`：ranking-focused 實驗至少要輸出主指標 uplift 與穩定性摘要，不接受只報單次最佳結果。
- `W4`：模型家族比較必須是同特徵、同切分、同 objective、同報表，不接受 apples-to-oranges。
- `W5`：二階段只能在 top-band FP 仍為主瓶頸、且殘餘 gap 與 serving / artifact / parity 成本判定都已明確記錄時進場，不得憑直覺跳關。
- `W6`：PoC stub 必須包含與單階最佳基線的公平對照設計，不得只寫二階段自身 train/eval 流程。

### 5.3 本輪最終 DoD

- `W1`~`W5` 均有對應輸出，且能 trace 回同一上游 implementation plan。
- 已明確回答：是否允許單一 constrained objective、哪一組 ranking-focused 設定值得保留、哪個單模 winner 暫時領先、R4 是否可進場。
- 若任一結論證據不足，明確降級為 exploratory / comparative，不偽裝成 decision-grade。

---

## 6. Gate 與升級規則（執行層）

### 6.1 進入下一階段條件

- `W2` 只有在 `W1` objective contract 凍結後才能正式開始。
- `W3` 只有在 `W1` / `W2` 契約與欄位已可比後才能開始。
- `W4` 只有在 `W1` / `W2` 契約與欄位已可比後才能開始。
- `W5` 只有在 `W2` / `W3` / `W4` 都產生可比性證據後才能做 go/no-go。
- `W6` 僅在 `W5=GO` 時建立。

### 6.2 證據不足處理

- `EVIDENCE MISSING`：結論降級；必要時重跑。
- `GATE BLOCKED`：先解 `blocking_reasons`，不跳關。

---

## 7. 每日執行節奏（Cadence）

- 每日一次：更新 `W1`~`W6` 狀態、阻塞清單、最新工件。
- 每 2~3 日：review 是否仍應維持當前任務排序，或因證據不足暫停下一列。
- 每個任務完成後：立即補記保留 / 淘汰 / 延後理由。

---

## 8. 風險與止損（Execution Risk Control）

- **資源風險（RAM/CPU/Runtime）**：先小窗 smoke，再擴窗；必要時降並行。
- **契約漂移**：run 中途改 window/label/path 一律重起。
- **證據幻覺**：單窗或不完整證據不得升級為 decision-grade。
- **工程複雜度失控**：新增複雜度若無可量化增益，降級投入；R4 不得在 R1~R3 尚未收斂前偷跑 production 化。
- **ensemble 誘惑過早介入**：本輪不展開 `#13`；若有人提出直接集成，先回到 `W4` 單模公平比較結果與 `W5` entry gate。

---

## 9. 跨文件連動（Traceability）

- Recommendation / ROI：`trainer/precision_improvement_plan/PRECISION_UPLIFT_FIELD_TEST_OBJECTIVE_RECOMMENDED_ITEMS_ROI.md`
- Implementation Plan：`trainer/precision_improvement_plan/PRECISION_UPLIFT_FIELD_TEST_OBJECTIVE_IMPLEMENTATION_PLAN.md`
- Sprint Plan：`.cursor/plans/PLAN_precision_uplift_sprint.md`
- Runbook：`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md`

---

## 10. 更新規則（本檔）

- 本檔只寫「本輪實際執行與順序」，不重寫需求或架構。
- 任務狀態更新以 Implementation Plan 為主；本檔同步節奏與阻塞策略。
- 若與 SSOT / Implementation Plan 衝突，先修上游契約再更新本檔。
- 本版只覆蓋 `3.1 第一層：主戰場` 的前四項；其餘任務待下一輪再展開。

---

## 11. 後續擴寫範圍（本輪不展開）

- 第二層（開上限）任務：待 `W1`~`W5` 至少達到 comparative 後再補 execution tasks。
- 第三層（守門與產品化）任務：待第一層完成保留/淘汰後再補 execution tasks。
- `#13 ensemble`：不在本輪 execution 範圍；僅在單模公平比較與互補性證據成立後再開。
