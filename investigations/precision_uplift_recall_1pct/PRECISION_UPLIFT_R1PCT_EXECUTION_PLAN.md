# Precision Uplift R1PCT Execution Plan

> 角色：執行計畫（Execution Plan）。  
> 目的：描述「怎麼推進專案」，不是「怎麼改程式」或「每個 CLI 旗標怎麼下」。  
> 參考：能力邊界以 SSOT 為準；低層命令與排障請看 Orchestrator Runbook。

---

## 1. 這份文件回答的問題

- 我們下一步要跑哪個 phase？
- 每個 phase 的進入條件、退出條件是什麼？
- 哪些輸出是可決策、哪些只是探索結果？
- 在資源受限（筆電）下，如何穩定往前推？

---

## 2. 執行策略（單輪 run 的標準節奏）

### 2.1 先固定 run 契約

每次正式執行前，先凍結以下資訊：
- `run_id`
- `model_version/model_dir`
- window（`start_ts/end_ts`）與時區
- 標籤契約（含 censored 規則）
- 主要資料路徑（state/prediction_log）

只要上述任一項中途改動，該 run 視為失效，需重新起 run。

### 2.2 再做 readiness

- 所有正式執行前必做 dry-run/readiness 檢查。
- 若是 all-phase，現況只允許 `--phase all --dry-run`，不得宣稱可直接 long run。

### 2.3 最後做分階段推進

- 按 `Phase1 -> Phase2 -> Phase3 -> Phase4` 推進。
- Gate 若 `BLOCKED/FAIL`，先處理阻塞再進下一階段。
- 不允許「先進下一階段，回頭補證據」。

---

## 3. Phase-by-Phase 推進規格

### 3.1 Phase 1（根因診斷，已升級為 decision-grade）

**目的**
- 回答 precision 未達標的主因：資料契約、標籤成熟、切片結構、模型能力哪個是主瓶頸。

**最低交付**
- `phase1/status_history_crosscheck.md` + `phase1/status_history_crosscheck.json`
- `phase1/slice_performance_report.md`
- `phase1/label_noise_audit.md`
- `phase1/point_in_time_parity_check.md`
- `phase1/upper_bound_repro.md`
- `phase1/phase1_gate_decision.md`

**RCA 5 項的最低可決策條件**
- 歷史對照：必有 unresolved blocker 掃描結果（不可只留 run note）。
- 細緻切片：必有 top 拖累切片排序（含樣本數與 delta）。
- 標籤品質：必有 `label_bottleneck_assessment` 分級。
- PIT：必有 `pit_contract_checks[]` 與 strict/warn 對應結論。
- 上限重現：必有 `comparison_contract.comparable`。

**退出條件（可進 Phase 2）**
- 已完成主因排序且有證據鏈（`root_cause_ranking`）。
- `phase1_conclusion_strength` 至少為 `comparative`；要作決策建議需 `decision_grade`。
- parity 結論與模式一致（`STRICT` 不可帶 violation 進 PASS）。

### 3.2 Phase 2（A/B/C 路線比較）

**目的**
- 找出相對 baseline 最有希望的建模路線與勝者候選。

**最低交付**
- `phase2/track_a_results.md`
- `phase2/track_b_results.md`
- `phase2/track_c_results.md`
- `phase2/phase2_gate_decision.md`

**退出條件（可進 Phase 3）**
- 至少 1 條路線有可稽核 uplift 證據。
- 有跨窗穩定性資訊（不是只靠單窗）。
- 淘汰路線有理由，不是「效果不好」這種空話。

### 3.3 Phase 3（勝者深化）

**目的**
- 在 Phase 2 勝者基礎上做特徵深化與集成收斂。

**退出條件（可進 Phase 4）**
- 相對 Phase 2 勝者有增量提升。
- 不以犧牲穩定性或關鍵切片為代價。

### 3.4 Phase 4（定版與決策）

**目的**
- 凍結候選、做多窗回放、輸出 go/no-go 證據包。

**退出條件（可提交簽核）**
- 主指標達標且跨窗一致。
- 關鍵切片沒有重大退化。
- 影響估算可被營運接受。

---

## 4. Phase 1 執行清單（可直接開工）

### 4.1 Sprint 內建議順序（兩週）

1. `status_history` 結構化（先讓 blocker 可判定）
2. `slice` 低成本維度排序（先拿到 top 拖累）
3. `label_noise` 分級判定（連動 timeline 重排）
4. `pit` critical checks（含 timezone/leakage）
5. `upper_bound` 可比性契約
6. `phase1_gate_decision` 匯總主因排序與結論強度

### 4.2 每步驟的完成定義（Definition of Ready to Merge）

- 有對應結構化欄位輸出（JSON 或 report 固定段落）
- 有至少 1 個正向測試 + 1 個反向測試
- 不增加長跑記憶體風險（預設並行仍為 1）
- failure path 有明確 `blocking_reasons`（不可 silent degrade）

### 4.3 Phase 1 重跑驗收（Definition of Done）

- 同一契約下重跑 2 次，結論等級與主因排序不翻轉。
- 六份報告都可回答：
  - 結論是什麼
  - 為什麼成立
  - 還缺哪些證據
- 若 `phase1_conclusion_strength != decision_grade`，必須在 gate 報告明確標示限制。

---

## 5. 結論強度分級（避免過度解讀）

| 等級 | 說明 | 可否做產品決策 |
| :--- | :--- | :--- |
| `exploratory` | 方向探索、證據不完整 | 否 |
| `comparative` | 可比較候選優劣 | 視風險，通常否 |
| `decision_grade` | 證據完整、可審核 | 是（仍需人工簽核） |

規則：
- 若證據不足或僅 plan-only，最高只能到 `exploratory`。
- 不得把 `BLOCKED` 包裝成「暫時 PASS」。

---

## 6. 執行節拍（建議）

- 每日：更新一次核心輸出與阻塞清單。
- 每 2~3 日：做一次 gate review（是否繼續、重排或暫停）。
- 每週：固定決策會，更新里程碑與風險。

---

## 7. 資源控管（筆電限制）

- 預設並行數 1，不先追求吞吐量。
- 重任務先小窗/小樣本 smoke，再擴大。
- slice 與 PIT 新檢查先從聚合/抽樣版本上線，再評估是否全量。
- 若出現記憶體壓力或長時間無進展，立即縮窗或拆批。
- 一旦發現 OOM 風險，不可忽略，必須先調整再繼續跑。

---

## 8. 異常處理原則

- `CONFIG/PREFLIGHT` 錯誤：先修環境與契約，不進入長跑。
- `EVIDENCE MISSING`：結論降級，必要時重跑，不補口頭推論。
- `GATE BLOCKED`：先解 `blocking_reasons`，不跳關。
- `RUN INTERRUPTED`：優先 `--resume`；若契約已漂移，開新 run。

---

## 9. 跨文件導覽

- SSOT：`PRECISION_UPLIFT_R1PCT_SSOT.md`
- Implementation Plan：`PRECISION_UPLIFT_R1PCT_IMPLEMENTATION_PLAN.md`
- Orchestrator Runbook（命令/旗標/排障）：`PRECISION_UPLIFT_R1PCT_ORCHESTRATOR_RUNBOOK.md`
