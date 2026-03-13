# Deploy: Scorer + Validator + ML API

Single process: continuously fetches from ClickHouse, runs scorer and validator, and exposes GET `/alerts` and GET `/validation` per `doc/ML_API_PROTOCOL.md`.

## Prerequisites

- Python 3.9+
- ClickHouse accessible; credentials supplied by you (not shipped).

## Setup

1. **Copy env file**  
   Copy `.env.example` to `.env` in this directory (e.g. `cp .env.example .env`) and fill in ClickHouse and optional settings:
   - `CH_HOST`, `CH_PORT`, `CH_USER`, `CH_PASS`, `SOURCE_DB` (required)
   - `PORT` or `ML_API_PORT` (default 8001)

2. **Install dependencies**  
   From **repository root** (so `walkaway_ml` installs from the repo):
   ```bash
   pip install -r package/deploy/requirements.txt
   ```
   If you use a wheel for self-contained deploy, add `-f wheels` and `walkaway_ml` (or the wheel file) to `requirements.txt` and run `pip install -r requirements.txt` from this directory.

3. **Model bundle**  
   When building the deploy package, `build_deploy_package.py` copies the model from `--model-source` (e.g. `trainer/models`) into `models/`. Required in that source: at least one of `model.pkl`, `rated_model.pkl`, `walkaway_model.pkl`, and `feature_list.json`. If you run the deploy app from this folder only (no full deploy package), put those files in `models/` yourself.

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

1. Re-run `python -m package.build_deploy_package --model-source <path-to-new-model>` (and `--archive` if needed) to produce a new deploy package with the new model in `models/`.
2. Or, if running from this folder only: replace the contents of `models/` with the new model files (or point `MODEL_DIR` in `.env` to another directory).
3. Restart `main.py`.

No code changes required; the app loads the model from `MODEL_DIR` at startup.
