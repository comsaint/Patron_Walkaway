# Player-Game Level Evaluation Implementation Plan

> 文件層級：**Implementation Plan**。  
> 目的：把 high-tier walkaway 模型的主評估與告警輸出，從 bet-level 轉為 **player-game level**，以符合「alert on player-game」的業務目標，並降低同一玩家在同一局內多筆 side bets 對 performance metric 與 alert volume 的放大偏差。  
> 本計畫不重新定義 label 業務語意、不重訓成 group-level model，也不引入新的 player-day / player-session alert 聚合。

---

## 1. Objective and Scope

### 1.1 Objective

目前模型仍對每一筆 bet 產生 score，但 offline evaluation 與 production alert decision 要改成：

- **Group key**：`player_id + game_id`
- **Score aggregation**：`player_game_score = max(score within player-game)`
- **Label aggregation**：`player_game_label = max(label within player-game)`
- **Alert output**：一列代表一個 player-game alert

此策略保留現有 bet-level model 與 feature pipeline，只在 prediction 後增加 aggregation layer，作為最快可驗證的行為修正。

### 1.2 Non-Scope

- 不改 `walkaway_label` 的定義與 lookahead/gap 規則。
- 不把訓練樣本直接改成 player-game grain。
- 不在本輪加入 bet sample weight，例如 `1 / bets_per_player_game`。
- 不把 alert 再聚合到 player-day 或 player-session。
- 不改 main bet / side bet 的 feature engineering 或 bet type hierarchy。
- 不用環境變數控制新行為；若需要開關，走現有 config / Python 常數模式。

---

## 2. Frozen Decisions

### 2.1 Grain Contract

Canonical evaluation grain 為：

```text
player_game_key = (player_id, game_id)
```

`game_id` 依目前資料假設為牌局唯一識別碼。本輪已用本地 `t_bet` sample 驗證：

- `rows = 494,755`
- `game_ids = 177,312`
- `null_game_id_rows = 0`
- `null_player_id_rows = 0`
- `null_payout_complete_dtm_rows = 0`
- `violating game_id -> payout_complete_dtm = 0`

因此本輪不把 `table_id`、`gaming_day`、`payout_complete_dtm` 加進 group key。它們可保留在 output metadata 供 audit，但不參與 grouping。

### 2.2 Null and Invalid Data Policy

`player_id`、`game_id`、score、label 在 eligible scoring/evaluation rows 中都應該存在。

若 offline evaluation 遇到同一 player-game 中任一必要欄位為 null 或 non-finite：

- log warning，包含 split、缺失欄位與受影響 row / player-game 數。
- 排除該 player-game，不做 fallback 到 bet-level。
- 在 `training_metrics.json` 中寫入排除計數，避免靜默吞掉 data bug。

若 production scoring 遇到缺 `game_id` 或 score non-finite：

- 該 row 不產生 alert。
- log warning 並在 cycle metrics 中暴露排除數。
- 不把缺 key 的 bet 當作單獨 player-game，避免污染 alert contract。

### 2.3 Metric Priority

主 KPI 改為 player-game level：

- `player_game_ap`
- `player_game_precision`
- `player_game_recall`
- `player_game_f1`
- `player_game_alerts`
- `player_game_samples`
- `player_game_positives`
- `player_game_alerts_per_hour`
- `player_game_true_labels_per_hour`

Bet-level metrics 可保留為 debug / compatibility，但命名必須明確標示 `bet_level_*` 或放入 legacy section，避免被下游誤當主 KPI。

### 2.4 Threshold Selection

第一版 threshold 應在 validation split 的 player-game scores 上重新挑選：

```text
val_player_game_score = max(val_bet_scores within player_id + game_id)
val_player_game_label = max(val_labels within player_id + game_id)
threshold = pick_threshold_precision_floor(val_player_game_label, val_player_game_score)
```

原因：如果 threshold 仍由 bet-level validation 選出，但 production alert 改成 player-game level，offline precision/recall 的 operating point 會不一致。

---

## 3. Solution Approach

### 3.1 Step 3 / Step 4 Data Contract

`trainer_hightier/03_build_training_data.py` 目前 join labels 後只明確加入 `canonical_id` 與 `gaming_day_event`，Step 5 目前也沒有直接看到 `game_id`。本輪要確保 split parquet 進 Step 5 前保留：

- `bet_id`
- `player_id`
- `game_id`
- `walkaway_label`
- `gaming_day_event`

建議在 Step 3 的 cleaned bet join 中把 `game_id` 以 `bet_id` join 回 training set，Step 4 僅做 pass-through，不把 `game_id` 視為 feature。

### 3.2 Step 5 Aggregation Helper

在 `trainer_hightier/05_lgbm_train.py` 新增本地 helper，將 bet-level arrays + grouping columns 聚合為 player-game arrays：

- 輸入：split DataFrame、score array、label column、`player_id`、`game_id`
- 輸出：一列一個 player-game，包含：
  - `player_id`
  - `game_id`
  - `player_game_score`
  - `player_game_label`
  - `bet_count`
  - `positive_bet_count`
  - optional metadata：`payout_complete_dtm` / `gaming_day_event` 若該 split 中存在

核心聚合規則：

```text
score = max(score)
label = max(walkaway_label)
```

此 helper 只服務 Step 5 evaluation，不進模型 feature set。

### 3.3 Step 5 Metrics and Artifact Contract

Step 5 flow 改為：

1. 保持 LightGBM 對 bet-level rows fit / predict。
2. 產生 train / val / test bet-level scores。
3. 對每個 split 聚合成 player-game scores。
4. 用 validation player-game scores 選 threshold。
5. 用同一 threshold 報告 train / val / test player-game metrics。
6. 把 bet-level metrics 保留為 debug block。

`training_metrics.json` 建議新增或調整欄位：

```text
evaluation_grain = "player_game"
player_game_group_key = ["player_id", "game_id"]
score_aggregation = "max"
label_aggregation = "max"
step5_threshold_grain = "player_game"
step5_threshold = <player-game selected threshold>
train_player_game_*
val_player_game_*
test_player_game_*
train_bet_level_*
val_bet_level_*
test_bet_level_*
```

若需要維持 legacy consumer，短期可把原本 `train_precision` / `val_precision` / `test_precision` 改為 player-game 主口徑，並把舊 bet-level 值改名為 `*_bet_level_*`。這是行為改變，必須在 release note 明確說明。

### 3.4 Production Scorer Alert Output

`trainer_hightier/serving/scorer.py` 目前對每筆 staged bet 算 `prob >= threshold` 後直接寫 alerts。新流程應改為：

1. 對 staged bet 保持逐 bet scoring。
2. append `prediction_log` 仍可保留 all scored bet rows，供 audit / debug。
3. 在 alert decision 前用 `player_id + game_id` 聚合 scored rows。
4. 每個 player-game 選 `score` 最大的代表 bet row。
5. 以 `player_game_score >= threshold` 決定是否寫一筆 alert。
6. alert row 使用代表 bet row 的 `bet_id` 作為兼容主鍵，同時新增 / 保留 `game_id` 與 `player_game_score` metadata。

代表 bet row 選擇規則：

- score 最大者優先。
- 若 score tie，選最早 `payout_complete_dtm`。
- 若仍 tie，選最小 `bet_id`。

這讓 alert row deterministic，也方便 validator 仍可沿用 bet-level anchor 時間做 label 驗證。

### 3.5 State DB and Prediction Log Contract

`prediction_log` 目前沒有 `game_id` migration column，`alerts` schema 目前也以 `bet_id` 為 primary key。本輪需要：

- 在 `prediction_log` migration columns 加入 `game_id`。
- 在 `alerts` table 加入 `game_id`，可選加入 `player_game_score`、`player_game_bet_count`。
- 新增 index：
  - `prediction_log(player_id, game_id)`
  - `alerts(player_id, game_id)`

不建議第一版把 `alerts` primary key 從 `bet_id` 改為 `(player_id, game_id)`，因為 validator / API / existing consumers 目前多半依賴 `bet_id`。用「代表 bet id + game metadata」是風險較低的過渡方式。

---

## 4. Workstreams and Phases

### Phase A — Offline Evaluation Contract

Deliverables:

- Step 3 / Step 4 split parquet 保留 `game_id`。
- Step 5 新增 player-game aggregation helper。
- Validation threshold 改用 player-game validation scores 選。
- `training_metrics.json` 主口徑改為 player-game metrics。
- bet-level metrics 改成 debug / legacy 欄位。

Acceptance:

- 若 split 缺 `player_id` 或 `game_id`，Step 5 直接 fail with actionable error。
- `val_player_game_alerts` 等於 validation player-game threshold 下的 group alert count，而不是 bet count。
- `player_game_samples <= bet_level_samples`，且任一 split 不應反向。

### Phase B — Production Alert Aggregation

Deliverables:

- Scorer incremental fetch / staged feature frame 保留 `game_id`。
- Alert decision 從 bet-level mask 改為 player-game group decision。
- 每個 alerted player-game 只寫一筆 alert。
- cycle summary 的 `n_alerts` 改為 player-game alert count。

Acceptance:

- 同一 `player_id + game_id` 下多筆高分 bet 只產生一筆 alert。
- `prediction_log` 仍可看到所有 scored bet rows。
- `alerts` row 含 `game_id`，並可追溯代表 bet row。

### Phase C — Validation, API, and Reporting Alignment

Deliverables:

- Validator 若仍以 `bet_id` 驗證，文件化其語意為「代表 bet anchor 驗證」。
- API / export display 顯示 `game_id`。
- 報表中的 precision / recall / alert volume 一律使用 player-game count。

Acceptance:

- API alert table 不再把同一 player-game 的 side bets 顯示成多筆 active alerts。
- validation export 可區分 bet anchor fields 與 player-game alert identity。

### Phase D — Monitoring and Backtest Comparison

Deliverables:

- 產出一次 before / after backtest summary：
  - bet-level alert count vs player-game alert count
  - player concentration change
  - AP / precision / recall delta
  - average bets per alerted player-game
- 監控 production alert volume 是否因 aggregation 明顯下降。

Acceptance:

- 新舊口徑差異可解釋。
- 若 precision/recall 變化異常，能回查受影響 player-game examples。

---

## 5. Main File Boundaries

| Area | Files | Expected Change |
|------|-------|-----------------|
| Training data contract | `trainer_hightier/03_build_training_data.py`, `trainer_hightier/04_split_dataset.py` | 保留 `game_id` 到 split parquet；不把 `game_id` 當 feature。 |
| Offline metrics | `trainer_hightier/05_lgbm_train.py` | 新增 player-game aggregation、threshold selection、metrics artifact 欄位。 |
| Production scoring | `trainer_hightier/serving/scorer.py` | bet scores 聚合成 player-game alerts；代表 bet deterministic selection。 |
| Prediction log | `trainer_hightier/serving/prediction_log.py` | migration 加 `game_id`，write path 帶入 group metadata。 |
| Alert DB | `trainer_hightier/serving/state_db.py` | alerts schema / upsert 增加 `game_id` 與可選 group metadata。 |
| API / UI output | `trainer_hightier/serving/api_server.py` | alert output 帶 `game_id`，命名避免誤解 bet-level vs player-game-level。 |
| Tests | `trainer_hightier/tests/` | 補 aggregation、metrics、scorer dedupe、DB migration regression tests。 |

---

## 6. Validation Strategy

### 6.1 Unit Tests

- Aggregation helper：
  - 同一 `player_id + game_id` 多筆 bet → score 取 max，label 取 max。
  - 不同 player 同 game 不合併。
  - 同 player 不同 game 不合併。
  - null / non-finite score 或 null key 會 warn + exclude / fail，依 offline policy。

- Threshold selection：
  - validation threshold 使用 player-game rows。
  - alert count 是 group count，不是 bet count。

- Scorer alert aggregation：
  - 多筆 side bets 同 group 且都過 threshold，只寫一筆 alert。
  - 代表 bet tie-break deterministic。
  - `prediction_log` 仍寫所有 scored bet。

### 6.2 Regression Tests

- Existing Step 5 tests 更新 expected metrics naming。
- Existing scorer tests 更新 `n_alerts` expected value。
- Existing state DB / API tests 確認新增 `game_id` migration 不破壞舊 DB。

### 6.3 Data Quality Checks

在 pipeline 或 smoke test 中保留 `game_id -> payout_complete_dtm` consistency check：

```text
GROUP BY game_id
HAVING COUNT(DISTINCT payout_complete_dtm) > 1
```

若未來 full production data 出現 violation，先 warn 並 sample examples；若同時影響 group key uniqueness，再升級為 blocking DQ gate。

---

## 7. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `game_id` 未進 Step 5 split parquet | 無法做 offline player-game metrics | Phase A 先補資料契約，Step 5 schema gate 必須 fail fast。 |
| Legacy consumers 仍讀 `val_precision` | 指標語意改變造成誤讀 | `evaluation_grain` 明確寫入 artifact；release note 標示主 KPI 已改。 |
| `alerts` primary key 仍是 `bet_id` | 一個 player-game 的 identity 不夠明確 | 第一版保留代表 `bet_id` 相容性，同時加入 `game_id` 與 group metadata。 |
| Validator 仍以代表 bet anchor 驗證 | player-game alert 與 bet anchor label 可能被混淆 | 文件化代表 bet selection；後續若需要再設計 player-game validator。 |
| Alert volume 下降影響 operator perception | 現場以為模型變鈍 | Backtest 報告同時展示 bet-level vs player-game-level volume。 |

---

## 8. Open Questions

1. `alerts` table 是否只加 `game_id`，還是同步加 `player_game_score`、`player_game_bet_count`？
2. API export 是否要保留代表 `bet_id` 欄位名稱，或新增 `representative_bet_id` 以降低歧義？
3. `training_metrics.json` 是否直接覆蓋原 `train_precision` / `val_precision` / `test_precision` 為 player-game 主口徑，或先雙寫一版再切換 consumer？

建議 implementation 先採保守過渡：保留代表 `bet_id`、新增 `game_id` 與 `evaluation_grain` metadata，並在 artifacts 中同時雙寫 player-game 主口徑與 bet-level debug 口徑。
