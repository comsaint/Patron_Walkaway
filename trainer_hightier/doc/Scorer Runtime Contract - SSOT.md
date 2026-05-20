# Scorer Runtime Contract - SSOT

This document is the current source of truth for `trainer_hightier` scorer packaging and runtime readiness.
Older packaging, self-contained, mid-term snapshot, and registry implementation plans are historical references
unless they agree with this contract.

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

Package-time output may therefore represent a refresh-required bundle.

## Deploy-Time Contract

Production deploy owns readiness:

- load the packaged model and manifest.
- validate source mirrors or ClickHouse access needed to refresh missing/stale layers.
- if required snapshots are missing, invalid, or beyond hard cap, run targeted refresh before scoring.
- fail deploy/scorer readiness if refresh fails.
- stale-but-allowed snapshots may run degraded only within the configured hard cap.

## Scoring-Time Contract

The scorer must never silently fill missing model features:

- every `model.pkl.feature_columns` entry must exist before `predict_proba`.
- required feature families must not be all null after joins.
- wrong-grain or training-scoped mid-term snapshots are hard failures.
- prediction logs should expose snapshot freshness/degraded state.

## Supplier Rules

- `baseline_model`: supplied from raw ClickHouse/scoring input fields.
- `feast_trial_1h`: supplied online by the serving PIT builder; bundled trial parquet is diagnostic only.
- short-term `fe__*`: supplied by `fe_short_term_parquet` or a declared online/micro-batch supplier.
- mid-term `fe__*`: supplied by production-scoped `mid_term_snapshot_parquet` at deploy/scoring time.
- `patron__*__w180d_m1snap`: supplied by canonical slow patron snapshot.

## Non-Goals

- Development packaging does not require latest production data.
- Training snapshots are not production readiness substitutes.
- Packaging is not responsible for rebuilding production snapshots.
