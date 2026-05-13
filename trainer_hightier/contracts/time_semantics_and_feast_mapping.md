# 時間語意與 Feast 時間欄位對照（trainer_hightier）

本文件為**實作契約草案**：跨表可複用的「事件／可觀測／反事實可視」欄位、`t_bet` 的具體公式，以及對應 Feast `DataSource` / `entity_df` 的設定。**數值預設**須與主專案設定一致：

- `BET_AVAIL_DELAY_MIN`：`trainer/core/_config_training_domain.py`
- `SCORER_POLL_INTERVAL_SECONDS`：`trainer/core/_config_serving_runtime.py`

若 trainer_hightier 內另建單一設定檔，應與上列來源對齊或明確 re-export，避免雙軌漂移。

---

## 1. 設計目標

1. **業務事件時間**不變：仍用來源表之「事件完成時間」排序與稽核。
2. **可觀測性（training / PIT）**與 **scorer 輪詢**一致：同一筆樣本的 `entity_df.event_timestamp` 應代表「在此時間點（含）之後的 PIT join 可合法使用之特徵狀態」，且與「每 N 秒一輪、且 bet 須過 `payout_complete_dtm <= now - BET_AVAIL_DELAY`」的語意相容。
3. **大量 backfill**：以 **synthetic observed-at**（例如 `__etl_insert_Dtm_synthetic`）表達反事實「無嚴重管線故障時」之可見序列；**raw `__etl_insert_Dtm` 保留為稽核欄**，可不作 Feast `timestamp_field`。

---

## 2. 跨表通用欄位（建議命名）

每張進 Feast 的實體表（或 feature 來源表）至少具備下列語意（實際欄名可依表前綴調整，但語意需一致）：

| 邏輯欄 | 用途 | Feast 是否必備 |
|--------|------|----------------|
| `event_time_raw` | 業務事件完成時間 | 建議保留為 **feature 欄或 metadata**，非 PIT 截止主欄 |
| `observed_at_raw` | 真實入倉／可追蹤時間（如 `__etl_insert_Dtm`） | 可選：`created_timestamp_column`（**as_observed** 視圖） |
| `observed_at_synthetic` | 經 cap／registry 校正後的邏輯可觀測時間 | 可作 PIT 主時間之輸入分量（**counterfactual** 視圖） |
| `avail_delay` | 與該表對齊的「事件後至少等待多久才算可服務」（分鐘或 interval） | 設定層常數，宜與 scorer / trainer 共用 |
| `poll_interval_sec` | 輪詢週期（秒） | 設定層常數，對齊 scorer |
| `prediction_visible_ts_cf` | **反事實＋服務語意**下「第一次允許當成預測時點快照」的時間 | 建議作 **`timestamp_field`**（counterfactual stack） |

**`prediction_visible_ts_cf` 通用定義（概念）：**

1. `available_cf_base = max(observed_at_synthetic, event_time_raw + avail_delay_interval)`
2. `prediction_visible_ts_cf = align_poll_ceiling(available_cf_base, poll_interval_sec)`

其中 `align_poll_ceiling` 表示以 Unix epoch 秒對齊之「不低於 `available_cf_base` 的第一個輪詢邊界」：

\[
\text{prediction\_visible\_ts\_cf} = \text{to\_timestamp}\Big(\lceil \text{epoch}(\text{available\_cf\_base}) / \text{poll\_interval\_sec} \rceil \times \text{poll\_interval\_sec}\Big)
\]

若未來 scorer 改為「非對齊 epoch 的相位偏移」，本函式應一併更新並在此文件舊版註記。

---

## 3. `t_bet`（清洗後）具體對照

### 3.1 欄位對應

| 通用邏輯欄 | `t_bet` / cleaned 建議欄名 |
|------------|---------------------------|
| `event_time_raw` | `payout_complete_dtm` |
| `observed_at_raw` | `__etl_insert_Dtm` |
| `observed_at_synthetic` | `__etl_insert_Dtm_synthetic`（與 `schema/preprocess_l0_data_contract_registry.yaml` 之 cap 一致） |
| `avail_delay` | `BET_AVAIL_DELAY_MIN` 分鐘 |
| `poll_interval_sec` | `SCORER_POLL_INTERVAL_SECONDS`（預設 45） |

### 3.2 DuckDB 表達式（範例）

參數：`bet_avail_delay_min`（int）、`poll_sec`（int），時間皆為可比較之 `TIMESTAMP`（建議統一 UTC 或 HK，與 scorer 一致後再比較）。

```sql
-- Step 1: 事件後可服務下界（對齊 scorer 對 payout 的 gating）
available_after_payout AS (
  TRY_CAST(payout_complete_dtm AS TIMESTAMP)
    + INTERVAL (bet_avail_delay_min) MINUTE
),

-- Step 2: 反事實可見下界（synthetic 與 payout 後下界取較晚）
available_cf_base AS (
  GREATEST(
    TRY_CAST(__etl_insert_Dtm_synthetic AS TIMESTAMP),
    TRY_CAST(payout_complete_dtm AS TIMESTAMP)
      + INTERVAL (bet_avail_delay_min) MINUTE
  )
),

-- Step 3: 對齊輪詢邊界（epoch 秒 ceil）
prediction_visible_ts_cf AS (
  to_timestamp(
    CEIL(EPOCH(available_cf_base) / poll_sec) * poll_sec
  )
)
```

> **記憶體／時間**：上式為每列純量運算，適合在 materialized Parquet 上一次算好；避免在超大表上反覆於 Feast 查詢內嵌複雜運算導致離線 job 膨脹。

### 3.3 Feast（counterfactual 訓練 stack）

| Feast 概念 | 建議對應 |
|------------|----------|
| `DataSource.timestamp_field` | `prediction_visible_ts_cf` |
| `DataSource.created_timestamp_column` | `__etl_insert_Dtm_synthetic`（與主時間同軸之反事實；同 event 點多版本時去重） |
| `entity_df.event_timestamp` | 與訓練標籤列對齊之「決策時點」：通常取該樣本對應之 `prediction_visible_ts_cf` 或業務定義之 snapshot 時間（必須 **≤** 特徵可見時間之語意與文件一致） |
| 保留於 schema、作特徵或稽核 | `payout_complete_dtm`、`__etl_insert_Dtm`、`ingestion_episode_id`（若有） |

### 3.4 雙軌（可選）

| 類型 | `timestamp_field` | `created_timestamp_column` | 用途 |
|------|---------------------|---------------------------|------|
| **as_observed** | `__etl_insert_Dtm` 或仅 `available_after_payout` 與 raw 組合之欄位（需另定名與文件） | `__etl_insert_Dtm` 或省略 | 回放真實管線延遲、監控與對照实验 |
| **counterfactual** | `prediction_visible_ts_cf` | `__etl_insert_Dtm_synthetic` | 與「無大規模 backfill 異常」之訓練目標對齊 |

---

## 4. 其他表（`t_session` 等）套用方式

1. 在 registry／time_semantics 中宣告每表之 `event_time_col`、`observed_at_col`、`ingest_delay_cap_sec`（或等價 synthetic 規則）。
2. 將本文件第 2 節之 `event_time_raw` / `observed_at_synthetic` 映射到該表欄名。
3. **服務延遲**：若該表在 scorer 中有獨立 `*_AVAIL_DELAY_*`（例如 session 之 `SESSION_AVAIL_DELAY_MIN`），則 `available_cf_base` 中與 **事件時間** 相加的 interval 應改用該表常數，而非一律使用 `BET_AVAIL_DELAY_MIN`。
4. 若某表 **不**經輪詢發現、或輪詢與 bet 不同，可將 `poll_interval_sec` 設為 1 或改用實際排程粒度，並在文件中註記例外。

---

## 5. TTL（FeatureView）提醒

- Feast 之 `ttl` 為相對 **每列 entity_df 之 `event_timestamp`** 往回搜尋之上界，與「現在」無關。
- `prediction_visible_ts_cf` 若晚於 raw 事件許久，過短的 `ttl` 可能無法 join 到足夠歷史特徵；過長則離線查詢成本與記憶體壓力上升。**宜按特徵家族分視圖設定 ttl**，並在筆電類環境監控離線 job 大小。

---

## 6. Feast 倉庫路徑與指令

- **目錄**：`trainer_hightier/feast_repo/`（`feature_store.yaml`、`definitions.py`）。
- **離線 store**：`feature_store.yaml` 使用 `offline_store.type: duckdb`（Ibis + 本機 Parquet；依賴根目錄 `requirements.txt` 的 `ibis-framework[duckdb]`）。`staging_location` 預設為 `trainer_hightier/tmp/feast_duckdb_staging`（相對於 `feast_repo` 的 `../tmp/feast_duckdb_staging`），大表離線 join 時可避免預設暫存磁碟不足。
- **清洗檔**：需含 `prediction_visible_ts_cf` 的 `trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet.parquet`；請在更新 preprocess 後重跑清洗。
- **CLI**（在 `feast_repo` 下）：
  - `feast plan`：完整檢核來源時，若 Parquet 仍缺 `prediction_visible_ts_cf` 則會失敗；可暫用 `feast plan --skip-source-validation` 僅驗證 registry 定義。
  - `feast apply`：寫入 registry／online schema；請在來源 Parquet 已對齊欄位後再執行，以便通過檢核與後續 `get_historical_features`。
- **`gaming_day`**：目前 **未** 納入 `definitions.py` 的 explicit schema（避免 `date32` inference 問題）；需要時請在 preprocess 衍生字串／數值欄並加進 FeatureView。

---

## 7. 變更紀錄

- 2026-05-13：初版草案（trainer_hightier／Feast 對齊 scorer 輪詢與 `BET_AVAIL_DELAY_MIN`）。
- 2026-05-13：離線 store 改為 `duckdb` + `ibis-framework[duckdb]`，並設定 `staging_location`。
