# 基線模型（Baseline models）

本目錄承載 walkaway 預警的對照基線：在與主流程一致的契約下，產出可重現的規則型、單特徵排名與簡單線性模型，並以 `precision@recall=1%` 為主軸與 LightGBM 同窗比較。

## 為什麼需要

- 建立性能下界與可解釋對照，避免只報複雜模型卻缺少審計錨點。  
- 強制同一標籤、時間窗、時序切分與指標口徑，避免 apples-to-oranges。  
- 以 Gate（PASS／BLOCKED／FAIL）收斂實驗是否完成、是否可對外宣稱結論。

## 契約與文件（請依此順序閱讀）

| 優先序 | 文件 | 說明 |
|--------|------|------|
| 1 | [`../ssot/baseline_model_eval_ssot.md`](../ssot/baseline_model_eval_ssot.md) | 唯一評估契約：baseline 清單、切分與指標、必填欄位、輸出工件、Gate。 |
| 2 | [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | 範圍、交付物、目錄規劃、工作拆解與里程碑。 |
| 3 | [`EXECUTION_PLAN.md`](EXECUTION_PLAN.md) | 執行順序、單次 run 檢查清單、與 SSOT 對齊的驗收要點。 |

## 範圍摘要（與 SSOT 一致）

- Tier-0（規則型）：pace、loss（`net` 與 `wager` 分開報告）、ADT／理論貢獻估算。  
- Tier-1（必跑）：`LogisticRegression`、`SGDClassifier`、單特徵排名 S1（無訓練）。  
- Tier-2（可選）：淺層決策樹、GaussianNB（僅輔助，不作主結論）。

主指標與輸出鍵名以 SSOT **§7** 為準（例如 `precision_at_recall_0.01`、`pr_auc`）；trainer 風格鍵僅可作額外除錯欄位，見 SSOT **§3.3**。

## 目錄規劃（目標狀態）

實作進行中時，建議逐步對齊下列結構（細節見 `IMPLEMENTATION_PLAN.md` §3）：

```text
baseline_models/
  README.md
  IMPLEMENTATION_PLAN.md
  EXECUTION_PLAN.md
  config/
    baseline_default.yaml   # 預設：synthetic_smoke + 可選同窗
    baseline_full.yaml      # 範例：Parquet 真資料 + 同窗（見檔內註解）
  scripts/
    export_baseline_slice_from_chunk.py  # 自 trainer chunk 匯出 Parquet 切片
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
      baseline_predictions.parquet   # 選配
```

## 每次 run 的產物

在 `results/<run_id>/` 下至少應有（SSOT §8）：

1. `baseline_metrics.json`：每筆 baseline 含 SSOT §7 必填欄位與 canonical 指標鍵。  
2. `baseline_summary.md`：含 LightGBM 同窗對照、pace／loss／ADT 分章、net／wager 分列、S1 獨立小節。  
3. `run_state.json`：實驗識別、時間窗、切分與版本欄位；`notes` 記錄符號約定、ADT 公式、降級理由等。

`run_id` 慣例與單次 run 勾選項見 `EXECUTION_PLAN.md`。

## 快速開始（如何執行）

在**倉庫根目錄**執行（需已安裝依賴且可 `import baseline_models`）。

### CLI

`smoke` 與 `run` 呼叫**同一實作**（`run_smoke`）：Tier-0／Tier-1 與 YAML 內 `reference_model` 同窗；差別僅語意（`smoke` 常搭配合成資料做契約檢查，`run` 常搭配真資料）。

```bash
python -m baseline_models smoke --config <設定檔.yaml> --run-id <run_id>
python -m baseline_models run   --config <設定檔.yaml> --run-id <run_id>
```

- **`--config`**：例如 `baseline_models/config/baseline_default.yaml` 或 `baseline_models/config/baseline_full.yaml`  
- **`--run-id`**：產物寫入 `baseline_models/results/<run_id>/`（慣例見 `EXECUTION_PLAN.md`）

### 設定檔範例

| 設定檔 | 用途 |
|--------|------|
| `config/baseline_default.yaml` | `data_source.kind: synthetic_smoke`；適合 CI／本機快速驗證 |
| `config/baseline_full.yaml` | `data_source.kind: parquet` 指向倉庫根 `data/` 下切片；檔內註解含資料準備指令 |

真資料切片可先用（倉庫根）：

```bash
python baseline_models/scripts/export_baseline_slice_from_chunk.py \
  --chunk trainer/.data/chunks/<你的_chunk>.parquet \
  --out data/baseline_for_baseline_models.parquet \
  --max-rows 200000
```

再執行：

```bash
python -m baseline_models run \
  --config baseline_models/config/baseline_full.yaml \
  --run-id 20260418_baseline_full
```

### 同窗（LightGBM）P@R=1%

摘要表與 Δ 所採之參考 precision：若 `training_metrics.json` 區段內有 **`test_precision_at_recall_0.01_prod_adjusted`**（負採樣／生產先驗修正），**優先使用**；否則退回 `test_precision_at_recall_0.01`。實作見 `src/eval/reference_model.py`。

### 單元測試

```bash
python -m pytest tests/unit/test_baseline_models_smoke.py -q
```

與主流程指標語意對齊時，可參考 `trainer/training/trainer.py`、`trainer/training/backtester.py` 中 precision-at-recall 與 PR 曲線相關實作。

## 相關程式（主專案）

- 訓練／回測主線：`trainer/`  
- 本套件目標為獨立可重現的 baseline 評估，輸出 schema 與 Gate 以 SSOT 為準，不依賴未文件化的捷徑。

---

若有契約或 baseline 清單變更，請先更新 SSOT（§10），再改程式與本目錄內計畫文件。
