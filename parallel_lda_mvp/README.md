# parallel_lda_mvp

與 `pipelines/layered_data_assets` **完全分離**：只 subprocess 呼叫既有 `scripts/preprocess_bet_v1.py`、`scripts/materialize_run_fact_v1.py`、`scripts/materialize_trip_fact_v1.py`，不修改 LDA 原始碼。

## 執行方式（零參數）

在 **repo 根目錄**：

```bash
python -m parallel_lda_mvp.run_mvp
```

不帶任何參數。啟動時會印出實際採用的 `gaming_ym`、`source_snapshot_id`、`cutoff`、`t_session`、bet 檔列表。

說明文件：

```bash
python -m parallel_lda_mvp.run_mvp -h
```

## 預設怎麼找資料

| 項目 | 預設邏輯 |
|------|-----------|
| **t_bet** | 環境變數 `PARALLEL_LDA_MVP_T_BET`（單一檔）→ 否則 `data/gmwds_t_bet.parquet` → 否則 `data/l0_layered/*/t_bet/**/*.parquet`（全部排序） |
| **t_session** | `PARALLEL_LDA_MVP_T_SESSION` → 否則 `data/gmwds_t_session.parquet` |
| **gaming_ym** | `PARALLEL_LDA_MVP_GAMING_YM`（`YYYY-MM`）→ 否則由 bet 檔內 `MAX(gaming_day)` 所屬月份（DuckDB） |
| **source_snapshot_id** | `PARALLEL_LDA_MVP_SNAPSHOT_ID` → 否則路徑若在 `l0_layered/<snap_...>/` 下則取該段 → 否則 `snap_mvp_<sha16>` |
| **cutoff** | `PARALLEL_LDA_MVP_CUTOFF_DTM`（ISO）→ 否則該 `gaming_ym` 最後一日 **Asia/Hong_Kong** 23:59:59.999999 |

與 trainer 本機慣例對齊：`data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`。

## 產出

`data/parallel_lda_mvp/<source_snapshot_id>/gaming_ym=YYYY-MM/`（含 `eligible_player_ids.parquet`、`t_bet/`、`run_fact/`、`trip_fact/`、`mvp_summary.json`）。

## Rated eligible（BET-DQ-03）與 canonical 快取

**不**讀寫 trainer 的 `data/canonical_mapping.*`（避免踩別人資料）。

- 計算方式與 **trainer Step 3** 相同：`build_canonical_links_and_dummy_from_duckdb` → `build_canonical_mapping_from_links`（DuckDB links + pandas M:N）。
- **Mapping 輸入**：一律經 `parallel_lda_mvp/session_for_mapping.py` 的 `prepare_session_parquet_for_canonical_mapping`；目前 **cleaned = raw**（無列級清洗），未來在此實作 backfill／去重等並改回傳 cleaned Parquet 路徑即可。邏輯版本常數 `SESSION_MAPPING_CLEAN_LOGIC_VERSION`：變更清洗規則時遞增，即使 L0 檔未變也會讓指紋變、強迫重算（驗證「來源不變、程式變」）。
- **快取只寫在** `parallel_lda_mvp/canonical_cache/`：`mapping_<sha256>.parquet` + `mapping_<sha256>.meta.json`。
- **失效條件**：對 **mapping 輸入** Parquet 做**整檔串流 SHA-256**，再與 naive-HK 的 cutoff、`SESSION_MAPPING_CLEAN_LOGIC_VERSION` 合併成最終指紋。輸入檔位元組、cutoff、清洗邏輯版本任一變更 → 重跑 DuckDB。

若要強制全重算：刪除整個 `parallel_lda_mvp/canonical_cache/` 目錄即可。

**成本**：每次執行到 eligible 步驟時，至少會**完整讀一次**「mapping 用 session」Parquet 來算內容 hash（快取命中時仍要讀檔以得到指紋；DuckDB 建 mapping 時還會再讀）。大檔時 I/O 時間會很明顯。

## 記憶體

整月逐日 preprocess，且每日 `run_fact` 會餵入**整月** `cleaned.parquet` 以保留跨日 run；大檔時 RAM 壓力高。必要時用 `PARALLEL_LDA_MVP_GAMING_YM` 鎖小月，或先縮小 bet 匯出。
