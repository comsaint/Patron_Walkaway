# trainer_hightier - Player DQ Working Plan（執行計畫）

本文件屬於 **Working / execution plan 層**，承接：

- Implementation Plan：`trainer_hightier/doc/implementation/active/Player DQ - IMPLEMENTATION_PLAN.md`

內容包含 player-level DQ v1 的可執行任務拆解、實作順序、DoD、cache 驗收與 rollback 檢查。**不重新定義** hard / review 規則；若規則需要改動，應先更新 Implementation Plan。

> **狀態（2026-06-14）**：active；待 implementation plan sign-off 後進入 code slice。

---

## 1) 範圍與護欄

### 1.1 In Scope

- 新增 `PlayerDqConfig`，預設 `enabled=True`。
- 新增 player DQ artifact materializer：
  - `player_dq_flags.parquet`
  - `player_dq_hard_exclude.parquet`
  - sidecar manifest / cache hit logic
- Hard exclude 接入 entity set v1 與 legacy ADT segment fallback。
- Run metrics / report metrics 補齊 player DQ 計數與 cache hit。
- Focused tests 覆蓋 rules、cache fingerprint、rollback、projection anti-join。

### 1.2 Out Of Scope

- Multi-table 同秒規則。
- 把 Review flag join 回 `training_set.parquet`。
- 把 Review flag 放入 model features。
- Short PIT shard-level partial invalidation。
- 改動 bet base clean SQL 或 bet base cache key。

### 1.3 已鎖定決策

| ID | 決策 |
|----|------|
| PDQ-001 | v1 hard exclude：cleaned `casino_player_id LIKE '4444%'` |
| PDQ-002 | v1 hard exclude：`distinct_game_id_per_hour > 240` 或 `distinct_game_id_per_day > 2880` |
| PDQ-003 | v1 review flag：`distinct_game_id_per_hour > 120` 或 `distinct_game_id_per_day > 1440`，但未達 hard |
| PDQ-004 | Threshold 使用嚴格 `>`，不使用 `>=` |
| PDQ-005 | Pace 使用 distinct `game_id`，bucket 時間使用 `payout_complete_dtm` |
| PDQ-006 | Hard exclude 粒度為 `player_id`，不是 `canonical_id` |
| PDQ-007 | Review 玩家留在訓練集，只輸出 artifact / metrics，不進 features |
| PDQ-008 | `PlayerDqConfig.enabled=True` 為預設；`False` 作 rollback |

---

## 2) Work Breakdown

### Phase A — Config 與 Policy Fingerprint

| Task | 內容 | DoD |
|------|------|-----|
| A1 | 在 `trainer_hightier/config.py` 新增 `PlayerDqConfig` | defaults 符合 PDQ-001 至 PDQ-008 |
| A2 | 將 `PlayerDqConfig` 接入 `HighTierTrainArgs` | trainer args 可取得 `args.player_dq` |
| A3 | 定義 hard policy fingerprint 與 flags policy fingerprint | review threshold 不影響 entity set fingerprint |
| A4 | 補 config / fingerprint focused tests | hard threshold 改動會改 hard fp；review threshold 改動不改 hard fp |

### Phase B — Player DQ Artifact Materializer

| Task | 內容 | DoD |
|------|------|-----|
| B1 | 新增 `trainer_hightier/utils/player_dq.py` | 函數含 type annotations 與 docstrings |
| B2 | 實作 pace metrics SQL | 輸出每個 `player_id` 的 max games/hour、max games/day |
| B3 | 實作 `4444` known-test mapping | 從 canonical mapping cleaned `casino_player_id` 判斷 |
| B4 | 輸出 `player_dq_flags.parquet` | 含 hard / review flags、reason 欄位與 max pace metrics |
| B5 | 輸出 `player_dq_hard_exclude.parquet` | 僅含 hard-excluded `player_id` |
| B6 | 實作 sidecar manifest / cache hit | manifest 綁 bet base fingerprint、canonical mapping hash、policy fp、module code fp |
| B7 | 補 materializer unit tests | 覆蓋 clean、`4444`、hard pace、review pace、review 非 hard exclude |

### Phase C — Entity Set 與 Legacy Segment 接入

| Task | 內容 | DoD |
|------|------|-----|
| C1 | 更新 `trainer_hightier/utils/entity_set_v1.py` projection SQL | 可選 anti-join `player_dq_hard_exclude.parquet` |
| C2 | 更新 entity set manifest / cache hit | manifest 比對 hard-exclude policy fp |
| C3 | 更新 legacy `segment_cleaned_bet_from_base_parquet` | legacy fallback 也套用同一 hard exclude |
| C4 | 更新 legacy policy fingerprint | enabled hard DQ 會讓 legacy segment cache miss；disabled 不改既有行為 |
| C5 | 補 projection tests | hard players 被排除，review players 保留 |

### Phase D — Trainer Orchestration

| Task | 內容 | DoD |
|------|------|-----|
| D1 | 在 `trainer_hightier/trainer.py` Step 2b base clean 後呼叫 player DQ | 只在 base bet 與 canonical mapping 可用時執行 |
| D2 | 將 hard exclude path / policy fp 傳入 entity set path | entity set v1 正常套用 anti-join |
| D3 | 將 hard exclude path / policy fp 傳入 legacy segment path | `use_entity_set_v1=False` 時行為一致 |
| D4 | 寫 run metrics | 至少包含 cache hit、hard count、review count、known-test count |
| D5 | 實作 `enabled=False` rollback path | 不物化 DQ、不 anti-join、不改 entity set fp |

### Phase E — Tests 與 Cache 驗收

| Task | 內容 | DoD |
|------|------|-----|
| E1 | 跑 focused unit tests | 新增 tests 全部通過 |
| E2 | 跑 entity-set cache fingerprint tests | review policy 不 bust entity set；hard policy 會 bust |
| E3 | 跑 small pipeline smoke | 首次預期 player DQ / entity set miss |
| E4 | 重跑 same-source smoke | player DQ / entity set 應 hit |
| E5 | 驗證 row impact | hard exclude row 數接近調查結果，Review 玩家仍保留 |

---

## 3) Execution Sequence

1. Phase A：先固定 config 與 policy fingerprint，避免後續 cache key 反覆改。
2. Phase B：完成 player DQ artifact，先獨立測試，不接 pipeline。
3. Phase C：接 entity set / legacy segment anti-join，並測 cache miss 邊界。
4. Phase D：接 trainer orchestration 與 metrics。
5. Phase E：跑 focused tests，再跑 cache smoke。

---

## 4) Definition Of Done

- `PlayerDqConfig.enabled=True` 下：
  - hard exclude 包含 `4444` known-test player IDs 與 pace hard players。
  - review pace players 不出現在 hard exclude parquet。
  - entity set / legacy segment 輸出不含 hard-excluded `player_id` rows。
  - Review players 仍留在 cleaned training bet universe。
- `PlayerDqConfig.enabled=False` 下：
  - 不套用 anti-join。
  - 不改變現有 entity set policy fingerprint 行為。
- bet base cache 保持原狀：
  - 不新增 player DQ fingerprint。
  - 不改 base clean SQL。
- Metrics 可觀察：
  - `player_dq_flags_cache_hit`
  - `player_dq_hard_player_count`
  - `player_dq_review_player_count`
  - `player_dq_known_test_player_count`
  - `player_dq_hard_exclude_path`

---

## 5) Validation Commands

建議實作後依序執行：

```bash
walkaway/Scripts/python.exe -m pytest trainer_hightier/tests/test_player_dq.py
walkaway/Scripts/python.exe -m pytest trainer_hightier/tests/test_entity_set_v1.py
walkaway/Scripts/python.exe -m pytest trainer_hightier/tests/test_cache_invalidation_v1.py
```

若 focused tests 通過，再跑 cache smoke：

```bash
walkaway/Scripts/python.exe -m trainer_hightier.trainer --skip-optuna
```

同一 source / same policy 重跑一次，確認 DQ 與 entity set cache hit。

---

## 6) Execution Risks

| Risk | Trigger | Mitigation |
|------|---------|------------|
| Review threshold 造成不必要 downstream cache miss | Review policy 被放入 entity set fp | hard policy fp 與 flags policy fp 分離 |
| Hard exclude 誤殺 canonical 其他 player | 使用 `canonical_id` exclude | v1 只用 `player_id` |
| Base cache 被 bust | 在 base preprocess 加 DQ | DQ 僅在 base output 後、entity projection 前執行 |
| Short PIT 首次全月 miss | entity set fp 改變 | 已接受；v2 再做 shard-level invalidation |
| Offline-only flag 進 model | Review flag join training features | v1 僅輸出 artifact / metrics |

---

## 7) Open Follow-Up（不阻擋 v1）

- 是否需要定期輸出 Review CSV 給人工調查流程。
- 若首次 short PIT rebuild 太慢，是否追加 v2 shard-level invalidation。
- 若 business 後續確認 Review players 也應排除，需先更新 Implementation Plan，再改 hard policy fingerprint。
