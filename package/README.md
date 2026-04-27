# Package: Model bundle & ML API

This folder provides:

1. **Deploy package** — One folder (or one `.zip`) you copy to the target machine and run there. It includes the app, model, and `requirements.txt`; no repo is needed on the target.
2. **ML API server** — GET `/alerts` and GET `/validation` at `http://localhost:8001` for the dashboard (contract: `package/ML_API_PROTOCOL.md`, also copied into each deploy bundle as `ML_API_PROTOCOL.md`).

Full design: **PLAN.md** in this folder.

---

## How to use

### 1. Build the deploy package (ship to target)

Deployment is **always** a single folder or archive that you copy to the target and run there. Build it from the **repository root**. Step-by-step text for the bundle is edited in **`README_DEPLOY.md`** in this folder; the build copies it into each output as `README_DEPLOY.md`.

```bash
# Default: model from out/models (or MODEL_DIR), output = deploy_dist/ (at repo root)
python -m package.build_deploy_package

# Optional: single file for transfer (e.g. deploy_dist.zip)
python -m package.build_deploy_package --archive

# Use a specific model directory (e.g. trainer/models_90d_weak)
python -m package.build_deploy_package --model-source trainer/models_90d_weak --archive

# Production: fail the build if player_profile.parquet is missing under ./data
python -m package.build_deploy_package --archive --strict-data

# Use a custom directory for serving artifacts (profile, optional canonical mapping)
python -m package.build_deploy_package --archive --data-source path/to/serving_data
```

| Option | Default | Description |
|--------|---------|-------------|
| `--model-source` | `out/models` or `MODEL_DIR` env | Directory with model artifacts (**`model.pkl`** required per DEC-040, `feature_list.json`, etc.). |
| `--output-dir` | `deploy_dist` | Output folder (default: ./deploy_dist at repo root). |
| `--archive` | Off | Also create `deploy_dist.zip` in the parent of output-dir for a single-file transfer. |
| `--data-source` | `data` | Directory (under repo root if relative) containing **serving** data: `player_profile.parquet`, optional `canonical_mapping.parquet` + `canonical_mapping.cutoff.json`, optional `player_profile.schema_hash`. Raw `gmwds_t_*.parquet` mirrors are **not** copied; live bet/session/game come from ClickHouse on the target. |
| `--strict-data` | Off | Exit non-zero if `player_profile.parquet` is missing or cannot be copied (recommended for production bundles when the model uses profile features). |

**Result:** A folder `deploy_dist/` (and optionally `deploy_dist.zip`) at repo root, containing everything needed on the target: `main.py`, `requirements.txt` (includes **numba**, **pyarrow**, and other serving deps), `.env.example`, `ML_API_PROTOCOL.md`, `wheels/`, `models/`, **`data/`** (serving artifacts when present at build time), `README_DEPLOY.md`, etc.

**Frontend:** The default build **does not include** the dashboard SPA (`trainer/frontend/`). The deploy package is API-only (GET `/alerts`, `/validation`). If you need the dashboard, serve it separately from the repo or add it to the build in a future step; static assets would then live under the deploy output (e.g. `deploy_dist/static/` or similar).

**On the target machine:** Copy the folder (or unzip the .zip), then:

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`, set **`CH_USER`** and **`CH_PASS`** (required), and uncomment any optional vars (log level, scorer windows, paths — see comments in the file).
3. `python main.py`

Endpoints: `http://0.0.0.0:8001/alerts`, `/validation`. See **README_DEPLOY.md** inside the package for step-by-step and platform-specific commands.

---

### 2. Run the ML API server (local dev)

From the **repository root** (for local testing with dashboard):

```bash
python -m trainer.api_server
```

**Note:** Trainer components (validator, scorer, etl_player_profile, trainer, status_server, etc.) must be run as a package (e.g. `python -m trainer.validator`). Direct script execution (e.g. `python trainer/validator.py`) is not supported; the deploy entrypoint uses the same package-style imports.

- **Base URL:** `http://localhost:8001`
- **Port override:** `ML_API_PORT=8002 python -m trainer.api_server`

The server reads from `trainer/local_state/state.db`. Run the **scorer** (and optionally **validator**) to populate data.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/alerts` | Walkaway alerts (query: `ts`, `limit`; no params = last 1h) |
| GET | `/validation` | Validation results (query: `ts`, `bet_id`, `bet_ids`; no params = last 1h) |

Example: `curl "http://localhost:8001/alerts"`. Full format: **`doc/ML_API_PROTOCOL.md`**.

---

### 3. End-to-end flow

1. **Train** — Run the training pipeline so that `trainer/models/` (or your chosen dir) has model artifacts.
2. **Build deploy package** — `python -m package.build_deploy_package [--model-source ...] [--archive] [--data-source ...] [--strict-data]` → `deploy_dist/` (and optionally `.zip`) at repo root. Ensure `./data/player_profile.parquet` exists before building, or use `--strict-data` so CI fails if it is missing.
3. **Ship** — Copy the folder or the `.zip` to the target machine.
4. **On target** — Unzip if needed, `pip install -r requirements.txt`, configure `.env`, `python main.py`.
5. **Dashboard** polls `http://<target>:8001/alerts` and `/validation`.

**Build player_profile (month-end only, same schedule as trainer):**

```bash
python -m trainer.etl_player_profile --start-date YYYY-MM-DD --end-date YYYY-MM-DD --local-parquet --month-end
python -m trainer.scripts.auto_build_player_profile --local-parquet --month-end
```

---

## Files in this folder

| File | Purpose |
|------|---------|
| **README.md** | This file — deploy package and API usage. |
| **PLAN.md** | Full plan: bundle contents, API behavior, implementation notes. |
| **build_deploy_package.py** | Build the shippable deploy folder (or .zip): app + model + requirements. Use this to deploy to the target. |
| **deploy_dist/** | Output of build_deploy_package (at repo root) — copy this folder or its .zip to the target. |
| **deploy/** | Source for main.py and config used by build_deploy_package. See deploy/README.md. |

---

## Troubleshooting

- **Profile not shipped / scorer NaN profile features** — Ensure `player_profile.parquet` exists under your `--data-source` directory before building, or use `--strict-data` so the build fails instead of producing a degraded bundle. On the target, profile is read from `deploy_dist/data/player_profile.parquet` (set via `DATA_DIR` by `main.py`).
- **"No model artifact found"** — Run the trainer first so that `trainer/models/` (or your `--model-source`) contains **`model.pkl`** and `feature_list.json` (DEC-040: `rated_model.pkl` / `walkaway_model.pkl` are not accepted as substitutes).
- **Empty `/alerts` or `/validation`** — The server reads from `trainer/local_state/state.db`. Run the scorer (and validator) to populate data.
- **Port in use** — Set another port with `ML_API_PORT=8002 python -m trainer.api_server`.

---

# Package：模型包與 ML API（中文）

本目錄提供：

1. **部署包** — 單一資料夾（或單一 `.zip`），複製到目標機即可在該機執行。內含應用、模型與 `requirements.txt`，目標機不需 repo。
2. **ML API 服務** — 在 `http://localhost:8001` 提供 GET `/alerts` 與 GET `/validation` 給儀表板使用（協定：`package/ML_API_PROTOCOL.md`；建包時會複製到部署目錄的 `ML_API_PROTOCOL.md`）。

完整設計見本目錄 **PLAN.md**。

---

## 使用方式

### 1. 建置部署包（可搬至目標機）

部署**一律**為單一資料夾或壓縮檔：複製到目標機後在該機執行。包內步驟說明請編輯本資料夾的 **`README_DEPLOY.md`**；建置時會複製到輸出為 `README_DEPLOY.md`。請在 **專案根目錄** 執行：

```bash
# 預設：模型來自 out/models（或 MODEL_DIR），輸出 = deploy_dist/（專案根目錄）
python -m package.build_deploy_package

# 可選：產生單一壓縮檔（例如 deploy_dist.zip）
python -m package.build_deploy_package --archive

# 指定模型目錄（例如 trainer/models_90d_weak）
python -m package.build_deploy_package --model-source trainer/models_90d_weak --archive

# 正式環境：若 ./data 下缺 player_profile.parquet 則建包失敗
python -m package.build_deploy_package --archive --strict-data

# 自訂 serving 資料目錄（profile、可選 canonical）
python -m package.build_deploy_package --archive --data-source path/to/serving_data
```

| 選項 | 預設 | 說明 |
|--------|---------|-------------|
| `--model-source` | `out/models` 或環境變數 `MODEL_DIR` | 模型產物目錄（**必須**含 **`model.pkl`**（DEC-040）、`feature_list.json` 等）。 |
| `--output-dir` | `deploy_dist` | 輸出資料夾（預設為專案根目錄 ./deploy_dist）。 |
| `--archive` | 關閉 | 另在輸出目錄上一層產生 `deploy_dist.zip`，便於單檔傳輸。 |
| `--data-source` | `data` | 相對路徑則在專案根下解析；內含 **serving** 資料：`player_profile.parquet`、可選 `canonical_mapping.parquet` + `canonical_mapping.cutoff.json`、可選 `player_profile.schema_hash`。不會複製 `gmwds_t_*.parquet`；線上注單/session/game 仍由目標機 ClickHouse 提供。 |
| `--strict-data` | 關閉 | 缺 `player_profile.parquet` 或複製失敗時建包非零結束（模型依賴 profile 時建議正式建包開啟）。 |

**結果：** 在專案根目錄產生 `deploy_dist/`（及可選的 `deploy_dist.zip`），內含目標機所需一切：`main.py`、`requirements.txt`（含 **numba**、**pyarrow** 等 serving 相依）、`.env.example`、`ML_API_PROTOCOL.md`、`wheels/`、`models/`、**`data/`**（建包時若來源目錄有則帶出 serving artifacts）、`README_DEPLOY.md` 等。

**前端：** 預設建包**不含**儀表板 SPA（`trainer/frontend/`），部署包僅含 API（GET `/alerts`、`/validation`）。若需儀表板，請自 repo 另行提供或於日後建包時一併帶出；若含前端，靜態檔將置於部署輸出目錄下（例如 `deploy_dist/static/`）。

**在目標機上：** 複製該資料夾（或解壓 .zip）後：

1. `pip install -r requirements.txt`
2. 將 `.env.example` 複製為 `.env`，填寫 **`CH_USER`**、**`CH_PASS`**（必填），其餘選項見檔內註解（日誌層級、scorer 視窗、路徑等）。
3. `python main.py`

端點：`http://0.0.0.0:8001/alerts`、`/validation`。詳見包內 **README_DEPLOY.md** 的步驟與各平台指令。

---

### 2. 啟動 ML API 服務（本機開發）

在 **專案根目錄** 執行（供本機與儀表板測試）：

```bash
python -m trainer.api_server
```

- **Base URL：** `http://localhost:8001`
- **改 port：** `ML_API_PORT=8002 python -m trainer.api_server`

服務從 `trainer/local_state/state.db` 讀取。請先執行 **scorer**（及可選的 **validator**）寫入資料。

| 方法 | 路徑 | 用途 |
|--------|------|---------|
| GET | `/alerts` | 離桌告警（query：`ts`、`limit`；無參數 = 最近 1 小時） |
| GET | `/validation` | 驗證結果（query：`ts`、`bet_id`、`bet_ids`；無參數 = 最近 1 小時） |

範例：`curl "http://localhost:8001/alerts"`。完整格式見 **`doc/ML_API_PROTOCOL.md`**。

---

### 3. 端到端流程

1. **訓練** — 執行訓練流程，產出 `trainer/models/`（或自訂目錄）之模型產物。
2. **建置部署包** — `python -m package.build_deploy_package [--model-source ...] [--archive] [--data-source ...] [--strict-data]` → 專案根目錄的 `deploy_dist/`（及可選 `.zip`）。建包前請確認 `./data/player_profile.parquet` 已就緒，或加上 `--strict-data` 讓 CI 在缺檔時失敗。
3. **搬運** — 將該資料夾或 `.zip` 複製到目標機。
4. **目標機** — 若有 .zip 先解壓，執行 `pip install -r requirements.txt`、設定 `.env`、`python main.py`。
5. **儀表板** 輪詢 `http://<目標機>:8001/alerts` 與 `/validation`。

**僅建每月（month-end）player_profile snapshot（與 trainer 排程一致）：**

```bash
python -m trainer.etl_player_profile --start-date YYYY-MM-DD --end-date YYYY-MM-DD --local-parquet --month-end
python -m trainer.scripts.auto_build_player_profile --local-parquet --month-end
```

---

## 本目錄檔案

| 檔案 | 用途 |
|------|---------|
| **README.md** | 本說明 — 部署包與 API 使用方式。 |
| **PLAN.md** | 完整計畫：bundle 內容、API 行為與實作說明。 |
| **build_deploy_package.py** | 建置可搬運的部署資料夾（或 .zip）：應用 + 模型 + requirements。部署至目標機請用此腳本。 |
| **deploy_dist/** | build_deploy_package 的輸出（專案根目錄）— 將此資料夾或其 .zip 複製到目標機。 |
| **deploy/** | build_deploy_package 使用的 main.py 與設定來源。詳見 deploy/README.md。 |

---

## 常見問題

- **建包顯示 profile 未帶出／線上 profile 為 NaN** — 建包前請在 `--data-source` 目錄（預設專案根 `data/`）放置 `player_profile.parquet`；正式環境建議加 `--strict-data` 讓缺檔時建包失敗。目標機上 profile 路徑為 `deploy_dist/data/player_profile.parquet`（由 `main.py` 設定 `DATA_DIR`）。
- **「No model artifact found」** — 請先執行訓練，讓 `trainer/models/`（或您的 `--model-source`）內含 **`model.pkl`** 與 `feature_list.json`（DEC-040：不接受以 `rated_model.pkl`／`walkaway_model.pkl` 替代）。
- **`/alerts` 或 `/validation` 回傳空** — 服務從 `trainer/local_state/state.db` 讀取。請先執行 scorer（及 validator）寫入資料。
- **Port 被佔用** — 可改用其他 port：`ML_API_PORT=8002 python -m trainer.api_server`。
