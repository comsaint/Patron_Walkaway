.# Plan: Model Bundle Packaging & ML API Server (port 8001)

**Goal**: After training, produce a deployable model bundle and expose GET `/alerts` and GET `/validation` at `http://localhost:8001` per `doc/ML_API_PROTOCOL.md`.

---

## 1. Current state

| Item | Current |
|------|--------|
| Training output dir | `trainer/models/` (`config.MODEL_DIR`) |
| Files written by trainer | `model.pkl`, `feature_list.json`, `feature_spec.yaml`, `model_version`, `reason_code_map.json`, `training_metrics.json`, `walkaway_model.pkl` |
| Scorer reads | `scorer.load_dual_artifacts(model_dir)` from same dir; fallback order: model.pkl → rated_model.pkl → walkaway_model.pkl |
| API server | `trainer/api_server.py`: port **8000**, paths **/get_alerts**, **/get_validation**; reads `trainer/local_state/state.db` |
| Protocol | Port **8001**, paths **/alerts**, **/validation**; query/response format in `doc/ML_API_PROTOCOL.md` |

---

## 2. Scope

1. **Packaging script** (under `package/`): From training output dir, select inference-required files and produce a deployable model bundle (directory + optional archive).
2. **API aligned to protocol**: Same server listens on port 8001 and exposes GET `/alerts` and GET `/validation` with behavior and response shape per `doc/ML_API_PROTOCOL.md` (24h default, `limit`, protocol fields only, `casino_player_id` / `is_known_player`).
3. **Run instructions**: Default start at `http://localhost:8001`.

---

## 3. Phase A: Model bundle packaging

### 3.1 Bundle contents (aligned with scorer / backtester)

| File | Required | Notes |
|------|----------|--------|
| `model.pkl` | Yes (or one fallback below) | v10 primary artifact |
| `rated_model.pkl` | Optional fallback | Legacy |
| `walkaway_model.pkl` | Optional fallback | Legacy |
| `feature_list.json` | Yes | Feature order and names |
| `feature_spec.yaml` | Recommended | Frozen spec for train–serve parity |
| `model_version` | Recommended | Version string |
| `reason_code_map.json` | Recommended | SHAP reason codes |
| `training_metrics.json` | Optional | For ops/audit |

### 3.2 Output format

- **Directory**: e.g. `package/bundles/<version>/` or `dist/model_bundle_<version>/` with the above files.
- **Optional archive**: `model_bundle_<version>.tar.gz` (or `.zip`) for transfer/versioning.
- **MANIFEST** (optional): `MANIFEST.txt` or `bundle_info.json` in the bundle dir listing files and `model_version`.

### 3.3 Packaging script

- **Location**: `package/package_model_bundle.py`.
- **Arguments**:
  - `--source-dir`: Default `trainer/models` (relative to repo root or absolute).
  - `--output-dir`: Default `package/bundles` or `dist`.
  - `--version`: Optional; if omitted, read from `source-dir/model_version` or use timestamp.
  - `--archive`: Optional flag to also produce `.tar.gz`.
- **Logic**:
  1. Ensure at least one of `model.pkl`, `rated_model.pkl`, `walkaway_model.pkl` exists and `feature_list.json` exists.
  2. Create `output-dir/<version>/` (or `output-dir/model_bundle_<version>/`).
  3. Copy the files above when present (skip missing optional files).
  4. Optionally write MANIFEST / bundle_info.
  5. If `--archive`, create `model_bundle_<version>.tar.gz`.
- **Errors**: Exit non-zero with clear message if required files are missing.

### 3.4 Usage

After training, run from repo root:

```bash
python -m package.package_model_bundle --source-dir trainer/models --output-dir package/bundles --archive
```

Deploy: unpack or point scorer `--model-dir` at the bundle directory.

---

## 4. Phase B: API server aligned to ML_API_PROTOCOL.md

### 4.1 Port and paths

- Default **port 8001** (override via env e.g. `ML_API_PORT`).
- Expose **GET `/alerts`** and **GET `/validation`**.
- Existing **/get_alerts** and **/get_validation** retained for backward compatibility (same logic).

### 4.2 GET `/alerts` behavior and response

- **Query**: `ts` (optional) = only alerts with `ts > ts`; `limit` (optional) = max count when `ts` is absent; **no params** = last **24 hours**.
- **Response**: Only protocol fields: `bet_id`, `ts`, `bet_ts`, `player_id`, `casino_player_id`, `table_id`, `position_idx`, `session_id`, `visit_avg_bet`, `is_known_player`. `casino_player_id` = `null`; `is_known_player` from DB `is_rated_obs` (1/0). NaN/Inf → `null`; timestamps HK.

### 4.3 GET `/validation` behavior and response

- **Query**: `ts` = results validated after `ts`; `bet_id` / `bet_ids`; **no params** = last **24 hours**.
- **Response**: `ts`, `player_id`, `casino_player_id`, `bet_id`, `walkaway_ts`, `TP`, `sync_ts`, `reason`, `bet_ts`. `casino_player_id` = `null`; `TP` as string (e.g. 1→"TP", 0→"FP"/"MISS"). NaN/Inf → `null`; timestamps HK.

### 4.4 Implementation notes

- In `trainer/api_server.py`: when no `ts` is given, filter alerts by `ts > now_hk - 24h` and validation by `validated_at > now_hk - 24h`. Apply `limit` on alerts when `ts` is absent. Before returning, select only protocol fields and add `casino_player_id`, `is_known_player` (and stringify `TP` for validation).
- CORS `*`, JSON, HK timezone: keep existing behavior.

### 4.5 Run

Default:

```bash
python -m trainer.api_server
```

Listens on `0.0.0.0:8001` (or from env). Document for dashboard: base URL `http://localhost:8001`, GET `/alerts`, GET `/validation`.

---

## 5. End-to-end flow

1. **Train**: Run existing pipeline → `trainer/models/`.
2. **Package**: Run `package/package_model_bundle.py` → `package/bundles/<version>/` (and optional `.tar.gz`).
3. **Deploy**: Copy bundle to target; point scorer at bundle dir; scorer writes to `local_state/state.db`; validator writes validation_results.
4. **Start API**: Run API server on port 8001; it reads same `state.db`; expose GET `/alerts`, GET `/validation`.
5. **Dashboard**: Poll `http://localhost:8001/alerts` and `http://localhost:8001/validation` per protocol.

---

## 6. Files / changes

| Item | Type | Description |
|------|------|-------------|
| `package/PLAN.md` | Doc | This plan. |
| `package/package_model_bundle.py` | New | Packaging script (source dir, output dir, optional version, optional archive). |
| `trainer/api_server.py` | Modify | Port 8001; add GET `/alerts` and GET `/validation` with 24h default, `limit`, protocol fields, `casino_player_id`/`is_known_player`/`TP`. |

---

## 7. Risks and notes

- **DB and timezone**: "Last 24 hours" should be computed from "now" in HK to avoid server/DB timezone mismatch.
- **Backward compatibility**: Keep `/get_alerts` and `/get_validation` and reuse the same logic so existing callers are unchanged.
- **Memory**: API only reads DB and serializes; use `ts` + `limit` to avoid pulling full tables in one response.
