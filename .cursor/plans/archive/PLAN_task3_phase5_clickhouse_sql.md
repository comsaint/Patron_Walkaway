# Task 3 / Phase 5 Implementation Plan — ClickHouse SQL（Training / Scorer / Validator）

> 對齊：[`PATCH_20260324.md`](PATCH_20260324.md) Task 3 / Phase 5  
> 交付物主檔：[`doc/task3_clickhouse_sql_analysis.md`](../../doc/task3_clickhouse_sql_analysis.md)  
> 限制：資料來源與索引不由本專案管理，**不得提出或依賴 schema/index 變更**。

---

## 1) Scope（收斂）

本計畫只覆蓋以下優先範圍：

1. **Training code（最高）**：`trainer/training/trainer.py`
2. **Scorer（高）**：`trainer/serving/scorer.py`
3. **Validator（高）**：`trainer/serving/validator.py`

不在本輪主交付：

- `trainer/serving/status_server.py`（可記錄觀察，但不列入本輪完成門檻）

---

## 2) Goals

1. 完成逐條 SQL 設計分析（用途、窗口、`FINAL` 必要性、風險、可控優化）。
2. 所有建議僅限**可在應用層落地**（查詢重寫、窗口治理、查詢觀測、輪詢治理）。
3. 保持 SSOT 語義一致（available-time、防洩漏、DQ、train-serve parity）。

---

## 3) Guardrails

1. 不改 ClickHouse table schema / index / partition。
2. 不改 SSOT 定義的時間語義與 DQ 規則。
3. 若建議涉及效能改善，必須同時列出語義風險與回退方式。
4. 優先避免高 RAM 峰值（筆電環境），避免提出容易造成 OOM 的方案。

---

## 4) Workstreams

### WS-A（Training，P0）

目標 SQL：

- `trainer._load_data_chunk`：`t_bet FINAL` 查詢
- `trainer._load_data_chunk`：`t_session` CTE 去重查詢（no FINAL）
- `trainer` profile fallback：chunked-IN 查詢

交付：

- 每條 SQL 的用途、窗口語義、成本風險、可控優化（不改 index）
- `FINAL` 必要性審核建議（僅設計層，不直接改）

### WS-B（Scorer，P1）

目標 SQL：

- `scorer.fetch_recent_data` bets/sessions 兩條主查詢

交付：

- 每輪輪詢成本風險與窗口治理建議
- `FINAL` 保留/審核條件（含驗證守門）

### WS-C（Validator，P1）

目標 SQL：

- `fetch_bets_by_canonical_id`（`FINAL` + chunk IN）
- `fetch_sessions_by_canonical_id`（CTE 去重）

交付：

- chunk/排序策略在不改索引下的調整方向
- 驗證正確性守門（避免 false MISS/TP 漂移）

---

## 5) Implementation Steps（文件落地步驟）

1. 建立 SQL inventory（檔案/函式/查詢目的）。
2. 逐條補齊五欄：
   - Purpose
   - Time window & availability semantics
   - Cost risk（含 `FINAL`）
   - Optimization options（no index change）
   - Safety / rollback note
3. 產出「立即可落地優先序」與「驗收門檻」。
4. 在 `PATCH_20260324.md` 同步狀態與剩餘事項。

---

## 6) DoD（Definition of Done）

1. `doc/task3_clickhouse_sql_analysis.md` 已覆蓋 Training/Scorer/Validator 三類路徑主要 SQL。
2. 每條 SQL 都有「不改索引」前提下的可行建議與風險說明。
3. 文件含明確優先序（Training first）與驗收建議。
4. `PATCH_20260324.md` 已更新 Phase 5 狀態與引用本計畫。

---

## 7) Remaining Items（本計畫內）

1. 補上每條 SQL 的實測欄位（rows/latency/frequency/FINAL_used）以完成「設計 + 證據」閉環。
2. 視需要把 status_server 分析補入附錄（非 P0）。

---

## 8) Rollback

本計畫目前屬文件工作；若需回退，還原以下檔案即可：

- `.cursor/plans/PLAN_task3_phase5_clickhouse_sql.md`
- `.cursor/plans/PATCH_20260324.md`
- `doc/task3_clickhouse_sql_analysis.md`
