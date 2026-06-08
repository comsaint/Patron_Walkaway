# trainer_hightier - Player Cooldown Offline Evaluation Implementation Plan

本文件屬於 **Implementation Plan 層**，定義如何在 offline training / evaluation 流程中落地 proposed 15 分鐘 player-level alert cooldown simulation。  
本文件不重新定義產品 SSOT，也不展開 ticket 級 task checklist；後續若要執行細項，應另由 Working Plan 承接。

## 0) 對齊範圍與非目標

### 0.1 背景

目前 high-tier pipeline 已將 bet-level model score 聚合到 `player_id + game_id` 的 player-game alert candidate。  
但同一玩家短時間內可能有多個 player-game candidate；從營運角度，若第一個 alert 已足以讓 host 去服務 patron，15 分鐘內重複 alert 的邊際價值較低。

因此本計畫先在 offline 評估中建立一個 proposed player-level cooldown reference，不立即改 production serving。

### 0.2 範圍

- 新增 offline-only player alert policy simulation module。
- 在現有 player-game aggregation 後，模擬 15 分鐘 player-level cooldown。
- 產出 proposed operational metrics，與現有 player-game metrics 並列。
- 保留現有 threshold selection 與 production serving 行為不變，第一輪只觀察差異。
- 為後續 actionable sample-weight training 預留資料契約與診斷輸出。

### 0.3 非目標

- 不把 cooldown 直接放進 production serving。
- 不改模型家族、不導入 ranker / reinforcement learning。
- 不在第一版改 threshold picking。
- 不在第一版把 covered positive 改成 negative。
- 不把 episode recall 設為主 KPI；第一版只可作後續延伸。

## 1) 已定決策（Decision Log）

| ID | 決策 | 理由 / 邊界 |
|----|------|-------------|
| D-001 | 第一版為 offline-only reference implementation。 | 先量化 proposed policy 對 precision / recall / alert volume 的影響，再決定是否進 production serving。 |
| D-002 | 新 module 放在 `trainer_hightier/evaluation/player_alert_policy.py`。 | 此邏輯屬 evaluation policy，不應塞入純 array metric 或 training script。 |
| D-003 | `cooldown_min = 15`。 | 對齊目前討論的 host response / label lookahead operational window。 |
| D-004 | Offline `alert_ts` 第一版使用 `payout_complete_dtm`。 | 與現有 labels、bet rows、player-game aggregation 較容易對齊；production `scored_at` 差異另行記錄。 |
| D-005 | Suppression 邊界採 `< 15 min` suppress，`>= 15 min` allow。 | 邊界明確且 deterministic。 |
| D-006 | 同 player candidate 排序採 `alert_ts ASC, score DESC, bet_id/game_id ASC`。 | 營運語意為先到先發；同時間才以 score 與穩定 id tie-break。 |
| D-007 | 第一版 recall 採 conservative player-game recall。 | 分母仍是所有 positive player-games；suppressed positives 不先從 recall 分母移除。 |
| D-008 | 第一版不改 threshold picking。 | 先並列報告 current player-game metrics 與 proposed operational metrics，避免一次改太多行為。 |
| D-009 | 後續 training feedback 先用 sample weight，不改 original label。 | Covered positive 仍可能是真風險，不應被教成 negative。 |

## 2) 實作目標（What to Realize）

### 2.1 Candidate Frame Contract

建立可供 policy simulation 消費的 player-game candidate frame。最小欄位：

- `player_id`
- `game_id`
- `player_game_score`
- `player_game_label`
- `alert_ts`
- `bet_id` 或 deterministic tie-break id

第一版可由現有 `aggregate_bets_to_player_game()` 擴充或伴隨 helper 建出；其語意必須與現有 Step 5 player-game evaluation 對齊：

- score: player-game 內 bet score 的 max。
- label: player-game 內 bet label 的 max。
- representative alert timestamp: 觸發 max score 的 bet 的 `payout_complete_dtm`；同 score tie 時選最早 timestamp，再以 `bet_id` 穩定排序。

### 2.2 Player Cooldown Simulation

新增小型函數，輸入 candidate frame 與 threshold，輸出每個 candidate 的 decision：

- `is_candidate`: `player_game_score >= threshold`
- `is_raised`: 通過 threshold 且未被同 player 前一個 raised alert cooldown suppress
- `is_suppressed`: 通過 threshold 但被 cooldown suppress
- `last_raised_alert_ts`: 診斷欄位，可選
- `cooldown_remaining_sec`: 診斷欄位，可選

核心語意：

```text
For each player, candidates are processed chronologically.
If alert_ts - last_raised_alert_ts < 15 minutes:
  suppress
Else:
  raise and update last_raised_alert_ts
```

### 2.3 Operational Metrics

新增 proposed operational metrics，與既有 player-game metrics 並列：

- `operational_precision`
- `operational_recall`
- `operational_f1`
- `operational_alert_count`
- `operational_alerts_per_hour`
- `candidate_alert_count`
- `suppressed_alert_count`
- `suppression_rate`

第一版 recall 定義：

```text
operational_recall = raised true positives / all positive player-games
```

此定義偏保守；若 suppressed positive 其實已被前一個 raised alert cover，第一版仍會算作未召回。後續可新增 episode-level recall 作業務解讀，但不應取代第一版主 KPI。

### 2.4 Reporting Integration

Step 5 training report 第一版應同時保留兩套指標：

- `player_game_*`: 現有 player-game independent alert metrics。
- `operational_*` 或 `player_cooldown_*`: proposed 15 分鐘 cooldown simulation metrics。

第一版不得讓 operational metrics 改變：

- threshold selection
- model artifact schema
- production serving behavior

### 2.5 Future Training Feedback Contract

Offline simulation 完成後，第二階段才建立 training feedback：

- actionable positive player-game: original label 保持 1，正常權重。
- covered positive player-game: original label 保持 1，weight = 0 或低權重。
- negative player-game: original label 保持 0，正常權重。
- bet-level weight: 對同一 player-game 內 bets 做 normalization，例如 `1 / bets_in_player_game`。

此階段不屬第一版交付，但第一版 module 的輸出應足以支援後續產生 `actionable_status` 與 `sample_weight`。

## 3) 模組邊界（Realization Boundaries）

### 3.1 `trainer_hightier/evaluation/player_alert_policy.py`

責任：

- 驗證 candidate frame 欄位與 dtype。
- 套用 deterministic candidate ordering。
- 執行 player-level cooldown simulation。
- 產出 operational metrics。

不負責：

- 訓練模型。
- 讀寫 artifact。
- 修改 production scorer。
- 選 threshold 的全域策略。

### 3.2 `trainer_hightier/05_lgbm_train.py`

責任：

- 在 player-game aggregation 後，建立 candidate frame 或呼叫 helper。
- 在既有 metrics report 中加入 operational metrics。
- 保持既有 player-game threshold picking 不變。

### 3.3 `trainer_hightier/evaluation/metrics_blocks.py`

責任：

- 保留純 array metrics。
- 如需共用 precision/recall/f1 計算，可提供小型 helper。

不建議把 temporal cooldown simulation 放在此檔，因為它不是純 array metric。

## 4) 工作流（Workstreams）

### Workstream A: Candidate Frame

- 擴充 player-game aggregation 輸出，保留 `player_id`、`game_id`、`player_game_score`、`player_game_label`、`alert_ts`、tie-break id。
- 確保空 split、missing timestamp、duplicate tie 的處理 deterministic。
- 明確記錄 `alert_ts` 來自 `payout_complete_dtm`。

### Workstream B: Cooldown Simulation Module

- 實作 `simulate_player_cooldown_alerts(candidates, threshold, cooldown_min)`。
- 驗證必要欄位、null rate、timestamp 可轉換性。
- 對每個 player 做 chronological simulation。
- 回傳包含 `is_candidate`、`is_raised`、`is_suppressed` 的 DataFrame。

### Workstream C: Operational Metrics

- 實作 `operational_metrics_at_threshold(candidates, threshold, cooldown_min, window_hours=None)`。
- metrics 必須能處理：
  - no candidates
  - no positives
  - all candidates suppressed
  - same timestamp tie
- `alerts_per_hour` 的時間分母第一版沿用現有 split/report 可用邏輯；若無可靠分母，先輸出 alert count 與 suppression diagnostics。

### Workstream D: Training Report Integration

- 在 validation / test report 中加入 operational metrics。
- 第一版只報告，不改 threshold picking。
- 報告中保留 current vs proposed 差異，避免使用者誤認 production 已啟用 cooldown。

### Workstream E: Tests

新增 deterministic unit tests，至少覆蓋：

- 同 player `10:00`, `10:08`, `10:15`：`10:08` suppressed，`10:15` allowed。
- 不同 player 同時間互不 suppress。
- 同時間同 player 多 candidate：score 高者優先，tie 由 id 決定。
- no candidate above threshold。
- suppressed positive 對 conservative recall 的影響。

## 5) 里程碑與交付物

### M1: Offline Policy Module Ready

交付物：

- `trainer_hightier/evaluation/player_alert_policy.py`
- Unit tests covering cooldown boundary and deterministic ordering。

### M2: Candidate Frame Integrated

交付物：

- Player-game aggregation 可產出 policy simulation 所需欄位。
- Existing player-game metrics 不回歸。

### M3: Operational Metrics Reported

交付物：

- Training metrics report 同時包含 `player_game_*` 與 `operational_*`。
- Report 中包含 `candidate_alert_count`、`raised_alert_count`、`suppressed_alert_count`、`suppression_rate`。

### M4: Decision Checkpoint

交付物：

- 比較 current player-game metrics 與 proposed operational metrics。
- 決定是否進入下一階段：
  - threshold picking 改用 operational metrics；
  - actionable sample-weight training；
  - production serving cooldown design。

## 6) 風險與緩解

- **風險：offline `payout_complete_dtm` 與未來 production `scored_at` 不一致。**
  - 緩解：第一版明確標記為 offline reference；若 serving 採 `scored_at`，需另做 parity check。

- **風險：conservative recall 低估 host 已 cover 的 suppressed positives。**
  - 緩解：第一版保守呈現；後續補 episode recall 作副指標。

- **風險：同時間 candidate ordering 影響 retained alert。**
  - 緩解：固定 deterministic ordering，並以 unit tests 鎖定。

- **風險：第一版 metrics 被誤用為 production 已上線 cooldown。**
  - 緩解：report 命名使用 `operational_simulated_*` 或 `player_cooldown_simulated_*`，並在文件與報表中標明 simulated。

- **風險：過早改 threshold picking 造成 operating point 改變難以歸因。**
  - 緩解：第一版不改 threshold selection，只做 side-by-side reporting。

## 7) 驗證策略

- Unit tests 驗證 policy mechanics。
- Regression tests 驗證既有 player-game metrics 不變。
- 在現有 train/val/test split 上跑一次 side-by-side report：
  - current player-game precision / recall / alert count
  - simulated operational precision / recall / raised alert count
  - suppression count / suppression rate
- 若 operational metrics 與 player-game metrics 差異顯著，再進入下一份 Working Plan。

## 8) 後續延伸（不屬第一版）

- Threshold picking 改為以 simulated operational precision floor 選 threshold。
- 建立 `actionable_status` 與 bet-level `sample_weight`。
- 使用 out-of-fold predictions 做 hard-negative / missed-positive reweighting。
- 新增 episode-level recall。
- 將 validated cooldown policy 轉成 production serving design。
