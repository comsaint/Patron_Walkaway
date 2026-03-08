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

---

## Round 372 — Plan B+ 階段 3：串流匯出 LibSVM + .weight（2026-03-08）

### 目標
實作 PLAN §4.3 階段 3：從 `train_path` / `valid_path`（Parquet）串流寫出  
`train_for_lgb.libsvm`、`train_for_lgb.libsvm.weight`、`valid_for_lgb.libsvm`；weight 語義與 `compute_sample_weights` 一致（run-level 1/N_run）。不載入完整 train 進記憶體。

### 改動的檔案

| 檔案 | 性質 | 說明 |
|------|------|------|
| `trainer/config.py` | 設定 | 新增 `STEP9_EXPORT_LIBSVM: bool = False`（Plan B+ 區塊） |
| `trainer/trainer.py` | 實作 | 讀取 `STEP9_EXPORT_LIBSVM`；新增 `_export_parquet_to_libsvm(...)`；在 `step7_train_path is not None` 且載入 train 前，若 `STEP9_EXPORT_LIBSVM` 且 `active_feature_cols` 非空則呼叫匯出 |

### 實作摘要

- **`_export_parquet_to_libsvm(train_path, valid_path, feature_cols, export_dir)`**：以 DuckDB 自 Parquet 串流讀取（`fetchmany(50_000)`），僅 `is_rated` 列；train 權重為 `1.0 / COUNT(*) OVER (PARTITION BY canonical_id, run_id)`；寫出 LibSVM（1-based 特徵索引、省略 0 做 sparse）與 `.weight`（一行一權重）；valid 僅寫 `.libsvm`。
- **接線**：在 `run_pipeline` 的「Step 7 B+ 載入 train」區塊內，在 `pd.read_parquet(step7_train_path)` 與 unlink 之前，若 `STEP9_EXPORT_LIBSVM` 且 `active_feature_cols` 非空則呼叫上述函式，輸出目錄為 `DATA_DIR / "export"`。

### 手動驗證

1. **單元/整合**：`python -m pytest tests/ -q`（見下方結果）。
2. **手動觸發匯出**：設定 `STEP9_EXPORT_LIBSVM = True` 且啟用 B+ 路徑（`STEP7_KEEP_TRAIN_ON_DISK` 等），跑一次 pipeline；檢查 `trainer/.data/export/` 下是否產生 `train_for_lgb.libsvm`、`train_for_lgb.libsvm.weight`、`valid_for_lgb.libsvm`，且 train 行數與 weight 行數一致。

### 下一步建議

1. **階段 4（PLAN §4.4）**：Step 9 改為自 LibSVM 訓練（`lgb.Dataset(libsvm_path, weight_file=...)`），不再載入 `train_df`，以完成 B+ 記憶體優化。
2. 可選：為 `_export_parquet_to_libsvm` 加小型單元測試（mock Parquet + DuckDB 或 fixture）以鎖定行為。

### pytest -q 結果

```
732 passed, 4 skipped, 28 warnings, 5 subtests passed in 19.11s
```

---

## Code Review：Plan B+ 階段 3 變更（Round 372，2026-03-08）

以下針對 **Round 372** 引入的 `STEP9_EXPORT_LIBSVM`、`_export_parquet_to_libsvm` 及 run_pipeline 接線進行審查。僅列出最可能的 bug、邊界條件、安全性與效能問題；每項附**具體修改建議**與**希望新增的測試**。不重寫整套實作。

---

### 1. [Bug] 特徵值 NaN 寫成字串 `"nan"`，LightGBM LibSVM 可能無法解析

- **問題描述**：`_export_parquet_to_libsvm` 中對特徵值做 `x = float(v)`，若 Parquet 內為 `NaN`，則 `float(v)` 為 `float('nan')`，`x != 0.0` 為 True，會寫出 `"idx:nan"`。LibSVM 格式通常要求數值，LightGBM 從檔案讀 LibSVM 時可能無法解析 `nan` 字串或行為未定義。
- **具體修改建議**：在寫入 LibSVM 前將 NaN 視為 0（與 PLAN「0 可省略為 sparse」一致）。例如在 `x = float(v)` 之後加：`if math.isnan(x): x = 0.0`（或 `x = 0.0 if (isinstance(x, float) and math.isnan(x)) else x`），再依 `x != 0.0` 決定是否輸出。
- **希望新增的測試**：單元測試：給定一筆 Parquet（或 mock DuckDB 回傳）其中某特徵為 `NaN`，匯出後該行 LibSVM 中該特徵索引要么省略（視為 0）、要么為 `idx:0`，且檔案可被 `lgb.Dataset(path)` 成功讀入（或至少斷言輸出行不包含字串 `"nan"`）。

---

### 2. [邊界條件] Train 或 Valid 無任何 is_rated 列時產出空檔，LightGBM 可能無法建 Dataset

- **問題描述**：若 `train_path` 或 `valid_path` 經 `WHERE is_rated = true` 後為 0 列，函式仍會寫出 0 行的 `.libsvm`（及 train 的 `.weight`）。後續階段 4 若以 `lgb.Dataset(train_for_lgb.libsvm, weight_file=...)` 讀取，LightGBM 可能報錯或行為未定義。
- **具體修改建議**：在寫入 train 後若 `n_train == 0`，記錄 `logger.warning` 並可選拋出 `ValueError("LibSVM export produced 0 train rows; cannot train from file.")`，或在 docstring 註明「caller 應在階段 4 檢查檔案非空再建 Dataset」。若選擇僅 warning，則在階段 4 實作時對空檔做明確處理（跳過或失敗）。
- **希望新增的測試**：整合或單元測試：mock/ fixture 使 train Parquet 的 `is_rated` 全為 false，呼叫 `_export_parquet_to_libsvm`，斷言要麼拋出明確錯誤、要麼至少有一次 `warning` 且 train 行數為 0；並可選斷言 `valid` 為 0 行時行為一致（例如 valid 空檔僅 warning、不阻斷）。

---

### 3. [邊界條件] train_path / valid_path 不存在時錯誤訊息不友善

- **問題描述**：未先檢查 `train_path.exists()` / `valid_path.exists()`，直接以 DuckDB `read_parquet(path)` 讀取。若檔案不存在或路徑錯誤，DuckDB 拋出的例外可能較難對應到「路徑錯誤」。
- **具體修改建議**：在函式開頭（建立 export_dir 之後）加上：  
  `if not train_path.exists(): raise FileNotFoundError(f"Train Parquet not found: {train_path}")`  
  以及對 `valid_path` 的同樣檢查。若希望 valid 可選，則改為僅對 train_path 必存在、valid_path 可選並在不存在時跳過寫 valid 或明確說明行為。
- **希望新增的測試**：傳入不存在的 `train_path`（或 `valid_path`），斷言拋出 `FileNotFoundError`（或明確的錯誤類型）且訊息中包含路徑或 "not found" 等關鍵字。

---

### 4. [邊界條件] label 未驗證為 0/1，非二分類值可能導致訓練或評估錯誤

- **問題描述**：目前以 `label = int(row[0])` 寫入 LibSVM。若 Parquet 中 `label` 為 2、-1 或浮點 0.5，會變成 2、-1、0。LightGBM 二分類預期 0/1，非 0/1 可能導致訓練異常或評估解讀錯誤。
- **具體修改建議**：寫入前驗證：若 `label not in (0, 1)` 則記錄 `logger.warning("LibSVM export: non-binary label %s at row, coercing to 0/1", label)` 並將 label 強制為 `1 if label else 0`，或在首筆異常時直接 `raise ValueError("LibSVM export expects binary label 0/1, got ...")`。依專案策略二擇一（寬鬆 coerce 或嚴格 fail）。
- **希望新增的測試**：給定一筆 label=2（或 -1）的列，斷言匯出後該行 label 被改為 0/1 或函式拋出 ValueError；並可選斷言日誌中出現對應 warning。

---

### 5. [安全性/可維護性] 路徑來自呼叫端，僅跳脫單引號；惡意或異常路徑仍有風險

- **問題描述**：`_esc_path` 僅對單引號做 `'` → `''`，避免 SQL 字串斷開。若 `train_path`/`valid_path` 來自不可信來源或含異常字元（如換行、多個單引號組合），理論上仍有注入或解析風險。目前呼叫端為 run_pipeline，路徑為 Step 7 產出，屬內部可控。
- **具體修改建議**：在 docstring 或模組註解中明確寫明「train_path / valid_path 必須為受信任的內部路徑，由 Step 7 產出；勿傳入使用者可控路徑」。若未來接受外部路徑，應改為 DuckDB 參數化查詢或僅接受絕對路徑白名單。
- **希望新增的測試**：靜態/規則測試：搜尋 `_export_parquet_to_libsvm` 的呼叫處，斷言傳入的 path 來自 `step7_train_path`/`step7_valid_path`（或常數 `DATA_DIR / "export"`），而非任意使用者輸入；或文件化「path 僅限內部」並由 code review 覆核。

---

### 6. [穩健性] 寫入中斷時可能產生 train 行數與 weight 行數不一致的殘留檔

- **問題描述**：train 的 `.libsvm` 與 `.libsvm.weight` 以兩個檔案同時逐行寫入。若寫入中途發生例外（磁碟滿、OOM、中斷），可能出現 .libsvm 行數 ≠ .weight 行數，階段 4 用 LightGBM 載入時會得到錯誤對齊的權重。
- **具體修改建議**：改為先寫入暫存檔（例如 `train_for_lgb.libsvm.tmp`、`train_for_lgb.libsvm.weight.tmp`），全部成功寫完後再 `os.replace(tmp, final)` 覆蓋正式檔；失敗時保留或刪除暫存檔並重新拋出例外，避免留下半成品為「正式」檔。
- **希望新增的測試**：單元測試：mock 在寫入第 N 行時拋出例外，斷言最終正式路徑下 either 無新檔案、或僅存在未更名的 .tmp；且成功路徑下不會出現「.libsvm 與 .weight 行數不同」的檔案對。

---

### 7. [效能] batch_size 固定 50_000，極低記憶體環境可能需更小批次

- **問題描述**：`fetchmany(50_000)` 與逐行處理的記憶體用量相對可控，但在極端低 RAM 環境下，50_000 行 × 特徵數 × 8 位元組可能仍偏高。
- **具體修改建議**：可選：將 `batch_size` 改為函式參數（預設 50_000）或從 config 讀取（例如 `STEP9_LIBSVM_BATCH_SIZE`），便於調校。非必須，可列為後續優化。
- **希望新增的測試**：可選：傳入較小 batch_size（如 100），斷言匯出結果與預設 batch 一致（相同行數、前幾行內容一致）；或僅在文件/註解中說明可調參數意圖。

---

### 8. [一致性] 與 Plan B CSV 匯出行為差異：未限制「僅 common 特徵」、未檢查路徑存在

- **問題描述**：`_export_train_valid_to_csv` 會取 train/valid 的 **common** 特徵、缺欄時 warning 並只匯出 common_cols，且會檢查 DataFrame 有 `label`。`_export_parquet_to_libsvm` 直接使用傳入的 `feature_cols`，若 Parquet 缺某欄由 DuckDB 拋錯；且未先檢查檔案存在。
- **具體修改建議**：若希望與 Plan B 行為對齊，可在匯出前用 DuckDB 查詢 Parquet 的 schema（或 `read_parquet 的 columns`），只保留 `feature_cols` 中實際存在的欄位，並對「僅存在於 train 或僅存在於 valid」的欄位打 warning；否則至少在 docstring 註明「caller 須保證 feature_cols 在兩份 Parquet 皆存在且順序一致」。路徑存在性見項目 3。
- **希望新增的測試**：當某 `feature_cols` 在 train 存在、在 valid 不存在（或反）時，斷言要麼匯出時有 warning 且僅使用 common 特徵、要麼 DuckDB 報錯被明確處理；並可選比對與 CSV 匯出在「common 特徵子集」上的一致性（若兩者皆啟用）。

---

### 總結

| # | 類型       | 嚴重度（主觀） | 建議優先處理 |
|---|------------|----------------|--------------|
| 1 | Bug        | 高             | 是（NaN → 0） |
| 2 | 邊界條件   | 中             | 是（0 行 train） |
| 3 | 邊界條件   | 中             | 是（路徑存在檢查） |
| 4 | 邊界條件   | 中             | 可選（label 0/1） |
| 5 | 安全性     | 低             | 文件化即可 |
| 6 | 穩健性     | 中             | 建議（原子寫入） |
| 7 | 效能       | 低             | 可選 |
| 8 | 一致性     | 低             | 可選／文件化 |

以上為 Round 372 Plan B+ 階段 3 變更之審查結果；實作修正與測試可依優先級分輪進行。

---

## Round 373 — Plan B+ 階段 3 Review 風險 → 最小可重現測試（僅 tests，未改 production）

### 目標
將 STATUS.md「Code Review：Plan B+ 階段 3 變更」中列出的 8 項風險轉成可執行的最小可重現測試或 lint/規則檢查；**僅新增測試，不修改 production code**。

### 新增檔案

| 檔案 | 說明 |
|------|------|
| `tests/test_review_risks_round372_plan_b_plus_libsvm.py` | Plan B+ LibSVM 匯出 8 項 review 風險對應的 guard 測試 |

### 測試與 Review 項目對應

| Review # | 風險要點 | 測試／規則 | 目前狀態 |
|----------|----------|------------|----------|
| 1 | 特徵 NaN 不得寫成字串 "nan" | `test_libsvm_output_contains_no_nan_literal_when_feature_is_nan`：Parquet 含 NaN 時匯出檔不得含 "nan" | ✅ PASS（DuckDB 回傳 None 時已省略） |
| 2 | 0 行 train 時應 warning 或 raise | `test_zero_rated_train_rows_should_warn_or_raise`：全 is_rated=False 時須有 warning 且 train 行數=0 | ⚠️ XFAIL（production 尚未實作） |
| 3 | 路徑不存在應拋 FileNotFoundError | `test_missing_train_path_raises_filenotfounderror`：傳入不存在的 train_path 須拋 FileNotFoundError 或訊息含 "not found" | ⚠️ XFAIL（production 依賴 DuckDB 錯誤） |
| 4 | label 須為 0/1 | `test_non_binary_label_should_be_coerced_or_raise`：label=2 時匯出須為 0/1 或 raise | ⚠️ XFAIL（production 原樣寫入） |
| 5 | 呼叫處僅用內部路徑 | `test_export_libsvm_is_called_with_step7_paths_and_export_dir`：靜態規則，call site 須為 step7_train_path、step7_valid_path、DATA_DIR/export | ✅ PASS |
| 6 | 應先寫 .tmp 再 rename 做原子寫入 | `test_export_uses_temp_file_then_rename`：函式內須有 .tmp 或 os.replace/rename | ⚠️ XFAIL（production 直接寫最終路徑） |
| 7 | batch_size 存在／可調 | `test_batch_size_present_in_export_function`：函式內須有 batch_size 與 fetchmany | ✅ PASS |
| 8 | valid 缺欄時須明確處理或 common 特徵 | `test_valid_missing_feature_column_handled_gracefully`：valid 缺 feature_cols 時須成功用 common 或錯誤訊息含 column/f2/common 等 | ✅ PASS（DuckDB 錯誤訊息已含欄位資訊） |

### 執行方式

僅跑本輪新增的 Plan B+ LibSVM review 測試：

```bash
python -m pytest tests/test_review_risks_round372_plan_b_plus_libsvm.py -v
```

預期結果：**4 passed, 4 xfailed**（4 項為尚未實作的預期失敗，待 production 修正後移除 `@unittest.expectedFailure`）。

全套測試（含本輪）：

```bash
python -m pytest -q
```

預期：**736 passed, 4 skipped, 4 xfailed**（新增 4 個通過、4 個 xfail）。

### 下一步建議

1. 依 Review 優先級修正 production（#1 NaN 已滿足；#2、#3、#4、#6 待實作），每修一項可移除對應測試的 `@expectedFailure` 並確認通過。
2. 若未來變更 call site 或匯出邏輯，須確保上述 5 項通過的規則／行為不回歸。

---

## Round 374 — Plan B+ 階段 3 Review 修正（production 實作 + 全綠）

### 目標
依 Round 372 Code Review 與 Round 373 測試，修正 production 直至所有 tests / typecheck / lint 通過；僅在 decorator 過時時移除 `@expectedFailure`，不改測試邏輯。

### 改動的檔案

| 檔案 | 性質 | 說明 |
|------|------|------|
| `trainer/trainer.py` | 實作 | `_export_parquet_to_libsvm`：路徑存在檢查、0 行 train warning、label 0/1 強制、NaN→0、原子寫入 .tmp + os.replace；call site 加 assert step7_valid_path 以滿足 mypy |
| `trainer/trainer.py` | 實作 | 頂層 `import math`（供 `math.isnan`） |
| `tests/test_review_risks_round372_plan_b_plus_libsvm.py` | tests | 移除 4 個已過時的 `@unittest.expectedFailure`（#2、#3、#4、#6） |

### 實作摘要（_export_parquet_to_libsvm）

| Review # | 修正內容 |
|----------|----------|
| 2 | 寫完 train 後若 `n_train == 0` 則 `logger.warning("LibSVM export produced 0 train rows...")` |
| 3 | 函式開頭 `train_path.exists()` / `valid_path.exists()` 若不存在則 `raise FileNotFoundError(...)` |
| 4 | 寫入前將 label 強制為 0/1（`label = 1 if raw_label else 0`），非 0/1 時 `logger.warning` |
| 6 | 先寫入 `train_for_lgb.libsvm.tmp`、`train_for_lgb.libsvm.weight.tmp`、`valid_for_lgb.libsvm.tmp`，成功後 `os.replace(tmp, final)` |
| 1（加固） | 特徵值 `float(v)` 後若 `math.isnan(x)` 則 `x = 0.0`，避免寫出字串 "nan" |

### 測試結果

```bash
python -m pytest tests/test_review_risks_round372_plan_b_plus_libsvm.py -v
# 8 passed in ~1s

python -m pytest -q
# 740 passed, 4 skipped, 28 warnings, 5 subtests passed in ~19.5s
```

### Lint / Typecheck

| 工具 | 範圍 | 結果 |
|------|------|------|
| ruff | `trainer/trainer.py`、`tests/test_review_risks_round372_plan_b_plus_libsvm.py` | All checks passed |
| mypy | `trainer/trainer.py` | 本次修正消除 `_export_parquet_to_libsvm` 呼叫處之 arg-type（`step7_valid_path` 加 assert）；其餘為既有 import-untyped 等 |

### 手動驗證

```bash
python -m pytest tests/test_review_risks_round372_plan_b_plus_libsvm.py -v
python -m pytest -q
python -m ruff check trainer/trainer.py tests/test_review_risks_round372_plan_b_plus_libsvm.py
```

### 下一步建議

1. **PLAN 方案 B+ 階段 4**：Step 9 改為自 LibSVM 訓練（`lgb.Dataset(libsvm_path, weight_file=...)`），不再載入 `train_df`，完成 B+ 記憶體優化。
2. 可選：第一次建 Dataset 後 `save_binary`、Valid/Test 從檔案或分塊 predict。

---

## Round 375 — Plan B+ 階段 4：Step 9 從 LibSVM 訓練（2026-03-08）

### 目標
實作 PLAN §4.4：當 `STEP9_EXPORT_LIBSVM` 且已匯出 LibSVM 時，Step 9 以 `lgb.Dataset(libsvm_path)` 從檔案訓練，並載入同名的 `.weight` 檔，不再依賴 in-memory train 建 Dataset。

### 改動的檔案

| 檔案 | 性質 | 說明 |
|------|------|------|
| `trainer/trainer.py` | 實作 | `train_single_rated_model` 新增參數 `train_libsvm_paths: Optional[Tuple[Path, Path]]`；當設且兩檔存在時以 `use_from_libsvm` 建 `dtrain`/`dvalid` 從路徑、讀 .weight 檔傳入、用預設 hp、`lgb.train`；`run_pipeline` 在 B+ 路徑下取得 `_export_parquet_to_libsvm` 回傳路徑並傳入 `train_libsvm_paths` |

### 實作摘要

- **train_libsvm_paths**：若為 `(train_path, valid_path)` 且兩檔存在，則 `use_from_libsvm = True`，不走 Plan B CSV 與 in-memory 訓練。
- **LibSVM 訓練**：0 行 train 時 fallback in-memory；否則讀 `.weight` 檔（一行一權重）傳入 `lgb.Dataset(..., weight=...)`；`dtrain`/`dvalid` 以 `feature_name=avail_cols` 建；用預設 hp、early_stopping 於 dvalid（若 valid 足夠）；產出 `_BoosterWrapper(booster)` 與 metrics，與既有 path 一致。
- **接線**：`run_pipeline` 在 `step7_train_path is not None` 且 `STEP9_EXPORT_LIBSVM` 時於 export 後取得 `_train_libsvm, _valid_libsvm`，呼叫 `train_single_rated_model(..., train_libsvm_paths=(_train_libsvm, _valid_libsvm))`。

### 手動驗證

1. **單元／整合**：`python -m pytest -q`（見下方結果）。
2. **B+ 端對端**：設定 `STEP7_KEEP_TRAIN_ON_DISK=True`、`STEP9_EXPORT_LIBSVM=True`，跑 `python -m trainer.trainer --days 30`（或 `--recent-chunks 3`），確認 log 出現 LibSVM 匯出與訓練，且產出 `model.pkl`。

### 下一步建議

1. **可選**：B+ 路徑下不載入 `train_df`（僅保留 valid/test），改為在 Step 9 後用 LibSVM 路徑訓練，進一步降 peak RAM。
2. **可選**：第一次建 Dataset 後 `save_binary`；Valid/Test 從檔案或分塊 predict（階段 5–6）。

### pytest -q 結果

```
740 passed, 4 skipped, 28 warnings, 5 subtests passed in 21.14s
```

---

## Code Review：Plan B+ 階段 4 變更（Round 375，2026-03-08）

以下針對 **Round 375** 引入的 `train_libsvm_paths`、`use_from_libsvm` 分支及 run_pipeline 接線進行審查。僅列出最可能的 bug、邊界條件、安全性與效能問題；每項附**具體修改建議**與**希望新增的測試**。不重寫整套實作。

---

### 1. [Bug] .weight 檔行數與 LibSVM 行數不一致時，LightGBM 可能錯位或報錯

- **問題描述**：目前讀取 `.weight` 檔為 `[float(line.strip()) for line in _wf]`，未驗證 `len(_train_weights)` 是否等於 LibSVM 行數 `_n_lines`。若 .weight 因寫入中斷、手動編輯或與舊版 export 混用而多/少幾行，LightGBM 會將權重對應到錯誤的樣本，或於內部檢查時拋錯。
- **具體修改建議**：在讀完 `_train_weights` 後加上：`if _train_weights is not None and len(_train_weights) != _n_lines: logger.warning("Plan B+: weight file line count (%d) != train LibSVM lines (%d); ignoring weights.", len(_train_weights), _n_lines); _train_weights = None`，或改為拋出 `ValueError` 明確中止，由呼叫端決定策略。
- **希望新增的測試**：單元測試：準備 train.libsvm（N 行）與 train.libsvm.weight（N-1 或 N+1 行），呼叫 `train_single_rated_model(..., train_libsvm_paths=(...))`，斷言要麼出現 warning 且權重被忽略、要麼拋出明確錯誤；並可選斷言 N 行對 N 行時訓練成功且無該 warning。

---

### 2. [邊界條件] 0 行 LibSVM fallback 後若 train_rated 為空，in-memory 路徑會失敗

- **問題描述**：當 `_n_lines < 1` 時設 `use_from_libsvm = False`，後續走 `_train_one_model(X_tr, y_tr, ...)`。目前 run_pipeline 仍會載入 train_df，故 `train_rated` 通常非空。若未來 B+ 路徑改為不載入 train，`train_rated` 可能為空，此時 `X_tr`/`y_tr` 為空，`_train_one_model` 或 LightGBM 可能報錯或產出無意義模型。
- **具體修改建議**：在設 `use_from_libsvm = False` 之後、進入 `if not use_from_file and not use_from_libsvm` 之前，若 `train_rated.empty`：記錄 `logger.warning("Plan B+: fallback to in-memory but no train rows; cannot train.")` 並 `return None, None, {"rated": None}`，與現有「no training rows」行為一致。
- **希望新增的測試**：整合或 mock 測試：在 `use_from_libsvm` 為 True 且 LibSVM 為 0 行的情境下，mock 或提供空的 `train_df`，斷言函式回傳 `(None, None, {"rated": None})` 或等同行為，且不呼叫 `_train_one_model` 或 LightGBM。

---

### 3. [邊界條件] test_rated 缺欄時可能 KeyError

- **問題描述**：LibSVM 路徑結束後 `avail_cols = list(booster.feature_name())`，後續 `test_rated[avail_cols]` 若 test_df 缺少任一 `avail_cols` 會 KeyError。Plan B CSV 路徑對 valid 有 `_missing_val_cols` 防呆，對 test 則未限制。
- **具體修改建議**：在 `if test_rated is not None and not test_rated.empty` 區塊內，計算 `_missing_te_cols = [c for c in avail_cols if c not in test_rated.columns]`；若非空則 `logger.warning("Plan B+: test_df missing columns %s; skipping test metrics.", _missing_te_cols)` 且不呼叫 `_compute_test_metrics`（或僅傳入 test_rated 中存在的欄位子集並在 doc 註明可能維度不符），避免 KeyError。
- **希望新增的測試**：給定 `train_libsvm_paths` 且訓練成功，傳入的 `test_df` 缺少部分 `avail_cols`，斷言不拋 KeyError，且日誌出現 missing columns 的 warning 或 test 指標被跳過。

---

### 4. [邊界條件] .weight 檔含非數值或空行時會拋 ValueError

- **問題描述**：`_train_weights = [float(line.strip()) for line in _wf]` 遇空行會 `float('')` 拋 ValueError；遇非數值字串亦然。若 export 或檔案損壞產生異常行，整段訓練會中斷。
- **具體修改建議**：逐行讀取時做 try/except 或驗證：例如 `_train_weights = []`，對每行 `s = line.strip(); _train_weights.append(float(s) if s else 0.0)` 或 `try: _train_weights.append(float(s)); except ValueError: logger.warning("Plan B+: invalid weight line %r, using 0.0", s); _train_weights.append(0.0)`。並在最後仍可選擇檢查 `len(_train_weights) == _n_lines`（見項目 1）。
- **希望新增的測試**：準備含一空行或含 "nan"/"x" 的 .weight 檔，斷言要麼轉為 0.0 且出現 warning、要麼拋出明確錯誤，且行為在文件中註明。

---

### 5. [效能] 大資料時 .weight 整檔載入記憶體

- **問題描述**：`.weight` 檔以 `[float(line.strip()) for line in _wf]` 一次載入。60M 行約 480MB+。PLAN 目標為避免 train 特徵/標籤進記憶體，權重仍進記憶體，對極大 window 仍可能推高 peak RAM。
- **具體修改建議**：在 docstring 或註解中註明「B+ 路徑下 train 特徵/標籤不進記憶體，但 .weight 會整檔讀入；若需進一步降 RAM 可考慮 LightGBM 支援從檔案讀 weight 或分塊讀入」。可選：當 `_n_lines` 超過某閾值（如 10M）時 `logger.info("Plan B+: loading %d weights into memory (~%.0f MB)", _n_lines, _n_lines * 8 / 1e6)` 以利觀察。
- **希望新增的測試**：可選：mock 或 fixture 產生大行數 .weight，斷言記憶體使用在合理範圍或僅文件化預期；或壓力測試在給定 RAM 下可完成訓練。

---

### 6. [一致性] LibSVM 路徑未做單一類別檢查（Plan B CSV 有）

- **問題描述**：Plan B CSV 路徑會檢查 `_train_labels["label"].nunique() < 2`，若僅單一類別則 fallback in-memory 並 warning。LibSVM 路徑未做等同檢查，若 export 後 train 僅 0 或僅 1，LightGBM 可能仍訓練但模型/閾值可能退化。
- **具體修改建議**：在 `use_from_libsvm` 且 `_n_lines >= 1` 時，快速掃過 LibSVM 第一欄（label）或讀取前若干行，若僅見 0 或僅見 1 則 `logger.warning("Plan B+: train LibSVM has only one class; falling back to in-memory training.")` 且 `use_from_libsvm = False`；或與 Plan B 一致改為 fallback。若掃描成本高可僅在文件註明「單一類別時行為未定義，建議由 caller 保證」。
- **希望新增的測試**：給定僅 label=0（或僅 label=1）的 train LibSVM，斷言要麼 fallback 並 warning、要麼明確拒絕訓練；並可選與 Plan B 單一類別行為對齊。

---

### 7. [可維護性] train_libsvm_paths 來源與契約

- **問題描述**：`train_libsvm_paths` 目前僅由 run_pipeline 在 B+ 路徑下傳入，路徑來自 `_export_parquet_to_libsvm` 回傳值，屬內部可控。若未來其他呼叫端傳入不可信路徑或錯誤順序（train/valid 對調），可能導致訓練/驗證資料錯置。
- **具體修改建議**：在 `train_single_rated_model` docstring 註明「train_libsvm_paths 須為 (train_libsvm_path, valid_libsvm_path)，且兩者皆為受信任的內部路徑（例如由 _export_parquet_to_libsvm 產出）；勿傳入使用者可控路徑。」若需要可加 assertion：`assert train_libsvm_paths[0].name.startswith("train") and train_libsvm_paths[1].name.startswith("valid")` 作為簡易防呆（或僅文件化）。
- **希望新增的測試**：靜態或規則測試：搜尋對 `train_single_rated_model` 的呼叫，斷言 `train_libsvm_paths` 僅在 run_pipeline 內傳入且來源為 `_export_parquet_to_libsvm` 回傳；或文件化契約並由 code review 覆核。

---

### 總結

| # | 類型       | 嚴重度（主觀） | 建議優先處理 |
|---|------------|----------------|--------------|
| 1 | Bug        | 高             | 是（weight 行數一致檢查） |
| 2 | 邊界條件   | 中             | 是（0 行 fallback + 空 train） |
| 3 | 邊界條件   | 中             | 是（test 缺欄 KeyError） |
| 4 | 邊界條件   | 中             | 是（.weight 非數值/空行） |
| 5 | 效能       | 低             | 文件化即可 |
| 6 | 一致性     | 中             | 可選（單一類別檢查） |
| 7 | 可維護性   | 低             | 文件化即可 |

以上為 Round 375 Plan B+ 階段 4 變更之審查結果；實作修正與測試可依優先級分輪進行。
