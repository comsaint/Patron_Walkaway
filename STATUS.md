# Code Review: ClickHouse 臨時表方案 (load_player_profile)

## 1. [Bug/資源洩漏] ClickHouse Session Client 未正確關閉 (Connection Leak)
*   **問題描述**：每次 `canonical_ids` 超過 5,000 時，都會實例化一個全新的 `session_client` (`_cc.get_client(...)`)，但在完成查詢並回傳 `df` 後，並沒有呼叫 `.close()`。這會導致背後的 HTTP 連線池 (urllib3) 與對應的 socket 沒有及時釋放，頻繁呼叫可能會導致 Connection Exhaustion 或資源洩漏。
*   **具體修改建議**：使用 `try...finally` 確保 client 關閉。
    ```python
    session_client = _cc.get_client(..., session_id=session_id)
    try:
        # ... CREATE, INSERT, SELECT ...
        df = session_client.query_df(...)
    finally:
        session_client.close()
    ```
*   **希望新增的測試**：新增一個壓力測試 (Stress Test)，迴圈呼叫 `load_player_profile` 50 次，並監聽 OS 級別的 TCP socket (或檢查 python `clickhouse_connect` 的連線池狀態)，斷言沒有未關閉的連線殘留。

## 2. [效能/Bug] JOIN 條件的型別不匹配 (Type Mismatch)
*   **問題描述**：臨時表宣告為 `CREATE TEMPORARY TABLE ... (canonical_id String)`，並與 `TPROFILE` 進行 `INNER JOIN`。如果生產環境中 `TPROFILE.canonical_id` 的實體型別是數值型別（如 `Int64` 或 `UInt64`），字串對數值的 JOIN 不僅可能會報錯，在 ClickHouse 中更會導致放棄使用主鍵索引 (Primary Key Index)，直接退化成全表掃描 (Full Table Scan)，嚴重拖垮 DB 效能。
*   **具體修改建議**：確認目標資料表 (`{SOURCE_DB}.{TPROFILE}`) 的 DDL。如果 DB 中是 `Int64`，請將臨時表改為 `(canonical_id Int64)` 並在 Python 端把 `cid` 轉為 `int`；如果是 `String` 則維持現狀。若要兼容，可強制在 JOIN 兩側做 `CAST`，但最好兩邊 DDL 保持一致。
*   **希望新增的測試**：整合測試中建立一個模擬的 ClickHouse DB，故意使用 `Int64` 與 `String` 的主表，確認 `load_player_profile` 會不會因為型別而引發 Exception，並利用 `EXPLAIN` 語法驗證是否有命中主鍵索引 (`granules scanned` 不應是全表)。

## 3. [邊界條件] 臨時表的資料寫入失敗導致後續崩潰
*   **問題描述**：在執行 `for _i in range(0, len(_cid_list), _INSERT_BATCH):` 的過程中，如果某一梯次的網路斷線或逾時 (Timeout)，拋出 Exception，該 session 的狀態會變成中斷。因為共用一個 try-except，它會被最外層捕獲，回傳 `None`，降級為 `NaN` 特徵。這雖然符合你的 Graceful Degradation 設計，但可能沒有留下足夠詳細的 Insert 失敗 log 來協助除錯。
*   **具體修改建議**：可以為臨時表的 `INSERT` 階段增加專屬的 `try...except` 或加上更詳細的 log：
    ```python
    try:
        session_client.insert(...)
    except Exception as e:
        logger.error(f"Failed to insert batch {_i} to temporary table: {e}")
        raise # 交給外層 catch 處理並回傳 None
    ```
*   **希望新增的測試**：Mock `session_client.insert` 讓它在第二批次時拋出 `ReadTimeout` 例外，斷言系統能優雅捕獲、不中斷訓練，並且能夠正確在日誌中記錄是「Insert 階段」發生的錯誤。

## 4. [效能微調] Insert 批次與傳輸效率
*   **問題描述**：目前每次塞 `50,000` 筆，針對 `323,608` 的量需要分約 7 趟 HTTP 請求。對於 ClickHouse 來說，原生插入 `100,000` 甚至 `500,000` 行單一字串欄位是毫不費力的 (幾 MB 而已)。
*   **具體修改建議**：將 `_INSERT_BATCH` 提升至 `200_000` 或 `500_000`，這樣 32 萬筆資料只需 1-2 次 request 就能寫完，降低 Round-trip time (RTT)。
*   **希望新增的測試**：提供包含 `1,000,000` 個隨機 ID 的大型 Mock List，測試函數不會因為單次批次過大而觸發 ClickHouse HTTP payload limit 或發生 OOM。

---

## 本次已新增：最小可重現測試 / lint-like 規則（tests-only）

新增檔案：

- `tests/test_trainer_review_risks_temp_table.py`

覆蓋項目（對應上述 4 個風險）：

1. `test_session_client_is_closed_in_finally`
   - 規則：`load_player_profile` 應包含 `session_client.close()` 且以 `finally` 保證釋放資源。
   - 目前狀態：`expectedFailure`（尚未修 production code）。

2. `test_join_type_rule_requires_non_string_temp_id_or_explicit_cast`
   - 規則：temp table 的 `canonical_id` 應使用 `Int/UInt`，或在 JOIN 條件顯式 `CAST(...)`。
   - 目前狀態：`expectedFailure`。

3. `test_insert_stage_has_specific_error_logging`
   - 規則：`session_client.insert(...)` 階段失敗時應有明確 `logger.error(...insert...)` 訊息。
   - 目前狀態：`expectedFailure`。

4. `test_insert_batch_size_rule_for_large_id_lists`
   - 規則：`_INSERT_BATCH` guardrail 門檻至少 `200_000`（降低 round trips）。
   - 目前狀態：`expectedFailure`。

另外加了 1 個 sanity guard：

- `test_temp_table_strategy_exists_for_large_canonical_ids`
  - 確認大 ID 清單路徑仍有 temp table + JOIN 策略，避免回歸。

### 執行方式

僅跑本次新增測試：

```bash
python -m pytest -q tests/test_trainer_review_risks_temp_table.py
```

本機實跑結果（初始建立時）：

- `1 passed, 4 xfailed`

---

## 實作修補輪（load_player_profile + tests）

### 修改清單

**`trainer/trainer.py`（`load_player_profile` ClickHouse 路徑）**

| # | Risk | 修改內容 |
|---|------|---------|
| 1 | 資源洩漏 | `session_client` 的建立後以 `try...finally: session_client.close()` 包裹，確保無論成功/例外都釋放連線 |
| 2 | JOIN 型別不匹配 | JOIN 條件由 `p.canonical_id = t.canonical_id` 改為 `CAST(p.canonical_id AS String) = t.canonical_id`，防止隱式型別轉換導致索引失效 |
| 3 | Insert 日誌不足 | 為 Insert 迴圈每梯次加上獨立 `try/except`，失敗時發出 `logger.error("temp-table insert batch %d failed: %s", _i, _ins_exc)` 再 re-raise |
| 4 | Insert 批次偏小 | `_INSERT_BATCH` 由 `50_000` 提升至 `200_000`，323k 筆 ID 從 7 趟降至 2 趟 HTTP round-trip |

**`tests/test_trainer_review_risks_temp_table.py`（測試本身的兩處 bug）**

| # | Bug | 修改內容 |
|---|-----|---------|
| A | Stale markers | 移除 4 個 `@unittest.expectedFailure` decorator（實作已修復，decorator 已過期） |
| B | Regex 不支援 `_` 分隔符 | `_extract_insert_batch_value` 的 regex 由 `\d+` 改為 `[\d_]+`，並在轉 `int` 前去除底線，以正確解析 `200_000` |

### 執行結果（修補後）

```
python -m pytest -q tests/test_trainer_review_risks_temp_table.py
```
→ `5 passed in 0.22s`

```
python -m pytest tests/ -q --tb=short
```
→ `531 passed, 1 skipped in 22.69s`（全套件零失敗）

---

## 換策略輪：Temp-table → 分批 IN 查詢（Chunked IN）

**背景**：生產環境 ClickHouse 帳號沒有 `CREATE TEMPORARY TABLE` 權限，臨時表方案無法使用。

### 改了哪些檔

| 檔案 | 動作 | 說明 |
|------|------|------|
| `trainer/trainer.py` | 改寫 `load_player_profile` ClickHouse 路徑 | 完整移除 temp-table 分支（session client、CREATE TABLE、INSERT、INNER JOIN）；改用三路分支：(1) 無 ID 篩選、(2) ≤ 4,000 筆單一查詢、(3) > 4,000 筆分批 IN 查詢 + `pd.concat` |
| `tests/test_trainer_review_risks_temp_table.py` | 全部改寫為 chunked-IN guardrail | 移除所有 temp-table 相關斷言，改為驗證：策略標記存在（`_IN_BATCH`/`pd.concat`/無 temp-table）、使用共享 client、無 INNER JOIN、批次大小在安全範圍（1,000–10,000）、三路分支邏輯、大列表路徑有日誌輸出 |

### 設計要點

- **`_IN_BATCH = 4_000`**：每批約 60–80 KB（ID 平均 15–20 字元），遠低於 ClickHouse 256 KB 的 `max_query_size`。
- 323,608 筆 ID 分成約 **81 批**，多次 round-trip 但不需要任何建表權限。
- 三路分支確保零 ID（無篩選）/ 小列表（單次查詢）/ 大列表（分批 concat）都有明確路徑，不會退化成全表掃描。

### 手動驗證方式

```bash
# 1. 只跑本次改動的 guardrail 測試
python -m pytest tests/test_trainer_review_risks_temp_table.py -v

# 2. 跑全套測試確認無回歸
python -m pytest tests/ -q --tb=short

# 3. 生產環境驗收（需 ClickHouse 連線）
python -m trainer.trainer --days 30
# 預期日誌出現：
# player_profile: XXXXX canonical_ids — chunked IN queries (batch=4000)
# player_profile: XXXXXX rows loaded from ClickHouse
```

### 測試結果

```
python -m pytest tests/test_trainer_review_risks_temp_table.py -v
```
→ `6 passed in 0.32s`

```
python -m pytest tests/ -q --tb=short
```
→ `532 passed, 1 skipped in 26.89s`（全套件零失敗）

### 下一步建議

1. **生產環境驗收**：跑一次 `python -m trainer.trainer --days 30`，確認日誌出現 `chunked IN queries` 並成功載入 profile rows，不再出現 `max_query_size exceeded` 警告。
2. **調整 `_IN_BATCH`（如需要）**：若 canonical_id 平均長度較短（例如全數字 8 碼），可適當調大至 6,000–8,000 以減少 round-trip 次數；若較長則保守一點。
3. **效能監控**：81 次 ClickHouse 查詢理論上需要幾秒到幾十秒（依網路延遲），若太慢可考慮在 Python 側做 `concurrent.futures.ThreadPoolExecutor` 並行發 batch 請求（但需確認 ClickHouse 帳號的並行查詢配額）。

---

## Chunked-IN Review（2026-03-06）

審查範圍：`trainer/trainer.py` 的 `load_player_profile` ClickHouse 路徑（chunked-IN 策略）與對應的 `tests/test_trainer_review_risks_temp_table.py`。

### R-CIN-1（中，正確性）— 大量分批查詢後 concat 的排序不保證全域正確

**問題**：每個 batch 內部各自帶有 `ORDER BY canonical_id, snapshot_dtm`，但 `pd.concat(_parts, ignore_index=True)` 後，不同 batch 的結果交錯排列。下游 `join_player_profile` 呼叫 `pd.merge_asof`，需要 left/right 兩側皆按 join key 排序。如果 `profile_df` 到達 merge 時沒有全域排序，`merge_asof` 會拋出 `MergeError: left keys must be sorted`，或者（若它碰巧已排序）在邊界處產生不正確的 as-of 匹配。

以 323,608 個 ID 分成 81 批為例：batch 0 拉回 canonical_id `"A001"–"A999"` 的所有 snapshots，batch 1 拉回 `"A100"–"B500"` — 兩批的 canonical_id 字典序有交叉，concat 後不會是全域排序的。

**具體修改建議**：在 `pd.concat` 之後加一行排序：
```python
df = pd.concat(_parts, ignore_index=True)
df.sort_values(["canonical_id", "snapshot_dtm"], inplace=True)
```
或者移除個別 batch 的 `ORDER BY`（讓 ClickHouse 省掉排序），只在最後 concat 完做一次 Python 排序。

**希望新增的測試**：`test_chunked_in_result_is_globally_sorted` — mock `client.query_df` 使其依 batch 回傳已內部排序但跨 batch 交錯的 DataFrame（例如 batch 0 包含 canonical_id `"C"`, `"A"`；batch 1 包含 `"B"`, `"D"`），驗證最終 `df` 的 `canonical_id` + `snapshot_dtm` 是全域遞增。

---

### R-CIN-2（中，Bug 邊界條件）— 空 batch 結果導致 `pd.concat([])` 拋出 ValueError

**問題**：在大列表路徑中，若某一批次的所有 canonical_id 在 profile 表中都不存在（例如新註冊玩家），`client.query_df` 回傳空 DataFrame。如果**所有**批次都回傳空 DataFrame，`_parts` 是一個全空 DataFrame 的 list，`pd.concat(_parts, ignore_index=True)` 會回傳空 DataFrame（這沒問題）。但如果 `_parts` 完全是空的（例如 `_cid_list` 剛好是空的、但由於某些邊界原因仍走到 `else` 分支），`pd.concat([])` 會拋出 `ValueError: No objects to concatenate`。

目前 `_cid_list` 的判斷 `len(_cid_list) <= _IN_BATCH` 確保了 `_cid_list` 非空才進 else 分支，且 `range(0, len(_cid_list), _IN_BATCH)` 至少會產出一個 batch，所以**當前不會觸發**。但這是隱性依賴於控制流的正確性，沒有顯式防護。

**具體修改建議**：在 concat 前加防護：
```python
df = pd.concat(_parts, ignore_index=True) if _parts else pd.DataFrame()
```

**希望新增的測試**：`test_chunked_in_all_batches_empty` — mock `client.query_df` 始終回傳空 DataFrame，傳入 5,000+ 個不存在的 canonical_id，驗證函式回傳 `None`（而非拋出例外）。

---

### R-CIN-3（中，效能）— 81 次序列 HTTP 請求的延遲累積

**問題**：323,608 個 ID ÷ 4,000 = 81 次序列 HTTP 請求。每次 ClickHouse HTTP round-trip（query parse + execute + response serialize）約 0.2–1s（取決於 profile 表大小和網路延遲），81 次累計約 16–81 秒。這遠比原來的單次查詢（或 temp-table + 單次 JOIN）慢很多。

日誌顯示 Step 5 之前的 Step 3（identity mapping）花了 1457 秒，所以額外幾十秒可能可接受。但如果 profile 表很大或網路慢，Step 5 會成為新瓶頸。

**具體修改建議**：
1. **短期**：增加進度日誌，讓使用者知道進度。例如每 10 批 log 一次：
```python
if (_i // _IN_BATCH) % 10 == 0:
    logger.info("player_profile: batch %d/%d", _i // _IN_BATCH + 1,
                (len(_cid_list) + _IN_BATCH - 1) // _IN_BATCH)
```
2. **中期**：考慮使用 `concurrent.futures.ThreadPoolExecutor(max_workers=4)` 並行發送 batch 請求，讓 81 次降到約 21 次的等待時間（~4× 加速）。需確認 `clickhouse_connect` client 的 thread safety（其底層是 `urllib3`，預設 thread-safe）。
3. **長期**：如果 DBA 未來開放 `max_query_size` 調大（例如到 50 MB），可以回退到單次查詢，完全消除分批。

**希望新增的測試**：無需自動化測試（效能類）；可用手動計時在生產環境驗證。

---

### R-CIN-4（低，穩健性）— 單一 batch 查詢失敗會中斷整個函式

**問題**：如果 81 次查詢中的第 50 次因為網路 timeout 而失敗，整個 `load_player_profile` 會被最外層 `except Exception` 捕獲，回傳 `None`，丟棄前 49 批的有效結果。這與 graceful degradation 策略一致（profile 全降為 NaN），但可能浪費了已經拉回的大量數據。

**具體修改建議**：可加入 per-batch 的 `try/except`，失敗時 log error 但繼續後續 batch，只是缺少部分 canonical_id 的 profile：
```python
for _i in range(0, len(_cid_list), _IN_BATCH):
    _batch = _cid_list[_i: _i + _IN_BATCH]
    try:
        _part = client.query_df(...)
        _parts.append(_part)
    except Exception as _exc:
        logger.error("player_profile batch %d failed: %s", _i // _IN_BATCH, _exc)
```
這樣即使部分 batch 失敗，仍能拿到大部分 profile 數據。下游 `join_player_profile` 對缺少 profile 的 canonical_id 會自動 zero-fill。

**希望新增的測試**：`test_chunked_in_partial_batch_failure` — mock `client.query_df` 讓第 2 批拋出 `ConnectionError`，驗證：(1) 函式仍回傳非 None（前 1 批的資料保留），(2) 日誌中出現 `batch.*failed` 的 error 訊息。

---

### R-CIN-5（低，安全性）— `_base_query` 使用 Python `.format()` 注入 `{cid_clause}`

**問題**：`_base_query` 使用 Python 的 `str.format(cid_clause=...)` 來動態插入 WHERE 子句。`cid_clause` 的值是硬寫在程式碼中的常數字串（`""` 或 `"AND canonical_id IN %(canonical_ids)s"`），不存在 injection 風險。但這種 pattern 容易被未來開發者誤用——如果有人把使用者輸入傳入 `cid_clause`，就會有 SQL injection 風險。

此外，`{cid_clause}` 使用的是 Python f-string 的 `{...}` 語法，但 `_base_query` 本身已是 f-string（第 632 行 `f"""`），意味著 `{{cid_clause}}` 會在 f-string 階段被解析為字面 `{cid_clause}`，然後在 `.format()` 階段被替換。這**目前是正確的**，但雙層模板容易造成維護者混淆。

**具體修改建議**：改為兩個明確的 query 字串常數，而非動態插入：
```python
_query_no_filter = f"""
    SELECT canonical_id, snapshot_dtm, {profile_cols_sql}
    FROM {SOURCE_DB}.{TPROFILE}
    WHERE snapshot_dtm >= %(snap_lo)s AND snapshot_dtm <= %(snap_hi)s
    ORDER BY canonical_id, snapshot_dtm
"""
_query_with_filter = f"""
    SELECT canonical_id, snapshot_dtm, {profile_cols_sql}
    FROM {SOURCE_DB}.{TPROFILE}
    WHERE snapshot_dtm >= %(snap_lo)s AND snapshot_dtm <= %(snap_hi)s
      AND canonical_id IN %(canonical_ids)s
    ORDER BY canonical_id, snapshot_dtm
"""
```
消除 `.format()` 層，維護者不需理解雙層模板語義。

**希望新增的測試**：source guard — `assertNotIn(".format(", src)` 確保不再有動態 format 注入。

---

### 問題優先度摘要

| # | 嚴重度 | 類別 | 問題 | 風險 |
|---|--------|------|------|------|
| R-CIN-1 | 中 | 正確性 | concat 後缺全域排序，`merge_asof` 可能報錯或匹配錯誤 | 生產環境會直接拋出 MergeError |
| R-CIN-2 | 中 | 邊界條件 | 空 batch 的 concat 防護缺失 | 當前不觸發但無顯式防護 |
| R-CIN-3 | 中 | 效能 | 81 次序列請求累積延遲 | Step 5 可能從 ~2s 變成 ~60s |
| R-CIN-4 | 低 | 穩健性 | 單一 batch 失敗即放棄所有已拉回的資料 | 浪費前 N-1 批的工作 |
| R-CIN-5 | 低 | 安全/可維護 | 雙層模板（f-string + .format）易混淆 | 目前安全但有維護風險 |

### 建議修復順序

1. **先修 R-CIN-1**（排序）：這是最可能在生產環境直接爆炸的問題（`merge_asof` MergeError）。
2. **順手修 R-CIN-2**（concat 防護）和 **R-CIN-5**（消除 .format）：改動極小。
3. **視需要修 R-CIN-3**（效能）和 **R-CIN-4**（partial failure）：可在生產驗收後根據實際延遲決定。

---

## Round 370（2026-03-06）— Chunked-IN 風險轉最小可重現測試（tests-only）

### 本輪範圍

- 依指示讀取 `.cursor/plans/PLAN.md`、`.cursor/plans/STATUS.md`、`.cursor/plans/DECISION_LOG.md`
- 只新增 tests 與 STATUS 紀錄，不改 production code

### 新增檔案

- `tests/test_review_risks_chunked_in_round370.py`

### 新增測試（5 項，皆為 expectedFailure）

1. `test_r_cin_1_chunked_concat_should_global_sort`
   - 目標：要求 chunked concat 後有全域 `sort_values(["canonical_id", "snapshot_dtm"])`
2. `test_r_cin_2_concat_should_have_empty_parts_guard`
   - 目標：要求 `pd.concat` 前有 `_parts` 空清單防護
3. `test_r_cin_3_large_list_path_should_log_progress`
   - 目標：要求大列表分批查詢有 batch-level progress log
4. `test_r_cin_4_per_batch_failure_should_be_logged`
   - 目標：要求 per-batch 失敗時有 stage-specific `logger.error`
5. `test_r_cin_5_avoid_str_format_on_sql_template`
   - 目標：要求移除 `.format()` SQL 模板（改成明確雙 query 字串）

> 說明：本輪是 tests-only，故以 `@unittest.expectedFailure` 顯性標記未修復風險，維持主線可執行並保留風險可見性。

### 執行方式

```bash
python -m pytest -q tests/test_review_risks_chunked_in_round370.py
```

### 執行結果

```text
5 xfailed in 0.99s
```

### 下一步建議

1. 先修 R-CIN-1（排序）與 R-CIN-2（concat guard）兩個 correctness 問題。
2. 再修 R-CIN-5（移除 `.format` 模板），降低 SQL 拼接維護風險。
3. R-CIN-3 / R-CIN-4 依生產環境延遲與穩定性需求決定是否實作（觀測性/容錯強化）。
---

## Round 371 — R-CIN-1~5 全部修復（2026-03-06）

### 目標
修改實作直到 Round 370 所有 guardrail tests 通過，不修改 tests（除非 test 本身 stale）。

### 改動的檔案

| 檔案 | 性質 | 說明 |
|------|------|------|
| `trainer/trainer.py` | 實作 | `load_player_profile` 大列表分支全面修補（見下） |
| `tests/test_review_risks_chunked_in_round370.py` | tests | 移除全部 5 個 `@expectedFailure`（已通過，decorator 過期） |
| `tests/test_trainer_review_risks_temp_table.py` | tests | `test_three_branch_logic_present` 更新 assertion（stale marker） |

### trainer.py 變更細節（load_player_profile 大列表分支）

| Risk | 修正內容 |
|------|---------|
| R-CIN-1 | `pd.concat` 後接 `sort_values(["canonical_id","snapshot_dtm"])` 全域排序 |
| R-CIN-2 | `pd.concat(_parts, ...) if _parts else pd.DataFrame()` 空列表防護 |
| R-CIN-3 | 迴圈內 `logger.info("player_profile: batch %d/%d", ...)` 進度日誌 |
| R-CIN-4 | 每批查詢包 `try/except`，失敗時 `logger.error("...batch %d/%d failed: %s")` 後 re-raise |
| R-CIN-5 | 拆成 `_query_no_filter` / `_query_with_filter` 兩個明確字串，移除 `.format()` SQL 模板 |

### test 修正理由

- **5 個 `@expectedFailure` 移除**：Round 370 建立測試時，生產程式碼尚未修補，因此加上 `@expectedFailure`。修補後測試通過，decorator 變成 stale（Unexpected success = pytest 報錯），依循前例移除。
- **`test_three_branch_logic_present` assertion 更新**：原本找 `cid_clause=""` 作為「無過濾分支」存在的標記；移除 `.format()` 模板後該字串消失。改為 `cid_clause="" in src OR _query_no_filter in src`，讓 guardrail 繼續有效。

### 測試結果

```
pytest tests/test_review_risks_chunked_in_round370.py tests/test_trainer_review_risks_temp_table.py -v
11 passed in 0.33s
```

### Lint / Typecheck 結果

| 工具 | 範圍 | 結果 |
|------|------|------|
| ruff | 本次改動的 3 個檔案 | ✅ All checks passed |
| mypy | trainer/trainer.py | ✅ 本次改動無新增錯誤；`features.py:1074 no-redef` 為預先存在問題（已用 git stash 確認） |

### 手動驗證步驟

```bash
# 1. 單元測試
python -m pytest tests/test_review_risks_chunked_in_round370.py tests/test_trainer_review_risks_temp_table.py -v

# 2. Lint
python -m ruff check trainer/trainer.py tests/test_review_risks_chunked_in_round370.py tests/test_trainer_review_risks_temp_table.py

# 3. 端對端（需 ClickHouse 連線）
python -m trainer.trainer --days 30
# 觀察 log 是否出現 "player_profile: batch X/Y" 及最終 "N rows loaded from ClickHouse"
```

### 下一步建議

1. 跑全套 `python -m pytest tests/` 確認其他 tests 未受影響。
2. 考慮對 `features.py:1074` 的 pre-existing mypy `no-redef` 錯誤開一個獨立 ticket 修復。
3. 觀察生產環境日誌，確認批次進度 log 頻率（每 10 批一次）合適，視需要調整。
