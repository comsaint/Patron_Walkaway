# trainer_hightier 運維手冊（RUNBOOK）

給要**跑離線管線、除錯或調資源**的人用；產品規格仍以程式與測試為準。

## 1. 資料與路徑

| 項目 | 預設 / 慣例 |
|------|-------------|
| **L0 來源（`python -m trainer_hightier.trainer`）** | **僅** partition snapshot：目錄內（**遞迴**）所有符合檔名的 `t_session__part_YYYYMM.parquet`、`t_bet__part_YYYYMM.parquet`。不再讀取 `<repo>/data/gmwds_t_session.parquet` 或 `gmwds_t_bet.parquet` 單檔。 |
| Snapshot 根目錄 | 預設 **`<repo>/data/partitions`**（須為已存在目錄）。可改 **`--partition-snapshot-dir <dir>`**（同樣須已存在）。子目錄內的 shard 會一併掃到（例如 `data/partitions/20260512/t_session__part_202501.parquet`）。 |
| 清洗輸出 | `trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_session.parquet`（檔名沿用歷史慣例；內容來自**合併後**的 session shards） |
| Bet base（ADT 路徑） | `trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet_base.parquet`（全玩家；segment 前） |
| Bet segment 輸出 | `trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet.parquet` |
| 訓練集 | `trainer_hightier/artifacts/training_data/training_set.parquet`（另見 `versions/training_set_<UTC>.parquet` 版本檔） |
| Run 報表 | `{output_dir}/run_report.json`（預設 `.data/trainer_hightier/run/run_report.json`） |
| 快取 sidecar | 與清洗檔同目錄：`cleaned__gmwds_t_session.cache.json` |

**Step 1** 對 **session** shards 做 schema / metadata 檢查（`01_data_ingest.validate_partition_session_ingress_or_raise`）；bet 在進入 Step 2b 前同樣驗 shard。必要欄位見 `01_data_ingest._REQUIRED_SESSION_PARQUET_COLS` / `_REQUIRED_BET_PARQUET_COLS`。

**其他模組**（例如 `validate_session_ingress_or_raise` + monolith 路徑）仍保留在 `01_data_ingest.py` 供測試或舊腳本；**主訓練 CLI 已不走該路徑**。

## 2. 從 raw Parquet snapshot 目錄跑到訓練集（建議閱讀順序）

以下假設在**儲存庫根目錄**執行（`Patron_Walkaway`），且 Python 已能 import 本專案。

### 2.0 分區路徑與 inventory baseline（預設行為）

- **Snapshot 目錄（`--partition-snapshot-dir`）**  
  - **省略時**：必須存在 **`<repo>/data/partitions`**（錨在儲存庫根，非 shell CWD）；**不存在則程式立即失敗**（`FileNotFoundError`）。  
  - **自訂**：`--partition-snapshot-dir /path/to/my_shards`（**該路徑必須已存在且為目錄**，否則起動即失敗）。  
  - **掃描**：對該根目錄 **遞迴** 尋找 `*.parquet`，檔名需符合 `t_bet__part_YYYYMM.parquet`、`t_session__part_YYYYMM.parquet`。  
  - **`--no-partition-snapshot`**：**已廢止**。若帶此旗標會直接 `ValueError`（本管線僅支援 partition）。

- **`--partition-inventory-previous` 是什麼？**  
  它是給 **「inventory diff」** 用的**上一版 JSON**：用來計算 `recompute_months`（哪些月份的分區檔相對於上次有新增或變更）。**不**等於「自動沿用所有已算好的 cleaned Parquet」——後者仍靠 **session/bet 的 `.cache.json` 指紋**（含 partition fingerprint、來源 mtime 等）決定是否跳過 preprocess。  

- **想盡量重用過去產物時的建議預設**  
  1. **不必手動帶** `--partition-inventory-previous`：若本次 snapshot 資料夾的** basename** 與上次相同（例如都用 `data/partitions`），且 `trainer_hightier/artifacts/manifests/partition_inventory_<basename>.json` **已存在**，程式會**自動**拿它當 baseline 做 diff，讓 `recompute_months` 較精準。  
  2. 若你改用了**不同資料夾名稱**，但仍想對齊某份舊 manifest，再**明確指定** `--partition-inventory-previous path/to/partition_inventory_xxx.json`。  
  3. 第一次跑、尚無任何 inventory 檔時：baseline 為空，行為等同「無舊版可比」；preprocess 是否重跑仍由 **cache manifest** 決定。

### 2.1 前置條件

1. **Partition snapshot（唯一 L0 來源）**  
   - 至少一個 **`t_session__part_YYYYMM.parquet`**（Step 1 / session 清洗必要）。  
   - **`t_bet__part_YYYYMM.parquet`**：若要跑 Step 2b bet 清洗、Step 2c labels、Step 3，目錄內須有可掃到的 bet shard；若僅有 session shard，bet 相關步驟會 skip（見 log）。  
   - 各 shard **schema 須一致**（與第一個掃到之檔比對）；檔名 `YYYYMM` 須六位數；拒絕 `.gstmp` 未完成檔。

2. **Feast（Step 3）**  
   - 在 `trainer_hightier/feast_repo` 執行過 `feast apply`，且 `feast_repo/data/registry.db` 存在。  
   - 範例：`cd trainer_hightier/feast_repo && feast apply`（需已安裝 Feast CLI 與專案依賴）。  
   - 若 registry 缺失，Step 3 會被 skip（log 會說明）。

3. **Walkaway labels（Step 2c，預設開）**  
   - 需已啟用 canonical mapping + profile 等（與 `trainer.py` 內 ADT segment 邏輯一致）；否則請 `--skip-walkaway-labels` 或自行產標籤後再跑 `03_build_training_data`。

### 2.2 一鍵：preprocess → labels → Feast → 訓練集

```bash
# 預設：必須已有 <repo>/data/partitions（否則立即失敗）
python -m trainer_hightier.trainer

# 自訂 snapshot 根目錄（不要求 <repo>/data/partitions 存在；自訂路徑須為已存在目錄）
# python -m trainer_hightier.trainer --partition-snapshot-dir /path/to/export_drop

# 若要強制指定與「自動 baseline」不同的 inventory JSON（跨資料夾名稱對齊時）：
# python -m trainer_hightier.trainer --partition-inventory-previous trainer_hightier/artifacts/manifests/partition_inventory_other.json
```

- **Snapshot**：掃描後寫 `trainer_hightier/artifacts/manifests/partition_inventory_<snapshot資料夾basename>.json`，並把 fingerprint 納入 session / bet **disk cache** 指紋；日誌會列出 **recompute months**。  
- **`--partition-inventory-previous`**（選填）：手動指定 baseline JSON；**省略時**若已存在與本次 snapshot **同名**之 `partition_inventory_<basename>.json` 則自動用作 diff baseline。  
- **`--partition-correction-month YYYYMM`**（可重複）：強制把某月納入重算集合。  
- **`--partition-backfill-count N`**：每個「變更月」再往前補 N 個日曆月（預設 1）。

預設會**接著跑 Step 3**（Feast + `walkaway_labels` → `artifacts/training_data/training_set.parquet`）。若只要 preprocess：

```bash
python -m trainer_hightier.trainer --partition-snapshot-dir "D:/exports/my_snapshot_202505" --skip-training-dataset
```

（若已指定 `--partition-snapshot-dir`，則**不會**再要求 `<repo>/data/partitions` 存在，但**自訂路徑**仍須為已存在之目錄。）

其他常用旗標：

| 旗標 | 用途 |
|------|------|
| `--ignore-caches` / `--no-cache` | **強制**重跑 session / bet 預處理，略過 clean-cache manifest |
| `--skip-bet-preprocess` | 略過 Step 2b（不重算 cleaned t_bet；需 `artifacts/cleaned` 已有產物） |
| `--skip-training-dataset` | 不跑 Step 3（不產 training_set） |
| `--skip-walkaway-labels` | 不物化 `walkaway_labels.parquet`（大表時可省時間；Step 3 需標籤則勿用或自行準備） |
| ~~`--no-partition-snapshot`~~ | **已廢止**；若指定會 `ValueError` |

跑完後請檢查：

- **訓練集**：`trainer_hightier/artifacts/training_data/training_set.parquet`  
- **版本保留**：`trainer_hightier/artifacts/training_data/versions/training_set_*.parquet`（預設保留最近 10 個；`03_build_training_data.py` 的 `--training-retention`）  
- **Run 報表**：`.data/trainer_hightier/run/run_report.json`（含 `partition_snapshot_dir_effective`、`partition_inventory_baseline_path`、耗時、cache hit、partition fingerprint、`partition_recompute_months` 等）

### 2.3 僅重跑 Step 3（preprocess 已完成時）

```bash
cd trainer_hightier
python 03_build_training_data.py --materialize-derived --feast-batch-by-month
```

- **`--feast-batch-by-month`**：依 `prediction_visible_ts_cf` 的**日曆月**分批拉 Feast，降低單次 `entity_df` 體積（筆電較友善）。  
- 路徑預設指向 `artifacts/cleaned/...`、`artifacts/labels/walkaway_labels.parquet`；需與你本機產物一致。

### 2.4 DVC（可選）

在 `trainer_hightier/` 內有 `dvc.yaml`：`preprocess_skeleton`（預設帶 `--skip-training-dataset`）與 `training_dataset_build` 兩段。`preprocess_skeleton` 依賴 **`../data/partitions`**（partition snapshot 目錄）與 `feast_repo/data/registry.db` 等；請依本機調整 deps 後再 `dvc repro`。

---

## 3. 執行訓練骨架（CLI：`python -m trainer_hightier.trainer`）

```bash
python -m trainer_hightier.trainer
```

等同不帶 `--partition-snapshot-dir`：必須存在 **`<repo>/data/partitions`**，否則起動即失敗。

| 旗標 | 用途 |
|------|------|
| `--ignore-caches` / `--no-cache` | **強制**重跑 session / bet 預處理，略過兩者的 clean-cache manifest 命中（大 L0 時 I/O 與耗時明顯） |
| `--skip-bet-preprocess` | 略過 Step 2b（不重算 cleaned t_bet；需預設 cleaned 路徑已有資料集） |

主 CLI **不再**透過 `HighTierTrainArgs.data_dir` 讀 monolith；若要改 snapshot 根目錄請用 **`--partition-snapshot-dir`**，或於程式中設定 `HighTierTrainArgs.partition_snapshot_dir`。DuckDB、`run_profile` 等仍透過 `HighTierTrainArgs` / `configs_from_run_profile` 注入。

**注意：**若要在 CLI 調 DuckDB／dedup 等，需自行包一層腳本或擴充 `trainer.main()` 對 `configs_from_run_profile` 的用法。

## 4. Session 清洗引擎與記憶體

### `engine="duckdb"`（預設）

- DuckDB `read_parquet` → SQL（L0 gate、impute、synthetic、FND-01、FND-04）→ `COPY` 單一輸出 Parquet。
- 峰值 RAM 主要由 **DuckDB** 與 OS 快取決定；請用 `DuckDbRuntimeConfig.memory_limit`、`temp_directory` 控制溢出與上限。
- **不**載入整張 L0 到 pandas。

### `engine="pandas_shards"`（後備）

- 以 `row_groups_per_shard` 個 row group 為一批進 pandas → 暫存 shard Parquet → DuckDB merge。
- **峰值 pandas RAM ≈ 單批合併後的資料量**；`row_groups_per_shard` 愈大，shard 檔愈少，但單批愈吃記憶體。大檔或肥 row group 時請**降低**此值或改回 `duckdb`。

### DuckDB 參數（本套件專用）

在程式中建立：

```python
from pathlib import Path
from trainer_hightier.config import DuckDbRuntimeConfig, SessionPreprocessConfig
from trainer_hightier.trainer import HighTierTrainArgs, run_training

args = HighTierTrainArgs(
    output_dir=Path(".data/trainer_hightier/run"),
    # partition_snapshot_dir=None → 必須已有 <repo>/data/partitions；或設為已存在的 snapshot 根目錄
    partition_snapshot_dir=None,
    duckdb_runtime=DuckDbRuntimeConfig(
        memory_limit="4GB",
        temp_directory=Path("D:/duckdb_spill"),  # 可選：本機快速磁碟
        threads=4,  # 可選
    ),
    session_preprocess=SessionPreprocessConfig(engine="duckdb"),
)
run_training(args)
```

這些 PRAGMA **不會**套用主 `trainer` 管線的 `get_duckdb_memory_config`。

## 5. Preprocess disk cache（session / bet）

- **命中條件：**清洗目標 Parquet 已存在，且 sidecar JSON 與 `build_session_clean_cache_record()` 計出的指紋一致（含來源 `mtime`/`size`、列數 metadata、`session_l0_preprocess` 模組 hash、**合併後的 session shard 路徑清單** 與 **partition inventory fingerprint**）。Bet 清洗對應 `bet_l0_preprocess` 之 `build_bet_clean_cache_record()` / `build_bet_base_clean_cache_record()` 與側車（含 base vs segment、inventory fingerprint、**ADT allowlist 之 distinct `player_id` 集合 hash**，**不依** allowlist 檔案 mtime）。
- **Bet 與 cleaned session：**bet 快取指紋**不**再綁定 `cleaned__gmwds_t_session.parquet` 的檔案統計。若磁碟上的舊 sidecar 仍含 `cleaned_session_dependency` 欄位，命中比對時會忽略該欄；`manifest_version` **8↔9** 在比對時會正規化為同一版本，以便升級後仍可回收舊 cache（其餘語義欄位須一致）。
- **失效：**來源 Parquet 或 registry 變更、或 `bet_l0_preprocess` / `session_l0_preprocess` 模組內容變更（SHA-256）時通常會 miss；session 檔遺失**不應**單獨導致 bet cache miss。改 `DuckDbRuntimeConfig` **不一定**讓指紋變——若仍要強制重跑，請用 `--ignore-caches` 或手動刪除 cleaned 與對應 `.cache.json`。
- **強制重算：**`--ignore-caches`（或 `--no-cache`）；大 L0 上可能耗時與 I/O 明顯；同一旗標同時作用於 session 與 bet preprocess cache。

## 6. 常見問題

| 現象 | 建議 |
|------|------|
| DuckDB OOM 或程序被殺 | 降低 `memory_limit`、設定 `temp_directory` 到有足夠空間的磁碟、或改用較小批次 / `pandas_shards` + 較小 `row_groups_per_shard`（仍須注意 pandas 峰值） |
| `pandas_shards` 很慢、暫存目錄爆量 | 正常：多顆 shard + merge；可改 `engine="duckdb"` 或調整 `row_groups_per_shard` |
| 找不到 session shard / `Partition ingress requires…` | 確認 snapshot 根目錄（預設 `<repo>/data/partitions`）內**遞迴**可掃到至少一個 `t_session__part_YYYYMM.parquet`；若檔案只在子目錄，請保留子目錄結構或改 `--partition-snapshot-dir` 指到實際根 |
| 與主 `trainer` DuckDB 行為不一致 | 預期內：本套件刻意**不**讀 `trainer.core` 的 DuckDB 設定；數值對齊需對照雙邊 SQL / 測試 |
| 多 shard **union** 後 OOM 或極慢 | DuckDB 會讀 **多檔 union**；請調低 `memory_limit`、指定夠大的 `temp_directory`，或縮小 snapshot 範圍／調高 `dedup_hash_buckets`（見 `config.py` run profile）／分批匯出 |

## 7. 測試與除錯

```bash
python -m pytest trainer_hightier/tests/ -q
```

日誌：`logging.getLogger("trainer_hightier")`；`trainer` 模組 `main()` 已 `basicConfig` INFO。

## 8. 其他 CLI：`python -m trainer_hightier`

`__main__.py` 為 **precision floor 合成 demo**，不依賴 Parquet；勿與 `python -m trainer_hightier.trainer` 混淆。
