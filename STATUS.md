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