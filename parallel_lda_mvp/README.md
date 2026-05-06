# parallel_lda_mvp

以 subprocess 呼叫 `scripts/preprocess_bet_v1.py`、`scripts/materialize_run_fact_v1.py`、`scripts/materialize_trip_fact_v1.py`（底層為 `pipelines/layered_data_assets` CLI）。為支援「每月固定少數目錄」的輸出，CLI 已增加可選的 `--output-parquet` / `--output-manifest`（及 trip 的對應參數），**不改** preprocess／run／trip 的核心 SQL。

## 執行方式

在 **repo 根目錄**：

```bash
python -m parallel_lda_mvp.run_mvp
```

預設不帶參數即跑完整 MVP。啟動時會印出 `gaming_ym_span`、`source_snapshot_id`、`cutoff`、`t_session`、bet 檔列表。

可選：

- `--emit-trainer-local-parquet`：MVP 結束後寫入 `data/gmwds_t_bet.parquet` / `data/gmwds_t_session.parquet`（含 Phase C：`run_fact`／`trip_*` 併回下注列，見 `trainer_bridge_mvp.py`）。
- `--trainer-bridge-emit-only --snapshot-id <snap>`：只跑橋接（需已存在 `data/parallel_lda_mvp/<snap>/` 與 `mvp_summary.json`）。亦可只設 `PARALLEL_LDA_MVP_SNAPSHOT_ID`。
- 橋接 idempotency：`PARALLEL_LDA_BRIDGE_SKIP_IF_UNCHANGED=1`（指紋相同則跳過寫檔）；`PARALLEL_LDA_BRIDGE_DUCKDB_MEMORY_LIMIT=4GB` 等。

說明文件：

```bash
python -m parallel_lda_mvp.run_mvp -h
```

驗證 trainer 特徵 YAML 可載入：

```bash
python -m parallel_lda_mvp.trainer_bridge_mvp
```

本機 smoke（需先有橋接產物與小視窗資料）：

```bash
python -m trainer.trainer --use-local-parquet --recent-chunks 1 --skip-optuna --no-gbm-bakeoff --no-preload
```

## 預設怎麼找資料

| 項目 | 預設邏輯 |
|------|-----------|
| **t_bet** | 環境變數 `PARALLEL_LDA_MVP_T_BET`（單一檔）→ 否則 `data/gmwds_t_bet.parquet` → 否則 `data/l0_layered/*/t_bet/**/*.parquet`（全部排序） |
| **t_session** | `PARALLEL_LDA_MVP_T_SESSION` → 否則 `data/gmwds_t_session.parquet` |
| **gaming_ym** | `PARALLEL_LDA_MVP_GAMING_YM`（單一 `YYYY-MM`）→ 否則 **所有在 bet 裡出現過的日曆月份**（`gaming_day` 去重後由小到大排序，涵蓋整段 t_bet 期間） |
| **source_snapshot_id** | `PARALLEL_LDA_MVP_SNAPSHOT_ID` → 否則路徑若在 `l0_layered/<snap_...>/` 下則取該段 → 否則 `snap_mvp_<sha16>` |
| **cutoff** | `PARALLEL_LDA_MVP_CUTOFF_DTM`（ISO）→ 否則 **span 中最後一個月** 最後一刻 **Asia/Hong_Kong** 23:59:59.999999（整段共用一個 cutoff 建 eligible） |
| **強制重算** | `PARALLEL_LDA_MVP_FORCE_RECOMPUTE` 設為 `1` / `true` / `yes` 時，略過**月級**快取跳過（強制重跑該月 preprocess／split／run_fact／trip） |

與 trainer 本機慣例對齊：`data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`。

## 產出

- **共用**：`data/parallel_lda_mvp/<source_snapshot_id>/eligible_player_ids.parquet`（整段 span 只建一次）
- **按月**：`data/parallel_lda_mvp/<source_snapshot_id>/gaming_ym=YYYY-MM/` 底下固定 **4 個子目錄**（降低「一日期一資料夾」的路徑數與列目錄成本）：
  - `t_bet/`：**整月**一次 preprocess 產物 `cleaned_month__YYYY-MM.parquet` + manifest；再 **DuckDB 僅拆分**（不重跑 dedup／patch）成每日 `cleaned__YYYY-MM-DD.parquet`（無 manifest，供 `run_fact` 讀取）。
  - `run_fact/`：`run_fact__YYYY-MM-DD.parquet` + 同 stem 的 `.manifest.json`
  - `trip_fact/`：`trip_fact__YYYY-MM-DD.parquet` + manifest
  - `trip_run_map/`：`trip_run_map__YYYY-MM-DD.parquet` + manifest
  - 另有 `mvp_summary.json`（含 `preprocess_month_batch_stamp`，與 `pipelines...PREPROCESS_MONTH_BATCH_STAMP` 對齊；**遞增該常數**可在 L0 未變時強迫重跑整月 preprocess）。
- **暫存**（可刪）：`.../<snap>/.mvp_scratch/`（計算月／日 slice 內容 hash 時的臨時 Parquet）

## 已跑過的月份何時會跳過

每個 `gaming_ym` 目錄下的 `mvp_summary.json` 會記錄用於月級快取的欄位：

- `t_bet_month_content_sha256`（64 hex）：L0 **該月** slice 的內容指紋（與整月 preprocess 的 WHERE 一致）。
- `mapping_cache_fingerprint`、`ingest_yaml_content_sha256`：同上版說明。
- `preprocess_month_batch_stamp`：字串，對應 `pipelines.layered_data_assets.core.preprocess_bet_v1.PREPROCESS_MONTH_BATCH_STAMP`；**變更整月 batch 語意或拆分欄位／排序**時在該檔遞增，可在 **L0 位元組不變** 下強迫重跑 preprocess+split。

再次執行時，若以上四項與本次一致，且未設 `PARALLEL_LDA_MVP_FORCE_RECOMPUTE`，則 **跳過該月的 preprocess／split／run_fact／trip**（仍會對每個月做一次 L0 月 slice 的 DuckDB COPY 以算 `t_bet_month_content_sha256`；canonical / eligible 仍照常執行）。

因此：**僅新增其他月份的資料、且未改動該月 L0 slice 時**，舊月可整段跳過；**該月 L0、mapping、ingest、或 preprocess batch stamp 任一變更**則重跑該月。

### preprocess 語意（整月一次 → 再拆日）

- **整月 preprocess**：`preprocess_bet_v1 --gaming-ym YYYY-MM` 對該曆月內所有列做 **同一套** dedup／ingestion cap／eligible 等邏輯（`bet_id` 以整月為 PARTITION），確保 backfill／patch 在**完整資料**上執行。
- **拆日**：僅從 `cleaned_month__*.parquet` 依 `gaming_day` 做 `COPY … WHERE`，**不重跑** preprocess SQL；若僅調整拆分規則，可 bump `PREPROCESS_MONTH_BATCH_STAMP` 觸發重跑。
- **run_fact**：仍合併整月各日 `cleaned__*.parquet`；該月若未命中月級 skip，即整月重跑 **全部** `run_fact`。
- **trip**：在 **span 內所有月份** 的 `run_fact` 都產出後第二階段執行；每個 `trip_start_gaming_day` 的 materialize 會讀 **整段 span 的全部** `run_fact__*.parquet`，並帶固定 `--coverage-end`（span 最後一個曆日的 `gaming_day`），避免跨月 trip 被月內視角切斷。`mvp_summary.json` 會寫入 `span_run_fact_input_fingerprint`（依各日 `run_fact` 路徑+size+mtime），鄰月 `run_fact` 變更時會迫使相關月份重跑 trip。

## Rated eligible（BET-DQ-03）與 canonical 快取

**不**讀寫 trainer 的 `data/canonical_mapping.*`（避免踩別人資料）。

- 計算方式與 **trainer Step 3** 相同：`build_canonical_links_and_dummy_from_duckdb` → `build_canonical_mapping_from_links`（DuckDB links + pandas M:N）。
- **Mapping 輸入**：一律經 `parallel_lda_mvp/session_for_mapping.py` 的 `prepare_session_parquet_for_canonical_mapping`。預設若 repo 內存在 `schema/preprocess_ingestion_fix_registry.yaml`（`tables.t_session`），會物化一份含 `__etl_insert_Dtm_synthetic`（636s cap）的 Parquet 至 `canonical_cache/session_mapping_input/`；`PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE=1` 則仍回傳 raw。邏輯版本常數 `SESSION_MAPPING_CLEAN_LOGIC_VERSION`：變更清洗規則時遞增，即使 L0 檔未變也會讓指紋變、強迫重算。
- **快取只寫在** `parallel_lda_mvp/canonical_cache/`：`mapping_<sha256>.parquet` + `mapping_<sha256>.meta.json`。
- **失效條件**：對 **mapping 輸入** Parquet 做**整檔串流 SHA-256**，再與 naive-HK 的 cutoff、`SESSION_MAPPING_CLEAN_LOGIC_VERSION` 合併成最終指紋。輸入檔位元組、cutoff、清洗邏輯版本任一變更 → 重跑 DuckDB。

若要強制全重算 mapping：刪除整個 `parallel_lda_mvp/canonical_cache/` 目錄即可。

**成本**：每次執行仍會建 eligible（含 session 整檔 hash 與可能的 canonical 快取 miss）；多個月時會對**每個月**做一次該月 bet 的 DuckDB COPY 以判斷是否跳過該月管線。大檔時 I/O 與總時間會隨月份數上升。

## 記憶體

整月逐日 preprocess，且每日 `run_fact` 會餵入**整月** `cleaned.parquet` 以保留跨日 run；**預設會對 span 內每個月各跑一輪**，RAM 與時間壓力更高。必要時用 `PARALLEL_LDA_MVP_GAMING_YM` 鎖單月，或先縮小 bet 匯出。

並行 `run_fact`／`trip` 時，多個程序會同時掃描同一批 L1／run_fact 檔，RAM 與磁碟 I/O 壓力隨並行度上升；並行上限見 `parallel_lda_mvp/run_mvp.py` 內常數 `DAY_MATERIALIZE_MAX_WORKERS`（並與該月天數、`cpu_count` 取 min）。
