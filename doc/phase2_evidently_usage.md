# Phase 2 P1.4：Evidently DQ / Drift 報告使用說明

> 本文件與 `doc/phase2_alert_runbook.md` 情境三（Drift report 異常）對應。  
> 依據：`.cursor/plans/PLAN_phase2_p0_p1.md` T8。Evidently 僅供 **manual / ad-hoc** 使用。

---

## 目的

- 提供可手動執行的 DQ / drift 報告產生工具。
- 輸入：reference（訓練或基準快照）、current（目前資料）的檔案路徑；由人工挑選或前置匯整。
- 輸出：本地 HTML（與可選 JSON）報告，供 triage 與 drift 調查使用。

---

## OOM 風險警告（必讀）

- **Evidently 執行可能耗用大量記憶體**。reference 或 current 資料過大時，易發生 OOM（Out of Memory）。
- **建議**：僅對**已下採樣或彙總後**的資料執行；或先以小型樣本確認腳本與環境正常。
- 本任務**不預先鎖死** downsampling / aggregation 策略；由執行者自行決定輸入資料規模與抽樣方式。
- 若遇 OOM，請縮小輸入檔或減少欄位／筆數後重試；並可參考 `doc/phase2_alert_runbook.md` 情境三處理步驟。

---

## 報告輸出位置

- **預設**：`out/evidently_reports/`（相對於**執行時之工作目錄**）；執行前會嘗試建立目錄。建議自 repo 根目錄執行以與文件一致。
- 可透過腳本參數 `--output-dir` 指定其他路徑。
- 可選：報告產出後可手動 sync 至 GCS 或內部儲存，本腳本不內建上傳。
- **路徑應為受控來源**：勿對未信任輸入或敏感路徑執行；輸出目錄勿指向系統或共用關鍵目錄。

---

## 如何執行

從 repo 根目錄執行：

```bash
python -m trainer.scripts.generate_evidently_report \
  --reference path/to/reference.parquet \
  --current path/to/current.parquet \
  --output-dir out/evidently_reports
```

- **--reference**：reference 資料檔路徑（CSV 或 Parquet）。
- **--current**：current 資料檔路徑（CSV 或 Parquet）。
- **--output-dir**：報告輸出目錄，預設 `out/evidently_reports`。

若未安裝 `evidently`，腳本會印出明確錯誤並 exit 1；請安裝後再執行（例如 `pip install evidently`，或使用已含 evidently 的 requirements.txt）。

---

## 手動驗證建議

1. 準備小檔：兩份欄位對齊的 CSV 或 Parquet（例如各數百列），分別作為 reference 與 current。
2. 執行上述指令，確認於 `--output-dir` 下產出 HTML 報告。
3. 以瀏覽器開啟 HTML，確認 drift 報告內容可讀。
4. （可選）卸載 evidently 後再執行，確認錯誤訊息清楚、exit code 非 0。

---

## 相關文件

- **Alert runbook（情境三）**：`doc/phase2_alert_runbook.md`
- **Message format**：`doc/phase2_alert_message_format.md`
- **Drift 調查模板與範例**：`doc/drift_investigation_template.md`、`doc/phase2_drift_investigation_example.md`（drift 確認後填寫正式紀錄用）
