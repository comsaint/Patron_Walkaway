# trainer_hightier - Player Alert Suppression Switch Implementation Plan

本文件屬於 **Implementation Plan 層**，承接 `Player Cooldown Offline Evaluation - IMPLEMENTATION_PLAN.md` 與已完成的 offline 15 分鐘 player-level cooldown simulation。  
本文件定義如何將 15 分鐘 suppression 變成 train / serve 共用、可追溯、可回退的 alert policy switch。

本文件不展開 ticket 級工作拆解；後續執行細項應由 Working Plan 承接。

## 0) 背景與目標

Offline evaluation 已顯示 15 分鐘 player-level suppression 在不調整模型的情況下，可讓 alert volume 約下降 19–20%，precision 幾乎不變，conservative recall 小幅下降。

接下來要做的是：

- 將 suppression 變成一個 **共用 train / serve policy switch**。
- Default 啟用 suppression，但保留 backup rollback path。
- Training 永遠輸出 `operational_simulated_*` 指標。
- 第一版只啟用 report / serving suppression；暫不啟用 operational threshold selection 或 actionable sample-weight training。
- Production serving 使用現有 `alerts` 表查最近 raised alert。
- Suppressed candidates 寫入現有 `prediction_log`，避免另建一套不可觀測狀態。

## 1) 已定決策（Decision Log）

| ID | 決策 | 理由 / 邊界 |
|----|------|-------------|
| D-001 | 使用共用 `PlayerAlertPolicyConfig` 管理 train / serve suppression 行為。 | 避免 training 與 serving 分散配置，並讓 artifact 可追溯。 |
| D-002 | `suppression_enabled` default = `True`。 | 目前 offline result 支持 adoption；仍可透過 config 關閉作 backup。 |
| D-003 | 永遠 report `operational_simulated_*`。 | 即使 serving 暫時關閉，也保留 side-by-side visibility。 |
| D-004 | 第一版 `threshold_selection_enabled=False`。 | Threshold 仍用 current player-game metrics，避免一次改 operating point。 |
| D-005 | 第一版 `sample_weight_enabled=False`。 | Actionable / covered sample weighting 屬後續模型優化，不是 production suppression 前置條件。 |
| D-006 | Production suppression 用現有 `alerts` 表查最近 raised alert。 | `state.db.alerts` 已有 `player_id` 與 `ts`，且有 `idx_alerts_player`。 |
| D-007 | Production suppression timestamp 使用 `ts` / `scored_at`。 | Production policy 語意是「host 最近是否收到 alert」，不是 bet event time。 |
| D-008 | Serving switch 為 static config at process start。 | 第一版要求改 config 後重啟 scorer；不做 DB runtime override。 |
| D-009 | Suppressed candidate 記錄到現有 `prediction_log`。 | 便於 debug bundle、API `/predictions`、audit CSV 共用既有觀測通道。 |
| D-010 | Train / serve policy mismatch 允許，但必須 warning。 | Backup switch 需要彈性，但 startup / report 必須顯示差異。 |

## 2) Config Contract

新增共用 dataclass：

```python
@dataclass(frozen=True)
class PlayerAlertPolicyConfig:
    suppression_enabled: bool = True
    cooldown_min: int = ALERT_HORIZON_MIN
    threshold_selection_enabled: bool = False
    sample_weight_enabled: bool = False
```

接入方式：

- `Step5TrainConfig.player_alert_policy: PlayerAlertPolicyConfig`
- `HightierServingConfig.player_alert_policy: PlayerAlertPolicyConfig`

實作原則：

- 不使用 environment variables 控制行為。
- Training 與 serving 共用同一個 dataclass 與 default。
- `suppression_enabled=False` 時：
  - Training 仍可報 `operational_simulated_*`，但 artifact 要記錄 policy disabled。
  - Serving 不 suppress，維持 current player-game alert 行為。
- `threshold_selection_enabled=True` 與 `sample_weight_enabled=True` 為後續擴充，第一版保留 config 欄位但不啟用行為。

## 3) Training Behavior

### 3.1 Always-on operational reporting

Step 5 應永遠輸出：

- `*_operational_simulated_precision`
- `*_operational_simulated_recall`
- `*_operational_simulated_alerts`
- `*_operational_simulated_candidate_alerts`
- `*_operational_simulated_suppressed_alerts`
- `*_operational_simulated_suppression_rate`

### 3.2 First-version switch behavior

第一版中：

- `suppression_enabled` 記錄到 report / artifact。
- `threshold_selection_enabled=False`：threshold picking 仍走 current player-game metrics。
- `sample_weight_enabled=False`：training labels / weights 不改。

### 3.3 Artifact metadata

`training_metrics.json` 與 `model.pkl` bundle metadata 應記錄：

- `player_alert_policy_suppression_enabled`
- `player_alert_policy_cooldown_min`
- `player_alert_policy_threshold_selection_enabled`
- `player_alert_policy_sample_weight_enabled`
- `player_alert_policy_train_alert_ts_source`
- `player_alert_policy_operational_metrics_reported`

建議值：

```text
player_alert_policy_train_alert_ts_source = payout_complete_dtm
player_alert_policy_operational_metrics_reported = true
```

## 4) Serving Behavior

### 4.1 Serving switch

在 scorer cycle 中：

1. 正常計算 bet-level scores。
2. 聚合到 one alert row per `player_id + game_id`。
3. 若 `player_alert_policy.suppression_enabled=False`：
   - 直接寫入 current player-game alerts。
4. 若 `player_alert_policy.suppression_enabled=True`：
   - 對 candidate alerts 套用 player-level 15 分鐘 suppression。
   - 只將 raised alerts 寫入 `state.db.alerts`。
   - suppressed candidates 不寫入 `alerts`，但要寫入 / 更新 `prediction_log` audit 欄位。

### 4.2 State source

Production suppression 查現有 `alerts` 表：

```sql
SELECT MAX(ts)
FROM alerts
WHERE player_id = ?
```

注意：

- `ts` / `scored_at` 是 production cooldown 的 anchor。
- Offline evaluation 使用 `payout_complete_dtm`，兩者差異要在 report 與 startup log 標明。
- 查詢應只看 raised alerts；因為 suppressed candidates 不寫入 `alerts`，自然不會延長 cooldown。

### 4.3 Same-cycle ordering

同一 scoring cycle 內，candidate alerts 仍需 deterministic：

```text
player_id ASC, ts/scored_at ASC, player_game_score DESC, bet_id ASC
```

但同一 cycle 的 `ts/scored_at` 通常相同，因此同 player 同 cycle 多個 candidate 時，實際 retained row 會由 score / bet_id tie-break 決定。

## 5) Prediction Log Audit Contract

現有 `prediction_log` 已有：

- `is_alert`
- `threshold`
- `score`
- `margin`
- `scored_at`
- `bet_ts`
- `player_id`
- `game_id`

因此不要新增重複的 `is_alert_candidate` 欄位。

但現有 `is_alert` 的語意是 bet-level audit flag（目前為 `margin >= 0` 且 rated），不是 player-game raised / suppressed decision。為避免語意混淆，新增欄位採 `alert_policy_*` 前綴：

| 欄位 | 型別 | 語意 |
|------|------|------|
| `alert_policy_candidate` | INTEGER | 該 scored row 是否屬於 player-game candidate representative row。 |
| `alert_policy_raised` | INTEGER | 該 candidate 是否最後寫入 `state.db.alerts`。 |
| `alert_policy_suppressed` | INTEGER | 該 candidate 是否因 player cooldown 被 suppress。 |
| `alert_policy_suppression_reason` | TEXT | 例如 `player_cooldown_15m`；非 suppressed 為 NULL。 |
| `alert_policy_cooldown_min` | INTEGER | 本輪 policy cooldown 分鐘數。 |
| `alert_policy_last_raised_ts` | TEXT | 同 player 前一個 raised alert 的 `ts`。 |
| `alert_policy_decision_ts` | TEXT | 本 candidate 使用的 production decision timestamp，通常等於 `scored_at`。 |

欄位設計說明：

- 不覆蓋既有 `is_alert`，避免破壞 API / audit 相容性。
- `alert_policy_candidate` 只應為 player-game representative candidate row 標 1；同一 player-game 內其他 scored bets 可為 0 / NULL。
- `alert_policy_raised=1` 與 `alert_policy_suppressed=1` 不可同時為 1。
- Below-threshold rows 不應被標為 `alert_policy_candidate=1`。

### 5.1 Prediction log write timing

目前 scorer 先寫 `prediction_log`，再建 `alerts`。要支援 suppression audit 有兩個可行方式：

1. **Preferred：先決策，再寫 prediction_log**
   - 將 player-game candidate / suppression decision 先算出。
   - `append_hightier_prediction_log(...)` 接收 optional policy decision map。
   - 單次 insert 即包含 policy 欄位。

2. **Fallback：先 insert，再 update**
   - 先維持現有 insert。
   - suppression decision 後用 `bet_id` update policy 欄位。

建議採 preferred，避免 update race / partial write；但 working plan 可保留 fallback 作為小步重構選項。

## 6) Train / Serve Mismatch Handling

Model artifact 會記錄 training policy；serving startup / bundle load 時比較：

- artifact `player_alert_policy_suppression_enabled`
- serving config `player_alert_policy.suppression_enabled`
- artifact `cooldown_min`
- serving config `cooldown_min`

若 mismatch：

- 不 hard fail。
- 輸出 structured warning log。
- 在 prediction log / debug bundle metadata 中保留 serving policy。

範例：

```text
[hightier_scorer] player_alert_policy_mismatch:
artifact suppression_enabled=True cooldown_min=15;
serving suppression_enabled=False cooldown_min=15
```

## 7) Module Boundaries

### 7.1 `trainer_hightier.config`

負責：

- 定義 `PlayerAlertPolicyConfig`。
- 將 config 接入 `Step5TrainConfig` 與 `HightierServingConfig`。

### 7.2 `trainer_hightier.05_lgbm_train`

負責：

- 持續輸出 operational simulated metrics。
- 寫入 policy metadata 到 `training_metrics.json` / model bundle。
- 第一版不改 threshold selection / sample weight。

### 7.3 `trainer_hightier.serving.scorer`

負責：

- 在 `_build_player_game_alert_frame` 後套用 serving suppression。
- 使用 `state.db.alerts` 查最近 raised alert。
- 寫入 raised alerts。
- 傳遞 policy decision 給 prediction log。

### 7.4 `trainer_hightier.serving.prediction_log`

負責：

- migration 新增 `alert_policy_*` 欄位。
- `append_hightier_prediction_log(...)` 支援 optional policy decisions。
- 保持既有欄位與 API 相容。

### 7.5 `trainer_hightier.evaluation.player_alert_policy`

負責：

- 保留 offline simulation / metrics。
- 可抽取純 decision helper 供 serving 重用，但不得讓 serving 依賴 training-only artifact。

## 8) Validation Strategy

### 8.1 Unit tests

新增 / 擴充測試：

- Config default：suppression enabled by default。
- Training report：policy metadata 存在且 operational metrics 永遠輸出。
- Serving disabled：candidate alerts 全部照 current player-game 行為寫入。
- Serving enabled：同 player 15 分鐘內 second candidate 不寫入 `alerts`。
- Boundary：`<15m` suppress，`>=15m` allow。
- Prediction log：suppressed candidate 有 `alert_policy_suppressed=1` 與 reason。
- Prediction log：raised candidate 有 `alert_policy_raised=1`。
- Mismatch：artifact / serving policy 不一致時 warning，但不 fail。

### 8.2 Integration / smoke

- 使用小型 staged batch 跑 scorer cycle。
- 檢查：
  - `state.db.alerts` 只含 raised alerts。
  - `prediction_log` 同時包含 raised / suppressed audit rows。
  - 關閉 switch 後同一批資料恢復 current player-game alert count。

## 9) Rollout Strategy

因 default enabled，實作完成後仍建議 staged rollout：

1. Local unit / smoke tests。
2. Production-like deploy E2E gate。
3. Dry-run 觀察 `prediction_log.alert_policy_*` 與 `alerts` volume。
4. 若 alert volume / precision 符合 offline 預期，保持 enabled。
5. 若需 rollback，改 config `suppression_enabled=False` 並重啟 scorer。

## 10) Open Follow-up（不屬第一版）

- `threshold_selection_enabled=True`：改用 operational metrics 選 threshold。
- `sample_weight_enabled=True`：引入 actionable / covered sample weights。
- 建立 separate `suppressed_alerts` table（若 prediction log audit 不夠查）。
- Runtime DB override（若未來需要免重啟切換）。
