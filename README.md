# Patron Walkaway

---

## 中文（繁體）

### 專案簡介

Patron Walkaway 離場偵測專案。

我們的大堂已部署 Smart Table 技術，可即時擷取每位賓客（不論是否為評級客）的下注行為。目標是即時預測**評級客**是否將在未來 15 分鐘內停止博彩並離開，以便公關能即時接觸並挽留。

### 概述

- **Phase 1** 實作：單一模型（僅評級客 Rated only）LightGBM 流程，含 Optuna 超參數搜尋、run-level 樣本權重、**三軌特徵工程**（Track Profile PIT/as-of join、Track LLM DuckDB + Feature Spec YAML、Track Human 向量化 `loss_streak`/`run_boundary`）、身分對應與告警驗證。
- **資料**：ClickHouse（`GDP_GMWDS_Raw`）或開發用本地 Parquet（置於 `data/`）。
- **產出**：訓練產物在 `trainer/models/`（`.pkl`、特徵清單、原因碼、模型版本）；即時 scorer 將告警寫入 SQLite；API 與前端儀表板供營運使用。

### 架構（高層）

```
ClickHouse ──► trainer.py ──► models/ (model.pkl, …)
     │
     ├──► scorer.py ──► SQLite (alerts) ──► api_server.py ──► Frontend (main.html + JS)
     │
     ├──► validator.py (match/miss vs realized walkaways)
     └──► status_server.py (floor occupancy → SQLite)
```

- **`trainer/`** — `config.py`、`db_conn.py`、`trainer.py`、`identity.py`、`labels.py`、`features.py`、`time_fold.py`、`backtester.py`、`scorer.py`、`validator.py`、`api_server.py`、`status_server.py`，以及 ETL 與腳本。
- **`trainer/frontend/`** — 儀表板 SPA（地圖、告警、驗證趨勢、人流），**可選**；部署包可僅含 API（無前端），若需儀表板再自 repo 另行部署或建包時一併帶出。詳見 PROJECT.md「前端與部署」。
- **`tests/`** — 單元與整合測試（pytest）。
- **`doc/`** — 規格、發現、API 協定。**`schema/`** — 資料表/欄位字典與 DQ 提示。
- **Scripts**：可執行腳本在 **`scripts/`**（含 `check_span.py`）；歷史／一次性腳本在 **`doc/one_time_scripts/`**（僅供參考、勿直接執行）。詳見 PROJECT.md。

### 開發狀態（對應 `.cursor/plans/`）

- **Phase 1**：PLAN.md Step 0–10 均已實作完成（單一 Rated 模型、三軌特徵、DuckDB Track LLM、Feature Spec YAML 凍結進 artifact、閾值 F-beta 最大化）。
- **Track A（Featuretools DFS）已移除**：特徵工程僅保留三軌——Track Profile（PIT/as-of join）、Track LLM（DuckDB + YAML）、Track Human（向量化 `loss_streak`/`run_boundary`）。
- **Scorer / API**：僅對評級客（`is_rated`）產生告警；訓練結束後會清理舊版 `nonrated_model.pkl` / `rated_model.pkl`。
- **測試**：全量 `pytest` 約 519 passed；實作計畫與狀態詳見 `.cursor/plans/PLAN.md`、`.cursor/plans/STATUS.md`、`.cursor/plans/DECISION_LOG.md`。Phase 2 規劃草稿見 `doc/phase2_planning.md`。

### 環境設定

**需求**：Python 3.10+，執行 `pip install -r requirements.txt`。主要套件：`lightgbm`、`duckdb`、`optuna`、`shap`、`pandas`、`pyarrow`、`python-dotenv` 等。

**環境變數**：將 `trainer/.env.example` 複製為 `trainer/.env`（或設定對應環境變數），用於 ClickHouse：`CH_HOST`、`CH_TEAMDB_HOST`、`CH_PORT`、`CH_USER`、`CH_PASS`、`CH_SECURE`、`SOURCE_DB`。

**資料（訓練/回測）**：預設為 ClickHouse，請確認 `SOURCE_DB` 與憑證正確。本地 Parquet（開發/測試）：在專案根目錄放置 `data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`（可選 `data/player_profile.parquet`），執行 trainer 或 backtester 時加上 `--use-local-parquet`。

**MLflow（GCP Cloud Run）連線（做法 A）**：訓練與 export 會將 run/artifact 寫入 MLflow。請建立 **`local_state/mlflow.env`**（或將檔案放在例如 **`credential/mlflow.env`**，此二目錄皆已被 `.gitignore` 涵蓋，勿 commit），內容兩行：

```
MLFLOW_TRACKING_URI=https://<your-mlflow-cloud-run-url>
GOOGLE_APPLICATION_CREDENTIALS=<絕對路徑或相對專案根>/mlflow-key.json
```

若使用 **`credential/mlflow.env`**（非預設路徑），須在執行訓練或 export **前**設定環境變數：  
`MLFLOW_ENV_FILE=credential/mlflow.env`（或 `MLFLOW_ENV_FILE=<絕對路徑>/credential/mlflow.env`），程式才會載入該檔。

`mlflow-key.json` 為可存取該 Cloud Run 服務的 GCP 服務帳戶金鑰。程式會自動以該金鑰取得 **GCP ID token** 並在每次 MLflow 請求帶上 `Authorization: Bearer <token>`，以通過 Cloud Run 驗證。無須在主 `.env` 填寫 MLflow 相關變數。詳見 `trainer/core/mlflow_utils.py` 與 `.cursor/plans/STATUS.md`。

**Canonical mapping 共用 artifact（Step 3）**：訓練 Step 3 會產出 `data/canonical_mapping.parquet` 與 `data/canonical_mapping.cutoff.json`（sidecar 記錄本次使用的 `train_end`）。若兩檔存在且 sidecar 的 `cutoff_dtm` ≥ 該次 run 的 `train_end`，且未指定 `--rebuild-canonical-mapping`，則 Step 3 會**載入既有 artifact 並跳過建表**。若 parquet 缺少必要欄位（`player_id`、`canonical_id`），Step 3 會記錄警告並改為從頭建表。共用 artifact 時（例如將 `data/` 複製至他機）：假設兩邊 session 資料一致且更新至同一時點，mapping 的 cutoff 應 ≥ 該次 run 的 `train_end`；請確保 `data/` 僅由受控程式寫入，勿讓未信任來源寫入該目錄。詳見 `.cursor/plans/PLAN.md` § Canonical mapping 寫出與載入。

### Data loading & preprocessing

不論資料來自 Parquet、ClickHouse 或 ETL，進入 pipeline 前一律先經 **Post-Load Normalizer**（`trainer/schema_io.py` 的 `normalize_bets_sessions`），再進行 DQ、特徵或寫出，以保證型別契約一致。須經 normalizer 的入口如下：

| 入口 | 取得資料後 |
|------|------------|
| **trainer** `process_chunk()` | 先 cache key(raw)，再 normalize，再 `apply_dq` |
| **trainer** sessions-only | normalize(sessions)，再 `apply_dq` |
| **backtester** `main()` | load 後 normalize，再 `backtest()` → `apply_dq` |
| **scorer** `score_once()` | `fetch_recent_data()` 後 normalize，再 `build_features_for_scoring` |
| **etl_player_profile** | 取得 `sessions_raw` 後、D2 join / `_compute_profile` 前 `normalize_bets_sessions(pd.DataFrame(), sessions_raw)` |

詳見 `.cursor/plans/PLAN.md` § Post-Load Normalizer 與 `trainer/schema_io.py` 模組說明。

### 使用方式

**訓練（完整流程）**（在專案根目錄）：

訓練、評估與 serving 一律使用同一 lookback 視窗（config 中 `SCORER_LOOKBACK_HOURS`，預設 8 小時），以維持 train–serve parity。

```bash
python -m trainer.trainer --use-local-parquet --days 365
```

低記憶體（如 8 GB）：加上 `--no-preload` 可避免 profile backfill 時將整張 session Parquet 一次載入記憶體：

```bash
python -m trainer.trainer --recent-chunks 3 --use-local-parquet --no-preload
```

如需只使用部分評級客（節省訓練時間），可加入 `--sample-rated N`：

```bash
python -m trainer.trainer --recent-chunks 3 --use-local-parquet --sample-rated 1000
```

**Backtester**：`python -m trainer.backtester --start "2025-01-01" --end "2025-01-31" --use-local-parquet`（可加 `--skip-optuna` 跳過閾值搜尋、`--n-trials N` 指定 Optuna 試驗次數）

**即時 scorer**：`python -m trainer.scorer --interval 45 --lookback-hours 8`（單次執行加 `--once`；可加 `--model-dir` 指定模型目錄、`--log-level DEBUG|INFO|WARNING`）。Scorer 也會讀取 `data/canonical_mapping.parquet` 與 sidecar（條件同 trainer）；若需強制重建 mapping 可加 `--rebuild-canonical-mapping`。所有觀測用同一 rated 模型評分；**僅評級客（is_rated）會產生告警**，非評級客分數僅供 volume 統計（UNRATED_VOLUME_LOG）。

**Validator**：`python -m trainer.validator --interval 60`（單次加 `--once`；手動強制結案 PENDING 加 `--force-finalize`）

**API 伺服器**：`python -m trainer.api_server`（預設 http://0.0.0.0:8001；見 `package/ML_API_PROTOCOL.md`）

**Status server**：`python -m trainer.status_server`

**ETL / profile**：`trainer/etl_player_profile.py` 用於 profile 回填；`python -m trainer.scripts.auto_build_player_profile --start-date ... --end-date ...` 用於排程建置，詳見腳本說明。

**部署**：訓練完成後可建置可部署套件（scorer + validator + Flask GET /alerts、GET /validation），從專案根目錄執行 `python -m package.build_deploy_package` 產出 `deploy_dist/`（可加 `--archive` 產出 zip）。目標機複製後 `pip install -r requirements.txt`、設定 `.env`、執行 `python main.py`。詳見 `package/README.md` 與 `.cursor/plans/DEPLOY_PLAN.md`。

### Trainer 指令參數（cmd flags）

| 參數 | 說明 |
|------|------|
| `--start` | 訓練視窗起日（YYYY-MM-DD 或 ISO）。須與 `--end` 同時指定，否則視窗由 `--days` 決定。 |
| `--end` | 訓練視窗迄日。須與 `--start` 同時指定。 |
| `--days` | 未給 `--start`/`--end` 時使用：取「迄日為現在減 30 分鐘」往前 N 天為視窗。預設由 `config.TRAINER_DAYS` 決定（通常 7）。 |
| `--use-local-parquet` | 從專案根目錄 `data/` 讀取 Parquet（`gmwds_t_bet.parquet`、`gmwds_t_session.parquet` 等），不連 ClickHouse。 |
| `--force-recompute` | 忽略已快取的 chunk Parquet（`trainer/.data/chunks/`），強制重新計算每個 chunk。 |
| `--skip-optuna` | 不跑 Optuna 超參搜尋，使用預設 LightGBM 超參（可節省約 10 分鐘）。 |
| `--recent-chunks N` | 僅使用訓練視窗內「最後 N 個」月 chunk（每 chunk 約一個月）。限制從 ClickHouse 或本地 Parquet 載入的資料量；建議 N≥3 以保持 train/valid/test 皆有資料。例如 `--recent-chunks 3` 約為最近 3 個月。 |
| `--no-preload` | 關閉 profile backfill 時對 session Parquet 的「全表一次載入」，改為每 snapshot 日用 PyArrow pushdown 讀取。預設（不加此旗標）會完整載入整張 session 表格。適合 ≤8 GB RAM 機器，避免 OOM，代價是 backfill 速度較慢。 |
| `--sample-rated N` | 僅使用 N 個評級客（canonical_id 字典序取前 N 個）。預設不抽樣（使用全部評級客）。 |
| `--rebuild-canonical-mapping` | 強制從頭建 canonical mapping，不載入既有 `data/canonical_mapping.parquet`；建完後照常寫出。用於 mapping 損壞/過期或 schema 變更後重算。 |

### 測試

全部測試：`pytest`  
僅 trainer 相關：`pytest tests/test_trainer.py -v`  
快速煙測：`python -m trainer.trainer --recent-chunks 1 --use-local-parquet --skip-optuna`  
程式碼品質：`ruff check .`、`mypy trainer/ --ignore-missing-imports`

### 文件

| 文件 | 說明 |
|------|------|
| `ssot/trainer_plan_ssot.md` | 訓練/標籤/特徵設計規格（單一事實來源 SSOT） |
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` | 資料表/欄位字典與 DQ 備註 |
| `doc/FINDINGS.md` | 資料品質與行為發現（可重現 SQL） |
| `doc/player_profile_spec.md` | 玩家 profile ETL 與 PIT/as-of 語意 |
| `doc/FEATURE_SPEC_GUIDE.md` | 特徵規格 YAML 與 Feature Spec 使用說明 |
| `doc/model_api_protocol.md` | 模型與應用 API 協定（如 POST /score） |
| `package/ML_API_PROTOCOL.md` | 部署用 ML API 協定（GET /alerts、GET /validation，儀表板輪詢） |
| `doc/TRAINER_SUMMARY.md` | 系統摘要（架構、模組、前端） |
| `doc/TRAINER_TEAM_PRESENTATION.md` | 團隊向系統概覽 |
| `doc/PLAN_VS_TRAINER_COMPARISON.md` | 計畫與實作對照 |
| `doc/TRAINER_ISSUES.md` | 已知問題與備註 |
| `.cursor/plans/` | 實作計畫（PLAN.md）、狀態（STATUS.md）、決策紀錄（DECISION_LOG.md） |
| `doc/phase2_planning.md` | Phase 2 規劃草稿（方向、文獻與業界建議） |
| **PROJECT.md** | 專案結構與目錄職責 SSOT；詳細計畫與狀態以 `.cursor/plans/` 為準，規格與 Phase 2 在 `doc/`。 |

### 產物（trainer 輸出）

> **路徑**：預設寫入 **`MODEL_DIR`**＝專案根下 **`out/models/`**（`trainer/core/config.py` 之 **`DEFAULT_MODEL_DIR`**）；可設環境變數 **`MODEL_DIR`** 覆寫。下文 **`trainer/models/`** 表同一 bundle 目錄（慣用簡稱）。

`trainer/models/` 下：`model.pkl`（v10 單一評級客模型；**DEC-040**：scorer／backtester **僅**從此檔載入模型）、`feature_list.json`、`feature_spec.yaml`（DEC-024 凍結特徵規格，訓練時寫入 bundle，scorer 優先從此載入）、`reason_code_map.json`、`model_version`、`training_metrics.json`（僅 rated 指標）、**`pipeline_diagnostics.json`**（訓練成功後寫入：pipeline 總／步驟耗時、`step7_rss_*`、OOM 預檢與 `oom_precheck_step7_rss_error_ratio` 等資源診斷；與模型效能指標分檔）。訓練結束後若存在舊版 `nonrated_model.pkl`、`rated_model.pkl` 或 legacy `walkaway_model.pkl` 會自動刪除，避免目錄內殘留可誤解的檔案（載入端亦不再讀 rated／walkaway）。

- **部署／MLflow**：`python -m package.build_deploy_package` 會將 `pipeline_diagnostics.json` 一併拷貝到產物 `models/`（來源目錄有該檔時；缺檔時建包僅 warning）。若已設定 tracking 且該次訓練有 active run，上述小檔另可以 **`bundle/`** 前綴出現在該 run 的 **Artifacts**（best-effort，與 `training_metrics.json` 等並列）。詳見 `doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md`、`doc/phase2_provenance_schema.md`。

### 注意事項

- **憑證**：請安全存放 ClickHouse 憑證，勿提交 `.env`。
- **時區**：業務邏輯使用 `Asia/Hong_Kong`（`config.HK_TZ`）。
- **閾值選擇**：Phase 1 以驗證集 **F-beta 最大化**（預設 β=0.5，偏重 precision）選定單一模型閾值（DEC-009, DEC-021）；可選最小 recall / 每小時警報量約束，詳見 `config.THRESHOLD_FBETA`。
- **告警範圍**：Scorer 與 API `POST /score` 僅對評級客（`is_rated=true`）回傳告警；非評級客仍會得到分數，但 `alert` 恆為 `false`。

---

## 中文（简体）

### 项目简介

Patron Walkaway 离场检测项目。

我们的大堂已部署 Smart Table 技术，可实时采集每位宾客（不论是否为评级客）的下注行为。目标是在实时预测**评级客**是否将在未来 15 分钟内停止博彩并离开，以便主持人能及时接触并挽留。

### 概述

- **Phase 1** 实现：单模型（仅评级客 Rated only）LightGBM 流程，含 Optuna 超参搜索、run-level 样本权重、**三轨特征工程**（Track Profile PIT/as-of join、Track LLM DuckDB + Feature Spec YAML、Track Human 向量化 `loss_streak`/`run_boundary`）、身份映射与告警验证。
- **数据**：ClickHouse（`GDP_GMWDS_Raw`）或开发用本地 Parquet（置于 `data/`）。
- **产出**：训练产物在 `trainer/models/`（`.pkl`、特征列表、原因码、模型版本）；实时 scorer 将告警写入 SQLite；API 与前端仪表盘供运营使用。

### 架构（高层）

```
ClickHouse ──► trainer.py ──► models/ (model.pkl, …)
     │
     ├──► scorer.py ──► SQLite (alerts) ──► api_server.py ──► Frontend (main.html + JS)
     │
     ├──► validator.py (match/miss vs realized walkaways)
     └──► status_server.py (floor occupancy → SQLite)
```

- **`trainer/`** — `config.py`、`db_conn.py`、`trainer.py`、`identity.py`、`labels.py`、`features.py`、`time_fold.py`、`backtester.py`、`scorer.py`、`validator.py`、`api_server.py`、`status_server.py`，以及 ETL 与脚本。
- **`trainer/frontend/`** — 仪表盘 SPA（地图、告警、验证趋势、人流），**可选**；部署包可仅含 API（无前端），若需仪表板再自 repo 另行部署或建包时一并带出。详见 PROJECT.md「前端与部署」。
- **`tests/`** — 单元与集成测试（pytest）。
- **`doc/`** — 规格、发现、API 协议。**`schema/`** — 表/字段字典与 DQ 提示。

### 开发状态（对应 `.cursor/plans/`）

- **Phase 1**：PLAN.md Step 0–10 均已实现完成（单一 Rated 模型、三轨特征、DuckDB Track LLM、Feature Spec YAML 冻结进 artifact、阈值 F-beta 最大化）。
- **Track A（Featuretools DFS）已移除**：特征工程仅保留三轨——Track Profile（PIT/as-of join）、Track LLM（DuckDB + YAML）、Track Human（向量化 `loss_streak`/`run_boundary`）。
- **Scorer / API**：仅对评级客（`is_rated`）产生告警；训练结束后会清理旧版 `nonrated_model.pkl` / `rated_model.pkl`。
- **测试**：全量 `pytest` 约 519 passed；实现计划与状态详见 `.cursor/plans/PLAN.md`、`.cursor/plans/STATUS.md`、`.cursor/plans/DECISION_LOG.md`。Phase 2 规划草稿见 `doc/phase2_planning.md`。

### 环境设置

**需求**：Python 3.10+，执行 `pip install -r requirements.txt`。主要包：`lightgbm`、`duckdb`、`optuna`、`shap`、`pandas`、`pyarrow`、`python-dotenv` 等。

**环境变量**：将 `trainer/.env.example` 复制为 `trainer/.env`（或设置对应环境变量），用于 ClickHouse：`CH_HOST`、`CH_TEAMDB_HOST`、`CH_PORT`、`CH_USER`、`CH_PASS`、`CH_SECURE`、`SOURCE_DB`。

**数据（训练/回测）**：默认为 ClickHouse，请确认 `SOURCE_DB` 与凭证正确。本地 Parquet（开发/测试）：在项目根目录放置 `data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`（可选 `data/player_profile.parquet`），运行 trainer 或 backtester 时加上 `--use-local-parquet`。

**Canonical mapping 共用 artifact（Step 3）**：训练 Step 3 会产出 `data/canonical_mapping.parquet` 与 `data/canonical_mapping.cutoff.json`（sidecar 记录本次使用的 `train_end`）。若两档存在且 sidecar 的 `cutoff_dtm` ≥ 该次 run 的 `train_end`，且未指定 `--rebuild-canonical-mapping`，则 Step 3 会**载入既有 artifact 并跳过建表**。若 parquet 缺少必要栏位（`player_id`、`canonical_id`），Step 3 会记录警告并改为从头建表。共用 artifact 时（例如将 `data/` 复制至他机）：假设两边 session 数据一致且更新至同一时点，mapping 的 cutoff 应 ≥ 该次 run 的 `train_end`；请确保 `data/` 仅由受控程式写入，勿让未信任来源写入该目录。详见 `.cursor/plans/PLAN.md` § Canonical mapping 写出与载入。

### 使用方式

**训练（完整流程）**（在项目根目录）：

```bash
python -m trainer.trainer
python -m trainer.trainer --use-local-parquet --recent-chunks 3
python -m trainer.trainer --skip-optuna --use-local-parquet
```

低内存（如 8 GB）：加上 `--no-preload` 可避免 profile backfill 时将整张 session Parquet 一次载入内存：

```bash
python -m trainer.trainer --recent-chunks 3 --use-local-parquet --no-preload
```

如需只使用部分评级客（节省训练时间），可加入 `--sample-rated N`：

```bash
python -m trainer.trainer --recent-chunks 3 --use-local-parquet --sample-rated 1000
```

**Backtester**：`python -m trainer.backtester --start "2025-01-01" --end "2025-01-31" --use-local-parquet`（可加 `--skip-optuna` 跳过阈值搜索、`--n-trials N` 指定 Optuna 试验次数）

**实时 scorer**：`python -m trainer.scorer --interval 45 --lookback-hours 8`（单次执行加 `--once`；可加 `--model-dir` 指定模型目录、`--log-level DEBUG|INFO|WARNING`）。Scorer 也会读取 `data/canonical_mapping.parquet` 与 sidecar（条件同 trainer）；若需强制重建 mapping 可加 `--rebuild-canonical-mapping`。所有观测用同一 rated 模型评分；**仅评级客（is_rated）会产生告警**，非评级客分数仅供 volume 统计（UNRATED_VOLUME_LOG）。

**Validator**：`python -m trainer.validator --interval 60`（单次加 `--once`；手动强制结案 PENDING 加 `--force-finalize`）

**API 服务**：`python -m trainer.api_server`（默认 http://0.0.0.0:8001；见 `package/ML_API_PROTOCOL.md`）

**Status server**：`python -m trainer.status_server`

**ETL / profile**：`trainer/etl_player_profile.py` 用于 profile 回填；`python -m trainer.scripts.auto_build_player_profile --start-date ... --end-date ...` 用于定时构建，详见脚本说明。

**部署**：训练完成后可构建可部署包（scorer + validator + Flask GET /alerts、GET /validation），从项目根目录执行 `python -m package.build_deploy_package` 产出 `deploy_dist/`（可加 `--archive` 产出 zip）。目标机复制后 `pip install -r requirements.txt`、配置 `.env`、执行 `python main.py`。详见 `package/README.md` 与 `.cursor/plans/DEPLOY_PLAN.md`。

### Trainer 指令参数（cmd flags）

| 参数 | 说明 |
|------|------|
| `--start` | 训练窗口起日（YYYY-MM-DD 或 ISO）。须与 `--end` 同时指定，否则窗口由 `--days` 决定。 |
| `--end` | 训练窗口迄日。须与 `--start` 同时指定。 |
| `--days` | 未给 `--start`/`--end` 时使用：取「迄日为现在减 30 分钟」往前 N 天为窗口。默认由 `config.TRAINER_DAYS` 决定（通常 7）。 |
| `--use-local-parquet` | 从项目根目录 `data/` 读取 Parquet（`gmwds_t_bet.parquet`、`gmwds_t_session.parquet` 等），不连 ClickHouse。 |
| `--force-recompute` | 忽略已缓存的 chunk Parquet（`trainer/.data/chunks/`），强制重新计算每个 chunk。 |
| `--skip-optuna` | 不跑 Optuna 超参搜索，使用默认 LightGBM 超参，可省约 10 分钟。 |
| `--recent-chunks N` | 仅使用训练窗口内「最后 N 个」月 chunk（每 chunk 约一个月）。限制从 ClickHouse 或本地 Parquet 载入的数据量；建议 N≥3 以保持 train/valid/test 皆有数据。例如 `--recent-chunks 3` 约最近 3 个月。 |
| `--no-preload` | 关闭 profile backfill 时对 session Parquet 的「全表一次载入」，改为每 snapshot 日用 PyArrow pushdown 读取。默认（不加此旗标）会完整载入整张 session 表格。适合 ≤8 GB RAM 机器，避免 OOM，代价是 backfill 速度较慢。 |
| `--sample-rated N` | 仅使用 N 个评级客（canonical_id 字典序取前 N 个）。默认不抽样（使用全部评级客）。 |
| `--rebuild-canonical-mapping` | 强制从头建 canonical mapping，不载入既有 `data/canonical_mapping.parquet`；建完后照常写出。用于 mapping 损坏/过期或 schema 变更后重算。 |

### 测试

全部测试：`pytest`  
仅 trainer 相关：`pytest tests/test_trainer.py -v`  
快速烟测：`python -m trainer.trainer --recent-chunks 1 --use-local-parquet --skip-optuna`  
代码质量：`ruff check .`、`mypy trainer/ --ignore-missing-imports`

### 文档

| 文档 | 说明 |
|------|------|
| `ssot/trainer_plan_ssot.md` | 训练/标签/特征设计规格（单一事实来源 SSOT） |
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` | 表/字段字典与 DQ 备注 |
| `doc/FINDINGS.md` | 数据质量与行为发现（可重现 SQL） |
| `doc/player_profile_spec.md` | 玩家 profile ETL 与 PIT/as-of 语义 |
| `doc/FEATURE_SPEC_GUIDE.md` | 特征规格 YAML 与 Feature Spec 使用说明 |
| `doc/model_api_protocol.md` | 模型与应用 API 协议（如 POST /score） |
| `package/ML_API_PROTOCOL.md` | 部署用 ML API 协议（GET /alerts、GET /validation，仪表板轮询） |
| `doc/TRAINER_SUMMARY.md` | 系统摘要（架构、模块、前端） |
| `doc/TRAINER_TEAM_PRESENTATION.md` | 团队向系统概览 |
| `doc/PLAN_VS_TRAINER_COMPARISON.md` | 计划与实现对照 |
| `doc/TRAINER_ISSUES.md` | 已知问题与备注 |
| `.cursor/plans/` | 实现计划（PLAN.md）、状态（STATUS.md）、决策记录（DECISION_LOG.md） |
| `doc/phase2_planning.md` | Phase 2 规划草稿（方向、文献与业界建议） |
| **PROJECT.md** | 项目结构与目录职责 SSOT；详细计划与状态以 `.cursor/plans/` 为准，规格与 Phase 2 在 `doc/`。 |

### 产物（trainer 输出）

> **路径**：默认写入 **`MODEL_DIR`**＝项目根下 **`out/models/`**（`trainer/core/config.py` 之 **`DEFAULT_MODEL_DIR`**）；可设环境变量 **`MODEL_DIR`** 覆盖。下文 **`trainer/models/`** 表同一 bundle 目录（惯用简称）。

`trainer/models/` 下：`model.pkl`（v10 单一评级客模型；**DEC-040**：scorer／backtester **仅**从此文件加载模型）、`feature_list.json`、`feature_spec.yaml`（DEC-024 冻结特征规格，训练时写入 bundle，scorer 优先从此载入）、`reason_code_map.json`、`model_version`、`training_metrics.json`（仅 rated 指标）、**`pipeline_diagnostics.json`**（训练成功后写入：pipeline 总/步骤耗时、`step7_rss_*`、OOM 预检与 `oom_precheck_step7_rss_error_ratio` 等资源诊断；与模型效能指标分档）。训练结束后若存在旧版 `nonrated_model.pkl`、`rated_model.pkl` 或 legacy `walkaway_model.pkl` 会自动删除；加载端不再读取 rated／walkaway。

- **部署／MLflow**：`python -m package.build_deploy_package` 会将 `pipeline_diagnostics.json` 一并拷贝到产物 `models/`（来源目录有该档时；缺档时建包仅 warning）。若已设定 tracking 且该次训练有 active run，上述小档另可以以 **`bundle/`** 前缀出现在该 run 的 **Artifacts**（best-effort）。详见 `doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md`、`doc/phase2_provenance_schema.md`。

### 注意事项

- **凭证**：请安全存放 ClickHouse 凭证，勿提交 `.env`。
- **时区**：业务逻辑使用 `Asia/Hong_Kong`（`config.HK_TZ`）。
- **阈值选择**：Phase 1 以验证集 **F-beta 最大化**（默认 β=0.5，偏重 precision）选定单模型阈值（DEC-009, DEC-021）；可选最小 recall / 每小时警报量约束，详见 `config.THRESHOLD_FBETA`。
- **告警范围**：Scorer 与 API `POST /score` 仅对评级客（`is_rated=true`）返回告警；非评级客仍会得到分数，但 `alert` 恒为 `false`。

---

## English

Patron Walkaway Detection project.

Our mass gaming floor has deployed Smart Table technology through which we are able to capture the betting behavior of every patron (rated or not) in real-time. The goal is to detect in real-time whether a rated gaming patron will stop gaming and leave in the upcoming 15 minutes, so that our hosts can approach and retain them.

## Overview

- **Phase 1** implementation: single-model (rated only) LightGBM pipeline with Optuna hyperparameter search, run-level sample weighting, **three-track feature engineering** (Track Profile PIT/as-of join, Track LLM DuckDB + Feature Spec YAML, Track Human vectorized `loss_streak`/`run_boundary`), identity mapping, and alert validation.
- **Data**: ClickHouse (`GDP_GMWDS_Raw`) or local Parquet under `data/` for development.
- **Output**: Trained artifacts in `trainer/models/` (`.pkl`, feature list, reason codes, model version); live scorer writes alerts to SQLite; API + frontend dashboard for operators.

---

## Architecture (high level)

```
ClickHouse ──► trainer.py ──► models/ (model.pkl, …)
     │
     ├──► scorer.py ──► SQLite (alerts) ──► api_server.py ──► Frontend (main.html + JS)
     │
     ├──► validator.py (match/miss vs realized walkaways)
     └──► status_server.py (floor occupancy → SQLite)
```

- **`trainer/`** — `config.py`, `db_conn.py`, `trainer.py`, `identity.py`, `labels.py`, `features.py`, `time_fold.py`, `backtester.py`, `scorer.py`, `validator.py`, `api_server.py`, `status_server.py`, ETL and scripts.
- **`trainer/frontend/`** — Dashboard SPA (map, alerts, validation trends, headcount), **optional**; deploy package can be API-only (no frontend). If you need the dashboard, serve it from the repo or include it in the build. See PROJECT.md § 前端與部署.
- **`tests/`** — Unit and integration tests (pytest).
- **`doc/`** — Specs, findings, API protocol. **`schema/`** — Table/column dictionary and DQ hints.

### Development status (see `.cursor/plans/`)

- **Phase 1**: PLAN.md Steps 0–10 are implemented (single Rated model, three-track features, DuckDB Track LLM, Feature Spec YAML frozen into artifact, F-beta threshold maximization).
- **Track A (Featuretools DFS) removed**: Feature engineering is three-track only — Track Profile (PIT/as-of join), Track LLM (DuckDB + YAML), Track Human (vectorized `loss_streak`/`run_boundary`).
- **Scorer / API**: Alerts are emitted only for rated patrons (`is_rated`); stale `nonrated_model.pkl` / `rated_model.pkl` are cleaned up after training.
- **Tests**: Full `pytest` ~519 passed; see `.cursor/plans/PLAN.md`, `.cursor/plans/STATUS.md`, `.cursor/plans/DECISION_LOG.md` for plan and status. Phase 2 planning draft: `doc/phase2_planning.md`.

---

## Setup

### Requirements

- Python 3.10+
- Install dependencies:

  ```bash
  pip install -r requirements.txt
  ```

  Key packages: `lightgbm`, `duckdb`, `optuna`, `shap`, `pandas`, `pyarrow`, `python-dotenv`, etc.

### Environment

Copy `trainer/.env.example` to `trainer/.env` (or set env vars) for ClickHouse:

- `CH_HOST`, `CH_TEAMDB_HOST`, `CH_PORT`, `CH_USER`, `CH_PASS`, `CH_SECURE`, `SOURCE_DB`

**MLflow (GCP Cloud Run) — Option A**  
To log runs and artifacts to MLflow on GCP Cloud Run, create **`local_state/mlflow.env`** (or put the file under **`credential/mlflow.env`**; both directories are gitignored). Add two lines:

```
MLFLOW_TRACKING_URI=https://<your-mlflow-cloud-run-url>
GOOGLE_APPLICATION_CREDENTIALS=<absolute-or-repo-relative-path>/mlflow-key.json
```

If you use **`credential/mlflow.env`** (non-default path), set **before** running trainer or export:  
`MLFLOW_ENV_FILE=credential/mlflow.env` (or an absolute path to that file), so the module loads it on import.

Use a GCP service account key file (`mlflow-key.json`) that can invoke the Cloud Run service. The code will obtain a **GCP ID token** and send `Authorization: Bearer <token>` with each MLflow request so Cloud Run authentication succeeds. You do not need to put MLflow-related variables in the main `.env`. See `trainer/core/mlflow_utils.py` and `.cursor/plans/STATUS.md`.

### Data (for training / backtest)

- **ClickHouse**: Default. Ensure `SOURCE_DB` and credentials are correct.
- **Local Parquet (dev/test)**:
  - Place exports in project root: `data/gmwds_t_bet.parquet`, `data/gmwds_t_session.parquet` (and optionally `data/player_profile.parquet`).
  - Use `--use-local-parquet` when running the trainer or backtester.

**Canonical mapping shared artifact (Step 3)**  
Step 3 writes `data/canonical_mapping.parquet` and `data/canonical_mapping.cutoff.json` (sidecar records this run’s `train_end`). If both exist and the sidecar’s `cutoff_dtm` ≥ this run’s `train_end`, and `--rebuild-canonical-mapping` is not set, Step 3 **loads the existing artifact and skips building**. If the parquet is missing required columns (`player_id`, `canonical_id`), Step 3 logs a warning and rebuilds from scratch. When sharing the artifact (e.g. copying `data/` to another machine), assume session data is consistent and up to the same point; the mapping’s cutoff should be ≥ that run’s `train_end`. Ensure `data/` is written only by controlled processes; do not allow untrusted sources to write to that directory. See `.cursor/plans/PLAN.md` § Canonical mapping write/load.

---

## Usage

### Training (full pipeline)

From project root:

```bash
# Default: ClickHouse, full window (configurable via --start / --end)
python -m trainer.trainer

# Local Parquet, last 3 months only (debug)
python -m trainer.trainer --use-local-parquet --recent-chunks 3

# Skip Optuna (use default hyperparameters)
python -m trainer.trainer --skip-optuna --use-local-parquet
```

### Low-RAM / subset training

Low-RAM (e.g. 8 GB): add `--no-preload` to avoid loading the full session Parquet into memory during profile backfill. By default (flag absent) the entire session table is preloaded once for efficiency.

```bash
python -m trainer.trainer --recent-chunks 3 --use-local-parquet --no-preload
```

To train on a deterministic subset of rated patrons:

```bash
python -m trainer.trainer --recent-chunks 3 --use-local-parquet --sample-rated 1000
```

### Backtester

```bash
python -m trainer.backtester --start "2025-01-01" --end "2025-01-31" --use-local-parquet
# Optional: --skip-optuna (skip threshold search), --n-trials N (Optuna trials)
```

### Live scorer (polling + alerts)

```bash
python -m trainer.scorer --interval 45 --lookback-hours 8
# Single run: --once. Override model dir: --model-dir PATH. Log level: --log-level DEBUG|INFO|WARNING
```

The scorer also loads `data/canonical_mapping.parquet` and sidecar (same conditions as trainer); use `--rebuild-canonical-mapping` to force a full rebuild. All observations are scored with the single rated model (v10). **Alerts are emitted only for rated patrons** (`is_rated`); unrated scores are used for volume telemetry only (UNRATED_VOLUME_LOG).

### Validator (match/miss vs realized walkaways)

```bash
python -m trainer.validator --interval 60
# Single pass: --once. Force-finalize PENDING: --force-finalize
```

### API server (dashboard backend)

```bash
python -m trainer.api_server
# Serves on http://0.0.0.0:8001 (see package/ML_API_PROTOCOL.md)
```

### Status server (floor occupancy)

```bash
python -m trainer.status_server
```

### ETL / profile

- **Player profile daily (backfill)**  
  `trainer/etl_player_profile.py` — used by the trainer when profile is required; can be run standalone with date range and local Parquet options.

- **Auto-build profile (scheduled)**  
  `python -m trainer.scripts.auto_build_player_profile --start-date ... --end-date ...`  
  See script help for ClickHouse vs local Parquet.

### Deployment

After training, build a deployable package (scorer + validator + Flask GET /alerts, GET /validation) from the repo root:

```bash
python -m package.build_deploy_package
# Optional: --archive for deploy_dist.zip; --model-source DIR to override model dir
```

Copy `deploy_dist/` (or the zip) to the target machine, then `pip install -r requirements.txt`, configure `.env`, and run `python main.py`. See `package/README.md` and `.cursor/plans/DEPLOY_PLAN.md` for details.

---

## Trainer command-line flags

| Flag | Description |
|------|-------------|
| `--start` | Training window start (YYYY-MM-DD or ISO). Must be used with `--end`; otherwise the window is determined by `--days`. |
| `--end` | Training window end. Must be used with `--start`. |
| `--days` | When `--start`/`--end` are not set: window is the last N days ending 30 minutes ago. Default from `config.TRAINER_DAYS` (often 7). |
| `--use-local-parquet` | Read from project root `data/` Parquet files (`gmwds_t_bet.parquet`, `gmwds_t_session.parquet`, etc.) instead of ClickHouse. |
| `--force-recompute` | Ignore cached chunk Parquets in `trainer/.data/chunks/` and recompute every chunk. |
| `--skip-optuna` | Skip Optuna hyperparameter search and use default LightGBM hyperparameters (saves ~10 min). |
| `--recent-chunks N` | Use only the last N monthly chunks in the training window (one chunk ≈ one month). Limits data loaded from ClickHouse or local Parquet; recommend N≥3 so train/valid/test are all non-empty. E.g. `--recent-chunks 3` ≈ last 3 months. |
| `--no-preload` | Disable full-table session Parquet preload during profile backfill; use per-snapshot PyArrow pushdown reads instead. Default (flag absent) is to preload the full session table once. Recommended for ≤8 GB RAM to avoid OOM at the cost of slower backfill. |
| `--sample-rated N` | Use only N canonical_ids (first N by lexicographic order). Default: no sampling (all rated). |
| `--rebuild-canonical-mapping` | Force rebuild canonical mapping from scratch; do not load existing `data/canonical_mapping.parquet`; write after build. Use when mapping is corrupted/expired or after schema changes. |

---

## Testing

Run all tests:

```bash
pytest
```

Run only trainer-related tests:

```bash
pytest tests/test_trainer.py -v
```

Quick smoke test (requires local Parquet data):

```bash
python -m trainer.trainer --recent-chunks 1 --use-local-parquet --skip-optuna
```

Lint and type-check:

```bash
ruff check .
mypy trainer/ --ignore-missing-imports
```

---

## Documentation

| Document | Description |
|----------|-------------|
| `ssot/trainer_plan_ssot.md` | Training/labels/features design spec (single source of truth) |
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` | Table/column dictionary and short DQ notes |
| `doc/FINDINGS.md` | Data quality and behavior findings (reproducible SQL) |
| `doc/player_profile_spec.md` | Player profile ETL and PIT/as-of semantics |
| `doc/FEATURE_SPEC_GUIDE.md` | Feature spec YAML and Feature Spec usage guide |
| `doc/model_api_protocol.md` | Model–app API contract (e.g. POST /score) for decoupled inference |
| `package/ML_API_PROTOCOL.md` | Deploy ML API contract (GET /alerts, GET /validation for dashboard polling) |
| `doc/TRAINER_SUMMARY.md` | System summary (architecture, modules, frontend) |
| `doc/TRAINER_TEAM_PRESENTATION.md` | Team-facing overview |
| `doc/PLAN_VS_TRAINER_COMPARISON.md` | Plan vs implementation comparison |
| `doc/TRAINER_ISSUES.md` | Known issues / notes |
| `.cursor/plans/` | Implementation plan (PLAN.md), status (STATUS.md), decision log (DECISION_LOG.md) |
| `doc/phase2_planning.md` | Phase 2 planning draft (directions, literature and industry notes) |
| **PROJECT.md** | Project structure and directory responsibilities (SSOT); detailed plan and status in `.cursor/plans/`, specs and Phase 2 in `doc/`. |

---

## Artifacts (trainer output)

**Path**: The trainer writes to **`MODEL_DIR`**, default **`out/models/`** under the repo root (`DEFAULT_MODEL_DIR` in `trainer/core/config.py`). Override with the **`MODEL_DIR`** environment variable. Below, `trainer/models/` is shorthand for that bundle directory.

Under `trainer/models/`:

- `model.pkl` — Single rated LightGBM model (v10 DEC-021)
- `feature_list.json` — Feature names and track classification
- `feature_spec.yaml` — Frozen feature spec snapshot (DEC-024) written at training time; scorer loads this first for train–serve consistency
- `reason_code_map.json` — Feature-to-reason-code mapping for SHAP
- `model_version` — Version string (e.g. `20260228-153000-abc1234`)
- `training_metrics.json` — Rated metrics only; flags such as `uncalibrated_threshold`, `sample_rated_n`
- `pipeline_diagnostics.json` — Written on successful training: pipeline/step timings, `step7_rss_*`, OOM precheck vs observed (`oom_precheck_step7_rss_error_ratio`), etc.; **separate** from model metrics in `training_metrics.json`

**Deploy / MLflow**: `python -m package.build_deploy_package` copies `pipeline_diagnostics.json` into the package `models/` when present (missing file → warning only). With tracking enabled and an active run, small files may also appear under the run’s **Artifacts** with the **`bundle/`** prefix (best-effort). See `doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md` and `doc/phase2_provenance_schema.md`.

**DEC-040**: Serving and backtesting load **`model.pkl` only** (no `rated_model.pkl` or `walkaway_model.pkl` fallback). The trainer does not emit `walkaway_model.pkl`. After each successful run, stale `nonrated_model.pkl`, `rated_model.pkl`, and `walkaway_model.pkl` are removed from the bundle directory when present.

---

## Notes

- **Credentials**: Store ClickHouse credentials securely; avoid committing `.env`.
- **Time zone**: Business logic uses `Asia/Hong_Kong` (see `config.HK_TZ`).
- **Threshold selection**: Phase 1 uses validation-set **F-beta maximization** (default β=0.5, precision-weighted) for the single-model threshold (DEC-009, DEC-021); optional min recall / alerts-per-hour constraints; see `config.THRESHOLD_FBETA`.
- **Alert scope**: The scorer and API `POST /score` return `alert=true` only for rated patrons (`is_rated=true`); unrated rows still receive a score but `alert` is always `false`.
