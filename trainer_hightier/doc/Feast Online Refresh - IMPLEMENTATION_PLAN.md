# Feast Online Refresh - IMPLEMENTATION_PLAN

This implementation plan defines how scorer v2's Feast online refresh plane is realized. It is subordinate to
`Scorer Runtime Contract - SSOT.md` and complements `Scorer v2 Feast Runtime - IMPLEMENTATION_PLAN.md`.

## Objective

Build a dedicated production orchestration entrypoint that refreshes the Feast online store used by scorer v2.

The entrypoint should live at:

`trainer_hightier/serving/feast_online_refresh.py`

It is responsible for moving production-scoped mid-term and long-term feature values from ClickHouse through local
materialization into Feast online store, then publishing auditable readiness metadata for scorer and deploy gates.

## Scope

Included:

- Refresh mid-term `fe__*` features supplied to scorer v2 by Feast online lookup.
- Refresh long-term `patron__*__w180d_m1snap` features supplied to scorer v2 by Feast online lookup.
- Export bounded production source data from ClickHouse for the ADT allowlist universe.
- Reuse existing production materializers to compute Parquet artifacts and layer metadata.
- Run Feast `apply` by default, then Feast online materialization for the selected layers.
- Publish `feast_online_readiness.json` only after online materialization and smoke validation succeed.
- Persist refresh run / layer / smoke audit metadata in `feature_state.db`.

Excluded:

- No short-term `fe__*` Feast online refresh in the first slice. Scorer v2 supplies supported short-term `fe__*`
  through the bounded PIT builder and fail-fast rejects unsupported short-term columns.
- No scoring-loop refresh. `trainer_hightier.serving.scorer` consumes readiness metadata and Feast online values; it
  must not compute or materialize mid/long features.
- No deploy-time auto repair. Deploy may check readiness and smoke status, but should not silently run a refresh.
- No production dependency on local cleaned Parquet tables. Local cleaned inputs may exist only as explicit debug or
  fixture overrides.

## Decisions

- Production source defaults to `clickhouse`.
- `local_cleaned` inputs are optional debug / fixture overrides, not the production path.
- First slice supports only `mid` and `slow` layers; default is both.
- Export scope is the ADT allowlist universe only, not all patrons.
- Export bounds come from existing config / constants, not ad hoc CLI values.
- Feast `apply` is enabled by default, with an explicit skip flag if operators need it.
- Feast materialization ranges are derived from the current artifact anchor / generated metadata.
- Readiness is published only after the full selected-layer online refresh and smoke checks succeed.
- Smoke missing entity rate follows scorer policy: over `scorer_feast_entity_missing_fail_fraction` hard-fails.
- Refresh observability is stored in `feature_state.db`; JSON summary can remain as a latest human-readable artifact
  but is not the operational source of truth.

## Module Boundaries

### `feast_online_refresh.py`

Production orchestration CLI. It coordinates source export, materialization, Feast apply/materialize, readiness
publication, smoke checks, and feature-state DB audit writes.

It should be a thin orchestration layer. Heavy computation and low-level feature logic should stay in existing
materializer / adapter modules.

### `production_materialize.py`

Low-level production materializer library. It continues to compute production-scoped mid-term and slow patron
artifacts from staging Parquet inputs.

The refresh orchestration may call:

- `materialize_production_mid_term_daily_snapshot`
- `materialize_production_slow_canonical_asof`
- `resolve_production_canonical_mapping`

It should not become the orchestration CLI.

### `feast_readiness.py`

Readiness model, merge/write helpers, and deploy/scorer readiness gate logic. The refresh orchestration should reuse
the existing readiness model and publish final production readiness after Feast online materialization succeeds.

### `feast_online_adapter.py`

Runtime Feast online lookup boundary. The refresh orchestration may reuse small online lookup smoke helpers, but should
avoid depending on scorer runtime state.

### `feature_state_store.py`

Operational metadata DB owner. It should be extended with Feast refresh audit tables rather than creating a separate
DB or writing refresh metadata into `prediction_log.db`.

## Production Flow

### 1. Resolve Runtime Inputs

Resolve from config and explicit CLI overrides:

- Feast repo path.
- Feast readiness path.
- Feature-state DB path.
- Canonical mapping Parquet.
- ADT allowlist Parquet.
- ClickHouse source table names / connection config.
- Output staging directory under the existing Feast artifacts area.

CLI path overrides are allowed for testability, but production defaults should not rely on environment variables.

### 2. Select Layers

Support:

- `mid`
- `slow`

Reject unsupported layers explicitly. Do not accept `short` or `fe_short_term` as aliases, because short-term Feast
online refresh is out of scope for scorer v2 first slice.

### 3. Export Source Data From ClickHouse

The production default is bounded ClickHouse export into staging Parquet.

For `mid`:

- Export minimal bet columns needed by the mid-term materializer.
- Filter by computed gaming-day bounds from existing config / constants.
- Filter to ADT allowlist players before export or by bounded player-id chunks.
- Preserve the spike-tested shape used by `feast_mid_term_spike.py`, but copy and consolidate only the small export
  logic needed for production orchestration.

For `slow`:

- Export minimal session columns needed by the slow patron materializer.
- Filter by configured slow lookback bounds.
- Filter to ADT allowlist players by bounded chunks.
- Preserve the spike-tested shape used by `feast_long_term_spike.py`, but do not make production code depend on the
  `feature_experiment` spike runner.

ClickHouse query design should filter by gaming day and allowlist/chunks before any expensive join or aggregation. Per
`query-join-filter-before`, production export queries should avoid joining broad tables and filtering afterwards.

### 4. Materialize Production Artifacts

Call existing production materializer functions using the staged ClickHouse export outputs.

The materializers should continue to own:

- DuckDB computation.
- Production-scoped artifact metadata.
- Row-count and anchor metadata.
- Bounded runtime configuration for laptop / small production box constraints.

The orchestration should fail fast on empty outputs, missing required columns, invalid grain, or stale anchors.

### 5. Feast Apply and Online Materialize

Run Feast `apply` by default before materialization. Operators may explicitly skip it if Feast registry deployment is
managed elsewhere.

Then materialize selected feature views into the online store:

- mid-term feature view for scorer v2 mid `fe__*`.
- long-term slow patron feature view for `patron__*__w180d_m1snap`.

Materialization windows should be derived from current artifact metadata, especially anchor dates and generated time,
not from arbitrary wall-clock ranges.

### 6. Online Smoke Validation

After Feast online materialization, run a small allowlist sample lookup.

Smoke must verify:

- Feast online store is reachable.
- Entity key type and feature refs match scorer expectations.
- Required mid/slow columns are returned.
- Entity present rate is within policy.
- Missing entity rate does not exceed `scorer_feast_entity_missing_fail_fraction`.

Default sample size should be conservative, for example 100 canonical IDs, with CLI override for larger release-gate
runs.

### 7. Publish Readiness

Publish `feast_online_readiness.json` only after selected layers pass materialization and smoke.

Readiness should include:

- schema version.
- generated timestamp.
- Feast repo path.
- layer readiness for mid and / or slow.
- source scope = production.
- anchor max.
- row count and feature columns.
- feature view name.
- lookup sample size / entity present rate when available.
- materialize source = Feast online refresh orchestration.

The scorer startup gate and deploy preflight continue to read this readiness document.

### 8. Persist Feature-State Audit

Persist refresh metadata in `feature_state.db`, not `prediction_log.db`.

Proposed tables:

`feast_refresh_run`

| Column | Purpose |
|--------|---------|
| `run_id` | Stable refresh run identifier |
| `started_at` / `finished_at` | Run timing |
| `status` | `running`, `ok`, or `error` |
| `source` | `clickhouse` or explicit debug source |
| `layers` | Selected layer list |
| `feast_repo` | Feast repo path |
| `readiness_path` | Final readiness JSON path |
| `apply_seconds` | Feast apply wall time |
| `materialize_seconds` | Total Feast materialize wall time |
| `summary_json` | Compact JSON payload for details not worth first-class columns |

`feast_refresh_layer`

| Column | Purpose |
|--------|---------|
| `run_id` | Parent refresh run |
| `layer` | `mid` or `slow` |
| `artifact_path` | Production artifact path used for Feast materialization |
| `row_count` | Materialized artifact rows |
| `anchor_gaming_day_max` | Latest anchor represented by the layer |
| `source_scope` | Must be `production` |
| `feature_view` | Feast feature view |
| `export_rows` / `export_seconds` | ClickHouse export metrics |
| `compute_seconds` | Local materialization wall time |
| `smoke_sample_size` | Online smoke sample size |
| `smoke_entity_present_rate` | Online smoke present rate |
| `status` | Layer result |
| `detail_json` | Layer-specific diagnostic payload |

Optional later table:

`feast_refresh_smoke`

Use only if combined smoke detail becomes too large or too frequently queried for `feast_refresh_layer.detail_json`.

## CLI Shape

Recommended first CLI:

```bash
python -m trainer_hightier.serving.feast_online_refresh \
  --layers mid,slow \
  --source clickhouse \
  --apply \
  --materialize \
  --smoke
```

Suggested options:

- `--layers mid,slow`
- `--source clickhouse`
- `--source local_cleaned`
- `--skip-apply`
- `--skip-materialize`
- `--smoke-only`
- `--dry-run`
- `--feast-repo PATH`
- `--readiness-path PATH`
- `--canonical-mapping PATH`
- `--adt-allowlist PATH`
- `--local-cleaned-bet PATH`
- `--local-cleaned-session PATH`
- `--max-smoke-entities 100`
- `--summary-path PATH`

`--local-cleaned-bet` and `--local-cleaned-session` should be valid only for explicit local/debug source modes.

## Failure Policy

Fail fast on:

- ClickHouse export failure.
- Empty source export for a selected layer.
- Export row cap breach.
- Missing canonical mapping or ADT allowlist.
- Materializer validation failure.
- Feast apply failure.
- Feast materialize failure.
- Online smoke failure.
- Readiness gate failure.
- Source scope not production.

Warnings are acceptable only for optional human-readable summary artifacts. Operational DB writes and readiness
publication are part of the core contract.

## Rollout Phases

### Phase 1: Orchestration Skeleton

Create the CLI, layer parsing, config resolution, run ID, feature-state DB audit scaffolding, and dry-run summary.

### Phase 2: ClickHouse Export

Copy and consolidate the minimal, spike-tested ClickHouse export logic for mid and slow into the production
orchestration boundary. Keep export bounded by ADT allowlist and existing lookback constants.

### Phase 3: Production Materialization

Wire staged ClickHouse exports into the existing production materializers and capture artifact metadata per layer.

### Phase 4: Feast Apply / Materialize

Run Feast apply and selected-layer online materialization using artifact-derived ranges.

### Phase 5: Smoke and Readiness

Run allowlist online smoke, publish final `feast_online_readiness.json`, and record final DB audit rows.

### Phase 6: Deploy Alignment

Keep deploy behavior as check-only: deploy validates readiness and optional smoke status, but does not run refresh
implicitly.

## Risks and Mitigations

- Risk: mid + slow full refresh can be slow or memory-heavy on a small production box.
  - Mitigation: ADT allowlist scope only; bounded player chunks; DuckDB runtime config; record export and compute
    timings per layer.
- Risk: readiness is published too early after Parquet computation but before online store is usable.
  - Mitigation: publish final readiness only after Feast online materialization and smoke pass.
- Risk: production code accidentally depends on spike modules.
  - Mitigation: copy and consolidate the small CH export logic into the production script; do not import the spike
    runners as production dependencies.
- Risk: feature view names still carry spike naming.
  - Mitigation: keep view names explicit in readiness metadata; production renaming can be a separate migration.
- Risk: smoke sample is too small to catch coverage gaps.
  - Mitigation: default to a small startup sample, but allow larger release-gate sample size via CLI.

## Open Questions

- Whether production Feast feature views should be renamed before the first scheduled refresh rollout, or whether the
  existing spike names remain accepted for scorer v2 first slice.
- Whether `feature_state.db` should keep only latest summary rows plus full detail JSON, or retain all historical
  refresh rows indefinitely.
