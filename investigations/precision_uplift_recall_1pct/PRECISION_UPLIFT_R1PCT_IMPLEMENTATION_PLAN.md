# Precision Uplift R1PCT Implementation Plan

> 角色：工程實作計畫（Implementation Plan）。  
> 來源契約：名詞、能力現況、治理與 Gate 以 **`../../.cursor/plans/PLAN_precision_uplift_sprint.md` §8–§12** 為準（與 §1 衝刺目標、§7 `slice_contract` 同檔單一 SSOT）。  
> **Phase 1 錯誤切片（`slice_contract`）**：分段定義之單一真相見 **`../../.cursor/plans/PLAN_precision_uplift_sprint.md` §7**；W1-B2 實作須逐條對齊該節。  
> 原則：本文件只管「要做什麼、做到哪裡、完成定義是什麼」。

---

## 1. 版本與狀態快照

- 最後更新：`2026-04-20`（W1-B2：`slice_contract.py` + collector 內嵌 spec + gate incomplete + `slice_performance.json`；W1-B3 標籤稽核半自動；§7 `slice_contract`）
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

契約全文：**`../../.cursor/plans/PLAN_precision_uplift_sprint.md` §7**（rated-only、`T0` as-of profile、十分位／tenure 規則、`slice_data_incomplete` 觸發條件）。以下為工程對照清單（實作時不得改用舊稱 `player_tier` / 未約定之 `activity_bucket` 語意）。

- [x] **純計算 + 可選內嵌 spec（MVP）**：`orchestrator/slice_contract.py` 之 `build_slice_contract_bundle`；Phase 1 config 可選 **`slice_contract`**（`T0`、`eval_rows`、`profiles`、…）由 `collect_phase1_artifacts` 併入 bundle；`slice_performance_report.md` 輸出 **`slice_contract`** JSON 區塊。單測：`tests/unit/test_slice_contract_w1b2.py`。
- [ ] **資料 join**：以與 **`precision@recall=1%`** 相同之 **eval／holdout 樣本列**（grain）為準；僅 **rated**；每列具 `decision_ts`（HKT 曆日見 §7.4）、`table_id`；能對每位 `canonical_id` 取得 **§7.2 T0 as-of** 之 `player_profile` 一列（`theo_win_sum_30d`、`active_days_30d`、`turnover_sum_30d`、`days_since_first_session`）。
- [ ] **分桶計算**（與 §7 一致）：
  - `eval_date`、`table_id`（§7.4–7.5）；
  - `adt_percentile_bucket`：`ADT_30d = theo_win_sum_30d / active_days_30d`，玩家級常數（T0 profile），**十分位**於該次 eval **全體 rated 列**上估（列加權語意 §7.6）；
  - `tenure_bucket`：`days_since_first_session` 玩家級常數 + **固定區間**（§7.7）；
  - `activity_percentile_bucket`：`active_days_30d` 玩家級常數 + **十分位**（§7.8）；
  - `turnover_30d_percentile_bucket`：`turnover_sum_30d` 玩家級常數 + **十分位**（§7.9）；
  - **不**實作已廢止之 `value_tier` / `player_tier`（§7.10）。
- [x] **Gate：`slice_data_incomplete` → `blocking_reasons`**：`evaluate_phase1_gate` 讀 `bundle["slice_contract"]`；若 `slice_data_incomplete` 為真，寫入 **`slice_data_incomplete`** 與 **`slice_contract:<code>`**；預設狀態 **PRELIMINARY**（`thresholds.slice_contract_incomplete_status` 可設 **`FAIL`** 強制失敗）。另：`slice_contract:asof_contract_unavailable_strict` 會 **強制 FAIL**。單測：`test_gate_preliminary_when_slice_contract_incomplete`、`test_gate_fail_when_slice_incomplete_status_fail`、`test_gate_fail_when_slice_asof_contract_unavailable_strict`。
- [ ] **Assertion 與 `slice_data_incomplete`（bundle 內容）**（§7.3、§7.12）：`slice_contract.py` 已對內嵌列做 assertion／`blocking_profile_codes`；**仍待**與真實 eval／profile join 後之完整證據鏈與（若需要）**BLOCKED** 語意定稿。
- [ ] **collector**：`collect_phase1_artifacts` 已支援 **內嵌** `slice_contract` spec；若 spec **未**含 `eval_rows` 且 `auto_eval_rows_from_prediction_log=true`，可由 SQLite 自動組 `eval_rows`（`prediction_log` 視窗內 `is_rated_obs=1` + `validation_results` finalized label）；若 spec **未**含/為空 `profiles` 且 `auto_profiles_from_state_db=true`，可依 `eval_rows.canonical_id` 回查 `state_db.player_profile` 五欄（`theo_win_sum_30d`/`active_days_30d`/`turnover_sum_30d`/`days_since_first_session`），且當表含 `as_of_ts` 時採 **`<= T0` 最近一筆**。**來源策略**：以 `state_db` 為 primary；當 `state_db` 不可行（缺欄/缺表/不可用）時，先嘗試 **Parquet fallback**（`profile_parquet_path`，DuckDB 讀取），仍失敗且 `auto_profiles_from_clickhouse=true` 時再走 **ClickHouse fallback**（最小查詢、按 canonical_id 小批 IN，`SOURCE_DB.TPROFILE` 可覆蓋）。若 spec **未**含 `recall_score_threshold`：先 R1 **`threshold_at_target`**，否則 **`backtest_metrics.threshold_at_recall_0.01`**（`model_default`／`rated`）。已自動附帶 `slice_contract_version`（預設 `plan7-sha256:<16>`）與 `slice_contract_plan_hash_sha256` / `slice_contract_plan_section`。**仍待** §7 全項驗收。
- [x] **報告**：`slice_performance_report.md` 含 **`slice_contract`** JSON 區塊；bundle 含 **`slice_contract`** 鍵時另寫 **`slice_performance.json`**。`top_drag_slices` 欄位格式對齊 W1-B2（真資料 Top10 仍待驗收）。
- [ ] **效能**：優先 **T0 上每 `canonical_id` 一列 profile** 再 join eval 列；十分位切點於聚合後小表計算；避免不必要之全量列 materialize（筆電 RAM，§6.2）。

#### W1-B3 標籤品質（label noise）：**完整記錄優先**；分級／全自動 gate **待資料審閱後再凍結**

> **治理**：`label_bottleneck_assessment` 之門檻映射、高分 FP「判因」枚舉是否收斂、以及「無已核准修復計畫 → 自動升級 BLOCK／timeline」等行為，在**未經實際資料審閱並凍結規則表前**不實作為**唯一依據的全自動 gate**（避免假精確）。在此之前，每次 run 必須輸出 **人類可讀 + 機器可讀之完整證據**，供事後定規則與簽核。

- [ ] **定義（與 repo 對齊；寫入 audit bundle）**  
  - **censored**：`trainer/labels.py` H1／右截尾：`censored=True` 為該 `canonical_id` 之 terminal 注單，且 `payout_complete_dtm + WALKAWAY_GAP_MIN` 無法在 `extended_end` 前被觀測滿足 → 訓練／嚴謹評估排除。輸出須註明所用 **`window_end` / `extended_end`、WALKAWAY_GAP_MIN、label 程式版本或 fingerprint**。  
  - **lag**：`decision_ts` → **ground truth 穩定**時刻；**`gt_stable_ts` 之欄位來源與版本**須在 bundle 註記（資料驗證後可收斂為 **本衝刺計畫 §9** 單一句）。  
  - **人口**：僅 **rated**（與 Phase 1 其餘口徑一致）。

- [ ] **必落地輸出（缺任一視為 W1-B3 未完成）** — `phase1/label_noise_audit.md` **與** `phase1/label_noise_audit.json`（或 collector 中等價、與 md 同源）：  
  - censored：**計數與比例**；分母（rated eval 列／注單列等）**必須文字說明**；並附與 run 契約 `exclude_censored` 是否一致之檢查欄位。  
  - lag：於可算 lag 之子集上之 **`<=1d` / `1-3d` / `3-7d` / `>7d` 分桶**（計數+比例）；**`gt_stable_ts` 缺失**之列數與比例。  
  - 高分 FP 抽樣：**逐列清單表**（每列固定欄位，允許人工欄為空但欄位不可刪）：至少含關聯 id（`bet_id` 或契約鍵）、`canonical_id`、`decision_ts`、分數或 alert 依據、決策當下所用 `label`、`gt_stable_ts`、`lag_bucket`、`censored`（若可得）、**`review_status`**（`pending` \| `reviewed`）、可選 **`reviewer_adjudication`**／**`reviewer_notes`**。  
  - 摘要欄：`label_audit_evidence_complete`；**`label_audit_pending_human_decision: true`** 直至閾值／枚舉規則凍結（凍結後改 `false` 並附 `label_audit_rules_version`）。

- [ ] **`label_bottleneck_assessment`（半自動）**：在無凍結門檻表前，固定輸出 **`pending_data_review`**（或等價單一枚舉），並在 md **醒目段**聲明：實際 `none|minor|major|blocking` 待人類依本 run 數據定案。**禁止**輸出偽造之 `none|minor|major|blocking`。規則凍結後才可輸出真實等級並附觸發證據索引。

- [ ] **Gate（W1-B3）**：規則凍結前，**不得**僅憑 `label_bottleneck_assessment` 將 Phase 1 標為 `FAIL`／`BLOCKED`；可輸出 `WARN` 或 **`label_quality_human_review_recommended`**，以及敘述性 **timeline 重排建議**（對齊衝刺計畫語意，**不**自動改 W2+ 排程狀態）。凍結後若需自動化：以 config **顯式 `label_audit_auto_gate_enabled: true`** 才允許「major／blocking + 無修復計畫 → 升級」類邏輯。

- [ ] （規則凍結後、可選）已核准修復計畫之機讀欄位與上項 auto-gate 聯動。

**W1-B3 本階段 DoD**  
- 任一正式 run 可僅憑工件還原：**censored 量、lag 分佈、FP 抽樣全表、gt_stable 缺失率**，無需翻 log。  
- `phase1_gate_decision.md`（或 bundle）明示：標籤子證據已完整落地；**bottleneck 自動 gate 未啟用或未配置**。

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
  - **`slice_data_incomplete`**：模擬 §7.12 觸發（例如 **T0 無 profile**、**`theo_win_sum_30d` 為 NULL**、**`active_days_30d` 小於 1**、**`turnover_sum_30d` 為 NULL**）→ `blocking_reasons` 含預期代碼／gate 預期狀態
  - label audit：`label_noise_audit.json` 含 censored／lag 分桶／FP 清單欄位；`label_audit_pending_human_decision=true`；**不得**僅因未凍結之 assessment 欄位而單獨將 gate 標為 `FAIL`（預設半自動契約）
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
- **切片計算放大風險**：§7 已定 **六類 marginal 維度**（含三個十分位）；實作先做 **單維 marginal** 與必要 join，**低階 joint** 僅在 `min_n` 門檻與資源評估通過後擴充；避免高階笛卡兒積一次全開。

---

## 7. 更新規則

- 任務狀態只在本檔更新（不要在 runbook 重複打勾）。
- 每次狀態變更要附「為何變更」一句描述，避免只改勾選。
- **`slice_contract` 變更**：先改 **`../../.cursor/plans/PLAN_precision_uplift_sprint.md` §7**，再同步本檔 W1-B2、Runbook／config 說明；禁止只改 collector 而不改上游契約。
- 若能力未完成，`PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md` 必須標示為「限制」，不可寫成可用。
