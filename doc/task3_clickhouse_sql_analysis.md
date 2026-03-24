# Task 3 — ClickHouse SQL 設計分析

> **對齊計畫**：`.cursor/plans/PATCH_20260324.md` — Task 3 / Phase 5  
> **限制條件（明確）**：資料來源與索引不由本專案管理，**不提出 schema/index 變更方案**。  
> **分析範圍（收斂版）**：優先 `trainer/training/trainer.py`，其次 `trainer/serving/scorer.py`、`trainer/serving/validator.py`。  
> `trainer/serving/status_server.py` 僅保留低優先觀察，不列入本輪主交付。

---

## Rules Checked

以下依 ClickHouse best practices 規則檢核（只採本專案可控範圍）：

- `schema-pk-filter-on-orderby` - 有風險（需確認 filter 是否貼合既有 ORDER BY；本階段僅觀測，不改索引）
- `query-index-skipping-indices` - 部分風險（規則提醒有效，但本專案不可執行 index 變更）
- `query-join-filter-before` - 大致符合（本批查詢多為單表，無先 join 再 filter）
- `query-join-choose-algorithm` - N/A（本批查詢幾乎無 JOIN）
- `query-join-use-any` - N/A（本批查詢幾乎無 JOIN）

---

## 全域結論（先看）

1. **主要風險不在 JOIN，而在大表掃描 + `FINAL` + 輪詢頻率。**
2. 在「不能改 index」前提下，最佳優化手段是：
   - 收斂時間窗
   - 嚴格控制 `FINAL` 使用範圍
   - 減欄位、減排序、減重複查詢
   - 增加查詢級成本可觀測性（p50/p95、rows returned）
3. 所有優化都必須維持 SSOT 的防洩漏語義（available-time、window 邊界、DQ 規則）。

---

## SQL Inventory 與逐條分析（Narrowed Scope）

## A. Training 路徑（最高優先）

### A1. `trainer._load_data_chunk` — bets 查詢（`t_bet FINAL`）

**位置**
- `trainer/training/trainer.py`（ClickHouse data loading 區段）

**用途**
- 依時間窗口（`[window_start, extended_end]`）拉訓練用 bets。

**現況重點**
- 使用 `FROM {SOURCE_DB}.{TBET} FINAL`
- 與 SSOT 的 C1 延伸窗口語義對齊（延伸區間僅供標籤，不進訓練樣本）

**成本風險**
- Training 是最大資料量路徑，`FINAL` 在長時間窗或多 chunk 下成本最高。

**可控優化（不改 index）**
- 先以 chunk 級統計（rows/latency）量化 `FINAL` 成本占比。
- 保留 `FINAL` 的前提下，優先做欄位裁切與窗口尺寸治理。
- 本機 Parquet 路徑持續作為離線加速（不改語義）。

**語義護欄**
- 必須保持 SSOT 的 available-time、C1 延伸窗口、DQ 規則一致性。

---

### A2. `trainer._load_data_chunk` — sessions 查詢（CTE 去重，no FINAL）

**位置**
- `trainer/training/trainer.py`（ClickHouse data loading 區段）

**用途**
- 提供訓練特徵與 identity 相關 session 輔助資料。

**現況重點**
- 明確「No FINAL on t_session」
- FND-01 `ROW_NUMBER ... rn=1` 去重

**成本風險**
- CTE 去重 + 寬窗口在月度/週期訓練會累積為顯著成本。

**可控優化（不改 index）**
- 量測每 chunk sessions 查詢耗時與返回量，定位熱區月份。
- 保持 DQ 語義前提下縮減非必要欄位輸出。

**語義護欄**
- 不可破壞 FND-01/FND-02/FND-04 與 train-serve parity。

---

### A3. `trainer` fallback 查詢 — `player_profile` chunked-IN

**位置**
- `trainer/training/trainer.py`（player profile fallback path）

**用途**
- 當本地 profile artifact 不可用時，回退 ClickHouse 讀取 profile。

**現況重點**
- 以 `_IN_BATCH = 4000` 分批查詢，避免 oversized IN。

**成本風險**
- canonical_id 清單很大時，查詢次數多、總耗時拉長。

**可控優化（不改 index）**
- 保持 batch 分塊，並把 batch 尺寸納入可調參。
- 強化 fallback 觸發率觀測，避免常態走回退路徑。

**語義護欄**
- PIT/as-of 邏輯與 profile 欄位語義不可改變。

---

## B. Scorer 路徑（高優先）

### A1. `scorer.fetch_recent_data` — bets 查詢（`t_bet FINAL`）

**位置**
- `trainer/serving/scorer.py` `fetch_recent_data()` 內 `bets_query`

**用途**
- 每輪拉取 lookback 窗口內的 bets，供 `score_once` 特徵與打分。

**現況重點**
- 使用 `FROM ... t_bet FINAL`
- 時間窗：`payout_complete_dtm >= start AND <= bet_avail`
- 過濾：`wager > 0`、`player_id` 非空且非 placeholder

**成本風險**
- `FINAL` 可能放大讀取成本（CPU/記憶體/延遲）。
- 每輪輪詢都重拉視窗，若 lookback 偏大，易形成固定高成本。

**可控優化（不改 index）**
- 僅在語義必要時保留 `FINAL`；以可重現對照驗證 non-FINAL 是否等價。
- 儘量縮欄位（僅保留當輪必需欄位）。
- 強化窗口守門（已有 lookback cap；建議在 runbook 固定資料窗比較）。

**語義護欄**
- 不可破壞 SSOT 的 event-time/available-time 約束與 DQ 過濾語義。

---

### A2. `scorer.fetch_recent_data` — sessions 查詢（CTE 去重）

**位置**
- `trainer/serving/scorer.py` `fetch_recent_data()` 內 `session_query`

**用途**
- 提供 session-level 輔助欄位（含 canonical mapping 相關資訊來源）。

**現況重點**
- CTE `ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY lud_dtm DESC, __etl_insert_Dtm DESC)` + `rn=1`
- 過濾：`is_deleted=0`、`is_canceled=0`、`is_manual=0`
- 可得性窗：`session_avail_dtm <= sess_avail`

**成本風險**
- CTE 去重 + 較寬時間窗可能在高頻輪詢下成本明顯。

**可控優化（不改 index）**
- 確認是否有冗餘欄位可減少 select payload。
- 檢查時間窗是否可再縮（不影響特徵與 identity 語義前提下）。
- 避免每輪不必要的全量 mapping 重建（已有 artifact 路徑，維持優先使用）。

**語義護欄**
- FND-01/FND-02/FND-04 去重與過濾語義需保留。

---

## C. Validator 路徑（高優先）

### B1. `validator.fetch_bets_by_canonical_id`（`t_bet FINAL` + chunk IN）

**位置**
- `trainer/serving/validator.py` `fetch_bets_by_canonical_id()`

**用途**
- 驗證 alert 時，按 canonical 對應的 player_id 清單抓 bet 時間序列。

**現況重點**
- `player_id IN %(players)s`，分 chunk 查詢
- 使用 `FINAL`
- `ORDER BY player_id, payout_complete_dtm`

**成本風險**
- `FINAL` + 排序在大 chunk 下仍可能吃重。
- `IN` 清單大小若偏大，單輪 latency 可能拉長。

**可控優化（不改 index）**
- 維持 chunking，並將 chunk size 視為可調參（環境依賴）。
- 僅在必要保留排序；若後續程式可容忍，改最小必要排序。
- 對 chunk 查詢加 query 級耗時/rows 觀測，找出最適區間。

**語義護欄**
- 不能以快取/降載方式犧牲驗證正確性（避免 false MISS/TP 漂移）。

---

### B2. `validator.fetch_sessions_by_canonical_id`（`t_session` CTE 去重）

**位置**
- `trainer/serving/validator.py` `fetch_sessions_by_canonical_id()`

**用途**
- 目前主判決已不依賴 session verdict，但此函式仍存在於相容路徑。

**現況重點**
- 與 scorer 類似的去重 CTE + `rn=1`
- `player_id IN chunk`
- `(turnover > 0 OR num_games_with_wager > 0)`

**成本風險**
- 若未實際使用卻仍查詢，屬純浪費成本。

**可控優化（不改 index）**
- 維持現行「validator 主路徑停用 session 查詢」策略。
- 確保任何 fallback 路徑不會誤觸發高頻 session 查詢。

**語義護欄**
- 不可影響 `MATCH/MISS/PENDING` 既有語義。

---

## D. Status Server 路徑（低優先，延後）

### C1. `status_server.fetch_table_ids`（`SELECT DISTINCT table_id FROM t_session`）

**位置**
- `trainer/serving/status_server.py` `fetch_table_ids()`

**用途**
- 取得可用 table 清單供 layout / status 展示。

**成本風險**
- `DISTINCT` 無時間條件時可能掃描範圍過大。

**可控優化（不改 index）**
- 加入合理時間條件（若業務語義允許）。
- 對結果做短 TTL 快取，降低重複查詢。

---

### C2. `status_server.fetch_open_sessions_ch`（近期 open sessions）

**位置**
- `trainer/serving/status_server.py` `fetch_open_sessions_ch()`

**用途**
- 拉取近期未結束 session 做 occupancy 展示。

**現況重點**
- 已有 `session_start_dtm >= cutoff` 與 `SETTINGS max_execution_time/max_result_rows`

**成本風險**
- `DISTINCT` + 多欄位輸出仍可能偏重（特別是尖峰時段）。

**可控優化（不改 index）**
- 進一步縮欄位（只保留畫面必要資料）。
- 調整 polling 頻率與快取策略，避免 UI 頻繁全量重查。

---

## E. 立即可落地優先序（不改索引版，已收斂）

1. **Training SQL 證據化量測（必做）**
   - `trainer._load_data_chunk` bets/sessions 分開記錄 rows、耗時、是否 `FINAL`。
   - 先確認訓練路徑 top cost（通常是全專案最重 SQL）。

2. **Training 的 `t_bet FINAL` 必要性審核（高優先）**
   - 在固定窗口抽樣驗證 non-FINAL 對標籤/特徵影響。
   - 若語義要求不可移除，至少縮窗與減欄位。

3. **Scorer/Validator 的 `FINAL` 與 chunk 治理（高優先）**
   - scorer/validator 的 `t_bet FINAL` 與 chunk 查詢延遲做對照。
   - 以 alert/validation 行為一致性作為守門。

4. **status 路徑降載（次優先，可延後）**
   - `fetch_table_ids` 加條件或快取。
   - `fetch_open_sessions_ch` 減欄位 + 降頻。

5. **共通：query-level 觀測欄位標準化（中優先）**
   - 每條關鍵 SQL 記錄：rows、耗時、頻率、是否 `FINAL`。
   - 目標：先定位 top cost SQL，再做手術。

---

## F. 驗收與回歸建議

- 功能正確性：
  - scorer alert 集合不異常漂移
  - validator `MATCH/MISS/PENDING` 分布無異常
- 效能：
  - SQL 耗時 p95 下降（至少關鍵 top 1-2 查詢）
  - 不增加記憶體峰值（筆電環境重點）
- 防洩漏：
  - 持續符合 SSOT 時間可得性與窗口邊界語義

---

## G. 後續補件（本文件下一版）

下一版建議補上每條 SQL 的實測欄位：

- `query_id`
- `rows_returned_p50/p95`
- `latency_p50/p95`
- `call_frequency_per_min`
- `FINAL_used`（Y/N）
- `notes`（語義約束與回退條件）

---

## H. 調查追加（完整 SQL 盤點，2026-03-24）

本節為依 `PLAN_task3_phase5_clickhouse_sql.md` 執行的「不改程式」純調查結果，範圍限於：

- `trainer/training/trainer.py`
- `trainer/serving/scorer.py`
- `trainer/serving/validator.py`

### H1. Training SQL（`trainer/training/trainer.py`）

### H1-1. ClickHouse 主資料拉取：`load_clickhouse_data`（P0）

- **Query T-CH-01（bets）**
  - `FROM {SOURCE_DB}.{TBET} FINAL`
  - 條件：`payout_complete_dtm` 時間窗（含 `HISTORY_BUFFER_DAYS`）、`wager > 0`、`player_id` 合法
  - 目的：訓練 chunk 的 bet 主輸入
  - **風險**：`FINAL` + 大時間窗是 training 路徑第一熱點候選
  - **優先級**：P0（最高）

- **Query T-CH-02（sessions）**
  - `WITH deduped AS (...) ROW_NUMBER() ... rn=1`，`FROM {SOURCE_DB}.{TSESSION}`（no `FINAL`）
  - 條件：`session_start_dtm` 視窗 ±1 day、`is_deleted/is_canceled/is_manual` 過濾
  - 目的：訓練所需 session 輔助欄位與 DQ 對齊
  - **風險**：CTE 去重成本在長窗下可觀
  - **優先級**：P0

### H1-2. Player profile fallback ClickHouse 查詢（P1）

位置：`_load_profile_for_training(...)` fallback path（local parquet 不可用時）

- **Query T-CH-03（no filter）**
  - `SELECT * FROM {SOURCE_DB}.{TPROFILE} WHERE snapshot_dtm between [lo, hi]`
  - 觸發：`canonical_ids is None`

- **Query T-CH-04（small IN）**
  - 同上 + `AND canonical_id IN (...)`（單批）
  - 觸發：`len(canonical_ids) <= _IN_BATCH`

- **Query T-CH-05（chunked IN）**
  - 同上 + 分批 `IN (...)`，`pd.concat` 合併
  - 觸發：`len(canonical_ids) > _IN_BATCH`

**共同風險**
- `SELECT *` 可能導致不必要 payload；大批 canonical_id 時查詢次數高。
- 但此路徑屬 fallback，應先控制「觸發率」而非直接重寫。

**優先級**
- P1（次於主資料拉取）

### H1-3. 非 ClickHouse SQL（記錄，不列入本輪主優化）

- `build_canonical_mapping_from_local_sessions(...)` 使用 DuckDB SQL（`read_parquet` + CTE dedup）
- Step7 parquet split/describe/copy 一系列 DuckDB SQL

說明：這些屬離線本地處理，不在本輪 ClickHouse Phase 5 主優先，但需保留作語義對照。

---

### H2. Scorer SQL（`trainer/serving/scorer.py`）

### H2-1. ClickHouse 查詢：`fetch_recent_data`（P1）

- **Query S-CH-01（bets）**
  - `FROM {config.SOURCE_DB}.{config.TBET} FINAL`
  - 條件：`payout_complete_dtm` 視窗、`wager > 0`、`player_id` 合法
  - 目的：每輪推論資料來源
  - **風險**：`FINAL` + polling 下重覆掃描
  - **優先級**：P1

- **Query S-CH-02（sessions）**
  - `WITH deduped ... ROW_NUMBER() ... rn=1`
  - `FROM {config.SOURCE_DB}.{config.TSESSION}`（no `FINAL`）
  - 目的：session 輔助欄位與 canonical mapping 支援
  - **風險**：CTE 去重在高頻輪詢可能吃重
  - **優先級**：P1

### H2-2. 非 ClickHouse SQL（記錄）

- scorer 其餘多為 SQLite（state/prediction_log/alerts），非本節主題。

---

### H3. Validator SQL（`trainer/serving/validator.py`）

### H3-1. ClickHouse 查詢（P1）

- **Query V-CH-01：`fetch_bets_by_canonical_id`**
  - `FROM {config.SOURCE_DB}.{config.TBET} FINAL`
  - 條件：`player_id IN chunk` + bet 時間窗 + DQ 基本過濾
  - `ORDER BY player_id, payout_complete_dtm`
  - **風險**：`FINAL` + 排序 + 多批 IN 查詢
  - **優先級**：P1

- **Query V-CH-02：`fetch_sessions_by_canonical_id`**
  - `WITH deduped ... ROW_NUMBER() ... rn=1`
  - `FROM {config.SOURCE_DB}.{config.TSESSION}`（no `FINAL`）
  - `ORDER BY player_id, session_avail_dtm, session_end_dtm`
  - **風險**：若誤觸發高頻路徑會有額外成本
  - **優先級**：P2（目前主判決路徑已盡量不依賴 session cache）

### H3-2. 非 ClickHouse SQL（記錄）

- validator 另有 SQLite 讀寫（alerts/validation_results/processed_alerts），不在本輪 ClickHouse 主優化。

---

### H4. 優先序重申（依本次「完整盤點」）

1. **P0 Training 主拉取（T-CH-01, T-CH-02）**
2. **P1 Serving 主路徑（S-CH-01, S-CH-02, V-CH-01）**
3. **P1 Training fallback（T-CH-03/04/05）**
4. **P2 Validator session 查詢（V-CH-02）**

---

### H5. 目前缺口（待後續補數據）

本次完成的是「查詢全盤點 + 風險分級」，尚未包含實測統計。  
下一步需對以上 query_id 追加：

- `rows_returned_p50/p95`
- `latency_p50/p95`
- `call_frequency_per_min`
- `FINAL_used`
