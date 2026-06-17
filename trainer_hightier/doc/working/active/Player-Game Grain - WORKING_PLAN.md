# trainer_hightier - Player-Game Grain Working Plan

本文件屬於 **Working / Execution Plan 層**，承接：

- Implementation Plan：[`Player-Game Grain - IMPLEMENTATION_PLAN.md`](../../implementation/active/Player-Game%20Grain%20-%20IMPLEMENTATION_PLAN.md)

內容僅包含 true player-game grain migration 的可執行任務拆解、順序、DoD 與驗證步驟。  
若本文件與 Implementation Plan 衝突，先更新上層 Implementation Plan，再執行。

---

## 1) 範圍與護欄

### 1.1 In Scope

- Offline player-game materializer prototype：從 cleaned / Step-3 bet rows 聚合 `player_id + game_id`。
- DQ audit：`payout_complete_dtm` span、`prediction_visible_ts_cf` span、main/side composition parity。
- Offline experiment：current `per-bet + top3_mean` baseline vs true player-game model。
- Serving ready queue dry-run：pending / re-fetch / defer / completed idempotency，不先切 production 行為。
- Shadow-capable scorer implementation：通過 offline gate 後建 PG scorer path；**不改 production alert route**，直到 Wave 6 gate review。
- Baseline-parity shadow path（Phase A）：非 `txn__*` 從 `representative_bet_id` 帶入；`txn__*` 以 `player_game_ready_ts` PIT。Full short/mid/feast PIT re-materialization 延後。

### 1.2 Out Of Scope

- 不改 `walkaway_label` 定義。
- 不調整 `SCORER_POLL_INTERVAL_SECONDS`。
- 不做 prefix / in-game alert。
- 不把 alert 聚合到 player-day / player-session。
- 不把 `top3_mean` 立即移除；先保留 baseline / rollback path。
- Full short/mid/feast PIT re-materialization at `player_game_ready_ts`（延後至 shadow gate 通過後；見 W5.2b）。

### 1.3 已鎖定決策

| 項目 | 決策 |
|------|------|
| Grain | `player_id + game_id` |
| Ready time | `player_game_ready_ts = max(prediction_visible_ts_cf)` |
| Serving holdback | `first_seen_prediction_visible_ts_cf + SCORER_POLL_INTERVAL_SECONDS` |
| Accepted latency | Worst approx +75s vs old first-visible scoring |
| Label | `max(walkaway_label)` within player-game |
| DQ | `payout_complete_dtm` span > 60s or `prediction_visible_ts_cf` span > 45s → warn/exclude |
| Serving migration gate | **Baseline-parity fair test**（42 MVP 特徵）；sparse composition arms 僅診斷 |
| Phase A features | Representative bet + txn_pg at ready_ts（對齊 offline B1） |

### 1.4 Current Status（2026-06-16）

| Wave | 狀態 | 備註 |
|------|------|------|
| W1 | Done | Materializer、DQ、DuckDB scale path、`test_player_game_grain_materializer.py` |
| W2 | Done（gate pass） | Sparse arms fail；**baseline_parity**（42 feat）pass @ K=3149 |
| W3 | Partial | PG model metadata 已有；registry / doc sync 待補 |
| W4 | Done | Ready-queue dry-run、`test_player_game_ready_queue.py`（7 pass） |
| W5 | Done | Shadow scorer Phase A、`test_player_game_shadow_scorer.py` |
| W6 | **Next** | Shadow comparison + gate review → production switch |

業務已確認：接受同一 player-game 全部 bets 可見後再 score（+45s holdback）。

Staging 啟用 shadow stack：

```python
player_game_ready_queue_dry_run_enabled = True
player_game_shadow_scoring_enabled = True
# bundle: out/player_game_w2/player_game_baseline_parity
```

---

## 2) 任務拆解

### Wave 1 — Offline DQ And Materializer

| ID | Task | DoD |
|----|------|-----|
| W1.1 | 實作 player-game aggregation helper | 輸入 bet rows，輸出一列一個 `(player_id, game_id)`；含 `player_game_ready_ts`, `player_game_payout_complete_dtm`, `player_game_label` |
| W1.2 | Main / side composition aggregation | `pg__bet_count`, `pg__main_bet_count`, `pg__side_bet_count`, wager sums / ratios 可重算 |
| W1.3 | Observation DQ audit | 產出 excluded counts、sample keys、span distribution；DQ 不 silent fallback |
| W1.4 | PIT cutoff contract | Materialized output 明確含 `player_game_ready_ts`，供 short/mid/txn join |
| W1.5 | Unit tests | Same pcd + late side bet、pcd span DQ、pv span DQ、main/side wager parity |

**主要檔案候選：**

- `trainer_hightier/player_game_grain.py`：materializer / train helper
- `trainer_hightier/feature_experiment/`：offline experiment harness
- `trainer_hightier/tests/test_player_game_grain_materializer.py`

---

### Wave 2 — Offline Experiment

| ID | Task | DoD |
|----|------|-----|
| W2.1 | 建立 baseline comparison harness | 同一 split 比較 current `top3_mean` 與 true player-game model |
| W2.2 | 訓練 player-game model without txn | 產出 metrics、feature importance、DQ summary（診斷用） |
| W2.3 | 訓練 player-game model with txn_pg | Txn 以 `player_id + player_game_ready_ts` PIT 聚合（診斷用） |
| W2.4 | Leakage ablation | 至少比較含 / 不含 settlement-heavy 欄位 |
| W2.5 | Decision report | 固定 capacity 下列出 precision / recall / alert volume / latency tradeoff |
| W2.6 | Baseline-parity fair test | 42 MVP 特徵：非 txn 從 representative bet，`txn__*` 用 `player_game_ready_ts`；`decision_baseline_parity` 寫入 report |

**Checkpoint：** Serving migration gate 以 **W2.6 baseline-parity fair test** 為準。W2.2–W2.4 sparse / composition-only arms **僅作診斷**，不作 go/no-go。若 baseline-parity 未勝過或未持平 current baseline，停止 serving migration，只保留 materializer / audit 結果。

**結果（2026-06）：** baseline-parity pass（val precision @ K=3149：58.8% vs baseline 57.9%）。

---

### Wave 3 — Contract Hardening

| ID | Task | DoD |
|----|------|-----|
| W3.1 | Training schema gate | Step 5 前 hard-require `player_id`, `game_id`, `prediction_visible_ts_cf`, label, main/side columns |
| W3.2 | Feature registry update | `pg__*` 欄位加入 candidate registry；source / time_horizon / grain 清楚標示 |
| W3.3 | Model artifact metadata | `training_metrics.json` 寫入 `model_grain=player_game`, `ready_time_column=player_game_ready_ts` |
| W3.4 | Backward compatibility notes | `top3_mean` artifact 標為 legacy aggregation baseline |
| W3.5 | Documentation sync | Implementation Plan、Working Plan、README links 對齊實作狀態 |

---

### Wave 4 — Serving Ready Queue Dry-Run

| ID | Task | DoD |
|----|------|-----|
| W4.1 | State DB schema proposal | Pending / completed player-game tables 欄位與 idempotency key 明確 |
| W4.2 | Re-fetch query prototype | Given `(player_id, game_id)` 可抓完整 currently visible player-game bets |
| W4.3 | Defer policy | 若 re-fetch 後 `max(prediction_visible_ts_cf) > now`，放回 pending；attempt count 可觀測 |
| W4.4 | Dry-run metrics | 不改 production alert，只記錄 pending age、ready lag、late-after-score hypothetical |
| W4.5 | Cursor policy validation | Incremental cursor 可前進，但 pending/completed state 不漏 score identity |

**Checkpoint：** Dry-run 必須證明 +75s latency budget 成立，且 late-after-score rate 接近 DQ 級別，才可進 Wave 5。

---

### Wave 5 — Shadow-Capable Scorer Implementation

不改 production alert route；產出可並行 shadow 的 PG scorer path。

| ID | Task | DoD |
|----|------|-----|
| W5.1 | Scorer model input grain switch | `_build_staged_features` 前先 consolidate player-game，model 一個 player-game score 一次；**shadow-only route** |
| W5.2a | Minimal parity feature path | 非 `txn__*` 從 `representative_bet_id` 對應 bet 帶入；`txn__*` 用 `player_game_ready_ts` PIT（對齊 offline B1） |
| W5.2b | Full PIT alignment *(deferred)* | short/mid/txn 全部 `player_game_ready_ts`；short-term 排除本 player-game bets；shadow gate 通過後再做 |
| W5.3 | Alert / prediction log update | Shadow log 以 `(player_id, game_id)` 為主；bet ids 只作 audit |
| W5.4 | Flight recorder support | Capture pending, re-fetch, aggregate, scored player-game stages |
| W5.5 | Regression tests | Scorer tests 更新為 one player-game one score；legacy path test 保留 |

---

### Wave 6 — Shadow Gate, Rollout And Legacy Baseline

| ID | Task | DoD |
|----|------|-----|
| W6.1 | Shadow comparison run | 在 W5 shadow path 就緒後，同一 live window 並行 legacy `top3_mean` 與 player-game score |
| W6.2 | Gate review | Precision / recall / alert volume / latency / completeness / train-serve parity 過門檻 |
| W6.3 | Production switch | Bundle metadata 與 scorer route 切到 player-game model |
| W6.4 | Rollback path | 可回 legacy `per-bet + top3_mean` bundle |
| W6.5 | Archive / cleanup | 舊文件標註 legacy，不刪除可追溯 baseline |

---

## 3) 建議執行順序

```text
W1 offline materializer + DQ                    [done]
  -> W2 offline experiment + baseline-parity      [done, gate pass]
    -> W3 contract hardening                    [partial]
      -> W4 serving ready-queue dry-run         [done]
        -> W5 shadow-capable scorer (W5.2a)     [done, shadow-only]
          -> W6 shadow comparison + gate review [next]
            -> W6 production switch / W5.2b full PIT (later)
```

---

## 4) 驗證指令草案

具體 test file 會在 W1 / W4 落地後固定。初版建議：

```bash
pytest trainer_hightier/tests/test_step5_lgbm_train.py -q
pytest trainer_hightier/tests/test_scorer_v2_feast.py -q
pytest trainer_hightier/tests/test_scoring_context_contract.py -q
```

新增測試建議：

```bash
pytest trainer_hightier/tests/test_player_game_grain_materializer.py -q
pytest trainer_hightier/tests/test_player_game_ready_queue.py -q
pytest trainer_hightier/tests/test_player_game_shadow_scorer.py -q
```

W6 staging shadow gate report（dry-run + shadow 跑一段 live window 後）：

```bash
python -m trainer_hightier.serving.player_game_shadow_scorer \
  --state-db trainer_hightier/local_state/state.db \
  --output-json out/player_game_w2/shadow_gate_report.json
```

Staging 啟用（`config.py` → `HightierServingConfig`）：

```python
player_game_ready_queue_dry_run_enabled = True
player_game_shadow_scoring_enabled = True
```

---

## 5) Definition Of Done

- Offline player-game materializer 能 deterministic 重建 row contract。
- DQ audit 可解釋所有 excluded player-games。
- **Baseline-parity offline gate pass**（42 MVP 特徵，fixed capacity vs `top3_mean` baseline）。
- Serving ready queue dry-run 證明 +75s latency budget 與 completeness 假設成立。
- Shadow scorer 可重現 offline B1 特徵邏輯（representative bet + txn_pg）；train-serve parity 在約定 tolerance 內。
- Production switch 後一個 `(player_id, game_id)` 僅產生一次 business score。
- Phase B（full PIT）：training / serving 均以 `player_game_ready_ts` 作所有 feature family 的 PIT cutoff。

---

## 6) 風險與執行注意

- Serving 不知道 late side bet 一定會來；完整性依賴 bounded holdback + re-fetch，不可寫成 oracle 邏輯。
- `__etl_insert_Dtm_synthetic` 不可作 group cutoff；只能 audit span。
- Settlement-heavy features 可能成為 label proxy；必須保留 leakage ablation。
- Pending queue 與 incremental cursor 是兩個狀態，不可用 cursor 取代 completed player-game idempotency。
- Phase A（representative bet 特徵）與 Phase B（full PG PIT）存在 train-serve skew 風險；shadow gate 必須驗證 parity。
- 若 future data 出現 `prediction_visible_ts_cf` spread > 45s 的非 DQ pattern，需回上層 Implementation Plan 重審 holdback policy。
