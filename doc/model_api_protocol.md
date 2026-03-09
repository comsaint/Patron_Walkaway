# Model–App Interfacing API Protocol

> **Purpose**: Decouple the ML model (training / testing / inference) from the app backend so the model team can develop independently and expose inference as a service.

---

## 1  Current Architecture (Monolith)

```
ClickHouse ──► scorer.py ──► SQLite (alerts table) ──► api_server.py ──► Frontend
                  │
                  ├─ loads walkaway_model.pkl (joblib)
                  ├─ fetches raw bets + sessions from ClickHouse
                  ├─ engineers features internally
                  ├─ calls model.predict_proba()
                  └─ writes alerts to SQLite
```

Everything lives in one process. The model team's deliverable today is a `.pkl` file dropped into `models/`.

---

## 2  Target Architecture (Decoupled)

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

#### Feature Schema (all required, all numeric)

| Feature                      | Type  | Description                                            |
|------------------------------|-------|--------------------------------------------------------|
| `bet_id`                     | int   | Unique bet identifier (pass-through, not a model input)|
| `session_id`                 | int   | Session identifier (pass-through, not a model input)   |
| `wager`                      | float | Wager amount for this bet                              |
| `payout_odds`                | float | Payout odds                                            |
| `base_ha`                    | float | Base house advantage                                   |
| `is_back_bet`                | int   | 1 = back bet, 0 = normal (filtered to 0 before sending)|
| `position_idx`               | int   | Seat position index                                    |
| `minutes_since_session_start`| float | Minutes elapsed since session opened                   |
| `minutes_to_session_end`     | float | Minutes remaining to session close (0 if still open)   |
| `cum_bets`                   | int   | Cumulative bet count within the session so far          |
| `cum_wager`                  | float | Cumulative wager within the session so far              |
| `avg_wager_sofar`            | float | Running average wager = cum_wager / cum_bets            |
| `bets_last_5m`               | int   | Rolling count of bets in last 5 minutes                 |
| `bets_last_15m`              | int   | Rolling count of bets in last 15 minutes                |
| `bets_last_30m`              | int   | Rolling count of bets in last 30 minutes                |
| `session_duration_min`       | float | Session duration in minutes up to this bet              |
| `wager_last_10m`             | float | Rolling wager sum in last 10 minutes                   |
| `wager_last_30m`             | float | Rolling wager sum in last 30 minutes                   |
| `bets_per_minute`            | float | Betting pace = cum_bets / (session_duration_min + ε)    |

> **Note**: `bet_id` and `session_id` are identifiers echoed back in the response for joining. They are **not** model input features.

#### Response

```json
{
  "model_version": "v2.1.0",
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
| `model_version` | string | Semantic version of the deployed model                        |
| `threshold`     | float  | The model's optimal threshold (app uses this for alerting)    |
| `scores[].bet_id`     | int    | Echo of input bet_id                                    |
| `scores[].session_id` | int    | Echo of input session_id                                |
| `scores[].score`      | float  | Walkaway probability ∈ [0, 1]                           |
| `scores[].alert`      | bool   | `score >= threshold` (convenience; app may override)     |

---

### 3.2  Endpoint: `GET /health`

```json
{
  "status": "ok",
  "model_version": "v2.1.0",
  "model_loaded": true
}
```

---

### 3.3  Endpoint: `GET /model_info`

Returns model metadata for monitoring and debugging.

```json
{
  "model_version": "v2.1.0",
  "model_type": "LightGBM",
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
  "training_metrics": {
    "validation_precision": 0.74,
    "validation_recall": 0.31,
    "val_samples": 12480,
    "positive_rate": 0.048
  }
}
```

---

## 4  Behavioral Rules

| Rule | Detail |
|------|--------|
| **Batch size** | App sends 500–5,000 rows per call (one scoring cycle). Service must handle up to 10,000 rows. |
| **Latency** | `POST /score` must respond within **3 seconds** for up to 5,000 rows. |
| **Missing features** | Service must reject with HTTP 422 listing missing columns. Do **not** silently impute. |
| **NaN / null** | App fills NaN → 0 before sending. Service should validate no nulls remain. |
| **Polling cadence** | App calls every ~45 seconds. |
| **Idempotency** | Scoring is stateless and idempotent. Same input → same output. |
| **Feature changes** | When the feature list changes, bump `model_version` and update `/model_info`. App will log a warning if feature list diverges from expected. |

---

## 5  Error Responses

| HTTP Status | Meaning | Body |
|-------------|---------|------|
| 200 | Success | Scores response |
| 400 | Malformed JSON or empty `rows` | `{"error": "description"}` |
| 422 | Missing or extra features | `{"error": "missing features", "missing": ["col1"], "extra": ["col2"]}` |
| 422 | Invalid feature types (non int/float/bool) | `{"error": "invalid feature types", "missing": [], "extra": ["field1", ...]}` |
| 503 | Model not loaded / warming up | `{"error": "model not ready"}` |

### 5.1  Implementation notes (api_server)

- **Request shape**: Body must be a single object `{"rows": [...]}`. Non-object, missing `rows`, or `rows` not an array → 400 `{"error": "Malformed JSON or missing 'rows'"}`. Empty array `rows` → 400 `{"error": "empty rows"}`.
- **Feature list**: The set of required feature keys is determined by the service (e.g. from `feature_list.json`). Each row must contain every feature in that list plus `bet_id`.
- **Reserved key**: `is_rated` (optional, default false) is reserved and not treated as a feature or pass-through.
- **Pass-through**: Any key in a row that is not in the feature list and not reserved is echoed as-is in the corresponding `scores[i]`. Server may log pass-through keys once per request.
- **training_metrics** (`GET /model_info`): Sourced from `training_metrics.json` (e.g. under model dir). If the file is missing or read fails, or the root value is not a JSON object, the response uses `{}`. The value is returned as-is from the file (no reshaping).

*Phase 1 alignment: `trainer/api_server.py` implements the above. Features are taken from `feature_list.json` in the model artifact; request/response and error bodies match §3 and §5.*

---

## 6  What Each Side Owns

### App Team (scorer.py + api_server.py)

- Fetch raw bets & sessions from ClickHouse
- Maintain session state (cumulative bet counts, wager sums) in SQLite
- Compute all features listed in § 3.1
- POST features to Model Service
- Apply returned `alert` flag (or override with own threshold)
- Deduplicate alerts, persist to SQLite, serve to frontend

### Model Team (Model Service)

- Train / retrain models on provided data
- Host `POST /score`, `GET /health`, `GET /model_info`
- Manage model versioning and threshold tuning
- Validate incoming feature schema
- Return calibrated probabilities

> **The model team does NOT need access to ClickHouse or SQLite.** All data arrives via the `/score` request payload.

---

## 7  Migration Path

1. Model team stands up the service with `/score`, `/health`, `/model_info`.
2. App team adds a `score_via_api()` function in `scorer.py` that POSTs features and parses the response.
3. Run both paths (local `.pkl` + API) in shadow mode; compare scores to validate parity.
4. Cut over: remove `joblib.load`, `model.predict_proba` from scorer; API becomes the single scoring path.
5. Remove `models/walkaway_model.pkl` from the app repo.

---

## 8  Quick-Start Example (Model Service)

Minimal Flask scaffold for the model team:

```python
from flask import Flask, request, jsonify
import joblib, numpy as np

app = Flask(__name__)
bundle = joblib.load("walkaway_model.pkl")
model = bundle["model"]
FEATURES = bundle["features"]
THRESHOLD = bundle.get("threshold", 0.5)
VERSION = "v1.0.0"

@app.post("/score")
def score():
    data = request.get_json()
    rows = data.get("rows", [])
    if not rows:
        return jsonify({"error": "empty rows"}), 400

    import pandas as pd
    df = pd.DataFrame(rows)

    missing = [f for f in FEATURES if f not in df.columns]
    extra = [c for c in df.columns if c not in FEATURES + ["bet_id", "session_id"]]
    if missing:
        return jsonify({"error": "missing features", "missing": missing, "extra": extra}), 422

    X = df[FEATURES].fillna(0)
    probs = model.predict_proba(X)[:, 1]

    scores = []
    for i, row in df.iterrows():
        scores.append({
            "bet_id": int(row["bet_id"]),
            "session_id": int(row["session_id"]),
            "score": round(float(probs[i]), 6),
            "alert": bool(probs[i] >= THRESHOLD),
        })

    return jsonify({
        "model_version": VERSION,
        "threshold": THRESHOLD,
        "scores": scores,
    })

@app.get("/health")
def health():
    return jsonify({"status": "ok", "model_version": VERSION, "model_loaded": True})

@app.get("/model_info")
def model_info():
    return jsonify({
        "model_version": VERSION,
        "model_type": type(model).__name__,
        "threshold": THRESHOLD,
        "feature_count": len(FEATURES),
        "features": FEATURES,
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001)
```

---

*Document generated from codebase analysis on 2026-02-27. Feature list reflects `trainer.py` and `scorer.py` as of this date.*
