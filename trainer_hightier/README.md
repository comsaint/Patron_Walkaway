# trainer_hightier

高階客群（high-tier patron）目標的**精簡離線管線**：與主套件 `trainer/` 分離，自行持有設定與 DuckDB 執行參數，不依賴 `trainer.core` 的 DuckDB 記憶體政策。

## 範圍（目前狀態）

| 區塊 | 說明 |
|------|------|
| **資料進場** | 本地 `gmwds_t_session.parquet`（必要）、`gmwds_t_bet.parquet`（後續步驟再驗證） |
| **Session 清洗** | L0 → 清洗後 Parquet（DuckDB 單段為預設；可選 pandas 分片後再 DuckDB merge） |
| **訓練 / 特徵** | 多為 skeleton（`fit_model`、`write_artifacts` 等仍待實作） |
| **評估 demo** | `python -m trainer_hightier` 為合成資料的 precision floor 示範，**不是**完整訓練 CLI |

詳細操作、快取與除錯請見 [RUNBOOK.md](./RUNBOOK.md)。

## 目錄結構（精要）

```
trainer_hightier/
  config.py              # DuckDbRuntimeConfig、SessionPreprocessConfig、HighTierObjectiveConfig
  01_data_ingest.py      # 路徑解析、session 進場 QC（metadata / schema）
  02_preprocess.py       # session / bet 清洗 facade（DuckDB streaming）
  03_build_training_data.py  # Step 3：Feast 離線特徵 + labels → training Parquet
  trainer.py             # HighTierTrainArgs、run_training（主流程骨架）
  eval.py                # precision floor 報告（demo 與單元測試會用到）
  utils/                 # 非步驟共用工具（例如 DuckDB PRAGMA 套用）
  artifacts/cleaned/     # 預設輸出：cleaned__gmwds_t_session.parquet（由程式建立）
  artifacts/training_data/  # `03_build_training_data` 預設輸出：`training_set.parquet`
  tests/                 # pytest
```

## 快速開始

1. 在資料目錄放置 **`gmwds_t_session.parquet`**（預設路徑：`<repo>/data/`）。
2. 執行訓練骨架（會跑 Step 1–2，寫入預設清洗 Parquet）：

```bash
python -m trainer_hightier.trainer
```

可選：`--ignore-caches`（等同 `--no-cache`）— 略過 session/bet 預處理磁碟快取並強制重算；`--skip-training-dataset` — 不執行 Step 3（預設**會**跑 Feast + labels → `artifacts/training_data/training_set.parquet`）；`--skip-training-materialize-derived` — Step 3 內不重算 trial 1h / slow 180d 物化檔（若已存在且想省時間可加）。其餘為程式預設（資料目錄為 `<repo>/data`、`config.DEFAULT_RUN_PROFILE_NAME` 等）。

3. （可選）在 **已** `feast apply`、具 cleaned bet 與 `artifacts/labels/walkaway_labels.parquet` 後，匯出訓練表：

```bash
python -m trainer_hightier.03_build_training_data
```

4. 執行評估 demo（與上面 **不同** 的入口）：

```bash
python -m trainer_hightier
```

## 設定（僅限本套件）

- **`DuckDbRuntimeConfig`**（`config.py`）：`memory_limit`、`temp_directory`、`threads`。透過 `trainer_hightier.utils.duckdb_runtime.apply_duckdb_runtime_pragmas` 套在連線上；**不**讀取 `trainer.core.config`。
- **`SessionPreprocessConfig`**：`engine`（`duckdb` | `pandas_shards`）、`row_groups_per_shard`（僅 pandas 分片路徑）。
- **`HighTierTrainArgs`**：程式化執行時可覆寫各欄；CLI 僅暴露是否略過預處理快取（`--ignore-caches`）。

預設清洗輸出路徑由 `02_preprocess.default_cleaned_session_parquet_path()` 決定（`trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_session.parquet`）。

## 依賴

與主專案共用 `requirements.txt` 中的 **duckdb**、**pandas**、**pyarrow** 等；本目錄不再重複維護獨立 manifest。

## 測試

```bash
python -m pytest trainer_hightier/tests/ -q
```

推送前請依團隊規範執行 ruff / mypy 等（本 README 不代為執行）。
