# Model–App Interfacing API Protocol (v2)

> **Purpose**: Decouple the ML model (training / testing / inference) from the app backend so the model team can develop independently and expose inference as a service.
>
> **v2** aligns this document with the current codebase: v10 single-model artifact (`model.pkl`), dynamic feature list from `feature_list.json`, and full artifact set (feature_spec, reason_code_map, training_metrics).

---

## Summary of changes from v1

| Area | v1 | v2 (current implementation) |
|------|----|-----------------------------|
| **Primary model artifact** | `walkaway_model.pkl` | **`model.pkl`** (v10 DEC-021); fallbacks: `rated_model.pkl` → `walkaway_model.pkl` |
| **Feature list** | Fixed 17-feature schema in doc | **Dynamic**: from `feature_list.json` or `model.pkl` key `"features"`; each row may include `{"name", "track"}` with `track` ∈ `track_llm` / `track_human` / `track_profile` |
| **Artifacts** | Single .pkl | **Full set**: `model.pkl`, `feature_list.json`, `feature_spec.yaml` (frozen), `reason_code_map.json`, `model_version`, `training_metrics.json`; legacy `walkaway_model.pkl` still written for backward compat |
| **Profile vs non-profile** | Not specified | **Profile** features may be NaN (no prior snapshot); **non-profile** filled 0; service/coerce per `coerce_feature_dtypes` for train–serve parity |
| **Model version format** | Semantic e.g. `v2.1.0` | **`YYYYMMDD-HHMMSS-<git7>`** (from `get_model_version()`) |
| **App api_server** | Implements /score, /health, /model_info | **Does not** expose scoring; only frontend, get_alerts, get_validation, get_floor_status, etc. The **Model Service** (separate) implements /score, /health, /model_info when decoupled |
| **Reserved / pass-through** | `bet_id`, `session_id` pass-through; `is_rated` reserved | Same; **pass-through**: any key not in feature list and not reserved echoed in `scores[i]` |

---

## 1  Current Architecture (Monolith)

**Changed (v2):** Diagram and artifact list reflect v10 and fallback chain.

```
ClickHouse ──► scorer.py ──► SQLite (alerts table) ──► api_server.py ──► Frontend
                  │
                  ├─ loads model.pkl (v10) or rated_model.pkl or walkaway_model.pkl (joblib)
                  ├─ feature list from feature_list.json (or pkl "features")
                  ├─ fetches raw bets + sessions from ClickHouse
                  ├─ engineers features internally (features.py; track_llm / track_human / track_profile)
                  ├─ calls model.predict_proba()
                  └─ writes alerts to SQLite
```

Everything lives in one process. The model team's deliverable is the **models/** artifact directory: **model.pkl** (primary), **feature_list.json**, **feature_spec.yaml**, **reason_code_map.json**, **model_version**, **training_metrics.json**; optionally **rated_model.pkl** / **walkaway_model.pkl** for backward compatibility.

---

## 2  Target Architecture (Decoupled)

Unchanged from v1.

```
ClickHouse ──► scorer.py ──────POST /score──────► Model Service (yours)
                  │                                     │
                  │  sends bet-level feature rows        │  returns scores
                  │◄────────────────────────────────────┘
                  │
                  └─► SQLite (alerts) ──► api_server ──► Frontend
```

- **scorer.py** (app side) remains responsible for: ClickHouse data fetch, session state tracking, alert dedup, alert persistence.
- **Model Service** (model team) is responsible for: model loading, feature validation, `predict_proba`, threshold management, model versioning.

---

## 3  The API Contract

### 3.1  Endpoint: `POST /score`

Score a batch of bet-level feature rows and return walkaway probabilities.

#### Request

**Changed (v2):** Required feature keys are **defined by the model artifact** (`feature_list.json` or `model.pkl` key `"features"`), not a fixed list. Each row must include every feature in that list plus at least `bet_id` (and typically `session_id`) for echo-back.

```
POST /score
Content-Type: application/json
```

```json
{
  "rows": [
    {
      "bet_id": 123456789,
      "session_id": 987654321,
      "<feature_1>": <numeric>,
      "<feature_2>": <numeric>,
      "…": "…"
    }
  ]
}
```

- Replace `<feature_1>`, `<feature_2>`, … with the **exact names and order** from the model artifact (`feature_list.json` or `model.pkl["features"]`). Use **GET /model_info** to obtain the current list.
- **bet_id** and **session_id** are identifiers (pass-through); they are echoed in the response and are **not** model input features.
- **Reserved key:** `is_rated` (optional, default false) is reserved; do not treat as a feature or pass-through.
- **Pass-through:** Any other key in a row that is not in the feature list is echoed as-is in the corresponding `scores[i]`.

#### Feature schema (v2: dynamic)

| Item | Description |
|------|--------------|
| **Feature set** | Determined by the service from **feature_list.json** (array of `{"name": "<id>", "track": "track_llm"|"track_human"|"track_profile"}`) or from **model.pkl** key `"features"`. Names are normalized to a list of strings for validation. |
| **Types** | All feature values must be numeric (int/float). App fills NaN → 0 for non-profile features before sending; profile features may be null/NaN if no prior snapshot (model uses NaN-aware splits). |
| **Order** | Rows must supply columns in the same order as the model expects (use `df[model_features]` or equivalent). |

Example shape when the artifact has 17 features (e.g. wager, payout_odds, base_ha, …):

```json
{
  "rows": [
    {
      "bet_id": 123456789,
      "session_id": 987654321,
      "wager": 500.0,
      "payout_odds": 1.0,
      "base_ha": 0.0262,
      "is_back_bet": 0,
      "position_idx": 3,
      "minutes_since_session_start": 42.5,
      "minutes_to_session_end": 0.0,
      "cum_bets": 18,
      "cum_wager": 9200.0,
      "avg_wager_sofar": 511.11,
      "bets_last_5m": 3,
      "bets_last_15m": 8,
      "bets_last_30m": 14,
      "session_duration_min": 42.5,
      "wager_last_10m": 2100.0,
      "wager_last_30m": 7500.0,
      "bets_per_minute": 0.42
    }
  ]
}
```

*(The exact feature names and count come from the deployed artifact; the above is an example.)*

#### Response

Unchanged from v1.

```json
{
  "model_version": "20260310-143022-a1b2c3d",
  "threshold": 0.62,
  "scores": [
    {
      "bet_id": 123456789,
      "session_id": 987654321,
      "score": 0.847,
      "alert": true
    }
  ]
}
```

| Field           | Type   | Description                                                   |
|-----------------|--------|---------------------------------------------------------------|
| `model_version` | string | Version of the deployed model (e.g. **YYYYMMDD-HHMMSS-&lt;git7&gt;** from `model_version` file) |
| `threshold`     | float  | The model's optimal threshold (app uses this for alerting)    |
| `scores[].bet_id`     | int    | Echo of input bet_id                                    |
| `scores[].session_id` | int    | Echo of input session_id                                |
| `scores[].score`      | float  | Walkaway probability ∈ [0, 1]                           |
| `scores[].alert`      | bool   | `score >= threshold` (convenience; app may override)     |

---

### 3.2  Endpoint: `GET /health`

Unchanged.

```json
{
  "status": "ok",
  "model_version": "20260310-143022-a1b2c3d",
  "model_loaded": true
}
```

---

### 3.3  Endpoint: `GET /model_info`

**Changed (v2):** `features` and `feature_count` come from the **artifact** (feature_list.json or model.pkl). **training_metrics** is read **as-is** from **training_metrics.json** (no reshaping). Optional **reason_code_map** can be exposed for SHAP/reason codes.

Returns model metadata for monitoring and debugging.

```json
{
  "model_version": "20260310-143022-a1b2c3d",
  "model_type": "Booster",
  "threshold": 0.62,
  "feature_count": 17,
  "features": [
    "wager", "payout_odds", "base_ha", "is_back_bet", "position_idx",
    "minutes_since_session_start", "minutes_to_session_end",
    "cum_bets", "cum_wager", "avg_wager_sofar",
    "bets_last_5m", "bets_last_15m", "bets_last_30m",
    "session_duration_min", "wager_last_10m", "wager_last_30m",
    "bets_per_minute"
  ],
  "training_metrics": {}
}
```

- **training_metrics**: Sourced from **training_metrics.json** in the model directory. If the file is missing or the root value is not a JSON object, use `{}`. Return the value as-is (no reshaping).
- **features**: List of feature names in model order (from **feature_list.json** or **model.pkl["features"]**).

---

## 4  Behavioral Rules

Unchanged from v1.

| Rule | Detail |
|------|--------|
| **Batch size** | App sends 500–5,000 rows per call (one scoring cycle). Service must handle up to 10,000 rows. |
| **Latency** | `POST /score` must respond within **3 seconds** for up to 5,000 rows. |
| **Missing features** | Service must reject with HTTP 422 listing missing columns. Do **not** silently impute. |
| **NaN / null** | App fills NaN → 0 for non-profile before sending. Profile features may be NaN; service should accept or reject per schema. |
| **Polling cadence** | App calls every ~45 seconds. |
| **Idempotency** | Scoring is stateless and idempotent. Same input → same output. |
| **Feature changes** | When the feature list changes, bump `model_version` and update `/model_info`. App will log a warning if feature list diverges from expected. |

---

## 5  Error Responses

Unchanged from v1.

| HTTP Status | Meaning | Body |
|-------------|---------|------|
| 200 | Success | Scores response |
| 400 | Malformed JSON or empty `rows` | `{"error": "description"}` |
| 422 | Missing or extra features | `{"error": "missing features", "missing": ["col1"], "extra": ["col2"]}` |
| 422 | Invalid feature types (non int/float/bool) | `{"error": "invalid feature types", "missing": [], "extra": ["field1", ...]}` |
| 503 | Model not loaded / warming up | `{"error": "model not ready"}` |

### 5.1  Implementation notes (Model Service)

**Changed (v2):** These notes apply to the **Model Service** (the service that exposes /score, /health, /model_info). The app’s **api_server** in this repo does **not** serve those endpoints; it serves the frontend, get_alerts, get_validation, get_floor_status, etc.

- **Request shape**: Body must be a single object `{"rows": [...]}`. Non-object, missing `rows`, or `rows` not an array → 400 `{"error": "Malformed JSON or missing 'rows'"}`. Empty array `rows` → 400 `{"error": "empty rows"}`.
- **Feature list**: The set of required feature keys is determined by the service from **feature_list.json** (normalize to list of names) or from **model.pkl** key **"features"**. Each row must contain every feature in that list plus `bet_id`.
- **Reserved key**: `is_rated` (optional, default false) is reserved and not treated as a feature or pass-through.
- **Pass-through**: Any key in a row that is not in the feature list and not reserved is echoed as-is in the corresponding `scores[i]`. Server may log pass-through keys once per request.
- **Model loading**: Load **model.pkl** first; if absent, **rated_model.pkl**; then **walkaway_model.pkl**. Each pkl is a dict: `{"model", "threshold", "features"}` (legacy may have `"features"` only in walkaway_model).
- **training_metrics** (`GET /model_info`): Sourced from **training_metrics.json** in the model directory. If the file is missing or read fails, or the root value is not a JSON object, the response uses `{}`. The value is returned as-is from the file (no reshaping).

---

## 6  What Each Side Owns

**Changed (v2):** App computes **all features** required by the **current artifact** (feature_list.json / model.pkl), not a fixed list in the doc.

### App Team (scorer.py + api_server.py)

- Fetch raw bets & sessions from ClickHouse
- Maintain session state (cumulative bet counts, wager sums) in SQLite
- Compute all features required by the model artifact (feature_list / feature_spec; track_llm, track_human, track_profile)
- POST features to Model Service (when decoupled)
- Apply returned `alert` flag (or override with own threshold)
- Deduplicate alerts, persist to SQLite, serve to frontend

### Model Team (Model Service)

- Train / retrain models; write **model.pkl**, **feature_list.json**, **feature_spec.yaml**, **reason_code_map.json**, **model_version**, **training_metrics.json**
- Host `POST /score`, `GET /health`, `GET /model_info` (when decoupled)
- Manage model versioning and threshold tuning
- Validate incoming feature schema against artifact feature list
- Return calibrated probabilities

> **The model team does NOT need access to ClickHouse or SQLite.** All data arrives via the `/score` request payload.

---

## 7  Migration Path

**Changed (v2):** Step 5 refers to **model.pkl** and legacy fallbacks.

1. Model team stands up the service with `/score`, `/health`, `/model_info`.
2. App team adds a `score_via_api()` function in `scorer.py` that POSTs features and parses the response.
3. Run both paths (local **model.pkl** + API) in shadow mode; compare scores to validate parity.
4. Cut over: remove `joblib.load`, `model.predict_proba` from scorer; API becomes the single scoring path.
5. Remove **models/model.pkl** (and optionally **rated_model.pkl** / **walkaway_model.pkl**) from the app repo once the service is the only scoring path.

---

## 8  Quick-Start Example (Model Service)

**Changed (v2):** Load **model.pkl** (with fallbacks), feature list from **feature_list.json** or pkl; use **model_version** file; support **training_metrics.json** in `/model_info`.

Minimal Flask scaffold for the model team:

```python
from pathlib import Path
import json
import joblib
from flask import Flask, request, jsonify

app = Flask(__name__)
MODEL_DIR = Path("models")

# Load artifact: model.pkl → rated_model.pkl → walkaway_model.pkl
def load_bundle():
    for name in ("model.pkl", "rated_model.pkl", "walkaway_model.pkl"):
        p = MODEL_DIR / name
        if p.exists():
            return joblib.load(p), name
    raise FileNotFoundError("No model.pkl / rated_model.pkl / walkaway_model.pkl found")

bundle, _ = load_bundle()
model = bundle["model"]
FEATURES = bundle.get("features") or []
if not FEATURES and (MODEL_DIR / "feature_list.json").exists():
    with (MODEL_DIR / "feature_list.json").open(encoding="utf-8") as f:
        raw = json.load(f)
    FEATURES = [e["name"] if isinstance(e, dict) else str(e) for e in raw]
THRESHOLD = float(bundle.get("threshold", 0.5))
VERSION = (MODEL_DIR / "model_version").read_text(encoding="utf-8").strip() if (MODEL_DIR / "model_version").exists() else "unknown"

@app.post("/score")
def score():
    data = request.get_json()
    rows = data.get("rows", [])
    if not rows:
        return jsonify({"error": "empty rows"}), 400

    import pandas as pd
    df = pd.DataFrame(rows)

    missing = [f for f in FEATURES if f not in df.columns]
    allowed = set(FEATURES) | {"bet_id", "session_id", "is_rated"}
    extra = [c for c in df.columns if c not in allowed]
    if missing:
        return jsonify({"error": "missing features", "missing": missing, "extra": extra}), 422

    X = df[FEATURES].copy()
    # Non-profile: fillna(0); profile may stay NaN if desired
    X = X.fillna(0)
    probs = model.predict_proba(X)[:, 1]

    scores = []
    for i, row in df.iterrows():
        out = {"bet_id": int(row["bet_id"]), "session_id": int(row["session_id"]),
               "score": round(float(probs[i]), 6), "alert": bool(probs[i] >= THRESHOLD)}
        for k, v in row.items():
            if k not in FEATURES and k not in ("bet_id", "session_id", "is_rated"):
                out[k] = v
        scores.append(out)

    return jsonify({"model_version": VERSION, "threshold": THRESHOLD, "scores": scores})

@app.get("/health")
def health():
    return jsonify({"status": "ok", "model_version": VERSION, "model_loaded": True})

@app.get("/model_info")
def model_info():
    metrics = {}
    try:
        p = MODEL_DIR / "training_metrics.json"
        if p.exists():
            metrics = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(metrics, dict):
                metrics = {}
    except Exception:
        pass
    return jsonify({
        "model_version": VERSION,
        "model_type": type(model).__name__,
        "threshold": THRESHOLD,
        "feature_count": len(FEATURES),
        "features": FEATURES,
        "training_metrics": metrics,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001)
```

---

*Document v2 aligned with current implementation. Artifacts and behaviour reflect `trainer/scorer.py`, `trainer/trainer.py`, and `trainer/features.py` as of the v10 single-model (DEC-021) and feature_list.json with name+track.*
