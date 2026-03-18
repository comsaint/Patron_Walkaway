# Phase 2 P1.2/P1.3：Alert Message Format

> Human-oriented 告警或異常訊息應包含的欄位與建議格式。  
> 依據：`.cursor/plans/PLAN_phase2_p0_p1.md` T7。供 runbook（`doc/phase2_alert_runbook.md`）與未來 Slack/email 等傳遞共用。

---

## 原則

- 訊息以**人可快速判斷來源、嚴重度與下一步**為目標。
- Phase 2 不實作傳遞管道；本文件定義**內容格式**，實作傳遞時可依此組裝 payload。

---

## 建議欄位

| 欄位 | 說明 | 範例 |
|------|------|------|
| **source** | 來源元件 | `scorer` / `export` / `validator` / `evidently` |
| **severity** | 嚴重度 | `info` / `warning` / `error` / `critical` |
| **ts** | 事件時間（ISO 8601，建議含時區） | `2026-03-18T12:00:00+08:00` |
| **summary** | 一句話摘要 | `Export failed: MLflow unreachable` |
| **model_version** | 若與模型相關 | `20260318-120000-abc1234` 或空 |
| **detail** | 可選詳細說明或 stack trace | 簡短錯誤訊息或 log 片段；**僅放已脫敏**之內容，勿放入密碼、API token、完整路徑或 PII；若需完整 log 以 `link` 指向內部 log 系統 |
| **action_hint** | 可選建議動作 | `Check MLFLOW_TRACKING_URI and retry export` |
| **link** | 可選連結（runbook、report、MLflow run） | URL 或 doc 路徑 |

---

## 範例（僅供格式參考）

```json
{
  "source": "export",
  "severity": "error",
  "ts": "2026-03-18T12:05:00+08:00",
  "summary": "Export failed: MLflow unreachable",
  "model_version": "",
  "detail": "ConnectionRefusedError: [Errno 111] Connection refused",
  "action_hint": "See doc/phase2_alert_runbook.md § 情境一",
  "link": ""
}
```

```json
{
  "source": "validator",
  "severity": "warning",
  "ts": "2026-03-18T12:10:00+08:00",
  "summary": "Validator precision below threshold",
  "model_version": "20260318-120000-abc1234",
  "detail": "last_1h precision=0.62, threshold=0.70",
  "action_hint": "Check doc/phase2_alert_runbook.md § 情境二",
  "link": ""
}
```

---

## 嚴重度建議

僅供參考，實際由實作與營運約定：scorer/export 無法寫入為 **error**；validator precision 低於閾值為 **warning**；drift 報告異常為 **warning** 或 **info**；**critical** 保留給服務完全不可用。

---

## 與 Runbook 的對應

- **source** 對應 runbook 中的「來源」與「誰看」。
- **action_hint** 可指向 `doc/phase2_alert_runbook.md` 的章節或步驟。
- **link** 可填 MLflow run URL、Evidently 報告路徑或內部 dashboard 連結。

---

## 手動驗證建議

1. 依本格式組一則 scorer / export / validator 範例訊息，確認欄位足以讓維運判斷下一步。
2. 與 `doc/phase2_alert_runbook.md` 對照，確認「情境 → 查證位置 → 建議動作」可與訊息中的 `action_hint` 或 `link` 銜接。
