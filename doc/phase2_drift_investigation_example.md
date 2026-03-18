# Drift 調查報告（範例）

> 本檔案為依 `doc/drift_investigation_template.md` 填寫的**範例**，使用 mock／dry-run 情境，供首次使用模板時參考。實際調查請另存新檔並依模板填寫。

---

## trigger

Evidently drift 報告顯示特徵 `avg_bet_size_30d` 的 PSI 為 0.28，超過內部暫定閾值 0.25。Validator 當週 precision@recall=0.01 略降但未達告警閾值。

---

## timeframe

- **調查區間**：production 進場資料 2026-03-01 ~ 2026-03-07（HKT）。
- **報告撰寫日**：2026-03-10。

---

## model_version

`walkaway_ml_v20260301`（artifact 目錄對應 commit abc123；MLflow run ID: xxx）。

---

## evidence used

- Evidently Data Drift 報告：`out/evidently_drift_20260308.html`。
- Prediction log 抽樣（同區間）：`prediction_log.db` 查詢結果匯出。
- `training_metrics.json`（該 model_version 對應之訓練階段）：Precision@Recall=0.01 與 backtest 一致。
- Validator verdict 匯出（同區間）：MATCH/MISS/PENDING 計數。

---

## hypotheses

- [x] data drift（輸入分佈）：`avg_bet_size_30d` 在 production 近期有明顯右偏。
- [ ] Data quality 異常（缺值、重複、上游錯誤）。
- [ ] concept drift（X→Y 關係改變）。
- [ ] validator 與 hold-out 評估口徑差異。
- [ ] training–serving skew（同 key 特徵計算不一致）。

---

## checks performed

1. 比對 Evidently 報告中 `avg_bet_size_30d` 的 reference（訓練期）與 current（調查區間）分布：current 中位數上升約 15%，高尾拉長，與近期活動檔期與高額桌開桌數增加一致。
2. 檢查 training–serving 特徵計算：以 `python -m trainer.scripts.check_training_serving_skew`（見 `doc/phase2_skew_check_runbook.md`）對同批 id 比對，無不一致欄位。
3. 查 Validator verdict：該區間 PENDING 比例正常，無異常 MATCH/MISS 偏斜。
4. Slice 分析：依時段與桌型分組，高額桌比例上升可解釋大部分分布變化。

結論：**What changed?** 進場資料中高額桌與大注比例上升。**When?** 約 3 月初起。**Why does it affect precision@recall=1%?** 目前影響有限；precision 略降可能為隨機波動，持續觀察一週。

---

## conclusion

根因歸屬為 **data drift（輸入分佈）**，與活動檔期及桌台組成變化一致，非資料品質或 training–serving skew。Concept drift 未見明顯證據。**不需重訓或回滾**；建議將本次區間納入下次重訓的資料窗口，並可選更新 reference 以反映新常態。

---

## recommended action

- 更新 reference：於下次 Evidently 執行時，可選以 3 月資料產出新 reference，供後續比對。
- 持續監控：一週內再產出一次 drift 報告，確認分布是否穩定。
- 無需依 `doc/phase2_model_rollback_runbook.md` 回滾。
