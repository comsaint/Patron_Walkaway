# Task 3 — ClickHouse SQL 深度分析（占位）

> **狀態**：占位文件；逐條 SQL 的專項分析將於後續補齊。  
> **對齊計畫**：[.cursor/plans/PATCH_20260324.md](../.cursor/plans/PATCH_20260324.md) — Task 3 Phase 5。

## 交付物（規劃中）

對 **scorer**、**validator**（bet 路徑）及與 serving 相關之查詢，各開一節，每條 SQL 至少涵蓋：

- 用途與呼叫情境  
- 必要欄位與可刪減欄位  
- 時間窗、`FINAL`、CTE／窗口函式之成本與語意風險  
- 可瘦身建議與正確性注意事項  

**備註**：本文件以設計審視為主，不要求附實測或 `EXPLAIN`。

## 待分析查詢清單

（實作分析時將在此列出檔案與函式錨點，並逐條勾選完成。）

- _TBD_
