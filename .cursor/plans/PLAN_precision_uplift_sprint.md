# Precision 提升衝刺計畫（Recall=1%）

> **單一 SSOT**：本檔同時為 **衝刺目標／Phase 路線**、**Phase 1 `slice_contract`（§7）**，以及 **調查專案 `investigations/precision_uplift_recall_1pct` 之文件分工、能力邊界、Gate 契約（§8–§12）**。原 `investigations/.../PRECISION_UPLIFT_R1PCT_SSOT.md` 已併入本檔，請勿再新增第二份調查 SSOT。  
> 最後更新：2026-04-20（§2 標籤品質稽核列對齊 §11.1／W1-B3；§8–§12 合併原調查 SSOT；§7 `slice_contract`；執行策略仍對齊 Autonomous-first）  
> 目標：在相同評估口徑下，將 `precision@recall=1%` 由目前約 40% 提升至 **>=60%**。

---

## 1. 成功定義與評估契約

| 項目 | 定義 |
| :--- | :--- |
| 主指標 | `precision@recall=1%` |
| 目標門檻 | `>= 60%` |
| 評估約束 | 同資料切分、同時間窗、同標籤定義（避免口徑漂移） |
| 穩定性要求 | Forward/Purged 時序驗證平均達標，且波動可控 |
| 上線門檻 | 不只單一 holdout 漂亮，需跨窗一致成立 |

### 1.1 執行策略（對齊文件）

- 預設採用 `run_pipeline.py --phase all --mode autonomous` 單一命令流程。
- Manual/ad-hoc 流程僅作 fallback（除錯、緊急接手），非日常主流程。

---

## 2. 四週執行路線圖（Sprint Plan）

### Phase 1：根因診斷（RCA）與上限拆解

| 任務 | 具體內容 | 產出 |
| :--- | :--- | :--- |
| 歷史紀錄對照（STATUS） | 對照 `STATUS.md` 過去迭代，盤點是否已有相同或相近的 label noise / lag / censored 發現被擱置，並標記「可直接沿用 / 需重驗 / 已失效」。 | `status_history_crosscheck` |
| 錯誤切片分析 | 依 **§7 `slice_contract`** 維度切片，檢查 `precision@recall=1%` 與樣本占比（rated-only；維度名見 §7）。 | `slice_performance_report`，列出 top 拖累切片 |
| 標籤品質稽核 | **rated-only**（與 §7 族群一致）。量化 **censored**（`trainer/labels.py` H1 右截尾語意）與 **lag**（`decision_ts` → ground truth **穩定**時刻；分桶與 `gt_stable_ts` 缺失率等）；抽樣高分 false positive 並保留逐列證據。**產出與 Gate 細節**（`label_noise_audit` 之 md+json 同源、`label_audit_pending_human_decision`、凍結前不以 bottleneck 單獨 FAIL／BLOCKED）：見 **§11.1** 與 `investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` **W1-B3**。 | `label_noise_audit`（`phase1/label_noise_audit.md` + `label_noise_audit.json`） |
| 特徵可用時點對齊 | 確認 train/serve 特徵 timestamp 對齊與無 leakage。 | `point_in_time_parity_check` |
| 現行上限確認 | 在固定契約下重跑「已知 threshold」上限測試，驗證 40% 結論可重現。 | `upper_bound_repro` |

**Phase 1 Gate**：完成 RCA，明確指出「模型限制 vs 標籤/資料限制」主因排序，並完成 `STATUS.md` 對照。
若結論顯示「標籤流程/資料契約」是主因，則啟動 **Timeline 重排**（先資料修復，後模型衝刺）。

---

### Timeline 重排規則（Phase 1 觸發）

| 觸發條件 | 動作 |
| :--- | :--- |
| `label_noise_audit` 判定主要瓶頸在標籤流程（例如延遲標註、censored 處理、契約不一致） | 將 Phase 2~4 改為「資料/標籤修復優先」：先修標註與契約，模型 A/B/C 順延 |
| `status_history_crosscheck` 發現歷史上已有同類問題且曾暫緩 | 將該議題提升為本輪必做，要求附「為何當時暫緩、此次是否解除阻礙」說明 |
| 觸發重排後一週內仍無法收斂標籤品質指標 | 啟動 scope cut：暫停 ensemble 與大規模特徵擴張，集中修復資料鏈路 |

---

### Phase 2：高槓桿模型策略（A/B/C 並行）

| Track | 任務 | 具體內容 | 預期效果 |
| :--- | :--- | :--- | :--- |
| A | 排序導向訓練 | 強化 class weighting / focal-like 權重，優先優化前段排序品質。 | 提升 top 段 precision |
| A | Hard Negative Mining | 對「高分但實際為負」樣本加權回訓。 | 直接降低誤報 |
| B | 分群建模 + Gating | 以玩家狀態/活躍度等路由到子模型（2~4 群起步）。 | 減少單一模型欠擬合 |
| C | 穩健時序驗證 | Forward/Purged CV，輸出 mean/std，過濾不穩配置。 | 防止單窗幻覺 |

**Phase 2 Gate**：至少 1 條路線相對基線有顯著 uplift（建議門檻：+3~5pp）。

**調查 repo 對照（`run_pipeline.py --phase phase2`）**：細項見 [`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`](../../investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md) **W2** 與 `evaluators.evaluate_phase2_gate`。

| Sprint 語意 | Orchestrator 契約（摘要） |
| :--- | :--- |
| 至少一條路線 uplift | 啟用軌道內 **per-job PAT@1% 預覽**：任一行 challenger 對 **YAML 序** baseline 之 uplift ≥ `gate.min_uplift_pp_vs_baseline`（範例 **3.0 pp**，落在 +3~5pp 建議下緣）→ **`PASS`** |
| 跨窗／波動可控 | 可選 **`phase2_bundle.phase2_pat_series_by_experiment`** + `gate.max_std_pp_across_windows`：**uplift 已 PASS** 且任列樣本 stdev（pp）超標 → **`FAIL`**；無手寫多窗時 **`collectors.merge_phase2_pat_series_from_shared_and_per_job`** 可組兩點 MVP 序列 |
| 產物／CI | **`phase2/phase2_gate_decision.md`**、**`phase2/track_*_results.md`**（uplift、PAT 序列與 std 摘要）；可選 **`--phase2-fail-on-gate-fail`**（exit **9**）、**`--phase2-fail-on-gate-blocked`**（exit **10**） |

---

### Phase 3：特徵深化與集成加碼（在勝者路線上）

| 任務 | 具體內容 | 產出 |
| :--- | :--- | :--- |
| 動態行為特徵 | 建立短中長期差值、變化率、波動度、連續性特徵。 | `feature_uplift_table` |
| 針對拖累切片做 feature pack | 僅對 top 拖累切片擴增最相關特徵，避免全域盲擴。 | `slice_targeted_features` |
| 分群後集成 | 群內最佳模型 + 群間融合（非盲目堆疊）。 | `ensemble_ablation` |
| 高分段校準 | 在高分區段做專門校準與 decision policy 檢查。 | `top_band_calibration_report` |

**Phase 3 Gate**：在 Phase 2 勝者基礎上再提升，且不犧牲跨窗穩定性。

---

### Phase 4：定版、回放與上線決策

| 任務 | 具體內容 | 產出 |
| :--- | :--- | :--- |
| 最終候選定版 | 鎖定資料窗、特徵集、模型設定、閾值規則。 | `candidate_freeze` |
| 多窗回放驗證 | 以多時間窗重跑主指標 + 切片指標。 | `multi_window_backtest` |
| 上線影響估算 | 告警量、誤報量、業務 KPI 變化預估。 | `impact_estimation` |
| Go/No-Go 會議包 | 匯總證據，做上線或延後判斷。 | `go_no_go_pack` |

**Phase 4 Gate**：主指標達標且跨窗穩定，才進入上線流程。

---

## 3. 實驗矩陣（標準記錄格式）

所有實驗統一記錄以下欄位，避免結果不可比較：

| 欄位 | 說明 |
| :--- | :--- |
| `experiment_id` | 唯一識別碼（含日期與路線） |
| `data_window` / `split_protocol` | 資料窗與切分規則 |
| `label_contract` | 標籤定義版本與觀測窗 |
| `feature_set_version` | 特徵版本與是否含切片專用特徵 |
| `model_config` | 模型類別與主要參數 |
| `objective_variant` | 權重/目標函數策略（如 focal-like） |
| `precision_at_recall_1pct` | 主指標 |
| `pr_auc` / `top_k_precision` | 輔助指標 |
| `slice_metrics` | 各切片指標與樣本量 |
| `cv_mean_std` | 時序驗證均值與波動 |
| `decision` | keep / drop / iterate + 理由 |

---

## 4. 優先候選技術包（可並行）

1. **Hard Negative Mining + 重加權目標**
2. **分群建模（2~4 群）+ 輕量 gating**
3. **拖累切片定向特徵工程**
4. **高分段專門校準與 decision policy**
5. **Forward/Purged CV 驗證框架常態化**

---

## 5. 風險與止損規則

1. 若 Phase 1 顯示主因為標籤噪音/延遲，優先修資料與標籤流程，暫停大規模模型擴張。  
2. 若任一路線 uplift 小於 +3pp 且不穩定，立即降級投入，避免無限調參。  
3. 若結果僅在單一時間窗成立，不納入定版候選。  
4. Ensemble 若僅帶來微小提升但大幅增加複雜度，優先保留可維運性更高方案。  
5. 若 `STATUS.md` 對照證實問題早已存在但前提未解，先補齊阻礙清單與責任歸屬，再批准進入下一週。  

---

## 6. 交付節奏（建議）

- 每週固定一次 checkpoint：更新主指標、切片排名、路線保留/淘汰決策。  
- 每兩週一次決策會：是否切換主路線、是否提早進入定版。  
- 每次 checkpoint 必須附實驗矩陣更新，不接受口頭結論。  
- Phase 1 checkpoint 必附 `status_history_crosscheck`（含「歷史結論是否被沿用」對照表）。  

---

## 7. Phase 1 錯誤切片分析：分段定義（`slice_contract`）

本節為衝刺內 **Phase 1 錯誤切片** 之 **單一真相**（`slice_metrics`、orchestrator W1-B2、調查 repo 實作皆須對齊）。與 `player_profile` 欄位語意對齊處以 `doc/player_profile_spec.md` 及 `ssot/baseline_model_eval_ssot.md` §4.1 R3 為準。

### 7.1 範圍與 grain

- **族群**：僅 **`canonical_id` 屬 rated**（D2 有效 `casino_player_id` 歸戶）之樣本列。Unrated 不納入本契約。
- **切片 grain**：與主指標 **`precision@recall=1%`** 相同之 **eval／holdout 樣本列**（下稱 **eval 列**）。
- **時區**：`decision_ts` 之曆法日換算採 **`Asia/Hong_Kong`**，與本專案 run 契約一致。
- **`T0`**：該次 eval／holdout 契約所定義之 **評估窗起點時刻**（單一 timestamp）。下文 **「T0 as-of profile」** 均指此固定時點。

### 7.2 T0 as-of profile（玩家級常數之唯一來源）

對每位在該次 eval 內出現之 **`canonical_id`**：

1. 自 **`player_profile`** 取滿足 **`snapshot_dtm <= T0`** 之 **最新一筆**（as-of **T0**）。
2. 自該筆讀出下列欄位；同一玩家於 **整段 eval 內** 僅使用此一次取值（**全 eval 不變**）：
   - `theo_win_sum_30d`
   - `active_days_30d`（30d 內不重複 `gaming_day` 之個數，見 profile 規格）
   - `turnover_sum_30d`
   - `days_since_first_session`（tenure 用）

若某玩家在 **T0 無可用 profile 列**：該玩家相關列不參與依 profile 之切片，並觸發 **`slice_data_incomplete`**（§7.12）。

### 7.3 Assertion（硬條件；違反即 `slice_data_incomplete`）

對每一位在 §7.2 成功取得 T0 profile 之 rated 玩家，**同時**滿足：

1. **`active_days_30d >= 1`**（不得為 0；不得為契約下不可用之 NULL）。
2. **`theo_win_sum_30d` 非 NULL**（計算 ADT 之必要條件）。
3. **`turnover_sum_30d` 非 NULL**（§7.9 F 維度之必要條件）。

任一玩家違反以上任一條 → 本次 run 之切片結論標記 **`slice_data_incomplete`**，並應寫入 **`blocking_reasons`**；**不得**在違反仍存在時宣稱 profile 型 decile 切片為完整、可決策結論。

> **說明**：ADT 在數學上可寫 `theo / max(active_days, 1)` 以避免除零，但 **契約層級** 以 **`active_days_30d >= 1`** 為必成立；出現 0 視為資料或 PIT 對齊異常，不進入可信 decile 結論。

### 7.4 維度 A：`eval_date`

- **定義**：`eval_date = date(decision_ts AT TIME ZONE 'Asia/Hong_Kong')`。
- **粒度**：日。
- **`decision_ts` 缺失**：該列不納入切片聚合；計入資料完整性統計（是否升級為 §7.12 由 gate 規則決定）。

### 7.5 維度 B：`table_id`

- **定義**：eval 列主表之 **`table_id`**（全 repo 對外 **canonical 欄位名** 應單一鎖死）。
- **缺失**：`NULL` 或空字串 → 桶 **`UNKNOWN_TABLE`**。

### 7.6 維度 C：`adt_percentile_bucket`（十分位，10 桶）

- **前提**：§7.3 assertion 對該玩家已全部成立。
- **指標**：  
  **`ADT_30d = theo_win_sum_30d / active_days_30d`**  
  語意與 `ssot/baseline_model_eval_ssot.md` §4.1 R3（ADT 估算）一致；回溯窗 **固定 W = 30d**。
- **玩家級常數**：每位玩家僅一個 **`ADT_30d`**；該玩家所有 eval 列帶 **同一數值、同一 decile 標籤**。
- **分位切點**：在 **「該次 eval／holdout 之全體 rated eval 列」** 上，對每列所帶之 **該列玩家之常數 `ADT_30d`** 計經驗 **十分位**（九個切點），得到 **`adt_d1` … `adt_d10`**（或專案統一之等價命名）。
- **權重語意**：同一玩家多列會重複同一 `ADT_30d` → 十分位為 **以 eval 列為權重** 之經驗分佈（非「每玩家一票」）。
- **小樣本**：**不**因樣本量少而改粗粒度；維持 10 桶。極小樣本下 decile 不穩時，報表以 **`confidence_flag`** 標註，契約不另開豁免。

### 7.7 維度 D：`tenure_bucket`（新舊戶；固定區間，非十分位）

- **指標**：§7.2 T0 as-of 之 **`days_since_first_session`**（玩家級常數）。
- **分桶**（全專案固定；邊界開閉整 repo 一致即可）：  
  **`T0_seg`：`[0, 7]` 天**；**`T1`：`(7, 30]`**；**`T2`：`(30, 90]`**；**`T3`：`(90, ∞)`**。

### 7.8 維度 E：`activity_percentile_bucket`（十分位）

- **指標**：§7.2 T0 as-of 之 **`active_days_30d`**（與 §7.3 一致，必 **≥ 1**）。
- **玩家級常數 + 十分位**：每位玩家一值；每 eval 列帶該玩家常數；在 **該次 eval／holdout 全體 rated eval 列** 上估 **`activity_d1` … `activity_d10`**（權重語意同 §7.6）。

### 7.9 維度 F：`turnover_30d_percentile_bucket`（十分位）

- **指標**：§7.2 T0 as-of 之 **`turnover_sum_30d`**（30d 累計 turnover）；**非 NULL** 為 §7.3 強制條件。
- **玩家級常數 + 十分位**：同 §7.6／§7.8，得 **`to_d1` … `to_d10`**（或專案統一之命名）。

### 7.10 廢止／不使用的舊稱

- **`value_tier` / `player_tier`（舊「價值／玩家層級」）**：不再定義、不輸出；價值相關視角由 **`adt_percentile_bucket`**（§7.6）承擔。
- 舊表「玩家層級、新舊戶、下注額、活躍度」口語分別對應：**本契約不採 unrated 切片**；**新舊戶 = `tenure_bucket`**；**下注額 = `turnover_30d_percentile_bucket`**；**活躍度 = `activity_percentile_bucket`**；**日期 / table = `eval_date` / `table_id`**。

### 7.11 報表最低欄位（與調查 repo W1-B2 對齊）

每個切片鍵（單維 **marginal** 或事先約定之低階 **joint**）至少包含：

- `n`、`tp`／`fp`／`fn`、`precision_at_target_recall`、`delta_vs_global`、`confidence_flag`（小樣本或 decile 不穩時標註）。

### 7.12 `slice_data_incomplete` 觸發條件（匯總）

- 任一 rated eval 所涉玩家在 **T0 無可用 `player_profile` 列**。  
- §7.3 **任一 assertion 失敗**（含 **`theo_win_sum_30d` 為 NULL**、**`turnover_sum_30d` 為 NULL**、**`active_days_30d` 未 ≥ 1**）。

**治理**：變更本節須先更新本檔 **§7**，再同步 `investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`（W1-B2）與 collector／gate 實作。調查專案之文件優先序、能力與 Gate 敘述以 **§8–§12** 為準。

---

## 8. 調查專案文件分工與治理（`precision_uplift_recall_1pct`）

### 8.1 文件角色（固定契約）

| 文件 | 唯一職責 | 是否可放命令 |
| :--- | :--- | :--- |
| **本檔** `PLAN_precision_uplift_sprint.md` | **SSOT**：衝刺目標、§7 切片契約、§8–§12 調查治理／能力／Gate | 否 |
| `investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` | **Implementation Plan**：工程任務、DoD、里程碑、狀態 | 否 |
| `investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md` | **Execution Plan**：推進節奏、階段輸入輸出、決策節點 | 可（高層） |
| `investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md` | **Runbook**：CLI、旗標、故障排查 | 可（操作層） |

**衝突優先序**：**本檔（PLAN）** > Implementation Plan > Execution Plan > Runbook。  
**禁止**：Runbook 把 Implementation Plan 標為未完成的能力寫成「已可用」。

### 8.2 變更流程（文件維護）

每次調整流程、能力邊界或 Gate 契約時，建議順序：

1. 先更新 **本檔**：§7（若動切片）、**§10**（現況能力）、**§11**（Gate）、必要時 §9。  
2. 再改 `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` 任務勾選與敘述。  
3. 最後更新 `PRECISION_UPLIFT_R1PCT_EXECUTION_PLAN.md` 與 `PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md`。

**禁止**：只改 Runbook 而不改本檔；把 roadmap 願景寫成「現況已可用」。

---

## 9. 調查專案非目標與名詞／Run 契約

### 9.1 非目標（調查 repo 範圍內）

- 不在此輪導入分散式排程系統。  
- 不在此輪把最終商業決策自動化（Go/No-Go 仍需人工簽核）。

### 9.2 名詞與契約

- **Run 契約**：`run_id`、`model_version/model_dir`、時間窗、時區、標籤與 censored 規則、資料來源路徑。  
- **Gate 狀態**：`PASS` / `BLOCKED` / `FAIL` / `PRELIMINARY`（依 phase 定義）。  
- **結論強度**：`exploratory` / `comparative` / `decision_grade`。  
- **證據鏈**：`run_state.json` + phase reports + metrics artifacts + stdout/stderr logs。  
- **Phase 1 錯誤切片（`slice_contract`）**：見 **本檔 §7**（單一真相）。

---

## 10. 現況能力快照（調查 orchestrator，2026-04）

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

## 11. Gate 契約（跨 Phase，調查 repo 與衝刺對齊）

### 11.1 Phase 1

- 最低要件：樣本量、觀測時長、R1/R6 一致性、主因排序。  
- PIT parity 若為 `STRICT`，違規須阻斷 PASS；`WARN_ONLY` 可警示不阻斷。  
- **標籤品質稽核（W1-B3）**：在門檻與判因規則**經資料審閱凍結前**，不以 `label_bottleneck_assessment` **單獨**將整體 Phase 1 gate 標為 `FAIL`／`BLOCKED`；但每次 run 仍須輸出 **完整可稽核證據**（`label_noise_audit` 之 md+json：censored 統計、lag 分桶、`gt_stable_ts` 缺失率、高分 FP 逐列清單等，細節見 `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` W1-B3）。凍結後若啟用全自動判斷，須以 config **明示開啟**並帶 `label_audit_rules_version`。

### 11.2 Phase 2

- 需比較 uplift 與波動（至少可比 baseline/challenger）。  
- 若證據不足（例如僅 plan-only），應為 `BLOCKED`，不可升級為 `PASS`。

### 11.3 Phase 3/4

- 目前僅定義目標，不宣稱已有可執行 full-run gate 引擎。

---

## 12. 效能與風險原則（筆電優先）

- 預設 `max_parallel_jobs=1`，逐步放寬，不一次開並行。  
- 先做 dry-run，再做長跑，避免中後段才發現路徑/權限問題。  
- 任一步驟若缺證據或輸出缺檔，應 fail-fast 並保留可恢復狀態。  
- 若觀測窗/試驗數過大，先縮窗做 smoke，通過後再擴大。

