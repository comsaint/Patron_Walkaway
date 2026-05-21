# Scorer Runtime Contract - SSOT

This document is the current source of truth for `trainer_hightier` scorer packaging and runtime readiness.
Older packaging, self-contained, mid-term snapshot, and registry implementation plans are historical references
unless they agree with this contract.

## Compatibility Policy

Scorer v2 drops unnecessary legacy feature-supplier compatibility. The only compatibility intentionally retained is
the external runtime contract needed by operators and downstream consumers:

- keep the scorer entrypoint and supported CLI shape unless a replacement is explicitly approved.
- keep outward `state.db` alerts / validation schema compatibility.
- do not keep production runtime fallbacks to legacy Parquet suppliers.
- test fixtures may use mocks or fixture data through test-only injection, but legacy Parquet must not enter the
  production scorer control flow.

## Objective

Build and ship a deployable scorer bundle from a development environment that may not have current production
data. Production readiness is checked at deploy/scoring time, where production ClickHouse or production source
mirrors are available.

## Package-Time Contract

`build_deploy_package` must verify that the bundle is structurally usable:

- model files, frozen registry, wheel, mapping, allowlist, and manifest are present and readable.
- model feature columns can be classified into known suppliers using the frozen registry.
- training-scoped artifacts must not be accepted as production-safe snapshots.
- optional seed snapshots may be packaged when present.
- missing or stale production-refreshable layers, such as `mid_term_snapshot_parquet`, do not block a dev package.
- `fe_short_term_parquet` may be packaged only as historical or test fixture data; production scorer v2 must not
  read it as a runtime supplier or use it to satisfy readiness for short-term `fe__*`.

Package-time output may therefore represent a refresh-required bundle.

## Deploy-Time Contract

Production deploy owns readiness:

- load the packaged model and manifest.
- validate source mirrors or ClickHouse access needed to refresh missing/stale layers.
- if required snapshots are missing, invalid, or beyond hard cap, run targeted refresh before scoring.
- if the model contains short-term `fe__*`, verify the scorer's bounded on-the-fly PIT supplier supports every
  required short-term column.
- fail deploy/scorer readiness if refresh fails.
- stale-but-allowed snapshots may run degraded only within the configured hard cap.

## Scoring-Time Contract

The scorer must never silently fill missing model features:

- every `model.pkl.feature_columns` entry must exist before `predict_proba`.
- required feature families must not be all null after joins.
- wrong-grain or training-scoped mid-term snapshots are hard failures.
- unsupported short-term `fe__*` are hard failures until the on-the-fly PIT supplier implements them.
- prediction logs should expose snapshot freshness/degraded state and missing-feature counts.

### Scorer v2 Feast missing policy

- **Cell-level NULL** (e.g. structural `prior_*` nulls within an otherwise present entity row): **allowed** for
  `predict_proba`, but prediction log must record per-row missing counts / degraded status.
- **Feast entity row missing** (entire mid-term or long-term family absent from online store for a patron): **skip
  row + write an auditable prediction-log status**; do not score as all-null features.
- **Batch entity-missing rate > 10%**: **hard fail** the scoring cycle (configurable via
  `scorer_feast_entity_missing_fail_fraction`) to surface refresh / key / mapping systemic issues.

## Supplier Rules

- `baseline_model`: supplied from raw ClickHouse/scoring input fields.
- `feast_trial_1h`: supplied online by the serving PIT builder; bundled trial parquet is diagnostic only.
- short-term `fe__*`: supplied by the scorer's bounded on-the-fly PIT builder for the currently deployed model
  feature set only. Unsupported short-term columns are hard failures until explicitly implemented. `fe_short_term_parquet`
  is not a production scorer v2 supplier and must not be wired into production runtime fallback.
- mid-term `fe__*`: **scorer v2 adopted supplier** is Feast online lookup (production-scoped materialization +
  refresh plane). Parquet manifest paths remain for refresh/deploy validation but are not the production runtime
  fallback for scorer v2.
- `patron__*__w180d_m1snap`: **scorer v2 adopted supplier** is Feast online lookup for canonical slow patron
  features. Parquet slow snapshots remain for refresh/materialize but not as a silent runtime substitute when Feast
  is configured.

Production scorer v2 must **not** fallback to legacy `fe_derived_parquet`, `fe_short_term_parquet`, or training
Parquet for model features, silently or explicitly.

## Non-Goals

- Development packaging does not require latest production data.
- Training snapshots are not production readiness substitutes.
- Packaging is not responsible for rebuilding production snapshots.
- Short-term `fe__*` Feast online lookup is not required in scorer v2 first slice; short-term production supply is
  the scorer's bounded on-the-fly PIT builder.

## Related documents

| Layer | Document |
|-------|----------|
| Feast spike decision | `Feast Production Feasibility Spike - DECISION_RECORD.md` |
| Scorer v2 realization | `Scorer v2 Feast Runtime - IMPLEMENTATION_PLAN.md` |
| Feast online refresh realization | `Feast Online Refresh - IMPLEMENTATION_PLAN.md` |
| Execution plan | `Scorer v2 Feast Runtime - WORKING_PLAN.md` |
