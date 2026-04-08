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

