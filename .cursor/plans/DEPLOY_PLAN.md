# Deploy Plan: Scorer + Validator + API (package/deploy/)

**Goal**: Once the app is deployed, it continuously fetches data from the ClickHouse stream, runs scoring and validation, and exposes GET `/alerts` and GET `/validation` per `doc/ML_API_PROTOCOL.md`. No Docker; target machine has Python and can install packages. Credentials (e.g. ClickHouse) are supplied by the deployer, not shipped.

---

## 1. Package name: walkaway_ml

- The application package that provides **scorer** and **validator** modules is named **walkaway_ml** (not "trainer") to avoid confusion—this package does **not** perform model training; it only runs inference and validation.
- **requirements.txt** in deploy installs **walkaway_ml** (e.g. from `./wheels/walkaway_ml-0.0.0-py3-none-any.whl` or `-f wheels walkaway_ml`).
- **package/deploy/main.py** uses `import walkaway_ml.scorer`, `import walkaway_ml.validator`, etc.

**Implementation note**: The repo currently uses the directory name `trainer/`. To ship as **walkaway_ml**, either (1) rename `trainer/` to `walkaway_ml/` and update all imports project-wide, or (2) keep `trainer/` and in `pyproject.toml` set `name = "walkaway_ml"` and `package_dir = {"walkaway_ml": "trainer"}`, then update all imports from `trainer.*` to `walkaway_ml.*`.

---

## 2. Ignore file: .deployignore

- Use **`.deployignore`** (not `.gcloudignore`) for a platform-neutral deploy exclude list.
- When packaging the deploy directory (e.g. for tar/zip or upload), exclude: `.venv/`, `__pycache__/`, `.env`, `*.pyc`, etc.

---

## 3. Deploy package contents (package/deploy/)

| Item | Description |
|------|-------------|
| **main.py** | Single entry: load `.env`, set STATE_DB_PATH / MODEL_DIR, start **scorer thread**, **validator thread**, and **Flask** (GET /alerts, GET /validation). Depends on **walkaway_ml** (`walkaway_ml.scorer`, `walkaway_ml.validator`). |
| **app.yaml** | Deploy config (runtime, entrypoint, port). |
| **requirements.txt** | Flask, pandas, numpy, joblib, lightgbm, pyyaml, python-dotenv, clickhouse-driver, and **walkaway_ml** (the application package; install via wheel or `-e`). |
| **.env.example** | CH_HOST, CH_PORT, CH_USER, CH_PASS, SOURCE_DB, PORT/ML_API_PORT, STATE_DB_PATH, MODEL_DIR; note that credentials are supplied by the deployer. |
| **.deployignore** | Files/directories to exclude when packaging. |
| **README.md** | How to deploy, configure .env, swap model, and the two APIs. |
| **models/** | Model bundle (copy output of `package_model_bundle` here). |
| **local_state/** | Runtime state; `state.db` is created here. |
| **wheels/** (optional) | Hold **walkaway_ml-*.whl** for self-contained install. |

---

## 4. Three components (one process)

- **Scorer thread**: `walkaway_ml.scorer.run_scorer_loop(...)`. Fetches from ClickHouse, scores, writes to **alerts** table.
- **Validator thread**: `walkaway_ml.validator.run_validator_loop(...)`. Reads alerts, checks against ClickHouse for walkaway, writes to **validation_results** table.
- **Flask**: GET /alerts and GET /validation only; reads from the same **state.db**. Port from PORT or ML_API_PORT (default 8001).

All share the same **state.db** (STATE_DB_PATH) and **ClickHouse** config (.env).

---

## 5. Data flow

```
ClickHouse (bets, sessions, …)
    │
    ├──► Scorer loop ──► alerts table (state.db)
    │                        │
    │                        ▼
    └──► Validator loop ──► validation_results table (state.db)
                                    │
                                    ▼
Flask GET /alerts ──────────► read alerts
Flask GET /validation ──────► read validation_results
```

---

## 6. Build and ship flow

1. After training → run `package_model_bundle.py` → get `package/bundles/<version>/`.
2. Copy bundle contents into `package/deploy/models/`.
3. From repo root run **`pip wheel .`** (pyproject.toml has `name = "walkaway_ml"`); copy **walkaway_ml-*.whl** into `package/deploy/wheels/`.
4. Package the **package/deploy/** directory (respecting .deployignore) as the deployable unit (e.g. tar/zip).
5. On target: unpack → copy `.env.example` to `.env` and fill in ClickHouse credentials etc. → **`pip install -r requirements.txt`** (installs **walkaway_ml** and dependencies) → **`python main.py`**.
6. To swap model: replace contents of `deploy/models/` and restart.

---

## 7. Files to add or change (when implementing)

| Item | Action |
|------|--------|
| **package/PLAN.md** | Optional: point to this DEPLOY_PLAN for deploy; or keep as bundle + API-only plan. |
| **pyproject.toml** (repo root) | Add: **name = "walkaway_ml"**; packages / package_dir so the app package installs as walkaway_ml. |
| **Repo imports** | Switch from `trainer` to **walkaway_ml** (scope depends on option A vs B in §1). |
| **trainer/scorer.py** | Read STATE_DB_PATH, MODEL_DIR from env; add **run_scorer_loop(...)** for programmatic use. |
| **trainer/validator.py** | Read STATE_DB_PATH from env; add **run_validator_loop(...)** for programmatic use. |
| **package/deploy/main.py** | New: set env, start scorer thread, validator thread, Flask using **walkaway_ml.scorer** and **walkaway_ml.validator**. |
| **package/deploy/app.yaml** | New: runtime, entrypoint (e.g. `python main.py`), port. |
| **package/deploy/requirements.txt** | New: list **walkaway_ml** and dependencies. |
| **package/deploy/.env.example** | New: CH_*, PORT, paths. |
| **package/deploy/.deployignore** | New: exclude .venv, __pycache__, .env, etc. |
| **package/deploy/README.md** | New: deploy steps, .env, model swap, two APIs. |
| **package/deploy/models/.gitkeep** | New: keep empty dir; copy bundle here. |
| **package/deploy/local_state/.gitkeep** | New: keep empty dir. |
| **package/README.md** | Update: describe deploy package under package/deploy/, **walkaway_ml**, .deployignore, and model-swap steps. |

---

## 8. Player profile 與 canonical mapping（部署資料目錄）

### 8.1 打包時

- **player_profile.parquet**：來源路徑為專案內與 trainer/etl 一致的 `PROJECT_ROOT / "data"`，即 **repo 根目錄下的 `data/player_profile.parquet`**（與 `trainer/etl_player_profile.py`、`trainer/trainer.py` 的 `LOCAL_PARQUET_DIR` 一致）。
- 建包時若該檔案存在，則複製到 deploy 套件的 **`output_dir / "data" / "player_profile.parquet"`**；若不存在則不複製，但在建包**結束時**（所有步驟與 archive 完成後）於 console 印出一行**錯誤級**訊息，提醒未帶出 profile、scorer 將以 NaN 跑。
- 不預先打包 canonical mapping；部署端一律由 scorer 從當輪 sessions 建出並寫入部署資料目錄，重啟後自該目錄讀取。

### 8.2 目標機（deploy 執行時）

- **DATA_DIR**：由 **package/deploy/main.py** 在 import walkaway_ml 前設定為 `DEPLOY_ROOT / "data"`，並寫入 `os.environ["DATA_DIR"]`；同時確保該目錄存在（mkdir）。
- **player_profile**：scorer 優先從 `DATA_DIR / "player_profile.parquet"` 讀取（即打包時若存在會放進的同一路徑）；若不存在則維持現有行為：僅打 warning、profile 特徵為 NaN。
- **canonical mapping**：
  - 不從套件內讀取預建 mapping；永遠走「由當輪 sessions 建出」分支。
  - 建出後**持久化**到 `DATA_DIR / "canonical_mapping.parquet"` 與 `DATA_DIR / "canonical_mapping.cutoff.json"`。
  - 下次啟動時若該二檔存在且 cutoff 仍有效（例如 cutoff >= now），則從磁碟載入，避免重啟後重新計算；否則再從 sessions 重建並覆寫。

### 8.3 小結

- 打包：有則帶出 `data/player_profile.parquet`，無則結尾錯誤提示；不帶 canonical mapping。
- 執行：profile 讀 DATA_DIR；canonical 先試讀 DATA_DIR 持久檔，無則建自 sessions 並寫回 DATA_DIR。
