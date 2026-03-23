# ML API Protocol

> **For**: Dashboard team (`wa1`) integration  
> **Base URL**: `http://localhost:8001`  
> **Content-Type**: `application/json`  
> **CORS**: fully open (`*`)

The ML service runs on the same machine as the dashboard and exposes two
GET endpoints. The dashboard polls them for data.

---

## Endpoints at a Glance

| Method | Path           | Purpose                          |
|--------|----------------|----------------------------------|
| GET    | `/alerts`      | Get walkaway alerts              |
| GET    | `/validation`  | Get validation results           |

---

## 1. `GET /alerts`

Returns walkaway alerts.

### Query Parameters

| Param   | Type   | Default | Description |
|---------|--------|---------|-------------|
| `ts`    | string | —       | ISO timestamp; return only alerts **after** this time |
| `limit` | int    | —       | Max number of alerts (only used when `ts` is absent) |

If neither is given, returns alerts from the **last 1 hour**.

### Response

```json
{
  "alerts": [
    {
      "bet_id": 123456789,
      "ts": "2026-03-11T14:30:00+08:00",
      "bet_ts": "2026-03-11T14:29:55+08:00",
      "player_id": "P001",
      "casino_player_id": null,
      "table_id": "T42",
      "position_idx": 3,
      "session_id": 987654321,
      "visit_avg_bet": 520.0,
      "is_known_player": 1
    }
  ]
}
```

| Field | Meaning |
|-------|---------|
| `ts`  | When the alert was generated (HK time) |
| `bet_ts` | Original bet timestamp |
| `bet_id` | Bet identifier |
| `player_id` | Player identifier |
| `casino_player_id` | Casino-assigned player ID |
| `table_id` | Table identifier |
| `position_idx` | Seat position |
| `session_id` | Session identifier |
| `visit_avg_bet` | Player's average bet for the visit |
| `is_known_player` | Whether the player is a known/tracked player |

All timestamps are **HK timezone** (`+08:00`).

---

## 2. `GET /validation`

Returns detailed validation results.

### Query Parameters

| Param    | Type   | Default | Description |
|----------|--------|---------|-------------|
| `ts`     | string | —       | ISO timestamp; results validated **after** this time |
| `bet_id` | string | —       | Single bet ID lookup |
| `bet_ids`| string | —       | Comma-separated bet IDs (e.g. `123,456,789`) |

If none given, returns results from the **last 1 hour**.

### Response

```json
{
  "results": [
    {
      "ts": "2026-03-11T14:30:00+08:00",
      "player_id": "P001",
      "casino_player_id": null,
      "bet_id": "123456789",
      "walkaway_ts": "2026-03-11T15:05:00+08:00",
      "TP": "TP",
      "sync_ts": "2026-03-11T15:50:00+08:00",
      "reason": "MATCH",
      "bet_ts": "2026-03-11T14:29:55+08:00"
    }
  ]
}
```

| Field         | Meaning |
|---------------|---------|
| `ts`          | Original alert timestamp |
| `player_id`   | Player identifier |
| `casino_player_id` | Casino-assigned player ID |
| `bet_id`      | Bet identifier |
| `walkaway_ts` | When the player actually walked away (`null` if MISS) |
| `TP`          | Validation result (`TP` = true positive, etc.) |
| `sync_ts`     | When validation was performed |
| `reason`      | `MATCH` or `MISS` |
| `bet_ts`      | Original bet timestamp |

---

## Polling Pattern (recommended for dashboard)

```
Every N seconds:
  GET /alerts?ts={last_seen_ts}        → incremental alert fetch
  GET /validation?ts={last_seen_ts}    → incremental validation fetch
```

Keep track of the latest `ts` / `sync_ts` you received and pass it as `?ts=`
on the next poll to avoid re-fetching old data.

---

## Notes

- **Port**: `8001`
- **No auth** — designed to run on the same machine / LAN only.
- **All timestamps** are Hong Kong time (`Asia/Hong_Kong`, `+08:00`).
- **`NaN` / `Inf`** values in alert fields are normalized to `null`.
