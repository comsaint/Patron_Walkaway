# Phase 2 規劃草稿（點子備忘）

> 重點：**穩定、可維護的部署**，而非效能優化。以下為待擴充的點子列表。

## 方向

| # | 方向 | 點子 | 備註 |
|---|------|------|------|
| 2 | 告警與人因 | Human-oriented reason for alerts |  |
| 7 | 告警與人因 | 告警與 runbook | 誰收警、收到後怎麼處理（搭配 #2） |
| 6 | 可追溯性 | Artifact 與 pipeline 溯源 | 哪份 code/data 產出哪個 model；rollback、審計用 |
| 10 | 部署 | 新模型 staged rollout（canary / 比例流量） | 先打一部份再全量，降低一次換錯風險 |
| 1 | 模型監控 | 模型監控（model versioning、drift detection 等） |  |
| 4 | 評估 | Run-level / Macro-by-run 評估（DEC-012） | 以 run 為單位的評估口徑 |
| 3 | 特徵 | Leverage game table | Track Human `table_hc` 啟用（Phase 1 未啟用） |
| 8 | 維運 | 設定與閾值管理（config / feature flag、model version 切換） | 閾值或模型版本可配置、可快速回滾 |
| 9 | 穩定性 | Health / SLA 定義與監控 | 明確定義「正常」、延遲與可用率 |
| 5 | 資料品質 | 資料品質監控（DQ dashboard / 定期檢查） | 輸入 null、schema、volume 等，問題早發現 |
|  |  | （待補充） |  |
