# Deploy: Scorer + Validator + ML API

Single process: continuously fetches from ClickHouse, runs scorer and validator, and exposes GET `/alerts` and GET `/validation` per `doc/ML_API_PROTOCOL.md`.

## Prerequisites

- Python 3.9+
- ClickHouse accessible; credentials supplied by you (not shipped).

## Two setups: repo (development) vs deploy bundle (production)

| | **From this repository** | **Deploy bundle** (`python -m package.build_deploy_package`) |
|---|---------------------------|----------------------------------------------------------------|
| Purpose | Local dev / CI; editable install | Copy `deploy_dist/` or `.zip` to the target; **no repo** on the machine |
| `walkaway_ml` | `package/deploy/requirements.txt` uses `-e .` from **repo root** | Built **wheel** under `wheels/`; generated `requirements.txt` lists `wheels/walkaway_ml-….whl` then PyPI deps |
| Docs for the bundle | — | `README_DEPLOY.txt` and `ML_API_PROTOCOL.md` inside the built folder |

## Setup

1. **Copy env file**  
   Copy `.env.example` to `.env` in this directory (e.g. `cp .env.example .env`) and fill in at least **`CH_USER`** and **`CH_PASS`**. Optional settings are documented inline in `.env.example` (e.g. `CH_HOST`, `CH_PORT`, `SOURCE_DB`, **`DEPLOY_LOG_LEVEL`** / **`LOGLEVEL`**, **`SCORER_COLD_START_WINDOW_HOURS`**, **`SCORER_LOOKBACK_HOURS`**, paths, SHAP, prediction-log retention).  
   - `PORT` or `ML_API_PORT` (default 8001)

2. **Install dependencies** (repo development only)  
   Optional but recommended: create a virtual environment. From **repository root**:
   ```bash
   python -m venv .venv
   ```
   Activate it: Linux/macOS — `source .venv/bin/activate`; Windows `cmd` — `.venv\Scripts\activate`; Windows Git Bash — `source .venv/Scripts/activate`.

   Then install from **repository root** (so `walkaway_ml` installs from the repo):
   ```bash
   pip install -r package/deploy/requirements.txt
   ```
   If you **already have a venv**, you do not need to recreate it when `requirements.txt` changes. Run the same `pip install -r package/deploy/requirements.txt` again: pip installs anything missing and adjusts versions to match the file. If the editable install of `walkaway_ml` seems stale, refresh only that package: `pip install --upgrade -e .` from repo root, or `pip install --force-reinstall --no-deps -e .` without reinstalling every dependency.

   **Deploy bundle** (folder produced by `build_deploy_package`): on the target, use **that folder’s** `requirements.txt` (not `package/deploy/requirements.txt`). First install: `pip install -r requirements.txt` from inside the extracted folder.

3. **Model bundle**  
   When building the deploy package, `build_deploy_package.py` copies the model from `--model-source` (e.g. `trainer/models`) into `models/`. Required in that source: at least one of `model.pkl`, `rated_model.pkl`, `walkaway_model.pkl`, and `feature_list.json`. If you run the deploy app from this folder only (no full deploy package), put those files in `models/` yourself.

### Production bundle: updates without reinstalling everything

Use the **same virtualenv** as the first install; do not delete it unless you want a clean slate.

1. **Model only** (new `.pkl` / bundle, same Python code and same dependency list): replace the files under `models/` on the target (or point `MODEL_DIR` in `.env` elsewhere), then restart the app. **No `pip`** — the model is data on disk, not a pip package.

2. **New bundle** (new wheel and/or changed `requirements.txt`): unpack the new deploy folder over the old one or merge files so `wheels/` and `requirements.txt` match the new build. Then run:
   ```bash
   pip install -r requirements.txt
   ```
   from the deploy folder (venv activated). Pip **skips** dependencies that already satisfy the file; it typically only pulls **new or changed** lines (e.g. a new `walkaway_ml-….whl` filename when the wheel version changes).

3. **Refresh only the `walkaway_ml` wheel** (avoid touching other packages): install the wheel from the new bundle with:
   ```bash
   pip install --upgrade --no-deps wheels/<walkaway_ml wheel filename>
   ```
   Use the exact filename from the first line of that bundle’s `requirements.txt` or under `wheels/` (e.g. `walkaway_ml-1.2.3-py3-none-any.whl`).

If something still imports an old install, `pip uninstall walkaway_ml` once, then `pip install -r requirements.txt` again.

## Run

From **repository root** (so `walkaway_ml` is importable):

```bash
python package/deploy/main.py
```

Or from this directory after installing the project (e.g. `pip install -e ../..`):

```bash
python main.py
```

- Scorer and validator run in background threads; Flask serves on port 8001 (or `PORT` / `ML_API_PORT`).
- Endpoints: `http://localhost:8001/alerts`, `http://localhost:8001/validation`.

## Swapping the model

1. **Production**: replace files under `models/` on the target (from a new bundle or by hand), then restart. No pip if code and dependencies are unchanged — see **Production bundle: updates** above.
2. **Build a new package** (to ship elsewhere): `python -m package.build_deploy_package --model-source <path-to-new-model>` (and `--archive` if needed).
3. **Repo-only run**: replace the contents of `models/` here (or set `MODEL_DIR` in `.env`).

No code changes required; the app loads the model from `MODEL_DIR` at startup.

---

## 中文版摘要（繁體）

單一程序：自 ClickHouse 持續拉資料、執行 scorer 與 validator，並依 `doc/ML_API_PROTOCOL.md` 提供 GET `/alerts` 與 GET `/validation`。

### 前置需求

- Python 3.9+
- 可連線的 ClickHouse；憑證由您自行設定（不隨套件提供）。

### 兩種情境：本機 repo（開發）與部署包（生產）

| | **本倉庫開發** | **部署包**（`python -m package.build_deploy_package`） |
|---|----------------|--------------------------------------------------------|
| 用途 | 本機開發／CI；可編輯安裝 | 將 `deploy_dist/` 或 `.zip` 複製到目標機；**不需**完整 repo |
| `walkaway_ml` | 在 **repo 根目錄** 用 `package/deploy/requirements.txt` 的 `-e .` | 建好的 **wheel** 在 `wheels/`；產生的 `requirements.txt` 先列 `wheels/walkaway_ml-….whl`，再列 PyPI 依賴 |
| 部署包說明 | — | 建置後資料夾內的 `README_DEPLOY.txt` 與 `ML_API_PROTOCOL.md` |

### 設定步驟

1. **環境檔**  
   將本目錄的 `.env.example` 複製為 `.env`，至少填寫 **`CH_USER`**、**`CH_PASS`**。其餘選項見 `.env.example` 內註解（如 `CH_HOST`、`SOURCE_DB`、**`DEPLOY_LOG_LEVEL`**／**`LOGLEVEL`**、**`SCORER_COLD_START_WINDOW_HOURS`**、**`SCORER_LOOKBACK_HOURS`**、路徑、SHAP、prediction log 保留等）。`PORT` 或 `ML_API_PORT`（預設 8001）。

2. **安裝依賴**（僅 **repo 開發**）  
   建議建立 venv。在 **repository root**：
   ```bash
   python -m venv .venv
   ```
   啟用：Linux/macOS — `source .venv/bin/activate`；Windows `cmd` — `.venv\Scripts\activate`；Windows Git Bash — `source .venv/Scripts/activate`。  
   接著在 **repository root** 執行：
   ```bash
   pip install -r package/deploy/requirements.txt
   ```
   若 **已有 venv**，`requirements.txt` 變更時不必重建，再執行同一行即可。若可編輯安裝的 `walkaway_ml` 看似過舊，可在 repo 根目錄只更新專案：`pip install --upgrade -e .`，或 `pip install --force-reinstall --no-deps -e .`（不必重裝所有依賴）。  

   **部署包**：在目標機請用**該部署資料夾內**的 `requirements.txt`（不是 `package/deploy/requirements.txt`）。首次安裝：在解壓後的資料夾內執行 `pip install -r requirements.txt`。

3. **模型檔**  
   執行 `build_deploy_package.py` 時會從 `--model-source`（例如 `trainer/models`）複製到 `models/`。來源至少需有 `model.pkl`、`rated_model.pkl` 或 `walkaway_model.pkl` 之一，以及 `feature_list.json`。若僅在本目錄跑、未用完整部署包，請自行將檔案放到 `models/`。

### 生產部署包：更新時不必重裝全部套件

請沿用**第一次安裝時同一個 venv**，除非您刻意要乾淨重建。

1. **只換模型**（新 `.pkl` 等，程式與依賴不變）：在目標機覆寫 `models/` 下檔案（或將 `.env` 的 `MODEL_DIR` 指到新路徑），重啟服務。**不必執行 pip**（模型是磁碟上的資料，不是 pip 套件）。

2. **新整包**（新 wheel 或 `requirements.txt` 有變）：將新部署目錄覆蓋或合併到舊目錄，使 `wheels/` 與 `requirements.txt` 與新建置一致。在部署資料夾內（已啟用 venv）執行：
   ```bash
   pip install -r requirements.txt
   ```
   pip 會跳過已滿足的依賴，通常只處理**新增或變更**的項目（例如 wheel 檔名因版本而變）。

3. **只更新 `walkaway_ml` wheel**（不動其他套件）：
   ```bash
   pip install --upgrade --no-deps wheels/<wheel 檔名>
   ```
   檔名請以該包 `requirements.txt` 第一行或 `wheels/` 內實際檔名為準。

若仍載入舊版，可先 `pip uninstall walkaway_ml`，再 `pip install -r requirements.txt`。

### 執行

在 **repository root**：
```bash
python package/deploy/main.py
```

或在本目錄且已安裝專案（例如 `pip install -e ../..`）：
```bash
python main.py
```

- Scorer 與 validator 在背景執行緒；Flask 監聽 8001（或 `PORT`／`ML_API_PORT`）。
- 端點：`http://localhost:8001/alerts`、`http://localhost:8001/validation`。

### 更換模型

1. **生產環境**：在目標機覆寫 `models/`（來自新包或手動），重啟；若程式與依賴未變則不必 pip（見上文「生產部署包」）。
2. **打包給他處**：`python -m package.build_deploy_package --model-source <新路徑>`（必要時加 `--archive`）。
3. **僅本倉庫執行**：在本目錄替換 `models/` 內容（或設定 `.env` 的 `MODEL_DIR`）。

無需改程式；啟動時自 `MODEL_DIR` 載入模型。
