# ML API deploy package — GET /alerts, GET /validation

Target machine can be Windows, Linux, or Mac. Steps below cover all.

## 1. Get the folder on the target

- Copy this folder to the target, or
- If you have the .zip: unzip it.
  - Windows (PowerShell): `Expand-Archive -Path deploy_dist.zip -DestinationPath .`
  - Windows (GUI): Right-click the .zip → Extract All
  - Linux / Mac: `unzip deploy_dist.zip`

Then open a terminal in the extracted folder (e.g. `deploy_dist`).

## 2. Install Python dependencies (no repo needed)

You need **Python 3.9+** on the target. Work in the deploy folder (where `main.py`, `requirements.txt`, and `wheels/` are).

### 2.1 Brand-new virtual environment (no venv yet)

Create an isolated environment next to the app, activate it, then install from the bundle.

**1. Create the venv** (from the deploy folder):

- Windows (try in order if one fails): `py -3 -m venv .venv` or `python -m venv .venv`
- Linux / Mac: `python3 -m venv .venv`

**2. Activate it** (required before `pip` / `python` use the venv):

- Windows **cmd**: `.venv\Scripts\activate.bat`
- Windows **PowerShell**: `.\.venv\Scripts\Activate.ps1`  
  If execution policy blocks scripts, either run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that shell first, or use **cmd** and `activate.bat` above.
- Linux / Mac: `source .venv/bin/activate`

Your prompt should show `(.venv)` (or similar).

**3. (Optional) Upgrade the installer:**

```bash
python -m pip install --upgrade pip
```

**4. Install the bundle** (local wheel under `wheels/` plus PyPI lines in `requirements.txt`):

```bash
pip install -r requirements.txt
```

If `pip` is not on PATH after activation, use `python -m pip install -r requirements.txt` instead.

From here on, always **activate `.venv`** in a new terminal before `python main.py` (step 4).

### 2.2 You already have a virtual environment

Use this when the machine already has a venv you want to reuse (same or another path), or you completed §2.1 earlier and only received an **updated** deploy folder (new wheel / `requirements.txt`).

1. **Activate** your existing venv (same style as §2.1 step 2; adjust the path if the venv is not `.venv`).
2. **`cd`** into this deploy folder (where the new `requirements.txt` and `wheels/` live).

**Refresh all packages from the new manifest** (recommended after any deploy bundle update — updates the `walkaway_ml` wheel and any changed PyPI pins):

```bash
pip install -r requirements.txt
```

**Install or upgrade only the application wheel** (when PyPI dependencies are unchanged and you only replaced `wheels/` and the first line of `requirements.txt`):

```bash
pip install --upgrade --no-deps wheels/<exact-wheel-filename>.whl
```

Use the exact `.whl` name from the **first line** of `requirements.txt` (the `wheels/...` entry) or from listing the `wheels/` directory. If the service fails with missing modules, run the full `pip install -r requirements.txt` once so transitive deps stay aligned.

## 3. Configure environment

Create `.env` from the example and set ClickHouse: `CH_USER`, `CH_PASS` (required); `CH_HOST`, `CH_PORT`, `SOURCE_DB`, etc. as needed.

See `.env.example` in this folder for optional tuning: `DEPLOY_LOG_LEVEL` / `LOGLEVEL`, `SCORER_LOOKBACK_HOURS`, `SCORER_COLD_START_WINDOW_HOURS`, `PREDICTION_LOG_DB_PATH`, `SCORER_ENABLE_SHAP_REASON_CODES`, and more.

Optional: `PORT` or `ML_API_PORT` (default 8001).

- Windows (cmd): `copy .env.example .env`
- Windows (PowerShell): `Copy-Item .env.example .env`
- Linux / Mac: `cp .env.example .env`

Then edit `.env` in any text editor (Notepad, VS Code, nano, vim, etc.).

**Serving data** (next to `main.py`, under `data/`):

- `player_profile.parquet` — required for models that use profile features (should be present if the package was built with it, or copy from your training machine into `data/`).
- Optional: `canonical_mapping.parquet` + `canonical_mapping.cutoff.json` — faster cold start; scorer can rebuild from ClickHouse sessions if absent.
- `gmwds_t_bet` / session / game Parquet exports are **not** shipped in the deploy bundle; live bet/session/game come from ClickHouse at runtime.

## 4. Start the service

All platforms:

```bash
python main.py
```

(Use `py main.py` on Windows or `python3 main.py` on Linux/Mac if needed.)

Scorer, validator, and Flask API run in one process.

The process listens on **all network interfaces** at port **8001** (Flask `host="0.0.0.0"`). That bind address means “accept connections on every local IP,” not “type this in the browser.”

**Open in a browser (same machine as the service):**

- `http://127.0.0.1:8001/alerts` and `http://127.0.0.1:8001/validation`  
  or `http://localhost:8001/alerts` and `http://localhost:8001/validation`

Using `http://0.0.0.0:8001/...` in the address bar often fails (e.g. `ERR_ADDRESS_INVALID`) because `0.0.0.0` is not a normal destination host for clients.

**From another machine:** use this host’s real hostname or IP, e.g. `http://203.0.113.10:8001/alerts`, and ensure firewalls allow the port.

Query parameters and default time windows are documented in `ML_API_PROTOCOL.md` (included in this folder).

### Feature parity audit (optional)

To debug **train vs serve** feature drift, the scorer can append audit tables into the same SQLite file as the prediction log (`PREDICTION_LOG_DB_PATH`, default `local_state/prediction_log.db`). **Default is off** (no extra I/O).

Set in `.env` (then restart `python main.py`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `SCORER_FEATURE_AUDIT_ENABLE` | `0` | Set to `1` / `true` to enable per-cycle audit when a prediction log DB path is configured. |
| `SCORER_FEATURE_AUDIT_SAMPLE_ROWS` | `1000` | Max rows per cycle for row-level + long-format feature samples (deterministic by `bet_id`). |
| `SCORER_FEATURE_AUDIT_EVERY_N_CYCLES` | `1` | Emit audit only every N scorer cycles. |
| `SCORER_FEATURE_AUDIT_RETENTION_HOURS` | `24` | Delete older `feature_audit_*` rows from the prediction log DB. |
| `SCORER_FEATURE_AUDIT_STORE_VALUES` | `1` | If `0`, only per-feature **summary** statistics are stored (no long table of raw feature values). |

Tables: `feature_audit_runs`, `feature_audit_feature_summary`, `feature_audit_row_sample`, `feature_audit_feature_sample_long`.

**Training side (on a machine with the repo):** build the same summary schema from a training chunk Parquet, then compare:

```bash
python -m trainer.scripts.export_training_feature_audit \
  --parquet path/to/chunk_....parquet \
  --feature-list-json path/to/models/feature_list.json \
  --out-db /tmp/training_audit.sqlite \
  [--feature-spec-yaml path/to/models/feature_spec.yaml]

python -m trainer.scripts.compare_feature_audit_summaries \
  --serving-db path/to/deploy/local_state/prediction_log.db \
  --training-db /tmp/training_audit.sqlite \
  --out-csv drift.csv
```

Copy `prediction_log.db` from production for offline comparison if needed. Audits may contain **player/bet identifiers**; keep retention short and handle exports under your data policy.

## 5. To swap the model only (same code / same requirements as before)

Replace the files in the `models/` folder with the new bundle, then restart (step 4).

No pip — the model is files on disk, not a Python package.

## 6. To update after a new deploy package (new wheel and/or new dependencies)

Keep the same venv from **§2**. Copy or merge the new folder over the old one so `wheels/` and `requirements.txt` match the new build, **`cd`** into that folder, activate the venv, then use **§2.2** — either `pip install -r requirements.txt` (full refresh) or `pip install --upgrade --no-deps wheels/<…>.whl` if only the wheel changed.

More detail: see `package/deploy/README.md` in the repository (section "Production bundle: updates").
