# 執行計畫：Precision 提升（Recall=1%）

> 單一真相來源（Source of Truth）：`.cursor/plans/PLAN_precision_uplift_sprint.md`  
> 工作主目錄：`investigations/precision_uplift_recall_1pct/`

---

## 0. 進度儀表板（先看這裡）

| 欄位 | 當前值 |
| :--- | :--- |
| 當前階段 | `Phase 1` |
| 整體狀態 | `🟡 進行中` |
| 最新更新日 | `YYYY-MM-DD` |
| 目前主指標 `precision@recall=1%` | `TBD` |
| 目標門檻 | `>= 60%` |
| 是否觸發重排 | `否 / 是（原因）` |
| Blocker | `無 / 有（簡述）` |
| 下一個里程碑 | `Phase 1 Gate` |

> 狀態圖例：`⚪ 未開始`、`🟡 進行中`、`🟢 已完成`、`🔴 阻塞`、`⏸ 暫停`

---

## 1. 目標與完成定義

- 在相同評估契約下，將 `precision@recall=1%` 由約 40% 提升至 `>=60%`。
- 提升需可跨時間窗穩定成立（forward/purged 驗證）。
- 任何階段結論都必須有對應工件，不接受口頭結論。

---

## 2. 階段總覽（完成 / 未完成一眼可見）

| Phase | 名稱 | 狀態 | Gate 狀態  |
| :--- | :--- | :--- | :---  |
| Phase 1 | 根因診斷（RCA）與限制條件確認 | ⚪ 未開始 | ⚪ 未達成 |  
| Phase 2 | 高槓桿建模路線（A/B/C） | ⚪ 未開始 | ⚪ 未達成 | 
| Phase 3 | 特徵深化與集成收斂 | ⚪ 未開始 | ⚪ 未達成  |
| Phase 4 | 定版、回放與 Go/No-Go | ⚪ 未開始 | ⚪ 未達成 |

---

## 3. 各階段執行清單（可打勾）

### Phase 1 - 根因診斷（RCA）與限制條件確認

**任務清單**
- [ ] 建立 `phase1/status_history_crosscheck.md`（含沿用/重驗/失效分類）
- [ ] 產出 `phase1/slice_performance_report.md`
- [ ] 產出 `phase1/label_noise_audit.md`
- [ ] 產出 `phase1/point_in_time_parity_check.md`
- [ ] 產出 `phase1/upper_bound_repro.md`
- [ ] 產出 `phase1/phase1_gate_decision.md`

**Gate 條件**
- [ ] 已完成「模型能力 vs 標籤/資料契約」主因排序
- [ ] 已完成 `STATUS.md` 歷史對照
- [ ] 已決定是否啟動重排（先資料修復、後模型擴張）

**交付物路徑**
- `phase1/status_history_crosscheck.md`
- `phase1/slice_performance_report.md`
- `phase1/label_noise_audit.md`
- `phase1/point_in_time_parity_check.md`
- `phase1/upper_bound_repro.md`
- `phase1/phase1_gate_decision.md`

### Phase 2 - 高槓桿建模路線（A/B/C）

**任務清單**
- [ ] Track A：排序導向目標 + hard negative mining（`phase2/track_a_results.md`）
- [ ] Track B：分群模型 + gating（`phase2/track_b_results.md`）
- [ ] Track C：時序穩定性過濾（`phase2/track_c_results.md`）
- [ ] 匯總 Gate 決策（`phase2/phase2_gate_decision.md`）

**Gate 條件**
- [ ] 至少 1 條路線相對基線達成顯著 uplift（+3 到 +5pp）

### Phase 3 - 特徵深化與集成收斂

**任務清單**
- [ ] 動態行為特徵（`phase3/feature_uplift_table.md`）
- [ ] 拖累切片專用特徵包（`phase3/slice_targeted_features.md`）
- [ ] 集成/群融合消融（`phase3/ensemble_ablation.md`）
- [ ] 高分段校準報告（`phase3/top_band_calibration_report.md`）
- [ ] Gate 決策（`phase3/phase3_gate_decision.md`）

**Gate 條件**
- [ ] 在 Phase 2 勝者基礎上再提升且跨窗穩定

### Phase 4 - 定版、回放與 Go/No-Go

**任務清單**
- [ ] 候選設定凍結（`phase4/candidate_freeze.md`）
- [ ] 多時間窗回放（`phase4/multi_window_backtest.md`）
- [ ] 上線影響估算（`phase4/impact_estimation.md`）
- [ ] Go/No-Go 決策包（`phase4/go_no_go_pack.md`）

**Gate 條件**
- [ ] 主指標達標（`>=60%`）且跨窗穩定

---

## 4. 時程重排規則（Phase 1 觸發）

- 若 `label_noise_audit` 顯示主要瓶頸在標籤流程：延後模型擴張，優先修復資料/標籤契約。
- 若 `status_history_crosscheck` 顯示歷史阻礙仍未解除：升級為必做項目，解除前不可進下一階段。
- 若重排後一週標籤品質 KPI 仍不收斂：執行 scope cut（暫停大型 ensemble 與大規模特徵擴張）。

---

## 5. 里程碑與決策日誌

| 日期 | 事件 | 結論 | 影響階段 | 下一步 |
| :--- | :--- | :--- | :--- | :--- |
| YYYY-MM-DD | 專案啟動 | TBD | Phase 1 | 建立 Phase 1 工件骨架 |

---

## 6. 實驗登錄契約（每次實驗必填）

請在實驗結果檔至少包含以下欄位：
- `experiment_id`
- `data_window`
- `split_protocol`
- `label_contract`
- `feature_set_version`
- `model_config`
- `objective_variant`
- `precision_at_recall_1pct`
- `pr_auc`
- `top_k_precision`
- `slice_metrics`
- `cv_mean_std`
- `decision`
- `decision_reason`

---

## 7. 更新規則（維持可讀性）

- 每次更新先改「進度儀表板」與「階段總覽」。
- 任務完成後同時：勾選 checklist + 補對應工件路徑。
- Gate 達成時必填「里程碑與決策日誌」。
- 若有阻塞，將狀態改為 `🔴 阻塞` 並在 Blocker 欄說明解除條件。

---

## 8. 一次性 Ad-hoc 執行方案（不使用 cron）

本節已移至獨立文件：  
`investigations/precision_uplift_recall_1pct/PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md`

該文件包含：

- 一次性 ad-hoc 執行方案（原 §8 完整內容）
- 明確時長建議與停止/延長條件
- 腳本化 implementation plan（模組拆分、CLI、Gate 引擎、里程碑）

---

## 9. Ad-hoc 全階段延伸（Phase 2~4）

> 目標：在完成 Phase 1 後，不改成 cron，直接以「手動分批執行」方式推進到 Phase 4。  
> 原則：每個 Phase 都以「固定 run_id + 固定資料契約 + 固定評估口徑」執行，避免跨階段結果不可比。

### 9.1 Phase 2（高槓桿模型路線 A/B/C）

**執行方式（ad-hoc）**

1. 鎖定 Phase 1 結論後的資料契約（特別是 censored / delayed label 規則）。
2. 以相同 window/split 建立 A/B/C 三條路線實驗（可分批手動跑，不需同時）。
3. 每條路線輸出到對應工件：
   - `phase2/track_a_results.md`
   - `phase2/track_b_results.md`
   - `phase2/track_c_results.md`
4. 以同一指標表比較 uplift 與穩定性，填 `phase2/phase2_gate_decision.md`。

**每條路線最低證據**

- 至少 2 個以上時間窗結果（避免單窗幻覺）
- 必填：`precision@recall=1%`、`pr_auc`、`top_k_precision`、`slice_metrics`、`cv_mean_std`
- 與 Phase 1 baseline 同口徑比較（不可換契約）

**Gate 建議（可進 Phase 3）**

- 至少 1 條路線達到 +3~5pp uplift（相對基線）
- 跨窗波動可解釋（非單窗偶然）
- 失敗路線必有淘汰理由（避免重複試錯）

**建議時長**

- 最短：2~3 天（僅初判）
- 建議：4~7 天（含至少一次跨週期窗）

### 9.2 Phase 3（特徵深化與集成收斂）

**執行方式（ad-hoc）**

1. 只在 Phase 2 勝者路線上加特徵，不做全域盲目擴張。
2. 先做切片定向特徵，再做集成/融合消融。
3. 每個變更都輸出對應工件：
   - `phase3/feature_uplift_table.md`
   - `phase3/slice_targeted_features.md`
   - `phase3/ensemble_ablation.md`
   - `phase3/top_band_calibration_report.md`
4. 匯總寫入 `phase3/phase3_gate_decision.md`。

**重點控制**

- 若 ensemble 提升小但複雜度高，優先捨棄（維運優先）
- 高分段（top band）校準必做，避免表面 uplift 實際誤報升高

**Gate 建議（可進 Phase 4）**

- 相對 Phase 2 勝者再提升（非持平）
- 未犧牲跨窗穩定性
- 拖累切片至少有 1~2 個被實質改善

**建議時長**

- 最短：3 天
- 建議：5~8 天

### 9.3 Phase 4（定版、回放、Go/No-Go）

**執行方式（ad-hoc）**

1. 凍結候選配置（資料窗、特徵、模型、閾值），填 `phase4/candidate_freeze.md`。
2. 手動跑多窗回放，填 `phase4/multi_window_backtest.md`。
3. 估算上線影響（告警量/誤報量/業務影響），填 `phase4/impact_estimation.md`。
4. 生成 `phase4/go_no_go_pack.md`，做最終決策。

**Go/No-Go 建議門檻**

- 主指標達標（`precision@recall=1% >= 60%`）
- 多窗一致，不依賴單窗
- 切片無重大退化（尤其高價值玩家/高分段）

**建議時長**

- 最短：2~3 天（僅技術驗證）
- 建議：4~7 天（含風險回放與影響估算）

### 9.4 全階段手動節奏（不使用 cron 的推薦節拍）

可用以下 ad-hoc 節拍推進（每次執行後人工審核）：

- 每日 1 次：更新 backtest + R1/R6 + 工件草稿
- 每 2~3 日 1 次：Phase Gate 預審（是否繼續/淘汰/重排）
- 每週 1 次：正式決策紀錄（里程碑表）

> 評語：Phase 2~4 若完全不做固定節拍，容易變成「憑印象調參」。即使不做 cron，也至少維持上述手動節拍。

### 9.5 粗估總時程（Phase 1~4，ad-hoc 模式）

- Phase 1：3~5 天（建議）
- Phase 2：4~7 天（建議）
- Phase 3：5~8 天（建議）
- Phase 4：4~7 天（建議）

**總計建議：16~27 天。**  
若樣本稀疏或 delayed label 嚴重，請預留 +30~50% 緩衝。

---

## 10. 單一整合 Runbook（Phase 1~4，一次看完）

> 本節是 **Phase 1~4 的唯一執行版**。  
> 章節 9 與 `PRECISION_UPLIFT_R1PCT_ADHOC_RUNBOOK.md` 可作補充說明，但實際執行請以本節為準。

### 10.1 全階段結論門檻（何時可下結論）

| Phase | 最短時長（僅初判） | 建議時長（可決策） | 可下結論條件（至少滿足） |
| :--- | :--- | :--- | :--- |
| Phase 1 | 48h | 72~120h（3~5 天） | finalized alerts >= 800（理想 >=1000）、TP >= 30、R1/R6 兩次方向一致、可完成主因排序 |
| Phase 2 | 2~3 天 | 4~7 天 | 至少 1 條 A/B/C 路線 uplift +3~5pp，且至少 2 個時間窗非單窗幻覺 |
| Phase 3 | 3 天 | 5~8 天 | 相對 Phase 2 勝者再提升，且切片改善與穩定性同時成立 |
| Phase 4 | 2~3 天 | 4~7 天 | 多窗回放仍達標（`precision@recall=1% >= 60%`），且無重大切片退化 |

> 評語：在 recall=1% 稀疏場景，若低於上表建議時長就做最終結論，誤判風險很高。

### 10.2 共通前置（所有 Phase 都先做）

| 步驟 | 要跑什麼 | 預期產物 | 失敗要看哪個訊號 |
| :--- | :--- | :--- | :--- |
| P0-1 | 固定 run 契約：`run_id`、`MODEL_DIR`、`STATE_DB_PATH`、`PREDICTION_LOG_DB_PATH`、window、label contract | `run_id` 與參數記錄（可寫入里程碑） | run 期間參數被改動、模型版本漂移 |
| P0-2 | 健康檢查：路徑存在、DB 可讀、ClickHouse 可查 | 可成功讀取 prediction/state/model artifact | 連線逾時、路徑不存在、權限錯誤 |
| P0-3 | 短窗 backtest smoke test | `trainer/out_backtest/backtest_metrics.json` | 無輸出、`No bets for the requested window`、JSON 欄位缺失 |

### 10.3 Phase 1 Runbook（RCA 與契約確認）

| 步驟 | 要跑什麼 | 預期產物（路徑） | 失敗要看哪個訊號 |
| :--- | :--- | :--- | :--- |
| P1-1 | 啟動 scorer 長跑：`python -m trainer.scorer` | `prediction_log`/`alerts` 持續增長（state DB） | alerts 長期為 0、log 重複 exception、DB 無新增列 |
| P1-2 | 啟動 validator 長跑：`python -m trainer.validator` | `validation_results`、`validator_metrics` 持續增長 | `validator_metrics insert failed`、finalized 長期不增 |
| P1-3 | 中途健康檢查（建議 T+6h）：`python investigations/test_vs_production/checks/run_r1_r6_analysis.py --mode all --pretty ...` | `snapshots/*.csv` + JSON payload（含 `n_censored_excluded`、`precision_at_recall_target`） | `sample CSV contains no bet_id rows`、`prediction_log table not found` |
| P1-4 | 觀測期末再跑一次 R1/R6 + 固定窗 backtest | 最終 payload + `trainer/out_backtest/backtest_metrics.json` | 與中途結果方向劇烈反轉、樣本不足 |
| P1-5 | 回填 Phase 1 六份工件 | `phase1/*.md` 全部完成 | 任一工件缺主證據、口徑不一致 |

**Phase 1 可下結論（Gate）**

- 可進 Phase 2：主瓶頸排序完成，且非單窗幻覺。
- 需重排：若 `label_noise_audit` 指向 delayed/censored/契約問題為主瓶頸，先修資料流程再做模型衝刺。

### 10.4 Phase 2 Runbook（A/B/C 路線並行比較）

| 步驟 | 要跑什麼 | 預期產物（路徑） | 失敗要看哪個訊號 |
| :--- | :--- | :--- | :--- |
| P2-1 | 以同一 Phase 1 契約建立 Track A/B/C 設定，分批訓練與回測（沿用既有訓練入口，例如 `python -m trainer.trainer ...` + 對應 config） | 每條路線都有可追溯實驗紀錄與模型輸出（`models/...` 或 `out/models/...`） | 只剩口頭結論、無可重現 artifact |
| P2-2 | 各路線跑固定窗 backtest + 至少第 2 個時間窗重跑 | 每路線至少 2 個窗的 metrics（可附 JSON/表格） | 僅單窗漂亮，第二窗崩潰 |
| P2-3 | 寫入 `phase2/track_a_results.md`、`track_b_results.md`、`track_c_results.md` | 三份 track 工件完整 | 缺 `precision@recall=1%` 或缺切片/波動資訊 |
| P2-4 | Gate 決策寫入 `phase2/phase2_gate_decision.md` | 明確保留/淘汰路線 | 無淘汰理由、重複試錯 |

**Phase 2 可下結論（Gate）**

- 至少 1 條路線達到 +3~5pp uplift（相對基線）且跨窗可重現，才能進 Phase 3。

### 10.5 Phase 3 Runbook（特徵深化與集成收斂）

| 步驟 | 要跑什麼 | 預期產物（路徑） | 失敗要看哪個訊號 |
| :--- | :--- | :--- | :--- |
| P3-1 | 只在 Phase 2 勝者路線加特徵（先切片定向、後全域） | 特徵 uplift 對照表 | 特徵變多但主指標不升、訓練時間暴增 |
| P3-2 | 跑集成/融合消融與高分段校準 | `phase3/ensemble_ablation.md`、`top_band_calibration_report.md` | ensemble 僅微幅提升但複雜度大幅上升 |
| P3-3 | 匯整 Phase 3 工件與 Gate | `phase3/phase3_gate_decision.md` | 只看 overall，不看切片退化 |

**Phase 3 可下結論（Gate）**

- 相對 Phase 2 勝者再提升，且跨窗穩定、切片改善非偶然，才能進 Phase 4。

### 10.6 Phase 4 Runbook（定版、回放、Go/No-Go）

| 步驟 | 要跑什麼 | 預期產物（路徑） | 失敗要看哪個訊號 |
| :--- | :--- | :--- | :--- |
| P4-1 | 凍結候選（資料窗/特徵/模型/閾值） | `phase4/candidate_freeze.md` | 候選設定仍持續變動、無凍結版本 |
| P4-2 | 多時間窗回放（至少 3 窗，含不同流量特性） | `phase4/multi_window_backtest.md` | 僅單窗達標、其餘窗明顯退化 |
| P4-3 | 上線影響估算（告警量/誤報量/業務成本） | `phase4/impact_estimation.md` | 僅報主指標，無告警量與誤報成本 |
| P4-4 | Go/No-Go 決策包 | `phase4/go_no_go_pack.md` | 缺風險清單與 fallback 計畫 |

**Phase 4 可下結論（最終）**

- `precision@recall=1% >= 60%` 且多窗一致成立。
- 無重大切片退化，且營運可承受告警量與誤報成本。

### 10.7 手動執行節拍（不用 cron 也要固定節奏）

- 每日 1 次：更新 backtest + R1/R6 + 工件草稿
- 每 2~3 日：做一次 Phase Gate 預審
- 每週 1 次：正式更新里程碑與決策日誌

> 即使不用排程，也要用固定節拍；否則很容易退化成「憑印象調參」。

