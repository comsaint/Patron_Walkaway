# Precision Uplift R1PCT Execution Plan

> 角色：執行計畫（Working/Execution Plan）。  
> 目的：定義「本輪實際要做的事、先後順序、每步完成定義、阻塞時怎麼處理」。  
> 邊界：本檔不重寫需求與架構；需求以 **`../../.cursor/plans/PLAN_precision_uplift_sprint.md`**（單一 SSOT，含 §7–§12）為準，工程細節以 Implementation Plan 為準。  
> 切片契約：Phase 1 `slice_contract` 單一真相見 `../../.cursor/plans/PLAN_precision_uplift_sprint.md` §7。

### 任務狀態標記（本檔）

| 標記 | 意義 |
| :--- | :--- |
| **✅** | 本檔該列 **DoD** 已滿足（或等價交付）；細項勾選仍以 `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` 為準，若有落差請回寫本表。 |
| **🟡** | **部分完成**：已有可跑產物／MVP，但尚未滿足本列 DoD、或缺結構化／Gate 整合等明確缺口（欄位「備註」簡述）。 |
| **⏳** | **進行中**：已開工、尚未結案。 |
| **⬜** | **未開始**。 |

**狀態維護**：與 Implementation Plan 衝突時，先釐清事實再同步兩檔；本檔表格式為執行排程「一眼狀態」。

---

## 1. 目前基線（Execution Baseline）

### 1.1 已可用能力（可直接執行）

- `run_pipeline.py --phase phase1`：batch 主流程可跑（R1/R6、backtest、collect、gate、report）。
- `run_pipeline.py --phase phase2`：MVP 可跑（plan/runner/gate/report 與可選 trainer/backtest job）。
- `--phase all --dry-run`：可用於 readiness 檢查與契約核對。
- `run_state.json` / `--resume`：可用於中斷恢復（契約一致前提）。

### 1.2 尚未完成能力（執行時視為限制）

- `--phase all` 非 dry-run 串接（W5）尚未完成。
- `--phase phase3` / `--phase phase4` full run（W3/W4）尚未完成。
- `--mode autonomous` 長跑 supervisor（W6）尚未完成（僅 stub/once 能力）。
- Phase 1 `slice_contract`：**MVP 已落地**（`slice_contract.py` + 可選內嵌 spec + 報告 JSON 區塊）；**真實 eval／profile join 與 gate 接線**仍待完成，尚不可宣稱完整決策級切片證據。

### 1.3 Phase 1 週任務狀態摘要（對齊 §4）

| 任務 | 狀態 | 備註 |
| :--- | :--- | :--- |
| W1-B1 | **✅** | `phase1/status_history_registry.yaml`、collect bundle `status_history_crosscheck`、`reports/phase1/status_history_crosscheck.json`、Gate `status_history_unresolved_blocker:*`（DEC-041）；`.md` 仍以 orchestrator 區塊 + 人工段為主。 |
| W1-B2 | **🟡** | 已落地 **`slice_contract.py`**、內嵌 spec collector、`slice_performance` **md+json**、gate incomplete、**`recall_score_threshold` 注入**（R1 `threshold_at_target`，否則 backtest `threshold_at_recall_0.01`；spec 未寫入時），以及 **`auto_eval_rows_from_prediction_log`**（SQLite：rated+finalized label）與 **`auto_profiles_from_state_db`**（按 canonical_id 回查 `player_profile`；若有 `as_of_ts` 則取 `<= T0` 最近一筆）。**資料源策略**：`state_db` primary；不可行時 Parquet fallback；仍失敗且啟用旗標時 ClickHouse fallback。**治理**：`asof_mode=STRICT` 時 `as_of` 證據不足一律 `slice_data_incomplete`，並由 gate 強制 **FAIL**。**契約版控**：`slice_contract_version` + `slice_contract_plan_hash_sha256` 已由 collector 自動注入（PLAN §7 指紋）。**仍待**：§7 完整證收。 |
| W1-B3 | **🟡** | 已有 `label_noise_audit.md` 自 R1 payload 之自動區塊。**契約**：censored／lag／rated 之**全量表列輸出**（md+json）優先；`none\|minor\|major\|blocking` 門檻與「無修復計畫→自動 BLOCK」**待資料審閱後凍結**，在此之前 gate **半自動**（見 §5.3）。 |
| W1-B4 | **🟡** | 已有 `pit_parity` MVP 指標與 `point_in_time_parity_check.md`；**尚未** `pit_contract_checks[]`、leakage sentinel、STRICT critical 全閉環。 |
| W1-B5 | **🟡** | 已有 `upper_bound_repro.md` 自 backtest／R1；**尚未** `comparison_contract`／comparable 自動降級。 |
| W1-B6 | **⬜** | `phase1_gate_decision.md` 有基礎 Gate 輸出；**尚未** `phase1_conclusion_strength`、`root_cause_ranking` 固定欄位。 |
| W5（`--phase all` 非 dry-run） | **⬜** | 見 §1.2。 |
| W6（autonomous supervisor） | **🟡** | stub／once 可跑；長跑 supervisor **未**完成。 |

---

## 2. 執行目標（全程 + 當前重點）

本檔覆蓋 **Phase 1~4 全程執行**，但按現況能力與風險，執行優先順序為：

1. **先完成 Phase 1 decision-grade 證據鏈**（目前最高優先）。
2. **再執行 Phase 2 路線比較與 winner 收斂**（利用既有 MVP 能力）。
3. **最後推進 Phase 3/4 full run 能力與定版決策包**（目前能力缺口最大）。

當前（兩週）仍以 Phase 1 收斂為主，避免在證據未閉環時提前擴張 Phase 2~4。

---

## 3. Run 契約凍結（每次正式 run 前必做）

### 3.1 必凍結欄位

- `run_id`
- `model_version/model_dir`
- window（`start_ts/end_ts`）與時區
- 標籤契約（含 censored 規則）
- 主要資料路徑（state/prediction_log）
- 切片契約相關：
  - `T0`（eval/holdout 起點時刻；對齊 PLAN §7.2）
  - `player_profile`（或等價來源）路徑/取得方式
  - rated-only 族群約束（PLAN §7.1）

### 3.2 契約漂移處理

- 上述任一項在 run 中途改動：該 run 視為失效，重新起 run。
- `--resume` 僅允許在同契約（fingerprint 一致）下使用。

---

## 4. 兩週執行排程（實際工作）

### 4.1 Week 1（P0：先把可判定證據做出來）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **✅** | W1-B1 歷史對照結構化 | 建 `status_history_registry`；產出 `status_history_crosscheck.json`；Gate 接 unresolved blocker | Orchestrator/ML Platform | W1-A 完成 | `phase1/status_history_crosscheck.md/.json` | unresolved blocker 可被機器判定並反映到 gate |
| **🟡** | W1-B2 切片契約落地（第一優先） | **MVP**：`slice_contract` 內嵌 spec + `top_drag_slices`；**仍待** T0/profile Parquet、gate 接 `blocking_reasons` | Orchestrator/DS | W1-B1 可並行 | `phase1/slice_performance_report.md`（可選 `.json`） | 報告含 slice_contract JSON；真實資料 join 後驗證 Top10／條款 |
| **🟡** | W1-B3 標籤品質判定 | censored／lag 分桶／FP 清單全量落地；分級門檻**待定** | DS | W1-B1 | `phase1/label_noise_audit.md` **+** `label_noise_audit.json` | md 與 json **同源**；人讀可還原全數；`label_audit_pending_human_decision` 至規則凍結；**不強求**已校準之 `none\|minor\|major\|blocking`（見 §5.3） |

### 4.2 Week 2（P0：把 Gate 與結論閉環）

| 狀態 | 任務 | 子任務 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **🟡** | W1-B4 PIT critical checks | `pit_contract_checks[]`；STRICT/WARN_ONLY 對應結論 | Orchestrator/ML Platform | Week 1 任務 | `phase1/point_in_time_parity_check.md` | STRICT 違規必 fail；WARN_ONLY 僅警示 |
| **🟡** | W1-B5 上限可比性契約 | `comparison_contract`；`comparable_metrics` vs `reference_only_metrics` | DS | Week 1 任務 | `phase1/upper_bound_repro.md` | `comparable=false` 時結論自動降級 |
| **⬜** | W1-B6 Gate 結論整合 | `phase1_conclusion_strength`、`root_cause_ranking`、行動項 | DS + Orchestrator | W1-B2/B3/B4/B5 | `phase1/phase1_gate_decision.md` | 報告可回答「結論/理由/缺口/下一步」 |

---

## 5. 逐任務執行規範（Definition of Ready / Done）

### 5.1 Ready to Merge（每項任務都要達成）

- 有機器可讀輸出（JSON 或固定欄位段落）。
- 至少 1 個正向 + 1 個反向測試。
- failure path 必寫 `blocking_reasons`（不得 silent degrade）。
- 不額外放大筆電風險（預設並行仍為 1）。

### 5.2 W1-B2（切片）額外硬條件

- 必須對齊 PLAN §7 維度與命名；不得回退舊稱 `player_tier` / `value_tier`。
- `slice_data_incomplete` 觸發條件至少覆蓋：
  - T0 無 profile
  - `active_days_30d < 1`
  - `theo_win_sum_30d` 為 NULL
  - `turnover_sum_30d` 為 NULL
- 一旦觸發，`phase1_gate_decision.md` 必須明示限制與 `blocking_reasons`。

### 5.3 W1-B3 標籤品質稽核（半自動、待決策）

- **待決（pending）**：`label_bottleneck_assessment` 由指標映射至 `none|minor|major|blocking` 之**門檻表**、高分 FP **判因枚舉**是否收斂、以及「無已核准修復計畫時是否自動升級 BLOCK／timeline」— **須看過實際資料後**由 DS／負責人凍結；凍結後寫入 Implementation Plan／config 之 `label_audit_rules_version`，方可啟用 **config 明示**之全自動 gate（`label_audit_auto_gate_enabled`）。
- **不得少做（強制）**：每一 run 必須留下 **人類可讀 + 機器可讀** 之完整證據——含 censored 統計、lag 分桶（及 `gt_stable_ts` 缺失率）、高分 FP **逐列清單**（欄位 schema 見 `PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md` W1-B3）。缺檔或缺欄視為證據鏈斷裂。
- **Gate 預設**：在規則凍結前，**不得**僅憑標籤子 assessment 將 Phase 1 打成 `FAIL`／`BLOCKED`；可 `WARN` 或標示 **`label_quality_human_review_recommended`** 並附 narrative timeline 建議。

### 5.4 Phase 1 DoD（本輪最終）

- 六份 Phase 1 報告齊全且可對照同一 run 契約。
- `phase1_gate_decision.md` 含：
  - `phase1_conclusion_strength`
  - `root_cause_ranking`
  - `blocking_reasons`
- 同契約重跑 2 次，結論等級與主因排序不翻轉。
- W1-B3：見 §5.3；**不以**未凍結之 bottleneck 規則阻斷整體 gate。

---

## 6. Gate 與升級規則（執行層）

### 6.1 Phase 1 -> Phase 2 進入條件

- 主因排序完成且有證據鏈。
- `phase1_conclusion_strength >= comparative`（要給決策建議需 `decision_grade`）。
- PIT 模式與結論一致（STRICT 不可帶 violation 進 PASS）。
- 若 `slice_data_incomplete=true`：
  - 切片子結論不得宣稱 decision-grade。
  - 整體是否阻斷進 Phase 2，依 gate 規則表定稿；但報告必須明示限制。

### 6.2 證據不足處理

- `EVIDENCE MISSING`：結論降級；必要時重跑，不補口頭推論。
- `GATE BLOCKED`：先解 `blocking_reasons`，不跳關。

---

## 7. 每日執行節奏（Cadence）

- 每日一次：更新阻塞清單、已完成工件、下一步。
- 每 2~3 日：Gate review（繼續/重排/暫停）。
- 每週一次：決策會，確認里程碑與 scope 調整。

---

## 8. 風險與止損（Execution Risk Control）

- **OOM 風險**：預設並行=1；先小窗 smoke，再擴窗。
- **切片計算膨脹**：先做六類 marginal；joint 僅在 `min_n` 與資源評估通過後開啟。
- **契約漂移**：run 中途改 window/label/path 一律重起。
- **假陽性結論**：單窗、plan-only 或 incomplete 證據不得升級為 decision-grade。

---

## 9. 跨文件連動（避免再失焦）

- **單一 SSOT**（衝刺、§7 `slice_contract`、§8–§12 調查治理／能力／Gate）：`../../.cursor/plans/PLAN_precision_uplift_sprint.md`（§1–§12）
- Implementation Plan（工程任務與狀態）：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`
- STATUS（歷史脈絡與完成證據）：`../../.cursor/plans/STATUS.md`
- Runbook（命令/排障）：`PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md`

---

## 10. 更新規則（本檔）

- 本檔只寫「本輪實際執行與順序」，不重寫需求或架構。
- 任務狀態更新以 Implementation Plan 為主；本檔同步節奏與阻塞策略。
- 若發現本檔與 PLAN（§7 切片與 §8–§12 治理／能力／Gate）或 Implementation Plan 衝突，先修上游契約再更新本檔。

---

## 11. Phase 2-4 執行清單（實際工作）

本節補充全程 execution：即使當前重點是 Phase 1，仍明確定義 Phase 2~4 的「進場條件 -> 實際任務 -> 退出條件」。

### 11.1 Phase 2（A/B/C 路線比較）— 可執行（MVP）

**進場條件**
- `phase1_gate_decision.md` 可用，且 `phase1_conclusion_strength >= comparative`。
- 若 `slice_data_incomplete` 尚未解除，需在 Phase 2 報告明示限制（不做無限制外推）。

| 狀態 | 任務 | 實際做的事情 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **✅（MVP）** | P2-1 計畫束產出 | 生成/核對 `job_specs`、baseline/challenger 對照、window 設定 | Orchestrator | Phase 1 完成 | `phase2_bundle.json`（含 plan） | 可追溯每個實驗配置與來源 |
| **✅（MVP）** | P2-2 路線執行 | 跑 Track A/B/C（trainer jobs + 可選 backtest jobs） | DS + Orchestrator | P2-1 | 每 job 指標、log、中繼工件 | 無靜默失敗；失敗可定位到 job |
| **✅（MVP）** | P2-3 gate 判定 | 產出 uplift / 波動 / blocker；形成 route keep/drop | Orchestrator + DS | P2-2 | `phase2/phase2_gate_decision.md` | 明確 `PASS/BLOCKED/FAIL` + reasons |
| **✅（MVP）** | P2-4 結果摘要 | 彙整 track 報告，標記 winner 與淘汰理由 | DS | P2-3 | `phase2/track_a_results.md` `track_b_results.md` `track_c_results.md` | 每條路線「為何保留/淘汰」可審核 |

**P2 狀態註**：上表四列 = `run_pipeline.py --phase phase2` 主鏈（計畫束／runner／gate／track 報告）之 **MVP 已可跑**；契約加深與 fail-fast 缺口見 Implementation Plan **W2-B**（與本表並列維護，不以此四列代替 W2-B 勾選）。

**退出條件（進入 Phase 3）**
- 至少 1 條路線有可稽核 uplift 證據。
- 有跨窗穩定性證據（非單窗敘事）。
- winner 與淘汰理由具結構化證據，不是口頭判斷。

### 11.2 Phase 3（勝者深化）— 尚未可全自動，採「任務先行」

**進場條件**
- Phase 2 有明確 winner（含限制條件）。
- 已列出要保護的關鍵切片（沿用 PLAN §7 口徑或其子集）。

| 狀態 | 任務 | 實際做的事情 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **⬜** | P3-1 勝者凍結 | 鎖定單一路線 winner 配置與資料窗 | DS | Phase 2 | winner freeze note | 後續實驗以同基線可比較 |
| **⬜** | P3-2 特徵深化 | 動態行為/切片定向 feature pack 實驗 | DS | P3-1 | `phase3/feature_uplift_table.md` `phase3/slice_targeted_features.md` | 有增量且非單切片幻覺 |
| **⬜** | P3-3 集成與校準 | 群內/群間融合、top band calibration、policy 檢查 | DS + ML Platform | P3-2 | `phase3/ensemble_ablation.md` `phase3/top_band_calibration_report.md` | 複雜度增加需有明確收益 |
| **⬜** | P3-4 gate 草案 | 先以人工 gate 模板做 pass/fail 草判，回填能力缺口 | DS + Orchestrator | P3-3 | `phase3/phase3_gate_decision.md`（草案可） | 可回答「能否進 Phase 4，缺口是什麼」 |

**退出條件（進入 Phase 4）**
- 相對 Phase 2 winner 有增量提升。
- 不以犧牲穩定性或關鍵切片為代價。
- 有可複跑的 Phase 3 證據包（即使 full-run engine 未齊）。

### 11.3 Phase 4（定版與決策）— 尚未可全自動，採「決策包先行」

**進場條件**
- Phase 3 有穩定候選。
- 業務方可接受回放窗口與影響估算口徑。

| 狀態 | 任務 | 實際做的事情 | Owner | 依賴 | 輸出 | DoD |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **⬜** | P4-1 候選定版 | 鎖定資料窗、特徵、模型、閾值與口徑版本 | DS + ML Platform | Phase 3 | `phase4/candidate_freeze.md` | freeze 後可重跑且結果一致 |
| **⬜** | P4-2 多窗回放 | 依固定契約跑多窗主指標與切片指標 | DS | P4-1 | `phase4/multi_window_backtest.md` | 不是單窗漂亮；跨窗可解釋 |
| **⬜** | P4-3 影響估算 | 告警量/誤報量/KPI 估算與營運承接檢查 | DS + Product/Ops | P4-2 | `phase4/impact_estimation.md` | 可被營運側理解與接受 |
| **⬜** | P4-4 Go/No-Go 包 | 彙整證據、風險、回滾策略、決策建議 | DS + Product + ML Platform | P4-3 | `phase4/go_no_go_pack.md` | 決策會可直接簽核或退回 |

**退出條件（可提交簽核）**
- 主指標達標且跨窗一致。
- 關鍵切片無重大退化。
- 影響估算與風險/回滾方案完整。

### 11.4 Phase 2-4 共通阻塞處理

- `BLOCKED`：列出 blocker owner + deadline，未解除不得跳關。
- `EVIDENCE MISSING`：不補口頭推論；重跑或降級結論。
- `OOM/長跑失控`：先縮窗與降並行，再逐步放大。
- 契約漂移：開新 run，不沿用舊 run_id 續跑。
