# trainer_hightier - Training Acceleration and Scope SSOT

本文件屬於 **SSOT 層**，定義 `trainer_hightier` 訓練加速、訓練 target scope、sampling、feature source coverage 與 downstream cache reuse 的治理真相。

本文件不包含 ticket 級任務拆解或實作順序；實作策略應由 implementation plan 承接，具體 execution plan 應另建 working plan。

## 1) Objective

在不破壞 train-serve parity、feature contract 與 PIT correctness 的前提下，縮短 `trainer_hightier` 日常訓練迭代時間。

核心目標：

- 用 recent target scope 減少進入 Step 3 / Step 3.5 / Step 4 / Step 5 的 target rows。
- 用 train-only negative downsampling 降低 Step 5 訓練成本。
- 用 optional feature screening 降低模型訓練欄位數與候選實驗成本。
- 用 cache 分層讓 expensive primitives 可跨不同 target horizon / sampling policy 重用。
- 明確區分 target scope 與 feature source scope，避免為了加速而截斷 short / mid / slow features 所需歷史。

## 2) Scope

### In Scope

- Training target horizon policy：最近 N 個完整月 + current partial month。
- Full-month source completeness checks for selected target months。
- Train / validation / test split 的 recency 與 uncensored-row rule。
- Train-only negative downsampling policy。
- Optional target-row sampling for experiments。
- Optional feature screening / top-k feature selection policy。
- Downstream cache policy for assembled dataset, split, sampled train, feature selection, and model artifacts。
- Feature family source coverage requirements for short / mid / slow / labels。

### Non-Scope

- 變更 walkaway label business definition。
- 變更 production scorer runtime contract。
- 變更 short-term PIT bounded semantics。
- 以 session table 取代現有 bet-based mid-term feature contract。
- 將 experimental target sampling 預設升級為 production training policy。
- Ticket 級 work breakdown。

## 3) Governing Decisions

| ID | 決策 | 理由 / 邊界 |
|----|------|-------------|
| TA-001 | Default training target horizon is `recent_full_months = 3` plus current partial month. | 先以速度與 drift sensitivity 為主；保留 config 可改為 6 / 12 / all history。 |
| TA-002 | `include_current_partial_month = true`. | Val/test 必須反映最新 data drift；partial month 不應被自動排除。 |
| TA-003 | All splits must drop `walkaway_censored = TRUE`. | Label 不可判定的 row 不得進 train / val / test。 |
| TA-004 | Validation/test should use the most recent uncensored rows available under the target scope. | Drift-sensitive model 不應刻意避開 current partial month。 |
| TA-005 | Recent horizon filters target training rows only. | 不得因 target horizon 變短而截斷 feature source history。 |
| TA-006 | Feature source scope is owned by feature family contracts. | short / mid / slow / labels 各自有不同 lookback / lookahead 需求。 |
| TA-007 | Expensive primitive caches are independent from target horizon and sampling policies. | `3m -> 6m` 不應重算 cleaned / labels / Feast / short PIT / mid / slow primitives。 |
| TA-008 | Train-only negative downsampling defaults to off (`neg_sample_frac = 1.0`). | 避免 silent class-prior change；speed runs may set e.g. `0.3` explicitly。 |
| TA-009 | Negative downsampling applies only to train split; val/test remain full unsampled. | 評估分佈與 precision / alert-volume 指標不可被 sampling 污染。 |
| TA-010 | Target-row experiment sampling must live in an experiment namespace. | Debug/prototype samples 不得覆蓋 production training artifacts。 |
| TA-011 | Feature screening is optional and must first produce a manifest, not directly mutate the registry baseline. | 先用 evidence 驅動 baseline contract 變更。 |
| TA-012 | Short-PIT feature pruning is a feature-contract decision, not a pure cache optimization. | 需由 importance / ablation / deploy contract 同步支撐。 |
| TA-013 | Mid-term acceleration should preserve bet-based mid-term semantics. | 不採 session table 替代現有 mid-term baseline contract。 |

## 4) Domain Definitions

### 4.1 Target Scope

Target scope defines which bet rows are eligible to enter the training dataset.

Required policy fields:

- `recent_full_months`
- `include_current_partial_month`
- `as_of_date`
- `target_months`
- `target_start_date`
- `target_end_date`
- `data_completeness_mode`

Example for `as_of_date = 2026-06-08` and `recent_full_months = 3`:

```text
target_months = 202603, 202604, 202605, 202606_partial
```

The current partial month is included up to available data, subject to label censoring.

### 4.2 Feature Source Scope

Feature source scope defines the historical source data that a feature family may read to compute correct values for target rows.

Feature source scope is not controlled by `recent_full_months`.

Examples:

| Feature family | Source | Required source scope |
|----------------|--------|-----------------------|
| Short-term PIT | cleaned bet | Scorer bounded pool: hot lookback hours + gaming-day floor + per-bet scoring bounds |
| Mid-term daily snapshot | cleaned bet | At least the configured mid-term lookback window before anchor days |
| Slow 180d monthly | cleaned session | Active slow anchor's inclusive 180 calendar-day session window |
| Labels | cleaned bet | Forward lookahead / terminal determinability window |

### 4.3 Label Scope

Label scope defines whether the label is determinable for a target row.

Rules:

- `walkaway_censored = TRUE` rows are excluded from train / val / test.
- Current partial month rows may be used if uncensored.
- Reports must expose censored row counts by target month and split.

### 4.4 Training Policy Cache

Training policy cache covers cheap downstream views and artifacts derived from primitives:

- assembled training dataset
- train / val / test split
- sampled train split
- selected feature set
- model artifacts

It must not be conflated with source / primitive feature caches.

## 5) Feature Family Source Contracts

### 5.1 Short-Term PIT

Short-term PIT features remain exact bounded PIT features.

Requirements:

- Target rows may be reduced by target scope.
- Source pool semantics must remain aligned with scorer bounds.
- Month-sharded short-PIT primitive cache may be reused across target scopes when shard universe and columns cover requested rows.
- Removing requested short-PIT columns may use existing superset shards.
- Adding new short-PIT primitive columns requires recompute or fill for affected shards only.

### 5.2 Mid-Term

Mid-term features remain bet-based daily gaming-day snapshots at `canonical_id + anchor_gaming_day_event` grain.

Requirements:

- Source table remains cleaned bet for current baseline mid-term features.
- Acceleration should prefer reusable daily / anchor-month cache or bet daily rollup cache over semantic substitution.
- Session-based mid-term features, if ever introduced, must be new feature families and cannot silently replace existing mid-term columns.

### 5.3 Slow / Long-Term

Slow 180d features are session-derived canonical active-anchor snapshots.

Requirements:

- Source table is cleaned session.
- The active slow anchor and month-turn contract are governed by slow train-serve parity documents.
- Target scope reduction must not truncate the session history needed for the active 180d window.
- Cache and reports must expose slow source coverage and anchor metadata.

### 5.4 Labels

Labels are computed from canonical bet sequence and forward determinability.

Requirements:

- Target scope may reduce candidate rows.
- Label source must still include enough future observation to determine uncensored labels.
- Censored rows are excluded after label computation, not by month heuristics alone.

## 6) Cache Invalidation Rules

### 6.1 Target Horizon Changes

Changing `recent_full_months`, `include_current_partial_month`, or `as_of_date` invalidates:

- assembled training dataset cache
- split cache
- sampled train cache
- feature selection cache
- model artifacts

It must not invalidate by itself:

- source manifests
- cleaned source cache
- entity set cache
- labels primitive cache, except when target rows require previously missing label shards
- Feast / slow primitive cache
- short-PIT primitive shards, except when requested target months are not materialized
- mid-term primitive snapshots, except when requested anchor coverage is missing

### 6.2 Negative Sampling Changes

Changing `neg_sample_frac` or sampling seed invalidates:

- sampled train cache
- model artifacts trained from that sampled train

It must not invalidate:

- assembled training dataset
- val/test splits
- feature primitives

### 6.3 Feature Screening Changes

Changing feature screening policy invalidates:

- selected feature manifest
- model artifacts

It must not invalidate feature primitives unless the selected features require primitives that are unavailable.

### 6.4 Feature Contract Changes

Changing feature formula, cadence, grain, supplier, or runtime contract invalidates the corresponding primitive family and downstream artifacts.

Removing a feature from the model input set invalidates assembly/model artifacts only when the requested columns remain covered by existing primitive shards.

## 7) Data Completeness Requirements

For target full months selected by `recent_full_months`, the pipeline must audit date completeness.

Minimum report fields:

- expected full target months
- observed date range per target month
- missing `gaming_day_event` dates per target month
- row counts per target month
- censored row counts per target month
- source coverage status per feature family

Completeness modes:

- `warn`: emit warnings and continue.
- `strict`: fail when complete target months have missing expected dates or feature source coverage is insufficient.

Default mode is `warn`; final release / deploy-candidate runs should use `strict` unless explicitly waived.

## 8) Artifact And Manifest Requirements

Every accelerated training run must persist a manifest or metrics block that records:

- target scope policy
- selected target months
- feature source coverage report
- split policy and split date ranges
- censored rows excluded
- negative sampling policy and pre/post train row counts
- selected feature policy and selected feature fingerprint, when screening is used
- cache hit/miss summary by layer

Cache layers must expose stable fingerprints for:

- `training_scope_policy_fingerprint`
- `split_policy_fingerprint`
- `sample_policy_fingerprint`
- `feature_selection_policy_fingerprint`
- `model_input_feature_fingerprint`

## 9) Non-Functional Requirements

- Daily iteration should avoid full 17-month recomputation when target scope is recent.
- Cache hit/miss reasons must be explainable from manifests.
- All policy changes must be explicit in Python/YAML config or run args, not environment variables.
- Same input + same policy must produce reproducible target scope and sampled train rows.
- Evaluation metrics must disclose whether train was sampled and whether val/test were full.
- The default production-safe path must prefer correctness and traceability over silent reuse.

## 10) Success Criteria

- Running with `recent_full_months = 3` materializes / assembles only selected target months while preserving feature source history.
- Switching `recent_full_months = 3 -> 6` reuses existing primitive month shards where coverage exists.
- Changing `neg_sample_frac` only rebuilds sampled train and downstream model artifacts.
- Val/test always contain the latest eligible uncensored rows under target scope.
- Feature source coverage warnings identify missing history before model training.
- The run report can explain time saved, cache reuse rate, target row reduction, and train row reduction.

## 11) Assumptions

- `gaming_day_event` is the authoritative day-level partition and split key.
- Source parquet partitions can be mapped to month-level coverage.
- Existing short / mid / slow feature contracts remain valid.
- Latest/global ADT entity set remains the accepted training universe policy.
- Current model evaluation values recency and drift sensitivity over all-history stability by default.

## 12) Open Questions

- Whether the default should remain `recent_full_months = 3` after the first measured model-quality comparison against 6 / 12 months.
- Whether final release runs should require `data_completeness_mode = strict` by default.
- Whether feature screening should become a formal pre-Step-5 gate or remain experiment-only.
- Whether short-PIT feature pruning should be driven by per-feature materialization cost, model importance, or both.
- Whether mid-term daily snapshots need anchor-month shard cache before or after recent target scope is implemented.

## 13) Relationship To Existing Documents

- `Data pipeline - SSOT.md` governs the overall offline training data and feature pipeline.
- `Cache Redesign - SSOT.md` governs source / primitive cache correctness and invalidation.
- `Scorer Runtime Contract - SSOT.md` governs serving and PIT runtime semantics.
- This document governs target scope, training acceleration policy, and how those policies may or may not affect cache reuse.

