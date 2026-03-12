# Package: Model bundle & ML API

This folder provides:

1. **Model bundle packaging** — After training, pack only the artifacts needed for inference into a versioned bundle (and optional `.tar.gz`) for deployment.
2. **ML API server** — A server that exposes GET `/alerts` and GET `/validation` at `http://localhost:8001` for the dashboard (see `doc/ML_API_PROTOCOL.md`).

Full design and scope: **PLAN.md** in this folder.

---

## How to use

### 1. Package the trained model (after training)

Run from the **repository root**:

```bash
# Use defaults: source = trainer/models, output = package/bundles
python -m package.package_model_bundle

# With optional archive (creates .tar.gz for transfer)
python -m package.package_model_bundle --archive

# Custom paths and version
python -m package.package_model_bundle --source-dir trainer/models --output-dir package/bundles --version 1.0.0 --archive
```

| Option | Default | Description |
|--------|---------|-------------|
| `--source-dir` | `trainer/models` | Directory where the trainer wrote artifacts (`model.pkl`, `feature_list.json`, etc.). |
| `--output-dir` | `package/bundles` | Directory where the versioned bundle folder will be created. |
| `--version` | From `model_version` file or timestamp | Version label for the bundle folder and archive name. |
| `--archive` | Off | If set, also create `model_bundle_<version>.tar.gz` in the output directory. |

**Result:** A folder `package/bundles/<version>/` (and optionally `model_bundle_<version>.tar.gz`) containing only the files needed for the scorer. Deploy by copying this folder to the target machine and pointing the scorer at it (e.g. `--model-dir package/bundles/1.0.0`).

---

### 2. Run the ML API server

Run from the **repository root** (so `trainer` and `package` are on the path):

```bash
python -m trainer.api_server
```

- **Base URL:** `http://localhost:8001`
- **Port override:** `ML_API_PORT=8002 python -m trainer.api_server`

The server reads alerts and validation results from `trainer/local_state/state.db`. Ensure the **scorer** (and optionally the **validator**) have been run so that table has data.

**Protocol endpoints:**

| Method | Path | Purpose |
|--------|------|--------|
| GET | `/alerts` | Walkaway alerts (query: `ts`, `limit`; no params = last 24h) |
| GET | `/validation` | Validation results (query: `ts`, `bet_id`, `bet_ids`; no params = last 24h) |

Example:

```bash
# Alerts from the last 24 hours (default)
curl "http://localhost:8001/alerts"

# Alerts after a timestamp
curl "http://localhost:8001/alerts?ts=2026-03-11T00:00:00%2B08:00"

# Validation for specific bet IDs
curl "http://localhost:8001/validation?bet_ids=123,456"
```

Full request/response format: **`doc/ML_API_PROTOCOL.md`**.

---

### 3. Deploy package (package/deploy/)

A **self-contained deploy** runs scorer + validator + Flask in one process, reading from ClickHouse and exposing GET `/alerts` and GET `/validation`. The app package is **walkaway_ml** (the same code as `trainer/`, installed under that name for deploy).

- **Setup:** Copy `package/deploy/.env.example` to `package/deploy/.env` and set ClickHouse credentials (`CH_HOST`, `CH_USER`, `CH_PASS`, `SOURCE_DB`, etc.). Put the model bundle in `package/deploy/models/` (e.g. copy from `package/bundles/<version>/`).
- **Install:** From repo root: `pip install -r package/deploy/requirements.txt` (installs walkaway_ml and dependencies).
- **Run:** From repo root: `python package/deploy/main.py`. Listens on port 8001 by default (`PORT` or `ML_API_PORT` to override).
- **Packaging:** Use **`.deployignore`** when building a tar/zip of `package/deploy/` (excludes `.venv/`, `__pycache__/`, `.env`, etc.). See `package/deploy/README.md` for full steps.

**Swapping the model:** Replace the contents of `package/deploy/models/` with a new bundle (from `package_model_bundle.py`), then restart `main.py`. No code changes.

---

### 3b. Single deploy package (one folder or file to move)

To get **one folder or one .zip file** that you can copy to the target machine (no repo needed there), run from repo root:

```bash
python -m package.build_deploy_package
# Optional: also create a single file for transfer
python -m package.build_deploy_package --archive
```

- **Output:** `package/deploy_dist/` (folder) and, with `--archive`, `package/deploy_dist.zip` (single file; Windows-friendly).
- **Contents:** walkaway_ml wheel, `main.py`, `.env.example`, `app.yaml`, model artifacts (including `feature_spec.yaml` and other config), `requirements.txt`, and `README_DEPLOY.txt`.
- **On the target (Windows / Linux / Mac):** Copy the folder or unzip the .zip, then: `pip install -r requirements.txt` → copy `.env.example` to `.env` and edit (ClickHouse settings) → `python main.py`. Endpoints: `http://0.0.0.0:8001/alerts`, `/validation`. See **README_DEPLOY.txt** inside the package for step-by-step instructions and platform-specific commands (unzip, copy, python).

---

### 4. End-to-end flow

1. **Train** — Run the training pipeline so that `trainer/models/` is populated.
2. **Package** — Run `python -m package.package_model_bundle [--archive]` → creates `package/bundles/<version>/`.
3. **Deploy** — Either (A) copy the bundle to a host and run scorer + validator + `trainer.api_server` there, or (B) use **package/deploy/**: copy bundle to `package/deploy/models/`, configure `.env`, run `python package/deploy/main.py` (scorer + validator + API in one process).
4. **Dashboard** polls `http://localhost:8001/alerts` and `http://localhost:8001/validation`.

---

## Files in this folder

| File | Purpose |
|------|---------|
| **README.md** | This file — how to use packaging, API, and deploy. |
| **PLAN.md** | Full plan: bundle contents, API behavior, and implementation notes. |
| **package_model_bundle.py** | Script that builds the deployable bundle from training output. |
| **build_deploy_package.py** | Script that builds a single deploy folder (or .zip): wheel + main.py + models + requirements. Copy to target and run per README_DEPLOY.txt. |
| **bundles/** | Default output directory for packaged bundles (created when you run the script). |
| **deploy_dist/** | Output of build_deploy_package: single folder to move to target (or use deploy_dist.zip). |
| **deploy/** | Deploy package source: scorer + validator + Flask (GET /alerts, /validation). See deploy/README.md. |

---

## Troubleshooting

- **"No model artifact found"** — Run the trainer first so that `trainer/models/` contains at least one of `model.pkl`, `rated_model.pkl`, `walkaway_model.pkl` and `feature_list.json`.
- **Empty `/alerts` or `/validation`** — The server reads from `trainer/local_state/state.db`. Run the scorer (and validator) to populate alerts and validation results.
- **Port in use** — Set another port with `ML_API_PORT=8002 python -m trainer.api_server`.

---

# Package：模型包與 ML API（中文）

本目錄提供：

1. **模型包打包** — 訓練完成後，僅將推論所需的產物打包成版本化 bundle（可選 `.tar.gz`）供部署使用。
2. **ML API 服務** — 在 `http://localhost:8001` 提供 GET `/alerts` 與 GET `/validation` 給儀表板使用（見 `doc/ML_API_PROTOCOL.md`）。

完整設計與範圍見本目錄 **PLAN.md**。

---

## 使用方式

### 1. 打包訓練好的模型（訓練後執行）

請在 **專案根目錄** 執行：

```bash
# 使用預設：來源 = trainer/models，輸出 = package/bundles
python -m package.package_model_bundle

# 可選產生壓縮檔（便於傳輸）
python -m package.package_model_bundle --archive

# 自訂路徑與版本
python -m package.package_model_bundle --source-dir trainer/models --output-dir package/bundles --version 1.0.0 --archive
```

| 選項 | 預設 | 說明 |
|--------|---------|-------------|
| `--source-dir` | `trainer/models` | 訓練寫入產物的目錄（`model.pkl`、`feature_list.json` 等）。 |
| `--output-dir` | `package/bundles` | 版本化 bundle 目錄的輸出位置。 |
| `--version` | 從 `model_version` 檔或時間戳 | Bundle 目錄與壓縮檔的版本標籤。 |
| `--archive` | 關閉 | 若設定，會在輸出目錄額外產生 `model_bundle_<version>.tar.gz`。 |

**結果：** 會產生 `package/bundles/<version>/`（及可選的 `model_bundle_<version>.tar.gz`），內含 scorer 所需檔案。部署時將此目錄複製到目標機，並以 `--model-dir` 指向該目錄（例如 `--model-dir package/bundles/1.0.0`）。

---

### 2. 啟動 ML API 服務

請在 **專案根目錄** 執行（確保 `trainer` 與 `package` 在路徑上）：

```bash
python -m trainer.api_server
```

- **Base URL：** `http://localhost:8001`
- **改 port：** `ML_API_PORT=8002 python -m trainer.api_server`

服務會從 `trainer/local_state/state.db` 讀取 alerts 與 validation 結果。請先執行 **scorer**（及可選的 **validator**）以寫入資料。

**協定端點：**

| 方法 | 路徑 | 用途 |
|--------|------|--------|
| GET | `/alerts` | 離桌告警（query：`ts`、`limit`；無參數 = 最近 24 小時） |
| GET | `/validation` | 驗證結果（query：`ts`、`bet_id`、`bet_ids`；無參數 = 最近 24 小時） |

範例：

```bash
# 最近 24 小時的 alerts（預設）
curl "http://localhost:8001/alerts"

# 指定時間之後的 alerts
curl "http://localhost:8001/alerts?ts=2026-03-11T00:00:00%2B08:00"

# 指定 bet ID 的 validation
curl "http://localhost:8001/validation?bet_ids=123,456"
```

完整請求/回應格式見 **`doc/ML_API_PROTOCOL.md`**。

---

### 3. 部署包（package/deploy/）

**自包含部署** 在單一 process 內執行 scorer + validator + Flask，從 ClickHouse 讀取資料並提供 GET `/alerts`、GET `/validation`。應用套件名稱為 **walkaway_ml**（與 `trainer/` 同一套程式，以該名稱安裝供部署使用）。

- **設定：** 將 `package/deploy/.env.example` 複製為 `package/deploy/.env`，填寫 ClickHouse 等設定（`CH_HOST`、`CH_USER`、`CH_PASS`、`SOURCE_DB` 等）。將 model bundle 放入 `package/deploy/models/`（例如從 `package/bundles/<version>/` 複製）。
- **安裝：** 在專案根目錄執行 `pip install -r package/deploy/requirements.txt`（會安裝 walkaway_ml 與依賴）。
- **執行：** 在專案根目錄執行 `python package/deploy/main.py`。預設監聽 port 8001（可透過 `PORT` 或 `ML_API_PORT` 覆寫）。
- **打包：** 製作 `package/deploy/` 的 tar/zip 時請依 **`.deployignore`** 排除（例如 `.venv/`、`__pycache__/`、`.env` 等）。完整步驟見 `package/deploy/README.md`。

**更換模型：** 用新 bundle（由 `package_model_bundle.py` 產出）替換 `package/deploy/models/` 內容，然後重啟 `main.py` 即可，無需改程式。

---

### 4. 端到端流程

1. **訓練** — 執行訓練流程，產出 `trainer/models/`。
2. **打包** — 執行 `python -m package.package_model_bundle [--archive]` → 產生 `package/bundles/<version>/`。
3. **部署** — (A) 將 bundle 複製到主機並在該機執行 scorer + validator + `trainer.api_server`；或 (B) 使用 **package/deploy/**：將 bundle 複製到 `package/deploy/models/`、設定 `.env`、執行 `python package/deploy/main.py`（scorer + validator + API 同一 process）。
4. **儀表板** 輪詢 `http://localhost:8001/alerts` 與 `http://localhost:8001/validation`。

---

## 本目錄檔案

| 檔案 | 用途 |
|------|---------|
| **README.md** | 本說明 — 打包、API 與部署使用方式。 |
| **PLAN.md** | 完整計畫：bundle 內容、API 行為與實作說明。 |
| **package_model_bundle.py** | 從訓練產出建置可部署 bundle 的腳本。 |
| **bundles/** | 打包產出的預設輸出目錄（執行腳本時會建立）。 |
| **deploy/** | 部署包：scorer + validator + Flask（GET /alerts、/validation）。詳見 deploy/README.md。 |

---

## 常見問題

- **「No model artifact found」** — 請先執行訓練，讓 `trainer/models/` 內至少存在 `model.pkl`、`rated_model.pkl`、`walkaway_model.pkl` 之一以及 `feature_list.json`。
- **`/alerts` 或 `/validation` 回傳空** — 服務從 `trainer/local_state/state.db` 讀取。請先執行 scorer（及 validator）寫入 alerts 與 validation 結果。
- **Port 被佔用** — 可改用其他 port：`ML_API_PORT=8002 python -m trainer.api_server`。
