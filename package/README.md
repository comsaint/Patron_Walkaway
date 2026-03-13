# Package: Model bundle & ML API

This folder provides:

1. **Deploy package** — One folder (or one `.zip`) you copy to the target machine and run there. It includes the app, model, and `requirements.txt`; no repo is needed on the target.
2. **ML API server** — GET `/alerts` and GET `/validation` at `http://localhost:8001` for the dashboard (see `doc/ML_API_PROTOCOL.md`).

Full design: **PLAN.md** in this folder.

---

## How to use

### 1. Build the deploy package (ship to target)

Deployment is **always** a single folder or archive that you copy to the target and run there. Build it from the **repository root**:

```bash
# Default: model from trainer/models, output = package/deploy_dist/
python -m package.build_deploy_package

# Optional: single file for transfer (e.g. deploy_dist.zip)
python -m package.build_deploy_package --archive

# Use a specific model directory (e.g. trainer/models_90d_weak)
python -m package.build_deploy_package --model-source trainer/models_90d_weak --archive
```

| Option | Default | Description |
|--------|---------|-------------|
| `--model-source` | `trainer/models` | Directory with model artifacts (`model.pkl` or `walkaway_model.pkl`, `feature_list.json`, etc.). |
| `--output-dir` | `package/deploy_dist` | Output folder. |
| `--archive` | Off | Also create `deploy_dist.zip` in the parent of output-dir for a single-file transfer. |

**Result:** A folder `package/deploy_dist/` (and optionally `package/deploy_dist.zip`) containing everything needed on the target: `main.py`, `requirements.txt`, `.env.example`, `wheels/`, `models/`, `README_DEPLOY.txt`, etc.

**On the target machine:** Copy the folder (or unzip the .zip), then:

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and set ClickHouse credentials.
3. `python main.py`

Endpoints: `http://0.0.0.0:8001/alerts`, `/validation`. See **README_DEPLOY.txt** inside the package for step-by-step and platform-specific commands.

---

### 2. Optional: versioned model bundle

If you want a versioned model directory (e.g. for multiple variants) before building the deploy package, run:

```bash
python -m package.package_model_bundle --source-dir trainer/models_90d_weak --output-dir package/bundles --archive
```

This produces `package/bundles/<version>/` with only model files (no app, no requirements). **Do not ship this alone.** Use it as the model source for the deploy package:

```bash
python -m package.build_deploy_package --model-source package/bundles/<version> --archive
```

Then ship the resulting `deploy_dist/` or `deploy_dist.zip` to the target as in section 1.

---

### 3. Run the ML API server (local dev)

From the **repository root** (for local testing with dashboard):

```bash
python -m trainer.api_server
```

- **Base URL:** `http://localhost:8001`
- **Port override:** `ML_API_PORT=8002 python -m trainer.api_server`

The server reads from `trainer/local_state/state.db`. Run the **scorer** (and optionally **validator**) to populate data.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/alerts` | Walkaway alerts (query: `ts`, `limit`; no params = last 24h) |
| GET | `/validation` | Validation results (query: `ts`, `bet_id`, `bet_ids`) |

Example: `curl "http://localhost:8001/alerts"`. Full format: **`doc/ML_API_PROTOCOL.md`**.

---

### 4. End-to-end flow

1. **Train** — Run the training pipeline so that `trainer/models/` (or your chosen dir) has model artifacts.
2. **Build deploy package** — `python -m package.build_deploy_package [--model-source ...] [--archive]` → `package/deploy_dist/` (and optionally `.zip`).
3. **Ship** — Copy the folder or the `.zip` to the target machine.
4. **On target** — Unzip if needed, `pip install -r requirements.txt`, configure `.env`, `python main.py`.
5. **Dashboard** polls `http://<target>:8001/alerts` and `/validation`.

---

## Files in this folder

| File | Purpose |
|------|---------|
| **README.md** | This file — deploy package and API usage. |
| **PLAN.md** | Full plan: bundle contents, API behavior, implementation notes. |
| **build_deploy_package.py** | Build the shippable deploy folder (or .zip): app + model + requirements. Use this to deploy to the target. |
| **package_model_bundle.py** | Optional: build a versioned model-only bundle; use its output as `--model-source` for build_deploy_package. |
| **deploy_dist/** | Output of build_deploy_package — copy this (or its .zip) to the target. |
| **bundles/** | Optional output of package_model_bundle (model files only; not shippable by itself). |
| **deploy/** | Source for main.py and config used by build_deploy_package. See deploy/README.md. |

---

## Troubleshooting

- **"No model artifact found"** — Run the trainer first so that `trainer/models/` (or your `--model-source`) contains at least one of `model.pkl`, `rated_model.pkl`, `walkaway_model.pkl` and `feature_list.json`.
- **Empty `/alerts` or `/validation`** — The server reads from `trainer/local_state/state.db`. Run the scorer (and validator) to populate data.
- **Port in use** — Set another port with `ML_API_PORT=8002 python -m trainer.api_server`.

---

# Package：模型包與 ML API（中文）

本目錄提供：

1. **部署包** — 單一資料夾（或單一 `.zip`），複製到目標機即可在該機執行。內含應用、模型與 `requirements.txt`，目標機不需 repo。
2. **ML API 服務** — 在 `http://localhost:8001` 提供 GET `/alerts` 與 GET `/validation` 給儀表板使用（見 `doc/ML_API_PROTOCOL.md`）。

完整設計見本目錄 **PLAN.md**。

---

## 使用方式

### 1. 建置部署包（可搬至目標機）

部署**一律**為單一資料夾或壓縮檔：複製到目標機後在該機執行。請在 **專案根目錄** 執行：

```bash
# 預設：模型來自 trainer/models，輸出 = package/deploy_dist/
python -m package.build_deploy_package

# 可選：產生單一壓縮檔（例如 deploy_dist.zip）
python -m package.build_deploy_package --archive

# 指定模型目錄（例如 trainer/models_90d_weak）
python -m package.build_deploy_package --model-source trainer/models_90d_weak --archive
```

| 選項 | 預設 | 說明 |
|--------|---------|-------------|
| `--model-source` | `trainer/models` | 模型產物目錄（`model.pkl` 或 `walkaway_model.pkl`、`feature_list.json` 等）。 |
| `--output-dir` | `package/deploy_dist` | 輸出資料夾。 |
| `--archive` | 關閉 | 另在輸出目錄上一層產生 `deploy_dist.zip`，便於單檔傳輸。 |

**結果：** 產生 `package/deploy_dist/`（及可選的 `package/deploy_dist.zip`），內含目標機所需一切：`main.py`、`requirements.txt`、`.env.example`、`wheels/`、`models/`、`README_DEPLOY.txt` 等。

**在目標機上：** 複製該資料夾（或解壓 .zip）後：

1. `pip install -r requirements.txt`
2. 將 `.env.example` 複製為 `.env` 並填寫 ClickHouse 等設定。
3. `python main.py`

端點：`http://0.0.0.0:8001/alerts`、`/validation`。詳見包內 **README_DEPLOY.txt** 的步驟與各平台指令。

---

### 2. 可選：版本化模型 bundle

若要先建出版本化模型目錄再建部署包，可執行：

```bash
python -m package.package_model_bundle --source-dir trainer/models_90d_weak --output-dir package/bundles --archive
```

會產生僅含模型檔的 `package/bundles/<version>/`。**請勿單獨搬此目錄部署。** 可將其作為部署包的模型來源：

```bash
python -m package.build_deploy_package --model-source package/bundles/<version> --archive
```

再將產生的 `deploy_dist/` 或 `deploy_dist.zip` 依第 1 節方式搬至目標機。

---

### 3. 啟動 ML API 服務（本機開發）

在 **專案根目錄** 執行（供本機與儀表板測試）：

```bash
python -m trainer.api_server
```

- **Base URL：** `http://localhost:8001`
- **改 port：** `ML_API_PORT=8002 python -m trainer.api_server`

服務從 `trainer/local_state/state.db` 讀取。請先執行 **scorer**（及可選的 **validator**）寫入資料。

| 方法 | 路徑 | 用途 |
|--------|------|---------|
| GET | `/alerts` | 離桌告警（query：`ts`、`limit`；無參數 = 最近 24 小時） |
| GET | `/validation` | 驗證結果（query：`ts`、`bet_id`、`bet_ids`） |

範例：`curl "http://localhost:8001/alerts"`。完整格式見 **`doc/ML_API_PROTOCOL.md`**。

---

### 4. 端到端流程

1. **訓練** — 執行訓練流程，產出 `trainer/models/`（或自訂目錄）之模型產物。
2. **建置部署包** — `python -m package.build_deploy_package [--model-source ...] [--archive]` → `package/deploy_dist/`（及可選 `.zip`）。
3. **搬運** — 將該資料夾或 `.zip` 複製到目標機。
4. **目標機** — 若有 .zip 先解壓，執行 `pip install -r requirements.txt`、設定 `.env`、`python main.py`。
5. **儀表板** 輪詢 `http://<目標機>:8001/alerts` 與 `/validation`。

---

## 本目錄檔案

| 檔案 | 用途 |
|------|---------|
| **README.md** | 本說明 — 部署包與 API 使用方式。 |
| **PLAN.md** | 完整計畫：bundle 內容、API 行為與實作說明。 |
| **build_deploy_package.py** | 建置可搬運的部署資料夾（或 .zip）：應用 + 模型 + requirements。部署至目標機請用此腳本。 |
| **package_model_bundle.py** | 可選：建置僅含模型的版本化 bundle；可作為 build_deploy_package 的 `--model-source`。 |
| **deploy_dist/** | build_deploy_package 的輸出 — 將此資料夾或其 .zip 複製到目標機。 |
| **bundles/** | package_model_bundle 的可選輸出（僅模型檔；不可單獨搬運部署）。 |
| **deploy/** | build_deploy_package 使用的 main.py 與設定來源。詳見 deploy/README.md。 |

---

## 常見問題

- **「No model artifact found」** — 請先執行訓練，讓 `trainer/models/`（或您的 `--model-source`）內至少存在 `model.pkl`、`rated_model.pkl`、`walkaway_model.pkl` 之一以及 `feature_list.json`。
- **`/alerts` 或 `/validation` 回傳空** — 服務從 `trainer/local_state/state.db` 讀取。請先執行 scorer（及 validator）寫入資料。
- **Port 被佔用** — 可改用其他 port：`ML_API_PORT=8002 python -m trainer.api_server`。
