# Drift 調查報告（模板）

> 當 Validator precision 掉落或 Evidently drift 報告異常時，依本模板填寫調查紀錄並存於 `doc/`。  
> 依據：`.cursor/plans/PLAN_phase2_p0_p1.md` T10、`doc/phase2_p0_p1_implementation_plan.md` §3.5。  
> 填寫後請**另存新檔**（建議檔名含日期或事件識別，例如 `phase2_drift_investigation_YYYYMMDD_簡述.md`），勿覆蓋本模板。

---

## trigger

（觸發此次調查的事件：例如「Validator precision@recall=0.01 低於閾值」「Evidently 特徵 X 的 PSI > 0.25」、或手動 triage 決定啟動。）

---

## timeframe

（調查涵蓋的時間範圍：例如「2026-03-01 00:00 ~ 2026-03-15 23:59 HKT」「production 最近 7 天」；以及報告撰寫日期。）

---

## model_version

（當時 production 或受影響的 model_version / artifact 識別；可對照 `doc/phase2_provenance_query_runbook.md`。）

---

## evidence used

（本調查所依據的資料與報告：例如 prediction log、Evidently drift HTML、training_metrics.json、Validator verdict 匯出、MLflow run 連結、slice 查詢結果等；列出路徑或連結。路徑可採相對路徑或代碼化；**勿寫入敏感主機名、帳號或僅限內網的完整 URL**，若需留存請改存內部儲存或脫敏。）

---

## hypotheses

（列出的假說方向，可複選或新增：Data quality 異常、data drift（輸入分佈）、concept drift（X→Y 關係）、validator 與 hold-out 評估口徑差異、training–serving skew、其他。）

---

## checks performed

（實際執行的檢驗步驟：例如比對 reference vs current 分布、查 precision@recall 依時段/群組、檢查特徵對齊、比對 training_metrics 與線上表現等。回答：What changed? When? Why does it affect precision@recall=1%?）

---

## conclusion

（結論：根因歸屬、是否為已知變更、是否需重訓或回滾、是否僅需更新 reference／文件化。）

---

## recommended action

（建議後續動作：例如「更新 reference 並記錄」「依 `doc/phase2_model_rollback_runbook.md` 評估回滾」「排入下次重訓」「無需變更，持續監控」等。）
