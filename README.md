# Patron Walkaway

---

## 中文（繁體）

### 專案簡介

Patron Walkaway 離場偵測專案。

我們的大堂已部署 Smart Table 技術，可即時擷取每位賓客（不論是否為評級客）的下注行為。目標是即時預測賓客是否將在未來 15 分鐘內停止博彩並離開，以便主持人能即時接觸並挽留。

### 概述

- **Phase 1** 實作：雙模型（評級 / 非評級）LightGBM 流程，含 Optuna 超參數搜尋、run-level 樣本權重、Track A（Featuretools DFS）與 Track B（如 `loss_streak`、`run_boundary`）特徵、身分對應與告警驗證。
- **資料**：ClickHouse（`GDP_GMWDS_Raw`）或開發用本地 Parquet（置於 `data/`）。
- **產出**：訓練產物在 `trainer/models/`（rated/nonrated `.pkl`、特徵清單、原因碼、模型版本）；即時 scorer 將告警寫入 SQLite；API 與前端儀表板供營運使用。

### 架構（高層）

```
ClickHouse ──► trainer.py ──► models/ (rated_model.pkl, nonrated_model.pkl, …)
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

### 環境設定

**需求**：Python 3.10+，執行 `pip install -r requirements.txt`。主要套件：`lightgbm`、`featuretools`、`optuna`、`shap`、`pandas`、`pyarrow`、`python-dotenv` 等。

**環境變數**：將 `trainer/.env.example` 複製為 `trainer/.env`（或設定對應環境變數），用於 ClickHouse：`CH_HOST`、`CH_TEAMDB_HOST`、`CH_PORT`、`CH_USER`、`CH_PASS`、`CH_SECURE`、`SOURCE_DB`。

**資料（訓練/回測）**：預設為 ClickHouse，請確認 `SOURCE_DB` 與憑證正確。本地 Parquet（開發/測試）：在專案根目錄放置 `data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`（可選 `data/player_profile_daily.parquet`），執行 trainer 或 backtester 時加上 `--use-local-parquet`。

### 使用方式

**訓練（完整流程）**（在專案根目錄）：

```bash
python -m trainer.trainer
python -m trainer.trainer --use-local-parquet --recent-chunks 3
python -m trainer.trainer --skip-optuna --use-local-parquet
```

**Fast mode（僅供測試，筆電約 &lt;10 分鐘）**：限制可用資料時間範圍（Data Horizon），所有子模組（identity / labels / features / profile ETL）共用同一個時間邊界，以較少資料量加速訓練；可選擇性搭配 `--sample-rated N` 只抽樣部分評級客。**請勿將產物用於生產。**

```bash
python -m trainer.trainer --fast-mode --recent-chunks 1 --use-local-parquet
```

低記憶體（如 8 GB）：加上 `--fast-mode-no-preload`：

```bash
python -m trainer.trainer --fast-mode --fast-mode-no-preload --recent-chunks 3 --use-local-parquet
```

如需在 Fast mode 下只使用部分評級客，可加入（與 `--fast-mode` 正交）：

```bash
python -m trainer.trainer --fast-mode --recent-chunks 3 --use-local-parquet --sample-rated 1000
```

**Backtester**：`python -m trainer.backtester --start "2025-01-01" --end "2025-01-31" --use-local-parquet`

**即時 scorer**：`python -m trainer.scorer --interval 45 --lookback-hours 8`（單次執行加 `--once`）

**Validator**：`python -m trainer.validator --interval 60`（單次加 `--once`）

**API 伺服器**：`python -m trainer.api_server`（預設 http://0.0.0.0:8000）

**Status server**：`python -m trainer.status_server`

**ETL / profile**：`trainer/etl_player_profile.py` 用於 profile 回填；`python -m trainer.scripts.auto_build_player_profile --start-date ... --end-date ...` 用於排程建置，詳見腳本說明。

### 測試

全部測試：`pytest`  
僅 trainer 相關：`pytest tests/test_trainer.py -v`  
Fast-mode 煙測：`python -m trainer.trainer --fast-mode --recent-chunks 1 --use-local-parquet`

### 文件

| 文件 | 說明 |
|------|------|
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` | 資料表/欄位字典與 DQ 備註 |
| `doc/FINDINGS.md` | 資料品質與行為發現（可重現 SQL） |
| `doc/player_profile_daily_spec.md` | 玩家每日 profile ETL 與 PIT/as-of 語意 |
| `doc/model_api_protocol.md` | 模型與應用 API 協定（如 POST /score） |
| `doc/TRAINER_SUMMARY.md` | 系統摘要（架構、模組、前端） |
| 其餘見 `doc/` 與 `.cursor/plans/` | 計畫與決策紀錄 |

### 產物（trainer 輸出）

`trainer/models/` 下：`rated_model.pkl`、`nonrated_model.pkl`、`saved_feature_defs/`、`feature_list.json`、`reason_code_map.json`、`model_version`、`training_metrics.json`。另保留 legacy `walkaway_model.pkl`。

### 注意事項

- **Fast-mode** 產物在 metadata 中標記 `fast_mode=true`，不得用於生產推論。
- **憑證**：請安全存放 ClickHouse 憑證，勿提交 `.env`。
- **時區**：業務邏輯使用 `Asia/Hong_Kong`（`config.HK_TZ`）。

---

## 中文（简体）

### 项目简介

Patron Walkaway 离场检测项目。

我们的大堂已部署 Smart Table 技术，可实时采集每位宾客（不论是否为评级客）的下注行为。目标是在实时预测宾客是否将在未来 15 分钟内停止博彩并离开，以便主持人能及时接触并挽留。

### 概述

- **Phase 1** 实现：双模型（评级 / 非评级）LightGBM 流程，含 Optuna 超参搜索、run-level 样本权重、Track A（Featuretools DFS）与 Track B（如 `loss_streak`、`run_boundary`）特征、身份映射与告警验证。
- **数据**：ClickHouse（`GDP_GMWDS_Raw`）或开发用本地 Parquet（置于 `data/`）。
- **产出**：训练产物在 `trainer/models/`（rated/nonrated `.pkl`、特征列表、原因码、模型版本）；实时 scorer 将告警写入 SQLite；API 与前端仪表盘供运营使用。

### 架构（高层）

```
ClickHouse ──► trainer.py ──► models/ (rated_model.pkl, nonrated_model.pkl, …)
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

### 环境设置

**需求**：Python 3.10+，执行 `pip install -r requirements.txt`。主要包：`lightgbm`、`featuretools`、`optuna`、`shap`、`pandas`、`pyarrow`、`python-dotenv` 等。

**环境变量**：将 `trainer/.env.example` 复制为 `trainer/.env`（或设置对应环境变量），用于 ClickHouse：`CH_HOST`、`CH_TEAMDB_HOST`、`CH_PORT`、`CH_USER`、`CH_PASS`、`CH_SECURE`、`SOURCE_DB`。

**数据（训练/回测）**：默认为 ClickHouse，请确认 `SOURCE_DB` 与凭证正确。本地 Parquet（开发/测试）：在项目根目录放置 `data/gmwds_t_bet.parquet`、`data/gmwds_t_session.parquet`（可选 `data/player_profile_daily.parquet`），运行 trainer 或 backtester 时加上 `--use-local-parquet`。

### 使用方式

**训练（完整流程）**（在项目根目录）：

```bash
python -m trainer.trainer
python -m trainer.trainer --use-local-parquet --recent-chunks 3
python -m trainer.trainer --skip-optuna --use-local-parquet
```

**Fast mode（仅供测试，笔记本约 &lt;10 分钟）**：限制可用数据时间范围（Data Horizon），所有子模块（identity / labels / features / profile ETL）共用同一个时间边界，以较少数据量加速训练；可选择性搭配 `--sample-rated N` 只抽样部分评级客。**请勿将产物用于生产。**

```bash
python -m trainer.trainer --fast-mode --recent-chunks 1 --use-local-parquet
```

低内存（如 8 GB）：加上 `--fast-mode-no-preload`：

```bash
python -m trainer.trainer --fast-mode --fast-mode-no-preload --recent-chunks 3 --use-local-parquet
```

如需在 Fast mode 下只使用部分评级客，可加入（与 `--fast-mode` 正交）：

```bash
python -m trainer.trainer --fast-mode --recent-chunks 3 --use-local-parquet --sample-rated 1000
```

**Backtester**：`python -m trainer.backtester --start "2025-01-01" --end "2025-01-31" --use-local-parquet`

**实时 scorer**：`python -m trainer.scorer --interval 45 --lookback-hours 8`（单次执行加 `--once`）

**Validator**：`python -m trainer.validator --interval 60`（单次加 `--once`）

**API 服务**：`python -m trainer.api_server`（默认 http://0.0.0.0:8000）

**Status server**：`python -m trainer.status_server`

**ETL / profile**：`trainer/etl_player_profile.py` 用于 profile 回填；`python -m trainer.scripts.auto_build_player_profile --start-date ... --end-date ...` 用于定时构建，详见脚本说明。

### 测试

全部测试：`pytest`  
仅 trainer 相关：`pytest tests/test_trainer.py -v`  
Fast-mode 烟测：`python -m trainer.trainer --fast-mode --recent-chunks 1 --use-local-parquet`

### 文档

| 文档 | 说明 |
|------|------|
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` | 表/字段字典与 DQ 备注 |
| `doc/FINDINGS.md` | 数据质量与行为发现（可重现 SQL） |
| `doc/player_profile_daily_spec.md` | 玩家每日 profile ETL 与 PIT/as-of 语义 |
| `doc/model_api_protocol.md` | 模型与应用 API 协议（如 POST /score） |
| `doc/TRAINER_SUMMARY.md` | 系统摘要（架构、模块、前端） |
| 其余见 `doc/` 与 `.cursor/plans/` | 计划与决策记录 |

### 产物（trainer 输出）

`trainer/models/` 下：`rated_model.pkl`、`nonrated_model.pkl`、`saved_feature_defs/`、`feature_list.json`、`reason_code_map.json`、`model_version`、`training_metrics.json`。另保留 legacy `walkaway_model.pkl`。

### 注意事项

- **Fast-mode** 产物在 metadata 中标记 `fast_mode=true`，不得用于生产推理。
- **凭证**：请安全存放 ClickHouse 凭证，勿提交 `.env`。
- **时区**：业务逻辑使用 `Asia/Hong_Kong`（`config.HK_TZ`）。

---

## English

Patron Walkaway Detection project.

Our mass gaming floor has deployed Smart Table technology through which we are able to capture the betting behavior of every patron (rated or not) in real-time. The goal is to detect in real-time whether a gaming patron will stop gaming and leave in the upcoming 15 minutes, so that our hosts can approach and retain them.

## Overview

- **Phase 1** implementation: dual-model (rated / non-rated) LightGBM pipeline with Optuna hyperparameter search, run-level sample weighting, Track A (Featuretools DFS) + Track B (e.g. `loss_streak`, `run_boundary`) features, identity mapping, and alert validation.
- **Data**: ClickHouse (`GDP_GMWDS_Raw`) or local Parquet under `data/` for development.
- **Output**: Trained artifacts in `trainer/models/` (rated/non-rated `.pkl`, feature list, reason codes, model version); live scorer writes alerts to SQLite; API + frontend dashboard for operators.

---

## Architecture (high level)

```
ClickHouse ──► trainer.py ──► models/ (rated_model.pkl, nonrated_model.pkl, …)
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

---

## Setup

### Requirements

- Python 3.10+
- Install dependencies:

  ```bash
  pip install -r requirements.txt
  ```

  Key packages: `lightgbm`, `featuretools`, `optuna`, `shap`, `pandas`, `pyarrow`, `python-dotenv`, etc.

### Environment

Copy `trainer/.env.example` to `trainer/.env` (or set env vars) for ClickHouse:

- `CH_HOST`, `CH_TEAMDB_HOST`, `CH_PORT`, `CH_USER`, `CH_PASS`, `CH_SECURE`, `SOURCE_DB`

### Data (for training / backtest)

- **ClickHouse**: Default. Ensure `SOURCE_DB` and credentials are correct.
- **Local Parquet (dev/test)**:
  - Place exports in project root: `data/gmwds_t_bet.parquet`, `data/gmwds_t_session.parquet` (and optionally `data/player_profile_daily.parquet`).
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

### Fast mode (testing only, &lt;10 min on laptop)

Constrains the available data time range (Data Horizon); all submodules (identity / labels / features / profile ETL) share the same time boundary so that a smaller slice of data is processed for faster iteration. You can optionally combine with `--sample-rated N` to train on a subset of rated patrons. **Do not use artifacts in production.**

```bash
python -m trainer.trainer --fast-mode --recent-chunks 1 --use-local-parquet
```

Low-RAM (e.g. 8 GB): add `--fast-mode-no-preload` to avoid loading the full session Parquet into memory:

```bash
python -m trainer.trainer --fast-mode --fast-mode-no-preload --recent-chunks 3 --use-local-parquet
```

To use only a subset of rated patrons under Fast mode (orthogonal to `--fast-mode`):

```bash
python -m trainer.trainer --fast-mode --recent-chunks 3 --use-local-parquet --sample-rated 1000
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

## Testing

Run all tests:

```bash
pytest
```

Run only trainer-related tests:

```bash
pytest tests/test_trainer.py -v
```

Fast-mode smoke test (requires local Parquet data):

```bash
python -m trainer.trainer --fast-mode --recent-chunks 1 --use-local-parquet
# Check trainer/models/ or training output for fast_mode=true in metadata
```

---

## Documentation

| Document | Description |
|----------|-------------|
| `schema/GDP_GMWDS_Raw_Schema_Dictionary.md` | Table/column dictionary and short DQ notes |
| `doc/FINDINGS.md` | Data quality and behavior findings (reproducible SQL) |
| `doc/player_profile_daily_spec.md` | Player profile daily ETL and PIT/as-of semantics |
| `doc/model_api_protocol.md` | Model–app API contract (e.g. POST /score) for decoupled inference |
| `doc/TRAINER_SUMMARY.md` | System summary (architecture, modules, frontend) |
| `doc/TRAINER_TEAM_PRESENTATION.md` | Team-facing overview |
| `doc/PLAN_VS_TRAINER_COMPARISON.md` | Plan vs implementation comparison |
| `doc/TRAINER_ISSUES.md` | Known issues / notes |

Internal planning (optional): `.cursor/plans/PLAN.md`, `STATUS.md`, `DECISION_LOG.md`.

---

## Artifacts (trainer output)

Under `trainer/models/`:

- `rated_model.pkl`, `nonrated_model.pkl` — LightGBM models
- `saved_feature_defs/` — Featuretools save_features output (Track A)
- `feature_list.json`, `reason_code_map.json` — Feature names and reason codes
- `model_version` — Version string (e.g. `20260228-153000-abc1234`)
- `training_metrics.json` — Metrics and flags (e.g. `fast_mode`)

Legacy single `walkaway_model.pkl` is still written for backward compatibility.

---

## Notes

- **Fast-mode** outputs are marked `fast_mode=true` in metadata and must not be used for production inference.
- **Credentials**: Store ClickHouse credentials securely; avoid committing `.env`.
- **Time zone**: Business logic uses `Asia/Hong_Kong` (see `config.HK_TZ`).
