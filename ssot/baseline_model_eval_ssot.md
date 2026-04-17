# BASELINE_MODEL_EVAL_SSOT (Consolidated v0.2)

> 版本：v0.2 (consolidated draft)  
> 目的：定義 Precision Uplift 專案 baseline 評估的唯一契約（SSOT）  
> 主指標：`precision@recall=1%`（同現行 pipeline 口徑）  
> 適用範圍：規則型 baseline + 簡單 ML baseline（不含強 boosting 競賽）

---

## 1) 文件角色與優先序

本文件為 baseline 評估唯一契約，定義：
- 候選 baseline 清單與邊界
- 資料/標籤/切分/評估口徑
- 輸出工件與 Gate
- 資源限制與風險控管

優先序（衝突時）：
1. 本文件（SSOT）
2. Implementation Plan
3. Runbook / 臨時執行筆記

---

## 2) 任務定義與成功條件

### 2.1 任務定義
- 任務型態：極度不平衡二元分類（walkaway 預警）
- 核心比較目標：建立「簡單方法」對現行 LightGBM 的性能下界與可解釋性基準

### 2.2 成功條件
- 所有 baseline 在**同一契約**下可重現
- 每個 baseline 必須產出：
  - `precision_at_recall_0.01`
  - `threshold_at_recall_0.01`
  - PR-AUC
  - alerts 量級（count / rate）
- 至少完成：
  - Tier-0（規則型）全數
  - Tier-1（Logistic + SGD）全數

---

## 3) 評估契約（不可變更）

### 3.1 標籤與樣本契約
- 標籤生成沿用現行流程（包含 extended window 邏輯）
- `censored=True` 樣本一律排除（訓練與評估都一致）
- 禁止任何未來資訊洩漏（PIT 一致）

### 3.2 切分契約
- 僅可用時序切分（forward/purged 或等價）
- 禁止隨機 shuffle split
- baseline 與 LightGBM 必須使用同一批時間窗

### 3.3 指標/門檻契約
- 主指標固定為 `precision@recall=1%`
- 以 PR 曲線操作點比較，不得私自改成 accuracy/F1 主導
- 所有模型回報欄位命名與 trainer/backtester 對齊

---

## 4) Baseline 候選（Consolidated）

## 4.1 Tier-0：規則型（無需訓練，必跑）

### R1. 活動下降規則（Pace Drop）
- 核心分數（擇一或並行）：
  - `pace_drop_ratio`（w5m / w30m）
  - `pace_drop_ratio_w15m_w30m`
  - `prev_bet_gap_min`
- 邏輯：分數越高（或 gap 越大）越接近離場風險，走排名+門檻評估

### R2. 損失上限規則（雙 proxy，必須分開報告）
- R2A：`net` proxy（累積淨輸贏）
- R2B：`wager` proxy（累積下注額）
- 注意：`net` 正負號語意需先鎖定（玩家視角, negative means player's loss），不得混用

### R3. ADT / 理論貢獻規則（估算版）
- 允許以現有 profile 欄位估算 ADT，不要求先新增資料源欄位
- 建議三種估算：
  - `ADT_30d = theo_win_sum_30d / max(active_days_30d, 1)`（若無 active_days_30d 可用 /30 fallback）
  - `ADT_180d = theo_win_sum_180d / max(active_days_180d, 1)`（若無可用 /180 fallback）
  - `TheoPerSession_30d = theo_win_sum_30d / max(sessions_30d, 1)`（EOP 近似）
- 規則分數建議：`current_session_theo / ADT_est`，測試多個 `tau`（如 0.8,1.0,1.2,1.5,2.0）

---

## 4.2 Tier-1：簡單 ML（必跑）

1. LogisticRegression
   - `class_weight=balanced`
   - solver 優先 `saga`
   - penalty：`l2` 或 `elasticnet`

2. SGDClassifier (`loss=log_loss`)
   - `class_weight=balanced`
   - 作為大樣本低記憶體基線

3. 單特徵排名（無訓練）
   - 對高訊號單欄位直接排序+門檻（如 pace 或 loss proxy）
   - 作為「最低工程成本可解釋基線」

---

## 4.3 Tier-2：可選補充

1. 淺層決策樹（`max_depth <= 3~6`）
2. GaussianNB（可選，僅做 sanity check，不作主要結論）

> 註：NB 因特徵獨立性假設與實際強相關特徵不符，結果僅供輔助。

---

## 4.4 明確排除（本輪不做）

- KNN（推論與記憶體成本不友善）
- RBF SVM（大樣本訓練不可行）
- XGBoost/CatBoost（不屬於「簡單 baseline」範圍）

---

## 5) 校準政策（Calibration Policy）

- 本輪主指標為 `precision@recall=1%`（PR 排序導向），**不強制所有模型先校準**。
- 以下情境才要求校準對照（Platt/Isotonic）：
  1. 需要固定單一 threshold 跨窗部署
  2. 需比較機率值可解釋性（非僅排序）
- 若做校準，必須額外輸出「校準前 vs 校準後」並列結果，不可覆蓋原始結果。

---

## 6) 資源與效能約束（Laptop-first）

- 預設單任務重訓，不平行重模型
- 先小窗 smoke，再擴窗 full 評估
- 若有 OOM/長時風險，優先策略：
  1. 縮短時間窗
  2. 降低特徵子集
  3. 優先規則型 + SGD
- 禁止無紀錄降級；任何降級必須寫入 `notes`

---

## 7) 必填實驗欄位（最小集合）

- `experiment_id`
- `baseline_family`（rule / linear / tree / nb）
- `model_type`
- `proxy_type`（net / wager / adt30 / adt180 / theo_per_session）
- `data_window`
- `split_protocol`
- `feature_set_version`
- `label_contract_version`
- `precision_at_recall_0.01`
- `threshold_at_recall_0.01`
- `pr_auc`
- `alerts` / `alerts_rate`
- `runtime_sec`
- `peak_memory_est_mb`
- `decision`（keep / drop / iterate）
- `notes`（含符號定義、降級、例外）

---

## 8) 輸出工件規範

每次 run 至少輸出：
1. `baseline_metrics.json`
2. `baseline_summary.md`
3. `run_state.json`
4. （可選）`baseline_predictions.parquet`

`baseline_summary.md` 必含：
- 與 LightGBM 同窗對照（pp 差異）
- 規則型三類（pace/loss/ADT）分開結果
- `loss` 兩種 proxy（net/wager）分開結果
- 若做校準，前後並列表格

---

## 9) Gate 定義

### PASS
- Tier-0 全部完成（含 loss 雙 proxy）
- Tier-1 全部完成（Logistic + SGD）
- 指標完整且與主流程口徑一致
- 有可執行結論（保留/淘汰名單）

### BLOCKED
- 缺必要工件
- ADT 或 net 定義不完整導致不可比較
- 資源限制導致核心模型未完成

### FAIL
- 發現資料洩漏
- 切分契約違反
- 指標口徑與主流程不一致

---

## 10) 變更管理

以下變更必須先改 SSOT 再執行：
- baseline 名單
- proxy 定義（尤其 net 符號）
- ADT 估算公式
- 指標或 Gate 規則

禁止事項：
- 未更新 SSOT 直接改評估口徑
- 將 net/wager 結果混成單一「loss baseline」而不分開報