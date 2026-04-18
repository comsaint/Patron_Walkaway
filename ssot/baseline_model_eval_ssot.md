# BASELINE_MODEL_EVAL_SSOT (Consolidated v0.3)

> 版本：v0.4（與 `baseline_models/IMPLEMENTATION_PLAN.md`、`baseline_models/EXECUTION_PLAN.md` 對齊：Tier-1 含 S1、§3.3 canonical 鍵名、§8 摘要、**§8.1 公平比較契約**）  
> 目的：定義 Precision Uplift 專案 baseline 評估的唯一契約（SSOT）  
> 主指標：`precision@recall=1%`（同現行 pipeline 口徑）  
> 適用範圍：規則型 baseline、**單特徵排名（無訓練）**、簡單 ML baseline（不含強 boosting 競賽）

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
  - `pr_auc`（PR-AUC 數值；鍵名以 §7 為準）
  - `alerts`／`alerts_rate`（量級）
- 至少完成：
  - Tier-0（規則型）全數
  - Tier-1 全數：`LogisticRegression`、`SGDClassifier`、**單特徵排名（無訓練）**（見 §4.2）

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
- 指標**語意**與 trainer／backtester 一致（含 PR 上 recall≥1% 之 precision 定義）
- **`baseline_metrics.json` 對外鍵名以本文件 §7（canonical）為準**（例如 `pr_auc`、`precision_at_recall_0.01`）；若另附 trainer 風格鍵（如 `test_precision_at_recall_0.01`）僅可作**額外**除錯欄位，**不得**取代 §7 或讓驗收依賴欄位映射表

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

## 4.2 Tier-1：簡單 ML 與單特徵排名（必跑）

1. LogisticRegression
   - `class_weight=balanced`
   - solver 優先 `saga`
   - penalty：`l2` 或 `elasticnet`

2. SGDClassifier (`loss=log_loss`)
   - `class_weight=balanced`
   - 作為大樣本低記憶體基線

3. 單特徵排名（無訓練；實作代號 **S1**）
   - 對**單一**高訊號欄位直接排序，並以 PR 曲線取 recall=1% 操作點（與主指標契約一致）
   - 建議至少覆蓋 **pace 類** 與 **loss proxy 類** 各一欄（與 §4.1 R2 之複合規則分列；不得合併成單一「loss baseline」分數）
   - 報告欄位：`baseline_family=rule`；`model_type` 標明欄位名與排序方向；`proxy_type` 能對應列舉則填，否則於 `notes` 註明欄位語意

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
- **單特徵排名（S1）**獨立小節或表格（欄位名、方向；與 R1／R2 不得互換名義）
- 若做校準，前後並列表格

---

## 8.1) 公平比較契約（Fair Compare Contract）

為避免 baseline 與主模型（LightGBM／trainer）比較失真，下列條件為**強制**；任一不成立時，該次比較**不得**作為勝負結論，僅能標示為並列觀察或 BLOCKED。

### 必要條件（A～F）

- **A. 全域時間窗一致**  
  baseline `data_window` 必須與 trainer `model_metadata.json` 之 `global_window`（起訖）一致。

- **B. 切分規則一致**  
  皆須時序切分、**禁止 shuffle**；train／valid／test 比例與協定與 trainer `split_method` 一致。

- **C. 切分邊界一致**  
  train／valid／test 之時間界須一致；若因欄位語意（例如 `bet_time` vs `payout_complete_dtm`）存在已知偏移，須在 `notes` 與 `fair_compare_checklist` 中**明文記錄**並宣告是否仍視為可比。

- **D. 標籤契約一致**  
  `label_contract_version` 一致，且 `censored` 排除規則一致。

- **E. 指標口徑一致**  
  比較時不得混用 raw 與 production-adjusted 指標（須同一口徑：raw 對 raw，或 adjusted 對 adjusted）。

- **F. 資料來源可追溯**  
  baseline 評估資料須可追溯到對應 trainer 訓練／評估視窗與資料定義（例如 `data_source`、`reference_model.apply_training_provenance`、匯出切片腳本與路徑）。

### 判定結果

- **PASS**：A～F 全部成立，可做公平比較。  
- **BLOCKED**：缺少必要證據，暫不可比較。  
- **FAIL**：存在明確不一致，不可比較。

### 工件要求

每次 run 必須在 `run_state.json` 記錄：

- `fair_compare_checklist`（A～F 各自 pass／fail／blocked 與簡短理由）  
- `overall_decision`（PASS／BLOCKED／FAIL）  
- 證據路徑或鍵引用（例如 trainer `model_metadata.json`、`training_metrics.json` 路徑）

`baseline_summary.md` 必須同步呈現上述判定（不得僅口頭宣稱同窗）。

---

## 9) Gate 定義

### PASS
- Tier-0 全部完成（含 loss 雙 proxy）
- Tier-1 全部完成：`LogisticRegression`、`SGDClassifier`、**單特徵排名 S1（無訓練）**
- 指標完整且與主流程口徑一致；**§7 canonical 鍵名**已出現在 `baseline_metrics.json`
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