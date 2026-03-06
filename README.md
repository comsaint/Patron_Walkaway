# Patron Walkaway

---

## 中文（繁體）

### 專案簡介

Patron Walkaway 離場偵測專案。

我們的大堂已部署 Smart Table 技術，可即時擷取每位賓客（不論是否為評級客）的下注行為。目標是即時預測**評級客**是否將在未來 15 分鐘內停止博彩並離開，以便主持人能即時接觸並挽留。

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
- **`trainer/frontend/`** — 儀表板 SPA（地圖、告警、驗證趨勢、人流）。
- **`tests/`** — 單元與整合測試（pytest）。
- **`doc/`** — 規格、發現、API 協定。**`schema/`** — 資料表/欄位字典與 DQ 提示。

### 開發狀態（對應 `.cursor/plans/`）

- **Phase 1**：PLAN.md Step 0–10 均已實作完成（單一 Rated 模型、三軌特徵、DuckDB Track LLM、Feature Spec YAML 凍結進 artifact、閾值 F-beta 最大化）。
- **Track A（Featuretools DFS）已移除**：特徵工程僅保留三軌——Track Profile（PIT/as-of join）、Track LLM（DuckDB + YAML）、Track Human（向量化 `loss_streak`/`run_boundary`）。
- **Scorer / API**：僅對評級客（`is_rated`）產生告警；訓練結束後會清理舊版 `nonrated_model.pkl` / `rated_model.pkl`。
- **測試**：全量 `pytest` 約 519 passed；實作計畫與狀態詳見 `.cursor/plans/PLAN.md`、`.cursor/plans/STATUS.md`、`.cursor/plans/DECISION_LOG.md`。

### 環境設定

**需求**：Python 3.10+，執行 `pip install -r requirements.txt`。主要套件：`lightgbm`、`duckdb`、`optuna`、`shap`、`pandas`、`pyarrow`、`python-dotenv` 等。

**環境變數**：將 `trainer/.env.example` 複製為 `trainer/.env`（或設定對應環境變數），用於 ClickHouse：`CH_HOST`、`CH_TEAMDB_HOST`、`CH_PORT`、`CH_USER`、`CH_PASS`、`CH_SECURE`、`SOURCE_DB`。

**資料（訓練/回測）**：預設為 ClickHouse，請確認 `SOURCE_DB` 與憑證正確。本地 Parquet（開發/測試）：在專案根目錄放置 `data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`（可選 `data/player_profile.parquet`），執行 trainer 或 backtester 時加上 `--use-local-parquet`。

### 使用方式

**訓練（完整流程）**（在專案根目錄）：

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

**Backtester**：`python -m trainer.backtester --start "2025-01-01" --end "2025-01-31" --use-local-parquet`

**即時 scorer**：`python -m trainer.scorer --interval 45 --lookback-hours 8`（單次執行加 `--once`）。所有觀測用同一 rated 模型評分；**僅評級客（is_rated）會產生告警**，非評級客分數僅供 volume 統計（UNRATED_VOLUME_LOG）。

**Validator**：`python -m trainer.validator --interval 60`（單次加 `--once`）

**API 伺服器**：`python -m trainer.api_server`（預設 http://0.0.0.0:8000）

**Status server**：`python -m trainer.status_server`

**ETL / profile**：`trainer/etl_player_profile.py` 用於 profile 回填；`python -m trainer.scripts.auto_build_player_profile --start-date ... --end-date ...` 用於排程建置，詳見腳本說明。

### Trainer 指令參數（cmd flags）

| 參數 | 說明 |
|------|------|
| `--start` | 訓練視窗起日（YYYY-MM-DD 或 ISO）。須與 `--end` 同時指定，否則視窗由 `--days` 決定。 |
| `--end` | 訓練視窗迄日。須與 `--start` 同時指定。 |
| `--days` | 未給 `--start`/`--end` 時使用：取「迄日為現在減 30 分鐘」往前 N 天為視窗。預設由 `config.TRAINER_DAYS` 決定（通常 7）。 |
| `--use-local-parquet` | 從專案根目錄 `data/` 讀取 Parquet（`gmwds_t_bet.parquet`、`gmwds_t_session.parquet` 等），不連 ClickHouse。 |
| `--force-recompute` | 忽略已快取的 chunk Parquet（`trainer/.data/chunks/`），強制重新計算每個 chunk。 |
| `--skip-optuna` | 不跑 Optuna 超參搜尋，使用預設 LightGBM 超參。 |
| `--recent-chunks N` | 僅使用訓練視窗內「最後 N 個」月 chunk（每 chunk 約一個月）。限制從 ClickHouse 或本地 Parquet 載入的資料量；建議 N≥3 以保持 train/valid/test 皆有資料。例如 `--recent-chunks 3` 約為最近 3 個月。 |
| `--no-preload` | 關閉 profile backfill 時對 session Parquet 的「全表一次載入」，改為每 snapshot 日用 PyArrow pushdown 讀取。預設（不加此旗標）會完整載入整張 session 表格。適合 ≤8 GB RAM 機器，避免 OOM，代價是 backfill 速度較慢。 |
| `--sample-rated N` | 僅使用 N 個評級客（canonical_id 字典序取前 N 個）。預設不抽樣（使用全部評級客）。 |

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
| `doc/player_profile_daily_spec.md` | 玩家每日 profile ETL 與 PIT/as-of 語意 |
| `doc/model_api_protocol.md` | 模型與應用 API 協定（如 POST /score） |
| `doc/TRAINER_SUMMARY.md` | 系統摘要（架構、模組、前端） |
| `doc/TRAINER_TEAM_PRESENTATION.md` | 團隊向系統概覽 |
| `doc/PLAN_VS_TRAINER_COMPARISON.md` | 計畫與實作對照 |
| `doc/TRAINER_ISSUES.md` | 已知問題與備註 |
| `.cursor/plans/` | 實作計畫（PLAN.md）、狀態（STATUS.md）、決策紀錄（DECISION_LOG.md） |

### 產物（trainer 輸出）

`trainer/models/` 下：`model.pkl`（v10 單一評級客模型）、`feature_list.json`、`feature_spec.yaml`（DEC-024 凍結特徵規格，訓練時寫入 bundle，scorer 優先從此載入）、`reason_code_map.json`、`model_version`、`training_metrics.json`（僅 rated 指標）。另保留 legacy `walkaway_model.pkl`。訓練結束後若存在舊版 `nonrated_model.pkl` / `rated_model.pkl` 會自動刪除，避免 scorer/backtester 誤用。

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
- **`trainer/frontend/`** — 仪表盘 SPA（地图、告警、验证趋势、人流）。
- **`tests/`** — 单元与集成测试（pytest）。
- **`doc/`** — 规格、发现、API 协议。**`schema/`** — 表/字段字典与 DQ 提示。

### 开发状态（对应 `.cursor/plans/`）

- **Phase 1**：PLAN.md Step 0–10 均已实现完成（单一 Rated 模型、三轨特征、DuckDB Track LLM、Feature Spec YAML 冻结进 artifact、阈值 F-beta 最大化）。
- **Track A（Featuretools DFS）已移除**：特征工程仅保留三轨——Track Profile（PIT/as-of join）、Track LLM（DuckDB + YAML）、Track Human（向量化 `loss_streak`/`run_boundary`）。
- **Scorer / API**：仅对评级客（`is_rated`）产生告警；训练结束后会清理旧版 `nonrated_model.pkl` / `rated_model.pkl`。
- **测试**：全量 `pytest` 约 519 passed；实现计划与状态详见 `.cursor/plans/PLAN.md`、`.cursor/plans/STATUS.md`、`.cursor/plans/DECISION_LOG.md`。

### 环境设置

**需求**：Python 3.10+，执行 `pip install -r requirements.txt`。主要包：`lightgbm`、`duckdb`、`optuna`、`shap`、`pandas`、`pyarrow`、`python-dotenv` 等。

**环境变量**：将 `trainer/.env.example` 复制为 `trainer/.env`（或设置对应环境变量），用于 ClickHouse：`CH_HOST`、`CH_TEAMDB_HOST`、`CH_PORT`、`CH_USER`、`CH_PASS`、`CH_SECURE`、`SOURCE_DB`。

**数据（训练/回测）**：默认为 ClickHouse，请确认 `SOURCE_DB` 与凭证正确。本地 Parquet（开发/测试）：在项目根目录放置 `data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`（可选 `data/player_profile.parquet`），运行 trainer 或 backtester 时加上 `--use-local-parquet`。

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

**Backtester**：`python -m trainer.backtester --start "2025-01-01" --end "2025-01-31" --use-local-parquet`

**实时 scorer**：`python -m trainer.scorer --interval 45 --lookback-hours 8`（单次执行加 `--once`）。所有观测用同一 rated 模型评分；**仅评级客（is_rated）会产生告警**，非评级客分数仅供 volume 统计（UNRATED_VOLUME_LOG）。

**Validator**：`python -m trainer.validator --interval 60`（单次加 `--once`）

**API 服务**：`python -m trainer.api_server`（默认 http://0.0.0.0:8000）

**Status server**：`python -m trainer.status_server`

**ETL / profile**：`trainer/etl_player_profile.py` 用于 profile 回填；`python -m trainer.scripts.auto_build_player_profile --start-date ... --end-date ...` 用于定时构建，详见脚本说明。

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
| `doc/player_profile_daily_spec.md` | 玩家每日 profile ETL 与 PIT/as-of 语义 |
| `doc/model_api_protocol.md` | 模型与应用 API 协议（如 POST /score） |
| `doc/TRAINER_SUMMARY.md` | 系统摘要（架构、模块、前端） |
| `doc/TRAINER_TEAM_PRESENTATION.md` | 团队向系统概览 |
| `doc/PLAN_VS_TRAINER_COMPARISON.md` | 计划与实现对照 |
| `doc/TRAINER_ISSUES.md` | 已知问题与备注 |
| `.cursor/plans/` | 实现计划（PLAN.md）、状态（STATUS.md）、决策记录（DECISION_LOG.md） |

### 产物（trainer 输出）

`trainer/models/` 下：`model.pkl`（v10 单一评级客模型）、`feature_list.json`、`feature_spec.yaml`（DEC-024 冻结特征规格，训练时写入 bundle，scorer 优先从此载入）、`reason_code_map.json`、`model_version`、`training_metrics.json`（仅 rated 指标）。另保留 legacy `walkaway_model.pkl`。训练结束后若存在旧版 `nonrated_model.pkl` / `rated_model.pkl` 会自动删除，避免 scorer/backtester 误用。

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
- **`trainer/frontend/`** — Dashboard SPA (map, alerts, validation trends, headcount).
- **`tests/`** — Unit and integration tests (pytest).
- **`doc/`** — Specs, findings, API protocol. **`schema/`** — Table/column dictionary and DQ hints.

### Development status (see `.cursor/plans/`)

- **Phase 1**: PLAN.md Steps 0–10 are implemented (single Rated model, three-track features, DuckDB Track LLM, Feature Spec YAML frozen into artifact, F-beta threshold maximization).
- **Track A (Featuretools DFS) removed**: Feature engineering is three-track only — Track Profile (PIT/as-of join), Track LLM (DuckDB + YAML), Track Human (vectorized `loss_streak`/`run_boundary`).
- **Scorer / API**: Alerts are emitted only for rated patrons (`is_rated`); stale `nonrated_model.pkl` / `rated_model.pkl` are cleaned up after training.
- **Tests**: Full `pytest` ~519 passed; see `.cursor/plans/PLAN.md`, `.cursor/plans/STATUS.md`, `.cursor/plans/DECISION_LOG.md` for plan and status.

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

### Data (for training / backtest)

- **ClickHouse**: Default. Ensure `SOURCE_DB` and credentials are correct.
- **Local Parquet (dev/test)**:
  - Place exports in project root: `data/gmwds_t_bet.parquet`, `data/gmwds_t_session.parquet` (and optionally `data/player_profile.parquet`).
  - Use `--use-local-parquet` when running the trainer or backtester.

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
```

### Live scorer (polling + alerts)

```bash
python -m trainer.scorer --interval 45 --lookback-hours 8
# Single run: --once
```

All observations are scored with the single rated model (v10). **Alerts are emitted only for rated patrons** (`is_rated`); unrated scores are used for volume telemetry only (UNRATED_VOLUME_LOG).

### Validator (match/miss vs realized walkaways)

```bash
python -m trainer.validator --interval 60
# Single pass: --once
```

### API server (dashboard backend)

```bash
python -m trainer.api_server
# Serves on http://0.0.0.0:8000
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
| `doc/player_profile_daily_spec.md` | Player profile daily ETL and PIT/as-of semantics |
| `doc/model_api_protocol.md` | Model–app API contract (e.g. POST /score) for decoupled inference |
| `doc/TRAINER_SUMMARY.md` | System summary (architecture, modules, frontend) |
| `doc/TRAINER_TEAM_PRESENTATION.md` | Team-facing overview |
| `doc/PLAN_VS_TRAINER_COMPARISON.md` | Plan vs implementation comparison |
| `doc/TRAINER_ISSUES.md` | Known issues / notes |
| `.cursor/plans/` | Implementation plan (PLAN.md), status (STATUS.md), decision log (DECISION_LOG.md) |

---

## Artifacts (trainer output)

Under `trainer/models/`:

- `model.pkl` — Single rated LightGBM model (v10 DEC-021)
- `feature_list.json` — Feature names and track classification
- `feature_spec.yaml` — Frozen feature spec snapshot (DEC-024) written at training time; scorer loads this first for train–serve consistency
- `reason_code_map.json` — Feature-to-reason-code mapping for SHAP
- `model_version` — Version string (e.g. `20260228-153000-abc1234`)
- `training_metrics.json` — Rated metrics only; flags such as `uncalibrated_threshold`, `sample_rated_n`

Legacy `walkaway_model.pkl` is still written for backward compatibility. After each run, any stale `nonrated_model.pkl` or `rated_model.pkl` from older dual-model runs is removed so the scorer and backtester do not load them.

---

## Notes

- **Credentials**: Store ClickHouse credentials securely; avoid committing `.env`.
- **Time zone**: Business logic uses `Asia/Hong_Kong` (see `config.HK_TZ`).
- **Threshold selection**: Phase 1 uses validation-set **F-beta maximization** (default β=0.5, precision-weighted) for the single-model threshold (DEC-009, DEC-021); optional min recall / alerts-per-hour constraints; see `config.THRESHOLD_FBETA`.
- **Alert scope**: The scorer and API `POST /score` return `alert=true` only for rated patrons (`is_rated=true`); unrated rows still receive a score but `alert` is always `false`.
