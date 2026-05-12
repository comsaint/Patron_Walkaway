# trainer_hightier 運維手冊（RUNBOOK）

給要**跑離線管線、除錯或調資源**的人用；產品規格仍以程式與測試為準。

## 1. 資料與路徑

| 項目 | 預設 / 慣例 |
|------|-------------|
| 資料根目錄 | `<repo>/data`（`01_data_ingest.default_data_dir()`） |
| Session L0 | `{data_dir}/gmwds_t_session.parquet`（**必要**） |
| Bet L0 | `{data_dir}/gmwds_t_bet.parquet`（Step 1 session 流程**不要求**存在；完整雙表驗證用其他 API） |
| 清洗輸出 | `trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_session.parquet` |
| 快取 sidecar | 與清洗檔同目錄：`cleaned__gmwds_t_session.cache.json` |

**Step 1** 會對 session Parquet 做 schema / metadata 層級檢查；缺欄或缺檔會直接 raise。完整欄位清單見 `01_data_ingest._REQUIRED_SESSION_PARQUET_COLS`。

## 2. 執行訓練骨架（session 清洗）

```bash
python -m trainer_hightier.trainer --data-dir /path/to/data
```

| 旗標 | 用途 |
|------|------|
| `--data-dir` | 覆寫資料目錄（省略則用 repo 下 `data/`） |
| `--output-dir` | 執行輸出根目錄（預設 `.data/trainer_hightier/run`；目前骨架對 session 清洗路徑影響有限，仍以 `02_preprocess` 預設 cleaned 路徑為主） |
| `--no-cache` | **強制**重算清洗結果，忽略 session clean cache |
| `--random-seed` | 預留給未來訓練步驟 |

**注意：**`duckdb_runtime` 與 `session_preprocess` 的細項目前僅能透過 `HighTierTrainArgs` 在程式內設定；若要在 CLI 調 DuckDB，需擴充 `trainer._build_argparser()` 或包一層呼叫腳本。

## 3. Session 清洗引擎與記憶體

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

## 4. Session clean cache

- **命中條件：**清洗目標 Parquet 已存在，且 sidecar JSON 與 `build_session_clean_cache_record()` 計出的指紋一致（含來源 `mtime`/`size`、列數 metadata、`02_preprocess.py` 原始碼 hash 等）。
- **失效：**來源 `gmwds_t_session.parquet` 變更、或 preprocess 邏輯變更（模組 hash 變）通常會 miss；改 `DuckDbRuntimeConfig` **不一定**會讓指紋變——若語意相同但想強制重跑，請用 `--no-cache` 或刪除 cleaned + sidecar。
- **強制重算：**`--no-cache`；大 L0 上可能耗時與 I/O 明顯。

## 5. 常見問題

| 現象 | 建議 |
|------|------|
| DuckDB OOM 或程序被殺 | 降低 `memory_limit`、設定 `temp_directory` 到有足夠空間的磁碟、或改用較小批次 / `pandas_shards` + 較小 `row_groups_per_shard`（仍須注意 pandas 峰值） |
| `pandas_shards` 很慢、暫存目錄爆量 | 正常：多顆 shard + merge；可改 `engine="duckdb"` 或調整 `row_groups_per_shard` |
| 找不到 session 檔 | 確認 `--data-dir` 與 `gmwds_t_session.parquet` 檔名、路徑 |
| 與主 `trainer` DuckDB 行為不一致 | 預期內：本套件刻意**不**讀 `trainer.core` 的 DuckDB 設定；數值對齊需對照雙邊 SQL / 測試 |

## 6. 測試與除錯

```bash
python -m pytest trainer_hightier/tests/ -q
```

日誌：`logging.getLogger("trainer_hightier")`；`trainer` 模組 `main()` 已 `basicConfig` INFO。

## 7. 其他 CLI：`python -m trainer_hightier`

`__main__.py` 為 **precision floor 合成 demo**，不依賴 Parquet；勿與 `python -m trainer_hightier.trainer` 混淆。
