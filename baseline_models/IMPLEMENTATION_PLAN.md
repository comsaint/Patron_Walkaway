# 基線模型實作計畫

可操作之執行順序、單次 run 檢查清單與 Gate 簽核，見同目錄 [`EXECUTION_PLAN.md`](EXECUTION_PLAN.md)。

## 1. 範圍

本計畫將 `baseline_models/` 下的基線實驗落地，並對齊以下契約（含 **§8.1 公平比較契約**）：
- `ssot/baseline_model_eval_ssot.md`
- 目前專案指標契約（`precision@recall=1%`）
- trainer/backtester 使用的時序切分與防洩漏規則

目標：
- 建立可重現的規則型、**單特徵排名（S1）**與簡單 ML 基線模型。
- 在完全相同的評估契約下，將所有基線與現行 LightGBM 對比。
- 讓執行時間與記憶體使用維持筆電可承受。


## 2. 交付物

必要產出：
1. `baseline_models/src/` 基線實作程式
2. `baseline_models/config/` 實驗設定（時間窗、特徵集、門檻）
3. `baseline_models/results/<run_id>/baseline_metrics.json`
4. `baseline_models/results/<run_id>/baseline_summary.md`
5. `baseline_models/results/<run_id>/run_state.json`

選配產出：
- `baseline_models/results/<run_id>/baseline_predictions.parquet`


## 3. 專案目錄規劃

建議結構：

```text
baseline_models/
  IMPLEMENTATION_PLAN.md
  EXECUTION_PLAN.md
  README.md
  config/
    baseline_default.yaml
  src/
    data_contract.py
    feature_views.py
    rules/
      pace_rules.py
      loss_rules.py
      adt_rules.py
      single_feature_rank.py
    models/
      logistic_baseline.py
      sgd_baseline.py
      tree_baseline.py
    eval/
      metrics.py
      runner.py
  results/
    <run_id>/
      baseline_metrics.json
      baseline_summary.md
      run_state.json
```


## 4. 工作拆解

## 4.1 基礎建設

任務 F1 - 專案骨架
- 在 `baseline_models/` 下建立資料夾與模組骨架。
- 新增 `README.md`，提供快速啟動指令。

DoD：
- 目錄樹完成，模組 import 可正常解析。

任務 F2 - 共用資料契約轉接層
- 新增 loader 包裝，強制以下一致性：
  - 相同標籤契約
  - 排除 censored 樣本
  - 相同時間窗與切分
  - 禁止隨機 shuffle

DoD：
- 契約不一致時會 fail-fast。
- 至少一次 smoke run 可驗證 schema 與必要欄位。


## 4.2 規則型基線（Tier-0）

任務 R1 - 活動下降（pace drop）基線
- 以現有 pace 訊號實作排序分數：
  - `pace_drop_ratio`
  - `pace_drop_ratio_w15m_w30m`
  - `prev_bet_gap_min`
- 使用 PR 曲線評估並回報 `precision_at_recall_0.01`。

DoD：
- 至少一個 run window 產出完整指標。

任務 R2 - 損失上限基線（雙 proxy，必做）
- 實作兩個獨立版本：
  - `loss_proxy=net`
  - `loss_proxy=wager`
- 結果必須分開報告（不得合併成單一分數）。

DoD：
- 兩個版本皆完成，且有各自 metrics 與 summary 列。
- `net` 正負號慣例已在 run notes 記錄。

任務 R3 - ADT/理論貢獻基線
- 由可用 profile 欄位估算 ADT 類數值：
  - `theo_win_sum_30d`, `theo_win_sum_180d`
  - 依設定使用 active-day/session fallback
- 實作比例分數（`current_session_theo / adt_est`）與門檻掃描。

DoD：
- ADT 估算定義已寫入 `run_state.json`。
- 至少一個 ADT 變體完成評估與摘要。


## 4.3 Tier-1：簡單 ML 與單特徵排名（必跑；對齊 SSOT §4.2）

任務 M1 - 邏輯回歸基線
- 僅使用時序切分訓練。
- 先以 `class_weight=balanced` 起跑；solver 優先 `saga`（SSOT §4.2）。

DoD：
- 報告包含 SSOT §7 canonical：`precision_at_recall_0.01`、`threshold_at_recall_0.01`、`pr_auc`。
- `baseline_family=linear`。

任務 M2 - SGD 分類器基線
- 以資源友善預設訓練 `SGDClassifier(loss="log_loss")`、`class_weight=balanced`（SSOT §4.2）。

DoD：
- 在目標筆電設定下可完成且不 OOM。
- 使用與 M1 相同 evaluator 產出指標；`baseline_family=linear`。

任務 S1 - 單特徵排名（無訓練）
- 對**單一**高訊號欄位直接排序，並以與 E1 相同之 PR／recall=1% 口徑評估。
- 建議至少：**pace 類**一欄、**loss proxy 類**一欄（`net` 與／或 `wager`；與 R2 分開列示，不得合併分數）。

DoD：
- `baseline_metrics.json` 至少兩筆合格 S1 列（pace + loss 各一，或等價覆蓋）。
- `baseline_family=rule`；`model_type` 標明欄位名與排序方向；`proxy_type` 能對應 SSOT 列舉則填，否則於 `notes` 註明。
- `baseline_summary.md` 含 S1 獨立小節（不得與 R1／R2 混名義）。


## 4.4 可選基線（Tier-2）

任務 O1 - 淺層決策樹
- 加入小範圍深度網格（`max_depth <= 6`），僅作可解釋性對照。

任務 O2 - GaussianNB 健康檢查
- 作為可選診斷，不列為決策級基線。

DoD：
- 可選輸出明確標示為非決策基線。


## 4.5 評估與報告

任務 E1 - 統一 evaluator
- 對所有基線使用同一 evaluator（語意對齊 trainer／backtester；**輸出鍵名對齊 SSOT §7**）：
  - `precision_at_recall_0.01`
  - `threshold_at_recall_0.01`
  - `pr_auc`
  - `alerts`／`alerts_rate`

DoD：
- 規則型、S1、ML 模型輸出 schema 一致（含 §7 身分欄與指標欄）。

任務 E2 - 摘要產生器
- 產生 markdown 報告，包含：
  - 與 LightGBM 基準對照表
  - pace/loss/ADT 分章呈現
  - `loss_proxy=net` 與 `loss_proxy=wager` 分列
  - **S1 單特徵排名**獨立小節或表格（SSOT §8）

DoD：
- `baseline_summary.md` 可在 results 目錄自動產生。


## 4.6 公平比較判定（Pass/Fail Gate）

任務 **E3** — Fair Compare Gate（與 trainer `model_metadata.json` 對齊）

### 任務目標

在 baseline 與 LightGBM 對照前，先做機械化「公平比較檢查」，避免 apples-to-oranges。

### 判定項（必做）

1. **A** 全域時間窗一致  
2. **B** 切分規則一致  
3. **C** 切分邊界一致  
4. **D** 標籤契約一致  
5. **E** 指標口徑一致  
6. **F** 資料來源可追溯  

（各項定義與證據來源見 [`EXECUTION_PLAN.md`](EXECUTION_PLAN.md) §4.1 表；契約見 [`ssot/baseline_model_eval_ssot.md`](../ssot/baseline_model_eval_ssot.md) §8.1。）

### DoD

- 每次與 trainer 同窗對照之 run，`run_state.json` 含 `fair_compare_checklist`（A～F 各自 pass/fail 與證據路徑）。  
- `baseline_summary.md` 含 `overall_decision`（PASS／BLOCKED／FAIL）。  
- 若非 PASS，summary 必須明示：失敗項（A～F）、差異細節、是否僅能並列觀察（不可下勝負結論）。

### 驗收規則

- 僅當 `overall_decision=PASS` 時，該次 run 可作為「baseline 相對 LightGBM」之性能結論依據。  
- `BLOCKED`／`FAIL` 之 run 可保留結果，但不得作為優劣定論依據。


## 5. 里程碑

M1（第 1-2 天）：基礎建設
- F1、F2 完成，smoke run 綠燈。

M2（第 3-4 天）：Tier-0 規則型
- R1、R2、**R3** 完成（含 loss **net／wager** 雙 proxy 分開報告、ADT 至少一變體），產出首份完整 Tier-0 報告。

M3（第 5-6 天）：Tier-1（ML + S1）
- M1、M2、**S1** 完成，產出整合比較報告。

M4（第 7 天）：收斂與穩定化
- E1、E2 打磨，若時間允許補 O1/O2。


## 6. Gate 準則

PASS：
- Tier-0 完成（含 loss 兩種 proxy）。
- Tier-1 完成：`LogisticRegression`、`SGDClassifier`、**S1 單特徵排名（無訓練）**。
- 指標 schema 完整且契約一致（`baseline_metrics.json` 含 SSOT §7 canonical 鍵名）。

BLOCKED：
- 缺少必要工件。
- ADT 或 net 定義未定案。
- 執行時間/記憶體限制導致必做項目無法完成。

FAIL：
- 發現資料洩漏。
- 違反時序切分契約。
- 評估 schema 與 SSOT 不一致。


## 7. 執行時間與記憶體控制

- 預設一次只跑一個重工作業。
- 先跑短時間窗，再擴展到完整窗口。
- 若資源壓力升高，優先規則型 + 線性模型。
- 每次 run 都記錄 `runtime_sec` 與 `peak_memory_est_mb`。


## 8. 風險與緩解

風險 1：net 正負號混淆（玩家視角 vs 場館視角）
- 緩解：在 config 與輸出備註中強制明確符號慣例。

風險 2：稀疏歷史下 ADT 估算不穩
- 緩解：使用 fallback 階層與最低歷史門檻保護。

風險 3：與既有 pipeline 變成 apples-to-oranges 比較
- 緩解：只允許中央契約轉接層 + 共用 evaluator。

風險 4：筆電 OOM/耗時爆炸
- 緩解：分階段時間窗、限制特徵集、優先 SGD 而非重模型。


## 9. 執行檢查清單

- [ ] 建立骨架（`config/`、`src/`、`results/`）
- [ ] 實作契約轉接層
- [ ] 實作 pace 規則基線
- [ ] 實作 loss 基線（`net`、`wager`）
- [ ] 實作 ADT 基線
- [ ] 實作 Logistic 基線
- [ ] 實作 SGD 基線
- [ ] 實作單特徵排名基線（S1，無訓練）
- [ ] 串接統一 evaluator
- [ ] 產出 baseline summary
- [ ] 驗證 Gate 並封裝最終 run 產物

