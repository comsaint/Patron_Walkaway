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

- `trainer/training/trainer.py` 已具備 Optuna HPO；本輪依 **DEC-043** 走 `selection_mode=field_test` 契約：當 W1 precondition 允許 constrained、rated、且可由 `payout_complete_dtm` 得到正之 validation span 時，啟用 **field-test DEC-026 validation precision** 目標（可搭配 `PRODUCTION_NEG_POS_RATIO` → trial 分數 prod-adjusted）；若 precondition 不允許或 validation span 不可行，直接 **`GATE BLOCKED`**（不再 AP fallback）。`training_metrics` 可寫入 **`optuna_hpo_*`**（實際優化目標與 `val_ap` 區隔）。refit 後驗證集 **`pick_threshold_dec026`** 與 HPO 試驗共用 **`FIELD_TEST_HPO_MIN_ALERTS_PER_HOUR`** + 同一 payout span（winner-pick 密度對齊）。
- `trainer/training/threshold_selection.py` 已具備 DEC-026 shared selector、`min_alert_count` / `min_alerts_per_hour` guards 與 fallback semantics。
- `trainer/training/backtester.py`：`compute_micro_metrics` 已與 trainer 測試指標鍵對齊 **`test_precision_prod_adjusted`** 與各 **`test_precision_at_recall_*_prod_adjusted`**，並補上 **`None -> single reason_code`**（`*_reason_code`）契約；`test_recall` / `alerts_per_hour` 等原有扁平鍵保留。
- `trainer/scripts/build_field_test_objective_precondition.py` 可產出 precondition JSON/MD；trainer 可經 **`FIELD_TEST_OBJECTIVE_PRECONDITION_JSON`** 讀取並寫入 `training_metrics` overlay（W1 gate 基礎已就緒）。
- `trainer/scripts/build_w1_freeze_evidence.py` 可彙整 precondition + run artifacts，輸出 **W1 freeze evidence JSON/MD**（可在缺少多窗 backtest 的筆電環境先產出 machine-readable 證據包）。
- `trainer/scripts/run_precision_uplift_bundle.py` 已提供一鍵入口：可在同一命令中執行訓練（`trainer.trainer`）並選配串接 W1 freeze evidence、W2 parity report；支援 `--precondition-json` 注入與 run dir 自動探索（降低手動 orchestration 成本）。
- `trainer/serving/scorer.py` 與 `prediction_log` / `runtime_rated_threshold` 已提供後續 DEC-032 對齊落點。
- **W2 run contract（程式層）**：`trainer/core/config.py` 之 **`SELECTION_MODE`**；`save_artifact_bundle` 寫入 **`training_metrics.json`**（`selection_mode` + `production_neg_pos_ratio`）；**`trainer/core/bundle_run_contract.read_bundle_run_contract_block`** 為 SSOT；**`backtest_metrics.json`** 與 **`load_dual_artifacts`** 頂層皆含 `selection_mode` / `selection_mode_source` / `production_neg_pos_ratio`（scorer 載入時 `logger.info` 審計）。
- **W2 證據工具（trainer 範圍）**：`trainer/scripts/report_w2_objective_parity.py` 可彙整多個 run 目錄，輸出 objective 對照 **CSV + Markdown** 與欄位對照快照；`trainer/scripts/calibrate_threshold_from_prediction_log.py --run-batch-calibration` 可由 SQLite `prediction_log` + `prediction_ground_truth` 自動選閾值並寫入 `calibration_runs`（可選擇同步 state DB）。

### 1.2 尚未完成能力（執行時視為限制）

- 依 **DEC-043**，本輪 run contract 固定 `selection_mode=field_test`；無有效 precondition／無 validation span／不滿足可行域時，視為 **`GATE BLOCKED`**（不納入可比 run），不以 AP fallback 充當可比結果。**此 fail-fast 行為已落地於 `run_optuna_search()`。**
- **訓練主路徑（Run-ready）**：已可用單指令 `trainer.trainer` 進行訓練與 Optuna（field-test objective）；不要求每次訓練都先產出 parity/freeze 報告工件。  
- **離線** trainer ↔ backtester 之 prod-adjusted / DEC-026 欄位已對齊；**`selection_mode` 與 bundle 契約**已寫入 `training_metrics` / `backtest_metrics` 並由 scorer 讀取；**state DB `runtime_rated_threshold.selection_mode`** 已可經 `upsert_runtime_rated_threshold` / `calibrate_threshold_from_prediction_log --selection-mode` 寫入（既有 DB 自動 `ALTER` 遷移）。**`calibration_runs`**：`insert_calibration_run_row` 將 W2 契約（`read_bundle_run_contract_block`）合併入 `summary_json`；校準 CLI 具備 `--log-calibration-run` 與 `--run-batch-calibration`。**仍待**：批次流程的排程化與長期穩定運行（定期 job / 監控）。
- 同一契約下 **多窗**「AP objective vs field-test objective」對照 **腳本能力已具備**（`report_w2_objective_parity.py`），且 W1 freeze evidence 已可由 `build_w1_freeze_evidence.py` 產出；但 **正式多窗報告工件（CSV/MD）與凍結欄位對照表版本**仍待產出（W2 Decision-grade DoD 未滿）。
- CatBoost / XGBoost bakeoff 尚未形成固定報表與單模公平比較基線。
- 二階段模型尚未有 PoC artifact / serving / evaluation 契約。

### 1.3 當前任務狀態摘要（對齊 §4）

| 任務 | 狀態 | 備註 |
| :--- | :--- | :--- |
| `W1 / R1 Optuna precondition + objective freeze` | **🟡** | `DEC-043` 已入決策；precondition builder 已改為 `BLOCKED` + 固定 reason code；trainer 已做 **GATE BLOCKED**；`build_w1_freeze_evidence.py` 已可輸出 freeze evidence。仍待：fold evidence 路徑蒐集流程固定化與多窗版本化欄位對照 |
| `W2 / R1 Optuna objective implementation parity` | **🟡** | **Run-ready 已達成**：`run_optuna_search` field-test-only fail-fast（無 AP fallback）、`selection_mode=field_test` artifact 封板、backtester `None -> single reason_code`。**Decision-grade 未滿**：缺正式多窗對照工件與校準批次排程化 |
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
- `selection_mode`（本輪依 **DEC-043** 固定為 `field_test`）
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
| **🟡** | `W1 / R1 Optuna precondition + objective freeze` | 1. 盤點 folds 的正例數、rated bet 數、`fold_duration_hours`、baseline `T_feasible` 集合大小、test neg/pos ratio。 2. 明確定義 Optuna / HPO 要採用的 constrained objective、fail-fast semantics、guardrails。 3. 決定是否允許單一 objective 或需 `BLOCKED`。 | `DS owner` | 現有 trainer/backtester 基線可用、validation folds 與評估資料可取得 | `out/precision_uplift_field_test_objective/field_test_objective_precondition_check.json`、`trainer/precision_improvement_plan/field_test_objective_precondition_check.md`、objective 設計摘要 | 有完整 precondition 產物；明確寫出 `selection_mode`、Optuna objective 定義、fallback semantics、是否允許單一 constrained objective；不存在未定義欄位 |
| **🟡** | `W2 / R1 Optuna objective implementation parity` | 1. 在 `run_optuna_search()`、winner-pick / early stopping 與 trainer / backtester 報表中對齊 objective 定義。 2. 確認欄位輸出含 `precision_raw` / `precision_prod_adjusted` / `recall` / `alerts_per_hour`（**本輪決策**：`precision_raw := test_precision`，不新增實體欄位；trainer/backtester 以 `test_precision` + `test_precision_prod_adjusted` 輸出）。 3. 以同一契約的多個資料窗比較 AP objective vs field-test objective，形成多窗對照報告（**仍待**）。 | `DS owner` | `W1` | objective 對照報告（多窗）、欄位對照表、run config 凍結紀錄 | **Run-ready 已達成**：單指令訓練可用、`run_optuna_search()` 與 artifact 語意一致、fallback 可辨識；**Decision-grade 仍須**多窗對照報告、凍結欄位對照表、批次校準排程化與長期監控 |
| **⬜** | `W3 / R2 ranking-focused training matrix` | 1. 定義 weighting / hard-negative / top-band reweighting 的最小矩陣。 2. 跑小矩陣實驗。 3. 保留版本化配置與結果。 4. 輸出主指標 uplift 與穩定性摘要。 | `DS owner` | `W1`、`W2` | ranking config matrix、實驗報告（含主指標 uplift / 穩定性摘要）、保留/淘汰建議 | 至少一組配置完成同契約比較；結果能回答是否值得進一步擴展，且有主指標 uplift 與穩定性摘要；無 silent resource blow-up |

#### 4.1.1 `W1` 當日最小可執行切面（Kickoff Checklist）

> 目標：在不改 architecture 的前提下，先把 `W1` 做到「可重跑、可審核、可阻擋跳關」。

| 狀態 | 子項 | 內容 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- |
| **✅** | `W1-C1 precondition schema freeze` | 凍結 precondition JSON 最小欄位：`run_id`、`window`、`fold_stats[]`、`t_feasible_stats`、`test_neg_pos_ratio`、`production_neg_pos_ratio_assumption`、`single_objective_allowed`、`blocking_reasons[]`，並固定 `allowed_reason_codes` 與 `objective_decision=BLOCKED` 語意。 | `field_test_objective_precondition_check.json` schema 區塊或等價欄位 | 欄位定義完整且無待定 placeholder |
| **🟡** | `W1-C2 fold evidence collect` | 依同一契約收集各 fold：正例數、finalized TP 數量級、rated bet 數、`fold_duration_hours`、baseline `T_feasible` 集合大小。 | precondition JSON `fold_stats[]` | 腳本可聚合手動餵入之 fold metrics；已定義固定化蒐集流程（見 §4.1.3），待依該流程連續落地至少一次正式 run |
| **🟡** | `W1-C3 objective decision` | 依 `W1-C2` 證據判斷：`single constrained objective` 或 `BLOCKED`；明確寫 fallback fold semantics（本輪為 no-fallback）。 | `field_test_objective_precondition_check.md` 決策段落 | 已依 **DEC-043** 凍結：`recall_floor=guardrail`、可行域不滿足一律 `gate_blocked`、`None -> 單一 reason_code`，且 precondition script 已輸出 `BLOCKED` 決策語意；仍待把所有 reason code 證據串進正式 freeze 工件 |
| **🟡** | `W1-C4 gate readiness` | 將 `selection_mode`、objective 定義、fallback semantics 寫回 run contract，並生成本輪 objective freeze 摘要。 | objective 設計摘要 + run contract 凍結紀錄 | 已可經 `FIELD_TEST_OBJECTIVE_PRECONDITION_JSON` 寫入 `training_metrics.json`；**config `SELECTION_MODE` 已凍結寫入 artifact**；`run_optuna_search` 已實作 `GATE BLOCKED`；`build_w1_freeze_evidence.py` 已可產出 freeze evidence；仍待 fold 證據蒐集流程固定化與多窗證據串接 |

#### 4.1.2 `W1` 阻擋規則（Fail-fast）

- 任一 fold 的關鍵欄位缺失且無 reason code：標記 `GATE BLOCKED`，不得進 `W2`。
- 任一 fold 的 `T_feasible` 過小、常為空，或尾段支撐不足：不得硬切單一 constrained objective；本輪依 DEC-043 直接 `BLOCKED`，不以複合目標視為同列可比。
- `PRODUCTION_NEG_POS_RATIO` 假設無法交代來源或敏感度：不得把 `prod_adjusted` 作唯一 driver。
- precondition 只產生 markdown、無 machine-readable JSON：視為未完成 `W1`。
- 依 **DEC-043**：任何不滿足 field-test objective 可行域條件之 run，一律 `GATE BLOCKED`；`None` 指標必須且僅能對應單一 reason code。

#### 4.1.3 `W1-C2` fold 路徑蒐集固定流程（MVP，trainer 範圍）

> 目的：避免每次靠人工臨時找檔，導致漏 fold 或同 run 使用不同契約。

- **路徑清單工件**：每次正式 run 先產生一份路徑清單檔（純文字，一行一個檔案），建議命名：
  - `out/precision_uplift_field_test_objective/fold_metrics_paths_<run_id>.txt`
- **最小要求**：
  - 僅允許 `.json` 檔案。
  - 同一清單內不可重複路徑。
  - 清單中的所有檔案需存在，缺一即視為 `W1` 未完成。
- **precondition 產生（固定模板）**：
  - 將路徑清單展開為 `--fold-metrics-json` 重複參數後，呼叫
    `trainer/scripts/build_field_test_objective_precondition.py` 產生 JSON/MD。
- **trainer 注入（固定模板）**：
  - 以 `FIELD_TEST_OBJECTIVE_PRECONDITION_JSON=<precondition_json_path>` 啟動 `trainer.trainer`，
    不在訓練命令中臨時改 contract。
- **一鍵執行（建議模板）**：
  - 優先使用 `trainer/scripts/run_precision_uplift_bundle.py`，以單命令統一：
    1) 訓練（必選）  
    2) freeze evidence（選配）  
    3) parity report（選配）
  - 最小訓練範例：
    `PYTHONPATH=. python -m trainer.scripts.run_precision_uplift_bundle -- --max-optuna-trials 20`
  - 含 precondition + freeze/parity 範例：
    `PYTHONPATH=. python -m trainer.scripts.run_precision_uplift_bundle --precondition-json out/precision_uplift_field_test_objective/field_test_objective_precondition_check.json --emit-w1-freeze-evidence --emit-w2-parity --w2-run-dir-glob "out/models/*" -- --max-optuna-trials 20`
- **freeze evidence 產生（固定模板）**：
  - 訓練完成後，呼叫 `trainer/scripts/build_w1_freeze_evidence.py`，至少帶入：
    - `--precondition-json <...field_test_objective_precondition_check.json>`
    - `--run-dir <model_run_dir>`
- **W1-C2 升級為 ✅ 的條件**：
  - 至少一次正式 run 完整留下：
    1) 路徑清單檔  
    2) precondition JSON/MD  
    3) 對應 run 的 freeze evidence JSON/MD  
  - 且三者的 `run_id` 可互相 trace。

#### 4.1.4 `R2` 最小實驗矩陣（Laptop-friendly，前置達成後可執行）

> 目的：先用最小成本確認「高分帶 FP 壓制」是否有方向性 uplift；此階段輸出屬 comparative，不宣告 decision-grade。  
> 啟動前置：需符合 §6.1（`W3` 僅在 `W1` / `W2` 契約與欄位可比後開始）。

- **固定契約（不得變動）**：
  - 沿用 `R1` 封板契約：`selection_mode=field_test`、`GATE BLOCKED` fail-fast、同一 objective 語意。
  - 不在 `R2` 期間改動資料窗定義、標籤契約、`PRODUCTION_NEG_POS_RATIO` 假設來源。
- **最小配方矩陣（3 組）**：
  - `r2_baseline`：延續當前 `R1` 既有訓練配方（作為對照組）。
  - `r2_weighted_light`：輕量正負樣本權重調整（避免一次過強 reweight）。
  - `r2_hnm_light`：輕量 hard-negative/mining 比率（僅小幅度）。
- **執行模板（單機資源保護）**：
  - 每組先跑小試：`max-optuna-trials` 低量（例：`10~20`），確認可跑再擴。
  - 不同配方分批跑（序列化），避免筆電 RAM/CPU 峰值疊加。
  - 優先用 `trainer/scripts/run_precision_uplift_bundle.py`，每次 run 必帶配方標記（`recipe_id`）進輸出摘要。
- **命名與落檔規則（避免混檔）**：
  - run label：`<date>-<recipe_id>-<shortsha>`（例：`20260422-r2_weighted_light-ab12cd`）。
  - 比較輸出：`out/precision_uplift_r2/r2_matrix_summary.csv` + `trainer/precision_improvement_plan/r2_matrix_summary.md`。
  - 每列至少含：`run_id`、`recipe_id`、`objective_mode`、`test_precision`、`test_precision_prod_adjusted`、`test_recall`、`alerts_per_hour`、`gate_blocked_reason_code`。
- **`W3` 升級為 ⏳ 的最小條件**：
  - 至少完成 1 組非 baseline 配方的可重跑 run，且能與 baseline 以同欄位並列比較。
- **`W3` 維持/升級判定（本輪）**：
  - 若三組皆無方向性 uplift：`W3` 保持 comparative、記錄「暫不擴矩陣」。
  - 若至少一組有穩定 uplift（且非偶發）：才擴到下一輪矩陣。

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

- `W1`：若任一 fold 的 `T_feasible` 過小或常為空，不得硬切單一 constrained objective（本輪輸出 `BLOCKED`）。
- `W2`：trainer / backtester 必須對齊同一 objective contract 與 fallback semantics。
- `W3`：ranking-focused 實驗至少要輸出主指標 uplift 與穩定性摘要，不接受只報單次最佳結果。
- `W4`：模型家族比較必須是同特徵、同切分、同 objective、同報表，不接受 apples-to-oranges。
- `W5`：二階段只能在 top-band FP 仍為主瓶頸、且殘餘 gap 與 serving / artifact / parity 成本判定都已明確記錄時進場，不得憑直覺跳關。
- `W6`：PoC stub 必須包含與單階最佳基線的公平對照設計，不得只寫二階段自身 train/eval 流程。

### 5.3 本輪最終 DoD

- `W1`~`W5` 均有對應輸出，且能 trace 回同一上游 implementation plan。
- 已明確回答：是否允許單一 constrained objective、哪一組 ranking-focused 設定值得保留、哪個單模 winner 暫時領先、R4 是否可進場。
- 若任一結論證據不足，明確降級為 exploratory / comparative，不偽裝成 decision-grade。

### 5.4 W2 分層 DoD（避免阻擋主路徑）

- **W2 Run-ready DoD（先達成）**：
  - 可用單指令訓練 + Optuna（field-test objective）。
  - 無有效 precondition / span 時明確 `GATE BLOCKED`（非 AP fallback）。
  - `training_metrics` / `backtest_metrics` / scorer 契約欄位一致可讀。
- **W2 Decision-grade DoD（後達成）**：
  - 多窗 parity 正式工件（CSV/MD）與版本化欄位對照完成。
  - 校準批次具排程化、監控與失敗告警。

---

## 6. Gate 與升級規則（執行層）

### 6.1 進入下一階段條件

- `W2` 只有在 `W1` objective contract 凍結後才能**宣告整列 DoD 完成（✅）**；目前已允許在 **W1 部分落地**（precondition 可讀、Phase 2 可注入 env）下，先完成 **W2 程式層 parity**（見 §1.1 / §1.3），**不**等同 W2 已結案。
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
- One-command bundle runner：`trainer/scripts/run_precision_uplift_bundle.py`
- W2 parity report script：`trainer/scripts/report_w2_objective_parity.py`
- Calibration CLI：`trainer/scripts/calibrate_threshold_from_prediction_log.py`

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
