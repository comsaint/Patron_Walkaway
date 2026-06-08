# trainer_hightier - Player Cooldown Offline Evaluation Working Plan

本文件屬於 **Working / Execution Plan 層**，承接：

- Implementation Plan：[`Player Cooldown Offline Evaluation - IMPLEMENTATION_PLAN.md`](../../implementation/active/Player%20Cooldown%20Offline%20Evaluation%20-%20IMPLEMENTATION_PLAN.md)

內容僅包含 offline player-level 15 分鐘 cooldown simulation 的可執行任務拆解、順序、DoD 與驗證步驟。

---

## 1) 範圍與護欄

### 1.1 In scope

- 新增 `trainer_hightier/evaluation/player_alert_policy.py`
- 擴充 Step 5 player-game aggregation，產出 cooldown simulation 所需 candidate frame
- 在 `training_metrics.json` 並列輸出 current player-game metrics 與 `operational_simulated_*` metrics
- 新增 deterministic unit tests

### 1.2 Out of scope

- Production serving cooldown
- Threshold picking 改用 operational metrics
- Actionable sample-weight training
- Episode-level recall 作為主 KPI

### 1.3 已鎖定決策（對齊 Implementation Plan）

| 項目 | 決策 |
|------|------|
| Policy 性質 | Offline proposed production behavior reference |
| Module | `trainer_hightier/evaluation/player_alert_policy.py` |
| Cooldown | 15 分鐘；`< 15 min` suppress，`>= 15 min` allow |
| `alert_ts` | `payout_complete_dtm`（offline simulation） |
| 排序 | `alert_ts ASC, score DESC, bet_id ASC` |
| Recall | Conservative：`raised TP / all positive player-games` |
| Threshold picking | 第一版不變 |

---

## 2) 任務拆解

### Wave 1 — Policy module（M1）

| # | 任務 | DoD |
|---|------|-----|
| W1.1 | 新增 `player_alert_policy.py` | 含 `simulate_player_cooldown_alerts`、`operational_simulated_metrics_block` |
| W1.2 | 欄位驗證與錯誤訊息 | 缺欄位時 raise `ValueError`，訊息含實際/期望欄位 |
| W1.3 | Unit tests | `10:00/10:08/10:15`、不同 player、同時間 tie-break、空 candidate、conservative recall |

### Wave 2 — Candidate frame（M2）

| # | 任務 | DoD |
|---|------|-----|
| W2.1 | 擴充 `aggregate_bets_to_player_game` | 回傳 `candidates` DataFrame（含 `alert_ts`, `bet_id`） |
| W2.2 | `_load_split_frame` 加入 `bet_id` | split parquet schema gate 通過 |
| W2.3 | Representative bet 語意 | 與 scorer `_build_player_game_alert_frame` 對齊：max score → earliest `payout_complete_dtm` → min `bet_id` |
| W2.4 | Regression | 既有 `y_true` / `scores` 行為不變 |

### Wave 3 — Report integration（M3）

| # | 任務 | DoD |
|---|------|-----|
| W3.1 | val/test/train 加入 `operational_simulated_*` | 與 `*_player_game_*` 並列 |
| W3.2 | Diagnostics | `candidate_alert_count`, `raised_alert_count`, `suppressed_alert_count`, `suppression_rate` |
| W3.3 | Policy metadata | report 標明 `player_cooldown_simulated=true`, `cooldown_min=15` |
| W3.4 | Threshold picking | 仍用 player-game metrics，不變 |

### Wave 4 — Verification（M4 checkpoint）

| # | 任務 | DoD |
|---|------|-----|
| W4.1 | `pytest trainer_hightier/tests/test_player_alert_policy.py` | 全過 |
| W4.2 | `pytest trainer_hightier/tests/test_step5_lgbm_train.py` | 全過、無 regression |
| W4.3 | Side-by-side 解讀 | 比較 player-game vs operational_simulated precision/recall/alerts |

---

## 3) 建議執行順序

```text
W1 policy module + tests
  → W2 candidate frame extension
    → W3 report integration
      → W4 verification + decision checkpoint
```

---

## 4) 驗證指令

```bash
pytest trainer_hightier/tests/test_player_alert_policy.py -q
pytest trainer_hightier/tests/test_step5_lgbm_train.py -q
```

---

## 5) Decision checkpoint（Wave 4 後）

比較：

- `*_player_game_precision` vs `*_operational_simulated_precision`
- `*_player_game_recall` vs `*_operational_simulated_recall`
- `*_player_game_alerts` vs `*_operational_simulated_alerts`
- `*_operational_simulated_suppression_rate`

若差異顯著且方向合理，下一輪 working plan 可選：

1. Operational threshold picking
2. Actionable sample-weight training
3. Production serving cooldown design

---

## 6) 風險與執行注意

- `payout_complete_dtm` 與未來 production `scored_at` 可能不一致；report 必須標明 simulated。
- Conservative recall 會低估已被第一個 alert cover 的 suppressed positives。
- 第一版不得讓 operational metrics 影響 threshold selection 或 model artifact。
