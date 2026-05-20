# Production Snapshot Serving — Manifest Inventory

> Historical reference. The current scorer packaging/runtime source of truth is
> [`Scorer Runtime Contract - SSOT.md`](Scorer%20Runtime%20Contract%20-%20SSOT.md).
> If this document conflicts with that SSOT, follow the SSOT.

Phase 0 inventory for `active_manifest.json` compatibility during production snapshot rollout.

## Legacy keys (compatibility only)

| Key | Purpose |
|-----|---------|
| `version` | Manifest publish id |
| `slow_patron_parquet` | Long-term slow patron artifact path |
| `fe_derived_parquet` | Legacy monolithic `fe__*` artifact path; retained only as compatibility alias / debug fallback |
| `trial_bet_behavior_parquet` | Optional diagnostic trial parquet |
| `adt_allowlist_parquet` | High-ADT allowlist |
| `adt_allowlist_version` | Allowlist sha256/version |
| `coverage_end_exclusive` | Legacy wall-clock freshness fallback; new mid-term checks prefer `mid_term_coverage_end_exclusive` |
| `training_cutoff_iso` | Training cutoff metadata |
| `fe_derived_source_kind` | `production_clickhouse` when Route B materialized production-compatible fe suppliers; `shipped_training_bundle` is not sufficient for production feature-supply readiness |
| `slow_patron_grain` | `canonical_asof` for production slow patron |

Legacy `fe_derived_parquet` must not be the only release gate for new models. Packaging and deploy preflight must split `source: fe_derived` by frozen registry cadence / `allowed_training_supplier`:

- short-term `fe__*` → `fe_short_term_parquet` or serving online/micro-batch supplier.
- mid-term `fe__*` → `mid_term_snapshot_parquet` with production scope, `canonical_daily_asof` grain, and freshness metadata.

## New per-layer keys

| Key | Purpose |
|-----|---------|
| `mid_term_snapshot_parquet` | Canonical daily mid-term snapshot (`canonical_id + anchor_gaming_day`) |
| `mid_term_grain` | Expected `canonical_daily_asof` |
| `mid_term_anchor_gaming_day_max` | Latest anchor in mid-term artifact |
| `mid_term_coverage_end_exclusive` | Mid-term refresh publish timestamp |
| `mid_term_generated_at` | Mid-term artifact generation timestamp |
| `mid_term_stale_hard_cap_days` | Hard cap (default 3) |
| `fe_short_term_parquet` | Explicit short-term bet-grain parquet alias |
| `slow_anchor_gaming_day_max` | Latest slow monthly anchor |
| `slow_generated_at` | Slow artifact generation timestamp |
| `slow_monthly_grace_days` | Monthly grace (default 1) |
| `slow_stale_hard_cap_days` | Hard cap (default 3) |
| `sha256_by_layer` | Optional per-layer content hashes |

## Deploy Refresh Supervisor

- `trainer_hightier.deploy.main` starts a refresh supervisor by default in scorer-capable modes (`all` and `scorer`).
- **Startup (blocking):** only **hard failures** trigger synchronous repair before scorer startup: missing/invalid artifacts, or staleness past hard cap. If targeted refresh fails, scorer-capable deploy fails fast.
- **Startup (non-blocking):** `stale_allowed` does not block startup; the background supervisor retries immediately and on each poll.
- **Background loop** (default poll 300s):
  - Mid-term: after 04:00 local, refresh when anchor does not cover `D - 1`; retry every poll until success.
  - Slow monthly: at most **once per calendar day**; refresh when anchor is missing or stale per monthly grace / hard cap.
- Refresh jobs validate **production source mirrors** before materialization:
  - `source_mirror/cleaned_bet/` — compact rolling cleaned bet partitions (mid-term + short-term).
  - `source_mirror/cleaned_session.parquet` — compact cleaned session mirror (slow monthly).
- Paths and retention are defined in `trainer_hightier.config` (`production_cleaned_bet_mirror_dir`, `production_cleaned_session_mirror_parquet`, retention days); not environment variables.
- Slow monthly anchor is **previous calendar month-end gaming day** (`MAX(gaming_day)` per patron per month); production refresh on the 1st uses data through that anchor.
- The supervisor writes staging artifacts first, validates them, and only then atomically publishes `active_manifest.json`.
- A bundle-local `.snapshot_refresh_supervisor.lock` prevents multiple deploy processes from materializing and publishing snapshots concurrently. Stale lock cleanup is governed by `snapshot_refresh_lock_stale_minutes`.
- `--no-refresh-supervisor` is available only for debug or deployments that intentionally use an external scheduler.

## Feature state observability (`feature_state_meta`)

| Key | Purpose |
|-----|---------|
| `refresh_supervisor_last_check_iso` | Last background supervisor poll timestamp (UTC ISO) |
| `mid_term_refresh_last_attempt_iso` | Last mid-term refresh attempt (startup or background) |
| `slow_refresh_last_attempt_iso` | Last slow refresh attempt |
| `slow_refresh_last_check_day` | Last calendar day (HK) slow refresh eligibility was evaluated |
| `source_mirror_bet_status` | Latest cleaned bet mirror validation summary |
| `source_mirror_session_status` | Latest cleaned session mirror validation summary |

## Compatibility aliases

- When `fe_short_term_parquet` is absent, readers may fall back to `fe_derived_parquet` only for legacy/debug bundles. New production bundles should publish `fe_short_term_parquet` explicitly when short-term `fe__*` depends on parquet.
- When `mid_term_coverage_end_exclusive` is absent, readers fall back to `coverage_end_exclusive`.
- When `mid_term_snapshot_parquet` is absent, production mid-term ASOF join is unavailable. Legacy bet-grain `fe_derived` mid-term columns may be used only in legacy/debug mode and must not satisfy the new cadence-aware production gate.
- Training-scoped snapshots (`snapshot_scope=training_step4_only` or missing/unsafe scope metadata) must not be copied into production `active_manifest.json` as `mid_term_snapshot_parquet`.

## Supplier mapping

| Feature family | Production supplier | Grain |
|----------------|--------------------|-------|
| `bet__*__w1h` | Online PIT builder | bet / event |
| Short-term `fe__*` | `fe_short_term_parquet` or serving online/micro-batch supplier; `fe_derived_parquet` only as legacy alias | bet_id |
| Mid-term `fe__*` | `mid_term_snapshot_parquet` | canonical_id + anchor_gaming_day |
| `patron__*__w180d_m1snap` | `slow_patron_parquet` | canonical ASOF |

## Packaging / Deploy Preflight Rules

- Build and deploy preflight must classify model features using the frozen registry, not just the manifest keys.
- A model with only short-term `fe__*` may pass with `fe_short_term_parquet` and no `mid_term_snapshot_parquet`.
- A model with any mid-term `fe__*` must fail if `mid_term_snapshot_parquet` is missing, stale, wrong grain, or not production-scoped.
- A manifest with `fe_derived_source_kind=shipped_training_bundle` is not production-ready for Route B mid-term feature serving unless a valid production `mid_term_snapshot_parquet` is also present.
