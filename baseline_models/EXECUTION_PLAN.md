# 基線模型執行計畫（Operational Runbook）

> 角色：把 `IMPLEMENTATION_PLAN.md` 的範圍與工作拆解，轉成**可照表操課**的執行順序、驗收動作與產物檢查。  
> 契約優先序：[`ssot/baseline_model_eval_ssot.md`](../ssot/baseline_model_eval_ssot.md) → `IMPLEMENTATION_PLAN.md` → 本文件。  
> Tier-1 必跑項與 Gate：與 SSOT **§2.2、§4.2、§8.1、§9** 及 `IMPLEMENTATION_PLAN.md` **§4.3、§4.6、§6** 一致（含 **S1**）。

---

## 實作狀態總覽（對照倉庫程式）

最後更新：**2026-04-18**。圖例：**✅** 已落地可跑 · **🔄** 部分完成 · **⏳** 未實作或僅占位 · **➖** 人工作業（非程式）。

| 區塊 | 項目 | 狀態 | 備註 |
|------|------|------|------|
| **§1 流水線** | F1 骨架 | ✅ | `config/`、`src/`、`results/`；`python -m baseline_models` |
| **§1** | F2 契約／loader | ✅ | 合成 `synthetic_smoke`／`parquet`；`censored`、時間窗、時序切分**禁 shuffle**、fail-fast |
| **§1** | Smoke | ✅ | `smoke --config … --run-id …` → 三件套 + SSOT §7 canonical 鍵 |
| **Phase B** | R1 Pace | ✅ | `rules/pace_rules.py`、`tier0.r1`；`baseline_summary` 有 R1 小節 |
| **Phase B** | R2A net／R2B wager | ✅ | `rules/loss_rules.py`；**兩筆** metrics、`proxy_type` 分 `net`／`wager`；summary 有 R2 小節 |
| **Phase B** | R3 ADT | ✅ | `rules/adt_rules.py`、`tier0.r3`（含 **`tau_grid`**、`model_type` 含 tau）；`run_state.tier0_r3`；summary 逐 (variant,tau) 列點 |
| **Phase C** | M1 LogisticRegression | ✅ | `models/logistic_baseline.py` + `tier1.m1` + `runner` smoke 路徑 |
| **Phase C** | M2 SGDClassifier | ✅ | `models/sgd_baseline.py` + `tier1.m2` + smoke `runner` |
| **Phase C** | S1 單特徵排名 | ✅ | `rules/single_feature_rank.py` + `tier1.s1.rankings` + smoke `runner`／summary |
| **Phase D** | E1 統一 evaluator | ✅ | 每列附 trainer 對齊鍵：`test_precision_at_recall_{r}`、`threshold_at_recall_{r}`、`n_alerts_at_recall_{r}`、`alerts_per_minute_at_recall_{r}`（r∈0.001,0.01,0.1,0.5）；canonical §7 不變 |
| **Phase D** | E2 摘要 | ✅ | `baseline_summary.md` 含 **Phase D — LightGBM 同窗對照** Markdown 表；`reference_model` YAML |
| **Phase E** | O1 淺樹／O2 GaussianNB | ⏳ | `tree_baseline.py` 占位；NB 未建檔 |
| **§4 Gate** | PASS／BLOCKED／FAIL | ➖ | 依 SSOT §9 人工簽核 |
| **§0** | 0.1 SSOT 對齊 | ➖ | 團隊自檢 |
| **§0** | 0.2 `net` 符號寫入 `run_state` | ✅ | `notes_contract` → `run_state.json` `notes`（合成 smoke 路徑） |
| **§0** | 0.3 LightGBM 對照來源寫入 `run_state` | ✅ | `run_state.reference_lightgbm`（`reference_model` YAML；預設載入 `package/deploy/models/training_metrics.json`） |
| **§0** | 0.4 `run_id` 慣例 | ✅ | CLI `--run-id` → `baseline_models/results/<run_id>/` |

---

## 0. 執行前對齊（同一工作階段內完成）

| 步驟 | 動作 | 完成判斷 | 程式／流程狀態 |
|------|------|----------|----------------|
| 0.1 | 閱讀 SSOT §3（標籤、切分、指標）與 §7（必填欄位） | 能口述：censored 排除、禁 shuffle、主指標為 PR 上 `precision@recall=1%` | ➖ 人工作業 |
| 0.2 | 鎖定 **`net` 正負號語意**（玩家視角：負值＝玩家虧損）並寫入首份 `run_state.json` 的 `notes` | 與 SSOT §4.1 R2 一致，後續不得改口徑不改 SSOT | ✅ `notes_contract` → `run_state.notes`（smoke 路徑） |
| 0.3 | 決定本次 **時間窗** 與 **與 LightGBM 對照的 metrics 來源**（同窗、同 split） | 路徑或實驗 ID 寫在 `run_state.json` | ✅ `reference_lightgbm`（見 `reference_model` 設定） |
| 0.4 | 建立 **`run_id`** 慣例（建議：`YYYYMMDD_baseline_<short_label>`） | 所有產物落在 `baseline_models/results/<run_id>/` | ✅ `--run-id` + `default_results_dir` |

---

## 1. 建議執行順序（依賴關係）

```text
F1 骨架 → F2 契約轉接 + smoke
    → Tier-0：R1 → R2(net) → R2(wager) → R3（可分日，但 R2 兩 proxy 不可合併成一欄）
    → Tier-1：M1 → M2 → S1（SSOT §4.2 單特徵排名，無訓練；與 Tier-0 共用 E1）
    → E2 摘要（每階段可增量更新）
    →（時間允許）O1 / O2
    → Gate 簽核（本文件 §4；PASS／BLOCKED／FAIL 定義見 SSOT §9）
```

**對照實作進度**：見本文件開頭 **「實作狀態總覽」** 表（依倉庫程式更新）。

**資源策略（與 IMPLEMENTATION_PLAN §7 一致）**

1. 同一時間只跑一個重工作業（訓練或大表掃描）。  
2. 先 **短窗 smoke**（驗證 schema、欄位、無洩漏、指標非空），再擴到目標窗。  
3. 任何降級（縮窗、減特徵、略過可選項）必須寫入 `run_state.json` 的 `notes`（SSOT §6）。

---

## 2. 分階段執行卡（Sprint 對照）

### Phase A — 基礎建設（對應 `IMPLEMENTATION_PLAN.md` §5 里程碑 M1：F1 + F2）**（實作：✅）**

1. **F1**：建立 `config/`、`src/`、`results/` 目錄與 `README.md` 中的啟動方式；確認模組可 import。**狀態：✅**  
2. **F2**：實作 loader／契約檢查：標籤、censored 過濾、時間窗、切分、**禁止 shuffle**；設計 **fail-fast**（收到欄位、dtype、缺欄時錯誤訊息含實際值）。**狀態：✅**（合成／parquet；生產資料源可再擴）  
3. **Smoke**：最小樣本或最短窗跑一次，產出 `baseline_metrics.json`（可僅含一筆 baseline 列或最小陣列）。**對外／對 SSOT 驗收鍵名必須使用 SSOT §7 canonical**：`precision_at_recall_0.01`、`threshold_at_recall_0.01`、`pr_auc`（以及 `alerts`／`alerts_rate` 等 §7 所列欄位）。內部若重複計算 trainer 風格鍵（例如 `test_precision_at_recall_0.01`）僅可作**額外**除錯欄位，**不得**取代 canonical 或讓驗收依賴映射表。**狀態：✅**（`python -m baseline_models smoke`）

**Phase A 出口**：目錄樹就緒；一次 smoke 綠燈；`run_state.json` 含 `experiment_id`、`data_window`、`label_contract_version`、`split_protocol`、`feature_set_version`（其餘 §7 欄位可於後續 phase 補齊，但 smoke 的 metrics 列仍須帶齊 canonical 指標鍵以便 early CI）。

---

### Phase B — Tier-0 規則型（R1、R2、R3）**（實作：R1／R2／R3 含 `tau` 網格 ✅）**

| 任務 | 執行要點 | 產物檢查 | 狀態 |
|------|----------|----------|------|
| **R1** Pace | 分數與排序方向與 SSOT 一致；以 PR 取 `precision_at_recall_0.01` | `baseline_metrics.json` 有獨立 `model_type`／規則識別；summary 有 pace 章節 | ✅ |
| **R2A net** | 與 R2B **分開** run 或分開 JSON 節點，**禁止**合併成單一 loss 分數 | 兩份列或兩筆記錄；`proxy_type=net` | ✅ |
| **R2B wager** | 同上 | `proxy_type=wager` | ✅ |
| **R3 ADT** | 估算式寫入 `run_state.json`；可掃 `tau`（`tier0.r3.tau_grid`；分母 `ADT_est * tau`） | `proxy_type` 使用 `adt30`／`adt180`／`theo_per_session`；`model_type` 如 `R3_adt:adt30:tau=1.0` | ✅ |

**Phase B 出口**：Tier-0 全跑完；loss 兩 proxy 在 `baseline_summary.md` 分列（IMPLEMENTATION_PLAN §4.5 E2）。  
**（進度備註）**：R1／R2／R3 已含 **`tau_grid`** 多點掃描（metrics 每個 variant×tau 一列、`run_state.tier0_r3.tau_grid`）。

---

### Phase C — Tier-1（M1、M2、S1）**（實作：M1 ✅；M2 ✅；S1 ✅）**

| 任務 | 執行要點 | 產物檢查 | 狀態 |
|------|----------|----------|------|
| **M1** LogisticRegression | `class_weight=balanced`；solver 優先 `saga`（SSOT §4.2）；僅時序訓練 | `baseline_family=linear`；每筆結果含 SSOT §7 canonical 欄位（含 `pr_auc` 鍵名） | ✅（`tier1.m1`、`run_state.tier1_m1`、`baseline_summary` Tier-1 小節） |
| **M2** SGDClassifier | `loss=log_loss`、`class_weight=balanced`；注意記憶體與迭代上限 | `baseline_family=linear`；`runtime_sec`、`peak_memory_est_mb` 有值；筆電可完成 | ✅（`tier1.m2`、`run_state.tier1_m2`、summary M2 小節） |
| **S1** 單特徵排名（無訓練） | SSOT §4.2：對**單一**高訊號欄位直接排序＋PR 曲線取 recall=1% 操作點；建議至少各跑一欄 **pace 類** 與 **loss proxy 類**（`net`／`wager` 擇一或兩者，與 R2 分開列示即可） | `baseline_family=rule`（無訓練、可解釋排序基線）；`model_type` 標明欄位名與排序方向（越高風險越…）；`proxy_type` 若該欄位屬 net／wager／ADT 估算則填 SSOT 列舉值，否則於 `notes` 註明欄位語意與為何不填列舉 | ✅（`tier1.s1`、`run_state.tier1_s1`、`model_type=S1_rank:<col>`） |

**Phase C 出口**：M1、M2、S1（至少 pace 與 loss 各一條單特徵列，或等價覆蓋）皆有 `precision_at_recall_0.01`、`threshold_at_recall_0.01`、`pr_auc`、`alerts`／`alerts_rate`。

---

### Phase D — 評估統一與報告（E1、E2）**（實作：E1／E2 核心 ✅；predictions parquet ⏳）**

1. **E1**：所有 baseline 走同一 PR／recall 掃描邏輯（`dec026_pr_alert_arrays` + `pick_threshold_dec026_from_pr_arrays`；與 `trainer/training/backtester.py` 之 `_TARGET_RECALLS` 一致）。**狀態：✅**（每列額外帶 `test_precision_at_recall_*` 等；主指標仍以 SSOT §7 canonical 為準）  
2. **E2**：`baseline_summary.md` 含 pace／loss／R3／M1／M2／S1 分章，以及 **LightGBM 同窗** Markdown 表（`eval/reference_model.py` + `reference_model` YAML）。**狀態：✅**  
3. 可選：`baseline_predictions.parquet` 若產出，需在 `run_state.json` 註明欄位與列數，避免無意義巨大檔案拖垮筆電。**狀態：⏳**

**Phase D 出口**：`baseline_models/results/<run_id>/` 內具備 SSOT §8 三件套 + 必填欄位（§7）；**§8 同窗對照**於 summary + `run_state.reference_lightgbm`（可關 `reference_model.enabled`）。

---

### Phase E — 可選 Tier-2（O1、O2）**（實作：⏳）**

- **O1**：淺樹深度網格小；結果標為可解釋對照。**狀態：⏳**（`tree_baseline.py` 占位）  
- **O2**：GaussianNB 標註「非決策基線／sanity only」。**狀態：⏳**  
- 若資源不足：記 `notes` 後跳過，並標 Gate **BLOCKED** 原因（不得假裝完成）。

---

## 3. 單次 Run 的操作檢查清單（複製使用）

在每次產生新 `run_id` 時逐項勾選（**SSOT §7 每一筆 baseline 列皆須可核對**）：

- [ ] **`baseline_metrics.json`**：每筆含 `experiment_id`、`baseline_family`、`model_type`、`proxy_type`、`data_window`、`split_protocol`、`feature_set_version`、`label_contract_version`  
- [ ] **`baseline_metrics.json`**：每筆含 canonical 指標鍵 `precision_at_recall_0.01`、`threshold_at_recall_0.01`、`pr_auc`、`alerts`／`alerts_rate`、`runtime_sec`、`peak_memory_est_mb`、`decision`、`notes`  
- [ ] **Tier-1**：M1、M2、**S1（單特徵排名）**皆已各至少一筆合格列（S1 欄位與 `proxy_type`／`notes` 約定見 Phase C）  
- [ ] `run_state.json` 已填與本次 run 一致之 `experiment_id`、`data_window`、`split_protocol`、`feature_set_version`、`label_contract_version`（與 metrics 對齊）  
- [ ] `net` 符號與 ADT 公式已寫入 `notes`（若本次涉及）  
- [ ] 已排除 `censored=True`；無未授權 shuffle  
- [ ] `baseline_metrics.json` 結構可被下游腳本解析（建議以小型 jq／Python one-liner 驗證）  
- [ ] `baseline_summary.md` 已含 SSOT §8 要求之對照與分章（含 S1 小節）  
- [ ] 已對照 Gate（**本文件 §4**；定義 **SSOT §9**）確認 PASS／BLOCKED／FAIL；每筆 `decision`（keep／drop／iterate）與理由可追溯到 `notes`

---

## 4. Gate 簽核（執行面）

| 狀態 | 觸發條件（摘要） | 執行動作 |
|------|------------------|----------|
| **PASS** | Tier-0 全、Tier-1 全（**M1 + M2 + S1 單特徵排名**）、指標完整、可比 | 封存 `results/<run_id>/`；在 summary 寫結論與保留名單 |
| **BLOCKED** | 缺工件、定義不清、資源不夠 | **停止**對外宣稱完成；補 SSOT 或縮 scope 後重跑 |
| **FAIL** | 洩漏、切分違規、口徑不一致 | 作廢該 run 結論；修復後新 `run_id` |

### 4.1 公平比較判定表（pass/fail）

> 目的：判定 baseline 與 LightGBM（trainer）是否屬於「同窗、同契約、可公平比較」。  
> 若任一必填項不通過，該輪比較僅可標示為「並列觀察」，不得作為勝負結論。

| 檢查項 | PASS 條件 | FAIL 條件 | 證據來源 |
|--------|-----------|-----------|----------|
| **A** 全域時間窗一致 | trainer `model_metadata.json` 之 `global_window.start/end` 與 baseline `data_window.start/end` 一致 | 任一端缺失或不一致 | `out/models/<version>/model_metadata.json`、`run_state.json` |
| **B** 切分規則一致 | 皆為時序切分、禁 shuffle；`train_frac`／`valid_frac`（及隱含 test）與 trainer `split_method` 一致 | 有 shuffle、比例或協定不同 | `model_metadata.split_method`、baseline `split`／`split_protocol` |
| **C** 切分邊界一致 | train/valid/test 之時間界（至少各 split 的 `start`/`end`）一致，或在 SSOT 已宣告之可接受誤差內 | 邊界明顯不同且未文件化 | `model_metadata.splits`、`run_state.temporal_split_ends` |
| **D** 標籤契約一致 | `label_contract_version` 一致；`censored` 排除規則一致 | 契約版本不同或排除規則不同 | `run_state.json`、`baseline_metrics.json` 列、`training_metrics.json` 相關註記 |
| **E** 指標口徑一致 | 比較時同一口徑（raw 對 raw；`prod_adjusted` 對 `prod_adjusted`） | 混用口徑 | `baseline_metrics.json`、`training_metrics.json`（`rated` 區） |
| **F** 資料來源可追溯 | baseline 評估用表／切片可追溯到該次 trainer 視窗與資料定義 | 來源為不同窗、不明切片或未記錄 | baseline YAML `data_source`、`reference_model`／`merge_training_provenance`、`run_state.notes` |

**判定規則**

- **PASS（可公平比較）**：A～F 全部通過。  
- **BLOCKED（不可下結論）**：任一項資料缺失，無法判定。  
- **FAIL（不公平比較）**：任一項明確不一致。

**執行輸出要求（每次與 trainer 對照時落地）**

- 在 `baseline_summary.md` 增一段「公平比較判定」：`overall_decision`（PASS／BLOCKED／FAIL）、`failed_checks`（A～F）、`notes`（口徑差異與處置）。  
- 在 `run_state.json` 增加 `fair_compare_checklist` 區塊，保存每項結果與證據路徑（對齊 SSOT §8.1）。

---

## 5. 與主程式碼的對齊錨點（實作時查）

- 指標語意：`trainer/training/trainer.py`（`_compute_test_metrics`／`_TARGET_RECALLS`）、`trainer/training/backtester.py`（precision-at-recall 與 alerts）。  
- 專案決策脈絡：`.cursor/plans` 內 DEC-026 與 phase1 文件（閾值與 recall 集合演進）。  
- **變更評估口徑或 baseline 清單**：先改 SSOT（SSOT §10），再改程式與本 runbook。

---

## 6. 文件維護

- **現況**：已可執行 `python -m baseline_models smoke --config <yaml> --run-id <id>`；`README.md` 仍可補齊與 **LightGBM 同窗對照**、完整 Tier-1 之一鍵範例（見上文 **§0.3** 狀態）。  
- 若與 `IMPLEMENTATION_PLAN.md` 衝突，以 **SSOT** 為準並回頭更新 `IMPLEMENTATION_PLAN.md`。
