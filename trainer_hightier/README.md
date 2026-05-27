# trainer_hightier

高階客群（high-tier patron）目標的**精簡離線管線**：與主套件 `trainer/` 分離，自行持有設定與 DuckDB 執行參數，不依賴 `trainer.core` 的 DuckDB 記憶體政策。

## 範圍（目前狀態）

| 區塊 | 說明 |
|------|------|
| **資料進場** | **僅** partition snapshot：`<repo>/data/partitions`（預設，**遞迴**掃描）或 `--partition-snapshot-dir` 下的 `t_session__part_YYYYMM.parquet` / `t_bet__part_YYYYMM.parquet`；**不再**讀 `<repo>/data/gmwds_t_*.parquet` 單檔 |
| **Session 清洗** | L0 → 清洗後 Parquet（DuckDB 單段為預設；可選 pandas 分片後再 DuckDB merge） |
| **訓練 / 特徵** | Step 3–5 與主 CLI 可跑通；特徵選欄由 [contracts/feature_candidate_registry.yaml](./contracts/feature_candidate_registry.yaml) 驅動；**四層命名與 short PIT cache** 見 [doc/Scorer Runtime Contract - SSOT.md](./doc/Scorer%20Runtime%20Contract%20-%20SSOT.md) §特徵四層 |
| **Production serving** | Scorer v2 + Feast online mid/long；`build_deploy_package` 產出自包含 bundle；`deploy.main` 負責 startup / post-startup Feast refresh（見下文） |
| **評估 demo** | `python -m trainer_hightier` 為合成資料的 precision floor 示範，**不是**完整訓練 CLI |

詳細操作、快取與除錯請見 [RUNBOOK.md](./RUNBOOK.md)。

## 目錄結構（精要）

```
trainer_hightier/
  config.py              # DuckDbRuntimeConfig、SessionPreprocessConfig、HighTierObjectiveConfig
  01_data_ingest.py      # 路徑解析、session 進場 QC（metadata / schema）
  02_preprocess.py       # session / bet 清洗 facade（DuckDB streaming）
  03_build_training_data.py  # Step 3：Feast 離線特徵 + labels → training Parquet
  trainer.py             # HighTierTrainArgs、run_training（主訓練 CLI）
  contracts/             # 契約與台帳（含 feature_candidate_registry.yaml）
  feature_experiment/    # 離線 Gate-1 實驗管線（與主線共用同一台帳選欄）
  eval.py                # precision floor 報告（demo 與單元測試會用到）
  utils/                 # 非步驟共用工具（例如 DuckDB PRAGMA 套用）
  artifacts/cleaned/     # 預設輸出：cleaned__gmwds_t_session.parquet（由程式建立）
  artifacts/training_data/  # `03_build_training_data` 預設輸出：`training_set.parquet`
  tests/                 # pytest
```

## 快速開始

1. 在 **`<repo>/data/partitions`**（或自訂目錄）放置符合檔名的 shard；子目錄內的 parquet 也會掃到（例如 `data/partitions/20260512/...`）。至少需有 **session** shard；若要跑 bet 清洗與 Step 3，亦需 **bet** shard。
2. 執行主訓練流程（會跑 Step 1–2，寫入預設清洗 Parquet；預設續跑 Step 3–5，見 RUNBOOK）：

```bash
python -m trainer_hightier.trainer
```

可選：`--partition-snapshot-dir <已存在目錄>` — 不用預設的 `data/partitions`；`--ignore-caches`（等同 `--no-cache`）— 略過 session/bet 預處理磁碟快取並強制重算；`--skip-bet-preprocess` — 略過 Step 2b、t_bet 清洗（沿用既有 `artifacts/cleaned`）；`--disable-auto-feast-apply` — Step 3 發現缺少 `trainer_hightier/feast_repo/data/registry.db` 時**不**自動執行 `feast apply`，改為立刻失敗（CI、唯讀複本或未裝 Feast CLI 可先手動 `feast apply` 再開此旗標）；`--skip-training-dataset` — 不執行 Step 3（預設**會**跑 Feast + labels → `artifacts/training_data/training_set.parquet`，且 Step 3 前若缺 registry 會**預設**嘗試在 `feast_repo` 內自動 `feast apply`）；`--skip-training-materialize-derived` — Step 3 內不重算 trial 1h / slow 180d 物化檔（若已存在且想省時間可加）；`--feature-candidate-registry <path>` — Step 5 baseline 欄位來源之台帳 YAML（預設為 `contracts/feature_candidate_registry.yaml`）。**`--no-partition-snapshot` 已廢止**（會 `ValueError`）。其餘為程式預設（`config.DEFAULT_RUN_PROFILE_NAME` 等）。

3. （可選）具 cleaned bet、`artifacts/labels/walkaway_labels.parquet`，且可自行匯出訓練表；若尚未有 `feast_repo/data/registry.db`，此步驟**預設**會自動在 `trainer_hightier/feast_repo` 執行 `feast apply`（可加 `--disable-auto-feast-apply` 強制改為缺檔即失敗）：

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
- **`HighTierTrainArgs`**：程式化執行時可覆寫各欄（含 **`partition_snapshot_dir`**，`None` 時等同 CLI 未指定、使用 `<repo>/data/partitions`；**`feature_candidate_registry`** 為台帳路徑，`None` 用預設 `contracts/feature_candidate_registry.yaml`；**`auto_feast_apply`**：`False` 時缺 Feast registry 則不前綴自動 `feast apply`）；CLI 另暴露 partition inventory、略過快取、`--disable-auto-feast-apply` 等旗標（見 [RUNBOOK.md](./RUNBOOK.md)）。

預設清洗輸出路徑由 `02_preprocess.default_cleaned_session_parquet_path()` 決定（`trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_session.parquet`）。

## MLflow 訓練紀錄

主入口 `python -m trainer_hightier.trainer`（[`trainer.py`](./trainer.py)）為每次完整訓練建立 **單一 MLflow run**，並沿用主套件 [`trainer/core/mlflow_utils.py`](../trainer/core/mlflow_utils.py)：`MLFLOW_TRACKING_URI` 未設定或伺服器不可達時僅記錄 warning，**不中斷**離線訓練。

| 項目 | 說明 |
|------|------|
| **Experiment** | 程式常數 `trainer_hightier.config.MLFLOW_EXPERIMENT_TRAIN_HIGHTIER`（預設 `patron/patron_walkaway/prod/train_hightier`） |
| **Run name** | `YYYYMMDD-HHMMSS-<git short HEAD>` |
| **Tags** | `RUNNING` → `SUCCESS` 或失敗時 `FAILED`（附截斷錯誤摘要） |
| **Artifacts** | 上傳至 run 內 `hightier_run/`：`run_report.json`、Step 5 metrics/model、`split_report.json`（檔案存在時） |

環境檔慣例與主 `trainer` 相同：`credential/mlflow.env` 或 `local_state/mlflow.env`。細節與查核步驟見 [RUNBOOK.md](./RUNBOOK.md) §3.1。

## Feature Candidate Registry（如何讀 `status` 與選欄）

單一真相檔：[contracts/feature_candidate_registry.yaml](./contracts/feature_candidate_registry.yaml)。每筆在 `features:` 下是一列，用來記錄**治理狀態**與**在哪些情境啟用**；語意契約（時間窗、Feast 對照等）仍以檔頭註解所列之其他 contracts 為準。

### 欄位怎麼看

| YAML 欄位 | 含義 |
|-----------|------|
| `feature_id` | 欄位名（與訓練 Parquet / split 欄名一致） |
| `group_id` | 分組標籤（實驗 ablation 依 `group_*` 聚合 `fe__*`） |
| `source` | 資料來源類型（如 `baseline_model`、`feast_trial_1h`、`fe_derived`） |
| **`status`** | 生命週期：**`active`** 可選用；**`experimental`** 可選用（通常僅候選／ablation）；**`disabled`** 不參與任何選欄（歷史留痕） |
| **`enabled_for`** | 在哪些「槽位」可選：**`baseline`**（主 trainer Step 5）、**`candidate`**／**`ablation`**（feature_experiment） |
| `drop_reason_code` | `status: disabled` 時**必填**；其他狀態可為 `null` |
| `semantic_owner` | 語意負責檔案／模組（台帳不重複寫語意，只指到 owner） |
| `first_seen_experiment` / `last_updated_experiment` / `note` | 實驗追溯（可選） |

### 如何判斷「這個特徵有沒有進管線」

程式規則（與 `feature_experiment/candidate_registry_loader.py` 一致）：**僅當** `status` 為 `active` 或 `experimental`，**且**對應槽位出現在 `enabled_for` 裡，該欄才會被選入。

- **主 trainer（`python -m trainer_hightier.trainer`，Step 5）**  
  只看 **`baseline` 槽**：`status ∈ {active, experimental}` 且 `baseline ∈ enabled_for` 的列；欄名與順序以 `contracts/feature_candidate_registry.yaml` 宣告為準（Step 5 由程式讀取台帳，不再與程式內另一份常數對齊）。
- **Feature experiment（`feature_experiment/run_pipeline`）**  
  - Baseline / full candidate / FQG：依 `baseline` / `candidate` 槽與 `status` 組合。  
  - Ablation：依 `ablation` 槽與 `group_id`。

因此：僅看 **`status`** 不夠；**`disabled`** 一定不進；**`active`** 但若 `enabled_for` 沒有 `baseline`，則不會進主線 Step 5，仍可能進實驗候選（若有 `candidate`）。

### 追溯

- 主線跑完：`{output_dir}/run_report.json` 內的 `candidate_registry`（版本、解析路徑、baseline 欄位數）。
- 實驗跑完：run 目錄內 `feature_experiment_report.json` 的 `candidate_registry` 與相關區塊。

維護流程與常見錯誤見 [RUNBOOK.md](./RUNBOOK.md)（含主 trainer 與實驗管線差異）。

## Production deploy（scorer v2 + Feast）

訓練完成並通過建包 gate 後，自 **repo 根目錄**建置可部署 bundle：

```bash
python -m trainer_hightier.build_deploy_package --model-version <run_id> [--archive]
```

預設輸出至 `out/deploy_hightier/<model_version>/`（詳見 [RUNBOOK.md](./RUNBOOK.md) §打包搬機）。目標機在 **bundle 根目錄**：

```bash
pip install -r requirements.txt
cp .env.example .env   # 至少設定 CH_USER、CH_PASS
python main.py --mode all --bundle-dir .
```

### Deploy 啟動順序（`mode=all` / `mode=scorer`）

1. Preflight model、mapping、allowlist、`feast_repo`
2. **Startup Feast online refresh**（readiness 缺失 / stale 或 `--force-feast-refresh` 時；失敗 **fail-fast**）
3. Deploy Feast readiness + allowlist online smoke
4. **Feast refresh supervisor**（daemon thread，預設 **on**；每 300s poll，依 gaming day / 月轉 eligibility 自動 refresh mid + slow）
5. API（background）、validator（background）、scorer（foreground）

Post-startup refresh 為 **fail-soft**：失敗只 log + 下輪 retry，scorer 繼續用 last-good `feast_online_readiness.json`。

### 常用 CLI flags

| Flag | 說明 |
|------|------|
| `--no-feast-startup-refresh` | 略過 startup refresh（debug；scorer 多半無法通過 readiness gate） |
| `--force-feast-refresh` | 強制 startup refresh |
| `--no-feast-refresh-supervisor` | 停用 post-startup daemon（debug；若改用 external cron 才開，勿與 daemon 並用） |
| `--mode api` / `scorer` / `validator` | 僅跑單一元件；supervisor 僅在 `all` / `scorer` 啟動 |

### 手動 refresh（ops fallback）

```bash
python -m trainer_hightier.serving.feast_online_refresh \
  --source clickhouse --layers mid,slow \
  --adt-allowlist mapping/adt_allowed_players_q0p99.parquet \
  --canonical-mapping mapping/canonical_player_mapping.parquet
```

Supervisor 觀測：`local_state/feature_state.db` → `feature_state_meta` 鍵 `feast_refresh_supervisor_last_check_iso`、`_last_attempt_iso`、`_last_success_iso`。

### 規格文件

| 文件 | 說明 |
|------|------|
| [doc/Scorer Runtime Contract - SSOT.md](./doc/Scorer%20Runtime%20Contract%20-%20SSOT.md) | Deploy / scoring contract |
| [doc/Feast Post-Startup Refresh Supervisor - IMPLEMENTATION_PLAN.md](./doc/Feast%20Post-Startup%20Refresh%20Supervisor%20-%20IMPLEMENTATION_PLAN.md) | Post-startup daemon 設計 |
| [doc/Feast Online Refresh - IMPLEMENTATION_PLAN.md](./doc/Feast%20Online%20Refresh%20-%20IMPLEMENTATION_PLAN.md) | Refresh orchestration CLI |
| bundle 內 `README_DEPLOY.md` | 建包時生成的 operator 速查 |

## 依賴

與主專案共用 `requirements.txt` 中的 **duckdb**、**pandas**、**pyarrow** 等；本目錄不再重複維護獨立 manifest。

## 測試

```bash
python -m pytest trainer_hightier/tests/ -q
```

推送前請依團隊規範執行 ruff / mypy 等（本 README 不代為執行）。
