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

### 3. End-to-end flow

1. **Train** — Run the training pipeline so that `trainer/models/` is populated.
2. **Package** — Run `python -m package.package_model_bundle [--archive]` → creates `package/bundles/<version>/`.
3. **Deploy** — Copy the bundle to the deployment host; run the scorer with `--model-dir` pointing at that bundle; scorer writes to `state.db`.
4. **Serve API** — Run `python -m trainer.api_server` on the same machine; dashboard polls `http://localhost:8001/alerts` and `http://localhost:8001/validation`.

---

## Files in this folder

| File | Purpose |
|------|---------|
| **README.md** | This file — how to use packaging and the API. |
| **PLAN.md** | Full plan: bundle contents, API behavior, and implementation notes. |
| **package_model_bundle.py** | Script that builds the deployable bundle from training output. |
| **bundles/** | Default output directory for packaged bundles (created when you run the script). |

---

## Troubleshooting

- **"No model artifact found"** — Run the trainer first so that `trainer/models/` contains at least one of `model.pkl`, `rated_model.pkl`, `walkaway_model.pkl` and `feature_list.json`.
- **Empty `/alerts` or `/validation`** — The server reads from `trainer/local_state/state.db`. Run the scorer (and validator) to populate alerts and validation results.
- **Port in use** — Set another port with `ML_API_PORT=8002 python -m trainer.api_server`.
