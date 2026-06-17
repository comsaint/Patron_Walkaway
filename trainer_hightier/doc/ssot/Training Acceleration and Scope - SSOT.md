# trainer_hightier - 訓練加速與範圍 SSOT

本文件屬於 **SSOT 層**，定義 `trainer_hightier` 訓練加速、訓練 target scope、sampling、feature source coverage 與 downstream cache reuse 的治理真相。

本文件不包含 ticket 級任務拆解或實作順序；實作策略應由 implementation plan 承接，具體 execution plan 應另建 working plan。

## 1) 目標

在不破壞 train-serve parity、feature contract 與 PIT correctness 的前提下，縮短 `trainer_hightier` 日常訓練迭代時間。

核心目標：

- 用 recent target scope 減少進入 Step 3 / Step 3.5 / Step 4 / Step 5 的 target rows。
- 用 train-only negative downsampling 降低 Step 5 訓練成本。
- 用 optional feature screening 降低模型訓練欄位數與候選實驗成本。
- 用 cache 分層讓 expensive primitives 可跨不同 target horizon / sampling policy 重用。
- 用 indexed short-PIT replay（WP-10）加速 Step 3.5 在 cache miss / cold-build 時的 short-term PIT 物化。
- 明確區分 target scope 與 feature source scope，避免為了加速而截斷 short / mid / slow features 所需歷史。

## 2) 範圍

### 納入範圍

- Training target horizon policy：最近 N 個完整月 + current partial month。
- 所選 target months 的 full-month source completeness checks。
- Train / validation / test split 的 recency 與 uncensored-row rule。
- Train-only negative downsampling policy。
- Optional target-row sampling for experiments。
- Optional feature screening / top-k feature selection policy。
- Downstream cache policy for assembled dataset, split, sampled train, feature selection, and model artifacts。
- Feature family source coverage requirements for short / mid / slow / labels。
- Step 3.5 short-term PIT materialization path selection：month-sharded bounded DuckDB vs indexed replay prototype（WP-10 gate）。

### 非範圍

- 變更 walkaway label business definition。
- 變更 production scorer runtime contract。
- 變更 short-term PIT bounded semantics（indexed replay 必須與 bounded oracle parity，不得改語意）。
- 以 session table 取代現有 bet-based mid-term feature contract。
- 將 experimental target sampling 預設升級為 production training policy。
- Ticket 級 work breakdown。

## 3) 治理決策

| ID | 決策 | 理由 / 邊界 |
|----|------|-------------|
| TA-001 | 預設 training target horizon 為 `recent_full_months = 3` 加上 current partial month。 | 先以速度與 drift sensitivity 為主；保留 config 可改為 6 / 12 / all history。 |
| TA-002 | `include_current_partial_month = true`。 | Val/test 必須反映最新 data drift；partial month 不應被自動排除。 |
| TA-003 | 所有 split 必須排除 `walkaway_censored = TRUE`。 | Label 不可判定的 row 不得進 train / val / test。 |
| TA-004 | Validation/test 應使用 target scope 下可用的最新 uncensored rows。 | Drift-sensitive model 不應刻意避開 current partial month。 |
| TA-005 | Recent horizon 僅過濾 target training rows。 | 不得因 target horizon 變短而截斷 feature source history。 |
| TA-006 | Feature source scope 由 feature family contracts 負責。 | short / mid / slow / labels 各自有不同 lookback / lookahead 需求。 |
| TA-007 | Expensive primitive caches 與 target horizon、sampling policies 獨立。 | `3m -> 6m` 不應重算 cleaned / labels / Feast / short PIT / mid / slow primitives。 |
| TA-008 | Train-only negative downsampling 預設關閉（`neg_sample_frac = 1.0`）。 | 避免 silent class-prior change；speed runs 可明確設定例如 `0.3`。 |
| TA-009 | Negative downsampling 僅套用於 train split；val/test 維持 full unsampled。 | 評估分佈與 precision / alert-volume 指標不可被 sampling 污染。 |
| TA-010 | Target-row experiment sampling 必須位於 experiment namespace。 | Debug/prototype samples 不得覆蓋 production training artifacts。 |
| TA-011 | Feature screening 為 optional，且必須先產出 manifest，不得直接變更 registry baseline。 | 先用 evidence 驅動 baseline contract 變更。 |
| TA-012 | Short-PIT feature pruning 屬 feature-contract 決策，非純 cache optimization。 | 需由 importance / ablation / deploy contract 同步支撐。 |
| TA-013 | Mid-term acceleration 應保留 bet-based mid-term semantics。 | 不採 session table 替代現有 mid-term baseline contract。 |
| TA-014 | Step 3.5 short-PIT **cold-build / cache-miss** path 在 full-month gate 完全通過，或對已文件化的 non-semantic discrepancy 明確 waiver 後，可採用 **indexed replay prototype**（`emit_opt`）。 | WP-10（202605）量測 10.28× speedup；`fe__*` gate columns 通過，剩餘 54 個 `bet__*` mismatches 已接受為 alias-driven pool-fanout discrepancy，而非 core short-PIT semantic failure。 |
| TA-015 | Indexed replay 僅為 **Step 3.5 primitive miss-path** accelerator。 | 不取代 recent target scope、train-only neg downsampling、feature screening 作為主要訓練加速槓桿；整條 pipeline 仍先縮 target rows。 |
| TA-016 | Indexed replay 整合需以 scorer-aligned gate validation 對 bounded oracle 驗證，`fe__*` columns 視為 hard parity，legacy `bet__*` trial-pack discrepancies 需 explicit waiver。 | Gate 欄位集以 `resolve_scorer_short_pit_prototype_gate_columns()` 為準；full-month decision 預設為 speedup ≥ 3.0× 且 parity + output validation 全過，但可對已 root-cause、低占比、非核心語意的 `bet__*` discrepancy 做文件化 waiver。 |
| TA-017 | Month-sharded short-PIT primitive cache 仍為 reuse layer；indexed replay 為 miss 時的 **fill / rebuild** engine。 | Hit path 仍讀 shard；miss path 用 indexed replay 產出或補 shard，不得每次 full bounded rescoring。 |

## 4) 領域定義

### 4.1 Target Scope

Target scope 定義哪些 bet rows 有資格進入 training dataset。

必要 policy fields：

- `recent_full_months`
- `include_current_partial_month`
- `as_of_date`
- `target_months`
- `target_start_date`
- `target_end_date`
- `data_completeness_mode`

範例：`as_of_date = 2026-06-08` 且 `recent_full_months = 3`：

```text
target_months = 202603, 202604, 202605, 202606_partial
```

Current partial month 納入至可用資料為止，受 label censoring 約束。

### 4.2 Feature Source Scope

Feature source scope 定義 feature family 為計算 target rows 正確值可讀取的歷史 source data。

Feature source scope 不受 `recent_full_months` 控制。

範例：

| Feature family | Source | Required source scope |
|----------------|--------|-----------------------|
| Short-term PIT | cleaned bet | Scorer bounded pool：hot lookback hours + gaming-day floor + per-bet scoring bounds |
| Mid-term daily snapshot | cleaned bet | 至少為 anchor days 前所設定的 mid-term lookback window |
| Slow 180d monthly | cleaned session | Active slow anchor 的 inclusive 180 calendar-day session window |
| Labels | cleaned bet | Forward lookahead / terminal determinability window |

### 4.3 Label Scope

Label scope 定義 target row 的 label 是否可判定。

規則：

- `walkaway_censored = TRUE` 的 rows 排除於 train / val / test 之外。
- Current partial month rows 若 uncensored 可使用。
- Reports 必須依 target month 與 split 揭露 censored row counts。

### 4.4 Training Policy Cache

Training policy cache 涵蓋自 primitives 衍生的 cheap downstream views 與 artifacts：

- assembled training dataset
- train / val / test split
- sampled train split
- selected feature set
- model artifacts

不得與 source / primitive feature caches 混為一談。

## 5) Feature Family Source Contracts

### 5.1 Short-Term PIT

Short-term PIT features 仍為 exact bounded PIT features。

要求：

- Target rows 可由 target scope 縮減。
- Source pool semantics 必須與 scorer bounds 對齊。
- Month-sharded short-PIT primitive cache 在 shard universe 與 columns 涵蓋 requested rows 時，可跨 target scopes 重用。
- 移除 requested short-PIT columns 可使用既有 superset shards。
- 新增 short-PIT primitive columns 僅需對 affected shards 做 recompute 或 fill。

#### 5.1.1 物化路徑（WP-10）

兩種 offline materializers 共享相同 bounded semantics；差異在 execution strategy：

| Path | 角色 | 使用時機 |
|------|------|----------|
| **A. Month-sharded bounded DuckDB**（`materialize_fe_derived_short_term_parquet`） | Oracle / legacy cold-build | Parity baseline；indexed replay 未通過 gate 時的 fallback |
| **B. Indexed replay prototype**（`materialize_short_term_replay_indexed_prototype`，`emit_opt`） | 首選 cold-build / cache-miss engine | Step 3.5 shard miss、新月份或 column fill |

**WP-10 full-month cold-build gate 結果（2026-06-08）**

Harness：`benchmark_indexed_replay_full_month_gate()`，payout month `202605`，gate-16 columns，`emit_opt` checkpoint。

| Evidence | Status | Value |
|----------|--------|-------|
| Target rows | Measured | 3,404,913 |
| Indexed replay wall time | Measured | ~23 min（1,386 s） |
| Replay output validation | Passed | 3,404,913 rows |
| Bounded oracle wall time | Measured | ~4.0 h（14,246 s） |
| Speedup ratio（`bounded / replay`） | Measured | **10.28×** |
| `fe__*` gate parity | Passed | Full-month hard-parity columns 與 bounded oracle 一致 |
| `bet__*` gate parity | Waived | 54 mismatches on `bet__*` w1h columns（0.0016% of rows），全部位於同一 canonical alias cluster |
| Waiver root cause | Confirmed | Indexed full-month replay 載入了 bounded 2000-row batch pool 未納入的 canonical alias player |
| Sample parity（10k / 50k / 100k） | Measured | 全部通過；與同一 `emit_opt` checkpoint 一致 |

**治理（WP-10 於 2026-06-08 關閉）**

- Speedup 超過 3.0× integrate threshold。
- `fe__*` gate columns 視為 Step 3.5 miss-path adoption 的 hard parity bar。
- 54 個 `bet__*` mismatches **以 waiver 接受**，因已追溯至 batch-local bounded pool fanout vs full-month alias fanout，而非 indexed replay core emit logic。
- 此 waiver **不**重新定義 production scorer semantics，**不**宣稱 legacy `bet__*` 1h pack 的 full parity。
- 主要訓練加速槓桿仍為 TA-001–TA-004（target scope）與 TA-008–TA-009（neg downsampling）；indexed replay 解決 cache miss 時 dominant 的 **per-target-row** short-PIT materialization cost。

Reference artifacts：

- `out/replay_benchmark_202605_indexed_full_month_gate16_emit_opt/`
- `out/replay_benchmark_202605_indexed_gate16_emit_opt_scaling_summary.json`

### 5.2 Mid-Term

Mid-term features 仍為 `canonical_id + anchor_gaming_day_event` grain 的 bet-based daily gaming-day snapshots。

要求：

- Source table 對現有 baseline mid-term features 仍為 cleaned bet。
- Acceleration 應優先 reusable daily / anchor-month cache 或 bet daily rollup cache，而非 semantic substitution。
- Session-based mid-term features 若日後引入，必須為新 feature families，不得 silently 取代既有 mid-term columns。

### 5.3 Slow / Long-Term

Slow 180d features 為 session-derived canonical active-anchor snapshots。

要求：

- Source table 為 cleaned session。
- Active slow anchor 與 month-turn contract 由 slow train-serve parity 文件治理。
- Target scope 縮減不得截斷 active 180d window 所需的 session history。
- Cache 與 reports 必須揭露 slow source coverage 與 anchor metadata。

### 5.4 Labels

Labels 由 canonical bet sequence 與 forward determinability 計算。

要求：

- Target scope 可縮減 candidate rows。
- Label source 仍須包含足夠 future observation 以判定 uncensored labels。
- Censored rows 在 label computation 後排除，非僅依 month heuristics。

## 6) Cache Invalidation Rules

### 6.1 Target Horizon Changes

變更 `recent_full_months`、`include_current_partial_month` 或 `as_of_date` 會 invalidate：

- assembled training dataset cache
- split cache
- sampled train cache
- feature selection cache
- model artifacts

本身**不得** invalidate：

- source manifests
- cleaned source cache
- entity set cache
- labels primitive cache，除非 target rows 需要先前缺失的 label shards
- Feast / slow primitive cache
- short-PIT primitive shards，除非 requested target months 未物化或新 columns 需要 shard fill（miss path 可依 TA-014 / TA-017  invoke indexed replay）
- mid-term primitive snapshots，除非 requested anchor coverage 缺失

### 6.2 Negative Sampling Changes

變更 `neg_sample_frac` 或 sampling seed 會 invalidate：

- sampled train cache
- 由該 sampled train 訓練的 model artifacts

**不得** invalidate：

- assembled training dataset
- val/test splits
- feature primitives

### 6.3 Feature Screening Changes

變更 feature screening policy 會 invalidate：

- selected feature manifest
- model artifacts

除非 selected features 需要 unavailable 的 primitives，否則不得 invalidate feature primitives。

### 6.4 Feature Contract Changes

變更 feature formula、cadence、grain、supplier 或 runtime contract 會 invalidate 對應的 primitive family 與 downstream artifacts。

從 model input set 移除 feature 僅在 requested columns 仍被既有 primitive shards 涵蓋時，才 invalidate assembly/model artifacts。

## 7) Data Completeness Requirements

對 `recent_full_months` 所選的 target full months，pipeline 必須 audit date completeness。

最低 report fields：

- expected full target months
- observed date range per target month
- missing `gaming_day_event` dates per target month
- row counts per target month
- censored row counts per target month
- source coverage status per feature family

Completeness modes：

- `warn`：發出 warnings 並繼續。
- `strict`：complete target months 有 missing expected dates 或 feature source coverage 不足時 fail。

預設 mode 為 `warn`；final release / deploy-candidate runs 應使用 `strict`，除非明確 waiver。

## 8) Artifact And Manifest Requirements

每次 accelerated training run 必須 persist manifest 或 metrics block，記錄：

- target scope policy
- selected target months
- feature source coverage report
- split policy 與 split date ranges
- censored rows excluded
- negative sampling policy 與 pre/post train row counts
- selected feature policy 與 selected feature fingerprint（使用 screening 時）
- cache hit/miss summary by layer

Cache layers 必須暴露 stable fingerprints：

- `training_scope_policy_fingerprint`
- `split_policy_fingerprint`
- `sample_policy_fingerprint`
- `feature_selection_policy_fingerprint`
- `model_input_feature_fingerprint`

## 9) Non-Functional Requirements

- Daily iteration 在 target scope 為 recent 時應避免 full 17-month recomputation。
- Cache hit/miss reasons 必須可從 manifests 解釋。
- 所有 policy changes 必須明確寫在 Python/YAML config 或 run args，不得用 environment variables。
- 相同 input + 相同 policy 必須產生可重現的 target scope 與 sampled train rows。
- Evaluation metrics 必須揭露 train 是否 sampled、val/test 是否 full。
- 預設 production-safe path 必須優先 correctness 與 traceability，而非 silent reuse。

## 10) Success Criteria

- 以 `recent_full_months = 3` 執行時，僅物化 / assemble 所選 target months，同時保留 feature source history。
- 切換 `recent_full_months = 3 -> 6` 時，在 coverage 存在處重用既有 primitive month shards。
- 變更 `neg_sample_frac` 僅 rebuild sampled train 與 downstream model artifacts。
- Val/test 始終包含 target scope 下最新 eligible uncensored rows。
- Feature source coverage warnings 在 model training 前識別 missing history。
- Run report 可解釋 time saved、cache reuse rate、target row reduction、train row reduction。
- Step 3.5 short-PIT cache miss 在 full target month 上，經 indexed replay 於 O(~20–30 min) 量級 wall time 完成 ~3.4M ADT-scoped targets（202605 reference），相較同規模 bounded oracle 的 O(~4 h)。
- Full-month indexed replay vs bounded oracle 在 Step 3.5 整合前，須於 scorer-relevant `fe__*` gate columns 達 hard parity；任何剩餘 legacy `bet__*` discrepancy 需 explicit waiver 與 root-cause documentation。

## 11) Assumptions

- `gaming_day_event` 為 authoritative day-level partition 與 split key。
- Source parquet partitions 可對應至 month-level coverage。
- 既有 short / mid / slow feature contracts 仍有效。
- Latest/global ADT entity set 仍為 accepted training universe policy。
- 現行 model evaluation 預設重視 recency 與 drift sensitivity，勝過 all-history stability。
- WP-10 full-month gate 在 3.4M scale 量測 10.28× speedup；唯一觀察到的 mismatch 為已接受的 alias-driven `bet__*` waiver，而非 core `fe__*` parity break。

## 12) Decision Log

| ID | Date | Decision | Evidence / notes |
|----|------|----------|------------------|
| DL-001 | 2026-06-08 | WP-10 full-month gate **關閉**：speedup 10.28×，output validation 通過，`fe__*` parity 通過，`bet__*` mismatch 以 waiver 接受；indexed replay 核准為 Step 3.5 miss-path candidate。 | `benchmark_report.json` 於修復 DuckDB `list(... LIMIT)` SQL bug（原 4.3h run 在 parity step 中止）後寫入。剩餘 54 個 `bet__bets_cnt__w1h`-family mismatches（~0.0016% rows）追溯至同一 canonical alias cluster（`bet_id` ~640591154），full-month indexed replay 納入了 bounded 2000-row batch pool 未納入的 alias player。 |

## 13) Open Questions

- 首次量測 model-quality comparison（對 6 / 12 months）後，預設是否仍維持 `recent_full_months = 3`。
- Final release runs 是否預設要求 `data_completeness_mode = strict`。
- Feature screening 是否應成為 formal pre-Step-5 gate，或維持 experiment-only。
- Short-PIT feature pruning 應由 per-feature materialization cost、model importance，或兩者驅動。
- Mid-term daily snapshots 是否需在 recent target scope 實作前或後建立 anchor-month shard cache。
- 已接受的 WP-10 `bet__*` waiver 是否維持為 documented exception，或日後 harmonize batch-local 與 full-month canonical fanout semantics 後移除。

## 14) Relationship To Existing Documents

- [`Data pipeline - SSOT.md`](Data%20pipeline%20-%20SSOT.md) 治理整體 offline training data 與 feature pipeline。
- [`Cache Redesign - SSOT.md`](Cache%20Redesign%20-%20SSOT.md) 治理 source / primitive cache correctness 與 invalidation。
- [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md) 治理 serving 與 PIT runtime semantics。
- 本文件治理 target scope、training acceleration policy，以及這些 policy 對 cache reuse 的允許與限制。
- Realization strategy：[`Training Acceleration and Scope - IMPLEMENTATION_PLAN.md`](../implementation/active/Training%20Acceleration%20and%20Scope%20-%20IMPLEMENTATION_PLAN.md)
- Execution tasks：[`Training Acceleration and Scope - WORKING_PLAN.md`](../working/active/Training%20Acceleration%20and%20Scope%20-%20WORKING_PLAN.md)
