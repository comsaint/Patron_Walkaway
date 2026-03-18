# Phase 2 P1.5：Training–Serving Skew Check Runbook

> 讓 skew 驗證為可執行流程。依據：`.cursor/plans/PLAN_phase2_p0_p1.md` T9。本工具為 **one-shot / manual**。

---

## 目的

- 比對同一批實體（ids / timestamps）在 **serving 路徑**與 **training 路徑**的特徵推導結果是否一致。
- 產出：不一致欄位列表、摘要表；可選 CSV / markdown 供留存。

---

## 輸入

- **Serving-side**：一批 id（與可選 timestamp）及其在 serving 端算出的特徵（CSV 或 Parquet）。
- **Training-side**：同一批 id（與 timestamp）在 training 端算出的特徵（CSV 或 Parquet）。
- 兩表須有共同鍵欄位（預設 `id`，可指定），其餘欄位為待比對之特徵。

---

## 如何執行

從 repo 根目錄執行：

```bash
python -m trainer.scripts.check_training_serving_skew \
  --serving path/to/serving_features.csv \
  --training path/to/training_features.csv \
  [--id-column id] \
  [--output out/skew_check_report.md]
```

- **--serving**：serving 端特徵檔路徑（CSV 或 Parquet）。
- **--training**：training 端特徵檔路徑（CSV 或 Parquet）。
- **--id-column**：共同鍵欄位名稱，預設 `id`。
- **--output**：可選；輸出 markdown 摘要檔路徑。不指定則僅印至 stdout。

比對方式：依共同鍵合併兩表，逐欄比較；列出至少有一筆不一致的欄位與不一致筆數。

**安全與使用注意**：路徑應為受控來源，勿對未信任輸入或敏感路徑執行。

---

## 手動驗證建議

1. 準備兩份小檔：同一批 id、相同欄位，一份與 training 一致、一份故意改一欄數值，執行腳本，確認輸出列出該欄為不一致。
2. 兩份完全一致時，確認輸出為「無不一致」或不一致列表為空。
3. 產出一份 skew check 輸出（markdown 或 stdout）留存，完成 DoD。

---

## 相關文件

- **Alert runbook**：`doc/phase2_alert_runbook.md`
- **Evidently 使用**：`doc/phase2_evidently_usage.md`
