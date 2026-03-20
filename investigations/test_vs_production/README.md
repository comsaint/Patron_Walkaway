# Test vs Production Investigation Workspace

本資料夾用於集中管理「test vs production 性能落差」調查資產，目標是讓調查可重現、可審計、可交接。

## Scope

- 對應文件：`.cursor/plans/INVESTIGATION_PLAN_TEST_VS_PRODUCTION.md`
- 調查主軸：R1–R9（閾值、口徑、label parity、資料 parity、漂移、可觀測性）

## Folder Layout

- `checks/`: 只讀檢查腳本（production preflight、快照採集）
- `snapshots/`: 每次 production 採樣輸出（不可覆蓋，僅新增）
- `analysis/`: 依根因編號整理分析記錄（R1~R9）
- `sql/`: 可重複執行的查詢樣板
- `reports/`: 調查結案報告與摘要

## Operating Rules

1. 每次採樣建立新目錄：`snapshots/prod_YYYYMMDD_HHMM/`
2. 禁止覆蓋既有快照檔（保留證據鏈）
3. 所有結論需可追溯到快照、SQL 或腳本輸出
4. 優先以小批次資料驗證，避免一次載入大資料導致高記憶體壓力
