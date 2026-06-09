# trainer_hightier - Training Acceleration and Scope - Implementation Plan

本文件屬於 **Implementation Plan 層**，承接 [`Training Acceleration and Scope - SSOT.md`](../../ssot/Training%20Acceleration%20and%20Scope%20-%20SSOT.md)，定義 training acceleration、target scope、Step 3.5 miss-path、gate/waiver governance、train-only downsampling 與 feature screening hook 如何落地到 `trainer_hightier` training pipeline。

本文件不包含 ticket 級工作拆解；具體任務、owner、執行順序與 DoD 應在後續 working plan 承接。

## 0) Alignment

### 0.1 Governing SSOT

| Document | Role |
|----------|------|
| [`Training Acceleration and Scope - SSOT.md`](../../ssot/Training%20Acceleration%20and%20Scope%20-%20SSOT.md) | 本計畫上位規格（TA-001～TA-017、DL-001） |
| [`Data pipeline - SSOT.md`](../../ssot/Data%20pipeline%20-%20SSOT.md) | Step 3/4/5 管線與 split 語意 |
| [`Cache Redesign - SSOT.md`](../../ssot/Cache%20Redesign%20-%20SSOT.md) | primitive vs policy cache 分層 |
| [`Scorer Runtime Contract - SSOT.md`](../../ssot/Scorer%20Runtime%20Contract%20-%20SSOT.md) | bounded PIT / short-term supplier 語意 |

### 0.2 Related Implementation Plans

| Document | Relationship |
|----------|--------------|
| [`Cache Redesign - IMPLEMENTATION_PLAN.md`](Cache%20Redesign%20-%20IMPLEMENTATION_PLAN.md) | entity set、assembly cache、invalidation 基礎 |
| [`Short-Term PIT Cache and Materialize Performance - IMPLEMENTATION_PLAN.md`](Short-Term%20PIT%20Cache%20and%20Materialize%20Performance%20-%20IMPLEMENTATION_PLAN.md) | month-sharded short-PIT cache；indexed replay prototype 歷史在此，本計畫只承接 WP-10 integration |
| [`Data pipeline - IMPLEMENTATION_PLAN.md`](Data%20pipeline%20-%20IMPLEMENTATION_PLAN.md) | `gaming_day_event`、split、parity 基礎 |

### 0.3 Objective

在不破壞 train-serve parity、feature contract 與 PIT correctness 的前提下，縮短日常訓練迭代時間，並讓加速策略可稽核、可回退、可解釋。

### 0.4 Non-Goals

- 不改 walkaway label business definition。
- 不改 production scorer runtime contract。
- 不把 experimental target sampling 預設升級為 production training policy。
- 不在本計畫中直接改 `feature_candidate_registry.yaml` baseline（screening 先產 manifest）。
- 不 harmonize batch-local vs full-month canonical fanout 語意（WP-10 `bet__*` waiver 先文件化接受）。

### 0.5 Frozen Implementation Decisions

| Decision | Choice |
|----------|--------|
| Indexed replay role | Step 3.5 cache miss / cold-build **default miss-path** |
| Waiver policy | 第一版寫入正式 gate / report code |
| Failure mode | **fail-fast**（不自動 fallback 到 bounded） |
| Neg downsampling | 與 acceleration 主線同一 rollout phase |
| Feature screening | Phase 2 hook only；預設關閉 |
| Policy config form | Python policy object / dataclass；不用 env var |
| Risk posture | balanced |

## 1) Guiding Constraints

- **Target scope ≠ feature source scope**（SSOT TA-005/006）：`recent_full_months` 只縮 target rows，不得截斷 short/mid/slow/labels 所需 source history。
- **Primitive cache ≠ training policy cache**（SSOT TA-007）：horizon / sampling / screening 變更不得誤 invalidate L0–L5 primitives。
- **Hard parity vs waiver**：`fe__*` gate columns 是 hard bar；legacy `bet__*` 1h pack 可接受已 root-cause 的 explicit waiver，但不代表 full parity pass。
- **Dual gate separation**：indexed-vs-bounded cold-build gate ≠ Step 4.5 train-vs-serve parity gate。
- **Resource guardrails**：沿用 DuckDB runtime config、month shard、batch policy；避免 full-month indexed replay 與 bounded oracle 同時常駐。
- **Traceability**：每次 accelerated run 必須能從 manifest / run report 解釋 policy、cache hit/miss、gate/waiver decision。

## 2) Realization Boundaries

| Module | Primary files | Policy / artifact outputs |
|--------|---------------|---------------------------|
| Target scope policy | `trainer_hightier/config.py`, `trainer_hightier/trainer.py`, `trainer_hightier/03_build_training_data.py` | `training_scope_policy_fingerprint`, completeness report |
| Split / censored rows | `trainer_hightier/04_split_dataset.py` | `split_policy_fingerprint`, censored counts by month |
| Step 3.5 short-PIT miss-path | `trainer_hightier/feature_experiment/short_term_pit_cache.py`, `trainer_hightier/feature_experiment/short_term_pit_replay_indexed_prototype.py`, `trainer_hightier/feature_experiment/materialize_fe_derived.py`, `trainer_hightier/serving/short_term_scoring_context.py` | month shard hit/miss, materializer policy fingerprint |
| Gate / waiver / benchmark | `trainer_hightier/feature_experiment/short_term_pit_replay_indexed_prototype.py`, `trainer_hightier/06_verify_training_serving_parity.py`, `trainer_hightier/reporting/writer.py` | `benchmark_report.json`, gate decision block |
| Train-only downsampling | `trainer_hightier/config.py`, `trainer_hightier/trainer.py`, `trainer_hightier/05_lgbm_train.py` | `sample_policy_fingerprint`, sampled train parquet |
| Feature screening hook | `trainer_hightier/feature_experiment/feature_quality_gate.py`, `trainer_hightier/contracts/feature_candidate_registry.yaml`, `trainer_hightier/trainer.py` | `feature_selection_policy_fingerprint`, selected feature manifest |
| Cache / assembly | `trainer_hightier/utils/assembly_cache_v1.py`, `trainer_hightier/utils/cache_invalidation_v1.py` | cache hit/miss reason codes |

## 3) Target Strategy

### 3.1 Training Scope Policy

Introduce a single policy object (Python dataclass) owning:

```text
recent_full_months
include_current_partial_month
as_of_date
data_completeness_mode   # warn | strict
target_months
target_start_date
target_end_date
```

Responsibilities:

- Resolve selected target months from `as_of_date` and horizon policy.
- Restrict Step 3 assembly / month batching to **selected target months only**; do not first assemble all historical months and then apply horizon filtering afterward.
- Emit completeness audit for full target months (`gaming_day_event` date coverage, row counts, censored counts).
- Persist `training_scope_policy_fingerprint` into run manifest.

Default for MVP rollout: `recent_full_months=3`, `include_current_partial_month=true`, `data_completeness_mode=warn`.

### 3.2 Step 3.5 Materialization Policy

Two-path strategy aligned with SSOT §5.1.1:

| Path | Trigger | Engine |
|------|---------|--------|
| **Hit** | month shard exists and covers requested columns / universe | read month-sharded short-PIT cache |
| **Miss** | shard missing, column fill needed, or cold-build | `materialize_short_term_replay_indexed_prototype` (`emit_opt`) |

Policy fields:

```text
step35_miss_path = "indexed_replay"
indexed_replay_gate_mode = "hard_fe_soft_bet_pack"
indexed_replay_failure_mode = "fail_fast"
```

On miss-path failure, hard parity failure, or waiver condition violation: **fail the run**. Do not silently auto-fallback to bounded DuckDB in v1.

Bounded DuckDB remains:

- parity oracle for benchmark harness
- diagnostic / regression baseline
- not the default silent fallback

### 3.3 Gate and Waiver Artifact Contract

Full-month cold-build gate report must expose both raw parity and governed decision:

```json
{
  "parity": {
    "passed": false,
    "hard_parity_passed": true,
    "hard_parity_columns": ["fe__..."],
    "waived_columns": ["bet__bets_cnt__w1h", "..."],
    "waiver_accepted": true,
    "waiver": {
      "scope": "legacy_bet_pack_1h",
      "mismatch_row_upper_bound": 54,
      "mismatch_row_ratio": 0.0000159,
      "root_cause": "...",
      "cluster_anchor_bet_id": 640591154
    }
  },
  "go_no_go": {
    "decision": "integrate_candidate_with_bet_pack_waiver",
    "hard_parity_passed": true,
    "waiver_accepted": true,
    "final_integration_met": true,
    "decision_basis": { "...": true }
  }
}
```

Governance rules:

- `fe__*` mismatch → fail
- waiver accepted only when root cause is documented, mismatch ratio is below agreed threshold, and hard parity passed
- `parity.passed=false` must remain when waiver applies (no silent rewrite to full pass)

Reference evidence: `out/replay_benchmark_202605_indexed_full_month_gate16_emit_opt/benchmark_report.json` (SSOT DL-001).

### 3.4 Train-Only Negative Downsampling

Policy fields:

```text
neg_sample_frac = 1.0          # default off
neg_sample_seed                 # fixed seed for reproducibility
neg_sample_scope = "train_only"
```

Behavior:

- Apply downsampling only after Step 4 split, before Step 5 fit.
- Val/test remain full unsampled.
- Persist pre/post train row counts and `sample_policy_fingerprint`.
- Invalidate only sampled-train cache + model artifacts on policy change.

### 3.5 Feature Screening Hook (Phase 2)

Default: disabled.

When enabled:

- produce selected feature manifest + `feature_selection_policy_fingerprint`
- do not mutate registry baseline directly
- reuse patterns from `feature_quality_gate.py` / experiment pipeline, but expose a trainer hook point only

## 4) Workstreams

### WS-A: Target Scope, Completeness, and Split Policy

**Goal**：以 recent horizon 縮小 target rows，同時保留 feature source history 與 recency-sensitive val/test。

**Approach**

- Add `TrainingScopePolicy` dataclass in `config.py`.
- Wire policy resolution into `trainer.py` and `03_build_training_data.py` before Step 3 month batching / assembly and Step 4 split reporting.
- Keep primitive materialization driven by entity set / source coverage, not by target horizon alone.
- Preserve feature source history for short / mid / slow / labels, but restrict Step 3 entity/output months to the selected target months.
- Extend split reporting with censored-row and target-month summaries.

**Deliverables**

- `TrainingScopePolicy` config object and fingerprint helper.
- Completeness report block (`expected months`, `missing gaming_day_event dates`, row/censored counts).
- Run manifest fields: `training_scope_policy_fingerprint`, selected target months.
- Tests for horizon resolution and target-vs-source boundary.

**Dependencies**

- Cache Redesign entity set / assembly layers.
- `04_split_dataset.py` censored exclusion already present.

### WS-B: Step 3.5 Indexed Replay Miss-Path Integration

**Goal**：cache miss / cold-build 預設走 indexed replay，保留 month shard hit path。

**Approach**

- Extend `short_term_pit_cache.py` miss branch to call indexed replay instead of bounded materializer.
- Expand code fingerprint to include indexed replay module + gate mode.
- Keep bounded path available for benchmark oracle only.
- Record miss-path timings and materializer choice in run report.

**Deliverables**

- Miss-path integration in `short_term_pit_cache.py`.
- Materializer policy fingerprint in cache manifest.
- Run report fields: `step35_miss_path`, shard hit/miss counts, replay timings.
- Regression tests on hit path unchanged + miss path wired.

**Dependencies**

- WP-10 benchmark evidence (10.28× speedup, `fe__*` hard parity pass).
- Short-Term PIT cache architecture from existing IMPLEMENTATION_PLAN.

### WS-C: Gate, Waiver, and Report Formalization

**Goal**：把 SSOT DL-001 waiver governance 固化到 code 與 artifacts。

**Approach**

- Refactor `evaluate_full_month_cold_build_gate()` to split hard parity vs waiver decision.
- Persist structured waiver block in benchmark and training run reports.
- Keep Step 4.5 `06_verify_training_serving_parity.py` as separate train-vs-serve gate.
- Fail-fast when waiver conditions are not met.

**Deliverables**

- Updated gate evaluator with `hard_parity_passed`, `waiver_accepted`, `decision_basis`.
- Benchmark report schema aligned with SSOT.
- Run manifest gate summary for Step 3.5 miss-path adoption.
- Tests for pass / fail / waiver-accepted / waiver-rejected cases.

**Dependencies**

- WS-B miss-path integration.
- Existing parity harness in `short_term_pit_replay_indexed_prototype.py`.

### WS-D: Train-Only Negative Downsampling

**Goal**：降低 Step 5 train cost，但不污染 val/test evaluation。

**Approach**

- Introduce sampled-train artifact between Step 4 and Step 5.
- Downsample negatives only (`neg_sample_frac < 1.0`), preserve positives unless explicitly configured otherwise later.
- Write metrics disclosure into `training_metrics.json` / bundle report.

**Deliverables**

- `SamplePolicy` config + fingerprint.
- Sampled train parquet artifact and cache invalidation rules.
- Step 5 hook to read sampled train by default when policy enabled.
- Tests for train-only scope, reproducibility, and val/test untouched.

**Dependencies**

- WS-A target scope / split artifacts stable enough to measure row reduction honestly.

### WS-E: Phase-2 Feature Screening Hook

**Goal**：預留 screening manifest 接口，不在第一版改 baseline registry。

**Approach**

- Define trainer hook after Step 4 / before Step 5 for optional manifest input.
- Reuse FQG manifest patterns from experiment pipeline.
- Default path remains full baseline feature set from registry.

**Deliverables**

- Manifest contract (`selected_features`, method, evidence refs, fingerprint).
- Trainer hook stub + report fields.
- Documentation only for screening rollout criteria (no execution in Phase 1–3).

**Dependencies**

- None blocking Phases 1–3.

## 5) Rollout Phases

### Phase 1 - Target Scope Foundation

**Deliverables**

- `TrainingScopePolicy` in `config.py`.
- Trainer wiring for target-month selection and completeness report.
- `training_scope_policy_fingerprint` in run manifest.
- Split/report exposure of censored counts by target month.

**Milestone**

- Running with `recent_full_months=3` materializes only selected target months while primitive source history remains intact.

**Validation**

- Horizon change invalidates assembly/split/sample/model only.
- Completeness `warn` emits report; `strict` fails on missing full-month dates.

### Phase 2 - Step 3.5 Miss-Path Wiring + Gate Formalization

**Deliverables**

- Indexed replay as default miss-path in `short_term_pit_cache.py`.
- Gate evaluator with hard parity + waiver artifact.
- Benchmark/report schema aligned with SSOT DL-001.
- Fail-fast on miss-path or hard parity failure.

**Milestone**

- Full-month miss-path completes in ~20–30 min class wall time for ~3.4M targets (202605 reference), with `fe__*` hard parity green.

**Validation**

- Shard hit path unchanged.
- Miss path produces gate artifact with decision basis.
- Step 4.5 parity gate remains separate and still enforced.

### Phase 3 - Train-Only Negative Downsampling

**Deliverables**

- Sampled-train layer + `sample_policy_fingerprint`.
- Step 5 integration and metrics disclosure.
- Cache invalidation limited to sampled train + model artifacts.

**Milestone**

- Changing `neg_sample_frac` rebuilds only sampled train and downstream model artifacts.

**Validation**

- Val/test row counts unchanged by downsampling.
- Reproducible sampled train at fixed seed.

### Phase 4 - Feature Screening Hook and Manifest

**Deliverables**

- Optional screening manifest hook (default off).
- `feature_selection_policy_fingerprint` contract.
- Integration notes with FQG / registry process.

**Milestone**

- Screening can be enabled in experiment namespace without touching production baseline path.

**Validation**

- Disabled hook is no-op.
- Enabled hook writes manifest only; registry baseline unchanged unless separately approved.

## 6) Validation Strategy

### 6.1 Policy / Cache Correctness

- Target horizon change → miss assembly, split, sampled train, model only.
- `neg_sample_frac` change → miss sampled train + model only.
- Feature screening change → miss selection manifest + model only.
- Primitive caches (cleaned, labels, Feast, short/mid/slow shards) remain reusable when coverage exists.

### 6.2 Step 3.5 Correctness and Gate

| Check | Pass criteria |
|-------|---------------|
| Output validation | row count / unique bet_id match targets |
| `fe__*` hard parity | zero mismatch vs bounded oracle |
| `bet__*` waiver | only accepted when documented + low-ratio + hard parity passed |
| Speedup gate | ≥ 3.0× on full-month cold-build benchmark |
| Fail-fast | replay failure, hard parity failure, invalid waiver → stop run |

### 6.3 Performance / Resource

Track in benchmark and run report:

- replay wall time
- bounded oracle wall time (benchmark only)
- speedup ratio
- memory peak
- cache hit/miss shard counts
- target row reduction from horizon policy
- train row reduction from downsampling

### 6.4 Rollout Safety

1. **Report-only dry run**：emit policy fingerprints and would-be miss-path decisions without switching default.
2. **Miss-path enablement**：switch default miss-path after gate artifact passes.
3. **Downsampling enablement**：opt-in via explicit policy (`neg_sample_frac < 1.0`).
4. **Screening enablement**：experiment namespace only until separately approved.

## 7) Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Target scope applied to primitive source history | correctness regression on mid/slow features | separate fingerprints; audit report; tests on TA-005/007 |
| Waiver misread as full parity pass | silent integration of incorrect legacy pack | keep `parity.passed=false`; require `hard_parity_passed` + waiver block |
| Indexed gate conflated with Step 4.5 parity | false confidence or false block | separate artifacts and runbook sections |
| Miss-path failure hidden by auto fallback | masks indexed replay bugs | fail-fast v1; bounded only as oracle |
| Downsampling pollutes val/test metrics | wrong threshold / alert volume interpretation | train-only enforcement + metrics disclosure |
| Feature screening mutates baseline too early | train-serve / registry drift | manifest-first hook; registry change out of scope |
| Code fingerprint incomplete after indexed integration | stale shard reuse | extend short-PIT cache fingerprint to indexed replay module |
| Canonical alias discrepancy grows | more `bet__*` mismatches | monitor waiver metrics; revisit fanout harmonization only if ratio rises |

## 8) Success Criteria

Aligned with SSOT §10:

- `recent_full_months=3` reduces target rows without truncating feature source history.
- Horizon change reuses existing primitive month shards where coverage exists.
- Step 3.5 miss-path uses indexed replay at full-month scale with hard parity + documented waiver governance.
- `neg_sample_frac` changes rebuild only sampled train + model artifacts.
- Val/test retain latest eligible uncensored rows under target scope.
- Run report explains time saved, cache reuse, target row reduction, sampled-train reduction, and gate/waiver decision.

## 9) Governance and Ownership

| Area | Owner (high level) |
|------|---------------------|
| SSOT / policy truth | training acceleration SSOT owner |
| Step 3 / 4 target scope & split | data pipeline owner |
| Step 3.5 miss-path & gate | trainer pipeline / short-PIT owner |
| Cache invalidation | cache redesign owner |
| Step 5 sampling & metrics | ML training owner |
| Step 4.5 parity gate | train-serve parity owner |

Review gates:

- ML owner: downsampling impact, screening manifest, evaluation disclosure
- Platform owner: resource limits, run report, fail-fast behavior
- Runtime contract owner: confirm no production scorer semantic change from waiver

## 10) Open Decisions

- Whether default `recent_full_months=3` remains after first model-quality comparison against 6 / 12 months.
- Whether release runs should default `data_completeness_mode=strict`.
- Whether the accepted WP-10 `bet__*` waiver should remain permanent or be removed by harmonizing batch-local vs full-month canonical fanout.
- Whether screening becomes a formal pre-Step-5 gate or stays experiment-only after Phase 4 hook lands.
- Whether refit-on-train+val interacts with sampled train policy (default assumption: refit uses unsampled val-side rows only for threshold selection, train fit uses sampled train when enabled).

## 11) Document Boundary

| Layer | Document | Contains |
|-------|----------|----------|
| SSOT | `Training Acceleration and Scope - SSOT.md` | policy truth, acceptance criteria, DL-001 |
| Implementation Plan | **this document** | realization strategy, module boundaries, phases, validation, risks |
| Working plan | [`Training Acceleration and Scope - WORKING_PLAN.md`](../../working/active/Training%20Acceleration%20and%20Scope%20-%20WORKING_PLAN.md) | ticket breakdown, sequence, DoD, verification |
| Short-Term PIT Performance IMPLEMENTATION_PLAN | existing doc | month-sharded cache + prototype history; referenced, not duplicated |

When this plan conflicts with SSOT, SSOT wins and this plan must be updated.
