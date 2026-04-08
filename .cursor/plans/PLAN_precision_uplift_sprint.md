# Precision 提升衝刺計畫（Recall=1%）

> 最後更新：2026-04-08  
> 目標：在相同評估口徑下，將 `precision@recall=1%` 由目前約 40% 提升至 **>60%**。

---

## 1. 成功定義與評估契約

| 項目 | 定義 |
| :--- | :--- |
| 主指標 | `precision@recall=1%` |
| 目標門檻 | `>= 60%` |
| 評估約束 | 同資料切分、同時間窗、同標籤定義（避免口徑漂移） |
| 穩定性要求 | Forward/Purged 時序驗證平均達標，且波動可控 |
| 上線門檻 | 不只單一 holdout 漂亮，需跨窗一致成立 |

---

## 2. 四週執行路線圖（Sprint Plan）

### Week 1：根因診斷（RCA）與上限拆解

| 任務 | 具體內容 | 產出 |
| :--- | :--- | :--- |
| 錯誤切片分析 | 依日期、table、玩家層級、新舊戶、下注額、活躍度切片，檢查 `precision@1% recall` 與樣本占比。 | `slice_performance_report`，列出 top 拖累切片 |
| 標籤品質稽核 | 量化 censored / 延遲標註比例；抽樣高分 false positive 判斷是否標註延遲或真噪音。 | `label_noise_audit` |
| 特徵可用時點對齊 | 確認 train/serve 特徵 timestamp 對齊與無 leakage。 | `point_in_time_parity_check` |
| 現行上限確認 | 在固定契約下重跑「已知 threshold」上限測試，驗證 40% 結論可重現。 | `upper_bound_repro` |

**Week 1 Gate**：完成 RCA，明確指出「模型限制 vs 標籤/資料限制」主因排序。

---

### Week 2：高槓桿模型策略（A/B/C 並行）

| Track | 任務 | 具體內容 | 預期效果 |
| :--- | :--- | :--- | :--- |
| A | 排序導向訓練 | 強化 class weighting / focal-like 權重，優先優化前段排序品質。 | 提升 top 段 precision |
| A | Hard Negative Mining | 對「高分但實際為負」樣本加權回訓。 | 直接降低誤報 |
| B | 分群建模 + Gating | 以玩家狀態/活躍度等路由到子模型（2~4 群起步）。 | 減少單一模型欠擬合 |
| C | 穩健時序驗證 | Forward/Purged CV，輸出 mean/std，過濾不穩配置。 | 防止單窗幻覺 |

**Week 2 Gate**：至少 1 條路線相對基線有顯著 uplift（建議門檻：+3~5pp）。

---

### Week 3：特徵深化與集成加碼（在勝者路線上）

| 任務 | 具體內容 | 產出 |
| :--- | :--- | :--- |
| 動態行為特徵 | 建立短中長期差值、變化率、波動度、連續性特徵。 | `feature_uplift_table` |
| 針對拖累切片做 feature pack | 僅對 top 拖累切片擴增最相關特徵，避免全域盲擴。 | `slice_targeted_features` |
| 分群後集成 | 群內最佳模型 + 群間融合（非盲目堆疊）。 | `ensemble_ablation` |
| 高分段校準 | 在高分區段做專門校準與 decision policy 檢查。 | `top_band_calibration_report` |

**Week 3 Gate**：在 Week 2 勝者基礎上再提升，且不犧牲跨窗穩定性。

---

### Week 4：定版、回放與上線決策

| 任務 | 具體內容 | 產出 |
| :--- | :--- | :--- |
| 最終候選定版 | 鎖定資料窗、特徵集、模型設定、閾值規則。 | `candidate_freeze` |
| 多窗回放驗證 | 以多時間窗重跑主指標 + 切片指標。 | `multi_window_backtest` |
| 上線影響估算 | 告警量、誤報量、業務 KPI 變化預估。 | `impact_estimation` |
| Go/No-Go 會議包 | 匯總證據，做上線或延後判斷。 | `go_no_go_pack` |

**Week 4 Gate**：主指標達標且跨窗穩定，才進入上線流程。

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

1. 若 Week 1 顯示主因為標籤噪音/延遲，優先修資料與標籤流程，暫停大規模模型擴張。  
2. 若任一路線 uplift 小於 +3pp 且不穩定，立即降級投入，避免無限調參。  
3. 若結果僅在單一時間窗成立，不納入定版候選。  
4. Ensemble 若僅帶來微小提升但大幅增加複雜度，優先保留可維運性更高方案。  

---

## 6. 交付節奏（建議）

- 每週固定一次 checkpoint：更新主指標、切片排名、路線保留/淘汰決策。  
- 每兩週一次決策會：是否切換主路線、是否提早進入定版。  
- 每次 checkpoint 必須附實驗矩陣更新，不接受口頭結論。  

