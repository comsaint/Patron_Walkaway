# Production Flight Recorder - IMPLEMENTATION PLAN

## Purpose

This document defines how to implement a model-agnostic production flight recorder for `trainer_hightier` deploy bundles. The goal is to collect audit-grade evidence for production performance degradation investigations, especially cases where existing debug bundles show feature warnings but do not prove root cause.

This is an implementation plan. Product scope and business definitions remain governed by the serving/runtime SSOT documents. Ticket-level sequencing belongs in a separate working plan.

## Problem Summary

The current production debug bundle captures useful state after the fact:

- `prediction_log.db` and `state.db` exports.
- Model identity files and metrics.
- Feature missing counts and readiness/audit reports.
- Validator outcomes.

However, it does not preserve the full row-level evidence chain from production data visibility to final model and validator outputs. That prevents a solid root-cause conclusion when production precision is much lower than offline test precision.

The recorder must answer, with evidence:

- What exact ClickHouse rows did the scorer and validator see at the time?
- Did ClickHouse results change later because of late arrival, CDC updates, merges, or `FINAL` semantics?
- Which pipeline stage introduced row drops, null features, stale snapshots, score drift, or validation mismatch?
- Can production scores and validator outcomes be replayed exactly from the bundle?

## Design Principles

- Model-agnostic: no hard-coded model version, threshold, or feature list. Read all model and supplier contracts from the deploy/model bundle.
- Production-exact first: shadow recording must capture the scorer and validator paths exactly as they ran.
- Diagnostic superset: record additional ClickHouse, Feast, and stage artifacts beyond the production path when they help explain hazards.
- Row-level evidence: aggregate warnings are insufficient. Every scored row and alert must be traceable through source rows, stage outputs, model matrix, score, and validation.
- Replayable offline: the shipped bundle must support both direct Parquet/JSON analysis and local replay using the captured bundle code and artifacts.
- No credentials in artifacts: store connection aliases, query metadata, permission results, and credential fingerprints only.

## Current Runtime Facts

The implementation must align with the current code behavior:

- Scorer `t_bet` reads use `FINAL` in incremental fetches, allowlist fetches, and short-term pool fetches.
- Validator `t_bet` reads also use `FINAL`.
- Validator currently uses bet-based verdict logic as the production truth; session fetch is not part of the active verdict path, though session evidence should still be recorded diagnostically.
- Deploy startup checks Feast readiness and can run startup Feast refresh when needed.
- Deploy also runs a post-startup Feast refresh supervisor for mid/slow refresh maintenance.
- In `mode=all`, deploy starts API and validator as daemon threads, then runs the scorer in the foreground.
- Schema evidence indicates CDC/arrival columns exist and must be recorded:
  - `t_bet`: `__ts_ms`, `__op`, `__deleted`, `__etl_insert_Dtm`
  - `t_session`: `lud_dtm`, `crtd_dtm`, `__ts_ms`, `__op`, `__deleted`, `__etl_insert_Dtm`
  - `t_game`: `__ts_ms`, `__op`, `__deleted`, `__etl_insert_Dtm`
- `session_id` and `game_id` have known duplicate hazards; recorder must treat them as business keys with versioned rows, not clean primary keys.

## Solution Overview

Implement three cooperating components:

1. `live_recorder`
   - Runs with the deploy scorer/validator in shadow mode.
   - Captures the exact data and intermediate artifacts used by production scoring and validation.

2. `ch_time_machine`
   - Runs as a standalone process.
   - Requeries the same ClickHouse windows repeatedly to detect late-arriving rows, CDC updates, `FINAL` differences, mutations, and part-level changes.

3. `replay_analyzer`
   - Runs offline against a shipped recording bundle.
   - Reconstructs scores and validator outcomes, compares them with production outputs, and produces root-cause reports.

The components can be deployed together or separately. For incident investigations, run both `live_recorder` and `ch_time_machine` on the deploy machine.

## Component 1: Live Recorder

### Responsibilities

The live recorder captures exact production-cycle artifacts:

- Scorer cycle metadata.
- Raw ClickHouse query results returned to scorer.
- Every major scoring stage output.
- Feature supplier inputs/outputs.
- Model feature matrix.
- Score outputs.
- Alert writes.
- Validator cycle inputs and query outputs.
- Validator decision state transitions.

### Integration Points

The recorder should integrate around existing functions rather than reimplementing business logic:

- Scorer incremental `t_bet` fetch.
- Scorer short-term pool fetch.
- Staged feature building.
- Feast mid/slow lookup.
- Composite feature creation.
- Model matrix preparation.
- Score and alert persistence.
- Validator alert loading.
- Validator `fetch_bets_by_canonical_id`.
- Validator no-bet retry lookup by `bet_id`.
- Validator final decision write.

### Scorer Stage Artifacts

For every scorer cycle, write a directory like:

```text
cycles/scorer/cycle_000001/
  cycle_manifest.json
  clickhouse/
    incremental_t_bet.final.parquet
    incremental_t_bet.query.json
    short_term_pool_t_bet.final.parquet
    short_term_pool_t_bet.query.json
  stages/
    stage_00_raw_clickhouse_bets.parquet
    stage_01_post_timestamp_normalization.parquet
    stage_02_after_allowlist.parquet
    stage_03_after_canonical_mapping.parquet
    stage_04_short_term_pool.parquet
    stage_05_staged_features.parquet
    stage_06_feast_mid_slow_lookup.parquet
    stage_07_after_composite_features.parquet
    stage_08_model_feature_matrix.parquet
    stage_09_scores.parquet
  audits/
    row_counts.json
    feature_missing_provenance.parquet
    feature_supplier_diagnostics.json
    score_distribution.json
```

### Required Scorer Metadata

`cycle_manifest.json` must include:

- Cycle id and process id.
- Hostname, OS, Python executable, package version.
- Client wall-clock start/end timestamps.
- ClickHouse server `now()` if available.
- Scorer cursor before/after.
- Lookback window, bet availability cutoff, and limit rows.
- Allowlist mode and allowlist hash.
- Model version, model hash, threshold, feature list hash.
- Registry snapshot hash.
- Feast readiness snapshot and online store hash when feasible.
- Query ids when available.
- All SQL text and parameters, with secrets redacted.

### Feature Provenance

For each scored row and each model feature, record:

- `bet_id`, `canonical_id`, `player_id`, `game_id`, `session_id`
- feature name
- feature value
- feature source layer:
  - raw bet
  - short-term PIT
  - Feast mid
  - Feast slow
  - composite
  - model/runtime constant
- null flag
- null reason when known:
  - not enough short-term history
  - Feast entity missing
  - Feast upstream null
  - stale snapshot allowed
  - canonical mapping missing
  - allowlist filtered
  - type coercion failure
  - unknown
- upstream feature ids for composite features
- snapshot anchor and age fields for mid/slow features

## Component 2: ClickHouse Time Machine

### Responsibilities

The time-machine process proves whether source data is stable or changes after the scorer/validator first observed it.

It re-runs diagnostic queries for the same windows on a schedule:

```text
T0 + 0m
T0 + 15m
T0 + 1h
T0 + 6h
T0 + 24h
T0 + 72h
```

The schedule should be configurable, but these defaults should be used for incident capture.

### Tables and Windows

Record diagnostic extracts for:

- `t_bet`
  - scorer incremental window
  - short-term feature pool window
  - validator ground-truth window
  - broader source mirror window
- `t_session`
  - all player/canonical ids in the scorer and validator windows
  - expanded context window around alerts
- `t_game`
  - all `game_id` values referenced by recorded `t_bet` rows
  - optional broader gaming-day slice for duplicate/mutation diagnostics

### FINAL vs Non-FINAL

For each relevant ClickHouse query, capture both:

- Production-exact `FINAL` result.
- Diagnostic non-`FINAL` result.

Compare:

- Row count.
- Business key count.
- Duplicate business key count.
- Added / removed / changed business keys.
- Latest-row selection by version columns.
- Per-column hashes.
- Schema fingerprints.

### Late Arrival and Mutation Reports

For every repeated query window, write:

```text
ch_time_machine/window_<id>/
  capture_t0/
    t_bet.final.parquet
    t_bet.non_final.parquet
    query_manifest.json
    fingerprints.json
  capture_t_plus_1h/
    ...
  diffs/
    final_vs_non_final.json
    t0_vs_t_plus_1h.json
    t0_vs_t_plus_24h.json
```

Diff reports must include:

- New rows by business key.
- Removed rows by business key.
- Changed rows by business key and changed column.
- Version column changes.
- `__deleted` / `__op` changes.
- Min/max `payout_complete_dtm`, `gaming_day`, `__etl_insert_Dtm`, `__ts_ms`, `lud_dtm`.

### System Table Permission Probe

At startup, try read-only probes for:

- `system.query_log`
- `system.parts`
- `system.mutations`
- `system.part_log`

Write `permissions_report.json` with:

- success/failure per table
- exception class/message
- sample query used

If accessible, record relevant metadata for each captured query/window:

- query id
- query start/end
- read rows/bytes
- exception information
- active mutations
- involved parts where available

## Component 3: Replay Analyzer

### Responsibilities

The replay analyzer runs offline against the recording bundle and produces evidence-backed reports.

It must support:

- Parquet/JSON-only analysis.
- Exact score replay using captured model bundle and package environment.
- Exact validator replay using captured bundle validator behavior.

### Required Analyses

Produce these reports:

```text
analysis/
  score_replay_diff_report.json
  validator_replay_diff_report.json
  clickhouse_late_arrival_report.json
  final_vs_non_final_report.json
  feature_stage_diff_report.json
  feature_root_cause_rank.json
  false_positive_casebook.parquet
  high_score_casebook.parquet
  all_scored_summary.parquet
```

### Score Replay

For every scored row:

- Rebuild model feature matrix from captured stage artifacts.
- Recompute score using captured model bundle.
- Compare against production score.
- Attribute differences to:
  - raw input mismatch
  - feature stage mismatch
  - Feast lookup mismatch
  - model/runtime mismatch
  - threshold mismatch
  - unknown

### Validator Replay

For every alert:

- Rebuild the exact alert row consumed by validator.
- Rebuild validator ground-truth bet list from captured `t_bet FINAL` query output.
- Re-run bundle validator logic.
- Compare result, reason, `gap_start`, and `gap_minutes`.
- Separately compare against later ClickHouse recaptures to identify late-arrival-driven verdict changes.

### Root-Cause Ranking

Rank candidate causes using evidence, not warnings alone:

- False positive concentration by feature null reason.
- Score drift by feature source layer.
- Precision by freshness status.
- Validator result changes after late-arrival recaptures.
- `FINAL` vs non-`FINAL` impact on scorer and validator rows.
- Distribution shift versus offline test split when test artifacts are available.

## Bundle Layout

The shipped recording bundle should be a directory or zip with this structure:

```text
production_flight_recording_<bundle_id>/
  MANIFEST.json
  README_REPLAY.md
  identity/
    bundle_info.json
    deploy_bundle_paths.json
    model_version
    model_hashes.json
    training_metrics.json
    run_summary.json
    feature_candidate_registry.snapshot.yaml
    runtime_config_snapshot.json
    package_freeze.txt
    git_status.txt
  permissions/
    clickhouse_system_table_permissions.json
  cycles/
    scorer/
      cycle_000001/
      cycle_000002/
    validator/
      cycle_000001/
      cycle_000002/
  ch_time_machine/
    window_000001/
    window_000002/
  state/
    state_db_export/
    prediction_log_db_export/
    feature_state_db_export/
  feast/
    readiness_snapshots/
    refresh_reports/
    registry_snapshots/
    online_store_hashes.json
  source_context/
    t_bet/
    t_session/
    t_game/
  analysis/
    ...
```

## Configuration

Avoid environment-variable-only behavior. Provide a Python or YAML config file inside the deploy bundle, with CLI overrides only for operational convenience.

Recommended config fields:

- recording enabled / disabled
- recording root directory
- capture scorer stages
- capture validator stages
- capture ClickHouse diagnostic requery
- requery schedule
- include non-`FINAL` diagnostics
- include `system.*` probes
- include full population diagnostic baseline
- include allowlist-exact production path
- source context lookback / lookahead
- compression codec
- parquet row group size
- redaction rules
- max cycles or max duration

No credentials should be written to the config or output bundle.

## CLI Surface

Provide model-agnostic CLIs:

```bash
python -m trainer_hightier.serving.production_flight_recorder \
  --bundle-dir /path/to/deploy_bundle \
  --mode shadow \
  --config /path/to/recording_config.yaml
```

```bash
python -m trainer_hightier.serving.ch_time_machine \
  --bundle-dir /path/to/deploy_bundle \
  --recording-root /path/to/recording \
  --config /path/to/recording_config.yaml
```

```bash
python -m trainer_hightier.serving.replay_recording_bundle \
  --recording-root /path/to/recording \
  --output-dir /path/to/analysis
```

Deploy integration can add an optional flag later:

```bash
python main.py --bundle-dir /path/to/deploy_bundle --mode all --record-production-flight
```

## Validation Strategy

### Unit Tests

- Query manifest serialization redacts secrets.
- Stable row fingerprints are deterministic under row ordering differences.
- `FINAL` vs non-`FINAL` diff logic detects duplicate keys and changed values.
- Feature provenance writer handles nulls, decimals, timestamps, and categorical values.
- Replay analyzer detects score mismatches and attributes them to feature matrix differences.

### Integration Tests

- Run recorder against local cleaned Parquet / mocked ClickHouse client.
- Run scorer shadow mode for a tiny fixture and verify all expected stage artifacts exist.
- Run validator shadow capture for a fixture alert and verify ground-truth query output is captured.
- Run replay analyzer and assert score/validator replay matches production fixture outputs.

### Production Dry Run

- Deploy a model in non-serving / shadow-only mode.
- Run recorder for at least one scorer cycle and one validator cycle.
- Confirm the bundle can be opened locally and replayed without production credentials.

## Rollout Plan

### Phase 1: Evidence Capture Skeleton

- Add recording config and bundle layout.
- Capture identity, config, package freeze, hashes, and SQLite exports.
- Capture scorer cycle manifests and core stage Parquets.
- Capture validator query manifests and result Parquets.

### Phase 2: ClickHouse Time Machine

- Add repeated requery scheduler.
- Add `FINAL` vs non-`FINAL` captures.
- Add row/key/value diff reports.
- Add system table permission probes and optional metadata capture.

### Phase 3: Replay Analyzer

- Add score replay from captured model matrix.
- Add validator replay from captured ground-truth windows.
- Add mismatch attribution and casebooks.

### Phase 4: Deploy Integration

- Add deploy CLI flags for shadow recording.
- Add safe defaults and runbook.
- Add debug bundle compatibility so `collect_debug_bundle.py` can include recording manifests when present.

## Risks and Mitigations

- High storage usage: accepted for investigation, but manifest every file with size/hash so partial bundles remain auditable.
- Production ClickHouse load: configurable schedule and query scopes; default can be heavy for incident mode, but must be explicit.
- Credential leakage: never persist credentials; redact SQL connection strings and environment-derived secrets.
- Non-replayable environment: capture wheel, package freeze, model hash, registry hash, and runtime config.
- Incomplete provenance for existing composite features: start with feature source layer and upstream feature ids; refine null reasons incrementally.
- Validator drift during recording: record every validator cycle input/output and pending transitions, not only final results.

## Open Questions

- Should the first implementation live under `trainer_hightier/serving/` or a separate `trainer_hightier/recording/` package?
- Should diagnostic non-`FINAL` queries run for every cycle or only scheduled windows?
- Which full-population diagnostic baseline is acceptable by default: all players, all active players, or allowlist plus high-score neighborhood?
- Should `t_game` be fully mirrored for the entire recording window by default, or only for referenced `game_id` values?
- Should the recorder package write zip files directly, or keep a directory tree and let a separate packager create zip/tar archives?

