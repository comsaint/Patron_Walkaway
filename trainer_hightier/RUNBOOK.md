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
| Step 5 模型版本根目錄 | **`<repo>/out/models_high_tier_mvp`**（`trainer_hightier.config.DEFAULT_MODEL_DIR`，與 main `trainer` 的 `out/models` 對齊慣例；`output_dir` SSOT） |
| 單次訓練 bundle | **`out/models_high_tier_mvp/<YYYYMMDD-HHMMSS-<git7>/`**：`model.pkl`、`training_metrics.json`、同名純文字 `model_version`、`run_report.json` |
| Latest 指標 | **`out/models_high_tier_mvp/_latest_model_manifest.json`**（`trainer.core.model_bundle_paths`）；可用 `resolve_model_bundle_dir(DEFAULT_MODEL_DIR)` 解析預設「最新」`model.pkl` |
| Run 報表（Step 5 有跑時） | 同上 bundle 目錄內 **`run_report.json`**；若僅 **`--skip-step5`**（未建 bundle），則落在 **`{output_dir}/run_report.json`**（通常仍為 versions 根） |
| 快取 sidecar | 與清洗檔同目錄：`cleaned__gmwds_t_session.cache.json` |

**Step 1** 對 **session** shards 做 schema / metadata 檢查（`01_data_ingest.validate_partition_session_ingress_or_raise`）；bet 在進入 Step 2b 前同樣驗 shard。必要欄位見 `01_data_ingest._REQUIRED_SESSION_PARQUET_COLS` / `_REQUIRED_BET_PARQUET_COLS`。

## Serving（ClickHouse + SQLite，與 ``trainer`` ML API 相容）

設定集中於 ``trainer_hightier.config.HightierServingConfig``（``default_hightier_serving_config()``），含 ClickHouse 連線與 ``state.db`` / ``feature_state.db`` 路徑。

| 元件 | 啟動 |
|------|------|
| Scorer | ``python -m trainer_hightier.run_hightier_scorer``（``--once`` 單輪） |
| Validator | ``python -m trainer_hightier.run_hightier_validator`` |
| ML API | ``python -m trainer_hightier.run_hightier_api`` → ``/alerts``、``/validation``、``/health`` |
| Snapshot（每日） | ``python -m trainer_hightier.run_hightier_snapshot_updater``（``--rematerialize-slow`` 需清洗後 Parquet 輸入） |

**快照目錄**：預設 ``trainer_hightier/artifacts/serving_snapshots/active_manifest.json``；scorer 會讀其中 ``slow_patron_parquet`` 並在版本變更時寫入共用 ``state.db`` meta。

**前置**：首次部署請先跑 snapshot updater（或 ``--slow-parquet`` 指定 slow patron Parquet），並確認 canonical mapping Parquet 存在（與訓練相同路徑）。

### Serving：ADT allowlist（`high_adt_only`）

| 項目 | 說明 |
|------|------|
| **預設** | `HightierServingConfig.high_adt_only=True`（`trainer_hightier/config.py`）。Scorer 只對訓練同源 **ADT allowlist** Parquet 內的 `player_id` 做 hot 特徵與打分。 |
| **名單解析順序** | CLI `--adt-allowlist` → `active_manifest.json` 的 `adt_allowlist_parquet` → `adt_allowed_players_parquet` → `default_adt_allowed_players_parquet_path(adt_allowlist_quantile)`。 |
| **Snapshot / manifest** | `run_hightier_snapshot_updater` 會複製 allowlist 至 `artifacts/serving_snapshots/adt_allowed_players_<run_id>.parquet`，並寫入 manifest 的 `adt_allowlist_parquet` 與 `adt_allowlist_version`（目前為 **整檔 SHA-256**）。`feature_state.db` 的 `adt_allowlist_meta` 單列與之一致。整段更新在 `try` 內：**例外時不會呼叫** `publish_manifest_atomic`，避免半套切換。 |
| **訓練 hash 防線** | 若 bundle 內 `training_metrics.json` 有 `adt_allowlist_sha256`，且 `adt_allowlist_fail_on_training_hash_mismatch=True`（預設），載入名單時 **hash 不符即起動失敗**。若設為 `False`，會 **ERROR log**，`state.db` meta `adt_allowlist_health=degraded_hash_mismatch`，流程仍繼續（**不建議正式環境**）。 |
| **除錯模式** | `python -m trainer_hightier.run_hightier_scorer --no-high-adt-only` 可對**全玩家**打分；**僅限**除錯／迴歸，**不可**當正式上線模式。 |
| **可觀測** | 啟動一行 log：`high_adt_only`、`model_version`、`allowlist_path`、`allowlist_sha`、`manifest_adt_allowlist_version`；每輪過濾時 log `adt_allowlist filter rows …`。`state.db` meta：`active_adt_allowlist_sha256`、`active_adt_allowlist_version`、`adt_allowlist_health`（`ok` / `degraded_hash_mismatch` / `full_population_mode`）。 |
| **排程契約（建議）** | **Snapshot updater**：缺檔、I/O、材化失敗 → **exit 非 0**，先修資料或路徑再重跑（不宜無限重試）。**Scorer**：ClickHouse 短暫錯誤可依 poll interval 重試；allowlist ／ manifest **結構性缺失**應 **fail-fast**（由程式抛錯退出）。 |
| **回滾** | 自備份還原整份 `active_manifest.json` 及其指向的 `slow_patron_*.parquet` 與 `adt_allowed_players_*.parquet`（路徑須仍存在）；重啟 scorer。若僅要回到上一版名單，還原**整份** manifest + 兩個 parquet 指標檔，避免 slow 與 allowlist 版本錯配。 |


- **`python -m trainer_hightier.trainer` 的 Step 5** 會讀 `trainer_hightier/contracts/feature_candidate_registry.yaml`（或 `--feature-candidate-registry`），以台帳中 **可選 baseline** 欄位（`status` 為 `active|experimental` 且 `enabled_for` 含 `baseline`）作為 `feature_columns`。
- **單一真相**：baseline / candidate / ablation 選欄皆以 [`feature_candidate_registry.yaml`](trainer_hightier/contracts/feature_candidate_registry.yaml) 為準；baseline 列为 YAML 順序下 `enabled_for` 含 **`baseline`** 且 `status` 為 `active` 或 `experimental` 的列（**不可**對 `fe__*` 使用 baseline 槽）。主線 Step 5 與實驗皆由 `candidate_registry_loader` 載入。
- **`run_report.json`** 會多出 `candidate_registry`：`registry_version`、`resolved_path`、`n_baseline_features` 等；以及 **`feast_auto_apply`**：`feast_auto_apply_attempted`、`feast_apply_wall_sec`、`feast_registry_path`（Step 3 前 registry 準備紀錄），便於對齊實驗與主線。

**常見錯誤**

| 現象 | 處理 |
|------|------|
| `Registry ... baseline` / 台帳 baseline 列設定錯誤 | 編輯 `feature_candidate_registry.yaml`：`baseline` 槽只給非 `fe__*` 欄；缺欄或少欄會在載入或 Step 5 前檢查時失敗 |
| `Feature candidate registry file not found` | 確認預設檔存在或傳入正確 `--feature-candidate-registry` |
| `missing baseline columns …` / `Step 5 schema gate failed` | Step 4 splits 缺欄：重跑 Step 3/4 或改台帳不要選不存在的欄位 |

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
   - 離線載入需要 `trainer_hightier/feast_repo/data/registry.db`。主訓練與 `03_build_training_data` / `feature_experiment.run_pipeline` **預設**：若該檔不存在，會在 `trainer_hightier/feast_repo` 底下自動跑 **`feast apply`**（需 **PATH** 上有 `feast` CLI；失敗為 **fail-fast**，不會再 silent skip Step 3）。  
   - 手動跑一次亦可：`cd trainer_hightier/feast_repo && feast apply`。  
   - **關閉自動 apply**：CLI 加上 **`--disable-auto-feast-apply`**（或在程式裡對 `HighTierTrainArgs`/ `BuildTrainingDataArgs` 設 `auto_feast_apply=False`）：缺 registry 時直接報錯，適合 CI、唯讀工作複本或未安裝 CLI 的情境（須事前備好 registry 或改用 `feast apply` 離線準備）。

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
| `--disable-auto-feast-apply` | 缺 `feast_repo/data/registry.db` 時不自動跑 `feast apply`，立刻失敗（適用 CI／唯讀／已離線準備 registry） |
| `--skip-walkaway-labels` | 不物化 `walkaway_labels.parquet`（大表時可省時間；Step 3 需標籤則勿用或自行準備） |
| `--feature-candidate-registry` | Step 5 baseline 欄位來源之台帳 YAML（預設 `trainer_hightier/contracts/feature_candidate_registry.yaml`） |
| ~~`--no-partition-snapshot`~~ | **已廢止**；若指定會 `ValueError` |

跑完後請檢查：

- **訓練集**：`trainer_hightier/artifacts/training_data/training_set.parquet`  
- **版本保留**：`trainer_hightier/artifacts/training_data/versions/training_set_*.parquet`（預設保留最近 10 個；`03_build_training_data.py` 的 `--training-retention`）  
- **Run 報表**：成功且 Step 5 有跑時在 **`out/models_high_tier_mvp/<model_version>/run_report.json`**；內含 `model_version`、`model_bundle_dir`、`partition_snapshot_dir_effective`、`partition_inventory_baseline_path`、耗時、cache hit、partition fingerprint、`partition_recompute_months`、`feast_auto_apply` 等

### 2.3 僅重跑 Step 3（preprocess 已完成時）

```bash
cd trainer_hightier
python 03_build_training_data.py --materialize-derived --feast-batch-by-month
```

- **`--feast-batch-by-month`**：依 `prediction_visible_ts_cf` 的**日曆月**分批拉 Feast，降低單次 `entity_df` 體積（筆電較友善）。  
- 路徑預設指向 `artifacts/cleaned/...`、`artifacts/labels/walkaway_labels.parquet`；需與你本機產物一致。缺 **`feast_repo/data/registry.db`** 時，此腳本**預設**會自動於 `trainer_hightier/feast_repo` 跑 `feast apply`（`--disable-auto-feast-apply` 改為立刻失敗）。

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
| `--disable-auto-feast-apply` | Step 3 缺 `registry.db` 時不自動 `feast apply`（fail-fast）；預設會自動 apply |
| `--skip-bet-preprocess` | 略過 Step 2b（不重算 cleaned t_bet；需預設 cleaned 路徑已有資料集） |

主 CLI **不再**透過 `HighTierTrainArgs.data_dir` 讀 monolith；若要改 snapshot 根目錄請用 **`--partition-snapshot-dir`**，或於程式中設定 `HighTierTrainArgs.partition_snapshot_dir`。DuckDB、`run_profile` 等仍透過 `HighTierTrainArgs` / `configs_from_run_profile` 注入。

**注意：**若要在 CLI 調 DuckDB／dedup 等，需自行包一層腳本或擴充 `trainer.main()` 對 `configs_from_run_profile` 的用法。

### 3.1 MLflow（訓練結果紀錄）

| 項目 | 說明 |
|------|------|
| 程式進入點 | `trainer_hightier/trainer.py` → `run_training()` |
| 共用_helper | `trainer/core/mlflow_utils.py`（`safe_start_run`、`log_*_safe`；不可用時 no-op） |
| Experiment 常數 | `trainer_hightier.config.MLFLOW_EXPERIMENT_TRAIN_HIGHTIER` |
| Run artifacts 目錄前綴 | `trainer_hightier.config.MLFLOW_HIGHTIER_ARTIFACT_PREFIX`（預設 `hightier_run/`） |
| 環境 | `credential/mlflow.env` 或 `local_state/mlflow.env`（載入 `MLFLOW_TRACKING_URI` 等；見 main trainer 慣例） |

每次完整跑會上傳**最小白名單**（檔案存在才傳）：bundle 內 **`run_report.json`**、**`training_metrics.json`**、**`model.pkl`**，以及 **`step4_split_report`** 指向之 `split_report.json`。**不**整包上傳 `artifacts/` 或大 Parquet，避免筆電/網路耗時。

**查核清單（成功）**

- [ ] MLflow UI / API 可見單一 run，`tags.status` 終態為 **`SUCCESS`**。
- [ ] Params 含 `pipeline=trainer_hightier`、`run_profile`（對應 `--run-profile`）、`feature_candidate_registry_path`；Step 5 成功時多有 `model_version`、`model_bundle_dir`。
- [ ] Metrics 含 `step5_seconds` 與 val/test 品質指標（來源：`run_report.json` 的可數值化欄位）。
- [ ] Artifacts 下 `hightier_run/` 至少含 `run_report.json`；Step 5 有跑時應另有 metrics JSON 與 model pickle。

**查核清單（失敗）**

- [ ] 訓練仍 **fail-fast**（程式結束碼反映錯誤）。
- [ ] Run（若 tracking 可用）具 **`FAILED`** 與 `error` tag（截斷至約 500 字元）。

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
from trainer_hightier.config import DEFAULT_MODEL_DIR, DuckDbRuntimeConfig, SessionPreprocessConfig
from trainer_hightier.trainer import HighTierTrainArgs, run_training

args = HighTierTrainArgs(
    output_dir=DEFAULT_MODEL_DIR,
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

## 5. Feature Candidate Registry（`feature_experiment`）

`python -m trainer_hightier.feature_experiment.run_pipeline` 的 **baseline / candidate / ablation 選欄**以台帳 YAML 為準（v0 只做 **selection**，不做 SQL 生成）。  
**主 trainer**（`python -m trainer_hightier.trainer`）Step 5 亦讀同一份檔案，但僅使用 **baseline** slot 欄位；見上文 **§1.1**。

| 項目 | 說明 |
|------|------|
| **預設台帳** | `trainer_hightier/contracts/feature_candidate_registry.yaml` |
| **實驗設定覆寫** | 在 `feature_experiment/experiment_config.yaml` 設 `feature_candidate_registry: <path>`（可省略＝用預設） |
| **報表追溯** | `feature_experiment_report.json`（或 run 目錄內之 report）的 `candidate_registry`、`feast_auto_apply`（若非 `--skip-step3`：`feast_auto_apply_attempted`、路徑等；`skip_step3` 時為 `skipped`） |

**語意契約不重複**：`bet__*` / `patron__*` 仍以 `contracts/trial_bet_behavior_1h_features.yaml`、`slow_patron_180d_monthly_features.yaml`、與 `time_semantics_and_feast_mapping.md` 為準；台帳只記 **status / enabled_for / drop_reason_code / 實驗註記**。

### 每輪實驗後維護台帳（建議流程）

1. **確認欄位可選性**：調整 `status` 與 `enabled_for`（`baseline` | `candidate` | `ablation`）；停用欄位設 `disabled`。
2. **停用時必填** `drop_reason_code`（例如治理移除：`governance_fqg_warn_removed`，或自訂短碼）。
3. **可追溯**：更新 `last_updated_experiment`、`note`（可選填 `first_seen_experiment`）；並視需要修改檔頭 `updated_at` / `registry_version`。
4. **再啟用**：改回 `active` 或 `experimental`，清空或保留 `drop_reason_code`（非 `disabled` 時允許為 `null`），並確認仍在 `FEATURE_GROUP_TAGS` 對應之 `group_id` 下列對 `feature_id`。

變更台帳後建議跑：`python -m pytest trainer_hightier/tests/test_candidate_registry_loader.py trainer_hightier/tests/test_feature_experiment_ablation.py -q`。

## 6. Preprocess disk cache（session / bet）

- **命中條件：**清洗目標 Parquet 已存在，且 sidecar JSON 與 `build_session_clean_cache_record()` 計出的指紋一致（含來源 `mtime`/`size`、列數 metadata、`session_l0_preprocess` 模組 hash、**合併後的 session shard 路徑清單** 與 **partition inventory fingerprint**）。Bet 清洗對應 `bet_l0_preprocess` 之 `build_bet_clean_cache_record()` / `build_bet_base_clean_cache_record()` 與側車（含 base vs segment、inventory fingerprint、**ADT allowlist 之 distinct `player_id` 集合 hash**，**不依** allowlist 檔案 mtime）。
- **Bet 與 cleaned session：**bet 快取指紋**不**再綁定 `cleaned__gmwds_t_session.parquet` 的檔案統計。若磁碟上的舊 sidecar 仍含 `cleaned_session_dependency` 欄位，命中比對時會忽略該欄；`manifest_version` **8↔9** 在比對時會正規化為同一版本，以便升級後仍可回收舊 cache（其餘語義欄位須一致）。
- **失效：**來源 Parquet 或 registry 變更、或 `bet_l0_preprocess` / `session_l0_preprocess` 模組內容變更（SHA-256）時通常會 miss；session 檔遺失**不應**單獨導致 bet cache miss。改 `DuckDbRuntimeConfig` **不一定**讓指紋變——若仍要強制重跑，請用 `--ignore-caches` 或手動刪除 cleaned 與對應 `.cache.json`。
- **強制重算：**`--ignore-caches`（或 `--no-cache`）；大 L0 上可能耗時與 I/O 明顯；同一旗標同時作用於 session 與 bet preprocess cache。

## 7. 常見問題

| 現象 | 建議 |
|------|------|
| DuckDB OOM 或程序被殺 | 降低 `memory_limit`、設定 `temp_directory` 到有足夠空間的磁碟、或改用較小批次 / `pandas_shards` + 較小 `row_groups_per_shard`（仍須注意 pandas 峰值） |
| `pandas_shards` 很慢、暫存目錄爆量 | 正常：多顆 shard + merge；可改 `engine="duckdb"` 或調整 `row_groups_per_shard` |
| 找不到 session shard / `Partition ingress requires…` | 確認 snapshot 根目錄（預設 `<repo>/data/partitions`）內**遞迴**可掃到至少一個 `t_session__part_YYYYMM.parquet`；若檔案只在子目錄，請保留子目錄結構或改 `--partition-snapshot-dir` 指到實際根 |
| 與主 `trainer` DuckDB 行為不一致 | 預期內：本套件刻意**不**讀 `trainer.core` 的 DuckDB 設定；數值對齊需對照雙邊 SQL / 測試 |
| `feast` CLI 缺失或自動 `feast apply` 失敗、`registry still missing` | 於 `trainer_hightier/feast_repo` 手動 `feast apply` 並確認 `data/registry.db`；錯誤訊息含命令與 stderr 摘要。CI／唯讀環境可先備好 registry 並加 **`--disable-auto-feast-apply`** |
| 多 shard **union** 後 OOM 或極慢 | DuckDB 會讀 **多檔 union**；請調低 `memory_limit`、指定夠大的 `temp_directory`，或縮小 snapshot 範圍／調高 `dedup_hash_buckets`（見 `config.py` run profile）／分批匯出 |

## 8. 測試與除錯

```bash
python -m pytest trainer_hightier/tests/ -q
```

日誌：`logging.getLogger("trainer_hightier")`；`trainer` 模組 `main()` 已 `basicConfig` INFO。

## 9. 其他 CLI：`python -m trainer_hightier`

`__main__.py` 為 **precision floor 合成 demo**，不依賴 Parquet；勿與 `python -m trainer_hightier.trainer` 混淆。
