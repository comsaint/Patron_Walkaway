# C1 多窗 Gate 契約 v1

> **層級**：Working / execution（與 `PRECISION_UPLIFT_DELIVERY_PLAN.md` C1 / R10 對齊）  
> **目的**：固定多窗評估與 gate 規則，使 `report_w2_objective_parity` 類彙總可對照本檔做自動 verdict。  
> **資料可用區間**：`2025-01-01` 起至 `2026-04` 初（以實際資料與 CLI `--end` 為準）。

---

## 1. 時間語意與邊界

- **時間欄位**：與 trainer/backtester 一致，以 `payout_complete_dtm` 為排序與切窗依據（見 `trainer/training/time_fold.py` 契約）。
- **Purge gap**：**2 日**（固定）。  
  - **Train 可含資料**：`[global_start, test_month_start - 2 days)`（exclusive end 與 trainer 視窗語意對齊時，以「test 窗第一筆 eligible 時間之前 48 小時」為 train 上界較穩；實作 orchestrator 時請用同一時區，例如 HK）。
  - **Test 窗**：僅該曆月內 eligible rows（見下節「評估窗」）。

---

## 2. 評估窗（Rolling-origin，6 窗）

**Global train anchor**：自 `2025-01-01` 起累積歷史至各 test 月 purge 上界止（與 §8「P1.2 預設 train-start」**不同**；C1 跑矩陣時必須覆寫 orchestrator 的 `--train-start`）。

| 窗 ID | Test 曆月（inclusive） | Train 上界（概念） | 說明 |
|:-----:|------------------------|-------------------|------|
| W1 | 2025-10 | test 月首日前 2 日 | 最早一窗；在 anchor=`2025-01-01` 時 train 約 9 個月（若誤用 `2024-01-01` 起則會多約 12 個月，**不**符合本契約） |
| W2 | 2025-11 | 同上規則 | |
| W3 | 2025-12 | 同上規則 | |
| W4 | 2026-01 | 同上規則 | |
| W5 | 2026-02 | 同上規則 | |
| W6 | 2026-03 | 同上規則 | |

- **不納入 hard gate 的月份**：`2026-04`（僅部分月、可作 **shadow / 參考**，不計入 `effective_windows` 的 6 窗母體）。
- **Stride**：每月一窗；test 窗彼此不重疊。

---

## 3. 指標與來源欄位（與現有報表對齊）

每窗需產出（與 `trainer/scripts/report_w2_objective_parity.py` 之 `RunRow` 對齊或可映射）：

- **主排序 / gate 指標**：`bt_optuna_test_precision_prod_adjusted`（若某窗僅有 `model_default`，則在契約中註明並統一用同一分支，全矩陣須一致）。
- **輔助**：`bt_optuna_test_recall`、`bt_optuna_alerts_per_hour`（用於解釋與 soft fail，v1 可不設 hard 下限，視下一版收斂）。
- **訓練側契約**：`selection_mode_train == field_test`；`backtest_metrics.selection_mode` 若存在亦應一致。

---

## 4. 有效窗（effective window）

下列任一成立則該窗標為 **invalid**（不計入 `effective_windows`，但保留列於報表）：

- 缺 `backtest_metrics.json` 或關鍵欄位為 null（無法解讀 `precision_prod_adjusted`）。
- `selection_mode` 非 `field_test`。
- Test 月內 rated 樣本過少（門檻由 orchestrator 另訂，建議：正例數 + 可行 DEC-026 點數；此 v1 可只記錄 reason_code，hard 規則見 §5）。

**母體窗數**：`total_windows = 6`（W1–W6）。  
**Gate 計算**：僅對 **valid** 窗聚合；並要求 `effective_windows >= 5`。

---

## 5. Gate 規則（v1 數值）

### 5.1 Hard（任一失敗 → `REJECT` 或契約約定之 `HARD_FAIL`）

| 規則 ID | 條件 |
|---------|------|
| `H1_selection_mode` | 所有 valid 窗：`selection_mode_train == field_test`（且 backtest 側若寫入則一致）。 |
| `H2_effective_count` | `effective_windows >= 5`（在 `total_windows == 6` 前提下）。 |
| `H3_worst_precision_floor` | `min(valid.bt_optuna_test_precision_prod_adjusted) >= 0.40`。 |

### 5.2 Soft（失敗 → `HOLD`，不升級 deploy；程式可視為 `SOFT_FAIL`）

| 規則 ID | 條件（建議初值，可於 `gates/c1_thresholds_overrides.json` 覆寫） |
|---------|------------------------------------------------------------------|
| `S1_median_precision` | `median(valid.bt_optuna_test_precision_prod_adjusted) >= 0.46` |
| `S2_p25_precision` | `p25(valid.bt_optuna_test_precision_prod_adjusted) >= 0.45` |

> **說明**：`N=6` 時 **p10** 與最差窗過近，v1 以 **worst + median + p25** 為主；p10 可選列於報表但不作 hard。

### 5.3 Baseline 對照（若有 baseline 同矩陣跑法）

- `delta_mean = mean(challenger) - mean(baseline) >= 0`
- `delta_worst = min(challenger) - min(baseline) >= 0`  
若無 baseline 列，本節略過，不視為 hard fail。

---

## 6. Verdict 枚舉

| verdict | 含義 |
|---------|------|
| `PASS` | 全部 Hard 通過，且無 Soft 失敗（或團隊約定允許 1 條 Soft 仍算 PASS——v1 預設 **不允許**）。 |
| `HOLD` | Hard 全過，但有 Soft 失敗；保留實作、不升級 bundle。 |
| `REJECT` | 任一 Hard 失敗；停用該候選組態（關 flag / 不選該 bundle），**不必**回滾整個 codebase。 |

輸出應附 `failed_rule_ids: string[]` 與 `reason_codes`（可對照本節 ID）。

---

## 7. 機讀摘要（建議由腳本複製或合併至 `c1_gate_result.json`）

```json
{
  "schema_version": "c1_gate_contract_v1",
  "purge_gap_days": 2,
  "global_train_start": "2025-01-01",
  "investigation_p1_2_default_train_start": "2024-01-01",
  "c1_orchestrator_note": "run_train_backtest_investigation_windows.py defaults encode P1.2 only; C1 runs must pass --train-start equal to global_train_start (see module constant C1_GATE_GLOBAL_TRAIN_START).",
  "evaluation_windows": [
    {"id": "W1", "test_month": "2025-10"},
    {"id": "W2", "test_month": "2025-11"},
    {"id": "W3", "test_month": "2025-12"},
    {"id": "W4", "test_month": "2026-01"},
    {"id": "W5", "test_month": "2026-02"},
    {"id": "W6", "test_month": "2026-03"}
  ],
  "shadow_windows": [{"id": "S1", "test_month": "2026-04", "note": "partial month; not in hard gate denominator"}],
  "total_windows": 6,
  "min_effective_windows": 5,
  "primary_metric_key": "bt_optuna_test_precision_prod_adjusted",
  "hard_rules": {
    "H3_worst_precision_floor": 0.40
  },
  "soft_rules": {
    "S1_median_precision_min": 0.46,
    "S2_p25_precision_min": 0.45
  }
}
```

---

## 8. 與 repo 腳本的關係

- **Subprocess 包裝**：`trainer/scripts/run_train_backtest_investigation_windows.py` 只做「組出 `trainer` / `backtester` 的 `--start` `--end` 並執行」；**不**內建 C1 的 6 個曆月 test 迴圈，多窗須外層迴圈或另撰 orchestrator，每窗帶正確的 `train_end` / backtest 區間。
- **日期預設不可混用**：
  - 該腳本 **CLI 預設**對齊 `INVESTIGATION_PLAN_TEST_VS_PRODUCTION` **P1.2**：`--train-start` 預設為 `2024-01-01`、`--train-end` 預設 `2025-12-31`、回測窗 `2026-01-01`～`2026-03-31`（見腳本模組常數 `_DEFAULT_TRAIN_*`）。
  - **C1** 的訓練錨點為 `global_train_start`（`2025-01-01`）；程式內對齊常數為 **`C1_GATE_GLOBAL_TRAIN_START`**（與 `_DEFAULT_TRAIN_START` 不同）。跑 C1 gate 矩陣時**必須**顯式傳入 `--train-start 2025-01-01`（及該窗對應的 `--train-end`、test 月之 `--backtest-start` / `--backtest-end`），不可沿用 P1.2 預設，否則訓練資料範圍與本契約 §1–§2 不一致。
- **彙總**：`python -m trainer.scripts.report_w2_objective_parity --run-dir ... --output-csv ... --output-md ...`；gate 引擎讀 CSV + 本契約產出 `verdict`（後續實作）。

---

## 9. 修訂紀錄

| 版本 | 日期 | 變更 |
|------|------|------|
| v1 | 2026-04-27 | 初稿：purge 2 日、6 月評估窗、Hard/Soft 門檻、verdict 語意。 |
| v1.1 | 2026-04-27 | 釐清 P1.2 腳本預設 `train-start=2024-01-01` 與 C1 `global_train_start` 差異；§2／§7／§8 與 `C1_GATE_GLOBAL_TRAIN_START`。 |
